"""00 Barbearia – site público + ERP + agenda + WhatsApp. Execução: gunicorn -w 1 app:app"""
import csv, io, json, logging, math, os, re, secrets, shutil, sqlite3, subprocess, hmac, time
from datetime import date, datetime, timedelta
from functools import wraps
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

import requests
from werkzeug.security import check_password_hash, generate_password_hash
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, Response, abort, g, jsonify, render_template, request, send_file, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix

log = logging.getLogger("barbearia")
logging.basicConfig(level=logging.INFO)
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 6 * 1024 * 1024  # uploads de fotos da galeria (cada uma até 3 MB)
# atrás do Caddy/Nginx: 1 proxy confiável (PROXY_HOPS=0 se rodar sem proxy) -> request.remote_addr = IP real do cliente
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=int(os.getenv("PROXY_HOPS", 1)), x_proto=1, x_host=1)

TZ = ZoneInfo("America/Sao_Paulo")
DB = os.getenv("DB_PATH", "barbearia.db")
FMT = "%Y-%m-%d %H:%M"
OPEN_H, CLOSE_H, STEP = 9, 19, 15
RETURN_DAYS = int(os.getenv("DEFAULT_RETURN_DAYS", 21))
# anti-spam do agendamento
BOOK_PER_IP_HOUR = int(os.getenv("BOOK_PER_IP_HOUR", 10))     # tentativas por IP por hora (celular usa IP compartilhado: não baixe demais)
MAX_OPEN_PER_PHONE = int(os.getenv("MAX_OPEN_PER_PHONE", 2))  # horários futuros por WhatsApp
MAX_DAYS_AHEAD = int(os.getenv("MAX_DAYS_AHEAD", 30))         # até quantos dias à frente
MIN_FORM_MS = 3000                                            # robôs enviam o formulário rápido demais
# backup
BACKUP_DIR = os.getenv("BACKUP_DIR") or os.path.join(os.path.dirname(os.path.abspath(DB)), "backups")
BACKUP_KEEP_DAYS = int(os.getenv("BACKUP_KEEP_DAYS", 14))
REMOTE_KEEP_DAYS = int(os.getenv("REMOTE_KEEP_DAYS", 60))  # quanto tempo manter na nuvem
# cliente: cancelar/remarcar por link e lembretes
CHANGE_DEADLINE_H = int(os.getenv("CHANGE_DEADLINE_HOURS", 2))  # até quantas horas antes o cliente pode cancelar/remarcar
MAX_RESCHEDULES = int(os.getenv("MAX_RESCHEDULES", 3))          # remarcações por agendamento
DAY_BEFORE_HOUR = int(os.getenv("DAY_BEFORE_HOUR", 18))         # hora do lembrete da véspera
MANAGE_PER_IP_HOUR = 60
# segurança do painel
ADMIN_MAX_FAILS = int(os.getenv("ADMIN_MAX_FAILS", 8))   # senhas DIFERENTES erradas por IP...
ADMIN_LOCK_MIN = int(os.getenv("ADMIN_LOCK_MIN", 15))    # ...nesta janela (min) bloqueiam o IP até a janela passar
# privacidade (LGPD)
POLICY_VERSION = "2026-10"
RETENTION_MONTHS = int(os.getenv("RETENTION_MONTHS", 24))  # anonimiza clientes sem atendimento há N meses (0 = desligado)
PRIVACY_CONTACT = os.getenv("PRIVACY_CONTACT", "")        # e-mail/WhatsApp do responsável, exibido na política
# monitoramento
WA_ALERT_MIN = int(os.getenv("WA_ALERT_MINUTES", 10))     # minutos com o WhatsApp desconectado até alertar
SCHED_ON = os.getenv("RUN_SCHEDULER", "1") == "1"
# pagamentos
PAY_METHODS = {"dinheiro": "Dinheiro", "pix": "Pix", "debito": "Cartão de débito", "credito": "Cartão de crédito", "outro": "Outro"}
WEEKDAYS = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]
# dados da barbearia: valores padrão (variáveis SHOP_*) que podem ser trocados no painel (aba Site) sem mexer no código
SHOP_DEFAULTS = {"name": os.getenv("SHOP_NAME", "D.Lourenço Barbearia"), "short_name": os.getenv("SHOP_SHORT_NAME", "D.Lourenço"), "brand": os.getenv("SHOP_BRAND", "00"),
                 "address": os.getenv("SHOP_ADDRESS", "Av. Mal. Argôlo, 912, Vila Passos, Lorena - SP, 12604-440"),
                 "whatsapp": os.getenv("SHOP_WHATSAPP", ""), "instagram": os.getenv("SHOP_INSTAGRAM", ""),
                 "tagline": os.getenv("SHOP_TAGLINE", "Barbearia em Lorena-SP. Agende online: cortes, barba e produtos.")}
SITE_FIELDS = {"name": 80, "short_name": 40, "brand": 12, "address": 160, "tagline": 200, "whatsapp": 20, "instagram": 60}
GALLERY_DIR = os.path.join(os.path.dirname(os.path.abspath(DB)), "gallery")
MAX_PHOTOS, MAX_PHOTO_BYTES = 24, 3 * 1024 * 1024

SCHEMA = """
CREATE TABLE IF NOT EXISTS barbers(id INTEGER PRIMARY KEY, name TEXT NOT NULL, role TEXT, calendar_id TEXT,
  commission_pct REAL NOT NULL DEFAULT 50, active INTEGER NOT NULL DEFAULT 1, login TEXT, pass_hash TEXT);
CREATE TABLE IF NOT EXISTS services(id INTEGER PRIMARY KEY, name TEXT NOT NULL, description TEXT,
  price REAL NOT NULL, minutes INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY, name TEXT NOT NULL, price REAL NOT NULL DEFAULT 0,
  stock INTEGER NOT NULL DEFAULT 0, for_sale INTEGER NOT NULL DEFAULT 1, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS stock_moves(id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES products(id),
  qty INTEGER NOT NULL, day TEXT NOT NULL, reason TEXT);
CREATE TABLE IF NOT EXISTS appointments(id INTEGER PRIMARY KEY, client_name TEXT NOT NULL, phone TEXT NOT NULL,
  barber_id INTEGER NOT NULL REFERENCES barbers(id), service_id INTEGER NOT NULL REFERENCES services(id),
  start_at TEXT NOT NULL, end_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'agendado',
  gcal_event_id TEXT, reminder_sent INTEGER NOT NULL DEFAULT 0, return_sent INTEGER NOT NULL DEFAULT 0,
  token TEXT, confirm_sent INTEGER NOT NULL DEFAULT 0, day_before_sent INTEGER NOT NULL DEFAULT 0, reschedules INTEGER NOT NULL DEFAULT 0, consent TEXT);
CREATE INDEX IF NOT EXISTS ix_appt ON appointments(barber_id, start_at);
CREATE TABLE IF NOT EXISTS book_attempts(id INTEGER PRIMARY KEY, ip TEXT NOT NULL, at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_attempts ON book_attempts(ip, at);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS optout(phone TEXT PRIMARY KEY, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS client_notes(phone TEXT PRIMARY KEY, note TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS gallery(id INTEGER PRIMARY KEY, filename TEXT NOT NULL UNIQUE, caption TEXT, position INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cash_closings(day TEXT PRIMARY KEY, opening REAL NOT NULL DEFAULT 0, expected REAL NOT NULL, counted REAL NOT NULL,
  notes TEXT, closed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS work_hours(barber_id INTEGER NOT NULL REFERENCES barbers(id), weekday INTEGER NOT NULL CHECK(weekday BETWEEN 0 AND 6),
  open INTEGER NOT NULL DEFAULT 1, t_start TEXT NOT NULL, t_end TEXT NOT NULL, lunch_start TEXT, lunch_end TEXT, PRIMARY KEY(barber_id, weekday));
CREATE TABLE IF NOT EXISTS time_off(id INTEGER PRIMARY KEY, barber_id INTEGER REFERENCES barbers(id), day_from TEXT NOT NULL, day_to TEXT NOT NULL,
  t_from TEXT, t_to TEXT, reason TEXT);
CREATE INDEX IF NOT EXISTS ix_timeoff ON time_off(day_from, day_to);
CREATE TABLE IF NOT EXISTS outbox(id INTEGER PRIMARY KEY, phone TEXT NOT NULL, text TEXT NOT NULL, sent_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS transactions(id INTEGER PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN('entrada','saida')),
  amount REAL NOT NULL CHECK(amount>0), day TEXT NOT NULL, description TEXT, category TEXT NOT NULL,
  barber_id INTEGER REFERENCES barbers(id), method TEXT, discount REAL NOT NULL DEFAULT 0, appointment_id INTEGER);
"""

# ---------- banco ----------
def connect():
    c = sqlite3.connect(DB, isolation_level=None, timeout=15)  # autocommit; espera até 15 s por um bloqueio em vez de falhar
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")    # leitores não bloqueiam o escritor (e vice-versa)
    c.execute("PRAGMA synchronous=NORMAL")  # seguro junto com WAL e mais rápido
    c.execute("PRAGMA foreign_keys=ON")
    return c

def db():
    if "db" not in g:
        g.db = connect()
    return g.db

@app.teardown_appcontext
def _close(_):
    c = g.pop("db", None)
    if c:
        c.close()

def init_db():
    c = connect()
    c.executescript(SCHEMA)
    for t in ("barbers", "services", "products"):  # bancos criados antes do 'active'
        if "active" not in [r["name"] for r in c.execute(f"PRAGMA table_info({t})")]:
            c.execute(f"ALTER TABLE {t} ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
    cols = [r["name"] for r in c.execute("PRAGMA table_info(appointments)")]
    for col, ddl, backfill in (("confirm_sent", "INTEGER NOT NULL DEFAULT 0", "UPDATE appointments SET confirm_sent=1"),  # agendamentos antigos não recebem confirmação
                               ("day_before_sent", "INTEGER NOT NULL DEFAULT 0", "UPDATE appointments SET day_before_sent=1"),
                               ("reschedules", "INTEGER NOT NULL DEFAULT 0", None), ("token", "TEXT", None),
                               ("consent", "TEXT", "UPDATE appointments SET consent='legado' WHERE phone<>''")):  # clientes antigos já tinham o aviso no formulário
        if col not in cols:
            c.execute(f"ALTER TABLE appointments ADD COLUMN {col} {ddl}")
            if backfill:
                c.execute(backfill)
    for r in c.execute("SELECT id FROM appointments WHERE token IS NULL").fetchall():
        c.execute("UPDATE appointments SET token=? WHERE id=?", (new_token(), r["id"]))
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_appt_token ON appointments(token)")
    tcols = [r["name"] for r in c.execute("PRAGMA table_info(transactions)")]
    for col, ddl in (("method", "TEXT"), ("discount", "REAL NOT NULL DEFAULT 0"), ("appointment_id", "INTEGER")):
        if col not in tcols:
            c.execute(f"ALTER TABLE transactions ADD COLUMN {col} {ddl}")
    c.execute("CREATE INDEX IF NOT EXISTS ix_tx_appt ON transactions(appointment_id)")
    bcols = [r["name"] for r in c.execute("PRAGMA table_info(barbers)")]
    for col in ("login", "pass_hash"):
        if col not in bcols:
            c.execute(f"ALTER TABLE barbers ADD COLUMN {col} TEXT")
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_barber_login ON barbers(lower(login)) WHERE login IS NOT NULL")
    if not c.execute("SELECT 1 FROM barbers").fetchone():
        c.executemany("INSERT INTO barbers(name,role) VALUES(?,?)", [
            ("João Lourenço", "Mestre Barbeiro"), ("Carlos Silva", "Barbeiro Sênior"),
            ("Pedro Mendes", "Especialista em Barba")])
        c.executemany("INSERT INTO services(name,description,price,minutes) VALUES(?,?,?,?)", [
            ("Corte Clássico", "Tesoura ou máquina.", 45, 30), ("Barba Terapia", "Toalha quente e navalha.", 35, 20),
            ("Combo Corte + Barba", "O pacote completo.", 70, 50), ("Pigmentação", "Cobertura de fios brancos.", 25, 15)])
        c.executemany("INSERT INTO products(name,price,stock,for_sale) VALUES(?,?,?,?)", [
            ("Pomada Modeladora Matte", 40, 15, 1), ("Óleo para Barba", 35, 8, 1), ("Shampoo 3 em 1", 30, 20, 1),
            ("Lâmina de barbear (cx)", 0, 10, 0), ("Toalha descartável (pct)", 0, 12, 0)])
    for r in c.execute("SELECT id FROM barbers").fetchall():
        seed_hours(c, r["id"])
    c.close()

# ---------- backup ----------
def backup_db():
    """Cópia consistente do banco (API de backup do SQLite) em BACKUP_DIR; apaga as mais velhas que BACKUP_KEEP_DAYS."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    dst = os.path.join(BACKUP_DIR, f"barbearia-{now():%Y-%m-%d}.db")
    src, out = connect(), sqlite3.connect(dst)
    try:
        src.backup(out)
        out.execute("PRAGMA journal_mode=DELETE")  # a cópia fica em um único arquivo .db (sem -wal/-shm)
    finally:
        out.close()
        src.close()
    limit = time.time() - BACKUP_KEEP_DAYS * 86400
    for f in os.listdir(BACKUP_DIR):
        p = os.path.join(BACKUP_DIR, f)
        if re.fullmatch(r"barbearia-\d{4}-\d{2}-\d{2}\.db", f) and os.path.getmtime(p) < limit:
            os.remove(p)
    return dst

def set_meta(k, v):
    c = connect()
    c.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, v))
    c.close()

def get_meta(k):
    c = connect()
    r = c.execute("SELECT value FROM meta WHERE key=?", (k,)).fetchone()
    c.close()
    return r["value"] if r else ""

def offsite_upload(path):
    """Envia o backup para a nuvem com rclone (Google Drive, Dropbox, Backblaze, S3...). RCLONE_REMOTE ex.: gdrive:barbearia-backups"""
    remote = os.getenv("RCLONE_REMOTE")
    if not remote:
        raise RuntimeError("RCLONE_REMOTE não está configurado")
    if not shutil.which("rclone"):
        raise RuntimeError("o rclone não está instalado no servidor")
    cmds = [["rclone", "copy", path, remote, "--retries", "2"],
            ["rclone", "delete", remote, "--min-age", f"{REMOTE_KEEP_DAYS}d", "--include", "barbearia-*.db"]]
    if os.path.isdir(GALLERY_DIR) and os.listdir(GALLERY_DIR):  # fotos da galeria também vão para a nuvem
        cmds.append(["rclone", "copy", GALLERY_DIR, remote.rstrip("/") + "/galeria", "--retries", "2"])
    for cmd in cmds:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        if r.returncode:
            raise RuntimeError((r.stderr or r.stdout).strip()[-250:] or "o rclone falhou")

def run_backup():
    """Backup local + (se RCLONE_REMOTE estiver definido) envio para a nuvem. Retorna (arquivo, erro_do_envio_ou_None)."""
    p, err = backup_db(), None
    stamp = now().strftime(FMT)
    set_meta("backup_local", stamp)
    if os.getenv("RCLONE_REMOTE"):
        try:
            offsite_upload(p)
            set_meta("offsite_ok", stamp)
            set_meta("offsite_error", "")
        except Exception as e:
            err = str(e)[:250]
            log.error("Falha ao enviar o backup para a nuvem: %s", err)
            set_meta("offsite_error", f"{stamp} – {err}")
    return p, err

def scheduled_backup():
    try:
        p, err = run_backup()
        log.info("Backup diário: %s%s", p, f" (envio à nuvem falhou: {err})" if err else "")
    except Exception:
        log.exception("Falha no backup diário")

# ---------- utilidades ----------
P = lambda s: datetime.strptime(s, FMT)
now = lambda: datetime.now(TZ).replace(tzinfo=None)
STARTED = now()

def norm_phone(s):
    d = re.sub(r"\D", "", str(s))
    if len(d) in (10, 11):
        d = "55" + d
    return d if len(d) in (12, 13) and d.startswith("55") else None

_shop_cache = {"t": 0.0, "v": None}

def split_logo(s):
    """Divide o nome curto em (parte destacada em âmbar, resto): 'D.Lourenço' -> ('D.', 'Lourenço')."""
    s = (s or "").strip()
    for sep in (".", " "):
        i = s.find(sep, 1)
        if i > 0:
            return s[:i + (1 if sep == "." else 0)], s[i + (1 if sep == "." else 0):]
    return s[:1], s[1:]

def clean_instagram(v):
    """Aceita @usuario, usuario ou a URL do perfil; devolve só o usuário ('' se vazio, None se inválido)."""
    h = re.sub(r"^(https?://)?(www\.)?instagram\.com/", "", str(v or "").strip()).strip("/@ ")
    return h if re.fullmatch(r"[A-Za-z0-9._]{1,30}", h) else ("" if not h else None)

def shop():
    """Dados da barbearia (padrões + o que foi salvo no painel), com atalhos prontos para os templates."""
    n = time.time()
    if _shop_cache["v"] is None or n - _shop_cache["t"] > 60:
        v = dict(SHOP_DEFAULTS)
        try:
            c = connect()
            for r in c.execute("SELECT key,value FROM settings"):
                if r["value"]:
                    v[r["key"]] = r["value"]
            c.close()
        except sqlite3.Error:
            pass
        v["logo_hl"], v["logo_rest"] = split_logo(v["short_name"])
        wa = norm_phone(v["whatsapp"])
        v["wa_link"] = f"https://wa.me/{wa}" if wa else ""
        ig = clean_instagram(v["instagram"])
        v["instagram_url"] = f"https://instagram.com/{ig}" if ig else ""
        _shop_cache.update(t=n, v=v)
    return _shop_cache["v"]

def body():
    return request.get_json(silent=True) or {}

def _salt():
    v = get_meta("salt")
    if not v:
        v = secrets.token_hex(16)
        set_meta("salt", v)
    return v

def login_prefix():
    return f"a:{request.remote_addr or '?'}:"

def login_fails(window_min):
    """Quantas senhas DIFERENTES erradas este IP tentou na janela (repetir a mesma senha guardada no navegador conta uma vez só)."""
    return db().execute("SELECT COUNT(DISTINCT ip) FROM book_attempts WHERE ip LIKE ? AND at>?",
                        (login_prefix() + "%", (now() - timedelta(minutes=window_min)).strftime(FMT))).fetchone()[0]

def admin_required(f):
    @wraps(f)
    def w(*a, **k):
        if login_fails(ADMIN_LOCK_MIN) >= ADMIN_MAX_FAILS:
            return Response("Muitas tentativas de login. Aguarde alguns minutos e tente de novo.", 429, {"Retry-After": str(ADMIN_LOCK_MIN * 60)})
        u, p = os.getenv("ADMIN_USER", "admin"), os.getenv("ADMIN_PASS")
        au = request.authorization
        ok = bool(p) and au and hmac.compare_digest(au.username or "", u) and hmac.compare_digest(au.password or "", p)
        if ok:
            return f(*a, **k)
        if au:  # só conta quando houve tentativa de senha (a 1ª visita sem credencial apenas abre a janela de login)
            record_login_fail(au.password)
        return Response("Acesso restrito", 401, {"WWW-Authenticate": 'Basic realm="ERP"'})
    return w

def record_login_fail(password):
    tag = hmac.new(_salt().encode(), (password or "").encode(), "sha256").hexdigest()[:12]
    db().execute("INSERT INTO book_attempts(ip,at) VALUES(?,?)", (login_prefix() + tag, now().strftime(FMT)))
    log.warning("Falha de login (IP %s)", request.remote_addr)

# ----- acesso do barbeiro (login próprio; vê só a própria agenda e os próprios ganhos) -----
_DUMMY_HASH = generate_password_hash("sem-usuario")  # hash falso: o tempo de resposta não revela se o usuário existe
_auth_ok = {}  # (id, hash, HMAC da senha) -> validade; evita refazer o hash caro a cada requisição (cache de 5 min, só em memória)

def barber_auth():
    au = request.authorization
    if not au or not au.username:
        return None
    b = db().execute("SELECT id,name,role,commission_pct,pass_hash FROM barbers WHERE lower(login)=? AND active=1 AND pass_hash IS NOT NULL",
                     (au.username.lower(),)).fetchone()
    pw, t = au.password or "", time.time()
    key = (b["id"], b["pass_hash"], hmac.new(_salt().encode(), pw.encode(), "sha256").hexdigest()) if b else None
    if key and _auth_ok.get(key, 0) > t:
        return b
    if not (check_password_hash(b["pass_hash"] if b else _DUMMY_HASH, pw) and b):
        return None
    if len(_auth_ok) > 200:
        for k in [k for k, v in _auth_ok.items() if v <= t]:
            del _auth_ok[k]
    _auth_ok[key] = t + 300
    return b

def barber_required(f):
    @wraps(f)
    def w(*a, **k):
        if login_fails(ADMIN_LOCK_MIN) >= ADMIN_MAX_FAILS:
            return Response("Muitas tentativas de login. Aguarde alguns minutos e tente de novo.", 429, {"Retry-After": str(ADMIN_LOCK_MIN * 60)})
        b = barber_auth()
        if b:
            g.barber = b
            return f(*a, **k)
        if request.authorization:
            record_login_fail(request.authorization.password)
        return Response("Acesso restrito", 401, {"WWW-Authenticate": 'Basic realm="Barbeiros"'})
    return w

# ---------- integrações ----------
DEMO = lambda: os.getenv("WA_DEMO") == "1"

def auto_send_enabled():
    return bool(DEMO() or os.getenv("WA_GATEWAY_URL") or os.getenv("WA_TOKEN"))

def send_whatsapp(phone, text):
    """1) gateway local não oficial (Baileys) se WA_GATEWAY_URL; 2) Meta Cloud API se WA_TOKEN; senão False (fila manual)."""
    if DEMO():  # apresentação: nada sai para o WhatsApp real; a mensagem vai para a caixa de saída simulada
        c = connect()
        c.execute("INSERT INTO outbox(phone,text,sent_at) VALUES(?,?,?)", (phone, text, now().strftime(FMT)))
        c.close()
        return True
    gw = os.getenv("WA_GATEWAY_URL")
    if gw:
        try:
            r = requests.post(gw.rstrip("/") + "/send", headers={"X-Secret": os.getenv("WA_GATEWAY_SECRET", "")},
                              json={"to": phone, "text": text}, timeout=30)
            if not r.ok:
                log.error("Gateway WhatsApp %s: %s", r.status_code, r.text)
            return r.ok
        except requests.RequestException as e:
            log.error("Gateway WhatsApp indisponível: %s", e)
            return False
    token, pid = os.getenv("WA_TOKEN"), os.getenv("WA_PHONE_ID")
    if not (token and pid):
        return False
    try:
        r = requests.post(f"https://graph.facebook.com/v20.0/{pid}/messages",
                          headers={"Authorization": f"Bearer {token}"}, timeout=10,
                          json={"messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": text}})
        if not r.ok:
            log.error("WhatsApp %s: %s", r.status_code, r.text)
        return r.ok
    except requests.RequestException as e:
        log.error("WhatsApp erro: %s", e)
        return False

def _sa_info():
    """Credencial da conta de serviço: arquivo (GOOGLE_SA_FILE) ou JSON em variável (GOOGLE_SA_JSON)."""
    f, raw = os.getenv("GOOGLE_SA_FILE"), os.getenv("GOOGLE_SA_JSON")
    if f:
        with open(f, encoding="utf-8") as fh:
            return json.load(fh)
    return json.loads(raw) if raw else None

def gcal():
    info = _sa_info()
    if not info:
        return None
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    cred = service_account.Credentials.from_service_account_info(info, scopes=["https://www.googleapis.com/auth/calendar"])
    return build("calendar", "v3", credentials=cred, cache_discovery=False)

def gcal_event_body(a, svc):
    return {"summary": f"{svc['name']} – {a['client_name']}", "location": shop()["address"],
            "description": f"Cliente: {a['client_name']}\nTel: {a['phone']}",
            "start": {"dateTime": P(a["start_at"]).isoformat(), "timeZone": "America/Sao_Paulo"},
            "end": {"dateTime": P(a["end_at"]).isoformat(), "timeZone": "America/Sao_Paulo"}}

def gcal_create(a, barber, svc):
    try:
        s = gcal()
        if not s or not barber["calendar_id"]:
            return None
        return s.events().insert(calendarId=barber["calendar_id"], body=gcal_event_body(a, svc)).execute()["id"]
    except Exception:
        log.exception("Falha ao criar evento no Google Agenda")
        return None

def gcal_delete(cal_id, ev_id):
    try:
        s = gcal()
        if s and cal_id and ev_id:
            s.events().delete(calendarId=cal_id, eventId=ev_id).execute()
    except Exception:
        log.exception("Falha ao remover evento")

def client_gcal_link(a, svc):
    fmt = lambda s: P(s).strftime("%Y%m%dT%H%M%S")
    return "https://calendar.google.com/calendar/render?" + urlencode({
        "action": "TEMPLATE", "text": f"{svc['name']} – {shop()['name']}", "location": shop()["address"],
        "dates": f"{fmt(a['start_at'])}/{fmt(a['end_at'])}", "ctz": "America/Sao_Paulo"})

# ---------- agenda ----------
def new_token():
    return secrets.token_urlsafe(12)

def seed_hours(c, bid):
    """Expediente padrão (seg a sáb, 9h às 19h, domingo fechado); ajustável no painel (aba Horários)."""
    for wd in range(7):
        c.execute("INSERT OR IGNORE INTO work_hours(barber_id,weekday,open,t_start,t_end) VALUES(?,?,?,?,?)",
                  (bid, wd, 0 if wd == 6 else 1, f"{OPEN_H:02d}:00", f"{CLOSE_H:02d}:00"))

def hours_for(c, bid):
    rows = c.execute("SELECT * FROM work_hours WHERE barber_id=?", (bid,)).fetchall()
    if len(rows) < 7:
        seed_hours(c, bid)
        rows = c.execute("SELECT * FROM work_hours WHERE barber_id=?", (bid,)).fetchall()
    return {r["weekday"]: r for r in rows}

def _at(d, hhmm):
    h, m = map(int, hhmm.split(":"))
    return d.replace(hour=h, minute=m)

def day_rules(c, barber_id, d):
    """Intervalos em que o barbeiro atende e bloqueios (folgas/feriados) numa data (d = meia-noite)."""
    h = hours_for(c, barber_id).get(d.weekday())
    opened = []
    if h and h["open"]:
        s, e = _at(d, h["t_start"]), _at(d, h["t_end"])
        if h["lunch_start"] and h["lunch_end"]:
            opened = [(s, _at(d, h["lunch_start"])), (_at(d, h["lunch_end"]), e)]
        else:
            opened = [(s, e)]
    day, blocked = d.strftime("%Y-%m-%d"), []
    for r in c.execute("SELECT * FROM time_off WHERE (barber_id IS NULL OR barber_id=?) AND day_from<=? AND day_to>=?", (barber_id, day, day)):
        if r["t_from"] and r["t_to"]:
            blocked.append((_at(d, r["t_from"]), _at(d, r["t_to"]), r["reason"] or "folga"))
        else:
            blocked.append((d, d + timedelta(days=1), r["reason"] or "folga"))
    return opened, blocked

def schedule_problem(c, barber_id, start, end):
    """Texto explicando por que [start,end] está fora da agenda do barbeiro (ou None se estiver tudo certo)."""
    d = start.replace(hour=0, minute=0, second=0, microsecond=0)
    opened, blocked = day_rules(c, barber_id, d)
    for bs, be, why in blocked:
        if start < be and end > bs:
            return f"Folga/feriado nesse horário ({why})."
    if not opened:
        return f"O barbeiro não atende neste dia da semana ({WEEKDAYS[d.weekday()]})."
    if not any(s <= start and end <= e for s, e in opened):
        return "Fora do expediente do barbeiro (ou no horário de almoço)."
    return None

def free_slots(c, barber_id, minutes, day, exclude_id=None):
    """Horários livres do barbeiro no dia: dentro do expediente (sem almoço), fora de folgas/feriados e de outros agendamentos."""
    d = datetime.strptime(day, "%Y-%m-%d")
    opened, blocked = day_rules(c, barber_id, d)
    busy = [(P(r["start_at"]), P(r["end_at"])) for r in c.execute(
        "SELECT start_at,end_at FROM appointments WHERE barber_id=? AND status='agendado' AND start_at LIKE ? AND id<>?",
        (barber_id, day + "%", exclude_id or 0))]
    busy += [(bs, be) for bs, be, _ in blocked]
    out, dur = [], timedelta(minutes=minutes)
    for s0, e0 in opened:
        t = s0
        while t + dur <= e0:
            e = t + dur
            if t > now() and all(e <= bs or t >= be for bs, be in busy):
                out.append(t.strftime("%H:%M"))
            t += timedelta(minutes=STEP)
    return out

def vespera_due(start):
    """Quando o lembrete da véspera passa a valer: DAY_BEFORE_HOUR do dia anterior."""
    return (start.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)).replace(hour=DAY_BEFORE_HOUR)

def born_day_before(start):
    """1 se o agendamento nasce depois da hora do lembrete da véspera (a confirmação já cobre; evita mensagem dupla)."""
    return 1 if now() >= vespera_due(start) else 0

def too_many(key, limit):
    """Conta a tentativa de `key` (IP) e diz se passou de `limit` na última hora."""
    c, n = db(), now()
    c.execute("DELETE FROM book_attempts WHERE at<?", ((n - timedelta(days=1)).strftime(FMT),))
    c.execute("INSERT INTO book_attempts(ip,at) VALUES(?,?)", (key, n.strftime(FMT)))
    return c.execute("SELECT COUNT(*) FROM book_attempts WHERE ip=? AND at>?",
                     (key, (n - timedelta(hours=1)).strftime(FMT))).fetchone()[0] > limit

def manage_url(token):
    return f"{(os.getenv('PUBLIC_URL') or request.host_url).rstrip('/')}/agendamento/{token}"

@app.context_processor
def _assets():
    """CSS do Tailwind gerado localmente (static/tailwind.css). Se ainda não existir, os templates usam o CDN como reserva."""
    p = os.path.join(app.static_folder, "tailwind.css")
    return {"css_url": f"/static/tailwind.css?v={int(os.path.getmtime(p))}" if os.path.exists(p) else None}

# ---------- rotas públicas ----------
DAYS_LD = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

def site_base():
    return (os.getenv("PUBLIC_URL") or request.host_url).rstrip("/")

def shop_hours(c):
    """Expediente da loja = união dos expedientes dos barbeiros ativos em cada dia (para o Google/Schema.org)."""
    ids, out = [r["id"] for r in c.execute("SELECT id FROM barbers WHERE active=1")], []
    for wd in range(7):
        rows = [r for r in (hours_for(c, b).get(wd) for b in ids) if r and r["open"]]
        if rows:
            out.append({"@type": "OpeningHoursSpecification", "dayOfWeek": DAYS_LD[wd],
                        "opens": min(r["t_start"] for r in rows), "closes": max(r["t_end"] for r in rows)})
    return out

DAYS_PT = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]

def hours_lines(c):
    """Linhas do 'Horário de Funcionamento' do site: dias seguidos com o mesmo horário são agrupados ('Segunda a Sexta 09:00 - 19:00')."""
    ids = [r["id"] for r in c.execute("SELECT id FROM barbers WHERE active=1")]
    if not ids:
        return []
    days = []
    for wd in range(7):
        rows = [r for r in (hours_for(c, b).get(wd) for b in ids) if r and r["open"]]
        days.append((min(r["t_start"] for r in rows), max(r["t_end"] for r in rows)) if rows else None)
    out, i = [], 0
    while i < 7:
        j = i
        while j + 1 < 7 and days[j + 1] == days[i]:
            j += 1
        label = DAYS_PT[i] if i == j else (f"{DAYS_PT[i]} e {DAYS_PT[j]}" if j == i + 1 else f"{DAYS_PT[i]} a {DAYS_PT[j]}")
        out.append({"label": label, "value": f"{days[i][0]} - {days[i][1]}" if days[i] else "Fechado", "closed": days[i] is None})
        i = j + 1
    return out

@app.get("/")
def index():
    c, s, base = db(), shop(), site_base()
    photos = [dict(r) for r in c.execute("SELECT id,filename,caption FROM gallery ORDER BY position,id")]
    ld = {"@context": "https://schema.org", "@type": "BarberShop", "name": s["name"], "description": s["tagline"], "url": base + "/",
          "address": {"@type": "PostalAddress", "streetAddress": s["address"], "addressCountry": "BR"}, "openingHoursSpecification": shop_hours(c)}
    if s["wa_link"]:
        ld["telephone"] = "+" + norm_phone(s["whatsapp"])
    if s["instagram_url"]:
        ld["sameAs"] = [s["instagram_url"]]
    if photos:
        ld["image"] = f"{base}/media/gallery/{photos[0]['filename']}"
    return render_template("index.html", shop=s, gallery=photos, base=base, og_image=ld.get("image", ""), hours=hours_lines(c), year=now().year,
                           jsonld=json.dumps(ld, ensure_ascii=False).replace("</", "<\\/"))

@app.get("/robots.txt")
def robots():
    return Response(f"User-agent: *\nDisallow: /admin\nDisallow: /api/\nDisallow: /agendamento/\nDisallow: /barbeiro\nSitemap: {site_base()}/sitemap.xml\n", mimetype="text/plain")

@app.get("/sitemap.xml")
def sitemap():
    from xml.sax.saxutils import escape
    b = site_base()
    return Response('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    + "".join(f"<url><loc>{escape(b + p)}</loc></url>" for p in ("/", "/privacidade")) + "</urlset>", mimetype="application/xml")

def img_ext(head):
    """Tipo da imagem pelos primeiros bytes (não confia na extensão do arquivo enviado)."""
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    return "webp" if head[:4] == b"RIFF" and head[8:12] == b"WEBP" else None

@app.get("/media/gallery/<name>")
def gallery_file(name):
    if not re.fullmatch(r"[0-9a-f]{16}\.(jpg|png|webp)", name):
        abort(404)
    r = send_from_directory(GALLERY_DIR, name)
    r.headers["Cache-Control"] = "public, max-age=604800, immutable"
    return r

@app.get("/api/public")
def public():
    c = db()
    q = lambda sql: [dict(r) for r in c.execute(sql)]
    return jsonify(barbers=q("SELECT id,name,role FROM barbers WHERE active=1"), services=q("SELECT id,name,description,price,minutes FROM services WHERE active=1"),
                   products=q("SELECT id,name,price,stock FROM products WHERE for_sale=1 AND active=1"))

@app.get("/api/slots")
def slots():
    try:
        svc = db().execute("SELECT minutes FROM services WHERE id=? AND active=1", (int(request.args["service"]),)).fetchone()
        return jsonify(free_slots(db(), int(request.args["barber"]), svc["minutes"], request.args["date"]))
    except (KeyError, ValueError, TypeError):
        return jsonify(error="Parâmetros inválidos"), 400

def spam_guard(j):
    """Honeypot, tempo mínimo de preenchimento e limite de tentativas por IP. Retorna resposta de erro ou None."""
    if j.get("website"):  # campo invisível: só robôs preenchem -> finge sucesso e não grava nada
        return jsonify(id=0, google_link="#"), 201
    try:
        if int(j.get("elapsed", 0)) < MIN_FORM_MS:
            return jsonify(error="Aguarde alguns segundos e confirme novamente."), 400
    except (TypeError, ValueError):
        return jsonify(error="Requisição inválida"), 400
    if too_many(request.remote_addr or "?", BOOK_PER_IP_HOUR):
        return jsonify(error="Muitas tentativas. Tente novamente mais tarde ou chame a barbearia no WhatsApp."), 429
    return None

@app.post("/api/appointments")
def book():
    j, c = body(), db()
    blocked = spam_guard(j)
    if blocked:
        return blocked
    name, phone = str(j.get("name", "")).strip()[:80], norm_phone(j.get("phone", ""))
    if len(name) < 2 or re.search(r"https?:|www\.|@", name, re.I):
        return jsonify(error="Informe seu nome."), 400
    if not j.get("consent"):
        return jsonify(error="Para agendar, aceite a Política de Privacidade e o recebimento de mensagens no WhatsApp."), 400
    consent_val = f"site {now():%Y-%m-%d %H:%M} v{POLICY_VERSION}"
    try:
        barber = c.execute("SELECT * FROM barbers WHERE id=? AND active=1", (int(j["barber"]),)).fetchone()
        svc = c.execute("SELECT * FROM services WHERE id=? AND active=1", (int(j["service"]),)).fetchone()
        start = P(f"{j['date']} {j['time']}")
    except (KeyError, ValueError, TypeError):
        return jsonify(error="Dados inválidos"), 400
    if not (name and phone and barber and svc):
        return jsonify(error="Informe nome, WhatsApp válido, barbeiro e serviço"), 400
    if start > now() + timedelta(days=MAX_DAYS_AHEAD):
        return jsonify(error=f"Só é possível agendar até {MAX_DAYS_AHEAD} dias à frente."), 400
    end = start + timedelta(minutes=svc["minutes"])
    c.execute("BEGIN IMMEDIATE")  # evita dupla reserva simultânea
    try:
        abertos = c.execute("SELECT COUNT(*) FROM appointments WHERE phone=? AND status='agendado' AND start_at>?",
                            (phone, now().strftime(FMT))).fetchone()[0]
        if abertos >= MAX_OPEN_PER_PHONE:
            c.execute("ROLLBACK")
            return jsonify(error=f"Você já tem {abertos} horário(s) marcado(s). Para marcar outro, cancele um deles pelo link da confirmação (WhatsApp) ou chame a barbearia."), 409
        if start.strftime("%H:%M") not in free_slots(c, barber["id"], svc["minutes"], start.strftime("%Y-%m-%d")):
            c.execute("ROLLBACK")
            return jsonify(error="Horário indisponível"), 409
        tok = new_token()
        cur = c.execute("INSERT INTO appointments(client_name,phone,barber_id,service_id,start_at,end_at,token,day_before_sent,consent) VALUES(?,?,?,?,?,?,?,?,?)",
                        (name, phone, barber["id"], svc["id"], start.strftime(FMT), end.strftime(FMT), tok, born_day_before(start), consent_val))
        if consent_val:  # novo consentimento desfaz um "parar de receber mensagens" anterior
            c.execute("DELETE FROM optout WHERE phone=?", (phone,))
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
    a = {"client_name": name, "phone": phone, "start_at": start.strftime(FMT), "end_at": end.strftime(FMT)}
    ev = gcal_create(a, barber, svc)
    if ev:
        c.execute("UPDATE appointments SET gcal_event_id=? WHERE id=?", (ev, cur.lastrowid))
    return jsonify(id=cur.lastrowid, google_link=client_gcal_link(a, svc), manage_url=manage_url(tok)), 201

# ---------- ERP ----------
@app.get("/admin")
@admin_required
def admin():
    return render_template("admin.html", demo=DEMO(), shop=shop())

@app.get("/api/admin/appointments")
@admin_required
def adm_appts():
    day = request.args.get("day", now().strftime("%Y-%m-%d"))
    rows = db().execute("""SELECT a.id,a.token,a.client_name,a.phone,a.start_at,a.status,b.name barber,s.name service,s.price
        FROM appointments a JOIN barbers b ON b.id=a.barber_id JOIN services s ON s.id=a.service_id
        WHERE a.start_at LIKE ? ORDER BY a.start_at""", (day + "%",))
    return jsonify([dict(r) for r in rows])

def parse_pay(j, price):
    """Forma de pagamento, desconto e gorjeta enviados ao concluir um atendimento (todos opcionais)."""
    method = str(j.get("method") or "").strip() or None
    discount, tip = round(float(j.get("discount") or 0), 2), round(float(j.get("tip") or 0), 2)
    if (method and method not in PAY_METHODS) or not (0 <= discount < price) or not (0 <= tip <= 10000):  # também rejeita NaN
        raise ValueError
    return method, discount, tip

def set_status(c, aid, st, method=None, discount=0.0, tip=0.0):
    """Conclui (lança o valor no caixa, com forma de pagamento/desconto/gorjeta), cancela ou marca falta. False se inválido."""
    a = c.execute("""SELECT a.*,s.name sname,s.price,b.calendar_id FROM appointments a
        JOIN services s ON s.id=a.service_id JOIN barbers b ON b.id=a.barber_id WHERE a.id=?""", (aid,)).fetchone()
    if not a or a["status"] != "agendado" or st not in ("concluido", "cancelado", "faltou"):
        return False
    c.execute("UPDATE appointments SET status=? WHERE id=?", (st, aid))
    if st == "concluido":
        day = a["start_at"][:10]
        c.execute("INSERT INTO transactions(kind,amount,day,description,category,barber_id,method,discount,appointment_id) VALUES('entrada',?,?,?,'Serviço',?,?,?,?)",
                  (round(a["price"] - discount, 2), day, f"{a['sname']} – {a['client_name']}", a["barber_id"], method, discount, aid))
        if tip > 0:  # gorjeta é do barbeiro: fica fora do cálculo de comissão
            c.execute("INSERT INTO transactions(kind,amount,day,description,category,barber_id,method,appointment_id) VALUES('entrada',?,?,?,'Gorjeta',?,?,?)",
                      (tip, day, f"Gorjeta ({a['sname']})", a["barber_id"], method, aid))
    else:
        gcal_delete(a["calendar_id"], a["gcal_event_id"])
    return True

def status_response(aid):
    c, j = db(), body()
    a = c.execute("SELECT s.price FROM appointments a JOIN services s ON s.id=a.service_id WHERE a.id=?", (aid,)).fetchone()
    try:
        pay = parse_pay(j, a["price"]) if a and j.get("status") == "concluido" else (None, 0.0, 0.0)
    except (ValueError, TypeError):
        return jsonify(error="Pagamento inválido: confira a forma de pagamento, o desconto (menor que o preço) e a gorjeta."), 400
    return jsonify(ok=True) if set_status(c, aid, j.get("status"), *pay) else (jsonify(error="Operação inválida"), 400)

@app.post("/api/admin/appointments/<int:aid>/status")
@admin_required
def adm_status(aid):
    return status_response(aid)

@app.post("/api/admin/appointments")
@admin_required
def adm_book():
    """Agendamento feito pela barbearia (telefone/balcão). WhatsApp é opcional; sem os limites anti-spam do site;
    aceita horário fora da grade de 15 min e, se 'concluido', já lança o atendimento no caixa."""
    j, c = body(), db()
    name, raw = str(j.get("name", "")).strip()[:80], str(j.get("phone", "")).strip()
    phone = norm_phone(raw) if raw else ""
    consent_val = f"balcao {now():%Y-%m-%d %H:%M} v{POLICY_VERSION}" if (j.get("consent") and phone) else None  # sem consentimento, não há mensagens automáticas
    try:
        barber = c.execute("SELECT * FROM barbers WHERE id=? AND active=1", (int(j["barber"]),)).fetchone()
        svc = c.execute("SELECT * FROM services WHERE id=? AND active=1", (int(j["service"]),)).fetchone()
        start = P(f"{j['date']} {j['time']}")
    except (KeyError, ValueError, TypeError):
        return jsonify(error="Dados inválidos"), 400
    if len(name) < 2 or (raw and not phone) or not (barber and svc):
        return jsonify(error="Informe o nome, um WhatsApp válido (ou deixe em branco), o barbeiro e o serviço."), 400
    end = start + timedelta(minutes=svc["minutes"])
    problem = schedule_problem(c, barber["id"], start, end)
    if problem and not j.get("force"):  # aviso, não bloqueio: o painel pergunta e reenvia com force=true
        return jsonify(error=problem, can_force=True), 409
    if j.get("concluido") and start > now():
        return jsonify(error="Não dá para marcar como atendido um horário que ainda não chegou."), 400
    try:
        pay = parse_pay(j, svc["price"]) if j.get("concluido") else (None, 0.0, 0.0)
    except (ValueError, TypeError):
        return jsonify(error="Pagamento inválido: confira a forma de pagamento, o desconto (menor que o preço) e a gorjeta."), 400
    c.execute("BEGIN IMMEDIATE")
    try:
        if c.execute("SELECT 1 FROM appointments WHERE barber_id=? AND status='agendado' AND start_at<? AND end_at>?",
                     (barber["id"], end.strftime(FMT), start.strftime(FMT))).fetchone():
            c.execute("ROLLBACK")
            return jsonify(error="Esse barbeiro já tem atendimento nesse horário."), 409
        tok = new_token()
        cur = c.execute("INSERT INTO appointments(client_name,phone,barber_id,service_id,start_at,end_at,token,day_before_sent,consent) VALUES(?,?,?,?,?,?,?,?,?)",
                        (name, phone, barber["id"], svc["id"], start.strftime(FMT), end.strftime(FMT), tok, born_day_before(start), consent_val))
        if consent_val:  # novo consentimento desfaz um "parar de receber mensagens" anterior
            c.execute("DELETE FROM optout WHERE phone=?", (phone,))
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
    a = {"client_name": name, "phone": phone, "start_at": start.strftime(FMT), "end_at": end.strftime(FMT)}
    ev = gcal_create(a, barber, svc)
    if ev:
        c.execute("UPDATE appointments SET gcal_event_id=? WHERE id=?", (ev, cur.lastrowid))
    if j.get("concluido"):
        set_status(c, cur.lastrowid, "concluido", *pay)
    return jsonify(id=cur.lastrowid, manage_url=manage_url(tok)), 201

@app.get("/api/admin/summary")
@admin_required
def summary():
    m = request.args.get("month", now().strftime("%Y-%m"))
    if not re.fullmatch(r"\d{4}-\d{2}", m):
        return jsonify(error="Mês inválido"), 400
    c, like = db(), m + "%"
    tot = lambda k: c.execute("SELECT COALESCE(SUM(amount),0) FROM transactions WHERE kind=? AND day LIKE ?", (k, like)).fetchone()[0]
    barbers = []
    for r in c.execute("""SELECT b.name,b.commission_pct pct,
        COALESCE(SUM(CASE WHEN t.category<>'Gorjeta' THEN t.amount END),0) gross,
        COUNT(CASE WHEN t.category<>'Gorjeta' THEN 1 END) n,
        COALESCE(SUM(CASE WHEN t.category='Gorjeta' THEN t.amount END),0) tips FROM barbers b
        LEFT JOIN transactions t ON t.barber_id=b.id AND t.kind='entrada' AND t.day LIKE ? GROUP BY b.id""", (like,)):
        barbers.append({**dict(r), "commission": round(r["gross"] * r["pct"] / 100, 2)})  # comissão sobre o valor líquido; gorjeta é repassada integralmente
    cats = [dict(r) for r in c.execute("""SELECT kind,category,SUM(amount) total FROM transactions WHERE day LIKE ?
        GROUP BY kind,category ORDER BY total DESC""", (like,))]
    tx = [dict(r) for r in c.execute("SELECT * FROM transactions WHERE day LIKE ? ORDER BY day DESC,id DESC", (like,))]
    by_method = {r["m"]: round(r["t"], 2) for r in c.execute(
        "SELECT COALESCE(method,'sem') m,SUM(amount) t FROM transactions WHERE kind='entrada' AND day LIKE ? GROUP BY m", (like,))}
    discounts = c.execute("SELECT COALESCE(SUM(discount),0) FROM transactions WHERE day LIKE ?", (like,)).fetchone()[0]
    return jsonify(income=tot("entrada"), expense=tot("saida"), balance=tot("entrada") - tot("saida"),
                   barbers=barbers, categories=cats, transactions=tx, by_method=by_method, discounts=discounts)

@app.post("/api/admin/transactions")
@admin_required
def add_tx():
    j = body()
    try:
        amount, day = round(float(j["amount"]), 2), datetime.strptime(j["day"], "%Y-%m-%d").strftime("%Y-%m-%d")
        method = str(j.get("method") or "").strip() or None
        assert amount > 0 and j["kind"] in ("entrada", "saida") and (method is None or method in PAY_METHODS)
    except (KeyError, ValueError, TypeError, AssertionError):
        return jsonify(error="Dados inválidos"), 400
    db().execute("INSERT INTO transactions(kind,amount,day,description,category,method) VALUES(?,?,?,?,?,?)",
                 (j["kind"], amount, day, str(j.get("description", ""))[:120], str(j.get("category", "Gasto"))[:40], method))
    return jsonify(ok=True), 201

@app.get("/api/admin/products")
@admin_required
def adm_products():
    return jsonify([dict(r) for r in db().execute("SELECT * FROM products ORDER BY active DESC,for_sale DESC,name")])

@app.post("/api/admin/sales")
@admin_required
def sale():
    j, c = body(), db()
    try:
        qty, p = int(j["qty"]), c.execute("SELECT * FROM products WHERE id=? AND for_sale=1 AND active=1", (int(j["product"]),)).fetchone()
        method = str(j.get("method") or "").strip() or None
        assert qty > 0 and p and p["stock"] >= qty and (method is None or method in PAY_METHODS)
    except (KeyError, ValueError, TypeError, AssertionError):
        return jsonify(error="Venda inválida ou estoque insuficiente"), 400
    day = now().strftime("%Y-%m-%d")
    c.execute("BEGIN")
    c.execute("UPDATE products SET stock=stock-? WHERE id=?", (qty, p["id"]))
    c.execute("INSERT INTO stock_moves(product_id,qty,day,reason) VALUES(?,?,?,'venda')", (p["id"], -qty, day))
    c.execute("INSERT INTO transactions(kind,amount,day,description,category,method) VALUES('entrada',?,?,?,'Produto',?)",
              (p["price"] * qty, day, f"{qty}x {p['name']}", method))
    c.execute("COMMIT")
    return jsonify(ok=True)

@app.post("/api/admin/stock")
@admin_required
def stock_move():
    """qty>0: compra/entrada (cost opcional vira despesa). qty<0: consumo de material."""
    j, c = body(), db()
    try:
        qty, cost = int(j["qty"]), float(j.get("cost") or 0)
        p = c.execute("SELECT * FROM products WHERE id=? AND active=1", (int(j["product"]),)).fetchone()
        assert qty != 0 and p and p["stock"] + qty >= 0 and cost >= 0
    except (KeyError, ValueError, TypeError, AssertionError):
        return jsonify(error="Movimentação inválida"), 400
    day = now().strftime("%Y-%m-%d")
    c.execute("BEGIN")
    c.execute("UPDATE products SET stock=stock+? WHERE id=?", (qty, p["id"]))
    c.execute("INSERT INTO stock_moves(product_id,qty,day,reason) VALUES(?,?,?,?)", (p["id"], qty, day, "compra" if qty > 0 else "consumo"))
    if qty > 0 and cost > 0:
        c.execute("INSERT INTO transactions(kind,amount,day,description,category) VALUES('saida',?,?,?,'Compra de material')",
                  (cost, day, f"{qty}x {p['name']}"))
    c.execute("COMMIT")
    return jsonify(ok=True)


# ---------- cadastros (barbeiros, serviços, produtos) ----------
def _txt(n, req=True):
    def f(v):
        v = str(v or "").strip()[:n]
        if req and not v:
            raise ValueError
        return v
    return f

def _num(lo, hi, cast=float):
    def f(v):
        x = cast(v)
        if not lo <= x <= hi:  # também rejeita NaN
            raise ValueError
        return round(x, 2) if cast is float else x
    return f

_bool = lambda v: 1 if v in (1, True, "1", "true", "on") else 0

BARBER_COLS = {"name": _txt(60), "role": _txt(60, False), "calendar_id": _txt(200, False),
               "commission_pct": _num(0, 100), "active": _bool}
SERVICE_COLS = {"name": _txt(60), "description": _txt(200, False), "price": _num(0.01, 10000),
                "minutes": _num(5, 480, int), "active": _bool}
PRODUCT_COLS = {"name": _txt(60), "price": _num(0, 10000), "stock": _num(0, 100000, int),
                "for_sale": _bool, "active": _bool}

def save_row(table, cols, required, j, no_update=(), future_col=None):
    """Cria (sem id) ou atualiza (com id). Nomes de tabela/coluna vêm só do código, nunca do cliente."""
    c = db()
    try:
        vals = {k: f(j[k]) for k, f in cols.items() if k in j}
        rid = j.get("id")
        if rid is None:
            if not required <= vals.keys():
                raise ValueError
            cur = c.execute(f"INSERT INTO {table}({','.join(vals)}) VALUES({','.join('?' * len(vals))})", list(vals.values()))
            return jsonify(ok=True, id=cur.lastrowid), 201
        rid = int(rid)
        for k in no_update:
            vals.pop(k, None)
        if not vals:
            raise ValueError
        if future_col and vals.get("active") == 0:
            n = c.execute(f"SELECT COUNT(*) FROM appointments WHERE {future_col}=? AND status='agendado' AND start_at>?",
                          (rid, now().strftime(FMT))).fetchone()[0]
            if n:
                return jsonify(error=f"Há {n} agendamento(s) futuro(s). Cancele ou conclua antes de desativar."), 409
        cur = c.execute(f"UPDATE {table} SET {','.join(k + '=?' for k in vals)} WHERE id=?", [*vals.values(), rid])
        return jsonify(ok=True, id=rid) if cur.rowcount else (jsonify(error="Não encontrado"), 404)
    except (ValueError, TypeError):
        return jsonify(error="Dados inválidos"), 400

@app.get("/api/admin/barbers")
@admin_required
def adm_barbers():
    return jsonify([dict(r) for r in db().execute("SELECT id,name,role,calendar_id,commission_pct,active,login FROM barbers ORDER BY active DESC,name")])

@app.post("/api/admin/barbers")
@admin_required
def adm_barbers_save():
    return save_row("barbers", BARBER_COLS, {"name"}, body(), future_col="barber_id")

@app.get("/api/admin/services")
@admin_required
def adm_services():
    return jsonify([dict(r) for r in db().execute("SELECT * FROM services ORDER BY active DESC,name")])

@app.post("/api/admin/services")
@admin_required
def adm_services_save():
    return save_row("services", SERVICE_COLS, {"name", "price", "minutes"}, body(), future_col="service_id")

@app.post("/api/admin/products")
@admin_required
def adm_products_save():
    return save_row("products", PRODUCT_COLS, {"name", "price"}, body(), no_update=("stock",))  # estoque só muda por movimentação

# ---------- Google Agenda ----------
@app.get("/api/admin/gcal")
@admin_required
def gcal_status():
    configured = bool(os.getenv("GOOGLE_SA_FILE") or os.getenv("GOOGLE_SA_JSON"))
    try:
        info = _sa_info()
        return jsonify(configured=configured, ok=bool(info), email=(info or {}).get("client_email"))
    except Exception as e:
        return jsonify(configured=configured, ok=False, email=None, error=str(e)[:200])

@app.post("/api/admin/gcal/test")
@admin_required
def gcal_test():
    """Cria e apaga um evento de teste na agenda do barbeiro, provando que o acesso de escrita funciona."""
    b = db().execute("SELECT name,calendar_id FROM barbers WHERE id=?", (body().get("barber"),)).fetchone()
    if not b or not b["calendar_id"]:
        return jsonify(error="Barbeiro sem ID de agenda cadastrado."), 400
    try:
        s = gcal()
        if not s:
            return jsonify(error="Conta de serviço não configurada no servidor (GOOGLE_SA_FILE)."), 400
        t = now() + timedelta(days=1)
        ev = s.events().insert(calendarId=b["calendar_id"], body={
            "summary": "Teste de conexão (pode apagar)",
            "start": {"dateTime": t.isoformat(), "timeZone": "America/Sao_Paulo"},
            "end": {"dateTime": (t + timedelta(minutes=5)).isoformat(), "timeZone": "America/Sao_Paulo"}}).execute()
        s.events().delete(calendarId=b["calendar_id"], eventId=ev["id"]).execute()
        return jsonify(ok=True)
    except Exception as e:
        log.exception("Teste do Google Agenda falhou")
        return jsonify(error=f"Falhou: {str(e)[:180]}. Confira o ID da agenda e se ela foi compartilhada com a conta de serviço (\"Fazer alterações\")."), 400

@app.post("/api/admin/gcal/sync")
@admin_required
def gcal_sync():
    """Cria no Google Agenda os agendamentos futuros que ainda não têm evento (ex.: feitos antes de ligar a integração)."""
    c, done = db(), 0
    rows = c.execute("""SELECT a.id,a.client_name,a.phone,a.start_at,a.end_at,a.barber_id,a.service_id FROM appointments a
        WHERE a.status='agendado' AND a.gcal_event_id IS NULL AND a.start_at>? ORDER BY a.start_at""", (now().strftime(FMT),)).fetchall()
    for a in rows:
        barber = c.execute("SELECT * FROM barbers WHERE id=?", (a["barber_id"],)).fetchone()
        svc = c.execute("SELECT * FROM services WHERE id=?", (a["service_id"],)).fetchone()
        ev = gcal_create(dict(a), barber, svc)
        if ev:
            c.execute("UPDATE appointments SET gcal_event_id=? WHERE id=?", (ev, a["id"]))
            done += 1
    return jsonify(ok=True, synced=done, pending=len(rows) - done)

# ---------- backup pelo painel ----------
@app.get("/api/admin/backup")
@admin_required
def backup_download():
    p = backup_db()
    return send_file(p, as_attachment=True, download_name=os.path.basename(p), mimetype="application/octet-stream")

@app.get("/api/admin/backup/status")
@admin_required
def backup_status():
    n = len([f for f in os.listdir(BACKUP_DIR) if f.startswith("barbearia-")]) if os.path.isdir(BACKUP_DIR) else 0
    return jsonify(local_count=n, keep_days=BACKUP_KEEP_DAYS, last_local=get_meta("backup_local"),
                   remote=os.getenv("RCLONE_REMOTE") or None, rclone=bool(shutil.which("rclone")),
                   remote_keep_days=REMOTE_KEEP_DAYS, last_offsite=get_meta("offsite_ok"), offsite_error=get_meta("offsite_error"))

@app.post("/api/admin/backup/upload")
@admin_required
def backup_upload():
    if not os.getenv("RCLONE_REMOTE"):
        return jsonify(error="Backup na nuvem não configurado (defina RCLONE_REMOTE no servidor)."), 400
    _, err = run_backup()
    return (jsonify(error=f"Falha ao enviar: {err}"), 400) if err else jsonify(ok=True)

# ---------- cliente: cancelar / remarcar por link ----------
def get_by_token(c, token):
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", token or ""):
        return None
    return c.execute("""SELECT a.*,b.name bname,s.name sname,s.minutes FROM appointments a
        JOIN barbers b ON b.id=a.barber_id JOIN services s ON s.id=a.service_id WHERE a.token=?""", (token,)).fetchone()

def change_rule(a):
    """(pode_alterar, motivo): só agendamentos ativos e com antecedência mínima."""
    if a["status"] != "agendado":
        return False, {"cancelado": "Este agendamento foi cancelado.", "concluido": "Este atendimento já foi realizado."}.get(a["status"], "Este agendamento foi encerrado.")
    if P(a["start_at"]) <= now():
        return False, "Este horário já passou."
    if P(a["start_at"]) - now() < timedelta(hours=CHANGE_DEADLINE_H):
        return False, f"Faltam menos de {CHANGE_DEADLINE_H}h para o horário. Para alterar, fale com a barbearia."
    return True, ""

def manage_open(token):
    """Valida limite de tentativas e o link. Retorna (agendamento, resposta_de_erro)."""
    if too_many("m:" + (request.remote_addr or "?"), MANAGE_PER_IP_HOUR):
        return None, (jsonify(error="Muitas tentativas. Tente novamente mais tarde."), 429)
    a = get_by_token(db(), token)
    return (a, None) if a else (None, (jsonify(error="Link inválido ou expirado."), 404))

@app.get("/agendamento/<token>")
def manage_page(token):
    r = app.make_response(render_template("manage.html", shop=shop(), token=token))
    r.headers.update({"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex"})
    return r

@app.get("/api/manage/<token>")
def manage_info(token):
    a, err = manage_open(token)
    if err:
        return err
    ok, why = change_rule(a)
    return jsonify(client=a["client_name"], service=a["sname"], barber=a["bname"], start_at=a["start_at"], end_at=a["end_at"],
                   status=a["status"], phone_hint=("•••• " + a["phone"][-4:]) if a["phone"] else "", can_change=ok, reason=why,
                   reschedules_left=max(0, MAX_RESCHEDULES - a["reschedules"]), deadline_hours=CHANGE_DEADLINE_H,
                   address=shop()["address"], max_days=MAX_DAYS_AHEAD,
                   has_phone=bool(a["phone"]), optout=bool(a["phone"] and db().execute("SELECT 1 FROM optout WHERE phone=?", (a["phone"],)).fetchone()))

@app.get("/api/manage/<token>/slots")
def manage_slots(token):
    a, err = manage_open(token)
    if err:
        return err
    try:
        day = datetime.strptime(request.args["date"], "%Y-%m-%d").strftime("%Y-%m-%d")
    except (KeyError, ValueError):
        return jsonify(error="Data inválida"), 400
    return jsonify(free_slots(db(), a["barber_id"], a["minutes"], day, exclude_id=a["id"]))

@app.post("/api/manage/<token>/cancel")
def manage_cancel(token):
    a, err = manage_open(token)
    if err:
        return err
    ok, why = change_rule(a)
    if not ok:
        return jsonify(error=why), 409
    set_status(db(), a["id"], "cancelado")  # libera o horário e remove o evento do Google Agenda
    return jsonify(ok=True)

@app.post("/api/manage/<token>/reschedule")
def manage_reschedule(token):
    a, err = manage_open(token)
    if err:
        return err
    c, j = db(), body()
    ok, why = change_rule(a)
    if not ok:
        return jsonify(error=why), 409
    if a["reschedules"] >= MAX_RESCHEDULES:
        return jsonify(error=f"Limite de {MAX_RESCHEDULES} remarcações atingido. Fale com a barbearia."), 409
    try:
        start = P(f"{j['date']} {j['time']}")
    except (KeyError, ValueError, TypeError):
        return jsonify(error="Dados inválidos"), 400
    if start > now() + timedelta(days=MAX_DAYS_AHEAD):
        return jsonify(error=f"Só é possível agendar até {MAX_DAYS_AHEAD} dias à frente."), 400
    end = start + timedelta(minutes=a["minutes"])
    c.execute("BEGIN IMMEDIATE")
    try:
        if start.strftime("%H:%M") not in free_slots(c, a["barber_id"], a["minutes"], start.strftime("%Y-%m-%d"), exclude_id=a["id"]):
            c.execute("ROLLBACK")
            return jsonify(error="Horário indisponível"), 409
        c.execute("UPDATE appointments SET start_at=?,end_at=?,reschedules=reschedules+1,confirm_sent=0,reminder_sent=0,day_before_sent=? WHERE id=?",
                  (start.strftime(FMT), end.strftime(FMT), born_day_before(start), a["id"]))
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
    barber = c.execute("SELECT * FROM barbers WHERE id=?", (a["barber_id"],)).fetchone()
    svc = c.execute("SELECT * FROM services WHERE id=?", (a["service_id"],)).fetchone()
    gcal_delete(barber["calendar_id"], a["gcal_event_id"])
    ev = gcal_create({"client_name": a["client_name"], "phone": a["phone"], "start_at": start.strftime(FMT), "end_at": end.strftime(FMT)}, barber, svc)
    c.execute("UPDATE appointments SET gcal_event_id=? WHERE id=?", (ev, a["id"]))
    return jsonify(ok=True, start_at=start.strftime(FMT))

# ---------- horários, folgas e feriados ----------
def _hhmm(v, req=True):
    v = str(v or "").strip()
    if not v:
        if req:
            raise ValueError
        return None
    return datetime.strptime(v, "%H:%M").strftime("%H:%M")

def _ymd(v):
    return datetime.strptime(str(v), "%Y-%m-%d").strftime("%Y-%m-%d")

def future_conflicts(c, barber_id, d1, d2, tf=None, tt=None):
    """Quantos agendamentos futuros já marcados caem dentro de uma folga/período (barber_id None = todos)."""
    sql, p = "SELECT COUNT(*) FROM appointments WHERE status='agendado' AND start_at>? AND substr(start_at,1,10) BETWEEN ? AND ?", [now().strftime(FMT), d1, d2]
    if barber_id is not None:
        sql += " AND barber_id=?"
        p.append(barber_id)
    if tf and tt:
        sql += " AND substr(start_at,12,5)<? AND substr(end_at,12,5)>?"
        p += [tt, tf]
    return c.execute(sql, p).fetchone()[0]

@app.get("/api/admin/hours")
@admin_required
def adm_hours():
    try:
        bid = int(request.args["barber"])
    except (KeyError, ValueError):
        return jsonify(error="Barbeiro inválido"), 400
    if not db().execute("SELECT 1 FROM barbers WHERE id=?", (bid,)).fetchone():
        return jsonify(error="Não encontrado"), 404
    h = hours_for(db(), bid)
    return jsonify([dict(h[d]) for d in range(7)])

@app.post("/api/admin/hours")
@admin_required
def adm_hours_save():
    j, c = body(), db()
    try:
        bid, days, rows = int(j["barber"]), j["days"], []
        if not c.execute("SELECT 1 FROM barbers WHERE id=?", (bid,)).fetchone() or not isinstance(days, list) or len(days) != 7:
            raise ValueError
        for d in days:
            wd, is_open = int(d["weekday"]), 1 if d.get("open") else 0
            ts, te = _hhmm(d.get("t_start") or f"{OPEN_H:02d}:00"), _hhmm(d.get("t_end") or f"{CLOSE_H:02d}:00")
            ls, le = _hhmm(d.get("lunch_start"), False), _hhmm(d.get("lunch_end"), False)
            if not 0 <= wd <= 6 or (is_open and te <= ts) or bool(ls) != bool(le) or (ls and not ts <= ls < le <= te):
                raise ValueError
            rows.append((bid, wd, is_open, ts, te, ls, le))
        if sorted(r[1] for r in rows) != list(range(7)):
            raise ValueError
    except (KeyError, ValueError, TypeError, AttributeError):
        return jsonify(error="Horários inválidos. Confira início, fim e almoço de cada dia."), 400
    c.execute("BEGIN")
    c.execute("DELETE FROM work_hours WHERE barber_id=?", (bid,))
    c.executemany("INSERT INTO work_hours(barber_id,weekday,open,t_start,t_end,lunch_start,lunch_end) VALUES(?,?,?,?,?,?,?)", rows)
    c.execute("COMMIT")
    fut = c.execute("SELECT start_at,end_at FROM appointments WHERE barber_id=? AND status='agendado' AND start_at>?", (bid, now().strftime(FMT))).fetchall()
    conflicts = sum(1 for a in fut if schedule_problem(c, bid, P(a["start_at"]), P(a["end_at"])))
    return jsonify(ok=True, conflicts=conflicts)

@app.get("/api/admin/timeoff")
@admin_required
def adm_timeoff():
    rows = db().execute("""SELECT t.*,COALESCE(b.name,'Todos') barber FROM time_off t LEFT JOIN barbers b ON b.id=t.barber_id
        WHERE t.day_to>=? ORDER BY t.day_from,t.id""", (now().strftime("%Y-%m-%d"),))
    return jsonify([dict(r) for r in rows])

def create_timeoff(c, j, bid):
    """Cria folga/bloqueio (bid None = loja inteira). Usado pelo painel e pelo barbeiro (só para si)."""
    try:
        d1, d2 = _ymd(j["from"]), _ymd(j.get("to") or j["from"])
        tf, tt = _hhmm(j.get("t_from"), False), _hhmm(j.get("t_to"), False)
        if d2 < d1 or bool(tf) != bool(tt) or (tf and tt <= tf):
            raise ValueError
        reason = _txt(80, False)(j.get("reason"))
    except (KeyError, ValueError, TypeError):
        return jsonify(error="Dados inválidos. Confira as datas e o horário."), 400
    cur = c.execute("INSERT INTO time_off(barber_id,day_from,day_to,t_from,t_to,reason) VALUES(?,?,?,?,?,?)", (bid, d1, d2, tf, tt, reason))
    return jsonify(ok=True, id=cur.lastrowid, conflicts=future_conflicts(c, bid, d1, d2, tf, tt)), 201

@app.post("/api/admin/timeoff")
@admin_required
def adm_timeoff_add():
    j, c = body(), db()
    try:
        bid = j.get("barber")
        bid = int(bid) if bid not in (None, "", "all") else None
        if bid is not None and not c.execute("SELECT 1 FROM barbers WHERE id=?", (bid,)).fetchone():
            raise ValueError
    except (ValueError, TypeError):
        return jsonify(error="Dados inválidos. Confira as datas e o horário."), 400
    return create_timeoff(c, j, bid)

@app.post("/api/admin/timeoff/delete")
@admin_required
def adm_timeoff_del():
    try:
        tid = int(body()["id"])
    except (KeyError, ValueError, TypeError):
        return jsonify(error="Dados inválidos"), 400
    cur = db().execute("DELETE FROM time_off WHERE id=?", (tid,))
    return jsonify(ok=True) if cur.rowcount else (jsonify(error="Não encontrado"), 404)

def easter(y):
    """Domingo de Páscoa (algoritmo de Meeus/Jones/Butcher)."""
    a, b, c = y % 19, y // 100, y % 100
    d, e, f = b // 4, b % 4, (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    return date(y, (h + l - 7 * m + 114) // 31, (h + l - 7 * m + 114) % 31 + 1)

def br_holidays(y):
    """Feriados nacionais (e Carnaval/Corpus Christi, que são pontos facultativos) de um ano."""
    e = easter(y)
    fixed = [(1, 1, "Confraternização Universal"), (4, 21, "Tiradentes"), (5, 1, "Dia do Trabalho"), (9, 7, "Independência do Brasil"),
             (10, 12, "Nossa Senhora Aparecida"), (11, 2, "Finados"), (11, 15, "Proclamação da República"),
             (11, 20, "Consciência Negra"), (12, 25, "Natal")]
    out = [(date(y, m, d), n) for m, d, n in fixed]
    out += [(e - timedelta(days=48), "Carnaval (segunda)"), (e - timedelta(days=47), "Carnaval (terça)"),
            (e - timedelta(days=2), "Sexta-feira Santa"), (e + timedelta(days=60), "Corpus Christi")]
    return sorted(out)

@app.post("/api/admin/holidays")
@admin_required
def adm_holidays():
    """Cadastra os feriados nacionais do ano como folga de TODOS os barbeiros (apague os que a barbearia abrir)."""
    c = db()
    try:
        y = int(body()["year"])
        assert 2000 <= y <= 2100
    except (KeyError, ValueError, TypeError, AssertionError):
        return jsonify(error="Ano inválido"), 400
    added = 0
    for d, name in br_holidays(y):
        ds = d.isoformat()
        if not c.execute("SELECT 1 FROM time_off WHERE barber_id IS NULL AND day_from=? AND day_to=?", (ds, ds)).fetchone():
            c.execute("INSERT INTO time_off(barber_id,day_from,day_to,reason) VALUES(NULL,?,?,?)", (ds, ds, name))
            added += 1
    return jsonify(ok=True, added=added)

# ---------- fechamento de caixa do dia ----------
def closing_data(c, day):
    """Totais do dia por forma de pagamento + conferência do dinheiro (abertura + entradas em dinheiro - saídas em dinheiro)."""
    one = lambda sql, *p: c.execute(sql, p).fetchone()[0]
    by = {r["m"]: round(r["t"], 2) for r in c.execute(
        "SELECT COALESCE(method,'sem') m,SUM(amount) t FROM transactions WHERE kind='entrada' AND day=? GROUP BY m", (day,))}
    income, expense = round(sum(by.values()), 2), round(one("SELECT COALESCE(SUM(amount),0) FROM transactions WHERE kind='saida' AND day=?", day), 2)
    cash_in, cash_out = by.get("dinheiro", 0.0), round(one("SELECT COALESCE(SUM(amount),0) FROM transactions WHERE kind='saida' AND method='dinheiro' AND day=?", day), 2)
    barbers = [dict(r) for r in c.execute("""SELECT b.name,COUNT(CASE WHEN t.category='Serviço' THEN 1 END) n,
        COALESCE(SUM(CASE WHEN t.category<>'Gorjeta' THEN t.amount END),0) gross,COALESCE(SUM(CASE WHEN t.category='Gorjeta' THEN t.amount END),0) tips
        FROM barbers b JOIN transactions t ON t.barber_id=b.id AND t.kind='entrada' AND t.day=? GROUP BY b.id""", (day,))]
    prev = c.execute("SELECT counted FROM cash_closings WHERE day<? ORDER BY day DESC LIMIT 1", (day,)).fetchone()
    cl = c.execute("SELECT * FROM cash_closings WHERE day=?", (day,)).fetchone()
    d = {"day": day, "by_method": by, "income": income, "expense": expense, "balance": round(income - expense, 2),
         "cash_in": cash_in, "cash_out": cash_out, "barbers": barbers, "closing": None,
         "services": one("SELECT COUNT(*) FROM transactions WHERE category='Serviço' AND day=?", day),
         "tips": round(one("SELECT COALESCE(SUM(amount),0) FROM transactions WHERE category='Gorjeta' AND day=?", day), 2),
         "discounts": round(one("SELECT COALESCE(SUM(discount),0) FROM transactions WHERE day=?", day), 2),
         "suggested_opening": prev["counted"] if prev else 0.0}
    if cl:
        d["closing"] = {"opening": cl["opening"], "expected": cl["expected"], "counted": cl["counted"], "diff": round(cl["counted"] - cl["expected"], 2),
                        "notes": cl["notes"] or "", "closed_at": cl["closed_at"],
                        "stale": abs(round(cl["opening"] + cash_in - cash_out, 2) - cl["expected"]) > 0.004}  # houve lançamentos depois do fechamento
    return d

@app.get("/api/admin/closing")
@admin_required
def closing_get():
    c = db()
    try:
        day = _ymd(request.args.get("day") or now().strftime("%Y-%m-%d"))
    except ValueError:
        return jsonify(error="Data inválida"), 400
    d = closing_data(c, day)
    d["recent"] = [{**dict(r), "diff": round(r["counted"] - r["expected"], 2)} for r in c.execute(
        "SELECT day,expected,counted FROM cash_closings ORDER BY day DESC LIMIT 7")]
    return jsonify(d)

@app.post("/api/admin/closing")
@admin_required
def closing_save():
    j, c = body(), db()
    try:
        day, opening, counted = _ymd(j["day"]), round(float(j.get("opening") or 0), 2), round(float(j["counted"]), 2)
        assert opening >= 0 and counted >= 0  # também rejeita NaN
    except (KeyError, ValueError, TypeError, AssertionError):
        return jsonify(error="Dados inválidos: informe a data e o dinheiro contado na gaveta."), 400
    d = closing_data(c, day)
    c.execute("""INSERT INTO cash_closings(day,opening,expected,counted,notes,closed_at) VALUES(?,?,?,?,?,?)
        ON CONFLICT(day) DO UPDATE SET opening=excluded.opening,expected=excluded.expected,counted=excluded.counted,notes=excluded.notes,closed_at=excluded.closed_at""",
              (day, opening, round(opening + d["cash_in"] - d["cash_out"], 2), counted, _txt(200, False)(j.get("notes")), now().strftime(FMT)))
    return jsonify(closing_data(c, day))

# ---------- privacidade (LGPD) ----------
@app.get("/privacidade")
def privacy_page():
    return render_template("privacy.html", shop=shop(), contact=PRIVACY_CONTACT, retention=RETENTION_MONTHS, version=POLICY_VERSION)

@app.post("/api/manage/<token>/optout")
def manage_optout(token):
    """O cliente deixa de receber mensagens automáticas (revoga o consentimento). Novo agendamento com aceite refaz o consentimento."""
    a, err = manage_open(token)
    if err:
        return err
    if a["phone"]:
        db().execute("INSERT OR IGNORE INTO optout(phone,at) VALUES(?,?)", (a["phone"], now().strftime(FMT)))
    return jsonify(ok=True)

def erase_phone(c, phone):
    """Anonimiza os dados pessoais de um cliente (nome/telefone/consentimento) mantendo os registros financeiros sem identificação."""
    rows = c.execute("""SELECT a.id,a.client_name,a.start_at,a.barber_id,a.gcal_event_id,s.name sname,b.calendar_id FROM appointments a
        JOIN services s ON s.id=a.service_id JOIN barbers b ON b.id=a.barber_id WHERE a.phone=?""", (phone,)).fetchall()
    for r in rows:
        c.execute("UPDATE transactions SET description=? WHERE appointment_id=? AND category='Serviço'", (f"{r['sname']} – Cliente removido", r["id"]))
        c.execute("UPDATE transactions SET description=? WHERE description=? AND day=? AND category='Serviço' AND barber_id=?",
                  (f"{r['sname']} – Cliente removido", f"{r['sname']} – {r['client_name']}", r["start_at"][:10], r["barber_id"]))
        if r["gcal_event_id"]:
            gcal_delete(r["calendar_id"], r["gcal_event_id"])  # o evento do Google Agenda também guarda nome e telefone
    c.execute("UPDATE appointments SET client_name='Cliente removido',phone='',consent=NULL,gcal_event_id=NULL WHERE phone=?", (phone,))
    c.execute("DELETE FROM optout WHERE phone=?", (phone,))
    c.execute("DELETE FROM client_notes WHERE phone=?", (phone,))
    return len(rows)

def retention_sweep():
    """Anonimiza clientes cujo último atendimento foi há mais de RETENTION_MONTHS meses."""
    if RETENTION_MONTHS <= 0:
        return 0
    c, limit, n = connect(), (now() - timedelta(days=round(RETENTION_MONTHS * 30.44))).strftime(FMT), 0
    try:
        for r in c.execute("SELECT phone FROM appointments WHERE phone<>'' GROUP BY phone HAVING MAX(start_at)<?", (limit,)).fetchall():
            erase_phone(c, r["phone"])
            n += 1
    finally:
        c.close()
    return n

def scheduled_retention():
    try:
        n = retention_sweep()
        if n:
            log.info("Retenção de dados: %s cliente(s) anonimizado(s)", n)
    except Exception:
        log.exception("Falha na rotina de retenção de dados")

def _phone_arg(v):
    p = norm_phone(v or "")
    if not p:
        raise ValueError
    return p

@app.get("/api/admin/privacy/export")
@admin_required
def privacy_export():
    """Direito de acesso: tudo o que o sistema guarda sobre um telefone."""
    try:
        phone = _phone_arg(request.args.get("phone"))
    except ValueError:
        return jsonify(error="Informe um WhatsApp válido."), 400
    rows = db().execute("""SELECT a.id,a.client_name nome,a.phone telefone,a.start_at inicio,a.status,a.consent consentimento,s.name servico,b.name barbeiro
        FROM appointments a JOIN services s ON s.id=a.service_id JOIN barbers b ON b.id=a.barber_id WHERE a.phone=? ORDER BY a.start_at""", (phone,)).fetchall()
    out = db().execute("SELECT at FROM optout WHERE phone=?", (phone,)).fetchone()
    note = db().execute("SELECT note FROM client_notes WHERE phone=?", (phone,)).fetchone()
    return jsonify(phone=phone, agendamentos=[dict(r) for r in rows], parou_mensagens_em=out["at"] if out else None, anotacao=note["note"] if note else None)

@app.post("/api/admin/privacy/erase")
@admin_required
def privacy_erase():
    """Direito de eliminação: anonimiza o cliente (recusa se ainda houver agendamento futuro)."""
    c = db()
    try:
        phone = _phone_arg(body().get("phone"))
    except ValueError:
        return jsonify(error="Informe um WhatsApp válido."), 400
    if c.execute("SELECT 1 FROM appointments WHERE phone=? AND status='agendado' AND start_at>?", (phone, now().strftime(FMT))).fetchone():
        return jsonify(error="Este cliente tem agendamento futuro. Cancele antes de apagar os dados."), 409
    n = erase_phone(c, phone)
    return jsonify(ok=True, anonymized=n) if n else (jsonify(error="Nenhum registro encontrado para este WhatsApp."), 404)

# ---------- monitoramento ----------
def gateway_status():
    """None = gateway não configurado; True/False = WhatsApp conectado ou não."""
    gw = os.getenv("WA_GATEWAY_URL")
    if not gw:
        return None
    try:
        r = requests.get(gw.rstrip("/") + "/status", headers={"X-Secret": os.getenv("WA_GATEWAY_SECRET", "")}, timeout=3)
        return bool(r.ok and r.json().get("ready"))
    except Exception:
        return False

def disk_free_pct():
    u = shutil.disk_usage(os.path.dirname(os.path.abspath(DB)))
    return round(u.free / u.total * 100, 1)

def notify_admin(title, msg):
    """Aviso no celular via ntfy.sh (grátis): defina ALERT_NTFY_URL=https://ntfy.sh/<um-nome-secreto> e assine o tópico no app ntfy."""
    url = os.getenv("ALERT_NTFY_URL")
    if not url:
        return False
    try:
        r = requests.post(url, data=msg.encode("utf-8"), headers={"Title": title, "Priority": "high", "Tags": "warning"}, timeout=10)
        return r.ok
    except requests.RequestException as e:
        log.error("Falha ao enviar alerta: %s", e)
        return False

def alerts_now():
    """Problemas atuais: lista de (chave, mensagem)."""
    out, n = [], now()
    since = get_meta("wa_down_since")
    if since and n - P(since) >= timedelta(minutes=WA_ALERT_MIN):
        out.append(("whatsapp", f"O WhatsApp automático está desconectado desde {since[8:10]}/{since[5:7]} {since[11:]}. Pareie de novo: sudo journalctl -u wa-gateway -f"))
    beat = get_meta("heartbeat")
    if SCHED_ON and n - (P(beat) if beat else STARTED) > timedelta(minutes=5):
        out.append(("agendador", "O agendador de lembretes parou. Reinicie: sudo systemctl restart barbearia"))
    if get_meta("offsite_error"):
        out.append(("backup", f"Falha ao enviar o backup para a nuvem: {get_meta('offsite_error')}"))
    if disk_free_pct() < 10:
        out.append(("disco", f"Pouco espaço livre em disco ({disk_free_pct()}%)."))
    return out

def monitor():
    """A cada 5 min: confere o WhatsApp e avisa (ntfy) quando um problema aparece e quando é resolvido."""
    g = gateway_status()
    if g is False:
        if not get_meta("wa_down_since"):
            set_meta("wa_down_since", now().strftime(FMT))
    else:
        set_meta("wa_down_since", "")
    before, cur = set(filter(None, get_meta("alerts_active").split(","))), dict(alerts_now())
    for k, msg in cur.items():
        if k not in before:
            notify_admin("Barbearia: alerta", msg)
    for k in before - set(cur):
        notify_admin("Barbearia: resolvido", f"Problema resolvido ({k}).")
    set_meta("alerts_active", ",".join(cur))

def tick():
    """Roda todo minuto: marca o 'batimento' (prova de que o agendador está vivo) e envia as mensagens pendentes."""
    set_meta("heartbeat", now().strftime(FMT))
    run_reminders()

@app.get("/healthz")
def healthz():
    """Para o UptimeRobot (ou similar): 200 = banco e agendador OK; 503 = problema."""
    try:
        db().execute("SELECT 1").fetchone()
        beat = get_meta("heartbeat")
        stale = SCHED_ON and now() - (P(beat) if beat else STARTED) > timedelta(minutes=5)
    except Exception:
        log.exception("healthz: banco indisponível")
        return jsonify(ok=False, db=False), 503
    return jsonify(ok=not stale, db=True, scheduler=not stale), (503 if stale else 200)

@app.get("/api/admin/health")
@admin_required
def adm_health():
    g = gateway_status()
    fails = db().execute("SELECT COUNT(DISTINCT ip) FROM book_attempts WHERE ip LIKE 'a:%' AND at>?", ((now() - timedelta(days=1)).strftime(FMT),)).fetchone()[0]
    return jsonify(alerts=[{"key": k, "msg": m} for k, m in alerts_now()], whatsapp=None if g is None else ("conectado" if g else "desconectado"),
                   heartbeat=get_meta("heartbeat"), scheduler_on=SCHED_ON, disk_free_pct=disk_free_pct(),
                   db_mb=round(os.path.getsize(DB) / 1048576, 2), failed_logins_24h=fails,
                   pending_messages=len(pending_reminders(db())), alert_channel=bool(os.getenv("ALERT_NTFY_URL")), retention_months=RETENTION_MONTHS)

# ---------- site: dados da barbearia e galeria ----------
@app.get("/api/admin/site")
@admin_required
def adm_site():
    saved = {r["key"]: r["value"] for r in db().execute("SELECT key,value FROM settings")}
    s = shop()
    return jsonify(values={k: saved.get(k, "") for k in SITE_FIELDS}, defaults={k: SHOP_DEFAULTS[k] for k in SITE_FIELDS},
                   wa_link=s["wa_link"], instagram_url=s["instagram_url"])

@app.post("/api/admin/site")
@admin_required
def adm_site_save():
    j, c, vals = body(), db(), {}
    for k, mx in SITE_FIELDS.items():
        if k in j:
            vals[k] = str(j[k] or "").strip()[:mx]
    if vals.get("whatsapp"):
        vals["whatsapp"] = norm_phone(vals["whatsapp"])
        if not vals["whatsapp"]:
            return jsonify(error="WhatsApp inválido. Use DDD + número, ex.: (12) 99999-9999."), 400
    if vals.get("instagram"):
        vals["instagram"] = clean_instagram(vals["instagram"])
        if vals["instagram"] is None:
            return jsonify(error="Instagram inválido. Use @usuario ou o link do perfil."), 400
    for k, v in vals.items():  # campo vazio = volta ao padrão
        if v:
            c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, v))
        else:
            c.execute("DELETE FROM settings WHERE key=?", (k,))
    _shop_cache["v"] = None
    return jsonify(ok=True)

@app.get("/api/admin/gallery")
@admin_required
def adm_gallery():
    return jsonify([dict(r) for r in db().execute("SELECT id,filename,caption,position FROM gallery ORDER BY position,id")])

@app.post("/api/admin/gallery")
@admin_required
def adm_gallery_add():
    c, f = db(), request.files.get("file")
    if not f:
        return jsonify(error="Escolha uma imagem."), 400
    data = f.read(MAX_PHOTO_BYTES + 1)
    if len(data) > MAX_PHOTO_BYTES:
        return jsonify(error=f"A imagem passa de {MAX_PHOTO_BYTES // 1048576} MB. Reduza e envie de novo."), 413
    ext = img_ext(data[:12])
    if not ext:
        return jsonify(error="Formato inválido. Use JPG, PNG ou WEBP."), 400
    if c.execute("SELECT COUNT(*) FROM gallery").fetchone()[0] >= MAX_PHOTOS:
        return jsonify(error=f"Limite de {MAX_PHOTOS} fotos. Apague alguma antes."), 409
    os.makedirs(GALLERY_DIR, exist_ok=True)
    name = f"{secrets.token_hex(8)}.{ext}"
    with open(os.path.join(GALLERY_DIR, name), "wb") as fh:
        fh.write(data)
    pos = c.execute("SELECT COALESCE(MAX(position),0) FROM gallery").fetchone()[0] + 1
    cur = c.execute("INSERT INTO gallery(filename,caption,position,created_at) VALUES(?,?,?,?)",
                    (name, _txt(120, False)(request.form.get("caption")), pos, now().strftime(FMT)))
    return jsonify(ok=True, id=cur.lastrowid, filename=name), 201

@app.post("/api/admin/gallery/caption")
@admin_required
def adm_gallery_caption():
    j = body()
    try:
        cur = db().execute("UPDATE gallery SET caption=? WHERE id=?", (_txt(120, False)(j.get("caption")), int(j["id"])))
    except (KeyError, ValueError, TypeError):
        return jsonify(error="Dados inválidos"), 400
    return jsonify(ok=True) if cur.rowcount else (jsonify(error="Não encontrada"), 404)

@app.post("/api/admin/gallery/delete")
@admin_required
def adm_gallery_del():
    c = db()
    try:
        r = c.execute("SELECT filename FROM gallery WHERE id=?", (int(body()["id"]),)).fetchone()
    except (KeyError, ValueError, TypeError):
        return jsonify(error="Dados inválidos"), 400
    if not r:
        return jsonify(error="Não encontrada"), 404
    c.execute("DELETE FROM gallery WHERE filename=?", (r["filename"],))
    try:
        os.remove(os.path.join(GALLERY_DIR, r["filename"]))
    except OSError:
        pass
    return jsonify(ok=True)

@app.post("/api/admin/gallery/move")
@admin_required
def adm_gallery_move():
    c, j = db(), body()
    ids = [r["id"] for r in c.execute("SELECT id FROM gallery ORDER BY position,id")]
    try:
        i, d = ids.index(int(j["id"])), {"up": -1, "down": 1}[j["dir"]]
    except (KeyError, ValueError, TypeError):
        return jsonify(error="Dados inválidos"), 400
    if 0 <= i + d < len(ids):
        ids[i], ids[i + d] = ids[i + d], ids[i]
    c.executemany("UPDATE gallery SET position=? WHERE id=?", [(n + 1, x) for n, x in enumerate(ids)])
    return jsonify(ok=True)

# ---------- 12) histórico por cliente ----------
def client_list(c, q=""):
    sql = """SELECT * FROM (SELECT a.phone,
        (SELECT x.client_name FROM appointments x WHERE x.phone=a.phone ORDER BY x.start_at DESC LIMIT 1) name,
        COUNT(*) total,SUM(a.status='concluido') visits,SUM(a.status='faltou') noshows,SUM(a.status='cancelado') cancels,
        MAX(CASE WHEN a.status='concluido' THEN a.start_at END) last_visit,
        MIN(CASE WHEN a.status='agendado' AND a.start_at>? THEN a.start_at END) next_visit,
        SUM(CASE WHEN a.status='concluido' THEN COALESCE((SELECT SUM(t.amount) FROM transactions t WHERE t.appointment_id=a.id AND t.kind='entrada'),s.price) ELSE 0 END) spent
        FROM appointments a JOIN services s ON s.id=a.service_id WHERE a.phone<>'' GROUP BY a.phone)"""
    p, q = [now().strftime(FMT)], (q or "").strip()
    if q:
        digits = re.sub(r"\D", "", q)
        sql += " WHERE name LIKE ?" + (" OR phone LIKE ?" if digits else "")
        p += [f"%{q}%"] + ([f"%{digits}%"] if digits else [])
    sql += " ORDER BY (last_visit IS NULL),last_visit DESC,next_visit,name LIMIT 300"
    out = {r["phone"] for r in c.execute("SELECT phone FROM optout")}
    return [{**dict(r), "spent": round(r["spent"] or 0, 2), "optout": r["phone"] in out} for r in c.execute(sql, p)]

@app.get("/api/admin/clients")
@admin_required
def adm_clients():
    return jsonify(client_list(db(), request.args.get("q")))

@app.get("/api/admin/clients/<phone>")
@admin_required
def adm_client(phone):
    c, p = db(), norm_phone(phone)
    if not p:
        return jsonify(error="WhatsApp inválido."), 400
    rows = c.execute("""SELECT a.id,a.start_at,a.status,a.reschedules,a.client_name,a.consent,s.name service,b.name barber,s.price,
        COALESCE((SELECT SUM(t.amount) FROM transactions t WHERE t.appointment_id=a.id AND t.kind='entrada'),CASE WHEN a.status='concluido' THEN s.price END) paid
        FROM appointments a JOIN services s ON s.id=a.service_id JOIN barbers b ON b.id=a.barber_id WHERE a.phone=? ORDER BY a.start_at DESC""", (p,)).fetchall()
    if not rows:
        return jsonify(error="Cliente não encontrado."), 404
    done = [r for r in rows if r["status"] == "concluido"]
    dts = sorted(P(r["start_at"]) for r in done)
    fav = lambda key: (lambda l: max(set(l), key=l.count) if l else None)([r[key] for r in done])
    avg = round(avg_interval_days(dts), 1) if len(dts) >= 2 else None
    nxt = c.execute("SELECT MIN(start_at) FROM appointments WHERE phone=? AND status='agendado' AND start_at>?", (p, now().strftime(FMT))).fetchone()[0]
    note = c.execute("SELECT note FROM client_notes WHERE phone=?", (p,)).fetchone()
    stats = {"visits": len(done), "noshows": sum(r["status"] == "faltou" for r in rows), "cancels": sum(r["status"] == "cancelado" for r in rows),
             "total": len(rows), "spent": round(sum(r["paid"] or 0 for r in done), 2), "first_visit": done[-1]["start_at"] if done else None,
             "last_visit": done[0]["start_at"] if done else None, "next_visit": nxt, "avg_interval_days": avg,
             "expected_return": (dts[-1] + timedelta(days=avg)).strftime("%Y-%m-%d") if avg else None, "fav_service": fav("service"), "fav_barber": fav("barber")}
    return jsonify(phone=p, name=rows[0]["client_name"], stats=stats, note=note["note"] if note else "", consent=rows[0]["consent"],
                   optout=bool(c.execute("SELECT 1 FROM optout WHERE phone=?", (p,)).fetchone()), appointments=[dict(r) for r in rows])

@app.post("/api/admin/clients/note")
@admin_required
def adm_client_note():
    j, c = body(), db()
    p = norm_phone(str(j.get("phone") or ""))
    if not p or not c.execute("SELECT 1 FROM appointments WHERE phone=?", (p,)).fetchone():
        return jsonify(error="Cliente não encontrado."), 404
    note = str(j.get("note") or "").strip()[:500]
    if note:
        c.execute("INSERT INTO client_notes(phone,note,updated_at) VALUES(?,?,?) ON CONFLICT(phone) DO UPDATE SET note=excluded.note,updated_at=excluded.updated_at",
                  (p, note, now().strftime(FMT)))
    else:
        c.execute("DELETE FROM client_notes WHERE phone=?", (p,))
    return jsonify(ok=True)

# ---------- 13) relatórios em CSV (Excel pt-BR: separador ';', vírgula decimal, UTF-8 com BOM) ----------
def fdate(d):
    return "/".join(reversed(str(d)[:10].split("-"))) if d else ""

def _cell(v):
    if v is None:
        return ""
    if isinstance(v, (int, float)):
        return str(v) if isinstance(v, int) else f"{v:.2f}".replace(".", ",")
    v = str(v)
    return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v  # evita "injeção de fórmula" no Excel (nomes vêm do formulário público)

def csv_response(name, header, rows):
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    w.writerow(header)
    for r in rows:
        w.writerow([_cell(x) for x in r])
    return Response("\ufeff" + buf.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})

def period_args():
    t = now()
    d1 = _ymd(request.args.get("from") or t.replace(day=1).strftime("%Y-%m-%d"))
    d2 = _ymd(request.args.get("to") or ((t.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)).strftime("%Y-%m-%d"))
    if d2 < d1 or (P(d2 + " 00:00") - P(d1 + " 00:00")).days > 3660:
        raise ValueError
    return d1, d2

def commission_rows(c, d1, d2, barber_id=None):
    """Por barbeiro no período: atendimentos, bruto (líquido de desconto), comissão sobre o bruto e gorjetas (repasse integral)."""
    sql = """SELECT b.id,b.name,b.commission_pct pct,COUNT(CASE WHEN t.category<>'Gorjeta' THEN 1 END) n,
        COALESCE(SUM(CASE WHEN t.category<>'Gorjeta' THEN t.amount END),0) gross,COALESCE(SUM(CASE WHEN t.category='Gorjeta' THEN t.amount END),0) tips
        FROM barbers b LEFT JOIN transactions t ON t.barber_id=b.id AND t.kind='entrada' AND t.day BETWEEN ? AND ?"""
    p = [d1, d2]
    if barber_id is not None:
        sql += " WHERE b.id=?"
        p.append(barber_id)
    return [{**dict(r), "gross": round(r["gross"], 2), "tips": round(r["tips"], 2), "commission": round(r["gross"] * r["pct"] / 100, 2)}
            for r in c.execute(sql + " GROUP BY b.id ORDER BY b.name", p)]

STATUS_PT = {"agendado": "Agendado", "concluido": "Concluído", "cancelado": "Cancelado", "faltou": "Faltou"}

@app.get("/api/admin/export/<kind>")
@admin_required
def export_csv(kind):
    c = db()
    if kind not in ("lancamentos", "comissoes", "agendamentos", "clientes", "fechamentos", "diario"):
        return jsonify(error="Relatório desconhecido."), 404
    try:
        d1, d2 = period_args()
    except ValueError:
        return jsonify(error="Período inválido."), 400
    name = f"{kind}-{d1}_a_{d2}.csv"
    if kind == "lancamentos":
        rows = c.execute("""SELECT t.day,t.kind,t.category,t.description,t.method,t.discount,t.amount,b.name barber FROM transactions t
            LEFT JOIN barbers b ON b.id=t.barber_id WHERE t.day BETWEEN ? AND ? ORDER BY t.day,t.id""", (d1, d2)).fetchall()
        return csv_response(name, ["Data", "Tipo", "Categoria", "Descrição", "Forma de pagamento", "Desconto", "Valor", "Barbeiro"],
                            [[fdate(r["day"]), "Entrada" if r["kind"] == "entrada" else "Saída", r["category"], r["description"], PAY_METHODS.get(r["method"], ""),
                              float(r["discount"]), float(r["amount"]), r["barber"]] for r in rows])
    if kind == "comissoes":
        return csv_response(name, ["Barbeiro", "Atendimentos", "Bruto (líquido de desconto)", "% comissão", "Comissão a repassar", "Gorjetas (repasse integral)", "Total a pagar ao barbeiro"],
                            [[r["name"], r["n"], r["gross"], float(r["pct"]), r["commission"], r["tips"], round(r["commission"] + r["tips"], 2)] for r in commission_rows(c, d1, d2)])
    if kind == "agendamentos":
        rows = c.execute("""SELECT a.start_at,a.client_name,a.phone,b.name barber,s.name service,a.status,s.price,a.reschedules FROM appointments a
            JOIN barbers b ON b.id=a.barber_id JOIN services s ON s.id=a.service_id WHERE substr(a.start_at,1,10) BETWEEN ? AND ? ORDER BY a.start_at""", (d1, d2)).fetchall()
        return csv_response(name, ["Data", "Hora", "Cliente", "WhatsApp", "Barbeiro", "Serviço", "Situação", "Preço de tabela", "Remarcações"],
                            [[fdate(r["start_at"]), r["start_at"][11:], r["client_name"], r["phone"], r["barber"], r["service"], STATUS_PT.get(r["status"], r["status"]), float(r["price"]), r["reschedules"]] for r in rows])
    if kind == "clientes":
        return csv_response(f"clientes-{now():%Y-%m-%d}.csv", ["Cliente", "WhatsApp", "Agendamentos", "Atendimentos", "Faltas", "Cancelamentos", "Última visita", "Próximo agendamento", "Total gasto", "Recebe mensagens"],
                            [[r["name"], r["phone"], r["total"], r["visits"], r["noshows"], r["cancels"], fdate(r["last_visit"]), fdate(r["next_visit"]), r["spent"], "Não" if r["optout"] else "Sim"] for r in client_list(c)])
    if kind == "fechamentos":
        rows = c.execute("SELECT * FROM cash_closings WHERE day BETWEEN ? AND ? ORDER BY day", (d1, d2)).fetchall()
        return csv_response(name, ["Dia", "Abertura", "Esperado na gaveta", "Contado", "Diferença", "Observações", "Fechado em"],
                            [[fdate(r["day"]), r["opening"], r["expected"], r["counted"], round(r["counted"] - r["expected"], 2), r["notes"], fdate(r["closed_at"]) + " " + r["closed_at"][11:]] for r in rows])
    rows = c.execute("""SELECT day,
        SUM(CASE WHEN kind='entrada' AND method='dinheiro' THEN amount ELSE 0 END) d, SUM(CASE WHEN kind='entrada' AND method='pix' THEN amount ELSE 0 END) p,
        SUM(CASE WHEN kind='entrada' AND method='debito' THEN amount ELSE 0 END) db, SUM(CASE WHEN kind='entrada' AND method='credito' THEN amount ELSE 0 END) cr,
        SUM(CASE WHEN kind='entrada' AND method='outro' THEN amount ELSE 0 END) o, SUM(CASE WHEN kind='entrada' AND method IS NULL THEN amount ELSE 0 END) sem,
        SUM(CASE WHEN kind='entrada' THEN amount ELSE 0 END) ent, SUM(CASE WHEN kind='saida' THEN amount ELSE 0 END) sai
        FROM transactions WHERE day BETWEEN ? AND ? GROUP BY day ORDER BY day""", (d1, d2)).fetchall()
    return csv_response(name, ["Data", "Dinheiro", "Pix", "Cartão de débito", "Cartão de crédito", "Outro", "Sem forma informada", "Total de entradas", "Saídas", "Saldo"],
                        [[fdate(r["day"])] + [float(r[k]) for k in ("d", "p", "db", "cr", "o", "sem", "ent", "sai")] + [round(r["ent"] - r["sai"], 2)] for r in rows])

# ---------- 14) acesso separado por barbeiro ----------
LOGIN_RE = re.compile(r"[a-z0-9._-]{3,30}")

@app.post("/api/admin/barbers/access")
@admin_required
def adm_barber_access():
    """O dono cria/troca o login e a senha de um barbeiro (ou remove o acesso)."""
    j, c = body(), db()
    try:
        bid = int(j["id"])
    except (KeyError, ValueError, TypeError):
        return jsonify(error="Dados inválidos"), 400
    b = c.execute("SELECT id,pass_hash FROM barbers WHERE id=?", (bid,)).fetchone()
    if not b:
        return jsonify(error="Barbeiro não encontrado."), 404
    if j.get("remove"):
        c.execute("UPDATE barbers SET login=NULL,pass_hash=NULL WHERE id=?", (bid,))
        return jsonify(ok=True)
    login, pw = str(j.get("login") or "").strip().lower(), str(j.get("password") or "")
    if not LOGIN_RE.fullmatch(login) or login == os.getenv("ADMIN_USER", "admin").lower():
        return jsonify(error="Usuário: 3 a 30 letras minúsculas, números, ponto, hífen ou _ (e diferente do usuário do dono)."), 400
    if (pw or not b["pass_hash"]) and len(pw) < 8:
        return jsonify(error="Senha: mínimo de 8 caracteres."), 400
    if c.execute("SELECT 1 FROM barbers WHERE lower(login)=? AND id<>?", (login, bid)).fetchone():
        return jsonify(error="Esse usuário já está em uso."), 409
    if pw:
        c.execute("UPDATE barbers SET login=?,pass_hash=? WHERE id=?", (login, generate_password_hash(pw), bid))
    else:
        c.execute("UPDATE barbers SET login=? WHERE id=?", (login, bid))
    return jsonify(ok=True)

@app.get("/barbeiro")
@barber_required
def barber_page():
    r = app.make_response(render_template("barber.html", shop=shop(), barber=g.barber["name"]))
    r.headers.update({"Cache-Control": "no-store", "X-Robots-Tag": "noindex"})
    return r

@app.get("/api/barber/appointments")
@barber_required
def barber_appts():
    try:
        day = _ymd(request.args.get("day") or now().strftime("%Y-%m-%d"))
    except ValueError:
        return jsonify(error="Data inválida"), 400
    rows = db().execute("""SELECT a.id,a.client_name,a.phone,a.start_at,a.end_at,a.status,s.name service,s.price FROM appointments a
        JOIN services s ON s.id=a.service_id WHERE a.barber_id=? AND a.start_at LIKE ? ORDER BY a.start_at""", (g.barber["id"], day + "%"))
    return jsonify([dict(r) for r in rows])

@app.post("/api/barber/appointments/<int:aid>/status")
@barber_required
def barber_status(aid):
    if not db().execute("SELECT 1 FROM appointments WHERE id=? AND barber_id=?", (aid, g.barber["id"])).fetchone():
        return jsonify(error="Agendamento não encontrado."), 404
    return status_response(aid)

@app.get("/api/barber/earnings")
@barber_required
def barber_earnings():
    m = request.args.get("month") or now().strftime("%Y-%m")
    if not re.fullmatch(r"\d{4}-\d{2}", m):
        return jsonify(error="Mês inválido"), 400
    c, bid = db(), g.barber["id"]
    r = commission_rows(c, m + "-01", m + "-31", bid)[0]
    items = c.execute("SELECT day,description,category,amount,method FROM transactions WHERE barber_id=? AND kind='entrada' AND day BETWEEN ? AND ? ORDER BY day DESC,id DESC",
                      (bid, m + "-01", m + "-31"))
    return jsonify(name=r["name"], pct=r["pct"], n=r["n"], gross=r["gross"], commission=r["commission"], tips=r["tips"],
                   total=round(r["commission"] + r["tips"], 2), items=[dict(i) for i in items])

@app.get("/api/barber/timeoff")
@barber_required
def barber_timeoff_list():
    rows = db().execute("""SELECT id,day_from,day_to,t_from,t_to,reason,(barber_id IS NOT NULL) own FROM time_off
        WHERE (barber_id IS NULL OR barber_id=?) AND day_to>=? ORDER BY day_from,id""", (g.barber["id"], now().strftime("%Y-%m-%d")))
    return jsonify([dict(r) for r in rows])

@app.post("/api/barber/timeoff")
@barber_required
def barber_timeoff_add():
    return create_timeoff(db(), body(), g.barber["id"])

@app.post("/api/barber/timeoff/delete")
@barber_required
def barber_timeoff_del():
    try:
        cur = db().execute("DELETE FROM time_off WHERE id=? AND barber_id=?", (int(body()["id"]), g.barber["id"]))  # só as próprias folgas
    except (KeyError, ValueError, TypeError):
        return jsonify(error="Dados inválidos"), 400
    return jsonify(ok=True) if cur.rowcount else (jsonify(error="Folga não encontrada."), 404)

@app.post("/api/barber/password")
@barber_required
def barber_password():
    j, c = body(), db()
    row = c.execute("SELECT pass_hash FROM barbers WHERE id=?", (g.barber["id"],)).fetchone()
    if not check_password_hash(row["pass_hash"], str(j.get("current") or "")):
        return jsonify(error="Senha atual incorreta."), 400
    new = str(j.get("new") or "")
    if len(new) < 8:
        return jsonify(error="A nova senha precisa ter no mínimo 8 caracteres."), 400
    c.execute("UPDATE barbers SET pass_hash=? WHERE id=?", (generate_password_hash(new), g.barber["id"]))
    return jsonify(ok=True)

# ---------- previsão de compra (média móvel ponderada) ----------
def forecast_purchases(c, horizon_days=30, weeks=8, safety=1.15, decay=0.85):
    """Consumo semanal dos últimos `weeks`, com peso maior nas semanas recentes -> taxa diária -> necessidade no horizonte."""
    today, out = now().date(), []
    for p in c.execute("SELECT * FROM products WHERE active=1"):
        buckets = [0.0] * weeks
        for m in c.execute("SELECT day,-qty q FROM stock_moves WHERE product_id=? AND qty<0 AND day>=?",
                           (p["id"], (today - timedelta(weeks=weeks)).isoformat())):
            age = (today - datetime.strptime(m["day"], "%Y-%m-%d").date()).days // 7
            if 0 <= age < weeks:
                buckets[age] += m["q"]
        w = [decay ** i for i in range(weeks)]
        daily = sum(b * wi for b, wi in zip(buckets, w)) / sum(w) / 7
        need = daily * horizon_days * safety
        out.append({"product": p["name"], "stock": p["stock"], "daily_rate": round(daily, 2),
                    "days_left": round(p["stock"] / daily) if daily else None,
                    "buy": max(0, math.ceil(need - p["stock"]))})
    return sorted(out, key=lambda r: -r["buy"])

@app.get("/api/admin/forecast")
@admin_required
def forecast():
    return jsonify(forecast_purchases(db(), int(request.args.get("days", 30))))

# ---------- lembretes automáticos ----------
def avg_interval_days(visits):
    gaps = [(b - a).days for a, b in zip(visits, visits[1:]) if (b - a).days > 0]
    return min(max(sum(gaps) / len(gaps), 7), 90) if gaps else RETURN_DAYS

REMINDER_COLS = {"confirmacao": "confirm_sent", "vespera": "day_before_sent", "lembrete": "reminder_sent", "retorno": "return_sent"}

def manage_line(token):
    base = os.getenv("PUBLIC_URL", "").rstrip("/")
    return f"\nPara cancelar ou remarcar: {base}/agendamento/{token}" if base and token else ""

def pending_reminders(c):
    """Mensagens devidas agora: confirmação (logo após agendar), véspera (18h do dia anterior), 1h antes e retorno do cliente."""
    n, out = now(), []
    base_sql = """SELECT a.id,a.token,a.phone,a.client_name,a.start_at,b.name bname,s.name sname FROM appointments a
        JOIN barbers b ON b.id=a.barber_id JOIN services s ON s.id=a.service_id
        WHERE a.status='agendado' AND a.phone<>'' AND a.consent IS NOT NULL AND a.phone NOT IN (SELECT phone FROM optout) AND a.start_at>? AND """
    for a in c.execute(base_sql + "a.confirm_sent=0 ORDER BY a.id", (n.strftime(FMT),)).fetchall():
        st = P(a["start_at"])
        out.append({"id": a["id"], "kind": "confirmacao", "phone": a["phone"], "name": a["client_name"], "when": a["start_at"],
                    "text": f"✅ Olá, {a['client_name']}! Seu horário está confirmado: {a['sname']} com {a['bname']}, {WEEKDAYS[st.weekday()]}, {st:%d/%m} às {st:%H:%M}.\n"
                            f"{shop()['name']} – {shop()['address']}{manage_line(a['token'])}"})
    for a in c.execute(base_sql + "a.day_before_sent=0 ORDER BY a.start_at", (n.strftime(FMT),)).fetchall():
        st = P(a["start_at"])
        if st.date() > n.date() and n >= vespera_due(st):
            out.append({"id": a["id"], "kind": "vespera", "phone": a["phone"], "name": a["client_name"], "when": a["start_at"],
                        "text": f"Olá, {a['client_name']}! Lembrete: amanhã, às {st:%H:%M}, você tem {a['sname']} com {a['bname']}.\n"
                                f"{shop()['name']} – {shop()['address']}{manage_line(a['token'])}"})
    for a in c.execute(base_sql + "a.reminder_sent=0 AND a.start_at<=?", (n.strftime(FMT), (n + timedelta(hours=1)).strftime(FMT))).fetchall():
        out.append({"id": a["id"], "kind": "lembrete", "phone": a["phone"], "name": a["client_name"], "when": a["start_at"],
                    "text": f"Olá, {a['client_name']}! Lembrete: seu horário ({a['sname']} com {a['bname']}) é hoje às {a['start_at'][11:]}. {shop()['name']} – {shop()['address']}"})
    by_phone = {}
    for r in c.execute("SELECT id,phone,client_name,start_at,return_sent FROM appointments WHERE status='concluido' AND phone<>'' AND consent IS NOT NULL AND phone NOT IN (SELECT phone FROM optout) ORDER BY start_at"):
        by_phone.setdefault(r["phone"], []).append(r)
    for phone, rows in by_phone.items():
        last = rows[-1]
        due = P(last["start_at"]) + timedelta(days=avg_interval_days([P(r["start_at"]) for r in rows]) + 2)
        future = c.execute("SELECT 1 FROM appointments WHERE phone=? AND status='agendado'", (phone,)).fetchone()
        if not last["return_sent"] and not future and n >= due:
            out.append({"id": last["id"], "kind": "retorno", "phone": phone, "name": last["client_name"], "when": due.strftime(FMT),
                        "text": f"Olá, {last['client_name']}! Já faz um tempo desde o seu último corte. Que tal agendar o próximo na {shop()['name']}?"})
    return out

def mark_done(c, kind, rid):
    c.execute(f"UPDATE appointments SET {REMINDER_COLS[kind]}=1 WHERE id=?", (rid,))

def run_reminders():
    """Modo automático (com WA_GATEWAY_URL ou WA_TOKEN; se o envio falhar, o item continua na fila manual). Sem token, os lembretes ficam na fila manual do /admin (links wa.me, custo zero)."""
    if not auto_send_enabled():
        return
    c = connect()
    for r in pending_reminders(c):
        if send_whatsapp(r["phone"], r["text"]):
            mark_done(c, r["kind"], r["id"])
    c.close()

@app.get("/api/admin/reminders")
@admin_required
def adm_reminders():
    return jsonify([{**r, "link": f"https://wa.me/{r['phone']}?text={quote(r['text'])}"} for r in pending_reminders(db())])

@app.post("/api/admin/reminders/done")
@admin_required
def adm_reminder_done():
    j = body()
    if j.get("kind") not in REMINDER_COLS or not isinstance(j.get("id"), int):
        return jsonify(error="Dados inválidos"), 400
    mark_done(db(), j["kind"], j["id"])
    return jsonify(ok=True)

def demo_only(f):
    @wraps(f)
    def w(*a, **k):
        return f(*a, **k) if DEMO() else (jsonify(error="Modo demo desativado"), 404)
    return w

@app.get("/api/admin/outbox")
@admin_required
@demo_only
def outbox():
    return jsonify([dict(r) for r in db().execute("SELECT * FROM outbox ORDER BY id")])

@app.post("/api/admin/demo/seed")
@admin_required
@demo_only
def demo_seed():
    """Cria: um agendamento daqui a 30 min (dispara o lembrete de 1h) e um cliente cujo último corte foi há 25 dias (dispara o retorno)."""
    c, n = db(), now()
    c.execute("DELETE FROM appointments WHERE client_name LIKE '%(demo)'")
    c.execute("DELETE FROM outbox")
    ins = "INSERT INTO appointments(client_name,phone,barber_id,service_id,start_at,end_at,status,token,confirm_sent,day_before_sent,consent) VALUES(?,?,1,1,?,?,?,?,1,1,'demo')"
    t = n + timedelta(minutes=30)
    c.execute(ins, ("Ana (demo)", "5512988880000", t.strftime(FMT), (t + timedelta(minutes=30)).strftime(FMT), "agendado", new_token()))
    o = n - timedelta(days=25)
    c.execute(ins, ("Bruno (demo)", "5512977770000", o.strftime(FMT), (o + timedelta(minutes=30)).strftime(FMT), "concluido", new_token()))
    return jsonify(ok=True)

@app.post("/api/admin/demo/run")
@admin_required
@demo_only
def demo_run():
    run_reminders()
    return jsonify(ok=True)

init_db()
if os.getenv("RUN_SCHEDULER", "1") == "1":
    sched = BackgroundScheduler(timezone=TZ)
    sched.add_job(tick, "interval", minutes=1, max_instances=1, coalesce=True)
    sched.add_job(monitor, "interval", minutes=5, max_instances=1, coalesce=True)
    sched.add_job(scheduled_backup, "cron", hour=3, minute=30, max_instances=1, coalesce=True)
    sched.add_job(scheduled_retention, "cron", hour=3, minute=40, max_instances=1, coalesce=True)
    sched.start()

if __name__ == "__main__":
    app.run(debug=False)
