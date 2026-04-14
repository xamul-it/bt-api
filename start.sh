#!/bin/bash

set -euo pipefail

export PATH=$PATH:../.local/bin

BT_ENV_FILE="${BT_ENV_FILE:-/home/htpc/backtrader/env/pa2}"
if [ -f "$BT_ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$BT_ENV_FILE"
  set +a
fi

BT_DB_ENV_FILE="${BT_DB_ENV_FILE:-/home/htpc/backtrader/env/bt-live-events}"
if [ -f "$BT_DB_ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$BT_DB_ENV_FILE"
  set +a
fi

BT_SRV_ENV_FILE="${BT_SRV_ENV_FILE:-/home/htpc/backtrader/env/server}"
if [ -f "$BT_SRV_ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$BT_SRV_ENV_FILE"
  set +a
fi


export SERVER_PORT="${SERVER_PORT:-9090}"
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
