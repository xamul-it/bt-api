import os
import json
from flask import jsonify,  Blueprint
import app.service.benchmark_service as srv
import app.service.main_service as mn_srv
from uuid import uuid4
from app.paths import BENCHMARK_PATH, BENCHMARK_FILE, TICKERLIST_PATH, OUT_PATH

bm_bp = Blueprint('benckmark', __name__)

@bm_bp.route('/')
def home():
    return "Ciao, mondo!"

@bm_bp.route('/get-benchmarks')
def get_benchmarks():
    '''
    Crea il file dei benchmark se non esiste
    '''
    if not os.path.isfile(BENCHMARK_FILE):
        srv.create_index_file(BENCHMARK_PATH ,BENCHMARK_FILE)
    try:
        with open(BENCHMARK_FILE, 'r') as file:
            data = json.load(file)
            return jsonify(data)
    except FileNotFoundError:
        return jsonify({"error": "File non trovato"}), 404
    except json.JSONDecodeError:
        return jsonify({"error": "Formato file non valido"}), 500


@bm_bp.route('/get/<name>')
def get_benchmark(name):
    '''
    Restituisce il file di benchmark
    '''
    file_path = os.path.join(BENCHMARK_PATH, f'{name}.csv')

    # Verifica che il file esista
    if not os.path.isfile(file_path):
        return jsonify({"fail": f"Record {name} non trovato"}), 404
    # Restituisce il contenuto del file
    return jsonify(srv.read_csv_to_json(file_path))


@bm_bp.route('/update')
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


@bm_bp.route('/update/<list_name>')
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
    mn_srv.runstrat_background(dati, args)

    # Rispondi al frontend
    return jsonify({"id": operation_id, "status": "success", "message": "Dati ricevuti con successo","Dati":data})
