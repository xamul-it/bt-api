import shutil
import os
import json
import app.service.ticker_service as srv
import app.service.main_service as mn_srv
import csv
from app.service.EventEmitter import EventEmitter
from app.paths import BENCHMARK_PATH, OUT_PATH, BENCHMARK_FILE

srv.benchmark = []

def create_index_file(benchmark_path,json_file_path):
    # Genera una lista di dizionari per ogni file nella cartella benchmark
    srv.benchmark = []
    for filename in os.listdir(benchmark_path):
        print(filename)
        if filename.endswith(".csv") and filename != "index.json":
            file_path = os.path.join(benchmark_path, filename)
            with open(file_path, newline='') as csvfile:
                    reader = csv.DictReader(csvfile)
                    rows = list(reader)
                    start = rows[0]['index'] if rows else None
                    end = rows[-1]['index'] if rows else None


            stats = os.stat(file_path)
            srv.benchmark.append({
                'name': filename[:-4],
                'start': start,
                'end': end,
                'des': filename
            })

    # Scrive i dati nel file index.json
    with open(json_file_path, 'w') as f:
        json.dump(srv.benchmark, f, indent=4)


def read_csv_to_json(filename):
    data = []
    with open(filename, newline='') as csvfile:
        reader = csv.DictReader(csvfile)
        for row in reader:
            data.append(row)
    return data

emitter = EventEmitter()

def copy_benchmark(data):
    print(f"Finito {data}")
    if data["stato"] == "Completato" and "benchmark" in data["args"]:
        print("EUREKKA")
        infile = os.path.join(OUT_PATH, "BuyAndHold", data["id"], "returns.csv")
        outfile = os.path.join(BENCHMARK_PATH, data["args"]["tickerList"]["value"].split('.')[0]+".csv")
        print(outfile)
        shutil.copyfile(infile,outfile)
        create_index_file(BENCHMARK_PATH, BENCHMARK_FILE)


emitter.on(emitter.EV_UPDATED_RUNS, copy_benchmark)

