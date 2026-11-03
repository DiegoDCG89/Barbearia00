# Barbearia — site de agendamento + painel (Flask na Vercel)

Sem banco de dados próprio: tudo mora no Google.

| Dado | Onde fica |
|---|---|
| Agendamentos | Google Agenda (um evento por atendimento) |
| Financeiro | Planilha 1 — abas `Lancamentos`, `Resumo` |
| Estoque | Planilha 2 — abas `Produtos`, `Movimentacoes` |
| Cadastro (serviços, barbeiros, horários, folgas, dados do site) | Planilha 1 (ou 3ª com `SHEET_CADASTRO_ID`) |
| Contas de usuário (aba `Usuarios`: login, senha em hash, nível de acesso) | Planilha de cadastro, ou uma **privada** com `SHEET_USUARIOS_ID` (recomendado) |

## Páginas e níveis de acesso
- `/` site público · para **agendar é preciso estar logado** (botão "Agendar" leva ao login e volta)
- `/entrar` login (**e-mail ou telefone** + senha) e criação de conta · `/minha-conta` agenda própria, histórico, dados e troca de senha
- `/agendamento/<código>` cancelar/remarcar pelo link do agendamento
- `/admin` painel (abas: **Agenda, Resumo, Lançamentos, Estoque, Cadastros, Horários, Usuários, Site**)

| Nível (`papel`) | Pode |
|---|---|
| `cliente` (padrão de quem se cadastra) | agendar, ver só a própria agenda e o próprio histórico |
| `barbeiro` | tudo isso + painel `/admin` (financeiro, estoque, cadastros, usuários) |

**Primeiro barbeiro:** crie sua conta em `/entrar` e, na aba `Usuarios` da planilha, troque `cliente` por `barbeiro` na sua linha. Depois disso você promove outras pessoas na aba **Usuários** do painel. Se `ADMIN_PASSWORD` estiver definida, o login `admin` + essa senha também dá acesso de emergência (apague a variável quando não precisar mais).

## Arquivos
- `app.py` rotas públicas, regras de horário, login e sincronização · `admin_panel.py` API do painel
- `contas.py` cadastro, login, níveis de acesso, minha conta e gestão de usuários
- `sheets.py` acesso às planilhas · `agenda.py` acesso ao Google Agenda
- `templates/admin.html` painel (CSS/JS próprios) · `templates/entrar.html`, `conta.html` contas · `templates/index.html`, `manage.html`, `privacy.html` site
- `vercel.json` rota + cron diário · `deploy/build-css.sh` regera `static/tailwind.css` (só o site público usa)

## Configuração (Vercel → Settings → Environment Variables)
Veja `.env.example`. Obrigatórias: `SHEET_FINANCEIRO_ID`, `SHEET_ESTOQUE_ID`, `CALENDAR_ID`, `GOOGLE_SA_JSON`,
`SECRET_KEY` (sem ela os logins caem toda hora), `CRON_SECRET`, `PUBLIC_URL`. Recomendada: `SHEET_USUARIOS_ID`. Opcional: `ADMIN_PASSWORD` (acesso de emergência). Passo a passo do Google em `GUIA_GOOGLE_SHEETS.md`.

## Como funciona na Vercel
- Cada requisição pode cair em uma instância diferente: o cache em memória é só um atalho (15–60 s) e a verdade está sempre no Google.
- Agendamento duplo: após criar o evento, o app confere se outro cliente pegou o mesmo horário e desfaz o mais novo.
- Atendimentos já realizados viram receita em `Lancamentos` pelo cron diário (`/api/cron/sync`, 00:00 de Brasília) ou ao clicar **Concluir** na Agenda do painel.

## Rodar localmente
```bash
pip install -r requirements.txt
export SHEET_FINANCEIRO_ID=... SHEET_ESTOQUE_ID=... CALENDAR_ID=... GOOGLE_SA_FILE=credenciais-google.json SECRET_KEY=teste ADMIN_PASSWORD=teste
python sheets.py   # cria as abas com exemplos (não apaga nada)
python app.py      # http://localhost:8080
```

## Segurança das contas
- Senhas só como hash (scrypt); nenhuma rota devolve o hash. Mínimo de 8 caracteres, senhas óbvias são recusadas.
- Rebaixar/desativar alguém vale em até ~20 s. Trocar a senha derruba os outros aparelhos conectados.
- Limite de tentativas de login por IP e por login. Na Vercel esse limite é **por instância** (a memória não é compartilhada), então é uma proteção parcial; para algo mais forte, use as regras de rate limit do Firewall da Vercel em `/api/auth/*`.
- O e-mail e o telefone **não são verificados** (não há envio de e-mail/SMS). Por isso a agenda de cada conta é ligada ao **id da conta**, nunca ao telefone digitado.
- Esqueci a senha: o barbeiro gera uma senha temporária em Usuários → Nova senha.
