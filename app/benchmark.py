import os
import json
from flask import jsonify, request, send_from_directory, Blueprint
import app.service.benchmark_service as srv
from app.paths import BENCHMARK_PATH, BENCHMARK_FILE, CONFIG_PATH

bm_bp = Blueprint('benckmark', __name__)


@bm_bp.route('/')
def home():
    return "Ciao, mondo!"

@bm_bp.route('/get-benchmarks')
def get_benchmarks():
    '''
    Crea il file dei benchmark se non esiste
    '''
    if not os.path.isfile(BENCHMARK_FILE):
        srv.create_index_file(BENCHMARK_PATH ,BENCHMARK_FILE)
    try:
        with open(BENCHMARK_FILE, 'r') as file:
            data = json.load(file)
            return jsonify(data)
    except FileNotFoundError:
        return jsonify({"error": "File non trovato"}), 404
    except json.JSONDecodeError:
        return jsonify({"error": "Formato file non valido"}), 500


@bm_bp.route('/benchmark/<name>')
def get_benchmark(name):
    '''
    Restituisce il file di benchmark
    '''
    file_path = os.path.join(BENCHMARK_PATH, f'{name}.csv')

    # Verifica che il file esista
    if not os.path.isfile(file_path):
        return jsonify({"fail": f"Record {name} non trovato"}), 404
    # Restituisce il contenuto del file
    return jsonify(srv.read_csv_to_json(file_path))