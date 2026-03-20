#!/bin/bash

set -euo pipefail

export PATH=$PATH:../.local/bin

export SERVER_PORT="${SERVER_PORT:-5000}"
export BT_SHARED_CONFIG="${BT_SHARED_CONFIG:-/home/htpc/backtrader/config-common}"
export BT_API_CONFIG="${BT_API_CONFIG:-/home/htpc/backtrader/bt-api/config}"
if [ -x "./.venv/bin/python" ]; then
  PYTHON_BIN="${PYTHON_BIN:-./.venv/bin/python}"
else
  PYTHON_BIN="${PYTHON_BIN:-python}"
fi
APP_SERVER="${APP_SERVER:-gunicorn}"

#git pull -f
"$PYTHON_BIN" -m pip install -r ./requirements.txt

if [ "$APP_SERVER" = "gunicorn" ]; then
  exec "$PYTHON_BIN" -m gunicorn server:app -c ./gunicorn.conf.py
fi

exec "$PYTHON_BIN" ./app.py
