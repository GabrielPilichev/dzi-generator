"""Keeps tests away from a developer's local runtime database, data/questions.db.

``data/questions.db`` is ignored, untracked runtime state: it may be absent (fresh clone) or hold
a developer's working data. Tests never need it, so a test running on a temporary database must
not reach it through the shared connection factory: asking ``web.db.connect_db`` for that path
raises ``RuntimeDatabaseAccess`` instead of opening it. Nothing here reads, hashes or copies the
file, and it works the same whether or not the file exists.
"""

import functools
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DB = ROOT / "data" / "questions.db"


class RuntimeDatabaseAccess(AssertionError):
    """A test tried to open the local runtime database through the web factory."""


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


def guard_connect_db(connect_db, protected=RUNTIME_DB):
    """Wrap a ``connect_db``-style factory so it refuses the protected database."""
    protected = Path(protected).resolve()

    @functools.wraps(connect_db)
    def guarded(path, *args, **kwargs):
        if _is_protected(path, protected):
            raise RuntimeDatabaseAccess(
                f"{path} is the local runtime database; tests must use a temporary "
                "database (tests.support.isolated_app)"
            )
        return connect_db(path, *args, **kwargs)

    return guarded
