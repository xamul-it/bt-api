import os
import json
from flask import jsonify, request, send_from_directory, Blueprint
from flask_cors import CORS
from datetime import datetime
import csv

bm_bp = Blueprint('benckmark', __name__)
CORS(bm_bp)  # Abilita CORS per tutto il tuo applicativo Flask

@bm_bp.route('/')
def home():
    return "Ciao, mondo!"

def create_index_file(path):
    # Genera una lista di dizionari per ogni file nella cartella benchmark
    benchmark = []
    for filename in os.listdir(path):
        print(filename)
        if filename.endswith(".csv") and filename != "index.json":
            file_path = os.path.join(path, filename)
            try:
                with open(file_path, newline='') as csvfile:
                        reader = csv.DictReader(csvfile)
                        rows = list(reader)
                        start = rows[0]['index'] if rows else None
                        end = rows[-1]['index'] if rows else None
            except json.JSONDecodeError:
                num_items = 0  # Se il file non è un JSON valido o non è un array


            stats = os.stat(file_path)
            benchmark.append({
                'name': filename[:-4],
                'start': start,
                'end': end,
                'des': filename
            })

    # Scrive i dati nel file index.json
    with open(os.path.join(path, 'index.json'), 'w') as f:
        json.dump(benchmark, f, indent=4)

# Percorso relativo al file JSON
benchmark_path = os.path.join(bm_bp.root_path, '..', 'benchmark')

@bm_bp.route('/get-benchmarks')
def get_benchmarks():
    # Percorso relativo al file JSON
    json_file_path = os.path.join(benchmark_path, 'index.json')
    print(json_file_path)
    # Crea il file index.json se non esiste
    if not os.path.isfile(json_file_path):
        create_index_file(benchmark_path)

    try:
        with open(json_file_path, 'r') as file:
            data = json.load(file)
            return jsonify(data)
    except FileNotFoundError:
        return jsonify({"error": "File non trovato"}), 404
    except json.JSONDecodeError:
        return jsonify({"error": "Formato file non valido"}), 500

@bm_bp.route('/update-benchmark/<name>', methods=['POST'])
def update_ticker(name):
    data_to_update = request.json
    file_path = os.path.join(benchmark_path, 'index.json')

    if not os.path.isfile(file_path):
        return jsonify({"error": "File non trovato"}), 404

    try:
        found = False
        with open(file_path, 'r+') as file:
            data = json.load(file)
            # Trova l'elemento da aggiornare nell'array
            for item in data:
                if item['name'].lower() == name.lower():
                    item['des'] = data_to_update['des']
                    found = True
                    break
            file.seek(0)  # Riporta il puntatore all'inizio del file
            json.dump(data, file, indent=4)
            file.truncate()  # Rimuovi il contenuto in eccesso
        if found:
            return jsonify({"success": "Dati aggiornati correttamente"})
        else:
            return jsonify({"fail": f"Record {name} non trovato"}), 404
    except json.JSONDecodeError:
        return jsonify({"error": "Errore nella lettura del file JSON"}), 500

def filter_dates(data):
    # Assicurati che i dati siano ordinati per data
    data.sort(key=lambda x: x['index'])

    # Aggiungi la prima e l'ultima data
    filtered_data = [data[0], data[-1]]

    # Aggiungi le date che cadono nella prima settimana di ogni anno
    for item in data:
        date = datetime.strptime(item['index'], '%Y-%m-%d')
        if date.isocalendar()[1] == 1:  # Settimana 1 dell'anno
            if date not in filtered_data:
                filtered_data.append(item)

    return filtered_data

def read_csv(file_path):
    data = []
    with open(file_path, mode='r', newline='', encoding='utf-8') as csvfile:
        reader = csv.DictReader(csvfile)
        for row in reader:
            data.append(row)
    return data

def read_csv_to_json(filename):
    data = []
    with open(filename, newline='') as csvfile:
        reader = csv.DictReader(csvfile)
        for row in reader:
            data.append(row)
    return data

@bm_bp.route('/benchmark/<name>')
def get_benchmark(name):
    file_path = os.path.join(benchmark_path, f'{name}.csv')

    # Verifica che il file esista
    if not os.path.isfile(file_path):
        return jsonify({"fail": f"Record {name} non trovato"}), 404
        #abort(404)  # Restituisce un errore 404 se il file non esiste
    raw_data = read_csv(file_path)
    # Restituisce il contenuto del file
    return jsonify(read_csv_to_json(file_path))
    #return jsonify(filter_dates(raw_data))



@bm_bp.route('/update-tickers/<list_name>', methods=['POST'])
def update_tickers(list_name):
    # Ottieni l'array di valori dal corpo della richiesta
    values = request.json

    # Costruisci il percorso del file
    file_path = os.path.join(tickers_path, f'{list_name}.json')

    # Verifica che i valori siano un array
    if not isinstance(values, list):
        return jsonify({"error": "I dati forniti non sono un array"}), 400
    
    # Assicurati che la directory esista
    if not os.path.exists(tickers_path):
        os.makedirs(tickers_path)

    # Scrivi l'array nel file JSON
    try:
        with open(file_path, 'w') as file:
            json.dump(values, file, indent=4)
        return jsonify({"message": "Dati salvati correttamente"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
