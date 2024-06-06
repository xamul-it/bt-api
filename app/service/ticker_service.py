import os
import json
from datetime import datetime
from threading import Thread, current_thread
import yfinance as yf
import requests
import urllib3
import pandas as pd
import app.service.ticker_service as srv
from app.service.EventEmitter import EventEmitter
from app.paths import TICKER_LISTS_FILE, TICKER_PATH, TICKER_FILE, CONFIG_PATH, TICKERLIST_PATH, OUT_PATH, BENCHMARK_PATH
from app.service.EventEmitter import EventEmitter
import time


data_list = []
blacklist = []

emitter = EventEmitter()

def get_ticker_lists():
    '''
    Restituisce la lista delle liste ticker
    '''
    if not os.path.isfile(TICKER_LISTS_FILE):
        # Genera una lista di dizionari per ogni file nella cartella tickers
        update_ticker_lists()

    with open(TICKER_LISTS_FILE, 'r') as file:
        data = json.load(file)
    return data

def update_ticker_lists():
    '''
    Aggiorna le lista dei ticker
    '''
    old=[]
    if os.path.isfile(TICKER_LISTS_FILE):
        with open(TICKER_LISTS_FILE, 'r') as file:
            old = json.load(file)
    tickers = []
    # Trasformiamo data_list in un set di filename per una ricerca più efficiente
    
    filenames_in_data_list = {item['filename'] for item in data_list if item['status'] == 'ok'}
    print(f"Inizio Elabolo la lista {filenames_in_data_list}")
    for filename in os.listdir(TICKERLIST_PATH):
        if filename.endswith(".json"):
            file_path = os.path.join(TICKERLIST_PATH, filename)
            try:
                with open(file_path, 'r') as file:
                    data = json.load(file)

                    # Contiamo quanti elementi di data sono presenti in filenames_in_data_list
                    valid = sum(1 for item in data if item in filenames_in_data_list)

                    num_items = len(data) #if isinstance(data, list) else 0
            except json.JSONDecodeError:
                num_items = 0  # Se il file non è un JSON valido o non è un array
            stats = os.stat(file_path)
            old_list = [lista for lista  in old if lista['name'] == filename[:-5]]
            old_list = None if len(old_list) == 0 else old_list[0]
                    
            tickers.append({
                    'name': filename[:-5],
                    'created': datetime.fromtimestamp(stats.st_ctime).strftime('%Y-%m-%d %H:%M:%S') if old_list is None else old_list["created"],
                    'updated': datetime.fromtimestamp(stats.st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
                    'num': num_items,
                    'valid': valid,
                    'avatar': '' if old_list is None else old_list["avatar"] ,
                    'des': filename if old_list is None else old_list["des"]
                })
    # Scrive i dati nel file index.json
    with open(TICKER_LISTS_FILE, 'w') as f:
        json.dump(tickers, f, indent=4)
    #data = json.load(file)
    print(tickers)

fetchlist={}

def fetch_ticker_data_background():
    '''
    Aggiorna i dati dei ticker in background
    '''

    thread = Thread(target=fetch_ticker_data)
    thread.start()

def fetch_ticker_data():
    '''
    Aggiorna tutti i ticker presenti nella cartella
    '''
    print(f"fetch file:{TICKER_FILE}")
    with open(TICKER_FILE, 'r') as file:
        ticker = json.load(file)
        for item in ticker:
            # Crea una sessione personalizzata
            session = requests.Session()
            session.verify = False  # Disabilita la verifica del certificato
            urllib3.disable_warnings()
            requests.packages.urllib3.disable_warnings() 

            name=item["filename"]
            # download ticker ‘Adj Close’ price from yahoo finance
            retries=3
            delay=5
            for attempt in range(1, retries + 1):
                try:
                    stock =  yf.download(name, progress=False, actions=True, period="max", interval="1d", session=session)
                    break
                except Exception as e:
                    print(f"errore nel caricamento di {name}: {e}")
                    if attempt < retries:
                        time.sleep(delay)  # Attendi prima di riprovare
            print(f'done {name}')

            fullname = os.path.join(f'{TICKER_PATH}', f'{name}.csv')
            stock.to_csv(fullname,sep = ',', decimal=".")
    emitter.emit(EV_TICKER_FETCHED)


def read_ticker_csv_files():
    '''
    Aggiorna la lista dei ticker e la blacklist

    '''
    csv_files = [f for f in os.listdir(TICKER_PATH) if f.endswith('.csv')]
    srv.data_list = []
    srv.blacklist = []

    #Carico la blacklist c he verrà aggiornata 
    bl_file = os.path.join(TICKERLIST_PATH, 'bl.json')
    with open(bl_file, 'r') as f:
        blacklist = json.load(f)

    for filename in csv_files:
        filepath = os.path.join(TICKER_PATH, filename)
        file_info = {
            'filename': filename.replace(".csv",""),
        }
        try:
            # Leggi il file CSV
            df = pd.read_csv(filepath)

            # Assicurati che il file non sia vuoto e che contenga la colonna 'Date'
            if not df.empty and 'Date' in df.columns:
                stats = os.stat(filepath)
                # Crea un dizionario con le informazioni richieste
                file_info['first'] = df['Date'].iloc[0] # Prima data
                file_info['last'] = df['Date'].iloc[-1]   # Ultima data
                file_info['status'] = "ok"
                file_info['updated'] = datetime.fromtimestamp(stats.st_mtime).strftime('%Y-%m-%d %H:%M:%S')
                file_info['log'] = ""
            else:
                msg = f"Il file {filename} è vuoto o manca la colonna 'Date'."
                file_info['status'] = "ko"
                file_info['log'] = msg
                print(msg)
                srv.blacklist.append(filename)
        except Exception as e:
            msg = f"Errore durante la lettura del file {filename}: {e}"
            file_info['status'] = "ko"
            file_info['log'] = msg
            print(msg)
            if filename not in blacklist:
                srv.blacklist.append(filename)

        srv.data_list.append(file_info)
    #Salvo la blacklist
    with open(bl_file, 'w') as f:
        json.dump(srv.blacklist, f, indent=4)

    # Scrive i dati nel file index.json
    with open(TICKER_FILE, 'w') as f:
        json.dump(srv.data_list, f, indent=4)
    return srv.data_list

#read_ticker_csv_files()

emitter.on(emitter.EV_TICKER_FETCHED,read_ticker_csv_files)
emitter.on(emitter.EV_TICKER_CHANGED,fetch_ticker_data)
emitter.on(emitter.EV_TICKERLIST_CHANGE,get_ticker_lists)
