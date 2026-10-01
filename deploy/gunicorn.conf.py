"""Gunicorn configuration for the initial 1 vCPU / 1 GB VPS (see docs/DEPLOYMENT.md).

One sync worker, one thread: requests are handled strictly one at a time per
process, which avoids the GIL contention measured with threaded servers. Do not
raise workers/threads without re-measuring on the real VPS.
"""

from pathlib import Path

chdir = str(Path(__file__).resolve().parent.parent)  # repository root, importable as web.wsgi
wsgi_app = "web.wsgi:app"

# Loopback only: Nginx is the single trusted proxy (see web.production.trusted_proxy).
bind = "127.0.0.1:8000"
forwarded_allow_ips = "127.0.0.1"

workers = 1
worker_class = "sync"
threads = 1
timeout = 30
graceful_timeout = 30
keepalive = 2

# Validate configuration and import the app in the master: a missing secret or
# a pending migration stops startup with one clear error. Deploy code changes
# with `systemctl restart` (a HUP reload would keep the preloaded code).
preload_app = True

# systemd supervises the process; never daemonize or auto-reload in production.
daemon = False
reload = False

# stdout/stderr -> journald. capture_output also routes print() from the app.
accesslog = "-"
errorlog = "-"
loglevel = "info"
capture_output = True
# %(h)s would always be Nginx (127.0.0.1); log the client address Nginx forwards.
access_log_format = '%({x-forwarded-for}i)s "%(r)s" %(s)s %(b)s %(L)ss'
proc_name = "learnpilot"
