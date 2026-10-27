"""Camada de acesso ao Google Agenda (API REST v3).

Cada agendamento é um evento. Os dados do sistema (código, serviço, telefone, status...) ficam em
`extendedProperties.private` do evento; o resto (título, descrição) é só para quem olha a agenda.
Eventos criados à mão na agenda (sem `codigo`) não são agendamentos: apenas bloqueiam o horário.
"""
import os
import sys
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

API = "https://www.googleapis.com/calendar/v3"
TZ = ZoneInfo("America/Sao_Paulo")

# campos do agendamento guardados no evento (data/hora vêm do início do evento)
PRIVATE_KEYS = ("codigo", "barbeiro_id", "barbeiro", "servico_id", "servico", "minutos", "preco",
                "cliente", "telefone", "status", "remarcacoes", "consentimento", "optout", "criado_em")


def _dt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(TZ)


def parse_event(ev, cal_id):
    """Evento da API -> dict simples (início/fim como datetime com fuso)."""
    st, en = ev.get("start", {}), ev.get("end", {})
    if "dateTime" in st:
        start, end = _dt(st["dateTime"]), _dt(en["dateTime"])
    else:  # dia inteiro: 00:00 até 00:00 do dia seguinte (fim exclusivo)
        start = datetime.fromisoformat(st["date"]).replace(tzinfo=TZ)
        end = datetime.fromisoformat(en["date"]).replace(tzinfo=TZ)
    return {"id": ev["id"], "cal": cal_id, "start": start, "end": end,
            "free": ev.get("transparency") == "transparent",
            "private": dict(ev.get("extendedProperties", {}).get("private", {}))}


def appt_from_event(ev):
    """Evento parseado -> agendamento (dict com as mesmas chaves de sempre) ou None se for evento manual."""
    p = ev["private"]
    if not p.get("codigo"):
        return None
    a = {k: p.get(k, "") for k in PRIVATE_KEYS}
    a["status"] = a["status"] or "agendado"
    a["data"] = ev["start"].date().isoformat()
    a["hora"] = ev["start"].strftime("%H:%M")
    a["minutos"] = a["minutos"] or str(int((ev["end"] - ev["start"]).total_seconds() // 60))
    a["_cal"], a["_event"] = ev["cal"], ev["id"]
    return a


def event_body(a, summary, description, location, start, end):
    return {
        "summary": summary, "description": description, "location": location,
        "start": {"dateTime": start.isoformat(), "timeZone": "America/Sao_Paulo"},
        "end": {"dateTime": end.isoformat(), "timeZone": "America/Sao_Paulo"},
        "extendedProperties": {"private": {k: str(a.get(k, "")) for k in PRIVATE_KEYS}},
    }


class CalendarBackend:
    """Interface mínima: list_events / insert / patch / delete. (Fácil de trocar nos testes.)"""

    def __init__(self):
        self._lock = threading.Lock()
        self._s = None

    def _session(self):
        with self._lock:
            if self._s is None:
                from google.auth.transport.requests import AuthorizedSession
                from sheets import credentials
                self._s = AuthorizedSession(credentials())
            return self._s

    def _req(self, method, path, **kw):
        r = self._session().request(method, API + path, timeout=15, **kw)
        r.raise_for_status()
        return r.json() if r.content else {}

    def list_events(self, cal_id, start=None, end=None, private=None):
        """Eventos (já expandidos) entre start e end; `private` = {chave: valor} filtra por propriedade."""
        params = [("singleEvents", "true"), ("maxResults", "250"), ("timeZone", "America/Sao_Paulo")]
        if start:
            params += [("timeMin", start.isoformat()), ("orderBy", "startTime")]
        if end:
            params.append(("timeMax", end.isoformat()))
        for k, v in (private or {}).items():
            params.append(("privateExtendedProperty", f"{k}={v}"))
        out, token = [], None
        while True:
            data = self._req("GET", f"/calendars/{_q(cal_id)}/events",
                             params=params + ([("pageToken", token)] if token else []))
            out += [parse_event(e, cal_id) for e in data.get("items", []) if e.get("status") != "cancelled"]
            token = data.get("nextPageToken")
            if not token:
                return out

    def insert(self, cal_id, body):
        return self._req("POST", f"/calendars/{_q(cal_id)}/events", json=body)

    def patch(self, cal_id, event_id, body):
        return self._req("PATCH", f"/calendars/{_q(cal_id)}/events/{_q(event_id)}", json=body)

    def delete(self, cal_id, event_id):
        self._req("DELETE", f"/calendars/{_q(cal_id)}/events/{_q(event_id)}")

    def info(self, cal_id):
        return self._req("GET", f"/calendars/{_q(cal_id)}")


def _q(s):
    from urllib.parse import quote
    return quote(s, safe="")


if __name__ == "__main__":
    # Testa o acesso à(s) agenda(s): python agenda.py [id_extra ...]
    ids = [i for i in [os.environ.get("CALENDAR_ID", "")] + sys.argv[1:] if i]
    if not ids:
        raise SystemExit("Defina CALENDAR_ID (ou passe os IDs como argumento).")
    cb = CalendarBackend()
    for cid in ids:
        try:
            print("OK  ", cid, "->", cb.info(cid).get("summary"))
        except Exception as e:
            print("ERRO", cid, "->", e, "(compartilhou a agenda com a conta de serviço, com permissão de edição?)")
