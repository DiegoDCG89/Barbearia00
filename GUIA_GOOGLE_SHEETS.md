# Planilhas + Google Agenda como "banco de dados"

| O quê | Onde fica |
|---|---|
| **Agendamentos** | Google Agenda (um evento por atendimento) |
| **Financeiro** | Planilha 1 (abas `Lancamentos`, `Resumo`) |
| **Estoque** | Planilha 2 (abas `Produtos`, `Movimentacoes`) |
| **Cadastro** (Config, Barbeiros, Servicos, Horarios, Folgas) | Planilha 1 (ou uma 3ª, com `SHEET_CADASTRO_ID`) |

## 1. Conta de serviço (uma vez)
1. console.cloud.google.com → crie um projeto → **APIs e serviços → Biblioteca** → ative **Google Sheets API** e **Google Calendar API**.
2. **Credenciais → Criar credenciais → Conta de serviço** → aba **Chaves → Adicionar chave → JSON**. Não suba esse arquivo para o GitHub.
3. Copie o e-mail da conta (`algo@projeto.iam.gserviceaccount.com`).

## 2. Compartilhar
- **Duas planilhas vazias** (financeiro e estoque): Compartilhar → e-mail da conta como **Editor**. Copie o ID de cada uma (trecho entre `/d/` e `/edit`).
- **Google Agenda**: Configurações da agenda → *Compartilhar com pessoas* → e-mail da conta com **Fazer alterações em eventos**. Em *Integrar agenda*, copie o **ID da agenda**. Recomendo uma agenda só para a barbearia (não a pessoal).

## 3. Preparar
```bash
pip install -r requirements.txt
export SHEET_FINANCEIRO_ID=... SHEET_ESTOQUE_ID=... CALENDAR_ID=... GOOGLE_SA_FILE=credenciais-google.json
python sheets.py     # cria as abas com dados de exemplo (pode rodar de novo sem apagar nada)
python agenda.py     # testa o acesso à agenda
python app.py        # http://localhost:8080
```

## Como funciona o dia a dia
**Agenda**
- O site cria o evento (título "Serviço – Cliente", telefone e link de gerenciar na descrição). Cancelar/remarcar pelo link do cliente apaga/move o evento.
- Evento criado por você na agenda (almoço, compromisso) **bloqueia o horário** no site. Evento de **dia inteiro** bloqueia o dia (folga). Evento marcado como "Disponível" não bloqueia.
- Vários barbeiros: preencha a coluna `calendario` da aba `Barbeiros` com o ID da agenda de cada um (vazio = usa `CALENDAR_ID`). Se dois barbeiros dividem a mesma agenda, só os eventos do site são separados por barbeiro; eventos manuais bloqueiam todos.
- **Cliente faltou?** Apague o evento (ou marque "Disponível") antes do app lançar o atendimento no financeiro.

**Financeiro** (planilha 1)
- Todo atendimento cujo horário já passou (cron diário na Vercel, ou ao clicar **Concluir** no painel) vira uma linha `receita` / `servico` em `Lancamentos` (valor = preço do serviço) e o evento fica verde (`concluido`). Não duplica.
- Ao concluir pelo painel a forma de pagamento já é gravada; os lançamentos automáticos ficam como “classificar” até você editar (aba Lançamentos). Despesas, venda de produtos e outras receitas: digite uma linha (`tipo` = `receita` ou `despesa`; `data` no formato AAAA-MM-DD).
- A aba **Resumo** mostra receitas, despesas, saldo e total por forma de pagamento do mês digitado em `B1` (AAAA-MM).

**Estoque** (planilha 2)
- Não edite a quantidade: registre **movimentações** em `Movimentacoes` (`tipo` = `entrada`, `saida` ou `ajuste`; ajuste aceita número negativo). O estoque atual = entradas − saídas + ajustes; aparece na coluna `estoque` de `Produtos` e no site.
- Produto novo: adicione a linha em `Produtos` e copie a fórmula da coluna `estoque` da linha de cima.

**Painel (`/admin`)** — faz pelo celular tudo o que antes exigia abrir a planilha: concluir/cancelar atendimentos (com forma de pagamento, desconto e gorjeta), ver o resumo do mês, lançar receitas e despesas, vender produtos e dar entrada/saída no estoque, cadastrar serviços e barbeiros, ajustar horários e folgas e editar os dados do site. Os atalhos do topo abrem as planilhas e o Google Agenda.

## Contas de usuário (login para agendar)
Para agendar, o cliente cria uma conta em `/entrar` (nome, WhatsApp, e-mail opcional e senha) e depois entra com **e-mail ou telefone + senha**. As contas ficam na aba `Usuarios` (colunas `id, nome, email, telefone, senha_hash, papel, ativo, consentimento, criado_em`). Nunca edite `senha_hash` à mão.

- **Planilha privada (recomendado):** crie uma 4ª planilha só para isso, compartilhe só com a conta de serviço (e com você) e coloque o ID em `SHEET_USUARIOS_ID`. Assim o contador ou sócios que veem o financeiro não veem os cadastros dos clientes. Depois rode `python sheets.py` para criar a aba.
- **Primeiro barbeiro:** crie sua conta pelo site e, na aba `Usuarios`, troque `papel` de `cliente` para `barbeiro` na sua linha. Os demais você promove no painel (aba **Usuários**).
- **Níveis:** `cliente` só vê a própria agenda e histórico; `barbeiro` acessa o painel inteiro (inclusive financeiro e usuários), então só promova quem é da equipe.
- Agendamentos feitos **antes** das contas não aparecem em "Minha conta" (não têm vínculo com usuário), mas continuam na Agenda e funcionam pelo link.

## Limites a conhecer
- A API do Google permite ~60 leituras/min por planilha; o app usa cache de 15–60 s (mudanças na planilha levam até 1 min para aparecer no site).
- Na Vercel há várias instâncias: o bloqueio de duplo agendamento é feito conferindo a Agenda depois de criar o evento (o horário mais novo é desfeito), não por trava em memória.
- Conta de serviço não convida clientes por e-mail; por isso o cliente recebe o botão "Adicionar ao Google Agenda" na tela de confirmação.
