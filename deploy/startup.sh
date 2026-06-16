#!/bin/bash
# GCE startup script: provision the Avtonomera bot + API on a fresh Ubuntu VM.
# Secrets are read from instance metadata (bot-token, pg-password, api-key); non-secret
# config is inline below.
set -e
export DEBIAN_FRONTEND=noninteractive

META="http://metadata.google.internal/computeMetadata/v1/instance/attributes"
mq() { curl -s -H "Metadata-Flavor: Google" "$META/$1"; }
BOT_TOKEN="$(mq bot-token)"
PG_PASSWORD="$(mq pg-password)"
API_KEY="$(mq api-key)"

apt-get update -y
apt-get install -y python3-pip python3-venv git curl

cd /opt
rm -rf app
git clone https://github.com/serArsenSidlak/avtonomera-server.git app
cd /opt/app
python3 -m venv venv
. venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

cat > /opt/app/local/.env <<ENVEOF
LOCAL_BOT_TOKEN=${BOT_TOKEN}
LOCAL_ADMIN_CHAT_ID=755423429
LOCAL_DB_BACKEND=postgres
LOCAL_PG_HOST=aws-1-us-east-1.pooler.supabase.com
LOCAL_PG_PORT=5432
LOCAL_PG_DB=postgres
LOCAL_PG_USER=postgres.mdnxzcskjocraxmcbncy
LOCAL_PG_PASSWORD=${PG_PASSWORD}
LOCAL_API_KEY=${API_KEY}
LOCAL_API_RATE_PER_MIN=120
ENVEOF

cat > /etc/systemd/system/avto-api.service <<SVC
[Unit]
Description=Avtonomera API
After=network-online.target
[Service]
WorkingDirectory=/opt/app
ExecStart=/opt/app/venv/bin/python -m local.api
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
SVC

cat > /etc/systemd/system/avto-bot.service <<SVC
[Unit]
Description=Avtonomera Bot
After=network-online.target
[Service]
WorkingDirectory=/opt/app
ExecStart=/opt/app/venv/bin/python -m local.bot
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
SVC

systemctl daemon-reload
systemctl enable --now avto-api avto-bot
