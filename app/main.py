import os
import json
from app.paths import DATA_PATH, OUT_PATH, SCHEDULE_PATH
from flask import jsonify, request, Blueprint, abort
from flask_cors import CORS
from uuid import uuid4
import shutil
import app.service.main_service as srv
from app.service.EventEmitter import EventEmitter
import logging


mn = Blueprint('main', __name__)
CORS(mn)

event_emitter = EventEmitter()
logger = logging.getLogger(__name__)
srv.load_data()


@mn.route('/main', methods=['POST'])
def main():
    # Ottieni i dati inviati dal frontend
    data = request.json
    operation_id = str(uuid4())

    args = srv.json2args(operation_id,data)

    data["id"] = str(operation_id)
    srv.runstrat_background(data, args)
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
    srv.load_data()
    # Restituisce l'elenco delle chiamate attive e il loro stato
    return jsonify(list(srv.runs.values()))

@mn.route('/schedule', methods=['POST'])
def schedule():
    '''
    Schedula l'esecuzione del calcolo.
    Le possibilità sono: 
        giornaliero: il calcolo viene fatto una volta al giorno alle 20:00
        orario: il calcolo viene fatto ogni ora dalle 8:00 alle 20:00
        settimanale: il calcolo viene fatto il venerdì sera alle 20:00
    1) Viene copiato un file id nella cartella schedule, con nome_file = nome e viene aggiunto un'attributo
    2) type che indica il tipo di schedulazione
    3) Viene invocato il servizio dello scheduler che avvia il job con nome nome_file

    '''
    id = request.get_json().get('id')
    name = request.get_json().get('name')
    scheduleType = request.get_json().get('type')
    r = srv.runs[id].copy()
    r["scheduleType"] = scheduleType
    r["id"] = name
    destination_path = os.path.join(SCHEDULE_PATH, f"{name}.json")

    logger.debug(f"{destination_path}-- {r}")

    if os.path.exists(destination_path):
        return abort(400, description='Schedulename alredy exixting')

    with open(destination_path, 'w') as f:
        json.dump(r, f)

    event_emitter.emit(EventEmitter.EV_SCHEDULER,r)
    return jsonify({'message': 'Successfully scheduled'})


@mn.route('/pin-switch', methods=['POST'])
def pin_switch():
    id = request.get_json().get('id')
    r = srv.runs[id]
    strategy = r["args"]["strategia"]["value"]

    # Define source and destination paths
    if r["pinned"]:
        source_path = DATA_PATH
        destination_path = OUT_PATH
    else:
        source_path = OUT_PATH
        destination_path = DATA_PATH
    
    strategy = strategy.split(".")[-1]
    source_path = os.path.join(source_path, strategy)
    source_path = os.path.join(source_path, id)

    destination_path = os.path.join(destination_path, strategy)
    destination_path = os.path.join(destination_path, id)

    print(f"{source_path}-- {destination_path}")
    # Move the folder using shutil.move()
    shutil.move(source_path, destination_path)
    r["pinned"] = not r["pinned"]
    event_emitter.emit(EventEmitter.EV_UPDATED_RUNS,r)
    return jsonify({'message': 'Data updated successfully'})

@mn.route('/delete', methods=['POST'])
def delete():
    id = request.get_json().get('id')
    del srv.runs[id]
    event_emitter.emit(EventEmitter.EV_UPDATED_RUNS,request.get_json())
    return jsonify({'message': 'Data updated successfully'})
