# Hospedar a barbearia na Oracle Cloud (Always Free)

Resultado: site em `https://SEU-DOMINIO`, painel em `/admin`, banco SQLite com backup diário e (opcional) WhatsApp automático.

## 1. Conta e VM
1. Crie a conta em cloud.oracle.com (pede cartão só para verificação; escolha a região **Brasil/Vinhedo ou São Paulo** como *home region* — não dá para trocar depois).
2. Em **Compute → Instances → Create instance**:
   - Imagem: **Ubuntu 22.04** (ou 24.04).
   - Shape: **VM.Standard.E2.1.Micro** (rótulo *Always Free-eligible*). Se aparecer "out of host capacity", tente outro *availability domain* ou mais tarde.
   - Rede: marque **atribuir IP público**.
   - Chave SSH: **gere/baixe o par de chaves** e guarde o arquivo `.key` (sem ele você perde o acesso).
3. Anote o **IP público** da VM.

## 2. Liberar as portas 80 e 443 na rede da Oracle
Instances → sua VM → **Subnet** → **Security List** padrão → **Add Ingress Rules**:
- Source CIDR `0.0.0.0/0`, protocolo TCP, porta de destino `80`
- Idem para a porta `443`

(O firewall interno da VM o script `setup.sh` já abre.)

## 3. Domínio grátis (necessário: o painel usa senha e precisa de HTTPS)
1. Entre em duckdns.org, crie um subdomínio (ex.: `minhabarbearia.duckdns.org`) e aponte para o **IP público** da VM.
2. Se você já tiver domínio próprio, crie um registro **A** apontando para o IP.

## 4. Colocar o código na VM
No seu computador (use o caminho da sua chave):
```bash
ssh -i minha-chave.key ubuntu@IP_DA_VM
sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/SEU-USUARIO/barbearia.git
cd barbearia
```
> Antes, suba para o GitHub o `app.py` corrigido e a pasta `deploy/` deste pacote (e **nunca** suba `.env`, `venv/` ou `*.db`; o `.gitignore` já cobre isso).

## 5. Instalar
```bash
sudo bash deploy/setup.sh minhabarbearia.duckdns.org
```
Leva alguns minutos. No fim ele mostra o endereço e a **senha do admin** (usuário `admin`; fica em `/opt/barbearia/.env`). Abra `https://SEU-DOMINIO` e `https://SEU-DOMINIO/admin`.
O certificado HTTPS é emitido sozinho na primeira visita (o DNS precisa já estar apontando para a VM).

### Primeiros ajustes no painel (`/admin`)
- **Horários:** o padrão é seg–sáb, 9h–19h, domingo fechado. Ajuste o expediente e o almoço de cada barbeiro e clique em **Cadastrar feriados** (remova os que a barbearia abrir).
- **Cadastros:** confira barbeiros, serviços, preços e a comissão de cada um.
- Atualizando uma instalação antiga: rode de novo `sudo bash deploy/setup.sh seudominio` — ele acrescenta o `PUBLIC_URL` ao `.env` (necessário para o link de cancelar/remarcar aparecer nas mensagens) e atualiza o banco sozinho.

## 6. WhatsApp automático (opcional)
1. `sudo journalctl -u wa-gateway -f` — aparece um QR code.
2. No celular (use um **chip dedicado**, não o número principal): WhatsApp → Aparelhos conectados → Conectar aparelho → escaneie.
3. Quando aparecer "WhatsApp conectado", pronto. Enquanto não estiver pareado, os lembretes ficam na aba **Lembretes** do admin (link `wa.me`, um clique).
> A biblioteca não é oficial e viola os termos do WhatsApp: existe risco de banimento do número. Mantenha baixo volume.

## 7. Google Agenda (opcional)
1. console.cloud.google.com → crie um projeto → **APIs e serviços → Biblioteca → Google Calendar API → Ativar**.
2. **Credenciais → Criar credenciais → Conta de serviço** → depois, na conta criada, **Chaves → Adicionar chave → JSON** (baixa um arquivo).
3. Copie o arquivo para a VM: `scp -i minha-chave.key google-sa.json ubuntu@IP_DA_VM:/opt/barbearia/secrets/`
4. Na VM: `sudo nano /opt/barbearia/.env`, descomente a linha `GOOGLE_SA_FILE=...` e `sudo systemctl restart barbearia`.
5. No Google Agenda de cada barbeiro: **Configurações → Compartilhar com pessoas** → adicione o e-mail da conta de serviço (termina em `iam.gserviceaccount.com`) com permissão **Fazer alterações em eventos**; copie o **ID da agenda** (Integrar agenda).
6. No `/admin` → **Cadastros → Barbeiros**: edite o barbeiro, cole o ID e clique em **Testar agenda**.

## 8. Backup na nuvem (Google Drive)
O backup local fica na própria VM. Para ter uma cópia **fora do servidor**, o app envia cada backup diário pelo `rclone` (o `setup.sh` já o instala).
1. No **seu computador**, instale o rclone (rclone.org/downloads) e rode: `rclone authorize "drive"`. Entre na sua conta Google; o terminal mostrará um **token** (um texto entre chaves `{...}`). Copie.
2. Na **VM**: `rclone config` e responda:
   - `n` (novo remoto) → nome `gdrive` → tipo `drive` (Google Drive)
   - `client_id` e `client_secret`: Enter (em branco)
   - `scope`: escolha **`drive.file`** (normalmente a opção 3: o rclone só enxerga os arquivos que ele mesmo criou)
   - `root_folder_id` e `service_account_file`: Enter
   - *Edit advanced config*: `n` · *Use web browser to automatically authenticate*: **`n`** · cole o token em `config_token`
   - *Shared Drive*: `n` · confirme com `y` e saia com `q`
3. Teste: `rclone mkdir gdrive:barbearia-backups` e `rclone lsd gdrive:`
4. `sudo nano /opt/barbearia/.env` → descomente/adicione `RCLONE_REMOTE=gdrive:barbearia-backups` → `sudo systemctl restart barbearia`
5. No `/admin` → aba **Backup** → **Enviar para a nuvem agora**. O status mostra o último envio (ou o erro, se houver). As cópias na nuvem mais antigas que 60 dias são apagadas (`REMOTE_KEEP_DAYS`).

Observações: o backup contém nome e telefone dos clientes — proteja a conta Google com verificação em duas etapas. Se quiser as cópias criptografadas, o rclone tem o remoto `crypt` (rclone.org/crypt). Se usar o seu próprio *client ID* do Google, publique o app (status "Em produção"), senão a autorização costuma expirar em 7 dias. Qualquer outro destino do rclone (Dropbox, Backblaze B2, S3) funciona do mesmo jeito.

## 9. Monitoramento e avisos (grátis)
- **Saber se o site caiu:** crie uma conta grátis no uptimerobot.com → *Add New Monitor* → tipo **HTTP(s)** → URL `https://SEU-DOMINIO/healthz`, intervalo de 5 min. Ele avisa por e-mail se a resposta não for 200 (site, banco ou agendador parados).
- **Avisos no celular:** instale o app **ntfy**, assine um tópico com nome difícil de adivinhar (ex.: `barbearia-k7x92q`; quem souber o nome consegue ler os alertas) e na VM: `sudo nano /opt/barbearia/.env` → `ALERT_NTFY_URL=https://ntfy.sh/barbearia-k7x92q` → `sudo systemctl restart barbearia`. Você será avisado quando o WhatsApp ficar desconectado por 10 min, quando o backup na nuvem falhar ou o disco estiver quase cheio — e quando o problema for resolvido.
- A aba **Sistema** do painel mostra o estado de tudo, e uma barra vermelha aparece no topo se houver problema.

## 10. Privacidade (LGPD)
- Defina `PRIVACY_CONTACT` no `.env` (e-mail ou WhatsApp que aparece na política) e **revise o texto** em `templates/privacy.html` com um profissional antes de divulgar.
- O aceite da política é obrigatório no agendamento do site. No painel (agendamento por telefone/balcão), marque *Cliente autorizou receber mensagens* só se ele concordou; sem isso o sistema não envia mensagens automáticas a ele.
- Pedidos de clientes: aba **Privacidade** (ver dados / anonimizar). Anonimização automática após `RETENTION_MONTHS` (24) meses sem atendimento.

## 11. Barbeiros e site
- **Acesso dos barbeiros:** `/admin` → **Cadastros** → **Dar acesso** (usuário + senha de 8+ caracteres). Passe a eles o endereço `https://SEU-DOMINIO/barbeiro`. Cada um vê só o que é dele. Para trocar a senha de alguém, use **Acesso** de novo (senha em branco mantém a atual); para tirar, **Remover acesso**.
- **Site:** em `/admin` → **Site**, confira nome, **marca do logo** (padrão “00”), endereço, WhatsApp, Instagram e a descrição que aparece no Google; envie as fotos da galeria. O botão de WhatsApp só aparece se houver número.
- **Google:** depois de publicar, entre no *Google Search Console* (grátis), adicione o seu domínio e envie o endereço `https://SEU-DOMINIO/sitemap.xml`. Crie/atualize o perfil da barbearia no *Google Meu Negócio* com o mesmo nome e endereço.
- As fotos ficam em `/opt/barbearia/data/gallery` e vão para a nuvem junto com o backup (pasta `galeria`).

## 12. Dia a dia
| Tarefa | Comando |
|---|---|
| Ver logs do site | `sudo journalctl -u barbearia -f` |
| Reiniciar | `sudo systemctl restart barbearia wa-gateway` |
| Atualizar o código | `cd ~/barbearia && git pull && sudo bash deploy/setup.sh SEU-DOMINIO` |
| Trocar a senha do admin | edite `/opt/barbearia/.env` e `sudo systemctl restart barbearia` |
| Mudou classes do Tailwind nos templates | `bash deploy/build-css.sh` e commit do `static/tailwind.css` |
| Backups | automáticos às 03:30 em `/opt/barbearia/backups` (14 dias); baixe uma cópia no link **Baixar backup** do `/admin`; envio à nuvem na aba **Backup** |

**Baixe um backup de vez em quando** para o seu computador, pelo link *Baixar backup* do painel ou por `scp` (a VM e os backups estão no mesmo lugar):
```bash
scp -i minha-chave.key ubuntu@IP_DA_VM:/opt/barbearia/backups/barbearia-*.db .
```

## 13. Riscos do plano gratuito
- A Oracle pode **parar instâncias Always Free ociosas** (uso baixo de CPU/rede por 7 dias). Manter a conta em *Pay As You Go* evita a recuperação de instâncias, e você continua sem pagar enquanto ficar nos limites gratuitos (confira no console).
- Os limites e regras do Always Free mudam: confira a página oficial *Always Free Resources* da Oracle.
- Sem o IP reservado, o IP público pode mudar se a VM for recriada.
