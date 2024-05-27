import os
import json
from app.service.EventEmitter import EventEmitter
from flask import jsonify, request, send_from_directory, Blueprint
from flask_cors import CORS
from uuid import uuid4
import shutil
import app.service.main_service as srv
from app.paths import RUNS_FILE
from app.service.EventEmitter import EventEmitter

mn = Blueprint('main', __name__)
CORS(mn)

event_emitter = EventEmitter()
with open(RUNS_FILE, 'r') as f:
    data = json.load(f)
    srv.runs.update(data)



def reload_data():
    if not srv.runs:
        try:
            with open(RUNS_FILE, 'r') as f:
                data = json.load(f)
                srv.runs.update(data)
        except Exception as e:
            print(e)
            pass


@mn.route('/main', methods=['POST'])
def main():
    # Ottieni i dati inviati dal frontend
    data = request.json

    # Dizionario per la mappatura dei nomi dei campi
    mappatura_campi = {"a": "todate", "cash": "cash", "da": "from",	"importoOperazioni": "amount", "parametriStrategia": "stratargs", }

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

    args.append(f'--benchmark')
    args.append(f'{data["tickerList"]["value"]}')

    if(data["debug"] == True):
       args.append(f'--debug')

    data["id"] = str(operation_id)
    srv.runstrat_background(event_emitter, data, args)
    # Rispondi al frontend
    return jsonify({"id": operation_id, "status": "success", "message": "Dati ricevuti con successo","Dati":data})

@mn.route('/stato_chiamate', methods=['GET'])
def stato_chiamate():
    # Restituisce l'elenco delle chiamate attive e il loro stato
    try:
        return jsonify(list(srv.runs.values()))
    except Exception as e:
        print(e)
        print(srv.runs)
        return "Errore"

@mn.route('/clear', methods=['GET'])
def clear():
    srv.runs = {}
    reload_data()
    # Restituisce l'elenco delle chiamate attive e il loro stato
    return jsonify(list(srv.runs.values()))

@mn.route('/pin-switch', methods=['POST'])
def pin_switch():
    id = request.get_json().get('id')
    r = srv.runs[id]
    strategy = r["args"]["strategia"]["value"]

    # Define source and destination paths
    if r["pinned"]:
        source_path = "stored"
        destination_path = "out"
    else:
        source_path = "out"
        destination_path = "stored"
    
    strategy = strategy.split(".")[-1]
    source_path = os.path.join(source_path, strategy)
    source_path = os.path.join(source_path, id)

    destination_path = os.path.join(destination_path, strategy)
    destination_path = os.path.join(destination_path, id)

    print(f"{source_path}-- {destination_path}")
    # Move the folder using shutil.move()
    shutil.move(source_path, destination_path)
    r["pinned"] = not r["pinned"]
    event_emitter.emit(srv.RUN_BACKTRADER,r)
    return jsonify({'message': 'Data updated successfully'})

@mn.route('/delete', methods=['POST'])
def delete():
    id = request.get_json().get('id')
    del srv.runs[id]
    event_emitter.emit(srv.RUN_BACKTRADER,request.get_json())
    return jsonify({'message': 'Data updated successfully'})
