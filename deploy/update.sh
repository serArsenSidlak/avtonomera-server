#!/bin/bash
# Pull the latest server code and restart services.
set -e
cd /opt/app
git pull --ff-only
systemctl restart avto-bot avto-api
echo "updated + restarted at $(date)"
