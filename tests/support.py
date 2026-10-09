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
import time
import unittest
from contextlib import ExitStack, closing, contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from tests.runtime_db_guard import guard_connect_db
from web import db as web_db

with patch.dict(os.environ, {"DZI_DB": ":memory:"}):
    from web import app as web_app


from web.services import lifecycle as lifecycle_service  # noqa: E402
from web.services import quiz as quiz_service  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- lifecycle clock
#
# Lifecycle writes (start, submit, upload, create, edit, duplicate, close) pass a
# clock-health check that needs evidence from outside the wall clock: a boot
# identity, a boot-time clock and the operating system's synchronisation report
# (docs/ASSIGNMENT_LIFECYCLE_DESIGN.md, section 3.6). Tests get that evidence from
# ``TEST_CLOCK``, installed once for the test process. By default it is real time
# on one fixed boot with a synchronised clock, so ordinary tests need nothing.
#
# A test that controls time does NOT patch ``quiz_current_timestamp`` by hand: a
# patched wall clock that disagrees with the database's clock row is, correctly,
# a clock fault. It uses ``lifecycle_time(start)``, which moves the application
# clock, the boot-time clock and the clock row together.

class LifecycleTestClock(lifecycle_service.ClockEnvironment):
    """Deterministic clock evidence for tests.

    Unfrozen: wall time is real, boot time is ``time.monotonic``. Frozen
    (``freeze``): time stands still until the test moves it. ``advance`` lets time
    pass (wall and boot time together); ``step_wall`` moves only the wall clock,
    which is what a stepped or corrected clock looks like; ``reboot`` starts a new
    boot; ``sync`` is the operating system's synchronisation report.
    """

    name = "test"
    FROZEN_BOOTTIME_MS = 3_600_000  # an hour after boot: past the synchronisation wait

    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self.frozen_at = None
        self.boot = "test-boot"
        self.sync = True
        self._boottime_ms = self.FROZEN_BOOTTIME_MS

    def wall(self) -> datetime:
        return self.frozen_at if self.frozen_at is not None else datetime.utcnow()

    def boot_id(self):
        return self.boot

    def boottime_ms(self):
        if self.frozen_at is None:
            return int(time.monotonic() * 1000)
        return self._boottime_ms

    def synchronized(self):
        return self.sync

    def freeze(self, at: datetime) -> None:
        self.frozen_at = at.replace(microsecond=0)
        self._boottime_ms = self.FROZEN_BOOTTIME_MS

    def advance(self, seconds: float) -> None:
        self.frozen_at += timedelta(seconds=seconds)
        self._boottime_ms += int(seconds * 1000)

    def set(self, at: datetime) -> None:
        """Let time pass until ``at`` (wall and boot time together)."""
        self.advance((at - self.frozen_at).total_seconds())

    def step_wall(self, seconds: float) -> None:
        """A clock step: the wall clock moves, elapsed real time does not."""
        self.frozen_at += timedelta(seconds=seconds)

    def reboot(self, boottime_ms: int = 5_000) -> None:
        self.boot = f"{self.boot}+"
        self._boottime_ms = boottime_ms


TEST_CLOCK = LifecycleTestClock()
lifecycle_service.configure_clock_environment(TEST_CLOCK)


def set_lifecycle_clock_row(db_path, *, high_water: datetime, state: str = "ok", reason=None,
                            changed_at=None, anchored: bool = True) -> None:
    """Write the database's clock row consistently with ``TEST_CLOCK``.

    The one place a test sets the clock row: no test relies on the migration seed.
    ``anchored`` writes a boot anchor for the clock's current boot at ``high_water``.
    """
    fmt = lifecycle_service.format_timestamp
    anchor = (fmt(high_water), TEST_CLOCK.boot_id(), TEST_CLOCK.boottime_ms()) if anchored else (None, None, None)
    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.execute("DELETE FROM lifecycle_clock")
        conn.execute(
            "INSERT INTO lifecycle_clock (id, high_water_at, state, state_reason, state_changed_at, "
            "ref_wall_at, ref_boot_id, ref_boottime_ms) VALUES (1, ?, ?, ?, ?, ?, ?, ?)",
            (fmt(high_water), state, reason, None if changed_at is None else fmt(changed_at), *anchor))
        conn.commit()


def reset_lifecycle_clock_row(db_path) -> None:
    """Back to the unanchored seed: state ``ok``, sentinel high-water mark, no anchor."""
    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.execute("DELETE FROM lifecycle_clock")
        conn.execute("INSERT INTO lifecycle_clock (id, high_water_at) VALUES (1, ?)",
                     (lifecycle_service.CLOCK_SEED,))
        conn.commit()


@contextmanager
def lifecycle_time(start: datetime, db_path=None):
    """Run the block at lifecycle time ``start``; yields ``TEST_CLOCK``.

    The application clock (``quiz_current_timestamp``), the boot-time clock and
    the clock row of ``db_path`` (default: the current test database) are set
    together, with the high-water mark and the boot anchor at ``start``. Move
    time with ``clock.advance(seconds)`` / ``clock.set(datetime)``. On exit real
    time resumes and the clock row returns to its unanchored seed, so nothing
    leaks into the tests that follow.
    """
    target = Path(db_path) if db_path is not None else Path(web_app.QUIZ_DB_PATH)
    TEST_CLOCK.freeze(start)
    try:
        set_lifecycle_clock_row(target, high_water=TEST_CLOCK.frozen_at)
        with patch.object(quiz_service, "quiz_current_timestamp", new=TEST_CLOCK.wall):
            yield TEST_CLOCK
    finally:
        TEST_CLOCK.reset()
        if target.is_file():
            reset_lifecycle_clock_row(target)


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
            )
            os.environ.update(DZI_DB=str(db_path), DZI_VAULT=str(vault_path))
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


def add_lifecycle_schema(conn: sqlite3.Connection, *, assignment_ids=()) -> None:
    """Give a hand-built minimal schema what lifecycle enforcement reads.

    Applies migration 018 (the lifecycle columns of ``quiz_assignments`` and the
    seeded ``lifecycle_clock`` row), creating a minimal ``quiz_assignments`` first
    if the schema has none, and adds ``quiz_attempts.started_at`` where a minimal
    attempts table left it out. ``assignment_ids`` are inserted as open
    assignments without a deadline. For tests that build their own tiny schema;
    databases from ``build_test_database`` already have all of it.
    """
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "users" not in tables:  # closed_by_user_id references it
        conn.executescript((ROOT / "web" / "migrations" / "012_users.sql").read_text(encoding="utf-8"))
    if "quiz_assignments" not in tables:
        conn.execute("CREATE TABLE quiz_assignments (id INTEGER PRIMARY KEY, title_bg TEXT, owner_user_id INTEGER)")
    conn.executescript((ROOT / "web" / "migrations" / "018_assignment_lifecycle.sql").read_text(encoding="utf-8"))
    if "quiz_attempts" in tables and "started_at" not in {
            row[1] for row in conn.execute("PRAGMA table_info(quiz_attempts)")}:
        conn.execute("ALTER TABLE quiz_attempts ADD COLUMN started_at TEXT DEFAULT '2026-01-01 10:00:00'")
    for assignment_id in assignment_ids:
        conn.execute("INSERT OR IGNORE INTO quiz_assignments (id, title_bg) VALUES (?, 'Fixture assignment')",
                     (assignment_id,))
    conn.commit()


# The explicit deadline choice every create, edit and duplicate form must state
# (docs/ASSIGNMENT_LIFECYCLE_DESIGN.md, 5.4): a request without one is refused.
# Tests that are not about deadlines state "no deadline" in the default zone.
NO_DEADLINE_FORM = {"deadline_choice": "none", "display_timezone": "Europe/Sofia"}


def student_entry_path(assignment_id: int) -> str:
    """The student link (``/quiz/s/<public_token>``) of an assignment in the current test database.

    Read-only: it returns the token the assignment already has and fails if there is
    none or it is not a valid token. It never generates or stores one, so a test that
    reaches an assignment through here proves that whatever created the assignment
    (the application, or a fixture that supplied a token explicitly) gave it a token.
    The numeric ``/quiz/<id>`` entry is retired; this is the only way to a start page.
    """
    from web.assignment_tokens import is_valid_public_token

    with closing(web_app.quiz_db()) as conn:
        row = conn.execute("SELECT public_token FROM quiz_assignments WHERE id = ?", (assignment_id,)).fetchone()
    if row is None:
        raise AssertionError(f"no assignment {assignment_id} in the test database")
    if not is_valid_public_token(row[0]):
        raise AssertionError(
            f"assignment {assignment_id} has no valid public token: create it through the application "
            "or give the fixture row a token explicitly")
    return f"/quiz/s/{row[0]}"
