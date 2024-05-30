import os
import json
from flask import Flask
from flask_cors import CORS
from datetime import datetime
from app.tickers import tk_bp
from app.benchmark import bm_bp
from app.strategy import st_bp
from app.scheduler import sc_bp

from app.main import mn
import sys

#sys.path.append('../')  # Aggiusta il percorso in base alla tua struttura di cartelle

app = Flask(__name__)
CORS(app)  # Abilita CORS per tutto il tuo applicativo Flask

#Gestione dei benchmark
app.register_blueprint(bm_bp, url_prefix='/bm')

#Gestionew dei Ticker
app.register_blueprint(tk_bp, url_prefix='/tk')

#Gestionew dei Main
app.register_blueprint(mn, url_prefix='/mn')

#Gestionew dei Strategy
app.register_blueprint(st_bp, url_prefix='/st')

# Registra il Blueprint dello scheduler
app.register_blueprint(sc_bp, url_prefix='/sc')

@app.route('/')
def home():
    return "Ciao, mondo!"


if __name__ == '__main__':
    app.run(debug=True)