"""Barbearia – site de agendamento: Google Agenda (horários), planilha de financeiro e planilha de estoque."""
import json
import logging
import os
import re
import secrets
import threading
import time
import unicodedata
from collections import defaultdict, deque
from datetime import date, datetime, timedelta
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, redirect, render_template, request, url_for, session
from werkzeug.middleware.proxy_fix import ProxyFix

import sheets  # Importa as suas funções
import agenda
from sheets import SCHEMA, Sheets

log = logging.getLogger("barbearia")
app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=int(os.environ.get("PROXY_HOPS", "1")))

# Chave secreta obrigatória para o uso de 'session' no Flask
app.secret_key = os.environ.get("SECRET_KEY", "chave_super_secreta_barbearia")

TZ = ZoneInfo("America/Sao_Paulo")
MAX_DAYS_AHEAD = int(os.environ.get("MAX_DAYS_AHEAD", "30"))
MAX_OPEN_PER_PHONE = int(os.environ.get("MAX_OPEN_PER_PHONE", "2"))
BOOK_PER_IP_HOUR = int(os.environ.get("BOOK_PER_IP_HOUR", "10"))
CHANGE_DEADLINE_HOURS = int(os.environ.get("CHANGE_DEADLINE_HOURS", "2"))
MAX_RESCHEDULES = int(os.environ.get("MAX_RESCHEDULES", "3"))
MIN_FILL_MS = 3000
PRIVACY_VERSION = "1.0"
TTL_CATALOG = 60   # segundos (Config, serviços, horários...)
TTL_APPTS = 15     # segundos (eventos do dia na agenda; é invalidado a cada gravação nossa)
SYNC_MINUTES = int(os.environ.get("SYNC_MINUTES", "10"))           # de quanto em quanto tempo lança atendimentos feitos
SYNC_LOOKBACK_DAYS = int(os.environ.get("SYNC_LOOKBACK_DAYS", "14"))  # até quantos dias atrás procura atendimentos

backend = None  # planilhas; criado sob demanda (os testes injetam um backend falso)
calendar_backend = None  # Google Agenda; idem
_cache = {}
_cache_lock = threading.RLock()
_book_lock = threading.Lock()
_hits = defaultdict(deque)


def get_backend():
    global backend
    if backend is None:
        backend = Sheets()
    return backend


def get_calendar():
    global calendar_backend
    if calendar_backend is None:
        calendar_backend = agenda.CalendarBackend()
    return calendar_backend


# ------------------------------------------------------------------ leitura em cache
def table(tab, ttl=TTL_CATALOG):
    """Devolve a aba como lista de dicts (com '_row' = nº da linha na planilha)."""
    with _cache_lock:
        hit = _cache.get(tab)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        raw = get_backend().values(tab)
        header = [h.strip() for h in raw[0]] if raw else SCHEMA[tab]
        rows = []
        for i, line in enumerate(raw[1:], start=2):
            if not any(c.strip() for c in line):
                continue
            line = list(line) + [""] * (len(header) - len(line))
            d = {h: line[j].strip() for j, h in enumerate(header)}
            d["_row"] = i
            rows.append(d)
        _cache[tab] = (time.time(), rows, header)
        return rows


def invalidate(tab):
    with _cache_lock:
        _cache.pop(tab, None)


def invalidate_cal():
    with _cache_lock:
        for k in [k for k in _cache if isinstance(k, tuple)]:
            del _cache[k]


# ------------------------------------------------------------------ utilidades
def now():
    return datetime.now(TZ)

def num(v, default=0.0):
    try:
        return float(str(v).replace("R$", "").replace(" ", "").replace(".", "").replace(",", ".")
                     if "," in str(v) else str(v).replace("R$", "").strip())
    except ValueError:
        return default

# ------------------------------------------------------------------ ROTAS DE ADMINISTRAÇÃO

@app.route('/admin', methods=['GET'])
def admin_panel():
    if not session.get('admin_logado'):
        return redirect('/login')
    
    # O admin.html original precisa destas variáveis para ser renderizado sem dar Erro 500
    try:
        lancamentos = table("Lancamentos", ttl=0)
    except Exception as e:
        log.error(f"Erro ao ler Lancamentos: {e}")
        lancamentos = []
    
    dados = []
    for l in lancamentos:
        dados.append({
            'nome': l.get('descricao', ''),
            'servico': l.get('categoria', ''),
            'data': l.get('data', ''),
            'status': l.get('tipo', '')
        })
        
    # Adicionamos shop=shop() e css_url=css_url() para satisfazer o template HTML
    return render_template('admin.html', dados=dados, shop=shop(), css_url=css_url(), demo=False)
    
    # Lê a aba "Lancamentos" da folha de cálculo usando a função nativa do projeto
    try:
        lancamentos = table("Lancamentos", ttl=0) # ttl=0 para obter sempre os dados mais recentes
    except Exception as e:
        log.error(f"Erro ao ler Lancamentos: {e}")
        lancamentos = []
    
    # Adapta os dados reais da folha de cálculo para o formato que o admin.html espera
    dados = []
    for l in lancamentos:
        dados.append({
            'nome': l.get('descricao', ''),
            'servico': l.get('categoria', ''),
            'data': l.get('data', ''),
            'status': l.get('tipo', '')
        })
        
    return render_template('admin.html', dados=dados)


@app.route('/admin/salvar', methods=['POST'])
def salvar_dados():
    if not session.get('admin_logado'):
        return redirect('/login')
    
    # Prepara a nova linha no formato exato que o seu sheets.py (SCHEMA) exige
    novo_registro = {
        "id": secrets.token_urlsafe(6),
        "data": request.form.get('data', '')[:10], # Garante o formato AAAA-MM-DD
        "tipo": request.form.get('status', ''),
        "categoria": request.form.get('servico', ''),
        "descricao": request.form.get('nome', ''),
        "valor": "0", # Adicione um campo de valor no formulário HTML mais tarde, se desejar
        "forma_pgto": "",
        "barbeiro": "",
        "origem": "painel_admin",
        "criado_em": now().isoformat(timespec="seconds")
    }
    
    # Grava a linha na aba "Lancamentos"
    append_row("Lancamentos", novo_registro)
    
    return redirect('/admin')

# ------------------------------------------------------------------ FIM ROTAS ADMIN

# ------------------------------------------------------------------ API DO PAINEL DE CADASTROS

@app.route('/api/admin/barbers', methods=['GET', 'POST'])
def api_admin_barbers():
    if not session.get('admin_logado'): return jsonify(error="Não autorizado"), 401
    
    if request.method == 'GET':
        res = []
        for r in table("Barbeiros", ttl=0):
            res.append({
                "id": r.get("id"),
                "name": r.get("nome"),
                "role": r.get("funcao"),
                "calendar_id": r.get("calendario"),
                "active": 1 if is_active(r) else 0
            })
        return jsonify(res)
        
    if request.method == 'POST':
        data = request.get_json() or {}
        if not data.get("id"):  # É um novo cadastro
            novo_id = "b_" + secrets.token_hex(3)
            append_row("Barbeiros", {
                "id": novo_id,
                "nome": data.get("name", ""),
                "funcao": data.get("role", ""),
                "calendario": data.get("calendar_id", ""),
                "ativo": "sim"
            })
            return jsonify(ok=True)
        return jsonify(error="A edição de registos antigos deve ser feita na folha de cálculo. Os cadastros novos funcionam por aqui!"), 400

@app.route('/api/admin/services', methods=['GET', 'POST'])
def api_admin_services():
    if not session.get('admin_logado'): return jsonify(error="Não autorizado"), 401
    if request.method == 'GET':
        res = []
        for r in table("Servicos", ttl=0):
            res.append({
                "id": r.get("id"),
                "name": r.get("nome"),
                "description": r.get("descricao"),
                "price": num(r.get("preco")),
                "minutes": int(num(r.get("minutos"))),
                "active": 1 if is_active(r) else 0
            })
        return jsonify(res)
    
    if request.method == 'POST':
        data = request.get_json() or {}
        if not data.get("id"):
            append_row("Servicos", {
                "id": "s_" + secrets.token_hex(3),
                "nome": data.get("name", ""),
                "descricao": data.get("description", ""),
                "minutos": data.get("minutes", "30"),
                "preco": data.get("price", "0"),
                "ativo": "sim"
            })
            return jsonify(ok=True)
        return jsonify(error="A edição deve ser feita no Sheets. Novos serviços funcionam!"), 400

@app.route('/api/admin/products', methods=['GET'])
def api_admin_products():
    if not session.get('admin_logado'): return jsonify(error="Não autorizado"), 401
    return jsonify([]) # Retorno vazio para não travar o painel

@app.route('/api/admin/gcal', methods=['GET'])
def api_admin_gcal():
    if not session.get('admin_logado'): return jsonify(error="Não autorizado"), 401
    return jsonify(ok=True, email="Integrado com as suas planilhas")

# ------------------------------------------------------------------



def is_active(row):
    return row.get("ativo", "").strip().lower() not in ("nao", "não", "n", "0", "false", "off", "inativo")


def parse_time(s):
    m = re.fullmatch(r"(\d{1,2})(?:[:hH](\d{2})?)?", (s or "").strip())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    return h * 60 + mi if h < 24 and mi < 60 else None


def fmt_time(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def parse_date(s):
    s = (s or "").strip()
    for f in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            pass
    return None


DAY_KEYS = {"seg": 0, "ter": 1, "qua": 2, "qui": 3, "sex": 4, "sab": 5, "dom": 6}
DAY_LABELS = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]
DAY_EN = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def day_index(s):
    s = unicodedata.normalize("NFD", (s or "").lower()).encode("ascii", "ignore").decode()
    return DAY_KEYS.get(s[:3])


def digits(s):
    return re.sub(r"\D", "", s or "")


def norm_phone(s):
    d = digits(s)
    if len(d) >= 12 and d.startswith("55"):
        d = d[2:]
    return d if len(d) in (10, 11) else None


def cfg(key, default=""):
    for r in table("Config"):
        if r.get("chave") == key and r.get("valor"):
            return r["valor"]
    return os.environ.get("SHOP_" + key.upper(), default)


def shop():
    wa = digits(cfg("whatsapp"))
    if wa and len(wa) <= 11:
        wa = "55" + wa
    ig = cfg("instagram").lstrip("@").strip()
    name = cfg("nome", "Barbearia")
    return {
        "name": name, "short_name": cfg("nome_curto", name), "brand": cfg("marca", name[:2]),
        "address": cfg("endereco"), "tagline": cfg("descricao"),
        "wa_link": f"https://wa.me/{wa}" if wa else "",
        "instagram_url": f"https://instagram.com/{ig}" if ig else "",
    }


def base_url():
    return (os.environ.get("PUBLIC_URL") or request.url_root).rstrip("/")


def css_url():
    return url_for("static", filename="tailwind.css") if os.path.exists(
        os.path.join(app.static_folder, "tailwind.css")) else None


def step_minutes():
    return max(5, int(num(cfg("intervalo_min", "30"), 30)))


# ------------------------------------------------------------------ expediente e horários livres
def shift_for(barber_id, d):
    """(abre, fecha, almoço_ini, almoço_fim) em minutos, ou None se fechado."""
    rows = [r for r in table("Horarios") if day_index(r.get("dia")) == d.weekday()]
    mine = [r for r in rows if r.get("barbeiro") == barber_id]
    r = (mine or [r for r in rows if r.get("barbeiro") in ("*", "")] or [None])[0]
    if not r:
        return None
    ab, fe = parse_time(r.get("abre")), parse_time(r.get("fecha"))
    if ab is None or fe is None or fe <= ab:
        return None
    return ab, fe, parse_time(r.get("almoco_ini")), parse_time(r.get("almoco_fim"))


def appt_start(a):
    d, t = parse_date(a.get("data")), parse_time(a.get("hora"))
    if not d or t is None:
        return None
    return datetime(d.year, d.month, d.day, t // 60, t % 60, tzinfo=TZ)


def occupying(a):
    return a.get("status", "").lower() not in ("cancelado", "faltou")


# ------------------------------------------------------------------ Google Agenda
def default_calendar():
    return os.environ.get("CALENDAR_ID", "")


def cal_for(barber_id):
    """Agenda do barbeiro: coluna `calendario` da aba Barbeiros, senão CALENDAR_ID."""
    b = next((r for r in table("Barbeiros") if r.get("id") == barber_id), None)
    cid = (b or {}).get("calendario") or default_calendar()
    if not cid:
        raise RuntimeError("Defina CALENDAR_ID no .env (ou a coluna `calendario` do barbeiro).")
    return cid


def all_calendars():
    ids = {r.get("calendario") for r in table("Barbeiros") if is_active(r)} | {default_calendar()}
    return sorted(i for i in ids if i)


def day_events(cal_id, d, ttl=TTL_APPTS):
    """Eventos da agenda que tocam o dia `d` (com cache curto)."""
    key = ("cal", cal_id, d.isoformat())
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    start = datetime(d.year, d.month, d.day, tzinfo=TZ)
    evs = get_calendar().list_events(cal_id, start, start + timedelta(days=1))
    with _cache_lock:
        _cache[key] = (time.time(), evs)
    return evs


def blocks_barber(ev, barber_id, ignore_code=None):
    """O evento ocupa a agenda deste barbeiro?"""
    if ev["free"]:  # marcado como "disponível" na agenda
        return False
    p = ev["private"]
    if ignore_code and p.get("codigo") == ignore_code:
        return False
    if p.get("codigo") and not occupying({"status": p.get("status", "")}):
        return False
    return p.get("barbeiro_id", barber_id) == barber_id


def find_appt(code):
    """Procura o agendamento pelo código em todas as agendas."""
    for cid in all_calendars():
        for ev in get_calendar().list_events(cid, private={"codigo": code}):
            a = agenda.appt_from_event(ev)
            if a:
                return a
    return None


def open_count_for_phone(phone):
    n = now()
    total = 0
    for cid in all_calendars():
        for ev in get_calendar().list_events(cid, n, private={"telefone": phone}):
            a = agenda.appt_from_event(ev)
            if a and a["status"] == "agendado":
                total += 1
    return total


def free_slots(barber_id, minutes, d, ignore_code=None):
    n = now()
    if d < n.date() or d > n.date() + timedelta(days=MAX_DAYS_AHEAD):
        return []
    sh = shift_for(barber_id, d)
    if not sh:
        return []
    ab, fe, la, lf = sh
    blocked = []
    if la is not None and lf is not None and lf > la:
        blocked.append((la, lf))
    for f in table("Folgas"):
        if f.get("barbeiro") in (barber_id, "*", "") and parse_date(f.get("data")) == d:
            i, j = parse_time(f.get("inicio")), parse_time(f.get("fim"))
            if i is None or j is None:
                return []  # folga/feriado o dia todo
            blocked.append((i, j))
    day0 = datetime(d.year, d.month, d.day, tzinfo=TZ)
    for ev in day_events(cal_for(barber_id), d):
        if blocks_barber(ev, barber_id, ignore_code):
            bs = max(0, int((ev["start"] - day0).total_seconds() // 60))
            be = min(1440, int((ev["end"] - day0).total_seconds() // 60))
            if be > bs:
                blocked.append((bs, be))
    now_min = n.hour * 60 + n.minute if d == n.date() else -1
    out, s, step = [], ab, step_minutes()
    while s + minutes <= fe:
        if s > now_min and not any(s < be and s + minutes > bs for bs, be in blocked):
            out.append(fmt_time(s))
        s += step
    return out


def find(tab, field, value, ttl=TTL_CATALOG):
    return next((r for r in table(tab, ttl) if r.get(field) == value and is_active(r)), None)


# ------------------------------------------------------------------ escrita
def patch_appt(a, **fields):
    """Atualiza campos do agendamento."""
    a.update(fields)
    body = {"extendedProperties": {"private": {k: str(a.get(k, "")) for k in agenda.PRIVATE_KEYS}}}
    st = appt_start(a)
    if st:
        end = st + timedelta(minutes=int(num(a.get("minutos"), 30)))
        body["start"] = {"dateTime": st.isoformat(), "timeZone": "America/Sao_Paulo"}
        body["end"] = {"dateTime": end.isoformat(), "timeZone": "America/Sao_Paulo"}
    if a.get("status") == "concluido":
        body["colorId"] = "10"  # verde
    get_calendar().patch(a["_cal"], a["_event"], body)
    invalidate_cal()


def append_row(tab, row):
    """Grava uma linha na aba respeitando a ordem das colunas."""
    header = _cache_header(tab)
    get_backend().append(tab, [row.get(h, "") for h in header])
    invalidate(tab)


def _cache_header(tab):
    table(tab)  # garante cache
    with _cache_lock:
        return _cache[tab][2]


def rate_limited(ip):
    q, t = _hits[ip], time.time()
    while q and t - q[0] > 3600:
        q.popleft()
    if len(q) >= BOOK_PER_IP_HOUR:
        return True
    q.append(t)
    return False


def gcal_link(a):
    s = appt_start(a)
    e = s + timedelta(minutes=int(num(a.get("minutos"), 30)))
    f = "%Y%m%dT%H%M%S"
    return "https://calendar.google.com/calendar/render?" + urlencode({
        "action": "TEMPLATE", "text": f"{a['servico']} – {shop()['name']}",
        "dates": f"{s.strftime(f)}/{e.strftime(f)}", "ctz": "America/Sao_Paulo",
        "details": f"Barbeiro: {a['barbeiro']}", "location": shop()["address"]})


# ------------------------------------------------------------------ páginas
def hours_display():
    per_day = {}
    for r in table("Horarios"):
        i, ab, fe = day_index(r.get("dia")), parse_time(r.get("abre")), parse_time(r.get("fecha"))
        if i is None or ab is None or fe is None:
            continue
        cur = per_day.get(i)
        per_day[i] = (min(ab, cur[0]), max(fe, cur[1])) if cur else (ab, fe)
    out, i = [], 0
    while i < 7:
        val, j = per_day.get(i), i
        while j + 1 < 7 and per_day.get(j + 1) == val:
            j += 1
        label = DAY_LABELS[i] if i == j else f"{DAY_LABELS[i]} – {DAY_LABELS[j]}"
        out.append({"label": label, "closed": val is None,
                    "value": "Fechado" if val is None else f"{fmt_time(val[0])} – {fmt_time(val[1])}"})
        i = j + 1
    return out


def jsonld(s, hours):
    per_day = {}
    for r in table("Horarios"):
        i, ab, fe = day_index(r.get("dia")), parse_time(r.get("abre")), parse_time(r.get("fecha"))
        if i is not None and ab is not None and fe is not None:
            c = per_day.get(i)
            per_day[i] = (min(ab, c[0]), max(fe, c[1])) if c else (ab, fe)
    spec = [{"@type": "OpeningHoursSpecification", "dayOfWeek": DAY_EN[i],
             "opens": fmt_time(v[0]), "closes": fmt_time(v[1])} for i, v in sorted(per_day.items())]
    data = {"@context": "https://schema.org", "@type": "BarberShop", "name": s["name"],
            "description": s["tagline"], "url": base_url(),
            "address": {"@type": "PostalAddress", "streetAddress": s["address"], "addressCountry": "BR"},
            "openingHoursSpecification": spec}
    if s["wa_link"]:
        data["telephone"] = "+" + s["wa_link"].rsplit("/", 1)[1]
    return json.dumps(data, ensure_ascii=False).replace("</", "<\\/")


@app.route("/")
def index():
    s = shop()
    return render_template("index.html", shop=s, base=base_url(), og_image=None, css_url=css_url(),
                           jsonld=jsonld(s, None), gallery=[], hours=hours_display(),
                           year=now().year)


@app.route("/privacidade")
@app.route("/privacy")
def privacy():
    return render_template("privacy.html", shop=shop(), css_url=css_url(), version=PRIVACY_VERSION,
                           contact=os.environ.get("PRIVACY_CONTACT", ""), retention=0)


@app.route("/agendamento/<token>")
def manage_page(token):
    return render_template("manage.html", shop=shop(), css_url=css_url(), token=token)


@app.route("/estoque")
def estoque_redirect():
    return redirect(get_backend().url("estoque"))


@app.route("/agenda")
@app.route("/barbeiro")
def agenda_redirect():
    return redirect("https://calendar.google.com/calendar/u/0/r")


@app.route("/robots.txt")
def robots():
    return f"User-agent: *\nAllow: /\nDisallow: /api/\nSitemap: {base_url()}/sitemap.xml\n", 200, {
        "Content-Type": "text/plain"}


@app.route("/sitemap.xml")
def sitemap():
    b = base_url()
    xml = ('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
           f"<url><loc>{b}/</loc></url><url><loc>{b}/privacidade</loc></url></urlset>")
    return xml, 200, {"Content-Type": "application/xml"}


@app.route("/healthz")
def healthz():
    try:
        table("Config", 30)
        return jsonify(ok=True)
    except Exception:
        log.exception("healthz")
        return jsonify(ok=False), 503


# ------------------------------------------------------------------ estoque
def stock_levels():
    """{produto_id: quantidade atual} = entradas − saídas + ajustes (aba Movimentacoes)."""
    lv = defaultdict(float)
    for m in table("Movimentacoes"):
        q, t = num(m.get("quantidade")), m.get("tipo", "").strip().lower()
        lv[m.get("produto_id", "")] += q if t in ("entrada", "ajuste") else -q if t == "saida" else 0
    return lv


# ------------------------------------------------------------------ API pública
@app.route("/api/public")
def api_public():
    return jsonify(
        services=[{"id": r["id"], "name": r["nome"], "description": r.get("descricao", ""),
                   "minutes": int(num(r.get("minutos"), 30)), "price": num(r.get("preco"))}
                  for r in table("Servicos") if is_active(r) and r.get("id") and r.get("nome")],
        barbers=[{"id": r["id"], "name": r["nome"], "role": r.get("funcao", "")}
                 for r in table("Barbeiros") if is_active(r) and r.get("id") and r.get("nome")],
        products=[{"name": r["nome"], "price": num(r.get("preco")), "stock": max(0, int(stock_levels().get(r.get("id", ""), 0)))}
                  for r in table("Produtos") if is_active(r) and r.get("nome")])


@app.route("/api/slots")
def api_slots():
    b, s = find("Barbeiros", "id", request.args.get("barber", "")), find("Servicos", "id", request.args.get("service", ""))
    d = parse_date(request.args.get("date"))
    if not (b and s and d):
        return jsonify(error="Dados inválidos."), 400
    try:
        return jsonify(free_slots(b["id"], int(num(s.get("minutos"), 30)), d))
    except Exception:
        log.exception("slots")
        return jsonify(error="Serviço indisponível. Tente novamente."), 503


@app.route("/api/appointments", methods=["POST"])
def api_book():
    j = request.get_json(silent=True) or {}
    if j.get("website"):  # honeypot
        return jsonify(error="Não foi possível agendar."), 400
    if int(num(j.get("elapsed"), 0)) < MIN_FILL_MS:
        return jsonify(error="Preencha o formulário com calma e tente de novo."), 400
    if j.get("consent") is not True:
        return jsonify(error="É necessário aceitar a Política de Privacidade."), 400
    if rate_limited(request.remote_addr or "?"):
        return jsonify(error="Muitas tentativas. Tente mais tarde."), 429
    name, phone = (j.get("name") or "").strip(), norm_phone(j.get("phone"))
    d, t = parse_date(j.get("date")), parse_time(j.get("time"))
    if not (2 <= len(name) <= 80):
        return jsonify(error="Informe seu nome."), 400
    if not phone:
        return jsonify(error="Informe um WhatsApp válido com DDD."), 400
    try:
        with _book_lock:
            invalidate_cal()
            b, s = find("Barbeiros", "id", j.get("barber", "")), find("Servicos", "id", j.get("service", ""))
            if not (b and s and d and t is not None):
                return jsonify(error="Dados inválidos."), 400
            minutes = int(num(s.get("minutos"), 30))
            if fmt_time(t) not in free_slots(b["id"], minutes, d):
                return jsonify(error="Esse horário acabou de ser ocupado. Escolha outro."), 409
            n = now()
            if open_count_for_phone(phone) >= MAX_OPEN_PER_PHONE:
                return jsonify(error="Você já tem horários marcados. Cancele ou remarque pelo link."), 429
            code = secrets.token_urlsafe(9)
            row = {"codigo": code, "data": d.isoformat(), "hora": fmt_time(t), "barbeiro_id": b["id"],
                   "barbeiro": b["nome"], "servico_id": s["id"], "servico": s["nome"], "minutos": minutes,
                   "preco": s.get("preco", ""), "cliente": name, "telefone": phone, "status": "agendado",
                   "remarcacoes": "0", "consentimento": f"sim v{PRIVACY_VERSION} {n.isoformat(timespec='seconds')}",
                   "optout": "", "criado_em": n.isoformat(timespec="seconds")}
            start = appt_start(row)
            manage_url = f"{base_url()}/agendamento/{code}"
            get_calendar().insert(cal_for(b["id"]), agenda.event_body(
                row, f"{s['nome']} – {name}",
                f"Cliente: {name}\nWhatsApp: {phone}\nServiço: {s['nome']} ({minutes} min)\n"
                f"Barbeiro: {b['nome']}\nGerenciar: {manage_url}\n(agendado pelo site)",
                shop()["address"], start, start + timedelta(minutes=minutes)))
            invalidate_cal()
        return jsonify(ok=True, google_link=gcal_link(row), manage_url=manage_url), 201
    except Exception:
        log.exception("agendar")
        return jsonify(error="Não foi possível agendar agora. Tente novamente em instantes."), 503


# ------------------------------------------------------------------ cliente: ver / cancelar / remarcar
def get_appt(token):
    return find_appt(token) if token else None


def change_state(a):
    if a.get("status") == "cancelado":
        return False, "Este agendamento foi cancelado."
    if a.get("status") != "agendado":
        return False, "Este agendamento já foi encerrado."
    st = appt_start(a)
    if not st or st - now() < timedelta(hours=CHANGE_DEADLINE_HOURS):
        return False, (f"O prazo para alterar online terminou ({CHANGE_DEADLINE_HOURS}h antes do horário). "
                       "Fale com a barbearia pelo WhatsApp.")
    left = MAX_RESCHEDULES - int(num(a.get("remarcacoes")))
    if left <= 0:
        return False, "Você atingiu o limite de remarcações. Para alterar, fale com a barbearia pelo WhatsApp."
    return True, ""


@app.route("/api/manage/<token>")
def api_manage(token):
    try:
        a = get_appt(token)
        if not a:
            return jsonify(error="Agendamento não encontrado ou já cancelado."), 404
        ok, reason = change_state(a)
        left = max(0, MAX_RESCHEDULES - int(num(a.get("remarcacoes"))))
        return jsonify(client=a["cliente"], service=a["servico"], barber=a["barbeiro"],
                       start_at=f"{a['data']} {a['hora']}", address=shop()["address"],
                       status=a["status"], can_change=ok, reason=reason,
                       deadline_hours=CHANGE_DEADLINE_HOURS, reschedules_left=left,
                       max_days=MAX_DAYS_AHEAD, has_phone=bool(a.get("telefone")),
                       optout=bool(a.get("optout")))
    except Exception:
        log.exception("manage")
        return jsonify(error="Serviço indisponível. Tente novamente."), 503


@app.route("/api/manage/<token>/slots")
def api_manage_slots(token):
    a, d = get_appt(token), parse_date(request.args.get("date"))
    if not a or not d:
        return jsonify(error="Dados inválidos."), 400
    ok, reason = change_state(a)
    if not ok:
        return jsonify(error=reason), 403
    return jsonify(free_slots(a["barbeiro_id"], int(num(a.get("minutos"), 30)), d, ignore_code=token))


@app.route("/api/manage/<token>/cancel", methods=["POST"])
def api_manage_cancel(token):
    try:
        with _book_lock:
            invalidate_cal()
            a = get_appt(token)
            if not a:
                return jsonify(error="Agendamento não encontrado."), 404
            ok, reason = change_state(a)
            if not ok:
                return jsonify(error=reason), 403
            get_calendar().delete(a["_cal"], a["_event"]) 
            invalidate_cal()
        return jsonify(ok=True)
    except Exception:
        log.exception("cancel")
        return jsonify(error="Não foi possível cancelar agora. Tente novamente."), 503

# ------------------------------------------------------------------ atendimentos -> financeiro
# (Mantenha a função sync_receitas() original e apenas substitua o bloco start_sync abaixo)

# Rota para a Vercel (Serverless) - Substitui o start_sync() com Thread
@app.route("/api/cron/sync")
def api_cron_sync():
    # Segurança básica: exige uma chave na URL para não ser executado por curiosos
    chave = request.args.get("key")
    if chave != os.environ.get("CRON_SECRET", "chave_cron_barbearia"):
        return jsonify(error="Não autorizado"), 401
    
    try:
        feitos = sync_receitas()
        return jsonify(ok=True, sincronizados=feitos)
    except Exception as e:
        return jsonify(error=str(e)), 500

if __name__ == "__main__":
    app.run(debug=True, port=8080)


@app.route("/api/manage/<token>/optout", methods=["POST"])
def api_manage_optout(token):
    try:
        with _book_lock:
            invalidate_cal()
            a = get_appt(token)
            if not a:
                return jsonify(error="Agendamento não encontrado."), 404
            patch_appt(a, optout="sim")
        return jsonify(ok=True)
    except Exception:
        log.exception("optout")
        return jsonify(error="Não foi possível concluir."), 503


# ------------------------------------------------------------------ atendimentos -> financeiro
_sync_lock = threading.Lock()


def sync_receitas():
    if not _sync_lock.acquire(blocking=False):
        return 0
    done = 0
    try:
        n = now()
        lancados = {r.get("origem") for r in table("Lancamentos", 0)}
        for cid in all_calendars():
            for ev in get_calendar().list_events(cid, n - timedelta(days=SYNC_LOOKBACK_DAYS), n):
                a = agenda.appt_from_event(ev)
                if not a or ev["free"] or a["status"] != "agendado" or ev["end"] > n:
                    continue
                try:
                    if a["codigo"] not in lancados:
                        append_row("Lancamentos", {
                            "id": a["codigo"], "data": a["data"], "tipo": "receita", "categoria": "servico",
                            "descricao": f"{a['servico']} – {a['cliente']}", "valor": num(a.get("preco")),
                            "forma_pgto": "", "barbeiro": a["barbeiro"], "origem": a["codigo"],
                            "criado_em": n.isoformat(timespec="seconds")})
                        lancados.add(a["codigo"])
                    patch_appt(a, status="concluido")
                    done += 1
                except Exception:
                    log.exception("sync %s", a.get("codigo"))
    except Exception:
        log.exception("sync")
    finally:
        _sync_lock.release()
    return done


def start_sync():
    if os.environ.get("SYNC_ENABLED", "1") != "1":
        return

    def loop():
        time.sleep(30)
        while True:
            sync_receitas()
            time.sleep(SYNC_MINUTES * 60)

    threading.Thread(target=loop, daemon=True, name="sync-receitas").start()


start_sync()

if __name__ == "__main__":
    app.run(debug=True, port=8080)
