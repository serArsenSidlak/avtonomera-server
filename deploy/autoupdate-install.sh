#!/bin/bash
# Install a systemd timer that runs update.sh every 2 minutes (auto-deploy on git push).
set -e
cat > /etc/systemd/system/avto-update.service <<SVC
[Unit]
Description=Avtonomera auto-update
After=network-online.target
[Service]
Type=oneshot
ExecStart=/bin/bash /opt/app/deploy/update.sh
SVC
cat > /etc/systemd/system/avto-update.timer <<TMR
[Unit]
Description=Run avto-update every 2 minutes
[Timer]
OnBootSec=2min
OnUnitActiveSec=2min
[Install]
WantedBy=timers.target
TMR
systemctl daemon-reload
systemctl enable --now avto-update.timer
echo "auto-update timer installed and active"
