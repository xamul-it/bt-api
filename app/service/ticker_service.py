import os
import json
from datetime import datetime
from threading import Thread, current_thread
import threading
import yfinance as yf
import requests
import urllib3
import pandas as pd
import app.service.ticker_service as srv
from app.service.EventEmitter import EventEmitter
from app.paths import TICKER_LISTS_FILE, TICKER_PATH, TICKER_FILE, CONFIG_PATH, TICKERLIST_PATH, OUT_PATH, BENCHMARK_PATH
from app.service.EventEmitter import EventEmitter
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
import time
import logging


data_list = []


emitter = EventEmitter()
logger = logging.getLogger(__name__)

try:
    with open(TICKER_FILE, 'r') as f:
        data_list = json.load(f)
except:
    logger.error("Impossibile inizializzare la lista dei ticker disponibili")


def get_ticker_lists(force='False'):
    '''
    Restituisce la lista delle liste ticker
    '''
    if not os.path.isfile(TICKER_LISTS_FILE) or force!='False':
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
            old_list = {} if len(old_list) == 0 else old_list[0]
            provider = old_list["provider"] if "provider" in old_list else "yahoo"
            tickers.append({
                    'name': filename[:-5],
                    'created': datetime.fromtimestamp(stats.st_ctime).strftime('%Y-%m-%d %H:%M:%S') if 'created' not in old_list else old_list["created"],
                    'updated': datetime.fromtimestamp(stats.st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
                    'num': num_items,
                    'valid': valid,
                    'avatar': '' if 'avatar' not in old_list else old_list["avatar"] ,
                    'des': filename if 'des' not in old_list else old_list["des"] ,
                    'provider': provider ,
                })
    # Scrive i dati nel file index.json
    with open(TICKER_LISTS_FILE, 'w') as f:
        json.dump(tickers, f, indent=4)
    #data = json.load(file)
    logger.debug(tickers)

fetchlist={}

def fetch_ticker_data_background(ticker_file=TICKER_FILE, provider='yahoo'):
    '''
    Aggiorna i dati dei ticker in background
    '''
    if provider=='yahoo':
        thread = Thread(target=fetch_yahoo_data, args=(ticker_file,))
    elif provider=='alpaca':
        thread = Thread(target=fetch_alpaca_data, args=(ticker_file,))
    thread.start()


def fetch_alpaca_data(ticker_file):
    logger.debug(f"avvio caricamento {ticker_file}")
    # Inserisci qui le tue credenziali Alpaca
    API_KEY = 'PK1IIXGVGZSTZVYZ713C'
    SECRET_KEY = '6EOmUlS8pSaJk7ZNhZVf5eodjFWeMlw0TWJUdhY9'

    # Inizializza il client per i dati storici
    client = StockHistoricalDataClient(API_KEY, SECRET_KEY)
    client._session.verify=False
    # Lista dei simboli dei titoli da scaricare


    with open(ticker_file, 'r') as file:
        tickers = json.load(file)

    # Definisci l'intervallo temporale desiderato
    start_date = datetime(2000, 1, 1)
    end_date   = datetime.now()


    # Prepara la richiesta per ottenere i daily bars per tutti i ticker insieme
    request_params = StockBarsRequest(
        symbol_or_symbols=tickers,
        timeframe=TimeFrame.Day,
        start=start_date,
        end=end_date
    )
    
    # Effettua la richiesta asincrona
    bars = client.get_stock_bars(request_params)
    
    # Ottieni il DataFrame; in questo caso l'indice sarà un MultiIndex:
    # livello 0 = ticker, livello 1 = data
    df = bars.df
    if df is not None and not df.empty:
        # Per ciascun ticker, estrai i dati e salva in un CSV
        for ticker in tickers:
            try:
                # Seleziona i dati relativi al ticker: il risultato avrà l'indice che ora è solo la data
                df_ticker = df.loc[ticker]
                # Per sicurezza, resetta l'indice così da avere una colonna 'datetime'
                df_ticker = df_ticker.reset_index()

                # Converte la colonna 'timestamp' in formato data (senza orario)
                if 'timestamp' in df_ticker.columns:
                    df_ticker['timestamp'] = pd.to_datetime(df_ticker['timestamp']).dt.date

                # Se non ti serve la colonna del ticker (in quanto il file è specifico per quel ticker), puoi eliminarla se presente:
                if 'symbol' in df_ticker.columns:
                    df_ticker.drop(columns=['symbol'], inplace=True)
                # Salva il DataFrame in un file CSV
                csv_filename = f"config/data/{ticker}.csv"
                df_ticker.to_csv(csv_filename, index=False)
                logger.debug(f"Salvate le quotazioni di {ticker} in {csv_filename}")
            except KeyError:
                logger.debug(f"Nessun dato ricevuto per il ticker {ticker}")
    else:
        logger.debug("Nessun dato restituito da Alpaca")
    emitter.emit(emitter.EV_TICKER_FETCHED,ticker_file)


def find_list_by_name(name):
    '''
    recupera dei  descrittori dei ticker la lista col nome specificato
    '''
    with open(TICKER_LISTS_FILE,'r') as file:
        data = json.load(file)
        for item in data:
            if item['name'] == name:
                return item
        return None  # Restituisce None se non trova alcun elemento con il nome specificato

# Creazione del lock
lock = threading.Lock()

def fetch_yahoo_data(ticker_file=TICKER_FILE):
    '''
    Aggiorna tutti i ticker presenti nella cartella
    Attenzione al parametro, 
    '''
    logger.debug(f"fetch file:{ticker_file}")
    with open(ticker_file, 'r') as file:
        ticker = json.load(file)
        # Crea una sessione personalizzata
        session = requests.Session()
        session.verify = False  # Disabilita la verifica del certificato
        urllib3.disable_warnings()
        requests.packages.urllib3.disable_warnings() 
        with lock:
            stocks =  yf.download(ticker, progress=False, actions=True, period="max", interval="1d", session=session, auto_adjust=False)
            logger.warning(f'{ticker_file}: {yf.warnings}')
        unique_tickers = stocks.columns.get_level_values('Ticker').unique()
        # Cicla su ciascun ticker e salva i relativi dati in un file CSV
        for ticker in unique_tickers:
            # Filtra il DataFrame per conservare solo le colonne relative a quel ticker
            data_for_ticker = stocks.xs(ticker, level='Ticker', axis=1)
            # Correzione degli errori di virgola
            #data_for_ticker = correct_anomalies(data_for_ticker)

            data_for_ticker = data_for_ticker.dropna()
            # Costruisci il nome del file usando il ticker
            filename = os.path.join(f'{TICKER_PATH}', f'{ticker}.csv')
            
            # Salva il DataFrame filtrato in un file CSV
            data_for_ticker.to_csv(filename,sep = ',', decimal=".", header=True, index=True)
            #stock.to_csv(fullname,sep = ',', decimal=".", header=True, index=True)
         
    logger.debug(f"Genero evento :{ticker_file}")
    emitter.emit(emitter.EV_TICKER_FETCHED,ticker_file)


def correct_anomalies(data, threshold=10):
    """
    Corregge i valori anomali causati da errori di virgola mancante.
    threshold: soglia per rilevare anomalie (es. 10 = 1000% di variazione)
    """
    data = data.copy()
    cols = ['High', 'Low', 'Open', 'Close']
    
    # Calcola la media mobile e il close precedente come riferimento
    data['Prev_Close'] = data['Close'].shift(1)
    data['Rolling_Ref'] = data['Close'].rolling(window=5, min_periods=1).mean()
    
    for i in range(1, len(data)):
        for col in cols:
            current_val = data.at[data.index[i], col]
            prev_close = data.at[data.index[i-1], 'Prev_Close']
            rolling_ref = data.at[data.index[i], 'Rolling_Ref']
            
            # Calcola il riferimento come max tra close precedente e media mobile
            ref = max(prev_close, rolling_ref)
            
            if ref > 0 and current_val > 0:
                ratio = current_val / ref
                

                # Se il rapporto supera la soglia, cerca il fattore di correzione
                if ratio >= threshold:
                    factor = 1
                    while (current_val / (10 ** factor)) / ref < threshold:
                        factor += 1
                    
                    factor = 10 ** factor
                    corrected_val = current_val / factor
                    
                    # Applica la correzione a tutte le colonne
                    for c in cols:
                        data.at[data.index[i], c] = data.at[data.index[i], c] / factor
                    data.at[data.index[i], 'Volume'] = data.at[data.index[i], 'Volume'] * factor
                    
                    print(f"Corretto: {data.index[i]} | {col} originale: {current_val} => corretto: {corrected_val} (fattore: {factor})")

    # Rimuove le colonne temporanee
    data.drop(['Prev_Close', 'Rolling_Ref'], axis=1, inplace=True)
    return data

def update_ticker_list_from_csv(ticker_file=None):
    '''
    Aggiorna la lsita dei symboli con le informazioni dello stato di download
    '''
    logger.debug(f"update_ticker_list_from_csv {ticker_file}")
    if ticker_file is None:
        csv_files = [f for f in os.listdir(TICKER_PATH) if f.endswith('.csv')]
    elif ticker_file == "bl.json":
        return 
    else:
        csv_files = []
        with open(ticker_file, 'r') as file:
            data = json.load(file)
            # Assumi che data sia una lista di stringhe
            if all(isinstance(item, str) for item in data):
                csv_files = [item + '.csv' for item in data]
            else:
                logger.error("JSON file format is incorrect: expected a list of strings")
                return []

    data_list = []

    for filename in csv_files:
        filepath = os.path.join(TICKER_PATH, filename)
        file_info = {
            'filename': filename.replace(".csv",""),
        }
        logger.debug(f"Leggo {filepath}")
        try:
            # Leggi il file CSV
            df = pd.read_csv(filepath)

            stats = os.stat(filepath)
            file_info['updated'] = datetime.fromtimestamp(stats.st_mtime).strftime('%Y-%m-%d %H:%M:%S')
            file_info['log'] = ""

            # Assicurati che il file non sia vuoto e che contenga la colonna 'Date'
            if not df.empty:
                if 'Date' in df.columns:
                    # Crea un dizionario con le informazioni richieste
                    file_info['first'] = df['Date'].iloc[0] # Prima data
                    file_info['last'] = df['Date'].iloc[-1]   # Ultima data
                    file_info['status'] = "ok"
                elif 'timestamp' in df.columns:
                    stats = os.stat(filepath)
                    # Crea un dizionario con le informazioni richieste
                    file_info['first'] = df['timestamp'].iloc[0] # Prima data
                    file_info['last'] = df['timestamp'].iloc[-1]   # Ultima data
                    file_info['status'] = "ok"
                else:
                    msg = f"Nel file {filename} manca la colonna 'Date' o 'timestamp'."
                    file_info['status'] = "ko"
                    file_info['log'] = msg
                    logger.debug(msg)
            else:
                msg = f"Il file {filename} è vuoto."
                file_info['status'] = "ko"
                file_info['log'] = msg
                logger.debug(msg)
        except Exception as e:
            msg = f"Errore durante la lettura del file {filename}: {e}"
            file_info['status'] = "ko"
            file_info['log'] = msg
            logger.debug(msg)

        data_list.append(file_info)

    #Metto insieme le liste globali da quelle locali
    #creo un dizionario dall'array data_list
    if not hasattr(srv, 'data_dict'):
        srv.data_dict = {item['filename']: item for item in srv.data_list}

    # Scorri ogni elemento in data_list e aggiorna/aggiungi nel dizionario
    for file_info in data_list:
        srv.data_dict[file_info['filename']] = file_info

    # Sincronizzare il dizionario con la lista
    srv.data_list = list(srv.data_dict.values())

    # Scrive i dati nel file index.json
    with open(TICKER_FILE, 'w') as f:
        json.dump(srv.data_list, f, indent=4)

    logger.debug(f"fine read_ticker_csv_files {ticker_file}")
    update_ticker_lists()
    return srv.data_list

#read_ticker_csv_files()

emitter.on(emitter.EV_TICKER_FETCHED,update_ticker_list_from_csv)
#TODO Sistemare perché deve aggiornare le lsite alpaca o yahoo
#emitter.on(emitter.EV_TICKER_CHANGED,fetch_ticker_data)
emitter.on(emitter.EV_TICKERLIST_CHANGE,get_ticker_lists)
