import json
from flask import jsonify, Blueprint
from app.paths import STRATEGIES_FILE

st_bp = Blueprint('strategy', __name__)


@st_bp.route('/')
def home():
    return "Ciao, mondo!"


@st_bp.route('/list')
def get_benchmarks():
    try:
        with open(STRATEGIES_FILE, 'r') as file:
            data = json.load(file)
            return jsonify(data)
    except FileNotFoundError:
        return jsonify({"error": f"File non trovato {STRATEGIES_FILE}"}), 404
    except json.JSONDecodeError:
        return jsonify({"error": "Formato file non valido"}), 500
