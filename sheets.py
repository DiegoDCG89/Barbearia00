"""Camada de acesso ao Google Sheets (e criação/seed das abas)."""
import json
import os
import threading

import gspread
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

SCHEMA = {
    "Config": ["chave", "valor"],
    "Barbeiros": ["id", "nome", "funcao", "ativo"],
    "Servicos": ["id", "nome", "descricao", "minutos", "preco", "ativo"],
    "Produtos": ["id", "nome", "preco", "estoque", "ativo"],
    "Horarios": ["barbeiro", "dia", "abre", "fecha", "almoco_ini", "almoco_fim"],
    "Folgas": ["barbeiro", "data", "inicio", "fim", "motivo"],
    "Agendamentos": ["codigo", "data", "hora", "barbeiro_id", "barbeiro", "servico_id", "servico",
                     "minutos", "preco", "cliente", "telefone", "status", "remarcacoes",
                     "consentimento", "optout", "criado_em"],
}
# abas em que tudo deve ser texto (evita o Sheets converter "09:00" ou "2026-12-25" sozinho)
TEXT_TABS = {"Config", "Barbeiros", "Horarios", "Folgas", "Agendamentos"}

SEED = {
    "Config": [
        ["nome", "D.Lourenço Barbearia"], ["nome_curto", "D.Lourenço"], ["marca", "00"],
        ["endereco", "Av. Mal. Argôlo, 912, Vila Passos, Lorena - SP, 12604-440"],
        ["whatsapp", ""], ["instagram", ""],
        ["descricao", "Barbearia em Lorena-SP. Agende online: cortes, barba e produtos."],
        ["intervalo_min", "30"],
    ],
    "Barbeiros": [["b1", "Barbeiro 1", "Especialista", "sim"]],
    "Servicos": [
        ["s1", "Corte", "Corte masculino na tesoura ou máquina", 30, 40, "sim"],
        ["s2", "Barba", "Barba com toalha quente", 30, 30, "sim"],
        ["s3", "Corte + Barba", "Combo completo", 60, 65, "sim"],
    ],
    "Produtos": [["p1", "Pomada modeladora", 35, 10, "sim"]],
    "Horarios": [["*", d, "09:00", "19:00", "", ""] for d in ("seg", "ter", "qua", "qui", "sex", "sab")],
    "Folgas": [["*", "2026-12-25", "", "", "Natal"]],
}


def _credentials():
    raw = os.environ.get("GOOGLE_SA_JSON", "").strip()
    if raw:
        return Credentials.from_service_account_info(json.loads(raw), scopes=SCOPES)
    path = os.environ.get("GOOGLE_SA_FILE", "credenciais-google.json")
    return Credentials.from_service_account_file(path, scopes=SCOPES)


class SheetsBackend:
    """Interface mínima: values / append / update_row. (Fácil de trocar nos testes.)"""

    def __init__(self, sheet_id=None):
        self.sheet_id = sheet_id or os.environ["SHEET_ID"]
        self._lock = threading.RLock()
        self._sh = None
        self._ws = {}

    def _book(self):
        if self._sh is None:
            self._sh = gspread.authorize(_credentials()).open_by_key(self.sheet_id)
        return self._sh

    def _sheet(self, tab):
        if tab not in self._ws:
            self._ws[tab] = self._book().worksheet(tab)
        return self._ws[tab]

    def values(self, tab):
        with self._lock:
            return self._sheet(tab).get_all_values()

    def append(self, tab, row):
        with self._lock:
            self._sheet(tab).append_row([str(c) for c in row], value_input_option="RAW",
                                        table_range="A1")

    def update_row(self, tab, rownum, row):
        with self._lock:
            self._sheet(tab).update(values=[[str(c) for c in row]], range_name=f"A{rownum}", raw=True)

    # ---------- preparação da planilha ----------
    def setup(self):
        with self._lock:
            sh = self._book()
            existing = {w.title: w for w in sh.worksheets()}
            for tab, header in SCHEMA.items():
                ws = existing.get(tab) or sh.add_worksheet(title=tab, rows=500, cols=max(len(header), 6))
                if tab in TEXT_TABS:
                    ws.format("A:Z", {"numberFormat": {"type": "TEXT"}})
                if not ws.get_all_values():
                    ws.update(values=[header] + SEED.get(tab, []), range_name="A1", raw=True)
                ws.freeze(rows=1)
                ws.format("1:1", {"textFormat": {"bold": True}})
            for w in sh.worksheets():  # remove a aba padrão vazia
                if w.title in ("Sheet1", "Página1", "Pagina1") and w.title not in SCHEMA:
                    if len(sh.worksheets()) > 1 and not w.get_all_values():
                        sh.del_worksheet(w)


if __name__ == "__main__":
    SheetsBackend().setup()
    print("Planilha pronta: abas criadas e dados de exemplo inseridos.")
