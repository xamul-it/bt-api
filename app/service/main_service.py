from datetime import datetime
import json
import traceback
from btmain import runstrat
from threading import Thread, current_thread
from app.service.EventEmitter import EventEmitter
from app.paths import RUNS_FILE

emitter = EventEmitter()

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
    print(f"Emetto segnale")
    emitter.emit(emitter.EV_RUN_BACKTRADER,tdata)
    return tdata


def btrunstrat(data, args=[]):
    """Funzione wrapper per eseguire runstrat in un thread separato e tenere traccia dello stato."""
    
    try:
        data["start"] = int(datetime.now().timestamp() * 1000)
        runstrat(args=args) 
    except Exception as e:
        print(traceback.print_exception(e))
        data["stato"] = "Errore"
        data["errorMessage"] = f"{e}"
        emitter.emit(emitter.EV_RUN_BACKTRADER, data)
    else:
        data["stato"] = "Completato"
        data["end"] = int(datetime.now().timestamp() * 1000)
        emitter.emit(emitter.EV_RUN_BACKTRADER, data)
        print("Fine elaborazione")


# Struttura dati per tenere traccia delle chiamate attive
runs = {}

def save_data(data):
    if data["id"] not in runs:
        runs[data["id"]] = data

    filtered_runs = {key: run for key, run in runs.items() if run.get('pinned')}

    with open(RUNS_FILE, 'w') as f:
        json.dump(filtered_runs, f)

emitter.on(emitter.EV_RUN_BACKTRADER, save_data)
