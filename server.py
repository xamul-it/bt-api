#!/usr/bin/python

from flask import Flask, request
from flask_cors import CORS
from datetime import datetime
from app.tickers import tk_bp
from app.benchmark import bm_bp
from app.strategy import st_bp
from app.scheduler import sc_bp
from app.fileserver import fs_bp
from app.live import al_bp
from app.main import mn

import os
import sys
import subprocess
import hmac
import hashlib
import logging
import warnings
from urllib3.exceptions import InsecureRequestWarning

warnings.filterwarnings("ignore", category=InsecureRequestWarning)

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

# CORS Configuration - Environment-based for dev/prod flexibility
# Set ALLOWED_ORIGINS env var for production, defaults to permissive for development
allowed_origins = os.getenv('ALLOWED_ORIGINS', '*')
if allowed_origins == '*':
    logger.warning("CORS configured for ALL origins (*) - only use in development!")
    CORS(app)
else:
    # Production: restrict to specific origins
    origins_list = [origin.strip() for origin in allowed_origins.split(',')]
    logger.info(f"CORS restricted to origins: {origins_list}")
    CORS(app, origins=origins_list, supports_credentials=True)

logger = logging.getLogger(__name__)

# Configura il livello di logging per matplotlib
logging.getLogger('matplotlib').setLevel(logging.WARNING)
logging.getLogger('yfinance').setLevel(logging.WARNING)
logging.getLogger('urllib3.connectionpool').setLevel(logging.WARNING)

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

@app.route('/')
def home():
    return "Ciao, mondo!"

if __name__ == '__main__':
    app.run(port=os.environ['SERVER_PORT'], debug=True, use_reloader=True)

# GitHub webhook configuration - load from environment
GITHUB_SECRET = os.environ.get('GITHUB_WEBHOOK_SECRET')
if GITHUB_SECRET:
    GITHUB_SECRET = GITHUB_SECRET.encode('utf-8') if isinstance(GITHUB_SECRET, str) else GITHUB_SECRET
REPO_PATH = os.environ.get('GITHUB_REPO_PATH', "/home/htpc/backtrader")
SERVICE_NAME = os.environ.get('GITHUB_RESTART_SERVICE', "zmq-proxy")


def verify_signature(payload, signature):
    """
    Verify GitHub webhook signature using HMAC SHA256.
    Returns False if signature is invalid or GITHUB_SECRET is not configured.
    """
    if signature is None or GITHUB_SECRET is None:
        logger.warning("GitHub webhook signature validation failed: missing signature or secret")
        return False

    try:
        mac = hmac.new(GITHUB_SECRET, msg=payload, digestmod=hashlib.sha256)
        expected_signature = 'sha256=' + mac.hexdigest()
        return hmac.compare_digest(expected_signature, signature)
    except Exception as e:
        logger.error(f"Error verifying webhook signature: {e}")
        return False

@app.route('/github-webhook', methods=['POST'])
def github_webhook():
    """
    GitHub webhook endpoint for automatic deployment.
    Requires GITHUB_WEBHOOK_SECRET environment variable to be set.
    """
    # Check if webhook is configured
    if GITHUB_SECRET is None:
        logger.error("GitHub webhook called but GITHUB_WEBHOOK_SECRET not configured")
        return "Webhook not configured", 503

    payload = request.get_data()
    signature = request.headers.get("X-Hub-Signature-256")

    # Verify signature
    if not verify_signature(payload, signature):
        logger.warning(f"Invalid webhook signature from {request.remote_addr}")
        return "Invalid signature", 403

    try:
        logger.info(f"Valid webhook received from {request.remote_addr}, starting deployment")

        # Pull repository
        pull_result = subprocess.run(
            ["git", "-C", REPO_PATH, "pull", "--no-edit"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30
        )
        logger.info(f"Git pull completed: {pull_result.stdout}")

        # Restart service
        restart_result = subprocess.run(
            ["systemctl", "--user", "restart", SERVICE_NAME],
            check=True,
            capture_output=True,
            text=True,
            timeout=10
        )
        logger.info(f"Service restart completed: {restart_result.stdout}")

        return f"Deployment successful:\n{pull_result.stdout}\n{restart_result.stdout}", 200

    except subprocess.TimeoutExpired as e:
        logger.error(f"Deployment timeout: {e}")
        return f"Deployment timeout: {e.cmd}", 500
    except subprocess.CalledProcessError as e:
        logger.error(f"Deployment failed: {e.stderr}")
        return f"Deployment failed: {e.stderr}", 500
    except Exception as e:
        logger.error(f"Unexpected error during deployment: {e}", exc_info=True)
        return "Internal server error", 500