"""Contas de usuário: cadastro, login (e-mail OU telefone + senha), "minha agenda" e gestão de acessos.

Dois níveis de acesso (coluna `papel` da aba Usuarios):
  cliente   -> agenda no site, vê só os próprios agendamentos e o histórico (/minha-conta)
  barbeiro  -> tudo isso + painel administrativo (/admin) e gestão de usuários

Segurança, em resumo:
  * senha guardada só como hash (scrypt); nunca em texto, nunca devolvida por nenhuma rota
  * o papel/ativo é relido da planilha a cada requisição (cache de 20 s): rebaixar ou desativar vale quase na hora
  * trocar a senha derruba as outras sessões (a sessão guarda uma "impressão digital" do hash)
  * limite de tentativas por IP e por login, mensagem genérica ("login ou senha incorretos")
  * rotas POST só aceitam JSON (bloqueia formulários de outros sites) + cookie SameSite=Lax
Acesso de emergência: login "admin" + variável ADMIN_PASSWORD (deixe vazia para desligar).
Registrado no app.py: contas.register(app, <módulo do app>) — antes do admin_panel.
"""
import hashlib
import logging
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import timedelta
from functools import wraps

from flask import g, jsonify, redirect, render_template, request, session
from werkzeug.security import check_password_hash, generate_password_hash

log = logging.getLogger("barbearia.contas")

PAPEIS = ("cliente", "barbeiro")
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")
SENHAS_FRACAS = {"12345678", "123456789", "1234567890", "11111111", "00000000", "87654321", "12341234",
                 "senha123", "senha1234", "password", "password1", "qwertyui", "qwerty123", "abc12345",
                 "admin123", "barbearia", "barbearia123", "iloveyou", "mudar123"}
SENHA_ALFABETO = "abcdefghjkmnpqrstuvwxyz23456789"  # sem letras/números que se confundem (0 O 1 l I)


class Erro(Exception):
    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.msg, self.status = msg, status


def register(app, core):
    lock = threading.Lock()
    falhas_ip, falhas_login = defaultdict(deque), defaultdict(deque)
    cadastros_ip = defaultdict(deque)
    dummy = []

    # ------------------------------------------------------------------ senhas
    def hash_senha(s):
        try:
            return generate_password_hash(s, method="scrypt")
        except (ValueError, AttributeError):  # Python sem scrypt
            return generate_password_hash(s, method="pbkdf2:sha256:600000")

    def confere(h, s):
        try:
            return bool(h) and check_password_hash(h, s)
        except Exception:
            return False

    def hash_falso():  # para gastar o mesmo tempo quando o login não existe (não revela quem tem conta)
        if not dummy:
            dummy.append(hash_senha(secrets.token_hex(8)))
        return dummy[0]

    def fp(s):
        return hashlib.sha256((s or "").encode()).hexdigest()[:16]

    def validar_senha(s, *pessoais):
        if not isinstance(s, str) or len(s) < 8:
            raise Erro("A senha precisa ter pelo menos 8 caracteres.")
        if len(s) > 128:
            raise Erro("A senha pode ter no máximo 128 caracteres.")
        if s.lower() in SENHAS_FRACAS or len(set(s)) < 3:
            raise Erro("Essa senha é fácil demais de adivinhar. Escolha outra.")
        if any(p and s.lower() == p.lower() for p in pessoais):
            raise Erro("A senha não pode ser igual ao seu e-mail ou telefone.")

    # ------------------------------------------------------------------ validação
    def txt(v, n):
        return re.sub(r"\s+", " ", "" if v is None else str(v)).strip()[:n]

    def norm_email(s):
        s = txt(s, 200).lower()
        if not s:
            return ""
        if len(s) > 120 or not EMAIL_RE.match(s):
            raise Erro("E-mail inválido.")
        return s

    def destino(n):
        """Só aceita caminhos do próprio site (impede redirecionar para outro domínio)."""
        n = (n or "").strip()
        if (not n.startswith("/")) or n.startswith("//") or "\\" in n or len(n) > 200 or any(ord(c) < 32 for c in n):
            return ""
        return n

    def papel_de(u):
        return "barbeiro" if str(u.get("papel", "")).strip().lower() in ("barbeiro", "admin") else "cliente"

    # ------------------------------------------------------------------ limite de tentativas (por instância)
    def bloqueado(q, limite, janela=900):
        t = time.time()
        while q and t - q[0] > janela:
            q.popleft()
        return len(q) >= limite

    def falhou(d, chave):
        if len(d) > 5000:
            d.clear()
        d[chave].append(time.time())

    def ip():
        return request.remote_addr or "?"

    # ------------------------------------------------------------------ usuário da sessão
    def carregar():
        if session.get("master"):
            if core.ADMIN_PASSWORD and core._same(session.get("pv", ""), fp(core.ADMIN_PASSWORD)):
                return {"id": "master", "nome": "Administrador", "papel": "barbeiro", "email": "", "telefone": "",
                        "master": True}
            session.clear()
            return None
        uid = session.get("uid")
        if not uid:
            return None
        try:
            u = next((r for r in core.table("Usuarios", 20) if r.get("id") == uid), None)
        except Exception:
            log.exception("ler usuários")
            return None  # na dúvida, trata como não logado
        if not u or not core.is_active(u) or not core._same(session.get("pv", ""), fp(u.get("senha_hash", ""))):
            session.clear()
            return None
        u = dict(u)
        u["papel"] = papel_de(u)
        return u

    def usuario_atual():
        if "_usuario" not in g:
            g._usuario = carregar()
        return g._usuario

    def publico(u):
        return {"id": u["id"], "nome": u.get("nome", ""), "email": u.get("email", ""),
                "telefone": u.get("telefone", ""), "papel": u["papel"]}

    def abrir_sessao(u):
        session.clear()
        session.permanent = True
        session["uid"], session["pv"] = u["id"], fp(u.get("senha_hash", ""))
        g.pop("_usuario", None)

    core.usuario_atual = usuario_atual  # o admin_panel e o agendamento usam
    core.papel_de = papel_de

    # ------------------------------------------------------------------ planilha
    def usuarios(fresco=False):
        return core.table("Usuarios", 0 if fresco else 20)

    def salvar(rownum, **mudancas):
        header = core._cache_header("Usuarios")
        atual = next((r for r in core.table("Usuarios", 0) if r["_row"] == rownum), None)
        if atual is None:
            raise Erro("Usuário não encontrado (a planilha mudou). Atualize a tela.", 404)
        row = {**atual, **mudancas}
        core.get_backend().update_row("Usuarios", rownum, [row.get(h, "") for h in header])
        core.invalidate("Usuarios")

    def achar_login(ident):
        ident = ident.strip()
        if "@" in ident:
            e = ident.lower()
            return next((u for u in usuarios() if u.get("email", "").lower() == e), None)
        tel = core.norm_phone(ident)
        return next((u for u in usuarios() if tel and core.norm_phone(u.get("telefone")) == tel), None) if tel else None

    def conflito(email, tel, ignorar_id=None):
        for u in usuarios(True):
            if u.get("id") == ignorar_id:
                continue
            if email and u.get("email", "").lower() == email:
                raise Erro("Este e-mail já tem cadastro. Entre na sua conta.", 409)
            if tel and core.norm_phone(u.get("telefone")) == tel:
                raise Erro("Este telefone já tem cadastro. Entre na sua conta.", 409)

    # ------------------------------------------------------------------ decorador das rotas
    def route(path, *, login=True, staff=False, **kw):
        def deco(fn):
            @wraps(fn)
            def wrapper(*a, **k):
                if request.method == "POST" and not request.is_json:
                    return jsonify(error="Requisição inválida."), 415
                u = usuario_atual()
                if (login or staff) and not u:
                    return jsonify(error="Entre na sua conta.", login=True), 401
                if staff and u["papel"] != "barbeiro":
                    return jsonify(error="Acesso restrito aos barbeiros."), 403
                try:
                    return fn(u, *a, **k)
                except Erro as e:
                    return jsonify(error=e.msg), e.status
                except Exception:
                    log.exception("contas %s", fn.__name__)
                    return jsonify(error="Não foi possível falar com o Google agora. Tente de novo em instantes."), 503
            app.add_url_rule(path, "contas_" + fn.__name__, wrapper, **kw)
            return fn
        return deco

    def body():
        return request.get_json(silent=True) or {}

    def nao_cache(resp):
        resp.headers["Cache-Control"] = "no-store"
        return resp

    # ------------------------------------------------------------------ páginas
    @app.route("/entrar")
    def pagina_entrar():
        u, nxt = usuario_atual(), destino(request.args.get("next"))
        if u:
            return redirect(nxt or ("/admin" if u["papel"] == "barbeiro" else "/minha-conta"))
        return render_template("entrar.html", shop=core.shop(), next=nxt, criar=bool(request.args.get("criar")))

    @app.route("/login")
    def pagina_login_antiga():
        return redirect("/entrar" + ("?next=/admin" if request.args.get("admin") else ""))

    @app.route("/minha-conta")
    def pagina_conta():
        u = usuario_atual()
        if not u:
            return redirect("/entrar?next=/minha-conta")
        return nao_cache(app.make_response(render_template("conta.html", shop=core.shop())))

    @app.route("/logout")
    def sair():
        session.clear()
        return redirect("/")

    # ------------------------------------------------------------------ API: cadastro / login
    @route("/api/me", login=False)
    def me(u):
        return nao_cache(jsonify(user=publico(u) if u else None))

    @route("/api/auth/register", login=False, methods=["POST"])
    def cadastrar(_):
        j = body()
        if j.get("website"):  # isca para robôs
            raise Erro("Não foi possível criar a conta.")
        if int(core.num(j.get("elapsed"), 0)) < 2500:
            raise Erro("Preencha com calma e tente de novo.")
        if j.get("consent") is not True:
            raise Erro("É necessário aceitar a Política de Privacidade.")
        if bloqueado(cadastros_ip[ip()], 5, 3600):
            raise Erro("Muitos cadastros deste aparelho. Tente mais tarde.", 429)
        nome, tel, email = txt(j.get("nome"), 80), core.norm_phone(j.get("telefone")), norm_email(j.get("email"))
        if len(nome) < 2:
            raise Erro("Informe seu nome.")
        if not tel:
            raise Erro("Informe um WhatsApp válido com DDD.")
        senha = j.get("senha")
        validar_senha(senha, email, tel)
        agora = core.now()
        novo = {"id": "u_" + secrets.token_hex(5), "nome": nome, "email": email, "telefone": tel,
                "senha_hash": hash_senha(senha), "papel": "cliente", "ativo": "sim",
                "consentimento": f"sim v{core.PRIVACY_VERSION} {agora.isoformat(timespec='seconds')}",
                "criado_em": agora.isoformat(timespec="seconds")}
        with lock:
            conflito(email, tel)
            core.append_row("Usuarios", novo)
            cadastros_ip[ip()].append(time.time())
            # outra instância pode ter cadastrado o mesmo e-mail/telefone ao mesmo tempo: a primeira linha fica
            iguais = [u for u in usuarios(True)
                      if (email and u.get("email", "").lower() == email) or core.norm_phone(u.get("telefone")) == tel]
            if len(iguais) > 1 and iguais[0]["id"] != novo["id"]:
                minha = next(u for u in iguais if u["id"] == novo["id"])
                core.get_backend().update_row("Usuarios", minha["_row"], [""] * len(core._cache_header("Usuarios")))
                core.invalidate("Usuarios")
                raise Erro("Este e-mail ou telefone já tem cadastro. Entre na sua conta.", 409)
        novo["papel"] = "cliente"
        abrir_sessao(novo)
        return jsonify(ok=True, user=publico(novo)), 201

    @route("/api/auth/login", login=False, methods=["POST"])
    def entrar(_):
        j = body()
        ident, senha = txt(j.get("login"), 120), j.get("senha")
        senha = senha if isinstance(senha, str) else ""
        if len(senha) > 128:
            raise Erro("Login ou senha incorretos.", 401)
        chave = ident.lower()
        if bloqueado(falhas_ip[ip()], 20) or bloqueado(falhas_login[chave], 5):
            raise Erro("Muitas tentativas. Aguarde 15 minutos e tente de novo.", 429)
        if chave == "admin" and core.ADMIN_PASSWORD:  # acesso de emergência
            if core._same(senha, core.ADMIN_PASSWORD):
                session.clear()
                session.permanent = True
                session["master"], session["pv"] = True, fp(core.ADMIN_PASSWORD)
                g.pop("_usuario", None)
                return jsonify(ok=True, user={"id": "master", "nome": "Administrador", "papel": "barbeiro"})
            falhou(falhas_ip, ip())
            falhou(falhas_login, chave)
            raise Erro("Login ou senha incorretos.", 401)
        u = achar_login(ident) if ident else None
        certo = confere(u.get("senha_hash", "") if u else hash_falso(), senha) and u is not None
        if not certo:
            falhou(falhas_ip, ip())
            falhou(falhas_login, chave)
            raise Erro("Login ou senha incorretos.", 401)
        if not core.is_active(u):
            raise Erro("Esta conta está desativada. Fale com a barbearia.", 403)
        falhas_login.pop(chave, None)
        u = dict(u)
        u["papel"] = papel_de(u)
        abrir_sessao(u)
        return jsonify(ok=True, user=publico(u))

    @route("/api/auth/logout", login=False, methods=["POST"])
    def api_sair(_):
        session.clear()
        return jsonify(ok=True)

    # ------------------------------------------------------------------ API: minha conta
    @route("/api/conta/agendamentos")
    def meus_agendamentos(u):
        if u.get("master"):
            return nao_cache(jsonify(proximos=[], historico=[]))
        n = core.now()
        ini, fim = n - timedelta(days=400), n + timedelta(days=core.MAX_DAYS_AHEAD + 60)
        proximos, historico, vistos = [], [], set()
        for cid in core.all_calendars():
            for ev in core.get_calendar().list_events(cid, ini, fim, private={"usuario_id": u["id"]}):
                a = core.agenda.appt_from_event(ev)
                if not a or a.get("usuario_id") != u["id"] or (cid, ev["id"]) in vistos:
                    continue
                vistos.add((cid, ev["id"]))
                st = core.appt_start(a)
                futuro = bool(st and st >= n and a["status"] == "agendado")
                item = {"codigo": a["codigo"], "data": a["data"], "hora": a["hora"], "servico": a["servico"],
                        "barbeiro": a["barbeiro"], "preco": core.num(a["preco"]), "status": a["status"],
                        "futuro": futuro, "pode_alterar": futuro and core.change_state(a)[0],
                        "link": f"/agendamento/{a['codigo']}"}
                (proximos if futuro else historico).append(item)
        proximos.sort(key=lambda x: (x["data"], x["hora"]))
        historico.sort(key=lambda x: (x["data"], x["hora"]), reverse=True)
        return nao_cache(jsonify(proximos=proximos, historico=historico[:100]))

    @route("/api/conta/perfil", methods=["POST"])
    def atualizar_perfil(u):
        if u.get("master"):
            raise Erro("O acesso de emergência não tem perfil.")
        j = body()
        nome, tel, email = txt(j.get("nome"), 80), core.norm_phone(j.get("telefone")), norm_email(j.get("email"))
        if len(nome) < 2:
            raise Erro("Informe seu nome.")
        if not tel:
            raise Erro("Informe um WhatsApp válido com DDD.")
        with lock:
            conflito(email, tel, ignorar_id=u["id"])
            salvar(u["_row"], nome=nome, telefone=tel, email=email)
        g.pop("_usuario", None)
        return jsonify(ok=True, user=publico({**u, "nome": nome, "telefone": tel, "email": email}))

    @route("/api/conta/senha", methods=["POST"])
    def trocar_senha(u):
        if u.get("master"):
            raise Erro("Troque a ADMIN_PASSWORD nas variáveis de ambiente da Vercel.")
        j = body()
        if bloqueado(falhas_login["id:" + u["id"]], 5):
            raise Erro("Muitas tentativas. Aguarde 15 minutos.", 429)
        if not confere(u.get("senha_hash", ""), j.get("atual") if isinstance(j.get("atual"), str) else ""):
            falhou(falhas_login, "id:" + u["id"])
            raise Erro("Senha atual incorreta.", 403)
        nova = j.get("nova")
        validar_senha(nova, u.get("email", ""), u.get("telefone", ""))
        h = hash_senha(nova)
        salvar(u["_row"], senha_hash=h)
        session["pv"] = fp(h)  # esta sessão continua; as outras caem
        g.pop("_usuario", None)
        return jsonify(ok=True)

    # ------------------------------------------------------------------ API: gestão de usuários (só barbeiros)
    @route("/api/admin/users", staff=True)
    def listar_usuarios(u):
        out = [{"id": r.get("id"), "nome": r.get("nome"), "email": r.get("email"), "telefone": r.get("telefone"),
                "papel": papel_de(r), "ativo": core.is_active(r), "criado_em": r.get("criado_em", "")[:10],
                "voce": r.get("id") == u["id"]} for r in usuarios(True) if r.get("id")]
        out.sort(key=lambda x: x["criado_em"], reverse=True)
        return nao_cache(jsonify(out))

    def alvo(u, uid):
        if uid == u["id"]:
            raise Erro("Você não pode alterar o seu próprio acesso aqui.")
        r = next((r for r in usuarios(True) if r.get("id") == uid), None)
        if r is None:
            raise Erro("Usuário não encontrado.", 404)
        return r

    @route("/api/admin/users/<uid>", staff=True, methods=["POST"])
    def alterar_usuario(u, uid):
        j, r, mud = body(), alvo(u, uid), {}
        if "papel" in j:
            if j["papel"] not in PAPEIS:
                raise Erro("Nível de acesso inválido.")
            mud["papel"] = j["papel"]
        if "ativo" in j:
            mud["ativo"] = "sim" if j["ativo"] is True else "nao"
        if not mud:
            raise Erro("Nada para alterar.")
        salvar(r["_row"], **mud)
        return jsonify(ok=True)

    @route("/api/admin/users/<uid>/senha", staff=True, methods=["POST"])
    def senha_temporaria(u, uid):
        r = alvo(u, uid)
        temp = "".join(secrets.choice(SENHA_ALFABETO) for _ in range(10))
        salvar(r["_row"], senha_hash=hash_senha(temp))
        return nao_cache(jsonify(ok=True, senha=temp))
