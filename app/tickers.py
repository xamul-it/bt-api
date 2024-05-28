import os
import shutil
import json
from threading import Thread, current_thread
from app.paths import TICKER_LISTS_FILE, TICKER_PATH, TICKER_FILE, CONFIG_PATH, TICKERLIST_PATH, OUT_PATH, BENCHMARK_PATH
from  app.service.EventEmitter import EventEmitter
from flask import jsonify, request, send_from_directory, Blueprint
from datetime import datetime
import app.service.ticker_service as srv
import app.service.main_service as mn_srv
from uuid import uuid4
from pytickersymbols import PyTickerSymbols


tk_bp = Blueprint('tickers', __name__)

event_emitter = EventEmitter()

def copy_benchmark(data):
    print(f"Finito {data}")
    if data["stato"] == "Completato" and "benchmark" in data["args"]:
        print("EUREKKA")
        infile = os.path.join(OUT_PATH, "BuyAndHold", data["id"], "returns.csv")
        outfile = os.path.join(BENCHMARK_PATH, data["args"]["tickerList"]["value"].split('.')[0]+".csv")
        print(outfile)
        shutil.copyfile(infile,outfile)


event_emitter.on(mn_srv.RUN_BACKTRADER, copy_benchmark)

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

@tk_bp.route('/benchmarks')
def benchmarks():
    data = []
    print("_______________________________Benchmark")
    for root, dirs, files in os.walk(TICKERLIST_PATH):
        for file in files:
            print(file)
            list_name= file.split('.')[0]
            print(list_name)
            benchmark(list_name)
            data.append(list_name)
    print("_______________________________Benchmark")
    # Rispondi al frontend
    return jsonify({"status": "success", "message": "Dati ricevuti con successo","Dati":data})



@tk_bp.route('/benchmark/<list_name>')
def benchmark(list_name):

    file_path = os.path.join(TICKERLIST_PATH, f'{list_name}.json')
    # Apri il file e carica il contenuto JSON
    with open(file_path, 'r') as file:
        data = json.load(file)

    # 'data' dovrebbe essere un array, quindi puoi usare la funzione len() per ottenere il numero di elementi
    number_of_elements = len(data)

    # Converti il dizionario JSON in una lista di argomenti da riga di comando
    # Ad esempio, converte {"param1": "val1", "param2": "val2"} in ["--param1", "val1", "--param2", "val2"]
    args = []
    operation_id = str(uuid4())

    #Il cash deve essere 10000+1% per titolo 
    args.append(f'--cash')
    cash = number_of_elements*10001
    args.append(f"{cash}")

    args.append(f'--amount')
    args.append("10000")

    args.append(f'--fromdate')
    args.append('2000-01-01')

    args.append(f'--id')
    args.append(str(operation_id))
    args.append(f'--strat')
    args.append(f'generic.BuyAndHold')
    args.append(f'--commission') 
    args.append(f'fineco')
    args.append(f'--ticker')
    args.append(f'{list_name}.json')
 
    dati = { "id":str(operation_id),"strategia":{"label":"BuyAndHold","value":"generic.BuyAndHold"},
             "tickerList":{"value":f"{list_name}.json","label":list_name},"cash": cash, "da":"2000-01-01", "importoOperazioni":10000, "benchmark":"benchmark"}
    mn_srv.runstrat_background(event_emitter, dati, args)

    # Rispondi al frontend
    return jsonify({"id": operation_id, "status": "success", "message": "Dati ricevuti con successo","Dati":data})


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

    srv.get_ticker_lists()
    init()
    return yahoo_symbols_eur

@tk_bp.route('/get/<symbol>')
def get(symbol):
    file_path = os.path.join(TICKER_PATH,symbol+".csv")
    with open(file_path, 'r') as file:
        return jsonify(file.read())