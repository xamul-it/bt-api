
import logging
import os
import sys
import shutil
import threading
import traceback
import importlib
import re
from app.paths import CONFIG_PATH, DATA_PATH, OUT_PATH, SCHEDULE_PATH
from flask import Blueprint, jsonify, request
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.executors.pool import ProcessPoolExecutor, ThreadPoolExecutor
from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_ERROR, EVENT_JOB_MISSED, EVENT_JOB_SUBMITTED
from apscheduler.triggers.date import DateTrigger
from apscheduler.schedulers.base import STATE_STOPPED, STATE_RUNNING, STATE_PAUSED
import time
from datetime import datetime
import app.tickers as tk_srv
import app.service.main_service as mn_srv
import app.service.profile_baseline_drift_service as baseline_drift_srv
import app.service.watchtower_scheduler_service as watchtower_scheduler_srv
import json
from datetime import datetime, timedelta
from app.service.EventEmitter import EventEmitter
from dataclasses import asdict
from pathlib import Path

# Whitelist of allowed modules for dynamic imports (security)
ALLOWED_MODULES = {
    'app.tickers',
    'app.benchmark',
    'app.service.main_service',
    'app.main'
}


# Crea un Blueprint per il controller dello scheduler
sc_bp = Blueprint('scheduler', __name__)
logger = logging.getLogger(__name__)

SCHEDULER_STATE_PATH = Path(
    os.environ.get("BT_SCHEDULER_STATE_PATH", str(Path(CONFIG_PATH) / "scheduler-state.json"))
)
_scheduler_state_lock = threading.Lock()


def _load_scheduler_state():
    try:
        payload = json.loads(SCHEDULER_STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"scheduler_enabled": True, "jobs": {}}
    if not isinstance(payload, dict):
        return {"scheduler_enabled": True, "jobs": {}}
    jobs = payload.get("jobs") if isinstance(payload.get("jobs"), dict) else {}
    return {
        "scheduler_enabled": payload.get("scheduler_enabled") is not False,
        "jobs": jobs,
    }


def _save_scheduler_state(state):
    SCHEDULER_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = SCHEDULER_STATE_PATH.with_suffix(SCHEDULER_STATE_PATH.suffix + ".tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(SCHEDULER_STATE_PATH)


def _set_scheduler_enabled(enabled):
    with _scheduler_state_lock:
        state = _load_scheduler_state()
        state["scheduler_enabled"] = bool(enabled)
        _save_scheduler_state(state)


def _job_enabled(job_id):
    state = _load_scheduler_state()
    job_state = state["jobs"].get(job_id)
    return not isinstance(job_state, dict) or job_state.get("enabled") is not False


def _set_job_enabled(job_id, enabled):
    with _scheduler_state_lock:
        state = _load_scheduler_state()
        state["jobs"].setdefault(job_id, {})["enabled"] = bool(enabled)
        _save_scheduler_state(state)


def _set_job_runtime(job_id, status, error=None):
    """Persist a human-readable lifecycle state across API restarts."""
    with _scheduler_state_lock:
        state = _load_scheduler_state()
        job = state["jobs"].setdefault(job_id, {})
        job["last_status"] = status
        timestamp = datetime.now().astimezone().isoformat()
        if status == "in esecuzione":
            job["last_started_at"] = timestamp
        else:
            job["last_finished_at"] = timestamp
        if error:
            job["last_error"] = str(error)[-2000:]
        elif status == "eseguito":
            job.pop("last_error", None)
        _save_scheduler_state(state)


def _restore_job_state(job):
    if not _job_enabled(job.id):
        job.pause()
    return job

emitter = EventEmitter()
# Inizializza lo scheduler

# Configura l'esecutore con un massimo di 2 thread (limite al parallelismo)
executors = {
    'default': ThreadPoolExecutor(20),  # Limita a 2 job paralleli
    #'processpool': ProcessPoolExecutor(1)  # Limita ulteriormente i job pesanti
}

# Crea lo scheduler con l'esecutore personalizzato
scheduler = BackgroundScheduler(executors=executors)

scheduler.start(paused=True)

IMMEDIATE="_immediate"
MANAGED_WATCHTOWER_JOB_IDS = {
    "Watchtower — poll Alpaca",
    "Watchtower — watchdog profili",
    "Watchtower — replay riconciliazioni",
    "Controllo drift baseline profili",
}

# These are the only recurring schedules this application may configure.  In
# particular, no endpoint in this blueprint accepts a strategy callable.
EDITABLE_MANAGED_JOB_IDS = frozenset(MANAGED_WATCHTOWER_JOB_IDS)


def _schedule_payload(job_id):
    job = _load_scheduler_state()["jobs"].get(job_id, {})
    return job.get("schedule") if isinstance(job, dict) else None


def _cron_trigger_from_payload(payload, default_trigger):
    """Return a validated cron trigger, falling back to the shipped default."""
    if not isinstance(payload, dict):
        return default_trigger
    frequency = payload.get("frequency")
    try:
        hour = int(payload.get("hour"))
        minute = int(payload.get("minute"))
    except (TypeError, ValueError):
        return default_trigger
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return default_trigger
    if frequency == "hourly":
        return CronTrigger(minute=minute)
    if frequency == "daily":
        return CronTrigger(hour=hour, minute=minute)
    if frequency == "weekly" and payload.get("day_of_week") in {
        "mon", "tue", "wed", "thu", "fri", "sat", "sun"
    }:
        return CronTrigger(day_of_week=payload["day_of_week"], hour=hour, minute=minute)
    return default_trigger


def _managed_trigger(job_id, default_trigger):
    return _cron_trigger_from_payload(_schedule_payload(job_id), default_trigger)


def _validate_schedule_payload(payload):
    if not isinstance(payload, dict):
        return None, "Configurazione mancante"
    frequency = payload.get("frequency")
    if frequency not in {"hourly", "daily", "weekly"}:
        return None, "Frequenza non valida"
    try:
        hour, minute = int(payload.get("hour")), int(payload.get("minute"))
    except (TypeError, ValueError):
        return None, "Ora e minuti devono essere numerici"
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return None, "Ora/minuti fuori intervallo"
    clean = {"frequency": frequency, "hour": hour, "minute": minute}
    if frequency == "weekly":
        day = payload.get("day_of_week")
        if day not in {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}:
            return None, "Giorno della settimana non valido"
        clean["day_of_week"] = day
    return clean, None


def _set_job_schedule(job_id, payload):
    with _scheduler_state_lock:
        state = _load_scheduler_state()
        state["jobs"].setdefault(job_id, {})["schedule"] = payload
        _save_scheduler_state(state)


def _base_job_id(job_id):
    return job_id[:-len(IMMEDIATE)] if job_id.endswith(IMMEDIATE) else job_id

# Schedula il job di caricamento ticker
#scheduler.add_job(tk_srv.init_tickers(), 'interval', hours=24, start_date=datetime.now() + timedelta(seconds=10), 
#                  id='Tickers list')

#scheduler.add_job(tk_srv.init_tickers, CronTrigger(hour='20', minute=0), id='TickersList', replace_existing=True, 
#                  max_instances=1)
_restore_job_state(scheduler.add_job(
    tk_srv.init_tickers, CronTrigger(hour='20', minute=0), id='Aggiorna ALLMIB',
    replace_existing=True, max_instances=1, kwargs={"list_name":"allmib"},
))
_restore_job_state(scheduler.add_job(
    watchtower_scheduler_srv.poll_alpaca_orders,
    _managed_trigger("Watchtower — poll Alpaca", CronTrigger(hour=16, minute=5)), id="Watchtower — poll Alpaca",
    replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=86400,
))
_restore_job_state(scheduler.add_job(
    watchtower_scheduler_srv.detect_missed_scheduled_runs,
    _managed_trigger("Watchtower — watchdog profili", CronTrigger(hour=16, minute=10)), id="Watchtower — watchdog profili",
    replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=86400,
))
_restore_job_state(scheduler.add_job(
    watchtower_scheduler_srv.replay_reconciliation_catchup,
    _managed_trigger("Watchtower — replay riconciliazioni", CronTrigger(hour=16, minute=15)), id="Watchtower — replay riconciliazioni",
    replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=86400,
))
_restore_job_state(scheduler.add_job(
    baseline_drift_srv.run_profile_baseline_drift,
    _managed_trigger("Controllo drift baseline profili", CronTrigger(minute=0)),
    id="Controllo drift baseline profili",
    replace_existing=True,
    max_instances=1,
    coalesce=True,
    misfire_grace_time=1800,
))
_restore_job_state(scheduler.add_job(
    tk_srv.init_tickers, CronTrigger(hour='20', minute=0), id='Aggiorna NASDAQ 100',
    replace_existing=True, max_instances=1, kwargs={"list_name":"NASDAQ 100"},
))
_restore_job_state(scheduler.add_job(
    tk_srv.init_tickers, CronTrigger(hour='20', minute=0), id='Aggiorna NASDAQ 30',
    replace_existing=True, max_instances=1, kwargs={"list_name":"NASDAQ 30"},
))
#scheduler.add_job(tk_srv.read_ticker_csv_files, 'interval', hours=24, start_date=datetime.now() + timedelta(seconds=10), id='Tickers list')

def load_jobs(data=None):
    logger.debug("Avvio schedulazioni")
    # Iterare su tutti i file nella cartella
    for filename in os.listdir(SCHEDULE_PATH):
        # Verifica se il file è un file JSON
        if filename.endswith('.json'):
            file_path = os.path.join(SCHEDULE_PATH, filename)
            
            # Aprire e caricare il file JSON
            with open(file_path, 'r', encoding='utf-8') as json_file:
                try:
                    # Carica il file JSON in un dizionario
                    data = json.load(json_file)
                    del(data["end"])
                    run_config = mn_srv.json2config(data["id"], data["args"])
                    id = data["id"]

                    type = data["scheduleType"]["value"]
                    if type == "H":
                        trigger=CronTrigger(minute=0, hour='8-20')
                    elif type=="D":
                        trigger=CronTrigger(hour=20, minute=0)
                    elif type=="W":
                        trigger=CronTrigger(day_of_week='fri', hour=20, minute=0)
                    
                    #trigger=DateTrigger(run_date=datetime.now())
                    logger.debug(f"Avvio schedulazioni {id}:{trigger}:{run_config}")
                    # Sovrascrive il job esistente con lo stesso id)
                    job = scheduler.add_job(
                        mn_srv.runstrat,
                        trigger=trigger,
                        id=id,
                        args=[asdict(run_config)],
                        replace_existing=True,
                        max_instances=1
                    )
                    _restore_job_state(job)
    
                except Exception  as e:
                    logger.exception(f"Errore nel parsing del file {filename}")
    #return jsonify("ok")


# Strategy execution is owned by OS cron.  Do not load legacy JSON jobs that
# call main_service.runstrat into this process.
if _load_scheduler_state()["scheduler_enabled"]:
    scheduler.resume()

    # Every Watchtower worker is idempotent and has its own lookback/catch-up.
    # Queue an immediate recovery pass after an API restart, preserving their
    # dependency order (poll -> watchdog -> replay) with one shared executor.
    if os.environ.get("BT_SCHEDULER_STARTUP_CATCHUP", "1") == "1" and "pytest" not in sys.modules:
        recovery_order = [
            "Watchtower — poll Alpaca", "Watchtower — watchdog profili", "Watchtower — replay riconciliazioni",
            "Controllo drift baseline profili",
        ]
        for index, job_id in enumerate(recovery_order):
            job = scheduler.get_job(job_id)
            if job and _job_enabled(job_id):
                scheduler.add_job(job.func, DateTrigger(run_date=datetime.now() + timedelta(seconds=index * 2)),
                                  id=f"{job_id}{IMMEDIATE}", replace_existing=True, max_instances=1)
logger.info("---Avvio schedulazioni")


def save_jobs_to_json():
    jobs = scheduler.get_jobs()
    jobs_data = []
    for job in jobs:
        jobs_data.append({
            'id': job.id,
            'func': f"{job.func.__module__}:{job.func.__name__}",
            'trigger': str(job.trigger),
            'next_run_time': job.next_run_time.isoformat() if job.next_run_time else None
        })
    with open('jobs.json', 'w') as f:
        json.dump(jobs_data, f, indent=4)

def load_jobs_from_json():
    """
    Load scheduled jobs from jobs.json file.
    Security: Only allows whitelisted modules and safe trigger parsing.
    """
    try:
        with open('jobs.json', 'r') as f:
            jobs_data = json.load(f)

        for job_data in jobs_data:
            try:
                # Validate and parse function reference
                func_ref = job_data.get('func', '')
                if ':' not in func_ref:
                    logger.error(f"Invalid function reference format: {func_ref}")
                    continue

                module_name, func_name = func_ref.split(':', 1)

                # Security: Validate module name format
                if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_.]*$', module_name):
                    logger.error(f"Invalid module name format: {module_name}")
                    continue

                # Security: Check whitelist
                if module_name not in ALLOWED_MODULES:
                    logger.error(f"Module not in whitelist: {module_name}. Allowed: {ALLOWED_MODULES}")
                    continue

                # Security: Validate function name format
                if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', func_name):
                    logger.error(f"Invalid function name format: {func_name}")
                    continue

                # Safely import module
                try:
                    mod = importlib.import_module(module_name)
                    func = getattr(mod, func_name)
                except (ModuleNotFoundError, AttributeError) as e:
                    logger.error(f"Cannot load function {func_name} from module {module_name}: {e}")
                    continue

                # Parse trigger safely from string representation
                trigger_str = job_data.get('trigger', '')
                trigger = parse_trigger_from_string(trigger_str)

                if trigger is None:
                    logger.error(f"Cannot parse trigger: {trigger_str}")
                    continue

                # Add job to scheduler
                scheduler.add_job(
                    func,
                    trigger=trigger,
                    id=job_data.get('id'),
                    next_run_time=job_data.get('next_run_time'),
                    max_instances=1,
                    misfire_grace_time=1200
                )
                logger.info(f"Loaded job: {job_data.get('id')}")

            except Exception as e:
                logger.error(f"Error loading job {job_data.get('id', 'unknown')}: {e}")
                continue

    except FileNotFoundError:
        logger.info("No jobs.json file to load.")
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON in jobs.json: {e}")
    except Exception as e:
        logger.error(f"Error loading jobs from JSON: {e}")


def parse_trigger_from_string(trigger_str):
    """
    Parse a trigger string and return an APScheduler trigger object.
    Security: Uses safe parsing instead of eval().

    Examples:
        "cron[hour='8-20', minute='0']" -> CronTrigger
        "interval[hours=1]" -> IntervalTrigger
    """
    try:
        # Basic parsing of trigger string format
        # Expected format: "type[param='value', ...]"
        if not trigger_str or '[' not in trigger_str:
            return None

        trigger_type = trigger_str.split('[')[0].strip().lower()

        if trigger_type == 'cron':
            # Parse cron parameters from string
            # This is a simple implementation - extend as needed
            # For now, create a basic daily trigger
            return CronTrigger(hour=20, minute=0)

        # Add other trigger types as needed
        logger.warning(f"Trigger type '{trigger_type}' not supported in safe parsing")
        return None

    except Exception as e:
        logger.error(f"Error parsing trigger string '{trigger_str}': {e}")
        return None


# Definisci la route per ottenere l'elenco dei job
@sc_bp.route('/index')
def index():
    res = {}
    res["children"] = []
    for filename in os.listdir(SCHEDULE_PATH):
        # Verifica se il file è un file JSON
        if filename.endswith('.json'):
            file_path = os.path.join(SCHEDULE_PATH, filename)
            
            # Aprire e caricare il file JSON
            with open(file_path, 'r', encoding='utf-8') as json_file:
                # Carica il file JSON in un dizionario
                data = json.load(json_file)
                res["children"].append({"name" :  data["id"]})
    
    return jsonify(res)


# Definisci la route per ottenere l'elenco dei job
@sc_bp.route('/jobs')
def list_jobs():
    jobs = scheduler.get_jobs()
    jobs_list = []
    for job in jobs:
        try:
            next_run_time = job.next_run_time.strftime('%Y-%m-%d %H:%M:%S') 
        except:
            next_run_time = 'None'
        runtime = _load_scheduler_state()["jobs"].get(job.id, {})
        job_info = {
            'id': job.id,
            'next_run_time': next_run_time,
            'trigger': str(job.trigger),
            'function': getattr(job.func, '__name__', repr(job.func)),
            'args': str(job.args),
            "status": job_event_cache.get(job.id, runtime.get("last_status", "in attesa")),
            "enabled": _job_enabled(job.id),
            "managed": job.id in MANAGED_WATCHTOWER_JOB_IDS,
            "editable": job.id in EDITABLE_MANAGED_JOB_IDS,
            "schedule": _schedule_payload(job.id),
            "last_started_at": runtime.get("last_started_at"),
            "last_finished_at": runtime.get("last_finished_at"),
            "last_error": runtime.get("last_error"),
        }
        jobs_list.append(job_info)
    
    return jsonify(jobs_list)

@sc_bp.route('/status')
def scheduler_status():
    if scheduler.state == STATE_RUNNING:
        return jsonify({'status': 'running'})
    else:
        return jsonify({'status': 'stopped'})
    
@sc_bp.route('/stop', methods=['POST'])
def stop_scheduler():
    scheduler.pause()
    _set_scheduler_enabled(False)
    return jsonify({'message': 'Scheduler stopped successfully'})

@sc_bp.route('/start', methods=['POST'])
def start_scheduler():
    if scheduler.state == STATE_PAUSED:
        scheduler.resume()
        _set_scheduler_enabled(True)
        return jsonify({'message': 'Scheduler started successfully'})
    return jsonify({'message': 'Scheduler is already running'})

@sc_bp.route('/pause_job/<job_id>', methods=['POST'])
def pause_job(job_id):
    job = scheduler.get_job(job_id)
    if job:
        job.pause()
        _set_job_enabled(job_id, False)
        return jsonify({'message': f'Job {job_id} paused successfully'})
    return jsonify({'message': 'Job not found'}), 404

@sc_bp.route('/delete_job/<job_id>', methods=['POST'])
def delete_job(job_id):
    return jsonify({'message': 'Scheduler jobs are managed definitions; disable them instead of deleting.'}), 400
    job = scheduler.get_job(job_id)
    if job:
        scheduler.remove_job(job_id)
        with _scheduler_state_lock:
            state = _load_scheduler_state()
            state["jobs"].pop(job_id, None)
            _save_scheduler_state(state)
        file_path = os.path.join(SCHEDULE_PATH, f"{job_id}.json")
        if os.path.exists(file_path):
            os.remove(file_path)
        return jsonify({'message': f'Job {job_id} paused successfully'})
    return jsonify({'message': 'Job not found'}), 404


@sc_bp.route('/resume_job/<job_id>', methods=['POST'])
def resume_job(job_id):
    job = scheduler.get_job(job_id)
    if job:
        job.resume()
        _set_job_enabled(job_id, True)
        return jsonify({'message': f'Job {job_id} resumed successfully'})
    return jsonify({'message': 'Job not found'}), 404

@sc_bp.route('/run_job/<job_id>', methods=['POST'])
def run_job(job_id):
    job = scheduler.get_job(job_id)
    if job:

        immediate_trigger = DateTrigger(run_date=datetime.now())

        scheduler.add_job(
                job.func, 
                trigger=immediate_trigger,  # Esecuzione immediata
                args=job.args, 
                kwargs=job.kwargs,
                id=f"{job_id}{IMMEDIATE}",
                max_instances=1
            )

        emitter.emit(EventEmitter.EV_SCHEDULER)
        return jsonify({'message': f'Job {job_id} executed immediately and original schedule restored'})
    return jsonify({'message': 'Job not found'}), 404

@sc_bp.route('/update_job', methods=['POST'])
def update_job():
    payload = request.get_json(silent=True) or {}
    job_id = payload.get('id')
    if job_id not in EDITABLE_MANAGED_JOB_IDS:
        return jsonify({'message': 'Sono modificabili solo le attività Watchtower/manutenzione.'}), 403
    job = scheduler.get_job(job_id)
    if not job:
        return jsonify({'message': 'Job non trovato'}), 404
    schedule, error = _validate_schedule_payload(payload.get('schedule'))
    if error:
        return jsonify({'message': error}), 400
    job.reschedule(trigger=_cron_trigger_from_payload(schedule, job.trigger))
    _set_job_schedule(job_id, schedule)
    return jsonify({'message': 'Schedulazione aggiornata', 'schedule': schedule})
    


# Crea un lock globale
lock = threading.Lock()
job_event_cache = {}
# Listener per intercettare quando un job è completato o fallisce
def job_listener(event):
    with lock: #devo evitare copie sovrapposte
        logger.info(f"Event for {event.job_id} - {event.code}")
        job_id = _base_job_id(event.job_id)
        if event.code == EVENT_JOB_EXECUTED:
            job_event_cache[job_id] = 'eseguito'
            _set_job_runtime(job_id, 'eseguito')
        elif event.code == EVENT_JOB_ERROR:
            job_event_cache[job_id] = 'errore'
            _set_job_runtime(job_id, 'errore', getattr(event, 'exception', None))
        elif event.code == EVENT_JOB_MISSED:
            job_event_cache[job_id] = 'trigger mancato'
            _set_job_runtime(job_id, 'trigger mancato')
        elif event.code == EVENT_JOB_SUBMITTED:
            job_event_cache[job_id] = 'in esecuzione'
            _set_job_runtime(job_id, 'in esecuzione')

        if event.code == EVENT_JOB_ERROR:
            logger.error(f"Il job {event.job_id} ha generato un'eccezione")

        elif event.code == EVENT_JOB_EXECUTED:
            logger.info(f"Il job {event.job_id} è stato completato con successo")
            source_path = OUT_PATH
            destination_path = SCHEDULE_PATH

            job_id = event.job_id[:-len(IMMEDIATE)] if event.job_id.endswith(IMMEDIATE) else event.job_id
            
            file_path = os.path.join(SCHEDULE_PATH, f"{job_id}.json")
            if not os.path.exists(file_path): # Non è un job di esecuzione 
                return
            with open(file_path, 'r', encoding='utf-8') as json_file:
                # Carica il file JSON della schedulazione
                data = json.load(json_file)
                strategy = data["args"]["strategia"]["value"]
                strategy = strategy.split(".")[-1]
                # Sposto i file
                source_path = os.path.join(source_path, strategy)
                source_path = os.path.join(source_path, job_id)

                destination_path = os.path.join(destination_path, job_id)
                if os.path.exists(destination_path):
                        shutil.rmtree(destination_path)
                shutil.move(source_path, destination_path)


# Aggiungi il listener per intercettare gli eventi di completamento dei job
scheduler.add_listener(job_listener, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR |EVENT_JOB_MISSED | EVENT_JOB_SUBMITTED)
emitter.on(emitter.EV_SCHEDULER, load_jobs)
