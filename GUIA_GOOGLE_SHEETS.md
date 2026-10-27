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
- A cada ~10 min (`SYNC_MINUTES`), todo atendimento cujo horário já passou vira uma linha `receita` / `servico` em `Lancamentos` (valor = preço do serviço) e o evento fica verde (`concluido`). Não duplica.
- Preencha `forma_pgto` (dinheiro, pix, debito, credito) e corrija `valor` se houve desconto. Despesas, venda de produtos e outras receitas: digite uma linha (`tipo` = `receita` ou `despesa`; `data` no formato AAAA-MM-DD).
- A aba **Resumo** mostra receitas, despesas, saldo e total por forma de pagamento do mês digitado em `B1` (AAAA-MM).

**Estoque** (planilha 2)
- Não edite a quantidade: registre **movimentações** em `Movimentacoes` (`tipo` = `entrada`, `saida` ou `ajuste`; ajuste aceita número negativo). O estoque atual = entradas − saídas + ajustes; aparece na coluna `estoque` de `Produtos` e no site.
- Produto novo: adicione a linha em `Produtos` e copie a fórmula da coluna `estoque` da linha de cima.

**Atalhos do app:** `/admin` abre a planilha financeira, `/estoque` a de estoque, `/agenda` o Google Agenda.

## Limites a conhecer
- A API do Google permite ~60 leituras/min por planilha; o app usa cache de 15–60 s (mudanças na planilha levam até 1 min para aparecer no site).
- Use **1 worker** do gunicorn (já configurado): o bloqueio de duplo agendamento e a rotina de lançamento dependem de um único processo.
- Conta de serviço não convida clientes por e-mail; por isso o cliente recebe o botão "Adicionar ao Google Agenda" na tela de confirmação.
