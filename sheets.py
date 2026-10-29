"""Camada de acesso ao Google Sheets: duas planilhas (financeiro e estoque) + criação/seed das abas.

Onde cada aba mora:
  financeiro -> Lancamentos, Resumo            (SHEET_FINANCEIRO_ID)
  estoque    -> Produtos, Movimentacoes        (SHEET_ESTOQUE_ID)
  cadastro   -> Config, Barbeiros, Servicos,   (SHEET_CADASTRO_ID; se vazio, usa a planilha financeira)
                Horarios, Folgas
Os agendamentos NÃO ficam em planilha: ficam no Google Agenda (veja agenda.py).
"""
import json
import os
import threading
from datetime import date

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/calendar",
]

SCHEMA = {
    # cadastro
    "Config": ["chave", "valor"],
    "Barbeiros": ["id", "nome", "funcao", "calendario", "ativo"],
    "Servicos": ["id", "nome", "descricao", "minutos", "preco", "ativo"],
    "Horarios": ["barbeiro", "dia", "abre", "fecha", "almoco_ini", "almoco_fim"],
    "Folgas": ["barbeiro", "data", "inicio", "fim", "motivo"],
    # financeiro
    "Lancamentos": ["id", "data", "tipo", "categoria", "descricao", "valor", "forma_pgto",
                    "barbeiro", "origem", "criado_em"],
    "Resumo": [],
    # estoque
    "Produtos": ["id", "nome", "preco", "estoque_minimo", "ativo", "estoque"],
    "Movimentacoes": ["data", "produto_id", "tipo", "quantidade", "motivo", "criado_em"],
}
TAB_BOOK = {
    "Config": "cadastro", "Barbeiros": "cadastro", "Servicos": "cadastro",
    "Horarios": "cadastro", "Folgas": "cadastro",
    "Lancamentos": "financeiro", "Resumo": "financeiro",
    "Produtos": "estoque", "Movimentacoes": "estoque",
}
# intervalos em que tudo deve ser texto (evita o Sheets converter "09:00" ou "2026-12-25" sozinho)
TEXT_RANGES = {
    "Config": ["A:Z"], "Barbeiros": ["A:Z"], "Horarios": ["A:Z"], "Folgas": ["A:Z"],
    "Lancamentos": ["A:B"],      # id e data (AAAA-MM-DD); o valor continua numérico
    "Movimentacoes": ["A:A"],    # data (AAAA-MM-DD); a quantidade continua numérica
    "Resumo": ["B1"],            # mês (AAAA-MM)
}
FORMULA_TABS = {"Resumo", "Produtos"}   # abas com fórmulas (gravadas como USER_ENTERED)


def _estoque_formula(row):
    m = "Movimentacoes"
    def soma(tipo):
        return f'SUMIFS({m}!$D:$D,{m}!$B:$B,$A{row},{m}!$C:$C,"{tipo}")'
    return f"={soma('entrada')}-{soma('saida')}+{soma('ajuste')}"


def _resumo_rows():
    L = "Lancamentos"
    def soma(tipo, extra=""):
        return f'=SUMIFS({L}!$F:$F,{L}!$C:$C,"{tipo}",{L}!$B:$B,$B$1&"*"{extra})'
    rows = [
        ["Mês (AAAA-MM)", date.today().strftime("%Y-%m")],
        [""],
        ["Receitas", soma("receita")],
        ["Despesas", soma("despesa")],
        ["Saldo", "=B3-B4"],
        ["Atendimentos (nº)", f'=COUNTIFS({L}!$D:$D,"servico",{L}!$C:$C,"receita",{L}!$B:$B,$B$1&"*")'],
        [""],
        ["Receitas por forma de pagamento", ""],
    ]
    for i, f in enumerate(("dinheiro", "pix", "debito", "credito")):
        rows.append([f, soma("receita", f',{L}!$G:$G,$A{9 + i}')])
    return rows


def seed(tab):
    header = SCHEMA[tab]
    if tab == "Resumo":
        return _resumo_rows()
    body = {
        "Config": [
            ["nome", "D.Lourenço Barbearia"], ["nome_curto", "D.Lourenço"], ["marca", "00"],
            ["endereco", "Av. Mal. Argôlo, 912, Vila Passos, Lorena - SP, 12604-440"],
            ["whatsapp", ""], ["instagram", ""],
            ["descricao", "Barbearia em Lorena-SP. Agende online: cortes, barba e produtos."],
            ["intervalo_min", "30"],
        ],
        "Barbeiros": [["b1", "Barbeiro 1", "Especialista", "", "sim"]],
        "Servicos": [
            ["s1", "Corte", "Corte masculino na tesoura ou máquina", 30, 40, "sim"],
            ["s2", "Barba", "Barba com toalha quente", 30, 30, "sim"],
            ["s3", "Corte + Barba", "Combo completo", 60, 65, "sim"],
        ],
        "Horarios": [["*", d, "09:00", "19:00", "", ""] for d in ("seg", "ter", "qua", "qui", "sex", "sab")],
        "Folgas": [["*", "2026-12-25", "", "", "Natal"]],
        "Produtos": [["p1", "Pomada modeladora", 35, 3, "sim", _estoque_formula(2)]],
        "Movimentacoes": [[date.today().isoformat(), "p1", "entrada", 10, "estoque inicial", ""]],
    }.get(tab, [])
    return [header] + body


def credentials():
    from google.oauth2.service_account import Credentials
    raw = os.environ.get("GOOGLE_SA_JSON", "").strip()
    if raw:
        return Credentials.from_service_account_info(json.loads(raw), scopes=SCOPES)
    path = os.environ.get("GOOGLE_SA_FILE", "credenciais-google.json")
    return Credentials.from_service_account_file(path, scopes=SCOPES)


def _cell(c):
    return c if isinstance(c, (int, float)) and not isinstance(c, bool) else str(c)


class SheetsBackend:
    """Uma planilha. Interface mínima: values / append / update_row."""

    def __init__(self, sheet_id):
        self.sheet_id = sheet_id
        self._lock = threading.RLock()
        self._sh = None
        self._ws = {}

    def _book(self):
        if self._sh is None:
            import gspread
            self._sh = gspread.authorize(credentials()).open_by_key(self.sheet_id)
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
            self._sheet(tab).append_row([_cell(c) for c in row], value_input_option="RAW",
                                        table_range="A1")

    def update_row(self, tab, rownum, row):
        with self._lock:
            self._sheet(tab).update(values=[[_cell(c) for c in row]], range_name=f"A{rownum}", raw=True)

    def setup(self, tabs):
        with self._lock:
            sh = self._book()
            existing = {w.title: w for w in sh.worksheets()}
            for tab in tabs:
                header = SCHEMA[tab]
                ws = existing.get(tab) or sh.add_worksheet(title=tab, rows=500, cols=max(len(header), 6))
                for rng in TEXT_RANGES.get(tab, []):
                    ws.format(rng, {"numberFormat": {"type": "TEXT"}})
                if not ws.get_all_values():
                    ws.update(values=seed(tab), range_name="A1", raw=tab not in FORMULA_TABS)
                if header:
                    ws.freeze(rows=1)
                    ws.format("1:1", {"textFormat": {"bold": True}})
                else:
                    ws.format("A:A", {"textFormat": {"bold": True}})
            for w in sh.worksheets():  # remove a aba padrão vazia
                if w.title in ("Sheet1", "Página1", "Pagina1") and w.title not in SCHEMA:
                    if len(sh.worksheets()) > 1 and not w.get_all_values():
                        sh.del_worksheet(w)


class Sheets:
    """Roteia cada aba para a planilha certa (financeiro, estoque ou cadastro)."""

    def __init__(self):
        fin = os.environ.get("SHEET_FINANCEIRO_ID") or os.environ.get("SHEET_ID", "")  # SHEET_ID: legado
        self.ids = {
            "financeiro": fin,
            "estoque": os.environ.get("SHEET_ESTOQUE_ID", ""),
            "cadastro": os.environ.get("SHEET_CADASTRO_ID") or fin,
        }
        self._books = {}

    def book_id(self, book):
        sid = self.ids[book]
        if not sid:
            raise RuntimeError(f"Defina SHEET_{book.upper()}_ID no .env")
        return sid

    def _b(self, tab):
        sid = self.book_id(TAB_BOOK[tab])
        if sid not in self._books:
            self._books[sid] = SheetsBackend(sid)
        return self._books[sid]

    def values(self, tab):
        return self._b(tab).values(tab)

    def append(self, tab, row):
        self._b(tab).append(tab, row)

    def update_row(self, tab, rownum, row):
        self._b(tab).update_row(tab, rownum, row)

    def url(self, book):
        return f"https://docs.google.com/spreadsheets/d/{self.book_id(book)}/edit"

    def setup(self):
        por_planilha = {}
        for tab, book in TAB_BOOK.items():
            por_planilha.setdefault(self.book_id(book), []).append(tab)
        for sid, tabs in por_planilha.items():
            self._b(tabs[0]).setup(tabs)
            print(f"Planilha {sid[:8]}…: abas {', '.join(tabs)}")


if __name__ == "__main__":
    Sheets().setup()
    print("Planilhas prontas: abas criadas e dados de exemplo inseridos (nada existente foi apagado).")
