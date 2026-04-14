from dataclasses import asdict
from datetime import datetime
import json
import os
from threading import Thread
import logging

from bt_api import BacktestConfig, run_backtest
from watchtower_runtime import WatchtowerRepository, build_outpath, evaluate_outcomes
from app.service.EventEmitter import EventEmitter
from app.paths import DATA_PATH, OUT_PATH


emitter = EventEmitter()
logger = logging.getLogger(__name__)
repo = WatchtowerRepository()


_VALID_MODES = {"backtest", "paper", "shadow", "live"}
_VALID_COMMISSIONS = {"none", "fineco", "lfineco"}
_VALID_PROVIDERS = {"yahoo", "alpaca"}


def _choice_value(value, default=None):
    if isinstance(value, dict):
        return value.get("value", default)
    return value if value is not None else default


def _normalize_ticker_list(raw_ticker):
    ticker = (raw_ticker or "").strip()
    if not ticker:
        raise ValueError("tickerList mancante")
    if ticker.endswith(".json"):
        ticker = ticker[:-5]
    return ticker


def _map_broker_to_mode(raw_broker):
    broker = (raw_broker or "").strip().lower()
    if not broker:
        return "backtest"
    mapping = {
        "backtest": "backtest",
        "shadow": "shadow",
        "paper": "paper",
        "live": "live",
        "alpaca-paper": "paper",
        "alpaca-live": "live",
        "default": "backtest",
    }
    return mapping.get(broker, "backtest")


def json2config(operation_id, data):
    """
    Converte il payload frontend in BacktestConfig validato.
    """
    if not isinstance(data, dict):
        raise ValueError("Payload request non valido")

    strategy = _choice_value(data.get("strategia"))
    if not strategy:
        raise ValueError("strategia mancante")

    ticker_base = _normalize_ticker_list(_choice_value(data.get("tickerList")))
    ticker_json = f"{ticker_base}.json"

    commission = (_choice_value(data.get("tipoCommissioni"), "fineco") or "fineco").strip().lower()
    if commission not in _VALID_COMMISSIONS:
        raise ValueError(f"Commission non valida: {commission}")

    provider = (_choice_value(data.get("provider"), "yahoo") or "yahoo").strip().lower()
    if provider not in _VALID_PROVIDERS:
        raise ValueError(f"Provider non valido: {provider}")

    mode = _map_broker_to_mode(_choice_value(data.get("broker")))
    if mode not in _VALID_MODES:
        raise ValueError(f"Mode non valido: {mode}")

    fromdate = data.get("da", "2010-01-01")
    todate = data.get("a")
    amount = str(data.get("importoOperazioni", "10000"))
    cash = str(data.get("cash", "200000"))
    stratargs = str(data.get("parametriStrategia", "") or "")
    timeframe = _choice_value(data.get("timeframe"))
    benchmark = str(data.get("benchmark") or ticker_base)
    debug = bool(data.get("debug", False))

    return BacktestConfig(
        id=str(operation_id),
        strat=str(strategy),
        ticker=ticker_json,
        benchmark=benchmark,
        fromdate=str(fromdate),
        todate=str(todate) if todate else None,
        amount=amount,
        cash=cash,
        stratargs=stratargs,
        provider=provider,
        mode=mode,
        commission=commission,
        timeframe=str(timeframe) if timeframe else None,
        debug=debug,
    )


def _coerce_run_config(run_config):
    if isinstance(run_config, BacktestConfig):
        return run_config
    if isinstance(run_config, dict):
        return BacktestConfig(**run_config)
    raise ValueError(f"Configurazione run non supportata: {type(run_config).__name__}")


def _run_outpath(run_config):
    return build_outpath(OUT_PATH, run_config.strat, run_config.id)


def _strategy_fingerprint(data, run_config):
    if isinstance(data, dict):
        explicit = data.get("strategy_fingerprint") or data.get("statVersion")
        if explicit:
            return str(explicit)
        if isinstance(data.get("args"), dict):
            explicit = data["args"].get("strategy_fingerprint") or data["args"].get("statVersion")
            if explicit:
                return str(explicit)
    return "v1"


def _persist_run_state(data, run_config, status=None):
    if not repo.available():
        return
    payload = {
        "id": data["id"],
        "args": data.get("args", {}),
        "metadata": {
            "errorMessage": data.get("errorMessage"),
            "descizione": data.get("descizione"),
        },
        "status": status or data.get("stato"),
        "started_at": datetime.fromtimestamp(data["start"] / 1000) if data.get("start") else None,
        "finished_at": datetime.fromtimestamp(data["end"] / 1000) if data.get("end") else None,
        "pinned": bool(data.get("pinned")),
        "outpath": _run_outpath(run_config),
        "strategy_fingerprint": _strategy_fingerprint(data, run_config),
    }
    try:
        repo.upsert_run(payload)
    except Exception:
        logger.exception("Errore nel persistere lo stato run %s", data["id"])


def _ingest_completed_run(data, run_config):
    if not repo.available():
        return
    outpath = _run_outpath(run_config)
    try:
        repo.ingest_run_directory(data["id"], outpath)
        baseline = repo.baseline_for_run(data["id"])
        current_outcomes = repo.load_trade_outcomes(data["id"])
        pnl_values = [float(row["pnl_percent"]) for row in current_outcomes if row.get("pnl_percent") is not None]
        check = evaluate_outcomes(pnl_values, baseline.get("metrics") if baseline else None)
        repo.persist_stat_check(
            data["id"],
            check,
            source_meta={
                "baseline_found": bool(baseline),
                "outpath": outpath,
                "trade_count": len(pnl_values),
            },
        )
    except Exception:
        logger.exception("Errore nell'ingest run DB/stat check %s", data["id"])


def runstrat(run_config):
    """
    Wrapper usato anche dallo scheduler.
    """
    config = _coerce_run_config(run_config)
    return run_backtest(config)


def runstrat_background(data, run_config=None):
    operation_id = data["id"]

    if run_config is None:
        run_config = json2config(operation_id, data)
    else:
        run_config = _coerce_run_config(run_config)

    tdata = {}
    tdata["id"] = operation_id
    tdata["args"] = data
    tdata["args"]["run_config"] = asdict(run_config)
    tdata["stato"] = "In esecuzione"
    tdata["pinned"] = False
    tdata["descizione"] = ""
    _persist_run_state(tdata, run_config, status=tdata["stato"])

    thread = Thread(target=btrunstrat, args=(tdata, run_config))
    thread.start()
    logger.debug("Emetto segnale")
    emitter.emit(emitter.EV_UPDATED_RUNS, tdata)
    return tdata


def btrunstrat(data, run_config):
    """Funzione wrapper per eseguire runstrat in un thread separato e tenere traccia dello stato."""
    try:
        data["start"] = int(datetime.now().timestamp() * 1000)
        _persist_run_state(data, run_config, status="In esecuzione")
        runstrat(run_config)
    except Exception as e:
        logger.exception("Errore nell'eseguire la strategia")
        data["stato"] = "Errore"
        data["errorMessage"] = f"{e}"
        data["end"] = int(datetime.now().timestamp() * 1000)
        _persist_run_state(data, run_config, status=data["stato"])
        emitter.emit(emitter.EV_UPDATED_RUNS, data)
    else:
        data["stato"] = "Completato"
        data["end"] = int(datetime.now().timestamp() * 1000)
        _persist_run_state(data, run_config, status=data["stato"])
        _ingest_completed_run(data, run_config)
        emitter.emit(emitter.EV_UPDATED_RUNS, data)
        logger.debug("Fine elaborazione")


# Struttura dati per tenere traccia delle chiamate attive
runs = {}


def load_data():
    """Load configuration from JSON files in DATA_PATH"""
    loaded_count = 0
    failed_files = []

    for filename in os.listdir(DATA_PATH):
        if not filename.endswith('.json'):
            continue

        file_path = os.path.join(DATA_PATH, filename)

        try:
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
            logger.exception(f"Unexpected error loading {filename}: {e}")
            raise

    logger.info(f"Loaded {loaded_count} JSON files from {DATA_PATH}")

    if failed_files:
        logger.warning(f"Failed to load {len(failed_files)} files: {', '.join(failed_files)}")

    if loaded_count == 0 and len([f for f in os.listdir(DATA_PATH) if f.endswith('.json')]) > 0:
        raise RuntimeError(f"No valid JSON files could be loaded from {DATA_PATH}")

    if repo.available():
        try:
            for row in repo.fetch_runs(include_deleted=False, limit=500):
                runs[row["run_id"]] = row
        except Exception:
            logger.exception("Errore nel caricare run persistite dal DB")


def save_data(data):
    if data["id"] not in runs and "stato" in data:
        runs[data["id"]] = data
    else:
        runs[data["id"]] = data

    filtered_runs = {key: run for key, run in runs.items() if run.get('pinned')}

    for _, run in filtered_runs.items():
        run_id = run.get("id")
        file = os.path.join(DATA_PATH, f'{run_id}.json')
        with open(file, 'w') as f:
            json.dump(filtered_runs, f)

    run_config = None
    if isinstance(data.get("args"), dict):
        raw_config = data["args"].get("run_config")
        if isinstance(raw_config, dict):
            try:
                run_config = BacktestConfig(**raw_config)
            except Exception:
                run_config = None
    if run_config is not None:
        _persist_run_state(data, run_config)


emitter.on(emitter.EV_UPDATED_RUNS, save_data)
