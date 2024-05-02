import os
import json
from flask import jsonify, request, send_from_directory, Blueprint
from datetime import datetime
import csv

st_bp = Blueprint('strategy', __name__)

# Percorso relativo al file JSON
config_path = os.path.join(st_bp.root_path, '..', 'config')

# Percorso relativo al file JSON
json_file_path = os.path.join(config_path, 'strategies.json')

@st_bp.route('/')
def home():
    return "Ciao, mondo!"


@st_bp.route('/list')
def get_benchmarks():
    try:
        with open(json_file_path, 'r') as file:
            data = json.load(file)
            return jsonify(data)
    except FileNotFoundError:
        return jsonify({"error": f"File non trovato {json_file_path}"}), 404
    except json.JSONDecodeError:
        return jsonify({"error": "Formato file non valido"}), 500
