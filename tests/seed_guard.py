"""Keeps the test suite away from the tracked seed database, data/questions.db.

Two independent safeguards, both standard library only and neither touching
``sqlite3`` globally:

* a suite-level tripwire: the sha256 of the seed (and its -wal, when present) is
  recorded when the ``tests`` package is first imported, and compared again when
  the interpreter exits. A mismatch makes the process exit with status 1, even
  if every test passed;
* a guard around ``web.db.connect_db`` for tests that run on a temporary
  database: asking the shared connection factory for the seed raises
  ``SeedDatabaseAccess`` instead of opening it. ``allow_seed_inspection()`` is
  the explicit opt-in for a test that must read the seed through the factory.

Reading the seed's bytes (as ``tests.support.copy_seed_database`` does) is fine
and is not guarded: only opening it through SQLite can create sidecars or
rewrite its header.
"""

import atexit
import contextvars
import functools
import hashlib
import os
import sys
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEED_DB = ROOT / "data" / "questions.db"
SIDECAR_SUFFIXES = ("", "-wal")  # the database and its write-ahead log

_inspection_allowed = contextvars.ContextVar("seed_inspection_allowed", default=False)
_installed = {}


class SeedDatabaseAccess(AssertionError):
    """A test tried to open the tracked seed database through the web factory."""


def seed_fingerprint(path=SEED_DB) -> dict:
    """sha256 of the database and its WAL; None for a file that does not exist."""
    fingerprint = {}
    for suffix in SIDECAR_SUFFIXES:
        file = Path(str(path) + suffix)
        fingerprint[file.name] = (
            hashlib.sha256(file.read_bytes()).hexdigest() if file.exists() else None
        )
    return fingerprint


def seed_changes(baseline: dict, path=SEED_DB) -> list:
    """Human-readable differences between ``baseline`` and the seed's current state."""
    current = seed_fingerprint(path)
    return [
        f"{name}: {(baseline[name] or 'absent')[:12]} -> {(current[name] or 'absent')[:12]}"
        for name in baseline if baseline[name] != current[name]
    ]


def install_seed_tripwire(path=SEED_DB):
    """Record the seed now; at interpreter exit, fail the run if it changed.

    ``atexit`` handlers cannot change the exit status by raising, so a mismatch
    reports and ends the process with ``os._exit(1)`` once output is flushed.
    Idempotent per path.
    """
    key = str(Path(path).resolve())
    if key in _installed:
        return _installed[key]
    baseline = seed_fingerprint(path)
    _installed[key] = baseline

    def verify():
        changes = seed_changes(baseline, path)
        if changes:
            sys.stderr.write(
                "\nSEED DATABASE TRIPWIRE: the tracked seed database changed during "
                "this run (" + "; ".join(changes) + ").\nA test or a local server "
                "wrote to " + str(path) + ". Restore it with `git restore "
                "data/questions.db` unless the change is intentional.\n"
            )
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(1)

    atexit.register(verify)
    return baseline


def _is_protected(database, protected: Path) -> bool:
    raw = os.fspath(database)
    if not raw or raw == ":memory:":
        return False
    candidate = Path(raw)
    if candidate.resolve() == protected:
        return True
    try:
        return os.path.samefile(candidate, protected)
    except OSError:  # one of them does not exist
        return False


def guard_connect_db(connect_db, protected=SEED_DB):
    """Wrap a ``connect_db``-style factory so it refuses the protected database."""
    protected = Path(protected).resolve()

    @functools.wraps(connect_db)
    def guarded(path, *args, **kwargs):
        if not _inspection_allowed.get() and _is_protected(path, protected):
            raise SeedDatabaseAccess(
                f"{path} is the tracked seed database; tests must use a temporary "
                "copy (tests.support.isolated_app) or allow_seed_inspection()"
            )
        return connect_db(path, *args, **kwargs)

    return guarded


@contextmanager
def allow_seed_inspection():
    """Let the guarded factory open the seed. Use only for read-only inspection."""
    token = _inspection_allowed.set(True)
    try:
        yield
    finally:
        _inspection_allowed.reset(token)
