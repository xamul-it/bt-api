import os
import json
from flask import jsonify, request, send_from_directory, Blueprint
from threading import Thread, current_thread
from flask_cors import CORS
from datetime import datetime
from app.utils.event_emitter import EventEmitter
import sys
from uuid import uuid4
import shutil
import traceback

sys.path.append('../')  # Aggiusta il percorso in base alla tua struttura di cartelle
from btmain import runstrat

mn = Blueprint('main', __name__)
CORS(mn)

# Percorso relativo al file JSON
benchmark_path = os.path.join(mn.root_path, '..', 'benchmark')
config_path = os.path.join(mn.root_path, '..', 'config')

# Percorso relativo al file JSON
json_file_path = os.path.join(benchmark_path, 'benchmarks.json')



# Percorso relativo al file JSON
data_path = os.path.join(mn.root_path, '..', 'stored')
# Percorso relativo al file JSON
json_file = os.path.join(data_path, 'runs.json')


# Struttura dati per tenere traccia delle chiamate attive
runs = {}
event_emitter = EventEmitter()

def save_data():
    filtered_runs = {key: run for key, run in runs.items() if run.get('pinned')}

    if not os.path.exists(data_path):
        os.makedirs(data_path)
    with open(json_file, 'w') as f:
        json.dump(filtered_runs, f)


event_emitter.on('data_changed', save_data)


@mn.before_request
def load_initial_data():
    if not runs:
        try:
            with open(json_file, 'r') as f:
                data = json.load(f)
                runs.update(data)
        except Exception as e:
            print(e)
            pass

def runstrat_background(event_emitter, data, args=[]):
    """Funzione wrapper per eseguire runstrat in un thread separato e tenere traccia dello stato."""
    #thread_id = current_thread().ident
    
    try:
        data["start"] = int(datetime.now().timestamp() * 1000)
        runstrat(args=args) 
    except Exception as e:
        print(traceback.print_exception())
        data["stato"] = f"Errore: {e}"
        event_emitter.emit('data_changed')
    else:
        data["stato"] = "Completato"
        data["end"] = int(datetime.now().timestamp() * 1000)
        event_emitter.emit('data_changed')


@mn.route('/main', methods=['POST'])
def main():
    # Ottieni i dati inviati dal frontend
    data = request.json

    # Dizionario per la mappatura dei nomi dei campi
    mappatura_campi = {
	"a": "todate",
	"cash": "cash",
	"da": "from",
	"importoOperazioni": "amount",
	"parametriStrategia": "stratargs",
    }

    # Converti il dizionario JSON in una lista di argomenti da riga di comando
    # Ad esempio, converte {"param1": "val1", "param2": "val2"} in ["--param1", "val1", "--param2", "val2"]
    args = []
    operation_id = str(uuid4())
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
    if(data["debug"] == True):
       args.append(f'--debug')

    tdata = {}
    tdata["id"] = operation_id
    tdata["args"] = data
    tdata["stato"] = "In esecuzione"
    tdata["pinned"] = False
    tdata["descizione"] = ""

    thread = Thread(target=runstrat_background, args=(event_emitter, tdata, args))
    runs[operation_id] = tdata

    # Invoca runstrat con gli argomenti convertiti
    thread.start()

    event_emitter.emit('data_changed')

    # Rispondi al frontend
    return jsonify({"id": operation_id, "status": "success", "message": "Dati ricevuti con successo","Dati":data})

@mn.route('/stato_chiamate', methods=['GET'])
def stato_chiamate():
    # Restituisce l'elenco delle chiamate attive e il loro stato
    return jsonify(list(runs.values()))

@mn.route('/clear', methods=['GET'])
def clear():
    runs = {}
    load_initial_data()
    # Restituisce l'elenco delle chiamate attive e il loro stato
    return jsonify(list(runs.values()))

@mn.route('/pin-switch', methods=['POST'])
def pin_switch():
    id = request.get_json().get('id')
    r = runs[id]
    strategy = r["args"]["strategia"]["value"]

    # Define source and destination paths
    if r["pinned"]:
        source_path = "stored"
        destination_path = "out"
    else:
        source_path = "out"
        destination_path = "stored"
    
    if not os.path.exists(data_path):
        os.makedirs(data_path)

    strategy = strategy.split(".")[-1]
    source_path = os.path.join(source_path, strategy)
    source_path = os.path.join(source_path, id)

    destination_path = os.path.join(destination_path, strategy)
    destination_path = os.path.join(destination_path, id)

    print(f"{source_path}-- {destination_path}")
    # Move the folder using shutil.move()
    shutil.move(source_path, destination_path)
    r["pinned"] = not r["pinned"]
    event_emitter.emit('data_changed')
    return jsonify({'message': 'Data updated successfully'})

@mn.route('/delete', methods=['POST'])
def delete():
    id = request.get_json().get('id')
    del runs[id]
    event_emitter.emit('data_changed')
    return jsonify({'message': 'Data updated successfully'})
