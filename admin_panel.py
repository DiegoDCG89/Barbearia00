"""Painel administrativo (/admin): lê e grava nas planilhas e no Google Agenda.

Não tem banco próprio. Tudo o que o painel faz é chamar as mesmas funções do app (`core`):
  Agenda            -> Google Agenda (eventos do site)         concluir / faltou / cancelar
  Resumo e Lançamentos -> planilha financeira (aba Lancamentos)
  Estoque           -> planilha de estoque (Produtos, Movimentacoes)
  Cadastros         -> abas Servicos e Barbeiros
  Horários e folgas -> abas Horarios e Folgas
  Site              -> aba Config
Registrado no fim do app.py: admin_panel.register(app, <módulo do app>).
"""
import logging
import math
import re
import secrets
from collections import defaultdict
from datetime import datetime, timedelta
from functools import wraps

from flask import jsonify, redirect, render_template, request

log = logging.getLogger("barbearia.admin")

FORMAS = ("dinheiro", "pix", "debito", "credito")
DIAS = ("seg", "ter", "qua", "qui", "sex", "sab", "dom")
CONFIG_KEYS = {"nome": 120, "nome_curto": 40, "marca": 6, "endereco": 200,
               "whatsapp": 20, "instagram": 60, "descricao": 300}
# colunas que precisam ficar NUMÉRICAS na planilha (senão SUMIFS e a fórmula do estoque não somam)
NUMERIC = {"Lancamentos": ("valor",), "Servicos": ("minutos", "preco"),
           "Produtos": ("preco", "estoque_minimo"), "Movimentacoes": ("quantidade",)}
# Fórmula da coluna `estoque` de Produtos, independente do nº da linha (usa ROW())
ESTOQUE_FORMULA = (
    '=SUMIFS(Movimentacoes!$D:$D,Movimentacoes!$B:$B,INDIRECT("A"&ROW()),Movimentacoes!$C:$C,"entrada")'
    '-SUMIFS(Movimentacoes!$D:$D,Movimentacoes!$B:$B,INDIRECT("A"&ROW()),Movimentacoes!$C:$C,"saida")'
    '+SUMIFS(Movimentacoes!$D:$D,Movimentacoes!$B:$B,INDIRECT("A"&ROW()),Movimentacoes!$C:$C,"ajuste")')


class Erro(Exception):
    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.msg, self.status = msg, status


def register(app, core):
    # ------------------------------------------------------------------ infraestrutura
    def api(fn):
        """Exige login, JSON nos POST (bloqueia formulários de outros sites) e trata erros."""
        @wraps(fn)
        def wrapper(*a, **kw):
            u = core.usuario_atual()
            if not u:
                return jsonify(error="Sessão expirada. Entre de novo."), 401
            if u["papel"] != "barbeiro":
                return jsonify(error="Acesso restrito aos barbeiros."), 403
            if request.method == "POST" and not request.is_json:
                return jsonify(error="Requisição inválida."), 415
            try:
                return fn(*a, **kw)
            except Erro as e:
                return jsonify(error=e.msg), e.status
            except Exception:
                log.exception("admin %s", fn.__name__)
                return jsonify(error="Não foi possível falar com o Google agora. Tente de novo em instantes."), 503
        return wrapper

    def route(path, **kw):
        def deco(fn):
            app.add_url_rule(path, "admin_" + fn.__name__, api(fn), **kw)
            return fn
        return deco

    def body():
        return request.get_json(silent=True) or {}

    # ------------------------------------------------------------------ validação
    def txt(v, n, nome="o campo", obrigatorio=False):
        s = re.sub(r"\s+", " ", "" if v is None else str(v)).strip()[:n]
        if obrigatorio and not s:
            raise Erro(f"Preencha {nome}.")
        return s

    def seguro(s):
        """Texto que vai para uma célula com fórmulas habilitadas (Produtos): neutraliza = + - @."""
        return "'" + s if s[:1] in ("=", "+", "-", "@") else s

    def dinheiro(v, nome, minimo=0.0, maximo=1e7):
        x = core.num(v, None)
        if x is None or not math.isfinite(x) or x < minimo or x > maximo:
            raise Erro(f"Valor inválido em {nome}.")
        return round(x, 2)

    def data_iso(v, nome="a data"):
        d = core.parse_date(v)
        if not d:
            raise Erro(f"Data inválida em {nome}.")
        return d.isoformat()

    def hora(v, nome="o horário"):
        s = str(v or "").strip()
        if not s:
            return ""
        t = core.parse_time(s)
        if t is None:
            raise Erro(f"Horário inválido em {nome}.")
        return core.fmt_time(t)

    def mes_arg():
        m = request.args.get("month") or core.now().strftime("%Y-%m")
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", m):
            raise Erro("Mês inválido.")
        return m

    def novo_id(prefixo):
        return f"{prefixo}_{secrets.token_hex(3)}"

    def sim_nao(v):
        return "sim" if v in (True, 1, "1", "sim", "true", "on") else "nao"

    def barbeiro_id(v, permitir_todos=False):
        v = str(v or "").strip()
        if permitir_todos and v in ("", "*"):
            return "*"
        if not any(r.get("id") == v for r in core.table("Barbeiros", 0)):
            raise Erro("Barbeiro não encontrado.")
        return v

    # ------------------------------------------------------------------ escrita nas planilhas
    def coerce(tab, row):
        for c in NUMERIC.get(tab, ()):
            v = row.get(c, "")
            if v not in ("", None):
                x = core.num(v, None)
                row[c] = "" if x is None or not math.isfinite(x) else (int(x) if x == int(x) else x)
        return row

    def add(tab, row, formulas=False):
        core.append_row(tab, coerce(tab, dict(row)), formulas=formulas)

    def save_row(tab, rownum, changes):
        """Atualiza uma linha (só os campos em `changes`). Em Produtos nunca toca a coluna da fórmula."""
        header = core._cache_header(tab)
        atual = next((r for r in core.table(tab, 0) if r["_row"] == rownum), None)
        if atual is None:
            raise Erro("Registro não encontrado (a planilha mudou). Atualize a tela.", 404)
        row = coerce(tab, {**atual, **changes})
        vals = [row.get(h, "") for h in header]
        if tab == "Produtos" and header and header[-1] == "estoque":
            vals = vals[:-1]
        core.get_backend().update_row(tab, rownum, vals)
        core.invalidate(tab)

    def achar(tab, campo, valor):
        r = next((r for r in core.table(tab, 0) if r.get(campo) == valor), None)
        if r is None:
            raise Erro("Registro não encontrado.", 404)
        return r

    # ------------------------------------------------------------------ página e atalhos
    @app.route("/admin")
    def admin_panel():
        u = core.usuario_atual()
        if not u:
            return redirect("/entrar?next=/admin")
        if u["papel"] != "barbeiro":
            return ('<meta name="viewport" content="width=device-width,initial-scale=1">'
                    '<body style="font-family:Manrope,system-ui,sans-serif;background:#090706;color:#ede7dd;text-align:center;padding:15vh 20px">'
                    '<h2 style="font:300 2.6rem Georgia,serif;margin:0 0 .5rem">Acesso restrito aos barbeiros</h2>'
                    '<p style="color:#938b7f">Sua conta é de cliente.</p>'
                    '<p><a style="color:#d7a85b" href="/minha-conta">Minha conta</a> · '
                    '<a style="color:#d7a85b" href="/">Voltar ao site</a></p></body>'), 403
        return render_template("admin.html", shop=core.shop(), eu=u)

    @route("/api/admin/links")
    def links():
        out = {"agenda": "https://calendar.google.com/calendar/u/0/r"}
        for k in ("financeiro", "estoque", "cadastro"):
            try:
                out[k] = core.get_backend().url(k)
            except Exception:
                out[k] = ""
        return jsonify(out)

    # ------------------------------------------------------------------ resumo do mês
    @route("/api/admin/summary")
    def summary():
        mes = mes_arg()
        rec = desp = 0.0
        atend = sem_forma = 0
        forma, barb, dia = defaultdict(float), defaultdict(float), defaultdict(float)
        for r in core.table("Lancamentos", 0):
            d = core.parse_date(r.get("data"))
            if not d or d.strftime("%Y-%m") != mes:
                continue
            v, tipo = core.num(r.get("valor")), r.get("tipo", "").lower()
            if tipo == "despesa":
                desp += v
            elif tipo == "receita":
                rec += v
                f = r.get("forma_pgto", "").lower()
                if f not in FORMAS:
                    sem_forma += 1
                    f = "sem_forma"
                forma[f] += v
                barb[r.get("barbeiro") or "—"] += v
                dia[d.day] += v
                if r.get("categoria", "").lower() == "servico":
                    atend += 1
        return jsonify(
            mes=mes, receitas=round(rec, 2), despesas=round(desp, 2), saldo=round(rec - desp, 2),
            atendimentos=atend, ticket_medio=round(rec / atend, 2) if atend else 0,
            por_forma={k: round(v, 2) for k, v in forma.items()}, sem_forma=sem_forma,
            por_barbeiro=sorted(({"nome": k, "total": round(v, 2)} for k, v in barb.items()),
                                key=lambda x: -x["total"]),
            por_dia=[{"dia": k, "total": round(v, 2)} for k, v in sorted(dia.items())])

    # ------------------------------------------------------------------ lançamentos
    @route("/api/admin/transactions", methods=["GET", "POST"])
    def transactions():
        if request.method == "GET":
            mes, out = mes_arg(), []
            for r in core.table("Lancamentos", 0):
                d = core.parse_date(r.get("data"))
                if d and d.strftime("%Y-%m") == mes:
                    out.append({"id": r.get("id"), "data": d.isoformat(), "tipo": r.get("tipo", "").lower(),
                                "categoria": r.get("categoria"), "descricao": r.get("descricao"),
                                "valor": core.num(r.get("valor")), "forma_pgto": r.get("forma_pgto", "").lower(),
                                "barbeiro": r.get("barbeiro"), "origem": r.get("origem"),
                                "_k": r.get("criado_em", "")})
            out.sort(key=lambda x: (x["data"], x["_k"]), reverse=True)
            for x in out:
                x.pop("_k")
            return jsonify(out[:400])
        j = body()
        tipo = txt(j.get("tipo"), 10).lower()
        if tipo not in ("receita", "despesa"):
            raise Erro("Escolha receita ou despesa.")
        forma = txt(j.get("forma_pgto"), 10).lower()
        if forma and forma not in FORMAS:
            raise Erro("Forma de pagamento inválida.")
        add("Lancamentos", {
            "id": novo_id("l"), "data": data_iso(j.get("data")), "tipo": tipo,
            "categoria": txt(j.get("categoria"), 40) or "outros",
            "descricao": txt(j.get("descricao"), 120, "a descrição", True),
            "valor": dinheiro(j.get("valor"), "valor", 0.01), "forma_pgto": forma,
            "barbeiro": txt(j.get("barbeiro"), 60), "origem": "painel",
            "criado_em": core.now().isoformat(timespec="seconds")})
        return jsonify(ok=True), 201

    @route("/api/admin/transactions/<tid>", methods=["POST"])
    def transaction_edit(tid):
        j, r = body(), achar("Lancamentos", "id", tid)
        ch = {}
        if "valor" in j:
            ch["valor"] = dinheiro(j["valor"], "valor", 0.0)
        if "forma_pgto" in j:
            f = txt(j["forma_pgto"], 10).lower()
            if f and f not in FORMAS:
                raise Erro("Forma de pagamento inválida.")
            ch["forma_pgto"] = f
        if "descricao" in j:
            ch["descricao"] = txt(j["descricao"], 120, "a descrição", True)
        if "categoria" in j:
            ch["categoria"] = txt(j["categoria"], 40) or "outros"
        save_row("Lancamentos", r["_row"], ch)
        return jsonify(ok=True)

    # ------------------------------------------------------------------ agenda do dia
    @route("/api/admin/appointments")
    def appointments():
        d = core.parse_date(request.args.get("day")) or core.now().date()
        ini = datetime(d.year, d.month, d.day, tzinfo=core.TZ)
        vistos, out = set(), []
        for cid in core.all_calendars():
            for ev in core.get_calendar().list_events(cid, ini, ini + timedelta(days=1)):
                a = core.agenda.appt_from_event(ev)
                if not a or (cid, ev["id"]) in vistos:
                    continue
                vistos.add((cid, ev["id"]))
                fim = ev["end"].strftime("%H:%M")
                out.append({"codigo": a["codigo"], "hora": a["hora"], "fim": fim, "cliente": a["cliente"],
                            "telefone": a["telefone"], "servico": a["servico"], "barbeiro": a["barbeiro"],
                            "preco": core.num(a["preco"]), "status": a["status"]})
        out.sort(key=lambda x: x["hora"])
        return jsonify(out)

    # ------------------------------------------------------------------ venda de produtos (avulsa e junto com o atendimento)
    def itens_venda(raw, max_linhas=30):
        """Valida [{product, quantidade}] e devolve [(linha_do_produto, qtd)], juntando repetidos.
        Confere o estoque de TODOS antes de gravar qualquer coisa (tudo ou nada)."""
        if raw in (None, []):
            return []
        if not isinstance(raw, list) or len(raw) > max_linhas:
            raise Erro("Lista de produtos inválida.")
        juntos = {}
        for it in raw:
            if not isinstance(it, dict):
                raise Erro("Lista de produtos inválida.")
            pid = str(it.get("product") or "")
            juntos[pid] = juntos.get(pid, 0) + int(dinheiro(it.get("quantidade"), "quantidade", 1, 1000))
        produtos = {r.get("id"): r for r in core.table("Produtos", 0)}
        niveis = core.stock_levels()
        out = []
        for pid, q in juntos.items():
            p = produtos.get(pid)
            if p is None:
                raise Erro("Produto não encontrado.", 404)
            if niveis.get(pid, 0) < q:
                raise Erro(f"Estoque insuficiente de {p.get('nome')} (restam {int(niveis.get(pid, 0))}).", 409)
            out.append((p, q))
        return out

    def vender(itens, forma, barbeiro, data, origem, motivo):
        """Dá baixa no estoque e lança uma receita (categoria produto) por item. Devolve o total vendido."""
        agora, total = core.now().isoformat(timespec="seconds"), 0.0
        for p, q in itens:
            movimento(p["id"], "saida", q, motivo)
            valor = round(core.num(p.get("preco")) * q, 2)
            add("Lancamentos", {
                "id": novo_id("v"), "data": data, "tipo": "receita", "categoria": "produto",
                "descricao": f"{p['nome']} x{q}", "valor": valor, "forma_pgto": forma,
                "barbeiro": barbeiro, "origem": origem(p), "criado_em": agora})
            total += valor
        return round(total, 2)

    @route("/api/admin/appointments/<code>/status", methods=["POST"])
    def appointment_status(code):
        j = body()
        novo = txt(j.get("status"), 12).lower()
        if novo not in ("concluido", "faltou", "cancelado"):
            raise Erro("Status inválido.")
        forma = txt(j.get("method"), 10).lower()
        if novo == "concluido" and forma not in FORMAS:
            raise Erro("Escolha a forma de pagamento.")
        desconto = dinheiro(j.get("discount") or 0, "desconto")
        gorjeta = dinheiro(j.get("tip") or 0, "gorjeta")
        itens = itens_venda(j.get("products")) if novo == "concluido" else []  # produtos vendidos junto com o corte
        with core._book_lock:
            core.invalidate_cal()
            a = core.find_appt(code)
            if not a:
                raise Erro("Agendamento não encontrado.", 404)
            if a["status"] != "agendado":
                raise Erro("Este agendamento já foi encerrado.", 409)
            if novo == "cancelado":
                core.get_calendar().delete(a["_cal"], a["_event"])
                core.invalidate_cal()
            elif novo == "faltou":
                core.patch_appt(a, status="faltou")
            else:
                n = core.now()
                if not any(r.get("origem") == code for r in core.table("Lancamentos", 0)):
                    add("Lancamentos", {
                        "id": code, "data": a["data"], "tipo": "receita", "categoria": "servico",
                        "descricao": f"{a['servico']} – {a['cliente']}",
                        "valor": max(0.0, core.num(a.get("preco")) - desconto), "forma_pgto": forma,
                        "barbeiro": a["barbeiro"], "origem": code, "criado_em": n.isoformat(timespec="seconds")})
                if gorjeta > 0:
                    add("Lancamentos", {
                        "id": novo_id("g"), "data": a["data"], "tipo": "receita", "categoria": "gorjeta",
                        "descricao": f"Gorjeta – {a['cliente']}", "valor": gorjeta, "forma_pgto": forma,
                        "barbeiro": a["barbeiro"], "origem": "gorjeta:" + code,
                        "criado_em": n.isoformat(timespec="seconds")})
                if itens:
                    # se uma tentativa anterior já lançou estes produtos, não repete (nem baixa o estoque de novo)
                    ja = {r.get("origem") for r in core.table("Lancamentos", 0)}
                    novos = [(pr, q) for pr, q in itens if f"venda:{code}:{pr['id']}" not in ja]
                    vender(novos, forma, a["barbeiro"], a["data"], lambda pr: f"venda:{code}:{pr['id']}", "venda com atendimento")
                core.patch_appt(a, status="concluido")
        return jsonify(ok=True)

    # ------------------------------------------------------------------ cadastros: serviços e barbeiros
    @route("/api/admin/services", methods=["GET", "POST"])
    def services():
        if request.method == "GET":
            return jsonify([{"id": r.get("id"), "nome": r.get("nome"), "descricao": r.get("descricao"),
                             "minutos": int(core.num(r.get("minutos"), 30)), "preco": core.num(r.get("preco")),
                             "ativo": core.is_active(r)} for r in core.table("Servicos", 0) if r.get("id")])
        j = body()
        dados = {"nome": txt(j.get("nome"), 60, "o nome", True), "descricao": txt(j.get("descricao"), 160),
                 "minutos": int(dinheiro(j.get("minutos"), "duração", 5, 480)),
                 "preco": dinheiro(j.get("preco"), "preço"), "ativo": sim_nao(j.get("ativo", True))}
        if j.get("id"):
            save_row("Servicos", achar("Servicos", "id", str(j["id"]))["_row"], dados)
        else:
            add("Servicos", {"id": novo_id("s"), **dados})
        return jsonify(ok=True)

    @route("/api/admin/barbers", methods=["GET", "POST"])
    def barbers():
        if request.method == "GET":
            return jsonify([{"id": r.get("id"), "nome": r.get("nome"), "funcao": r.get("funcao"),
                             "calendario": r.get("calendario"), "ativo": core.is_active(r)}
                            for r in core.table("Barbeiros", 0) if r.get("id")])
        j = body()
        dados = {"nome": txt(j.get("nome"), 60, "o nome", True), "funcao": txt(j.get("funcao"), 60),
                 "calendario": txt(j.get("calendario"), 200), "ativo": sim_nao(j.get("ativo", True))}
        if j.get("id"):
            save_row("Barbeiros", achar("Barbeiros", "id", str(j["id"]))["_row"], dados)
        else:
            add("Barbeiros", {"id": novo_id("b"), **dados})
        core.invalidate_cal()
        return jsonify(ok=True)

    # ------------------------------------------------------------------ estoque
    @route("/api/admin/products", methods=["GET", "POST"])
    def products():
        if request.method == "GET":
            niveis = core.stock_levels()
            out = []
            for r in core.table("Produtos", 0):
                if not r.get("id"):
                    continue
                q = niveis.get(r["id"], 0)
                minimo = core.num(r.get("estoque_minimo"))
                out.append({"id": r["id"], "nome": r.get("nome"), "preco": core.num(r.get("preco")),
                            "estoque_minimo": minimo, "ativo": core.is_active(r),
                            "estoque": q, "baixo": q <= minimo})
            return jsonify(out)
        j = body()
        dados = {"nome": seguro(txt(j.get("nome"), 60, "o nome", True)),
                 "preco": dinheiro(j.get("preco"), "preço"),
                 "estoque_minimo": dinheiro(j.get("estoque_minimo") or 0, "estoque mínimo", 0, 100000),
                 "ativo": sim_nao(j.get("ativo", True))}
        if j.get("id"):
            save_row("Produtos", achar("Produtos", "id", str(j["id"]))["_row"], dados)
        else:
            add("Produtos", {"id": novo_id("p"), **dados, "estoque": ESTOQUE_FORMULA}, formulas=True)
        return jsonify(ok=True)

    def movimento(pid, tipo, qtd, motivo):
        add("Movimentacoes", {"data": core.now().date().isoformat(), "produto_id": pid, "tipo": tipo,
                              "quantidade": qtd, "motivo": motivo,
                              "criado_em": core.now().isoformat(timespec="seconds")})

    @route("/api/admin/stock", methods=["POST"])
    def stock():
        j = body()
        p = achar("Produtos", "id", str(j.get("product", "")))
        tipo = txt(j.get("tipo"), 10).lower()
        if tipo not in ("entrada", "saida", "ajuste"):
            raise Erro("Tipo de movimentação inválido.")
        qtd = dinheiro(j.get("quantidade"), "quantidade", -100000 if tipo == "ajuste" else 0.01, 100000)
        if qtd == 0:
            raise Erro("Informe a quantidade.")
        if tipo == "saida" and core.stock_levels().get(p["id"], 0) < qtd:
            raise Erro("Estoque insuficiente para essa saída.", 409)
        movimento(p["id"], tipo, qtd, txt(j.get("motivo"), 80) or tipo)
        return jsonify(ok=True)

    @route("/api/admin/sales", methods=["POST"])
    def sale():
        """Venda avulsa. Aceita {items:[{product, quantidade}], method, barbeiro} (carrinho)
        ou o formato antigo de um produto só: {product, quantidade, method, barbeiro}."""
        j = body()
        raw = j.get("items")
        if raw is None:
            raw = [{"product": j.get("product"), "quantidade": j.get("quantidade")}]
        itens = itens_venda(raw)
        if not itens:
            raise Erro("Adicione pelo menos um produto.")
        forma = txt(j.get("method"), 10).lower()
        if forma not in FORMAS:
            raise Erro("Escolha a forma de pagamento.")
        total = vender(itens, forma, txt(j.get("barbeiro"), 60), core.now().date().isoformat(), lambda pr: "venda", "venda")
        return jsonify(ok=True, total=total)

    # ------------------------------------------------------------------ horários e folgas
    @route("/api/admin/hours", methods=["GET", "POST"])
    def hours():
        if request.method == "GET":
            b = request.args.get("barber", "*") or "*"
            linhas = core.table("Horarios", 0)
            out = []
            for i, dia in enumerate(DIAS):
                r = next((r for r in linhas if core.day_index(r.get("dia")) == i
                          and (r.get("barbeiro") or "*") == b), {})
                def norm(k):
                    t = core.parse_time(r.get(k, ""))
                    return "" if t is None else core.fmt_time(t)
                out.append({"dia": dia, "abre": norm("abre"), "fecha": norm("fecha"),
                            "almoco_ini": norm("almoco_ini"), "almoco_fim": norm("almoco_fim")})
            return jsonify(out)
        j = body()
        b = barbeiro_id(j.get("barber"), permitir_todos=True)
        dias = j.get("days") or []
        if len(dias) != 7:
            raise Erro("Dados incompletos.")
        for i, d in enumerate(dias):
            vals = {"abre": hora(d.get("abre"), "abre"), "fecha": hora(d.get("fecha"), "fecha"),
                    "almoco_ini": hora(d.get("almoco_ini"), "almoço"), "almoco_fim": hora(d.get("almoco_fim"), "almoço")}
            if bool(vals["abre"]) != bool(vals["fecha"]):
                raise Erro(f"{DIAS[i].upper()}: preencha abre e fecha (ou deixe os dois vazios para fechado).")
            if vals["abre"] and core.parse_time(vals["fecha"]) <= core.parse_time(vals["abre"]):
                raise Erro(f"{DIAS[i].upper()}: o fechamento deve ser depois da abertura.")
            if bool(vals["almoco_ini"]) != bool(vals["almoco_fim"]):
                raise Erro(f"{DIAS[i].upper()}: preencha início e fim do almoço (ou nenhum).")
            linhas = core.table("Horarios", 0)
            r = next((r for r in linhas if core.day_index(r.get("dia")) == i and (r.get("barbeiro") or "*") == b), None)
            if r:
                save_row("Horarios", r["_row"], vals)
            elif vals["abre"] or b != "*":  # barbeiro sem linha herdaria o horário geral; linha vazia = fechado
                add("Horarios", {"barbeiro": b, "dia": DIAS[i], **vals})
        return jsonify(ok=True)

    @route("/api/admin/timeoff", methods=["GET", "POST"])
    def timeoff():
        if request.method == "GET":
            hoje, out = core.now().date(), []
            for r in core.table("Folgas", 0):
                d = core.parse_date(r.get("data"))
                if d and d >= hoje:
                    out.append({"row": r["_row"], "barbeiro": r.get("barbeiro") or "*", "data": d.isoformat(),
                                "inicio": r.get("inicio", ""), "fim": r.get("fim", ""), "motivo": r.get("motivo", "")})
            out.sort(key=lambda x: x["data"])
            return jsonify(out)
        j = body()
        ini, fim = hora(j.get("inicio"), "início"), hora(j.get("fim"), "fim")
        if bool(ini) != bool(fim):
            raise Erro("Preencha início e fim, ou deixe os dois vazios para o dia todo.")
        if ini and core.parse_time(fim) <= core.parse_time(ini):
            raise Erro("O fim deve ser depois do início.")
        add("Folgas", {"barbeiro": barbeiro_id(j.get("barbeiro"), True), "data": data_iso(j.get("data")),
                       "inicio": ini, "fim": fim, "motivo": txt(j.get("motivo"), 80)})
        return jsonify(ok=True), 201

    @route("/api/admin/timeoff/delete", methods=["POST"])
    def timeoff_delete():
        j = body()
        r = next((r for r in core.table("Folgas", 0) if r["_row"] == j.get("row")), None)
        if not r or core.parse_date(r.get("data")) != core.parse_date(j.get("data")):
            raise Erro("Folga não encontrada (a planilha mudou). Atualize a tela.", 404)
        # linha em branco = removida (table() ignora linhas vazias)
        core.get_backend().update_row("Folgas", r["_row"], [""] * len(core._cache_header("Folgas")))
        core.invalidate("Folgas")
        return jsonify(ok=True)

    # ------------------------------------------------------------------ dados do site (aba Config)
    @route("/api/admin/config", methods=["GET", "POST"])
    def config():
        if request.method == "GET":
            linhas = {r.get("chave"): r.get("valor", "") for r in core.table("Config", 0)}
            out = {k: linhas.get(k, "") for k in CONFIG_KEYS}
            out["intervalo_min"] = linhas.get("intervalo_min") or "30"
            return jsonify(out)
        j = body()
        novos = {k: txt(j[k], n) for k, n in CONFIG_KEYS.items() if k in j}
        if "intervalo_min" in j:
            novos["intervalo_min"] = str(int(dinheiro(j["intervalo_min"], "intervalo", 5, 120)))
        if "nome" in novos and not novos["nome"]:
            raise Erro("O nome da barbearia não pode ficar vazio.")
        for k, v in novos.items():
            r = next((r for r in core.table("Config", 0) if r.get("chave") == k), None)
            if r:
                save_row("Config", r["_row"], {"valor": v})
            else:
                add("Config", {"chave": k, "valor": v})
        core.invalidate("Config")
        return jsonify(ok=True)
