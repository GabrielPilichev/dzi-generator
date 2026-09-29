"""Temporary application state for unittest discovery and individual modules.

Importing web.app runs the legacy quiz migration. Bootstrap it in memory, then
configure its two independent DB paths before class fixtures or requests run;
environment variables alone cannot reconfigure an already imported application.
"""

import os
import shutil
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

with patch.dict(os.environ, {"DZI_DB": ":memory:"}):
    from web import app as web_app


ROOT = Path(__file__).resolve().parents[1]


def copy_seed_database(source: Path, destination: Path):
    """Copy a quiescent seed plus committed WAL data without opening SQLite.

    Even mode=ro SQLite connections can update the source -shm file. Let SQLite
    rebuild shared memory only for the temporary copy. Fail if the source changes
    during copying rather than running tests against an inconsistent snapshot.
    """
    source_wal = Path(str(source) + "-wal")

    def source_state():
        return {
            path: (path.stat().st_ino, path.stat().st_size,
                   path.stat().st_mtime_ns, path.stat().st_ctime_ns)
            for path in (source, source_wal) if path.exists()
        }

    before = source_state()
    shutil.copyfile(source, destination)
    if source_wal in before:
        shutil.copyfile(source_wal, Path(str(destination) + "-wal"))
    if source_state() != before:
        raise RuntimeError("Seed database changed while copying; retry when it is idle")


@contextmanager
def preserved_app_state():
    """Restore mutable configuration even when setup, a test, or cleanup fails."""
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ))
        stack.enter_context(patch.dict(web_app.app.config))
        for name in (
            "DB_PATH", "QUIZ_DB_PATH", "QUIZ_VAULT_PATH", "PROJECT_ROOT",
            "PRACTICAL_RESOURCE_ROOTS",
        ):
            stack.enter_context(patch.object(web_app, name, getattr(web_app, name)))
        yield


@contextmanager
def isolated_app():
    """A seeded DB and writable artifact directories owned by one test class."""
    with tempfile.TemporaryDirectory(prefix="learnpilot-tests-") as directory:
        root = Path(directory).resolve()
        db_path = root / "questions.db"
        copy_seed_database(ROOT / "data/questions.db", db_path)
        vault_path = root / "vault"
        vault_path.mkdir()
        upload_root = root / "uploads" / "practical"
        upload_root.mkdir(parents=True)

        with preserved_app_state(), patch.object(web_app.app, "_got_first_request", False):
            # Some legacy class fixtures register a test-only auth route. Reset
            # Flask's setup marker for that fixture and restore it on exit.
            web_app.DB_PATH = db_path
            web_app.QUIZ_DB_PATH = db_path
            web_app.QUIZ_VAULT_PATH = vault_path
            web_app.app.config.update(
                TESTING=True,
                DB_PATH=str(db_path),
                PRACTICAL_UPLOAD_ROOT=str(upload_root),
            )
            os.environ.update(
                DZI_DB=str(db_path), DZI_VAULT=str(vault_path),
                DZI_ADMIN_PASSWORD="admin-pass", DZI_TESTER_PASSWORD="tester-pass",
            )
            yield root


class IsolatedAppTestCase(unittest.TestCase):
    """Call super().setUpClass() before a subclass's DB-backed class fixtures.

    DB data is shared within a class for existing class-level question fixtures,
    but each class starts from the seed. Tests using custom temporary schemas may
    override paths in setUp; all globals/configuration are restored afterwards.
    """

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        # Python 3.9 otherwise inherits one cleanup list across subclasses.
        cls._class_cleanups = []

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        stack = ExitStack()
        cls.addClassCleanup(stack.close)
        cls.isolation_root = stack.enter_context(isolated_app())

    def run(self, result=None):
        with preserved_app_state():
            return super().run(result)
