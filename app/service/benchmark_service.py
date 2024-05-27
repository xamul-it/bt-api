import os
import json
import app.service.ticker_service as srv
import csv

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
