#!/usr/bin/env bash
# One-time setup on a fresh Ubuntu VM to run the Akamai bypass tester.
# Usage:  bash akamai_test_setup.sh
set -e

echo "== встановлюю системні пакети (python, pip, xvfb) =="
sudo apt-get update -y
sudo apt-get install -y python3 python3-pip python3-venv xvfb

echo "== python-залежності =="
python3 -m pip install --user --upgrade pip
python3 -m pip install --user playwright

echo "== завантажую Chromium + його системні залежності =="
export PLAYWRIGHT_BROWSERS_PATH="$HOME/ms-playwright"
python3 -m playwright install --with-deps chromium

echo
echo "✅ Готово. Тепер запусти тест (headed через xvfb):"
echo "    xvfb-run -a python3 akamai_test.py \"Львівська\""
echo "або через проксі:"
echo "    xvfb-run -a python3 akamai_test.py \"Львівська\" http://user:pass@host:port"
