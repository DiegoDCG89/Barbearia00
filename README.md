# Barbearia — site de agendamento + painel (Flask na Vercel)

Sem banco de dados próprio: tudo mora no Google.

| Dado | Onde fica |
|---|---|
| Agendamentos | Google Agenda (um evento por atendimento) |
| Financeiro | Planilha 1 — abas `Lancamentos`, `Resumo` |
| Estoque | Planilha 2 — abas `Produtos`, `Movimentacoes` |
| Cadastro (serviços, barbeiros, horários, folgas, dados do site) | Planilha 1 (ou 3ª com `SHEET_CADASTRO_ID`) |

## Páginas
- `/` site público com agendamento · `/agendamento/<código>` o cliente cancela/remarca
- `/login` → `/admin` painel do dono (abas: **Agenda, Resumo, Lançamentos, Estoque, Cadastros, Horários, Site**)

## Arquivos
- `app.py` rotas públicas, regras de horário, login e sincronização · `admin_panel.py` API do painel
- `sheets.py` acesso às planilhas · `agenda.py` acesso ao Google Agenda
- `templates/admin.html` painel (CSS/JS próprios) · `templates/index.html`, `manage.html`, `privacy.html` site
- `vercel.json` rota + cron diário · `deploy/build-css.sh` regera `static/tailwind.css` (só o site público usa)

## Configuração (Vercel → Settings → Environment Variables)
Veja `.env.example`. Obrigatórias: `SHEET_FINANCEIRO_ID`, `SHEET_ESTOQUE_ID`, `CALENDAR_ID`, `GOOGLE_SA_JSON`,
`ADMIN_PASSWORD`, `SECRET_KEY`, `CRON_SECRET`, `PUBLIC_URL`. Passo a passo do Google em `GUIA_GOOGLE_SHEETS.md`.

## Como funciona na Vercel
- Cada requisição pode cair em uma instância diferente: o cache em memória é só um atalho (15–60 s) e a verdade está sempre no Google.
- Agendamento duplo: após criar o evento, o app confere se outro cliente pegou o mesmo horário e desfaz o mais novo.
- Atendimentos já realizados viram receita em `Lancamentos` pelo cron diário (`/api/cron/sync`, 00:00 de Brasília) ou ao clicar **Concluir** na Agenda do painel.

## Rodar localmente
```bash
pip install -r requirements.txt
export SHEET_FINANCEIRO_ID=... SHEET_ESTOQUE_ID=... CALENDAR_ID=... GOOGLE_SA_FILE=credenciais-google.json ADMIN_PASSWORD=teste
python sheets.py   # cria as abas com exemplos (não apaga nada)
python app.py      # http://localhost:8080
```
