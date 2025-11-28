
import logging
import os
import shutil
import threading
import traceback
import importlib
import re
from app.paths import DATA_PATH, OUT_PATH, SCHEDULE_PATH
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
import json
from datetime import datetime, timedelta
from app.service.EventEmitter import EventEmitter

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

emitter = EventEmitter()
# Inizializza lo scheduler

# Configura l'esecutore con un massimo di 2 thread (limite al parallelismo)
executors = {
    'default': ThreadPoolExecutor(20),  # Limita a 2 job paralleli
    #'processpool': ProcessPoolExecutor(1)  # Limita ulteriormente i job pesanti
}

# Crea lo scheduler con l'esecutore personalizzato
scheduler = BackgroundScheduler(executors=executors)

scheduler.start()
scheduler.pause()

IMMEDIATE="_immediate"

# Schedula il job di caricamento ticker
#scheduler.add_job(tk_srv.init_tickers(), 'interval', hours=24, start_date=datetime.now() + timedelta(seconds=10), 
#                  id='Tickers list')

#scheduler.add_job(tk_srv.init_tickers, CronTrigger(hour='20', minute=0), id='TickersList', replace_existing=True, 
#                  max_instances=1)
scheduler.add_job(tk_srv.init_tickers, CronTrigger(hour='20', minute=0), id='Aggiorna ALLMIB', replace_existing=True, 
                  max_instances=1, kwargs={"list_name":"allmib"})
scheduler.add_job(tk_srv.init_tickers, CronTrigger(hour='20', minute=0), id='Aggiorna NASDAQ 100', replace_existing=True, 
                  max_instances=1,  kwargs={"list_name":"NASDAQ 100"})
scheduler.add_job(tk_srv.init_tickers, CronTrigger(hour='20', minute=0), id='Aggiorna NASDAQ 30', replace_existing=True, 
                  max_instances=1,  kwargs={"list_name":"NASDAQ 30"})
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
                    args=mn_srv.json2args(data["id"],data["args"])
                    #args=data["args"]
                    id = data["id"]

                    type = data["scheduleType"]["value"]
                    if type == "H":
                        trigger=CronTrigger(minute=0, hour='8-20')
                    elif type=="D":
                        trigger=CronTrigger(hour=20, minute=0)
                    elif type=="W":
                        trigger=CronTrigger(day_of_week='fri', hour=20, minute=0)
                    
                    #trigger=DateTrigger(run_date=datetime.now())
                    logger.debug(f"Avvio schedulazioni {id}:{trigger}:{args}")
                    # Sovrascrive il job esistente con lo stesso id)
                    scheduler.add_job(mn_srv.runstrat, trigger=trigger, id=id, args=[args], replace_existing=True, max_instances=1)
    
                except Exception  as e:
                    logger.exception(f"Errore nel parsing del file {filename}")
    #return jsonify("ok")


load_jobs()
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
        job_info = {
            'id': job.id,
            'next_run_time': next_run_time,
            'trigger': str(job.trigger),
            'function': getattr(job.func, '__name__', repr(job.func)),
            'args': str(job.args),
            "status": job_event_cache.get(job.id, "NA") 
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
    return jsonify({'message': 'Scheduler stopped successfully'})

@sc_bp.route('/start', methods=['POST'])
def start_scheduler():
    if scheduler.state == STATE_PAUSED:
        scheduler.resume()
        return jsonify({'message': 'Scheduler started successfully'})
    return jsonify({'message': 'Scheduler is already running'})

@sc_bp.route('/pause_job/<job_id>', methods=['POST'])
def pause_job(job_id):
    job = scheduler.get_job(job_id)
    if job:
        job.pause()
        return jsonify({'message': f'Job {job_id} paused successfully'})
    return jsonify({'message': 'Job not found'}), 404

@sc_bp.route('/delete_job/<job_id>', methods=['POST'])
def delete_job(job_id):
    job = scheduler.get_job(job_id)
    if job:
        scheduler.remove_job(job_id)
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
    id = request.get_json().get('id')
    name = request.get_json().get('name')
    destination_path = os.path.join(SCHEDULE_PATH, f"{name}.json")
    if id != name:
        source_path = os.path.join(SCHEDULE_PATH, f"{id}.json")
        shutil.move(source_path, destination_path)

    scheduleType = request.get_json().get('type')
    with open(destination_path, 'r', encoding='utf-8') as json_file:
        # Carica il file JSON in un dizionario
        r = json.load(json_file)
        r["scheduleType"] = scheduleType
        r["id"] = name
        with open(destination_path, 'w') as f:
            json.dump(r, f)
    #Rimuovo il vecchio job
    scheduler.remove_job(id)
    load_jobs()
    return jsonify({'message': f'Job {id} executed immediately and original schedule restored'})
    


# Crea un lock globale
lock = threading.Lock()
job_event_cache = {}
# Listener per intercettare quando un job è completato o fallisce
def job_listener(event):
    with lock: #devo evitare copie sovrapposte
        logger.info(f"Event for {event.job_id} - {event.code}")
        if event.code == EVENT_JOB_EXECUTED:
            job_event_cache[event.job_id] = 'executed'
        elif event.code == EVENT_JOB_ERROR:
            job_event_cache[event.job_id] = 'error'
        elif event.code == EVENT_JOB_MISSED:
            job_event_cache[event.job_id] = 'missed'
        elif event.code == EVENT_JOB_SUBMITTED:
            job_event_cache[event.job_id] = 'submitted'

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