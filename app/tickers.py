import os
import json
from flask import jsonify, request, send_from_directory, Blueprint
from datetime import datetime

tk_bp = Blueprint('tickers', __name__)

# Percorso relativo al file JSON
tickers_path = os.path.join(tk_bp.root_path, '..', 'tickers')
config_path = os.path.join(tk_bp.root_path, '..', 'config')
# Percorso relativo al file JSON
json_file_path = os.path.join(config_path, 'tickers.json')


def create_index_file(tickers_path,json_file_path):
    # Genera una lista di dizionari per ogni file nella cartella tickers
    tickers = []
    for filename in os.listdir(tickers_path):
        if filename.endswith(".json") and filename != "index.json":
            file_path = os.path.join(tickers_path, filename)
            try:
                with open(file_path, 'r') as file:
                    data = json.load(file)
                    num_items = len(data) if isinstance(data, list) else 0
            except json.JSONDecodeError:
                num_items = 0  # Se il file non è un JSON valido o non è un array


            stats = os.stat(file_path)
            tickers.append({
                'name': filename[:-5],
                'created': datetime.fromtimestamp(stats.st_ctime).strftime('%Y-%m-%d %H:%M:%S'),
                'updated': datetime.fromtimestamp(stats.st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
                'num': num_items,
                'avatar': '',
                'des': filename
            })

    # Scrive i dati nel file index.json
    with open(json_file_path, 'w') as f:
        json.dump(tickers, f, indent=4)


@tk_bp.route('/get-tickerls')
def get_tickers():

    # Crea il file index.json se non esiste
    if not os.path.isfile(json_file_path):
        create_index_file(tickers_path,json_file_path)

    try:
        with open(json_file_path, 'r') as file:
            data = json.load(file)
            return jsonify(data)
    except FileNotFoundError:
        return jsonify({"error": "File non trovato"}), 404
    except json.JSONDecodeError:
        return jsonify({"error": "Formato file non valido"}), 500


@tk_bp.route('/options')
def options():
    response = get_tickers()
    # Assumiamo che la risposta sia già un oggetto Flask.Response con dati JSON
    # Se non lo è, dovresti adattare il codice per gestire la risposta correttamente
    original_data = response.get_json()

    transformed_data = [
        {
            "label": item["des"],
            "value": item["name"]
        } for item in original_data
    ]

    return jsonify(transformed_data)

@tk_bp.route('/update-tickerls/<list_name>', methods=['POST'])
def update_ticker(list_name):
    data_to_update = request.json
    file_path = os.path.join(tickers_path, 'index.json')

    if not os.path.isfile(file_path):
        return jsonify({"error": "File non trovato"}), 404

    try:
        found = False
        with open(file_path, 'r+') as file:
            data = json.load(file)
            # Trova l'elemento da aggiornare nell'array
            for item in data:
                if item['name'].lower() == list_name.lower():
                    item.update(data_to_update)
                    item['updated'] = datetime.now().strftime('%d/%m/%Y')  # Aggiorna la data di modifica
                    found = True
                    break
            file.seek(0)  # Riporta il puntatore all'inizio del file
            json.dump(data, file, indent=4)
            file.truncate()  # Rimuovi il contenuto in eccesso
        if found:
            return jsonify({"success": "Dati aggiornati correttamente"})
        else:
            return jsonify({"fail": f"Record {list_name} non trovato"}), 404
    except json.JSONDecodeError:
        return jsonify({"error": "Errore nella lettura del file JSON"}), 500

@tk_bp.route('/tickerls/<ticker_name>')
def get_ticker(ticker_name):
    file_path = os.path.join(tickers_path, f'{ticker_name}.json')

    # Verifica che il file esista
    if not os.path.isfile(file_path):
        return jsonify({"fail": f"Record {ticker_name} non trovato"}), 404
        #abort(404)  # Restituisce un errore 404 se il file non esiste

    # Restituisce il contenuto del file
    return send_from_directory(tickers_path, f'{ticker_name}.json')

@tk_bp.route('/update-tickers/<list_name>', methods=['POST'])
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
