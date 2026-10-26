"""Barbearia – site de agendamento usando o Google Sheets como banco de dados."""
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

from flask import Flask, jsonify, redirect, render_template, request, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

from sheets import SCHEMA, SheetsBackend

log = logging.getLogger("barbearia")
app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=int(os.environ.get("PROXY_HOPS", "1")))

TZ = ZoneInfo("America/Sao_Paulo")
MAX_DAYS_AHEAD = int(os.environ.get("MAX_DAYS_AHEAD", "30"))
MAX_OPEN_PER_PHONE = int(os.environ.get("MAX_OPEN_PER_PHONE", "2"))
BOOK_PER_IP_HOUR = int(os.environ.get("BOOK_PER_IP_HOUR", "10"))
CHANGE_DEADLINE_HOURS = int(os.environ.get("CHANGE_DEADLINE_HOURS", "2"))
MAX_RESCHEDULES = int(os.environ.get("MAX_RESCHEDULES", "3"))
MIN_FILL_MS = 3000
PRIVACY_VERSION = "1.0"
TTL_CATALOG = 60   # segundos (Config, serviços, horários...)
TTL_APPTS = 15     # segundos (agendamentos; é invalidado a cada gravação nossa)

backend = None  # criado sob demanda (os testes injetam um backend falso)
_cache = {}
_cache_lock = threading.RLock()
_book_lock = threading.Lock()
_hits = defaultdict(deque)


def get_backend():
    global backend
    if backend is None:
        backend = SheetsBackend()
    return backend


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


# ------------------------------------------------------------------ utilidades
def now():
    return datetime.now(TZ)


def num(v, default=0.0):
    try:
        return float(str(v).replace("R$", "").replace(" ", "").replace(".", "").replace(",", ".")
                     if "," in str(v) else str(v).replace("R$", "").strip())
    except ValueError:
        return default


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
    for a in table("Agendamentos", TTL_APPTS):
        if (a.get("barbeiro_id") == barber_id and parse_date(a.get("data")) == d
                and occupying(a) and a.get("codigo") != ignore_code):
            s = parse_time(a.get("hora"))
            if s is not None:
                blocked.append((s, s + int(num(a.get("minutos"), 30))))
    now_min = n.hour * 60 + n.minute if d == n.date() else -1
    out, s, step = [], ab, step_minutes()
    while s + minutes <= fe:
        if s > now_min and not any(s < be and s + minutes > bs for bs, be in blocked):
            out.append(fmt_time(s))
        s += step
    return out


def find(tab, field, value, ttl=TTL_CATALOG):
    return next((r for r in table(tab, ttl) if r.get(field) == value and (tab == "Agendamentos" or is_active(r))), None)


# ------------------------------------------------------------------ escrita
def write_appt(a):
    header = _cache_header("Agendamentos")
    get_backend().update_row("Agendamentos", a["_row"], [a.get(h, "") for h in header])
    invalidate("Agendamentos")


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


@app.route("/admin")
@app.route("/barbeiro")
def admin_redirect():
    """Sem painel próprio: a planilha é o painel."""
    return redirect(f"https://docs.google.com/spreadsheets/d/{get_backend().sheet_id}/edit")


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


# ------------------------------------------------------------------ API pública
@app.route("/api/public")
def api_public():
    return jsonify(
        services=[{"id": r["id"], "name": r["nome"], "description": r.get("descricao", ""),
                   "minutes": int(num(r.get("minutos"), 30)), "price": num(r.get("preco"))}
                  for r in table("Servicos") if is_active(r) and r.get("id") and r.get("nome")],
        barbers=[{"id": r["id"], "name": r["nome"], "role": r.get("funcao", "")}
                 for r in table("Barbeiros") if is_active(r) and r.get("id") and r.get("nome")],
        products=[{"name": r["nome"], "price": num(r.get("preco")), "stock": int(num(r.get("estoque")))}
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
    if j.get("website"):  # honeypot: robô preencheu o campo escondido
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
            invalidate("Agendamentos")
            b, s = find("Barbeiros", "id", j.get("barber", "")), find("Servicos", "id", j.get("service", ""))
            if not (b and s and d and t is not None):
                return jsonify(error="Dados inválidos."), 400
            minutes = int(num(s.get("minutos"), 30))
            if fmt_time(t) not in free_slots(b["id"], minutes, d):
                return jsonify(error="Esse horário acabou de ser ocupado. Escolha outro."), 409
            n = now()
            open_n = sum(1 for a in table("Agendamentos", TTL_APPTS)
                         if a.get("telefone") == phone and a.get("status") == "agendado"
                         and (appt_start(a) or n) >= n)
            if open_n >= MAX_OPEN_PER_PHONE:
                return jsonify(error="Você já tem horários marcados. Cancele ou remarque pelo link."), 429
            code = secrets.token_urlsafe(9)
            row = {"codigo": code, "data": d.isoformat(), "hora": fmt_time(t), "barbeiro_id": b["id"],
                   "barbeiro": b["nome"], "servico_id": s["id"], "servico": s["nome"], "minutos": minutes,
                   "preco": s.get("preco", ""), "cliente": name, "telefone": phone, "status": "agendado",
                   "remarcacoes": "0", "consentimento": f"sim v{PRIVACY_VERSION} {n.isoformat(timespec='seconds')}",
                   "optout": "", "criado_em": n.isoformat(timespec="seconds")}
            header = _cache_header("Agendamentos")
            get_backend().append("Agendamentos", [row.get(h, "") for h in header])
            invalidate("Agendamentos")
        return jsonify(ok=True, google_link=gcal_link(row), manage_url=f"{base_url()}/agendamento/{code}"), 201
    except Exception:
        log.exception("agendar")
        return jsonify(error="Não foi possível agendar agora. Tente novamente em instantes."), 503


# ------------------------------------------------------------------ cliente: ver / cancelar / remarcar
def get_appt(token):
    return find("Agendamentos", "codigo", token, TTL_APPTS) if token else None


def change_state(a):
    """(pode_alterar, motivo)"""
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
        invalidate("Agendamentos")
        a = get_appt(token)
        if not a:
            return jsonify(error="Agendamento não encontrado."), 404
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
            invalidate("Agendamentos")
            a = get_appt(token)
            if not a:
                return jsonify(error="Agendamento não encontrado."), 404
            ok, reason = change_state(a)
            if not ok:
                return jsonify(error=reason), 403
            a["status"] = "cancelado"
            write_appt(a)
        return jsonify(ok=True)
    except Exception:
        log.exception("cancel")
        return jsonify(error="Não foi possível cancelar agora. Tente novamente."), 503


@app.route("/api/manage/<token>/reschedule", methods=["POST"])
def api_manage_reschedule(token):
    j = request.get_json(silent=True) or {}
    d, t = parse_date(j.get("date")), parse_time(j.get("time"))
    try:
        with _book_lock:
            invalidate("Agendamentos")
            a = get_appt(token)
            if not a:
                return jsonify(error="Agendamento não encontrado."), 404
            ok, reason = change_state(a)
            if not ok:
                return jsonify(error=reason), 403
            if int(num(a.get("remarcacoes"))) >= MAX_RESCHEDULES:
                return jsonify(error="Limite de remarcações atingido. Fale com a barbearia."), 403
            if not d or t is None or fmt_time(t) not in free_slots(
                    a["barbeiro_id"], int(num(a.get("minutos"), 30)), d, ignore_code=token):
                return jsonify(error="Esse horário não está mais livre. Escolha outro."), 409
            a["data"], a["hora"] = d.isoformat(), fmt_time(t)
            a["remarcacoes"] = str(int(num(a.get("remarcacoes"))) + 1)
            write_appt(a)
        return jsonify(ok=True)
    except Exception:
        log.exception("reschedule")
        return jsonify(error="Não foi possível remarcar agora. Tente novamente."), 503


@app.route("/api/manage/<token>/optout", methods=["POST"])
def api_manage_optout(token):
    try:
        with _book_lock:
            invalidate("Agendamentos")
            a = get_appt(token)
            if not a:
                return jsonify(error="Agendamento não encontrado."), 404
            a["optout"] = "sim"
            write_appt(a)
        return jsonify(ok=True)
    except Exception:
        log.exception("optout")
        return jsonify(error="Não foi possível concluir."), 503


if __name__ == "__main__":
    app.run(debug=True, port=8080)
