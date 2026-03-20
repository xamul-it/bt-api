import os
from pathlib import Path


CURRENT_DIRECTORY = Path(__file__).resolve().parent
API_ROOT = CURRENT_DIRECTORY.parent
WORKSPACE_ROOT = API_ROOT.parent

# Shared config (common for core + api)
SHARED_CONFIG_PATH = Path(
    os.getenv('BT_SHARED_CONFIG', str(WORKSPACE_ROOT / 'config-common'))
).expanduser()

# API-specific runtime/config folder
CONFIG_PATH = Path(
    os.getenv('BT_API_CONFIG', str(API_ROOT / 'config'))
).expanduser()

TICKER_PATH = SHARED_CONFIG_PATH / 'data'
TICKERLIST_PATH = SHARED_CONFIG_PATH / 'tickers'
BENCHMARK_PATH = SHARED_CONFIG_PATH / 'benchmark'

OUT_PATH = API_ROOT / 'out'
DATA_PATH = CONFIG_PATH / 'stored'
SCHEDULE_PATH = CONFIG_PATH / 'schedule'

TICKER_LISTS_FILE = CONFIG_PATH / 'tickers.json'
TICKER_FILE = CONFIG_PATH / 'ticker.json'
STRATEGIES_FILE = CONFIG_PATH / 'strategies.json'
BENCHMARK_FILE = CONFIG_PATH / 'benchmarks.json'

# Keep str compatibility for legacy os.path usage across the app.
CONFIG_PATH = str(CONFIG_PATH)
TICKER_PATH = str(TICKER_PATH)
TICKERLIST_PATH = str(TICKERLIST_PATH)
OUT_PATH = str(OUT_PATH)
BENCHMARK_PATH = str(BENCHMARK_PATH)
DATA_PATH = str(DATA_PATH)
SCHEDULE_PATH = str(SCHEDULE_PATH)
TICKER_LISTS_FILE = str(TICKER_LISTS_FILE)
TICKER_FILE = str(TICKER_FILE)
STRATEGIES_FILE = str(STRATEGIES_FILE)
BENCHMARK_FILE = str(BENCHMARK_FILE)


CONFIG_IMPORTED = False

if not CONFIG_IMPORTED:
    CONFIG_IMPORTED = True

    for path in (
        DATA_PATH,
        SCHEDULE_PATH,
        TICKERLIST_PATH,
        BENCHMARK_PATH,
        OUT_PATH,
        CONFIG_PATH,
        TICKER_PATH,
    ):
        os.makedirs(path, exist_ok=True)
