#!/bin/bash
# Pull latest code; reinstall deps and restart services ONLY if something changed.
set -e
cd /opt/app
BEFORE=$(git rev-parse HEAD)
git pull --ff-only -q
AFTER=$(git rev-parse HEAD)
if [ "$BEFORE" != "$AFTER" ]; then
  /opt/app/venv/bin/pip install -q -r requirements.txt || true
  systemctl restart avto-bot avto-api
  echo "updated ${BEFORE:0:7} -> ${AFTER:0:7}, restarted"
else
  echo "no changes"
fi
