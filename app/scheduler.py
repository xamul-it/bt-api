from flask import Blueprint, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime

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

# Definisci la route per ottenere l'elenco dei job
@scheduler_controller.route('/jobs')
def list_jobs():
    jobs = scheduler.get_jobs()
    jobs_list = []
    for job in jobs:
        job_info = {
            'id': job.id,
            'next_run_time': job.next_run_time.strftime('%Y-%m-%d %H:%M:%S') if job.next_run_time else 'None',
            'trigger': str(job.trigger)
        }
        jobs_list.append(job_info)
    
    return jsonify(jobs_list)
