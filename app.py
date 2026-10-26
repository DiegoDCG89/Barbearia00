import os
from flask import Flask, render_template, request, jsonify
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime

app = Flask(__name__)

# Configuração da ligação ao Google Sheets
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]
CREDENCIAIS_JSON = "credenciais-google.json"
ID_PLANILHA = "1aXqWHbLcRHx-8oyglj99hl5JIUvWqVavBVus8QA8hPw"

def get_planilha():
    """Autentica e devolve a referência da primeira aba da planilha."""
    creds = Credentials.from_service_account_file(CREDENCIAIS_JSON, scopes=SCOPES)
    client = gspread.authorize(creds)
    return client.open_by_key(ID_PLANILHA).sheet1

# ==========================================
# ROTAS DE INTERFACE (PÁGINAS HTML)
# ==========================================

@app.route('/')
def index():
    """Página principal para o cliente fazer o agendamento."""
    return render_template('index.html')

@app.route('/admin')
def admin():
    """Painel de administração geral."""
    return render_template('admin.html')

@app.route('/barber')
def barber():
    """Visão específica para os barbeiros."""
    return render_template('barber.html')

@app.route('/manage')
def manage():
    """Página de gestão de serviços e configurações."""
    return render_template('manage.html')

@app.route('/privacy')
def privacy():
    """Página de política de privacidade."""
    return render_template('privacy.html')

# ==========================================
# ROTAS DE API (DADOS)
# ==========================================

@app.route('/api/agendar', methods=['POST'])
def criar_agendamento():
    """Recebe os dados do formulário e insere na planilha."""
    try:
        dados = request.json
        data = dados.get('data')
        hora = dados.get('hora')
        cliente = dados.get('cliente')
        servico = dados.get('servico')
        telefone = dados.get('telefone')
        
        if not all([data, hora, cliente, servico, telefone]):
            return jsonify({"erro": "Todos os campos são obrigatórios."}), 400

        sheet = get_planilha()
        
        # Estrutura da linha: Data, Hora, Cliente, Serviço, Telefone, Status
        nova_linha = [data, hora, cliente, servico, telefone, "Confirmado"]
        sheet.append_row(nova_linha)
        
        return jsonify({"mensagem": "Agendamento realizado com sucesso!"}), 201
        
    except Exception as e:
        return jsonify({"erro": str(e)}), 500

@app.route('/api/ag
