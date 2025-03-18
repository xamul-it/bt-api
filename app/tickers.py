import os
import json
from app.paths import TICKER_LISTS_FILE, TICKER_PATH,  TICKERLIST_PATH
from flask import jsonify, request, Blueprint
from datetime import datetime
import app.service.ticker_service as srv
import logging
from pytickersymbols import PyTickerSymbols


tk_bp = Blueprint('tickers', __name__)
logger = logging.getLogger(__name__)


@tk_bp.route('/get-lists')
def get_ticker_lists():
    force = request.args.get('force', default='False', type=str)
    data = srv.get_ticker_lists(force=force)
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
        srv.update_ticker_list_from_csv()
    return jsonify(srv.data_list)


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
    Restituisce i ticker contenuti in una lista
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
     Aggiorna i ticker presenti in una lista, non viene agiornata la blacklist
    '''
    logger.debug(f"LIST {list_name}")
    if list_name == "bl":
        return jsonify({"message": "Blacklist non aggiornabile"}), 400
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



@tk_bp.route('/init', defaults={'list_name': None})
@tk_bp.route('/init/<list_name>')
def init_tickers(list_name=None):
    '''
    Scarica i ticker dal provider.
    Se non viene passato il nome della lista aggiorna l'intero elenco dei tcker
    Se viene passato un il nome della lista scarica i ticker presenti nella lista

    '''
    logger.info(f"Inizializzazione {list_name}")
    
    def elabora_lista(list_name, provider=None):
        file_path = os.path.join(TICKERLIST_PATH, f'{list_name}')
        #Inizializzo la lista di ticker e la blacklist
        with open(file_path, 'r') as file:
            if provider not in providers:
                providers[provider] = []
            tic = providers[provider]
            data = json.load(file)
            logger.debug(data)
            for item in data:
                fullname = os.path.join(f'{TICKER_PATH}', f'{item}.csv')
                with open(fullname, 'w') as file:
                    pass
                tic.append(item)
        #Chiamata a download
        srv.fetch_ticker_data_background(file_path,provider)

    providers={}
    # Costruisci il percorso del file
    if list_name is None:
        with open(TICKER_LISTS_FILE,'r') as file:
            ticker_lists=json.load(file)
            for ticker_list in ticker_lists:
                logger.debug(f"Elaboro la lista: {ticker_list['name']} {TICKERLIST_PATH}")
                elabora_lista(f'{ticker_list["name"]}.json',ticker_list["provider"])        
    else:
        metadata = srv.find_list_by_name(list_name)
        elabora_lista(f"{list_name}.json",metadata["provider"])

    return jsonify({"ok": f"TODO Restrituire l'ID ","data":providers}), 200



@tk_bp.route('/symbols')
def symbols():
    stock_data = PyTickerSymbols()
    indices = stock_data.get_all_indices()
    logger.debug(f'{indices}')
    for idx in indices:
        yahoo_symbols_eur = []
        stocks = stock_data.get_stocks_by_index(idx)  # Assumendo che tu sia interessato all'indice DAX
        for item in stocks:
            logger.debug(item)
            for symbol in item['symbols']:
                if symbol['currency'] == 'EUR':
                    yahoo_symbols_eur.append(symbol['yahoo'])
        file_path = os.path.join(TICKERLIST_PATH,f"{idx}.json")
        with open(file_path, 'w') as file:
            json.dump(yahoo_symbols_eur, file, indent=4)

    srv.update_ticker_lists()
    #init()
    return yahoo_symbols_eur

@tk_bp.route('/info/<symbol>')
def get_details(symbol):
    stock_data = PyTickerSymbols()
    # Cerca i dettagli della società basandoti sul simbolo del ticker
    company_info = stock_data.get_stock_by_yahoo_symbol(symbol)
    
    if company_info:
        return jsonify(company_info)
    else:
        return jsonify({"erroe" : f"Nessun dettaglio trovato per il simbolo {symbol}"}),500

@tk_bp.route('/nd')
def get_nasdaq_tickers():
    stock_data = PyTickerSymbols()
    nasdaq_stocks = stock_data.get_stocks_by_index('NASDAQ 100')  # Usa il metodo get_stocks_by_index
    
     # Creare una lista di ticker del NASDAQ
    nasdaq_tickers = [stock['symbol'] for stock in nasdaq_stocks if 'symbol' in stock]

    return jsonify(nasdaq_tickers)

    
@tk_bp.route('/get/<symbol>')
def get(symbol):
    file_path = os.path.join(TICKER_PATH,symbol+".csv")
    with open(file_path, 'r') as file:
        return jsonify(file.read())
