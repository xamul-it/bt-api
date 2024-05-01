import os
import json
from flask import Flask
from flask_cors import CORS
from datetime import datetime
from tickers import tk_bp
from benchmark import bm_bp

app = Flask(__name__)
CORS(app)  # Abilita CORS per tutto il tuo applicativo Flask
app.register_blueprint(bm_bp, url_prefix='/bm')
app.register_blueprint(tk_bp, url_prefix='/tk')



@app.route('/')
def home():
    return "Ciao, mondo!"


if __name__ == '__main__':
    app.run(port=5001, debug=True)
