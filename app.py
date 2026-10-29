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

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        senha = request.form.get('senha')
        # Pode alterar a palavra-passe abaixo
