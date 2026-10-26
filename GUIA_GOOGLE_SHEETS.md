# Usando o Google Sheets como banco de dados

A planilha É o banco e também o painel: você edita serviços, horários, folgas e vê os agendamentos direto nela.

## 1. Criar a conta de serviço (uma vez)
1. Acesse console.cloud.google.com e crie um projeto (ex.: "barbearia").
2. Menu **APIs e serviços → Biblioteca** → ative a **Google Sheets API**.
3. **APIs e serviços → Credenciais → Criar credenciais → Conta de serviço**. Dê um nome e conclua.
4. Abra a conta criada → aba **Chaves → Adicionar chave → Criar nova chave → JSON**. O arquivo baixado são as suas credenciais: **não suba para o GitHub**.
5. Copie o e-mail da conta (algo como `barbearia@projeto.iam.gserviceaccount.com`).

## 2. Criar e compartilhar a planilha
1. Crie uma planilha vazia em sheets.google.com.
2. Clique em **Compartilhar**, cole o e-mail da conta de serviço e dê permissão de **Editor**.
3. Copie o ID da planilha: na URL `docs.google.com/spreadsheets/d/ESTE_TRECHO/edit`.

## 3. Preparar as abas automaticamente
```bash
pip install -r requirements.txt
export SHEET_ID=cole-o-id
export GOOGLE_SA_FILE=credenciais-google.json
python sheets.py
```
Isso cria as abas **Config, Barbeiros, Servicos, Produtos, Horarios, Folgas, Agendamentos** com dados de exemplo. Pode rodar de novo sem apagar nada.

## 4. Testar localmente
```bash
python app.py        # abre em http://localhost:8080
```

## Como editar pela planilha
| Aba | O que fazer |
|---|---|
| **Config** | Nome, endereço, WhatsApp (só números com DDD), Instagram, descrição, `intervalo_min` (passo dos horários). |
| **Barbeiros** | `id` único (b1, b2...), nome, função. `ativo` = `nao` esconde do site. |
| **Servicos** | `minutos` define quanto tempo o barbeiro fica ocupado. `preco` em número (45 ou 45,50). |
| **Produtos** | Aparecem no site se houver ao menos um ativo. |
| **Horarios** | `barbeiro` = `*` vale para todos, ou o id para um barbeiro específico. `dia` = seg, ter, qua, qui, sex, sab, dom. Dia sem linha = fechado. Almoço é opcional. |
| **Folgas** | `*` ou id do barbeiro + `data` (AAAA-MM-DD). Sem `inicio`/`fim` bloqueia o dia todo (feriados, férias). |
| **Agendamentos** | Criada pelo site. Mude a coluna `status` para `concluido`, `faltou` ou `cancelado` (os dois últimos liberam o horário). **Não apague o cabeçalho.** |

## Limites a conhecer
- A API do Google permite ~60 leituras/min. O app usa cache de 15–60 s, então uma mudança na planilha pode levar até 1 min para aparecer no site.
- Use **1 worker** do gunicorn (já configurado). Com vários processos o bloqueio de duplo agendamento deixa de valer.
- A planilha aguenta bem milhares de linhas, mas fica lenta acima de algumas dezenas de milhares. Se chegar lá, arquive meses antigos em outra aba/planilha.
