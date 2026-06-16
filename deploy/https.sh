#!/bin/bash
# Install Caddy as an HTTPS reverse proxy (auto Let's Encrypt) in front of the API on :8000.
# Domain: nip.io wildcard that resolves to this VM's public IP — no domain purchase needed.
set -e
DOMAIN="34.123.136.171.nip.io"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl gnupg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
apt-get update -y
apt-get install -y caddy
cat > /etc/caddy/Caddyfile <<CADDY
${DOMAIN} {
    reverse_proxy localhost:8000
}
CADDY
systemctl restart caddy
echo "HTTPS ready: https://${DOMAIN}"
