# Percorso relativo al file JSON dei ticker
import os

# Ottenere il percorso del file corrente
current_file_path = os.path.abspath(__file__)

# Ottenere il percorso della directory del file corrente
current_directory = os.path.dirname(current_file_path)

# Root path sarà il percorso della directory superiore
root_path = os.path.abspath(os.path.join(current_directory, '..'))


TICKER_PATH = os.path.join(root_path,  'data')

TICKERLIST_PATH = os.path.join(root_path,  'tickers')

# Percorso relativo al file JSON
OUT_PATH = os.path.join(root_path,  'out')

# Percorso relativo al file JSON
CONFIG_PATH = os.path.join(root_path,  'config')

# Percorso relativo al file JSON
BENCHMARK_PATH = os.path.join(root_path,  'benchmark')

# Percorso relativo al file JSON
DATA_PATH = os.path.join(CONFIG_PATH,  'stored')

# Percorso relativo al file JSON
SCHEDULE_PATH = os.path.join(CONFIG_PATH,  'schedule')

# Percorso relativo al file JSON con le liste ticker
TICKER_LISTS_FILE = os.path.join(CONFIG_PATH, 'tickers.json')

# Percorso relativo al file JSON con i ticker
TICKER_FILE = os.path.join(CONFIG_PATH, 'ticker.json')

# Percorso relativo al file JSON
STRATEGIES_FILE = os.path.join(CONFIG_PATH, 'strategies.json')

# Percorso relativo al file JSON
BENCHMARK_FILE = os.path.join(CONFIG_PATH, 'benchmarks.json')


# Definisci una variabile globale per tenere traccia dello stato di importazione
CONFIG_IMPORTED = False

# Esegui l'azione solo al primo import
if not CONFIG_IMPORTED:

    # Imposta la variabile globale a True per indicare che il file è stato importato
    CONFIG_IMPORTED = True

    if not os.path.exists(DATA_PATH):
        os.makedirs(DATA_PATH)

    if not os.path.exists(SCHEDULE_PATH):
        os.makedirs(SCHEDULE_PATH)

    if not os.path.exists(TICKERLIST_PATH):
        os.makedirs(TICKERLIST_PATH)

    if not os.path.exists(BENCHMARK_PATH):
        os.makedirs(BENCHMARK_PATH)

    if not os.path.exists(OUT_PATH):
        os.makedirs(OUT_PATH)

    if not os.path.exists(TICKERLIST_PATH):
        os.makedirs(TICKERLIST_PATH)

    if not os.path.exists(CONFIG_PATH):
        os.makedirs(CONFIG_PATH)

    if not os.path.exists(TICKER_PATH):
        os.makedirs(TICKER_PATH)

