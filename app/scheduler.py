from flask import Blueprint, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime
import app.service.ticker_service as tk_srv
import json
from datetime import datetime, timedelta


# Crea un Blueprint per il controller dello scheduler
sc_bp = Blueprint('scheduler', __name__)

# Inizializza lo scheduler
scheduler = BackgroundScheduler()
scheduler.start()

def some_job():
    print("Doing some job...")

# Schedula alcuni job di esempio
scheduler.add_job(some_job, 'interval', seconds=30, id='job1')
scheduler.add_job(some_job, 'interval', minutes=1, id='job2')
scheduler.add_job(tk_srv.read_ticker_csv_files, 'interval', hours=24, start_date=datetime.now() + timedelta(seconds=10), id='Tickers list')


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
    try:
        with open('jobs.json', 'r') as f:
            jobs_data = json.load(f)
        for job_data in jobs_data:
            module_name, func_name = job_data['func'].split(':')
            mod = __import__(module_name, fromlist=[func_name])
            func = getattr(mod, func_name)
            scheduler.add_job(func, trigger=eval(job_data['trigger']), id=job_data['id'],
                              next_run_time=job_data['next_run_time'])
    except FileNotFoundError:
        print("No jobs to load.")



# Definisci la route per ottenere l'elenco dei job
@sc_bp.route('/jobs')
def list_jobs():
    jobs = scheduler.get_jobs()
    jobs_list = []
    for job in jobs:
        job_info = {
            'id': job.id,
            'next_run_time': job.next_run_time.strftime('%Y-%m-%d %H:%M:%S') if job.next_run_time else 'None',
            'trigger': str(job.trigger),
            'function': str(job.func.__name__ ),
            'args ': str(job.args)
        }
        jobs_list.append(job_info)
    
    return jsonify(jobs_list)

@sc_bp.route('/status')
def scheduler_status():
    if scheduler.running:
        return jsonify({'status': 'running'})
    else:
        return jsonify({'status': 'stopped'})
    
@sc_bp.route('/stop', methods=['POST'])
def stop_scheduler():
    #scheduler.shutdown(wait=False)
    scheduler.pause()
    return jsonify({'message': 'Scheduler stopped successfully'})

@sc_bp.route('/start', methods=['POST'])
def start_scheduler():
    if not scheduler.running:
        scheduler.start()
        return jsonify({'message': 'Scheduler started successfully'})
    return jsonify({'message': 'Scheduler is already running'})

@sc_bp.route('/pause_job/<job_id>', methods=['POST'])
def pause_job(job_id):
    job = scheduler.get_job(job_id)
    if job:
        job.pause()
        return jsonify({'message': f'Job {job_id} paused successfully'})
    return jsonify({'message': 'Job not found'}), 404


@sc_bp.route('/resume_job/<job_id>', methods=['POST'])
def resume_job(job_id):
    job = scheduler.get_job(job_id)
    if job:
        job.resume()
        return jsonify({'message': f'Job {job_id} resumed successfully'})
    return jsonify({'message': 'Job not found'}), 404

