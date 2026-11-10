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
- `/admin` painel (abas: **Agenda, Caixa, Resumo, Lançamentos, Despesas, Estoque, Cadastros, Horários, Usuários, Mensagens, Site**)
- `/redefinir-senha` página aberta pelo link de recuperação de senha

| Nível (`papel`) | Pode |
|---|---|
| `cliente` (padrão de quem se cadastra) | agendar, ver só a própria agenda e o próprio histórico |
| `barbeiro` | tudo isso + painel `/admin` (financeiro, estoque, cadastros, usuários) |

**Primeiro barbeiro:** crie sua conta em `/entrar` e, na aba `Usuarios` da planilha, troque `cliente` por `barbeiro` na sua linha. Depois disso você promove outras pessoas na aba **Usuários** do painel. Se `ADMIN_PASSWORD` estiver definida, o login `admin` + essa senha também dá acesso de emergência (apague a variável quando não precisar mais).

## Arquivos
- `app.py` rotas públicas, regras de horário, login e sincronização · `admin_panel.py` API do painel
- `contas.py` cadastro, login, níveis de acesso, minha conta, recuperação de senha e gestão de usuários
- `emailer.py` envio de e-mail por SMTP (só para a recuperação de senha)
- `sheets.py` acesso às planilhas · `agenda.py` acesso ao Google Agenda
- `templates/admin.html` painel (CSS/JS próprios) · `templates/entrar.html`, `conta.html` contas · `templates/index.html`, `manage.html`, `privacy.html` site
- `vercel.json` rota + cron diário
- `static/noir.css` identidade visual de todo o site (CSS puro, sem build; fontes Cormorant Garamond + Manrope via Google Fonts) · `templates/_logo.html` logo (1ª palavra do nome normal, o resto em dourado itálico)
- `static/tailwind.css`, `tailwind.config.js` e `deploy/build-css.sh` **não são mais usados** pelas páginas; podem ser apagados

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

## Vendas de produtos
- **Junto com o corte:** na Agenda, ao clicar em **Concluir**, a seção "Produtos vendidos junto" soma os produtos ao atendimento. Cada produto vira uma receita (categoria `produto`, mesma forma de pagamento do atendimento) e dá baixa no estoque.
- **Avulsa:** aba **Caixa** — monte a venda com vários produtos, escolha a forma de pagamento e finalize.
- O estoque de **todos** os itens é conferido antes de gravar: se faltar um, nada é lançado. Se o Google falhar no meio de uma conclusão, repetir a conclusão não duplica lançamentos nem a baixa do estoque.

## Recuperação de senha
1. **Por e-mail:** "Esqueci minha senha" na tela de login envia um link (vale 1 hora, uso único). Precisa das variáveis `SMTP_*` (ver `.env.example`).
2. **Pelo WhatsApp:** em **Usuários → Link de senha** o barbeiro gera o link (24 h, uso único) e abre o WhatsApp com a mensagem pronta. Serve para quem se cadastrou só com telefone.
3. **Senha temporária:** em **Usuários → Senha temporária**.
O link é assinado com a `SECRET_KEY` e inclui a impressão digital da senha atual: ao trocar a senha, ele morre. A tela de pedido responde igual exista a conta ou não.

## Telefone do barbeiro e mensagens por WhatsApp
- **Cadastro:** em Cadastros → Barbeiros → Editar, preencha o *WhatsApp do barbeiro*. A coluna `telefone` é criada sozinha na aba `Barbeiros` se a sua planilha for antiga. **O número aparece no site** (cartão da equipe, confirmação do agendamento, "Meu agendamento" e "Minha conta"); só aparece quem tiver número cadastrado.
- **Cliente → barbeiro:** botão "WhatsApp" no cartão do barbeiro e "Falar com <nome>" depois de agendar, com a mensagem pronta.
- **Painel → barbeiro:** na Agenda, "Avisar barbeiro" (dados do cliente, agenda de hoje ou de amanhã) ou aba **Mensagens → Equipe**. Você escolhe o barbeiro e o WhatsApp abre no número dele.
- **Painel → cliente:** na Agenda, "Mensagem ao cliente" (lembrete, confirmação, pedir para remarcar, pós-atendimento, chamar de volta) ou aba **Mensagens → Clientes**. Você escolhe **qual barbeiro assina**: nome e número dele vão no fim da mensagem. O link de senha (Usuários) também permite escolher quem assina.
- O WhatsApp abre na conta que estiver logada no aparelho de quem clica: o sistema monta a mensagem e o link, mas não envia sozinho nem "pelo número" do barbeiro escolhido.

## Despesas
Aba **Despesas**: registre gastos com categoria (aluguel, energia, água, internet, produtos, salários, marketing, manutenção, impostos, material, outros), forma de pagamento e data. *Repetir por (meses)* cria um lançamento por mês (aluguel, internet). Veja o total do mês e por categoria (também no Resumo), edite data/valor/categoria e exclua o que foi lançado à mão. Receitas automáticas (atendimentos, vendas) só podem ser editadas.
