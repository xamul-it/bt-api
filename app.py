#!/usr/bin/python
import os
import json
from flask import Flask
from flask_cors import CORS
from datetime import datetime
from app.tickers import tk_bp
from app.benchmark import bm_bp
from app.strategy import st_bp
from app.scheduler import sc_bp
from app.fileserver import fs_bp
from app.live import al_bp
from app.watchtower import obs_bp
import sys

import subprocess
import hmac
import hashlib
from flask import request


from app.main import mn
import logging
import warnings
import re
from urllib3.exceptions import InsecureRequestWarning

warnings.filterwarnings("ignore", category=InsecureRequestWarning)


def env_flag(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ('1', 'true', 'yes', 'on')


def parse_allowed_origins(value):
    if not value:
        return []
    return [origin.strip() for origin in value.split(',') if origin.strip()]


def build_cors_origins(origins):
    cors_origins = []
    for origin in origins:
        if origin.endswith(':*'):
            base = re.escape(origin[:-2])
            cors_origins.append(re.compile(rf"^{base}(?::\d+)?$"))
        else:
            cors_origins.append(origin)
    return cors_origins

# Leggi il livello di logging dalla variabile di ambiente, default a 'INFO'
log_level = os.getenv('LOG_LEVEL', 'DEBUG').upper()

url_prefix = '/dyn'

# Assicurati che il livello di log sia valido
numeric_level = getattr(logging, log_level, None)
if not isinstance(numeric_level, int):
    raise ValueError(f'Invalid log level: {log_level}')


if log_level != "DEBUG":
    # Ignora tutti i FutureWarning
    warnings.simplefilter(action='ignore', category=FutureWarning)


# Configurazione di base del logging
logging.basicConfig(level=numeric_level)


logging.error(f"{log_level} : {os.getenv('LOG_LEVEL')}")
#sys.path.append('../')  # Aggiusta il percorso in base alla tua struttura di cartelle

app = Flask(__name__)
logger = logging.getLogger(__name__)

# CORS Configuration - Environment-based for dev/prod flexibility
allowed_origins = os.getenv('ALLOWED_ORIGINS', '*')
if allowed_origins == '*':
    logger.warning("CORS configured for ALL origins (*) - only use in development!")
    CORS(app)
else:
    origins_list = parse_allowed_origins(allowed_origins)
    logger.info(f"CORS restricted to origins: {origins_list}")
    CORS(app, origins=build_cors_origins(origins_list), supports_credentials=True)

# Configura il livello di logging per matplotlib
logging.getLogger('matplotlib').setLevel(logging.WARNING)
logging.getLogger('yfinance').setLevel(logging.WARNING)
logging.getLogger('urllib3.connectionpool').setLevel(logging.WARNING)
logging.getLogger('peewee').setLevel(logging.INFO)

if False and not app.logger.handlers:
    # Crea un gestore che invia i log a stdout
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.DEBUG)

    # Definisci il formato dei log
    formatter = logging.Formatter('%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]')
    handler.setFormatter(formatter)

    # Aggiungi il gestore al logger dell'app Flask
    app.logger.addHandler(handler)

#Gestione dei fileserver
app.register_blueprint(fs_bp, url_prefix=f'/fs')

#Gestione dei benchmark
app.register_blueprint(bm_bp, url_prefix=f'{url_prefix}/bm')

#Gestionew dei Ticker
app.register_blueprint(tk_bp, url_prefix=f'{url_prefix}/tk')

#Gestionew dei Main
app.register_blueprint(mn, url_prefix=f'{url_prefix}/mn')

#Gestionew dei Strategy
app.register_blueprint(st_bp, url_prefix=f'{url_prefix}/st')

#Gestionew dei alpaca
app.register_blueprint(al_bp, url_prefix=f'{url_prefix}/al')

# Registra il Blueprint dello scheduler
app.register_blueprint(sc_bp, url_prefix=f'{url_prefix}/sc')

# Watchtower / watchdog
app.register_blueprint(obs_bp, url_prefix=f'{url_prefix}/obs')

@app.route('/')
def home():
    return "Ciao, mondo!"


if __name__ == '__main__':
    host = os.getenv('SERVER_HOST', '127.0.0.1')
    port = int(os.getenv('SERVER_PORT', '9090'))
    debug = env_flag('APP_DEBUG', True)
    reload_enabled = env_flag('APP_RELOAD', debug)
    app.run(host=host, port=port, debug=debug, use_reloader=reload_enabled)


GITHUB_SECRET = b'ssQroXoUKHRspjsONB9bQiyHmjK6nrh1'  # Sostituisci con il secret configurato nel webhook GitHub
REPO_PATH = "/home/htpc/backtrader"
SERVICE_NAME = "zmq"  # es. "myapp.service"

def verify_signature(payload, signature):
    if signature is None:
        return False
    mac = hmac.new(GITHUB_SECRET, msg=payload, digestmod=hashlib.sha256)
    return hmac.compare_digest('sha256=' + mac.hexdigest(), signature)

@app.route('/github-webhook', methods=['POST'])
def github_webhook():
    payload = request.data
    signature = request.headers.get("X-Hub-Signature-256")

    if not verify_signature(payload, signature):
        return "Invalid signature", 403

    try:
        # Stop servizio
        subprocess.run(["systemctl", "stop", SERVICE_NAME], check=True)
        # Pull repository
        subprocess.run(["git", "-C", REPO_PATH, "pull"], check=True)
        # Start servizio
        subprocess.run(["systemctl", "start", SERVICE_NAME], check=True)
        return "Deployment eseguito", 200
    except subprocess.CalledProcessError as e:
        return f"Errore durante il deployment: {e}", 500
