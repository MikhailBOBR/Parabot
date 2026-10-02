#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Создан .env. Заполните BOT_TOKEN и ADMIN_IDS, затем запустите ./start.sh снова."
  exit 0
fi
if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
fi
.venv/bin/python -m pip install -r requirements.txt --disable-pip-version-check
exec .venv/bin/python -m parabot
