import os

bind = os.getenv('GUNICORN_BIND', f"0.0.0.0:{os.getenv('SERVER_PORT', '9090')}")
workers = int(os.getenv('GUNICORN_WORKERS', '2'))
threads = int(os.getenv('GUNICORN_THREADS', '4'))
timeout = int(os.getenv('GUNICORN_TIMEOUT', '60'))
graceful_timeout = int(os.getenv('GUNICORN_GRACEFUL_TIMEOUT', '30'))
keepalive = int(os.getenv('GUNICORN_KEEPALIVE', '5'))
# Safety net, not a fix: recycle each worker after N requests (+/- jitter, to
# avoid all workers restarting in lockstep) so a single request that leaks or
# spikes memory can't accumulate across the worker's whole lifetime. This
# does NOT replace fixing an actual unbounded query/loop -- it only bounds
# the blast radius if one slips through.
max_requests = int(os.getenv('GUNICORN_MAX_REQUESTS', '500'))
max_requests_jitter = int(os.getenv('GUNICORN_MAX_REQUESTS_JITTER', '50'))
accesslog = os.getenv('GUNICORN_ACCESSLOG', '-')
errorlog = os.getenv('GUNICORN_ERRORLOG', '-')
loglevel = os.getenv('GUNICORN_LOGLEVEL', 'info')
reload = os.getenv('GUNICORN_RELOAD', 'false').lower() in ('1', 'true', 'yes', 'on')
