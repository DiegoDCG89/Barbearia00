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
