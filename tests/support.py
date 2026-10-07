"""Temporary application state for unittest discovery and individual modules.

Importing web.app never changes a database schema. Import it against an in-memory
DB, then configure its two independent DB paths before class fixtures or requests run;
environment variables alone cannot reconfigure an already imported application.
"""

import atexit
import hashlib
import os
import shutil
import sqlite3
import stat
import tempfile
import threading
import unittest
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path
from unittest.mock import patch

from tests.runtime_db_guard import guard_connect_db
from web import db as web_db

with patch.dict(os.environ, {"DZI_DB": ":memory:"}):
    from web import app as web_app


ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- canonical template
#
# Test databases are never copies of a repository database. They are clones of one
# template per test process that ``src.build_database`` builds from migrations +
# data/canonical (about 1.5 s, once). The template is read-only, hash-checked on every
# clone and never handed to a test; each test class/test gets its own writable copy.
# Runtime rows (assignments, attempts, answers, submissions) start empty: tests create
# what they need with ``tests.runtime_fixtures``.

_template_lock = threading.Lock()
_template: dict = {}


def _template_inputs_digest() -> str:
    """Digest of the tracked inputs the template is built from (manifest + migrations)."""
    digest = hashlib.sha256()
    for path in sorted((ROOT / "data" / "canonical").glob("manifest.json")) + \
            sorted((ROOT / "web" / "migrations").glob("*.sql")):
        digest.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


CANONICAL = "canonical"   # every canonical table, as data/canonical holds it
IT_ONLY = "it_only"       # canonical minus Physics/Biology: IT/DZI content only (see below)
PROFILES = (CANONICAL, IT_ONLY)


def _lock_template(path: Path) -> dict:
    path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    return {"path": path, "sha256": _file_sha256(path)}


def canonical_template(profile: str = CANONICAL) -> Path:
    """The per-process, read-only template database of ``profile`` (built on first use).

    ``canonical`` is built by ``src.build_database`` from migrations + data/canonical.
    ``it_only`` is that same template with the Physics and Biology content removed by the
    explicit fixture ``tests.subject_fixtures.remove_subject_content``: the content state
    the subject-preparation tests pin (a subject without content renders as "in
    preparation"), which the canonical data no longer has for those two subjects.

    Never open a template for writing and never return it to a test: use
    ``build_test_database`` for a writable copy. Raises if its bytes ever change.
    """
    from src import build_database

    if profile not in PROFILES:
        raise ValueError(f"unknown test database profile {profile!r}")
    with _template_lock:
        if "directory" not in _template:
            directory = Path(tempfile.mkdtemp(prefix="learnpilot-template-"))
            atexit.register(shutil.rmtree, directory, ignore_errors=True)
            _template["directory"] = directory
            _template["inputs"] = _template_inputs_digest()
        if CANONICAL not in _template:
            path = _template["directory"] / "canonical.db"
            build_database.build_database(path)
            _template[CANONICAL] = _lock_template(path)
        if profile == IT_ONLY and IT_ONLY not in _template:
            from tests.subject_fixtures import remove_subject_content

            path = _template["directory"] / "it_only.db"
            shutil.copyfile(_template[CANONICAL]["path"], path)
            remove_subject_content(path)
            _template[IT_ONLY] = _lock_template(path)
        entry = _template[profile]
        if _file_sha256(entry["path"]) != entry["sha256"]:
            raise RuntimeError(f"the {profile} test template database was modified")
        return entry["path"]


def build_test_database(destination: Path, profile: str = CANONICAL) -> Path:
    """An isolated, writable database built from migrations + canonical content.

    A byte clone of the read-only template: no runtime rows, no sidecars,
    rollback-journal mode. Mutating it never affects the template or another test.
    """
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError(destination)
    shutil.copyfile(canonical_template(profile), destination)  # copyfile keeps default (writable) mode
    return destination


# The newest migration a database created before the baseline (000) had already applied.
LEGACY_LEVEL = "010_topic_section_display_order.sql"


def build_legacy_database(destination: Path) -> Path:
    """A small synthetic database that predates the baseline migration (000).

    It has the schema an existing deployment had before 000 existed (the 000 DDL plus
    migrations 001-010, executed directly) and ``schema_migrations`` recording 001-010 but
    not 000, with a few content rows. Used to test baseline adoption; no binary fixture and
    no historical content are involved.
    """
    from src import manage_migrations

    destination = Path(destination)
    if destination.exists():
        raise FileExistsError(destination)
    migrations = sorted((ROOT / "web" / "migrations").glob("*.sql"))
    legacy = [path for path in migrations if path.name != "000_baseline.sql" and path.name <= LEGACY_LEVEL]
    with closing(sqlite3.connect(destination)) as conn:
        conn.executescript((ROOT / "web" / "migrations" / "000_baseline.sql").read_text(encoding="utf-8"))
        manage_migrations.ensure_schema_migrations(conn)
        for path in legacy:
            conn.executescript(path.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_migrations (filename, applied_at) VALUES (?, CURRENT_TIMESTAMP)",
                         (path.name,))
        conn.execute("INSERT INTO curricula (grade, subject, education_type, title_bg) "
                     "VALUES (8, 'informatics', 'general', 'Legacy curriculum')")
        conn.execute("INSERT INTO curricula (grade, subject, education_type, title_bg) "
                     "VALUES (9, 'informatics', 'general', 'Legacy curriculum 2')")
        conn.commit()
    return destination


@contextmanager
def preserved_app_state():
    """Restore mutable configuration even when setup, a test, or cleanup fails."""
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ))
        stack.enter_context(patch.dict(web_app.app.config))
        for name in (
            "DB_PATH", "QUIZ_DB_PATH", "QUIZ_VAULT_PATH", "PROJECT_ROOT",
        ):
            stack.enter_context(patch.object(web_app, name, getattr(web_app, name)))
        yield


@contextmanager
def isolated_app(profile: str = CANONICAL):
    """A canonical-built DB and writable artifact directories owned by one test class."""
    with tempfile.TemporaryDirectory(prefix="learnpilot-tests-") as directory:
        root = Path(directory).resolve()
        db_path = root / "questions.db"
        build_test_database(db_path, profile)
        vault_path = root / "vault"
        vault_path.mkdir()
        upload_root = root / "uploads" / "practical"
        upload_root.mkdir(parents=True)

        # A test on a temporary DB must never reach the local runtime database through the
        # shared web connection factory (e.g. leaked DB_PATH/QUIZ_DB_PATH).
        with preserved_app_state(), \
                patch.object(web_db, "connect_db", guard_connect_db(web_db.connect_db)), \
                patch.object(web_app.app, "_got_first_request", False):
            # Some legacy class fixtures register a test-only auth route. Reset
            # Flask's setup marker for that fixture and restore it on exit.
            web_app.DB_PATH = db_path
            web_app.QUIZ_DB_PATH = db_path
            web_app.QUIZ_VAULT_PATH = vault_path
            web_app.app.config.update(
                TESTING=True,
                DB_PATH=str(db_path),
                PRACTICAL_UPLOAD_ROOT=str(upload_root),
                # The transitional bridge, whatever the developer's shell exports.
                # Cutover behaviour (production) is pinned by tests that turn it off.
                LEGACY_SHARED_LOGIN=True,
            )
            os.environ.update(
                DZI_DB=str(db_path), DZI_VAULT=str(vault_path),
                DZI_ADMIN_PASSWORD="admin-pass", DZI_TESTER_PASSWORD="tester-pass",
            )
            yield root


class IsolatedAppTestCase(unittest.TestCase):
    """Call super().setUpClass() before a subclass's DB-backed class fixtures.

    DB data is shared within a class for existing class-level question fixtures,
    but each class starts from a fresh canonical-built database. Tests using custom temporary schemas may
    override paths in setUp; all globals/configuration are restored afterwards.
    """

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        # Python 3.9 otherwise inherits one cleanup list across subclasses.
        cls._class_cleanups = []

    # Which content the class's database holds: CANONICAL (default) or IT_ONLY.
    content_profile = CANONICAL

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        stack = ExitStack()
        cls.addClassCleanup(stack.close)
        cls.isolation_root = stack.enter_context(isolated_app(cls.content_profile))

    def run(self, result=None):
        with preserved_app_state():
            return super().run(result)
