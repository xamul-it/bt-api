from datetime import datetime
import json
import os
import traceback
from btmain import runstrat
from threading import Thread, current_thread
from app.service.EventEmitter import EventEmitter
from app.paths import  DATA_PATH
import logging

emitter = EventEmitter()
logger = logging.getLogger(__name__)


def json2args(operation_id,data):
    # Dizionario per la mappatura dei nomi dei campi
    mappatura_campi = {"a": "todate", "cash": "cash", "da": "from",	"importoOperazioni": "amount", "parametriStrategia": "stratargs", }

    # Converti il dizionario JSON in una lista di argomenti da riga di comando
    # Ad esempio, converte {"param1": "val1", "param2": "val2"} in ["--param1", "val1", "--param2", "val2"]
    args = []
    for key, value in data.items():

        # Usa get per prevenire KeyError se la chiave non è trovata nella mappatura
        if key in mappatura_campi:
            arg_name = mappatura_campi.get(key, key)

            args.append(f'--{arg_name}')
            args.append(str(value))

    args.append(f'--id')
    args.append(str(operation_id))    
    args.append(f'--strat')
    args.append(f'{data["strategia"]["value"]}')
    args.append(f'--commission') 
    args.append(f'{data["tipoCommissioni"]["value"]}')
    args.append(f'--ticker')
    args.append(f'{data["tickerList"]["value"]}.json')
    
    if "broker" in data:
        args.append(f'--broker')
        args.append(f'{data["broker"]["value"]}')


    args.append(f'--benchmark')
    args.append(f'{data["tickerList"]["value"]}')

    if(data["debug"] == True):
       args.append(f'--debug')
    return args


def runstrat_background( data, args=[]):
    operation_id = data["id"]

    tdata = {}
    tdata["id"] = operation_id
    tdata["args"] = data
    tdata["args"]["args"] = args
    tdata["stato"] = "In esecuzione"
    tdata["pinned"] = False
    tdata["descizione"] = ""

    thread = Thread(target=btrunstrat, args=(tdata, args))
    # Invoca runstrat con gli argomenti convertiti
    thread.start()
    logger.debug(f"Emetto segnale")
    emitter.emit(emitter.EV_UPDATED_RUNS,tdata)
    return tdata


def btrunstrat(data, args=[]):
    """Funzione wrapper per eseguire runstrat in un thread separato e tenere traccia dello stato."""
    try:
        data["start"] = int(datetime.now().timestamp() * 1000)
        runstrat(args=args) 
    except Exception as e:
        logger.exception("Errore nell'eseguire la strategia")
        data["stato"] = "Errore"
        data["errorMessage"] = f"{e}"
        emitter.emit(emitter.EV_UPDATED_RUNS, data)
    else:
        data["stato"] = "Completato"
        data["end"] = int(datetime.now().timestamp() * 1000)
        emitter.emit(emitter.EV_UPDATED_RUNS, data)
        logger.debug("Fine elaborazione")


# Struttura dati per tenere traccia delle chiamate attive
runs = {}

def load_data():
    """Load configuration from JSON files in DATA_PATH"""
    loaded_count = 0
    failed_files = []

    # Iterare su tutti i file nella cartella
    for filename in os.listdir(DATA_PATH):
        if not filename.endswith('.json'):
            continue

        file_path = os.path.join(DATA_PATH, filename)

        try:
            # Aprire il file JSON e caricarne i dati
            with open(file_path, 'r', encoding='utf-8') as json_file:
                data = json.load(json_file)
                runs.update(data)
                loaded_count += 1
                logger.debug(f"Loaded {filename}: {len(data)} entries")

        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in {filename}: {e}")
            failed_files.append(filename)

        except (OSError, PermissionError) as e:
            logger.error(f"Cannot read {filename}: {e}")
            failed_files.append(filename)

        except KeyError as e:
            logger.error(f"Missing required key in {filename}: {e}")
            failed_files.append(filename)

        except Exception as e:
            # Unexpected errors should NOT be silently ignored
            logger.exception(f"Unexpected error loading {filename}: {e}")
            raise  # Re-raise to surface bugs

    logger.info(f"Loaded {loaded_count} JSON files from {DATA_PATH}")

    if failed_files:
        logger.warning(f"Failed to load {len(failed_files)} files: {', '.join(failed_files)}")

    if loaded_count == 0 and len([f for f in os.listdir(DATA_PATH) if f.endswith('.json')]) > 0:
        raise RuntimeError(f"No valid JSON files could be loaded from {DATA_PATH}")


def save_data(data):
    #aggiungo l'elòemento se è valido
    if data["id"] not in runs and "stato" in data:
        runs[data["id"]] = data

    filtered_runs = {key: run for key, run in runs.items() if run.get('pinned')}
    
    for key, run in filtered_runs.items():
        id = run.get("id")
        file = os.path.join(DATA_PATH, f'{id}.json')
        with open(file, 'w') as f:
            json.dump(filtered_runs, f)

emitter.on(emitter.EV_UPDATED_RUNS, save_data)
