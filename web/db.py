"""The one place that opens and configures SQLite connections for the web app.

Deliberately low-level: only the standard library is imported, never ``web.app``
or a blueprint, so any web module (and ``src.subject_browse_cache``) can use it
without an import cycle.

Policy for every connection returned by :func:`connect_db`:

* ``sqlite3.Row`` rows;
* ``PRAGMA foreign_keys=ON`` (a per-connection setting, off by default);
* a busy timeout of ``timeout`` seconds (``sqlite3.connect`` applies it as
  SQLite's busy handler; ``PRAGMA busy_timeout`` reports it in milliseconds);
* with ``query_only=True``: an existing database is opened and ``PRAGMA
  query_only=ON`` refuses every write through the connection.

Journal mode is intentionally absent. ``journal_mode=WAL`` is persistent,
database-level state, so opening or serving from a connection must never change
it. WAL is chosen once when a live database is prepared (docs/DEPLOYMENT.md,
"Database preparation"); the web application only consumes the prepared file.

The database *path* is always an argument. Callers read their own configured
path at call time (``app.config["DB_PATH"]``, ``QUIZ_DB_PATH``), which is what
tests patch; resolving a path here would bypass that isolation.
"""

import sqlite3
from pathlib import Path

DEFAULT_TIMEOUT_SECONDS = 5.0  # Python's own default, made explicit


def connect_db(path, *, query_only: bool = False,
               timeout: float = DEFAULT_TIMEOUT_SECONDS) -> sqlite3.Connection:
    """Open ``path`` under the shared connection policy; the caller closes it.

    A writable connection opens ``path`` as given (as ``sqlite3.connect`` does,
    so ``:memory:`` keeps working). A ``query_only`` connection never creates a
    missing database, and is opened ``mode=rw`` rather than ``mode=ro`` so SQLite
    can initialize absent WAL/SHM sidecars (``mode=ro`` cannot on all builds);
    ``query_only`` is what keeps it read-only.
    """
    if query_only:
        conn = sqlite3.connect(
            Path(path).resolve().as_uri() + "?mode=rw", uri=True, timeout=timeout)
    else:
        conn = sqlite3.connect(str(path), timeout=timeout)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        if query_only:
            conn.execute("PRAGMA query_only=ON")
    except BaseException:
        conn.close()
        raise
    return conn
