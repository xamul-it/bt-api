import os
import json
from app.paths import TICKER_LISTS_FILE, TICKER_PATH,  TICKERLIST_PATH
from flask import jsonify, request, Blueprint
from datetime import datetime
import app.service.ticker_service as srv

from pytickersymbols import PyTickerSymbols


tk_bp = Blueprint('tickers', __name__)


@tk_bp.route('/get-lists')
def get_ticker_lists():
    data = srv.get_ticker_lists()
    return jsonify(data)


@tk_bp.route('/options')
def options():
    response = get_ticker_lists()
    original_data = response.get_json()

    transformed_data = [
        {
            "label": item["des"],
            "value": item["name"]
        } for item in original_data
    ]

    return jsonify(transformed_data)

@tk_bp.route('/list')
def list():
    '''
    Legge tutti i simboli e verifica lo stato di ciascun file csv

    '''
    if len(srv.data_list) == 0:
        srv.read_ticker_csv_files()
    return jsonify(srv.data_list)


@tk_bp.route('/update')
def update():
    '''
    Aggiorna i ticker
    '''

    srv.fetch_ticker_data_background()
    return list()


@tk_bp.route('/update-list/<list_name>', methods=['POST'])
def update_list(list_name):
    '''
    Aggiorna il file che descrive le liste
    '''
    data_to_update = request.json

    if not os.path.isfile(TICKER_LISTS_FILE):
        return jsonify({"error": f"File non trovato {TICKER_LISTS_FILE}"}), 404

    try:
        found = False
        with open(TICKER_LISTS_FILE, 'r+') as file:
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


@tk_bp.route('/ticker-list/<ticker_name>')
def get_ticker(ticker_name):
    '''
    Restituisce la lista deio ticker contenuti in una lista
    TODO: Aggiungere la gestione per avere il dettaglio di ogni ticker
    '''
    file_path = os.path.join(TICKERLIST_PATH, f'{ticker_name}.json')

    # Verifica che il file esista
    if not os.path.isfile(file_path):
        return jsonify({"fail": f"Record {ticker_name} non trovato"}), 404
    file_name = os.path.join(TICKERLIST_PATH, f'{ticker_name}.json')
    with open(file_name, 'r') as file:
        data = json.load(file)
    # Filtra i dati
    filtered_data = [item for item in srv.data_list if item["filename"] in data]

    # Restituisce il contenuto del file
    #return send_from_directory(TICKERLIST_PATH, f'{ticker_name}.json')
    return jsonify(filtered_data)


@tk_bp.route('/update-tickers/<list_name>', methods=['POST'])
def update_tickers(list_name):
    '''
    Restituisce l'elenco dei ticker presenti in una lista
    '''
    # Ottieni l'array di valori dal corpo della richiesta
    values = request.json

    # Costruisci il percorso del file
    file_path = os.path.join(TICKERLIST_PATH, f'{list_name}.json')

    # Scrivi l'array nel file JSON
    try:
        with open(file_path, 'w') as file:
            json.dump(values, file, indent=4)
        return jsonify({"message": "Dati salvati correttamente"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@tk_bp.route('/init')
def init():
    '''
    Inizializza un nuovalista di ticker
    '''
    print("chiamata")
    tic = []
    
    for list_name in os.listdir(TICKERLIST_PATH):
        # Costruisci il percorso del file
        file_path = os.path.join(TICKERLIST_PATH, f'{list_name}')

        #Inizializzo la lista di ticker e la blkacklist
        with open(file_path, 'r') as file:
            data = json.load(file)
            for item in data:
                fullname = os.path.join(f'{TICKER_PATH}', f'{item}.csv')
                with open(fullname, 'w') as file:
                    pass
                tic.append(item)
    
    #Chiamata a download
    srv.fetch_ticker_data_background()

    return jsonify({"ok": f"TODO Restrituire l'ID {file_path}","data":tic}), 200



@tk_bp.route('/init/<list_name>')
def init_tickers(list_name):
    '''
    Inizializza un nuovalista di ticker
    '''
    print("chiamata")
    tic = []
    # Costruisci il percorso del file
    file_path = os.path.join(TICKERLIST_PATH, f'{list_name}.json')

    #Inizializzo la lista di ticker e la blkacklist
    with open(file_path, 'r') as file:
        data = json.load(file)
        for item in data:
            fullname = os.path.join(f'{TICKER_PATH}', f'{item}.csv')
            with open(fullname, 'w') as file:
                pass
            tic.append(item)
    #Chiamata a download
    srv.fetch_ticker_data_background()

    return jsonify({"ok": f"TODO Restrituire l'ID {file_path}","data":tic}), 200



@tk_bp.route('/symbols')
def symbols():
    stock_data = PyTickerSymbols()
    indices = stock_data.get_all_indices()
    for idx in indices:
        yahoo_symbols_eur = []
        stocks = stock_data.get_stocks_by_index(idx)  # Assumendo che tu sia interessato all'indice DAX
        for item in stocks:
            for symbol in item['symbols']:
                if symbol['currency'] == 'EUR':
                    yahoo_symbols_eur.append(symbol['yahoo'])
        file_path = os.path.join(TICKERLIST_PATH,f"{idx}.json")
        with open(file_path, 'w') as file:
            json.dump(yahoo_symbols_eur, file, indent=4)

    srv.update_ticker_lists()
    #init()
    return yahoo_symbols_eur

@tk_bp.route('/get/<symbol>')
def get(symbol):
    file_path = os.path.join(TICKER_PATH,symbol+".csv")
    with open(file_path, 'r') as file:
        return jsonify(file.read())
