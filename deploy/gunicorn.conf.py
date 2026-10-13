"""Gunicorn configuration for the initial 1 vCPU / 2 GB VPS (see docs/DEPLOYMENT.md).

One sync worker, one thread: requests are handled strictly one at a time per
process, which avoids the GIL contention measured with threaded servers. Do not
raise workers/threads without re-measuring on the real VPS.
"""

import logging
from pathlib import Path

from gunicorn.glogging import Logger

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
# Not "debug": at that level Gunicorn logs the unread buffer of a client that
# disconnects in the middle of a request, a cookie included, for every route.
loglevel = "info"
capture_output = True
# %(h)s would always be Nginx (127.0.0.1); log the client address Nginx forwards.
# Method, path and protocol instead of the request line (%(r)s): no query string
# reaches the access log. The OAuth callback carries a code and a state in it, and
# other routes a return target.
access_log_format = '%({x-forwarded-for}i)s "%(m)s %(U)s %(H)s" %(s)s %(b)s %(L)ss'
proc_name = "learnpilot"


class RedactingErrorLogFilter(logging.Filter):
    """Keep request text out of Gunicorn's error log (docs/DEPLOYMENT.md, section 29).

    Gunicorn's handler for a request that failed logs the request's URI, and for a
    request its own parser refused it logs the exception's text, which quotes what was
    refused: a request line, what follows a second space in it, or a header line. A
    callback address would leave its code and state there, a header line a cookie.

    * A message that begins with ``Invalid request from ip=`` is cut at its first
      quotation mark of either kind, whichever comes first: the parser quotes the
      refused text with ``%r``, and Python picks the mark by what the text contains.
      The reason stays and the text goes.
    * Every message is cut at its first ``?``.

    What was cut is replaced by a fixed marker. The message is formatted once, here,
    and the record's arguments are cleared so that nothing is formatted again.

    It rewrites the message only. A traceback is added to the line afterwards, by the
    formatter, and ends with the exception's own text: that is not filtered.
    """

    MARKER = "[redacted]"
    REFUSED_REQUEST_PREFIX = "Invalid request from ip="

    def filter(self, record):
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        cut = len(message)
        if message.startswith(self.REFUSED_REQUEST_PREFIX):
            for mark in ("'", '"'):
                position = message.find(mark)
                if position != -1:
                    cut = min(cut, position)
        query = message.find("?")
        if query != -1:
            cut = min(cut, query)
        record.msg = message if cut == len(message) else message[:cut] + self.MARKER
        record.args = ()
        return True


class RedactingLogger(Logger):
    """Gunicorn's logger with the filter above on its error logger (gunicorn.error).

    Every record of that logger passes the filter, whichever method Gunicorn called
    for it.
    """

    def setup(self, cfg):
        super().setup(cfg)
        if not any(isinstance(existing, RedactingErrorLogFilter) for existing in self.error_log.filters):
            self.error_log.addFilter(RedactingErrorLogFilter())


logger_class = RedactingLogger
