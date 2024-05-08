import os
import json
import pandas as pd
from threading import Thread, current_thread
from flask import jsonify, request, send_from_directory, Blueprint
from datetime import datetime
import yfinance as yf
import requests
import urllib3

tk_bp = Blueprint('tickers', __name__)

# Percorso relativo al file JSON delle liste di ticker
tickers_path = os.path.join(tk_bp.root_path, '..', 'tickers')
# Percorso relativo al file JSON dei ticker
ticker_path = os.path.join(tk_bp.root_path, '..', 'data')

config_path = os.path.join(tk_bp.root_path, '..', 'config')

# Percorso relativo al file JSON con le liste ticker
tickers_file_path = os.path.join(config_path, 'tickers.json')
# Percorso relativo al file JSON con i ticker
ticker_file_path = os.path.join(config_path, 'ticker.json')


def create_index_file():
    # Genera una lista di dizionari per ogni file nella cartella tickers
    tickers = []
    for filename in os.listdir(tickers_path):
        if filename.endswith(".json"):
            file_path = os.path.join(tickers_path, filename)
            try:
                with open(file_path, 'r') as file:
                    data = json.load(file)
                    num_items = len(data) #if isinstance(data, list) else 0
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
    with open(tickers_file_path, 'w') as f:
        json.dump(tickers, f, indent=4)


@tk_bp.route('/get-tickerls')
def get_tickers():
    # Crea il file index.json se non esiste
    if not os.path.isfile(tickers_file_path):
        create_index_file()
    try:
        with open(tickers_file_path, 'r') as file:
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

@tk_bp.route('/read')
def read_ticker_csv_files():
    '''
    Legge i ticker da usare e crea una lista

    '''
    csv_files = [f for f in os.listdir(ticker_path) if f.endswith('.csv')]
    data_list = []

    for filename in csv_files:
        filepath = os.path.join(ticker_path, filename)
        try:
            # Leggi il file CSV
            df = pd.read_csv(filepath)

            # Assicurati che il file non sia vuoto e che contenga la colonna 'Date'
            if not df.empty and 'Date' in df.columns:
                stats = os.stat(filepath)
                # Crea un dizionario con le informazioni richieste
                file_info = {
                    'filename': filename.replace(".csv",""),
                    'first': df['Date'].iloc[0],  # Prima data
                    'last': df['Date'].iloc[-1],   # Ultima data
                    'updated': datetime.fromtimestamp(stats.st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
                }
                data_list.append(file_info)
            else:
                print(f"Il file {filename} è vuoto o manca la colonna 'Date'.")
        except Exception as e:
            print(f"Errore durante la lettura del file {filename}: {e}")

    # Scrive i dati nel file index.json
    with open(ticker_file_path, 'w') as f:
        json.dump(data_list, f, indent=4)

    return data_list

@tk_bp.route('/fix')
def fix():
    '''
    Legge i ticker da usare e crea una lista

    '''
    csv_files = [f for f in os.listdir(ticker_path) if f.endswith('.MI')]

    for filename in csv_files:
        filepath = os.path.join(ticker_path, filename)
        try:
            os.remove(filepath)
        except Exception as e:
            print(f"Errore durante la lettura del file {filename}: {e}")
    return csv_files

@tk_bp.route('/list')
def list():
    '''
    Lista di ticker
    '''
    if not os.path.isfile(ticker_file_path):
        read_ticker_csv_files()
    try:
        with open(ticker_file_path, 'r') as file:
            ticker = json.load(file)
            return jsonify(ticker)
    except FileNotFoundError:
        return jsonify({"error": "File non trovato"}), 404
    except json.JSONDecodeError:
        return jsonify({"error": "Formato file non valido"}), 500


def update_ticker():
    with open(ticker_file_path, 'r') as file:
        ticker = json.load(file)
        for item in ticker:
            # Crea una sessione personalizzata
            session = requests.Session()
            session.verify = False  # Disabilita la verifica del certificato
            urllib3.disable_warnings()
            requests.packages.urllib3.disable_warnings() 

            name=item["filename"]
            # download ticker ‘Adj Close’ price from yahoo finance
            stock =  yf.download(name, progress=False, actions=True, period="max", interval="1d", session=session)
            print(f'done {name}')

            fullname = os.path.join(f'{ticker_path}', f'{name}.csv')
            stock.to_csv(fullname,sep = ',', decimal=".")
    read_ticker_csv_files()


@tk_bp.route('/update')
def update():
    thread = Thread(target=update_ticker, args=())
    # Invoca runstrat con gli argomenti convertiti
    thread.start()

    try:
        with open(ticker_file_path, 'r') as file:
            ticker = json.load(file)
            return jsonify(ticker)
    except FileNotFoundError:
        return jsonify({"error": "File non trovato"}), 404
    except json.JSONDecodeError:
        return jsonify({"error": "Formato file non valido"}), 500



@tk_bp.route('/update-tickerls/<list_name>', methods=['POST'])
def update_tickerls(list_name):
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
