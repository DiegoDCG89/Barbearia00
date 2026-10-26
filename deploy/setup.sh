#!/usr/bin/env bash
# Instala e (re)atualiza a barbearia numa VM Ubuntu da Oracle.
# Uso:  sudo bash deploy/setup.sh seudominio.duckdns.org
# Pode ser rodado de novo para atualizar o código (não apaga .env, banco nem sessão do WhatsApp).
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

DOMAIN="${1:?Informe o domínio. Ex.: sudo bash deploy/setup.sh minhabarbearia.duckdns.org}"
APP_USER="${SUDO_USER:-ubuntu}"
APP_DIR=/opt/barbearia
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"
[ "$(id -u)" -eq 0 ] || { echo "Rode com sudo."; exit 1; }

echo "==> Pacotes base"
apt-get update -y
apt-get install -y python3 python3-venv python3-pip sqlite3 curl git rsync openssl ca-certificates gnupg \
  debian-keyring debian-archive-keyring apt-transport-https iptables-persistent

echo "==> Swap de 2 GB (a VM gratuita tem só 1 GB de RAM)"
if ! swapon --show | grep -q .; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "==> Node.js 20"
if ! command -v node >/dev/null || [ "$(node -p 'process.versions.node.split(".")[0]')" -lt 20 ]; then
  curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
  apt-get install -y nodejs
fi

echo "==> Caddy (HTTPS automático)"
if ! command -v caddy >/dev/null; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y
  apt-get install -y caddy
fi

echo "==> rclone (backup na nuvem)"
command -v rclone >/dev/null || curl -fsSL https://rclone.org/install.sh | bash

echo "==> Copiando o código para $APP_DIR"
mkdir -p "$APP_DIR/data" "$APP_DIR/backups" "$APP_DIR/secrets"
rsync -a --exclude '.git' --exclude 'venv' --exclude 'node_modules' --exclude '.env' \
  --exclude 'data' --exclude 'backups' --exclude 'secrets' --exclude 'wa-gateway/auth' "$SRC_DIR"/ "$APP_DIR"/
chown -R "$APP_USER":"$APP_USER" "$APP_DIR"
chmod 700 "$APP_DIR/secrets"

echo "==> Python (venv) e dependências"
sudo -u "$APP_USER" python3 -m venv "$APP_DIR/venv"
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

echo "==> Gateway WhatsApp (npm)"
cd "$APP_DIR/wa-gateway"
if [ -f package-lock.json ]; then sudo -u "$APP_USER" npm ci --omit=dev --no-audit --no-fund
else sudo -u "$APP_USER" npm install --omit=dev --no-audit --no-fund; fi

echo "==> Configuração (.env)"
NEW_PASS=""
if [ ! -f "$APP_DIR/.env" ]; then
  NEW_PASS="$(openssl rand -base64 18 | tr -d '/+=')"
  cat > "$APP_DIR/.env" <<ENVF
ADMIN_USER=admin
ADMIN_PASS=$NEW_PASS
DB_PATH=$APP_DIR/data/barbearia.db
BACKUP_DIR=$APP_DIR/backups
# Google Agenda: envie o JSON da conta de serviço para $APP_DIR/secrets/ e descomente a linha abaixo
# GOOGLE_SA_FILE=$APP_DIR/secrets/google-sa.json
# Avisos no celular (veja "Monitoramento" no guia): ALERT_NTFY_URL=https://ntfy.sh/<nome-secreto>
# Contato exibido na Política de Privacidade: PRIVACY_CONTACT=seuemail@exemplo.com
# Backup na nuvem (veja a seção "Backup na nuvem" do guia): descomente depois de configurar o rclone
# RCLONE_REMOTE=gdrive:barbearia-backups
DEFAULT_RETURN_DAYS=21
WA_GATEWAY_URL=http://127.0.0.1:3001
WA_GATEWAY_SECRET=$(openssl rand -hex 24)
WA_DEMO=0
ENVF
  chown "$APP_USER":"$APP_USER" "$APP_DIR/.env"; chmod 600 "$APP_DIR/.env"
fi

# endereço público (usado nos links de cancelar/remarcar enviados por WhatsApp); também corrige instalações antigas
grep -q '^PUBLIC_URL=' "$APP_DIR/.env" || echo "PUBLIC_URL=https://$DOMAIN" >> "$APP_DIR/.env"

echo "==> Serviços (systemd)"
sed "s/__USER__/$APP_USER/" "$APP_DIR/deploy/barbearia.service"  > /etc/systemd/system/barbearia.service
sed "s/__USER__/$APP_USER/" "$APP_DIR/deploy/wa-gateway.service" > /etc/systemd/system/wa-gateway.service
sed "s/__DOMAIN__/$DOMAIN/" "$APP_DIR/deploy/Caddyfile"          > /etc/caddy/Caddyfile
rm -f /etc/cron.d/barbearia-backup   # o backup diário agora roda dentro do app (03:30)

echo "==> Firewall da VM (portas 80 e 443)"
for p in 80 443; do
  iptables -C INPUT -p tcp --dport $p -j ACCEPT 2>/dev/null || iptables -I INPUT 1 -p tcp --dport $p -j ACCEPT
done
netfilter-persistent save >/dev/null

systemctl daemon-reload
systemctl enable --now barbearia wa-gateway
systemctl restart barbearia wa-gateway caddy

echo
echo "=============================================================="
echo " Pronto!  Site:  https://$DOMAIN      Admin:  https://$DOMAIN/admin"
if [ -n "$NEW_PASS" ]; then
  echo " Usuário do admin: admin     Senha: $NEW_PASS   (guarde; está em $APP_DIR/.env)"
fi
echo " Parear o WhatsApp (QR):  sudo journalctl -u wa-gateway -f"
echo "=============================================================="
