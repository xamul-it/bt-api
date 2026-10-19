import os

bind = os.getenv('GUNICORN_BIND', f"0.0.0.0:{os.getenv('SERVER_PORT', '9090')}")
# The API embeds an in-process APScheduler. Multiple worker processes would
# each create an independent scheduler and duplicate every scheduled job.
# Keep one worker and use threads for concurrent HTTP requests.
workers = int(os.getenv('GUNICORN_WORKERS', '1'))
threads = int(os.getenv('GUNICORN_THREADS', '4'))
timeout = int(os.getenv('GUNICORN_TIMEOUT', '60'))
graceful_timeout = int(os.getenv('GUNICORN_GRACEFUL_TIMEOUT', '30'))
keepalive = int(os.getenv('GUNICORN_KEEPALIVE', '5'))
# This worker owns APScheduler and calculation children. Request-count
# recycling invokes shutdown and interrupts active backtests, so disable it
# by default. Memory limits remain enforced by the service manager.
max_requests = int(os.getenv('GUNICORN_MAX_REQUESTS', '0'))
max_requests_jitter = int(os.getenv('GUNICORN_MAX_REQUESTS_JITTER', '50'))
accesslog = os.getenv('GUNICORN_ACCESSLOG', '-')
errorlog = os.getenv('GUNICORN_ERRORLOG', '-')
loglevel = os.getenv('GUNICORN_LOGLEVEL', 'info')
reload = os.getenv('GUNICORN_RELOAD', 'false').lower() in ('1', 'true', 'yes', 'on')
