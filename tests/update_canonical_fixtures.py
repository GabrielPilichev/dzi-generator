"""Synthetic fixtures for the canonical content updater tests (standard library only).

These helpers deliberately do not import ``tests.support`` (which imports ``web.app``): the
updater is a stdlib tool and its tests must not need the application's dependencies. The
replacement for the ``tests.support`` isolation is the connect spy below, which refuses any
connection to the developer's local runtime database ``tests.runtime_db_guard.RUNTIME_DB`` by
path or by ``os.path.samefile`` and records every other connection.

Every database is a temporary file. A synthetic "mini canonical" database holds a few
explicit-id rows in each of the 27 canonical tables; its export is the preimage, and clones
plus additions are exported to make sources.
"""

from __future__ import annotations

import atexit
import hashlib
import io
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock
from urllib.parse import unquote, urlparse

from src import canonical_export as export
from src import canonical_load as load
from src import canonical_tables as ct
from src import update_canonical_content as tool
from tests.runtime_db_guard import RUNTIME_DB, RuntimeDatabaseAccess, _is_protected

REAL_CONNECT = sqlite3.connect          # captured at import: hooks use it to bypass the spy
ROOT = Path(__file__).resolve().parents[1]
CANONICAL_DIR = ROOT / "data" / "canonical"
FIXED_TS = "2026-01-01 00:00:00"
SHA_A = "a" * 64
SHA_B = "b" * 64


# ----------------------------------------------------------------------------- connect spy

def decoded_target(database) -> str:
    raw = os.fspath(database) if not isinstance(database, str) else database
    if raw.startswith("file:"):
        return unquote(urlparse(raw).path)
    return raw


class ConnectSpy:
    """Replacement for ``sqlite3.connect``: records every call, refuses the runtime database."""

    def __init__(self, real=REAL_CONNECT):
        self.real, self.calls = real, []

    def __call__(self, database, *args, **kwargs):
        target = decoded_target(database)
        if _is_protected(target, RUNTIME_DB):
            raise RuntimeDatabaseAccess(f"{target} is the local runtime database")
        self.calls.append({"raw": database if isinstance(database, str) else os.fspath(database),
                           "path": target, "uri": bool(kwargs.get("uri"))})
        return self.real(database, *args, **kwargs)

    def clear(self) -> None:
        self.calls.clear()

    def uri_targets(self) -> list[dict]:
        return [call for call in self.calls if call["raw"].startswith("file:")]


def install_connect_spy(test: unittest.TestCase) -> ConnectSpy:
    spy = ConnectSpy()
    patcher = mock.patch.object(sqlite3, "connect", spy)
    patcher.start()
    test.addCleanup(patcher.stop)
    return spy


# ----------------------------------------------------------------------------- statement spy and COMMIT injection

class CommitInjection:
    """What the next COMMIT does: ``before`` raises with the transaction open, ``after`` commits
    and then raises, ``ended`` rolls back and then raises, ``busy`` raises a busy error and
    ``after_journal`` commits, creates ``journal`` and raises. ``code`` sets ``sqlite_errorcode``
    on the raised error; without it the error is a plain Python-built one (no error code)."""

    CODE_NAMES = {5: "SQLITE_BUSY", 6: "SQLITE_LOCKED", 10: "SQLITE_IOERR"}

    def __init__(self, mode: str, message: str = "disk I/O error", *, code: int | None = None, journal=None):
        self.mode, self.message, self.code, self.journal = mode, message, code, journal

    def error(self, message=None) -> sqlite3.OperationalError:
        exc = sqlite3.OperationalError(message or self.message)
        if self.code is not None:
            exc.sqlite_errorcode = self.code
            exc.sqlite_errorname = self.CODE_NAMES.get(self.code, "SQLITE_ERROR")
        return exc


def make_connection_class(log: list, injection: CommitInjection | None = None, hide_table: str | None = None,
                          rewrite=None, strip_errors: bool = False):
    """A ``sqlite3.Connection`` that logs statements and authorizer changes and injects COMMIT outcomes.

    ``rewrite(sql)`` may return a replacement statement (used to show a probe a partial view);
    ``strip_errors`` re-raises every error as a plain one without ``sqlite_errorcode``."""
    class SpyConnection(sqlite3.Connection):
        def set_authorizer(self, callback):
            log.append(("authorizer", getattr(callback, "__name__", None)))
            return super().set_authorizer(callback)

        def _plain(self, sql, params):
            if not strip_errors:
                return super().execute(sql, *params)
            try:
                return super().execute(sql, *params)
            except sqlite3.OperationalError as exc:
                raise sqlite3.OperationalError(str(exc)) from None

        def execute(self, sql, *params):
            text = sql.strip() if isinstance(sql, str) else sql
            log.append(text)
            if rewrite is not None and isinstance(text, str):
                replacement = rewrite(text)
                if replacement is not None:
                    return self._plain(replacement, params)
            if injection is not None and isinstance(text, str) and text.upper() == "COMMIT":
                if injection.mode == "before":
                    raise injection.error()
                if injection.mode == "busy":
                    raise injection.error("database is locked")
                if injection.mode == "after":
                    super().execute(sql, *params)
                    raise injection.error()
                if injection.mode == "after_journal":
                    super().execute(sql, *params)
                    Path(injection.journal).write_bytes(b"")
                    raise injection.error()
                if injection.mode == "ended":
                    super().execute("ROLLBACK")
                    raise injection.error()
            if hide_table and isinstance(text, str) and text.upper().startswith("SELECT") and hide_table in text:
                return super().execute("SELECT 1 WHERE 0")
            return self._plain(sql, params)
    return SpyConnection


class FactoryConnect:
    """A ``connect`` seam: returns instrumented connections and records what was opened.

    ``classes`` lists, per target connect (the first is the working connection, the second the
    COMMIT probe), the connection class to use; ``fail_on`` makes that connect raise."""

    def __init__(self, classes=None, *, fail_on=(), before_open=None):
        self.classes = list(classes or [])
        self.fail_on = set(fail_on)
        self.before_open = before_open or {}
        self.calls: list[str] = []

    def __call__(self, database, *args, **kwargs):
        index = len(self.calls)
        self.calls.append(database)
        if index in self.before_open:
            self.before_open[index]()
        if index in self.fail_on:
            raise sqlite3.OperationalError("unable to open database file")
        factory = self.classes[index] if index < len(self.classes) else sqlite3.Connection
        return REAL_CONNECT(database, *args, factory=factory, **kwargs)


class LegacyAuthorizerConnection(sqlite3.Connection):
    """A connection with the ``set_authorizer(None)`` semantics of CPython 3.9 and 3.10.

    Before 3.11 ``None`` is not "remove the authorizer": it installs a callback that denies every
    action (the real system 3.9 does exactly that), so ``PRAGMA query_only=OFF`` fails after it."""

    def set_authorizer(self, callback):
        if callback is None:
            return super().set_authorizer(lambda *_args: 1)     # SQLITE_DENY for everything
        return super().set_authorizer(callback)


class StuckRollbackConnection(sqlite3.Connection):
    """A connection whose COMMIT raises and whose single ROLLBACK afterwards fails without ending the
    transaction, so ``in_transaction`` stays true. ``events`` records ``commit`` and ``rollback``."""

    events: list = []

    def execute(self, sql, *params):
        text = sql.strip().upper() if isinstance(sql, str) else ""
        if text == "COMMIT":
            self.events.append("commit")
            raise sqlite3.OperationalError("disk I/O error")
        if text == "ROLLBACK" and "commit" in self.events:
            self.events.append("rollback")
            raise sqlite3.OperationalError("cannot rollback")
        return super().execute(sql, *params)


def stuck_rollback_class(events: list):
    return type("StuckRollback", (StuckRollbackConnection,), {"events": events})


# ----------------------------------------------------------------------------- open spy and manifest redirects

class OpenSpy:
    """Records every path opened while active, through the interpreter's audit ``open`` event.

    That one event covers ``open``, ``io.open``, ``os.open`` and ``Path.read_bytes``/``read_text``/``open``,
    so no read of a redirected file can bypass it. Audit hooks cannot be removed; the single installed
    hook records only for spies that are currently active."""

    _active: list = []
    _installed = False

    def __init__(self):
        self.paths: list[str] = []

    @classmethod
    def _hook(cls, event, args):
        if event == "open" and cls._active:
            path = args[0]
            if isinstance(path, (str, bytes)):
                text = os.fsdecode(path)
                for spy in cls._active:
                    spy.paths.append(text)

    def start(self) -> None:
        if not OpenSpy._installed:
            import sys
            sys.addaudithook(OpenSpy._hook)
            OpenSpy._installed = True
        if self not in OpenSpy._active:
            OpenSpy._active.append(self)

    def stop(self) -> None:
        if self in OpenSpy._active:
            OpenSpy._active.remove(self)

    def opened_under(self, directory) -> list[str]:
        root = os.path.realpath(directory)
        resolved = [(path, os.path.realpath(path)) for path in self.paths]
        return [path for path, real in resolved if real == root or real.startswith(root + os.sep)]

    def opened_named(self, name: str) -> list[str]:
        return [path for path in self.paths if os.path.basename(path) == name]


def redirect_table_file(directory, table: str, how: str, outside) -> Path | None:
    """Make ``directory``'s manifest name something other than ``<table>.jsonl``.

    ``absolute``/``relative`` move the table file to a valid JSONL file under ``outside`` and point the
    manifest at it (``relative`` as ``../../<path from the temporary root>``, which the run's private
    copy two levels below the temporary root resolves to the same outside file); ``other_table`` names
    another table's file; ``missing`` deletes the expected file; the rest are malformed entries.
    Returns the outside file, if one was made."""
    import tempfile
    directory, outside = Path(directory), Path(outside)
    external = None
    if how in ("absolute", "relative"):
        outside.mkdir(parents=True, exist_ok=True)
        external = outside / f"{table}.jsonl"
        shutil.move(str(table_path(directory, table)), external)
        if how == "absolute":
            named = str(external)
        else:
            named = "../../" + os.path.relpath(external, tempfile.gettempdir())
    elif how == "other_table":
        named = "curricula.jsonl" if table != "curricula" else "questions.jsonl"
    elif how == "missing":
        table_path(directory, table).unlink()
        return None
    else:
        named = None

    def change(manifest):
        if how == "missing":
            return
        entries = manifest["tables"]
        if how == "entry_is_string":
            entries[table] = "questions.jsonl"
        elif how == "entry_is_null":
            entries[table] = None
        elif how == "file_is_number":
            entries[table]["file"] = 5
        elif how == "file_is_list":
            entries[table]["file"] = [f"{table}.jsonl"]
        elif how == "file_absent":
            entries[table].pop("file")
        elif how == "tables_is_list":
            manifest["tables"] = list(entries.values())
        else:
            entries[table]["file"] = named
    edit_manifest(directory, change)
    return external


# ----------------------------------------------------------------------------- running the tool

class Result:
    def __init__(self, exit_code: int, out: str, err: str):
        self.exit, self.out, self.err = exit_code, out, err
        self._report = None

    @property
    def report(self) -> dict:
        if self._report is None:
            self._report = json.loads(self.out)
        return self._report


def run_tool(db, source, *, preimage=None, apply_hash=None, extra=(), fmt="json", hooks=None, connect=None,
             migrations_dir=ct.MIGRATIONS_DIR, out=None, err=None, busy_ms=None, raw_argv=None) -> Result:
    if raw_argv is not None:
        argv = list(raw_argv)
    else:
        argv = ["--db", str(db), "--source", str(source), "--format", fmt]
        if preimage is not None:
            argv += ["--preimage", str(preimage)]
        if apply_hash is not None:
            argv += ["--apply", "--expect-plan-sha256", apply_hash]
        if busy_ms is not None:
            argv += ["--busy-timeout-ms", str(busy_ms)]
        argv += list(extra)
    out = out if out is not None else io.StringIO()
    err = err if err is not None else io.StringIO()
    code = tool.run(argv, out=out, err=err, hooks=hooks, connect=connect, migrations_dir=migrations_dir)
    return Result(code, out.getvalue() if isinstance(out, io.StringIO) else "", err.getvalue()
                  if isinstance(err, io.StringIO) else "")


# ----------------------------------------------------------------------------- snapshots

def file_bytes_digest(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def tree_listing(directory) -> list[str]:
    return sorted(entry.name for entry in os.scandir(directory))


def db_snapshot(source) -> dict:
    """Every table with storage classes, order-independent. ``source`` is a path or a connection."""
    def dump(conn):
        result = {}
        names = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
        for name in names:
            columns = [row[1] for row in conn.execute(f'PRAGMA table_info("{name}")')]
            parts = []
            for column in columns:
                parts.append(f'typeof("{column}")')
                parts.append(f'CASE WHEN typeof("{column}") IN (\'text\', \'blob\') '
                             f'THEN CAST("{column}" AS BLOB) ELSE "{column}" END')
            rows = [repr(tuple(row)) for row in conn.execute(f'SELECT {", ".join(parts)} FROM "{name}"')]
            result[name] = sorted(rows)
        result["__master__"] = sorted(repr(tuple(row)) for row in conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master"))
        return result

    if isinstance(source, sqlite3.Connection):
        return dump(source)
    with closing(REAL_CONNECT(str(source))) as conn:
        return dump(conn)


def canonical_lines(path, table: str) -> list[str]:
    with closing(REAL_CONNECT(str(path))) as conn:
        return list(ct.iter_table_lines(conn, table))


def sequence_map(path) -> dict:
    with closing(REAL_CONNECT(str(path))) as conn:
        return dict(conn.execute("SELECT name, seq FROM sqlite_sequence"))


def scalar(path, sql, params=()):
    with closing(REAL_CONNECT(str(path))) as conn:
        return conn.execute(sql, params).fetchone()[0]


def execute(path, sql, params=()) -> None:
    with closing(REAL_CONNECT(str(path), isolation_level=None)) as conn:
        conn.execute(sql, params)


# ----------------------------------------------------------------------------- temporary space

_SHARED: dict = {}


def _shared_root() -> Path:
    if "root" not in _SHARED:
        root = Path(tempfile.mkdtemp(prefix="learnpilot-updater-fixture-"))
        atexit.register(shutil.rmtree, root, True)
        _SHARED["root"] = root
    return _SHARED["root"]


def insert(conn, table: str, **values) -> int:
    columns = [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]
    for stamp in ("created_at", "parsed_at"):
        if stamp in columns and stamp not in values:
            values[stamp] = FIXED_TS
    names = list(values)
    cursor = conn.execute(f'INSERT INTO "{table}" ({", ".join(f"{n}" for n in names)}) '
                          f'VALUES ({", ".join("?" for _ in names)})', [values[n] for n in names])
    return cursor.lastrowid


def populate_mini(conn) -> None:
    """The base mini canonical rows (explicit ids, with gaps, in all 27 canonical tables)."""
    insert(conn, "curricula", id=1, grade=11, subject="IT", education_type="profile", title_bg="Учебна програма 11")
    insert(conn, "curricula", id=2, grade=12, subject="IT", education_type="profile", title_bg="Учебна програма 12")
    insert(conn, "curriculum_areas", id=1, area_id="spreadsheets", title_bg="Електронни таблици",
           moc_filename="spreadsheets-moc.md")
    insert(conn, "curriculum_areas", id=2, area_id="databases", title_bg="Бази данни", moc_filename="databases-moc.md")
    insert(conn, "dzi_blueprints", id=1, blueprint_slug="dzi-2025", title_bg="ДЗИ 2025")
    insert(conn, "exams", id=1, subject="informatika_it", level="DZI", year=2025, session="may", variant=1,
           source_file="data/reference/dzi/official_pdfs/may_2025.pdf", sha256=SHA_A, parser_version="t1")
    insert(conn, "assets", id=1, asset_type="pdf", local_path="data/reference/dzi/official_pdfs/may_2025.pdf",
           sha256=SHA_A, mime_type="application/pdf", file_size=10)
    insert(conn, "assets", id=2, asset_type="image", local_path="web/static/img/questions/dzi/q1.png",
           sha256=SHA_B, mime_type="image/png", file_size=20)
    insert(conn, "assets", id=3, asset_type="image", local_path="web/static/img/questions/dzi/q2.png",
           sha256=SHA_B, mime_type="image/png", file_size=30)
    insert(conn, "curriculum_modules", id=1, module_number=1, title_bg="Модул 1", **{"class": 11},
           curriculum_id=1, module_slug="mod-1", moc_filename="mod-1-moc.md")
    insert(conn, "curriculum_modules", id=2, module_number=2, title_bg="Модул 2", **{"class": 12},
           curriculum_id=2, module_slug="mod-2")
    insert(conn, "curriculum_sections", id=1, curriculum_id=1, module_id=1, section_slug="sec-a",
           title_bg="Раздел А", **{"class": 11}, section_type="topic_group")
    insert(conn, "curriculum_sections", id=2, curriculum_id=2, module_id=2, section_slug="sec-b",
           title_bg="Раздел Б", **{"class": 12}, section_type="topic_group")
    insert(conn, "lessons", id=1, section_id=1, lesson_number=1, title_bg="Урок 1", lesson_type="lesson")
    insert(conn, "curriculum_topics", id=1, topic_slug="topic-a", title_bg="Тема А", area_id=1, module_id=1,
           section_id=1, lesson_id=1, note_path="Topics/topic-a.md")
    insert(conn, "curriculum_topics", id=2, topic_slug="topic-b", title_bg="Тема Б", area_id=2, module_id=2,
           section_id=2, note_path="Topics/topic-b.md")
    insert(conn, "questions", id=1, exam_id=1, source_exam="may_2025_v1", source_number=1, subject="informatika_it",
           level="DZI", year=2025, question_type="multiple_choice", topic="Таблици", points=1,
           prompt="Първи въпрос", topic_id=1)
    insert(conn, "questions", id=2, exam_id=1, source_exam="may_2025_v1", source_number=2, subject="informatika_it",
           level="DZI", year=2025, question_type="multiple_choice", topic="Таблици", points=1,
           prompt="Втори въпрос", topic_id=2)
    insert(conn, "questions", id=3, exam_id=1, source_exam="may_2025_v1", source_number=16, subject="informatika_it",
           level="DZI", year=2025, question_type="short_answer", points=3, prompt="Кратък отговор", topic_id=1)
    for qid in (5, 7):   # the deliberate non-id duplicate pair, a stand-in for 371/393
        insert(conn, "questions", id=qid, source_exam="legacy", source_number=None, question_type="multiple_choice",
               points=1, prompt="Дубликат на въпрос", topic_id=1)
    option_id = 0
    for qid, letters in ((1, "ABCD"), (2, "AB"), (5, "AB"), (7, "AB")):
        for letter in letters:
            option_id += 1
            insert(conn, "multiple_choice_options", id=option_id, question_id=qid, option_letter=letter,
                   option_text=f"Отговор {letter}", is_correct=1 if letter == "A" else 0)
    insert(conn, "fill_in_subquestions", id=1, question_id=3, subquestion_number=1, correct_answer="42")
    insert(conn, "question_topic_assignments", id=1, question_id=1, topic_id=1, section_id=1, assignment_type="primary")
    insert(conn, "exam_tasks", id=1, exam_id=1, task_number=1, exam_part=1, task_kind="multiple_choice", points=1)
    insert(conn, "exam_tasks", id=2, exam_id=1, task_number=2, exam_part=1, task_kind="multiple_choice", points=1)
    insert(conn, "exam_tasks", id=3, exam_id=1, task_number=26, exam_part=2, task_kind="practical_spreadsheet", points=15)
    insert(conn, "exam_task_questions", task_id=1, question_id=1, role="primary")
    insert(conn, "exam_task_questions", task_id=2, question_id=2, role="primary")
    insert(conn, "practical_tasks", task_id=3, work_environment="spreadsheet")
    insert(conn, "practical_task_resources", id=1, exam_task_id=3,
           resource_path="data/reference/may_2025.zip::папка/файл 1.xlsx", sha256=SHA_A, file_size_bytes=5)
    insert(conn, "asset_links", id=1, asset_id=1, owner_type="exam", owner_id=1, role="source_pdf", display_order=0)
    insert(conn, "asset_links", id=2, asset_id=2, owner_type="question", owner_id=1, role="figure", display_order=0)
    insert(conn, "asset_links", id=3, asset_id=3, owner_type="exam_task", owner_id=2, role="figure", display_order=0)
    insert(conn, "official_exam_sources", id=1, exam_id=1, source_kind="pdf", source_url="https://example.invalid/x.pdf",
           local_path="data/reference/dzi/official_pdfs/may_2025.pdf", sha256=SHA_A)
    insert(conn, "dzi_blueprint_slots", id=1, blueprint_id=1, slot_number=1, exam_part=1, task_kind="multiple_choice",
           points=1, topic_id=1, section_id=1)
    insert(conn, "topic_aliases", id=1, alias_slug="ta-a", topic_id=1, action="merge")
    insert(conn, "topic_classes", topic_id=1, **{"class": 11})
    insert(conn, "topic_classes", topic_id=2, **{"class": 12})
    insert(conn, "topic_concepts", id=1, topic_id=1, concept="syntax")
    insert(conn, "topic_prerequisites", topic_id=2, requires_topic_id=1)
    insert(conn, "topic_section_assignments", id=1, topic_id=1, section_id=1, is_primary=1)
    insert(conn, "topic_section_assignments", id=2, topic_id=2, section_id=2, is_primary=1)
    insert(conn, "assessment_events", id=1, curriculum_id=1, module_id=1, section_id=1, event_slug="ev-1",
           title_bg="Проверка 1", **{"class": 11}, event_type="test")
    insert(conn, "assessment_event_sections", assessment_event_id=1, section_id=1, weight=1.0)


def build_mini_database(path: Path) -> None:
    load.migrated_empty_database(path)
    with closing(REAL_CONNECT(str(path), isolation_level=None)) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN")
        populate_mini(conn)
        conn.execute("COMMIT")


def mini_template() -> Path:
    if "mini" not in _SHARED:
        path = _shared_root() / "mini-template.db"
        build_mini_database(path)
        _SHARED["mini"] = path
    return _SHARED["mini"]


def mini_preimage_dir() -> Path:
    if "preimage" not in _SHARED:
        out = _shared_root() / "mini-preimage"
        with closing(REAL_CONNECT(str(mini_template()))) as conn:
            export.export_database(conn, out)
        _SHARED["preimage"] = out
    return _SHARED["preimage"]


def real_canonical_template() -> Path:
    """One database built from the tracked ``data/canonical`` (the control of case 1)."""
    if "real" not in _SHARED:
        path = _shared_root() / "real-canonical.db"
        load.rebuild_for_parity(path, CANONICAL_DIR)
        _SHARED["real"] = path
    return _SHARED["real"]


# ----------------------------------------------------------------------------- runtime population

def populate_runtime(conn, marker: str = "marker") -> None:
    """Rows in every non-canonical table (accounts of all roles, credentials, assignments, attempts...)."""
    mark = marker.lower()
    key = "k" * 40
    insert(conn, "users", id=1, email=f"{mark}-admin@example.test", display_name=f"{marker} Admin", role="admin",
           password_hash="scrypt$hash-admin", session_key=key)
    insert(conn, "users", id=2, email=f"{mark}-teacher@example.test", display_name=f"{marker} Teacher",
           role="teacher", password_hash="scrypt$hash-teacher", session_key=key + "t")
    insert(conn, "users", id=3, email=f"{mark}-student@example.test", display_name=f"{marker} Student",
           role="student", password_hash="scrypt$hash-student", session_key=key + "s")
    insert(conn, "totp_credentials", user_id=1, secret_ciphertext=f"cipher-{marker}", confirmed_at=FIXED_TS)
    insert(conn, "totp_recovery_codes", user_id=1, code_hash=f"recovery-{marker}-1")
    insert(conn, "totp_recovery_codes", user_id=1, code_hash=f"recovery-{marker}-2", used_at=FIXED_TS)
    insert(conn, "user_identities", user_id=3, issuer="https://issuer.example.test", subject=f"sub-{marker}",
           provider="google", email_at_link=f"{mark}-student@example.test")
    insert(conn, "user_profiles", user_id=3, account_type="student", first_name=f"{marker}First",
           last_name=f"{marker}Last", grade=11, grade_set_at=FIXED_TS)
    insert(conn, "user_profiles", user_id=2, account_type="teacher", first_name="Teach", last_name="Er",
           school_name=f"{marker} School")
    insert(conn, "teacher_subjects", user_id=2, position=1, subject_id="it", label=f"{marker} IT")
    insert(conn, "pending_registrations", binding_hash="1" * 64, account_type="student", state="pending", is_shadow=1,
           email=f"{mark}-pending@example.test", first_name=marker, last_name="Pending", grade=9,
           password_hash="scrypt$pending", created_at=FIXED_TS, expires_at=FIXED_TS)
    insert(conn, "password_recoveries", binding_hash="2" * 64, state="pending", is_shadow=1,
           email=f"{mark}-recover@example.test", created_at=FIXED_TS, expires_at=FIXED_TS)
    insert(conn, "registration_sends", email_hash="3" * 64, kind="code", attempted_at=FIXED_TS)
    insert(conn, "registration_control", id=1, high_water_at=FIXED_TS)
    insert(conn, "assignment_creations", id=1, owner_user_id=2, assignment_id=1, created_at=FIXED_TS)
    insert(conn, "quiz_assignments", id=1, section_id=1, title_bg=f"{marker} задание", question_count=2,
           created_at=FIXED_TS, owner_user_id=2, public_token="tok-" + "a" * 20)
    insert(conn, "quiz_assignments", id=2, section_id=2, title_bg="С план", question_count=2, created_at=FIXED_TS,
           owner_user_id=2, question_plan_json=json.dumps({"question_ids": [1, 2], "open_question_ids": [],
                                                           "mixed_open_enabled": False,
                                                           "include_open_answers_in_final_score": False}))
    insert(conn, "quiz_attempts", id=1, assignment_id=1, student_name=f"{marker} Ученик", seed="s1",
           question_ids_json="[1, 2]", started_at=FIXED_TS, submitted_at=FIXED_TS, score_correct=1, score_total=2,
           user_id=3)
    insert(conn, "quiz_attempts", id=2, assignment_id=2, student_name="Втори", seed="s2",
           question_ids_json=json.dumps({"question_ids": [1, 3]}), started_at=FIXED_TS)
    insert(conn, "quiz_answers", id=1, attempt_id=1, question_id=1, chosen_letter="A", is_correct=1)
    insert(conn, "quiz_answers", id=2, attempt_id=1, question_id=2, chosen_letter="B", is_correct=0)
    insert(conn, "quiz_text_answers", id=1, attempt_id=2, question_id=3, subquestion_id=None, subquestion_number=1,
           raw_answer=f"{marker}-answer")
    insert(conn, "practical_submissions", id=1, quiz_attempt_id=1, exam_task_id=3, status="submitted")
    insert(conn, "practical_submission_files", id=1, practical_submission_id=1, stored_path="uploads/1/a.xlsx",
           original_filename="a.xlsx")
    insert(conn, "practice_sessions", id=1, user_id=3, section_id=1, topic_id=None, seed=f"{marker}-p1",
           question_ids_json="[1, 2]", started_at=FIXED_TS, submitted_at=FIXED_TS, score_correct=1, score_total=2)
    insert(conn, "practice_sessions", id=2, user_id=3, section_id=1, topic_id=1, seed="p2",
           question_ids_json=json.dumps([1, 3]), started_at=FIXED_TS)
    insert(conn, "practice_sessions", id=3, user_id=2, section_id=2, topic_id=2, seed="p3",
           question_ids_json="[2]", started_at=FIXED_TS, abandoned_at=FIXED_TS)
    insert(conn, "practice_answers", session_id=1, question_id=1, chosen_letter="A", is_correct=1)
    insert(conn, "practice_answers", session_id=1, question_id=2, chosen_letter=None, is_correct=0)
    # DZI Part 1 attempts in every terminal state; the plan is flat ids, the slot format separate (migration 027).
    part1 = dict(blueprint_id=1, duration_seconds=5400, max_points_planned=4, started_at=FIXED_TS,
                 deadline_at="2026-01-01 01:30:00", submit_cutoff_at="2026-01-01 01:31:00",
                 slot_format_json='[{"kind": "multiple_choice", "points": 1}, {"kind": "short_answer", "points": 3}]')
    insert(conn, "dzi_part1_attempts", id=1, user_id=3, seed=f"{marker}-d1", question_ids_json="[1, 2]",
           submitted_at="2026-01-01 00:20:00", score_points=3, score_max=4, **part1)
    insert(conn, "dzi_part1_attempts", id=2, user_id=3, seed="d2", question_ids_json="[1, 3]", **part1)
    insert(conn, "dzi_part1_attempts", id=3, user_id=2, seed="d3", question_ids_json="[2, 3]",
           expired_at="2026-01-01 02:00:00", **part1)
    insert(conn, "dzi_part1_attempts", id=4, user_id=2, seed="d4", question_ids_json="[3, 1]",
           abandoned_at="2025-12-31 23:00:00", **part1)       # earlier than started_at: allowed (design 7.4)
    insert(conn, "dzi_part1_answers", attempt_id=1, slot_number=1, subquestion_number=0, question_id=1,
           kind="multiple_choice", chosen_letter="A", is_correct=1, points_possible=1, points_awarded=1,
           grader_version="fixture-1")
    insert(conn, "dzi_part1_answers", attempt_id=1, slot_number=2, subquestion_number=1, question_id=2,
           kind="short_answer", raw_answer=f"{marker}-raw", is_correct=1, points_possible=2, points_awarded=2,
           grader_version="fixture-1")
    insert(conn, "dzi_part1_answers", attempt_id=1, slot_number=2, subquestion_number=2, question_id=2,
           kind="short_answer", raw_answer=None, is_correct=0, points_possible=1, points_awarded=0,
           grader_version="fixture-1")
    insert(conn, "generated_exams", id=1, exam_name="gen", question_ids="[1, 2, 3]")
    insert(conn, "login_throttle", identifier_hash="4" * 64, failure_count=2)
    insert(conn, "oidc_consumed_transactions", tx_hash="5" * 64, expires_at=123)
    insert(conn, "obsidian_notes", id=1, file_path="Topics/topic-a.md", note_type="topic", title="Тема А", topic_id=1)
    insert(conn, "note_question_links", id=1, question_id=1, note_id=1)
    insert(conn, "scrape_log", id=1, source="example.test", url="https://example.test/a", status="success")


# ----------------------------------------------------------------------------- scenarios

class Workspace:
    """A private temporary directory with targets, preimages and sources built on demand."""

    def __init__(self, test: unittest.TestCase, prefix: str = "ws"):
        self.root = Path(tempfile.mkdtemp(prefix=f"learnpilot-updater-{prefix}-", dir=_shared_root()))
        test.addCleanup(shutil.rmtree, self.root, True)
        self._count = 0

    def path(self, name: str) -> Path:
        return self.root / name

    def target(self, name: str = "target.db", *, runtime: bool = False, marker: str = "marker", wal: bool = False,
               template: Path | None = None, mutate=None, foreign_keys: bool = True) -> Path:
        path = self.root / name
        shutil.copyfile(template or mini_template(), path)
        with closing(REAL_CONNECT(str(path), isolation_level=None)) as conn:
            conn.execute(f"PRAGMA foreign_keys={'ON' if foreign_keys else 'OFF'}")
            if mutate is not None or runtime:
                conn.execute("BEGIN")
                if runtime:
                    populate_runtime(conn, marker)
                if mutate is not None:
                    mutate(conn)
                conn.execute("COMMIT")
            if wal:
                conn.execute("PRAGMA journal_mode=WAL")
        return path

    def preimage(self, name: str = "preimage") -> Path:
        destination = self.root / name
        shutil.copytree(mini_preimage_dir(), destination)
        return destination

    def source(self, mutate=None, *, name: str | None = None, base: Path | None = None,
               migrations_dir: Path = ct.MIGRATIONS_DIR) -> Path:
        """A canonical directory: the export of ``base`` (default: the mini template) after ``mutate``."""
        self._count += 1
        label = name or f"source-{self._count}"
        scratch = self.root / f"{label}.db"
        shutil.copyfile(base or mini_template(), scratch)
        with closing(REAL_CONNECT(str(scratch), isolation_level=None)) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            if mutate is not None:
                conn.execute("BEGIN")
                mutate(conn)
                conn.execute("COMMIT")
            out = self.root / label
            export.export_database(conn, out, migrations_dir=migrations_dir)
        scratch.unlink()
        return out


# ----------------------------------------------------------------------------- mutation helpers (new rows)

def add_new_family(conn, *, base: int = 100) -> dict:
    """A permitted addition: a new exam with tasks, a new question with options and a visual asset."""
    ids = {"exam": base, "task_mc": base, "task_pr": base + 1, "question": base, "asset": base}
    insert(conn, "exams", id=base, subject="informatika_it", level="DZI", year=2026, session="may", variant=1,
           source_file="data/reference/dzi/official_pdfs/may_2026.pdf", sha256="c" * 64)
    insert(conn, "questions", id=base, exam_id=base, source_exam="may_2026_v1", source_number=1,
           question_type="multiple_choice", points=1, prompt="Нов въпрос", topic_id=1)
    for offset, letter in enumerate("AB"):
        insert(conn, "multiple_choice_options", id=base + offset, question_id=base, option_letter=letter,
               option_text=f"Нов отговор {letter}", is_correct=1 if letter == "A" else 0)
    insert(conn, "exam_tasks", id=base, exam_id=base, task_number=1, exam_part=1, task_kind="multiple_choice", points=1)
    insert(conn, "exam_tasks", id=base + 1, exam_id=base, task_number=26, exam_part=2,
           task_kind="practical_spreadsheet", points=15)
    insert(conn, "exam_task_questions", task_id=base, question_id=base, role="primary")
    insert(conn, "practical_tasks", task_id=base + 1, work_environment="spreadsheet")
    insert(conn, "practical_task_resources", id=base, exam_task_id=base + 1,
           resource_path="data/reference/may_2026.zip::нов/файл.xlsx", sha256=SHA_B, file_size_bytes=7)
    insert(conn, "assets", id=base, asset_type="image", local_path="web/static/img/questions/dzi/new.png", sha256=SHA_A)
    insert(conn, "asset_links", id=base, asset_id=base, owner_type="question", owner_id=base, role="figure",
           display_order=0)
    insert(conn, "question_topic_assignments", id=base, question_id=base, topic_id=1, section_id=1,
           assignment_type="primary")
    insert(conn, "fill_in_subquestions", id=base, question_id=base, subquestion_number=1, correct_answer="1")
    return ids


def add_topic(conn, topic_id: int, *, section_id: int | None = 1, slug: str | None = None) -> None:
    insert(conn, "curriculum_topics", id=topic_id, topic_slug=slug or f"topic-{topic_id}", title_bg=f"Тема {topic_id}",
           area_id=1, module_id=1, section_id=section_id)


def add_question(conn, question_id: int, *, number: int | None = None, exam_id: int | None = None,
                 topic_id: int | None = 1, prompt: str | None = None, source_exam: str = "extra") -> None:
    insert(conn, "questions", id=question_id, exam_id=exam_id, source_exam=source_exam, source_number=number,
           question_type="multiple_choice", points=1, prompt=prompt or f"Допълнителен въпрос {question_id}",
           topic_id=topic_id)


def raise_sequence(conn, table: str, value: int) -> None:
    row = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = ?", (table,)).fetchone()
    if row is None:
        conn.execute("INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)", (table, value))
    else:
        conn.execute("UPDATE sqlite_sequence SET seq = ? WHERE name = ?", (value, table))


# ----------------------------------------------------------------------------- source and fixture editing

def edit_manifest(directory, change) -> None:
    path = Path(directory) / ct.MANIFEST_NAME
    manifest = json.loads(path.read_text(encoding="utf-8"))
    change(manifest)
    path.write_text(export.manifest_text(manifest), encoding="utf-8", newline="\n")


def table_path(directory, table: str) -> Path:
    return Path(directory) / f"{table}.jsonl"


def read_rows(directory, table: str) -> list[dict]:
    return list(ct.read_table_rows(table_path(directory, table)))


def write_rows(directory, table: str, rows: list[dict], *, keep_sequence: bool = False) -> None:
    """Rewrite one canonical file and bring the manifest entry (rows, sha256, max_id, sequence) in line."""
    lines = [ct.serialize_row(list(row), row.values()) for row in rows]
    table_path(directory, table).write_text("".join(line + "\n" for line in lines), encoding="utf-8", newline="\n")
    count, digest = ct.hash_lines(lines)

    def change(manifest):
        entry = manifest["tables"][table]
        entry["rows"], entry["sha256"] = count, digest
        keys = entry["primary_key"]
        ids = [row[keys[0]] for row in rows] if len(keys) == 1 and rows and isinstance(rows[0][keys[0]], int) else []
        entry["max_id"] = max(ids) if ids else None
        if entry["autoincrement"] and ids and not keep_sequence:
            entry["sqlite_sequence"] = max(entry["sqlite_sequence"] or 0, max(ids))
    edit_manifest(directory, change)


def tamper_table(directory, table: str, transform, **kwargs) -> None:
    rows = read_rows(directory, table)
    new_rows = transform(rows)
    write_rows(directory, table, rows if new_rows is None else new_rows, **kwargs)


def fixture_migrations(ws: "Workspace", extra: dict[str, str], name: str = "migrations") -> Path:
    """A copy of the repository migrations plus extra numbered files (a stand-in for a later schema)."""
    directory = ws.root / name
    shutil.copytree(ct.MIGRATIONS_DIR, directory)
    for filename, sql in extra.items():
        (directory / filename).write_text(sql, encoding="utf-8")
    return directory


def custom_base(ws: "Workspace", migrations_dir: Path, name: str = "custom-base.db") -> Path:
    """A mini canonical database migrated with ``migrations_dir`` instead of the repository migrations."""
    from src import manage_migrations
    path = ws.root / name
    REAL_CONNECT(str(path)).close()
    if manage_migrations.apply_migrations(path, Path(migrations_dir), out=io.StringIO()) != 0:
        raise AssertionError("fixture migrations failed")
    with closing(REAL_CONNECT(str(path), isolation_level=None)) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN")
        populate_mini(conn)
        conn.execute("COMMIT")
    return path


def export_database_dir(ws: "Workspace", database: Path, name: str, migrations_dir: Path = ct.MIGRATIONS_DIR) -> Path:
    out = ws.root / name
    with closing(REAL_CONNECT(str(database))) as conn:
        export.export_database(conn, out, migrations_dir=migrations_dir)
    return out


# ----------------------------------------------------------------------------- the shared test case

class UpdaterTestCase(unittest.TestCase):
    """Installs the replacement runtime-database guard and a private workspace for every test."""

    def setUp(self):
        self.spy = install_connect_spy(self)
        self.ws = Workspace(self)

    # running ---------------------------------------------------------------
    def dry(self, target, source, preimage=None, **kwargs) -> Result:
        return run_tool(target, source, preimage=preimage, **kwargs)

    def plan_of(self, target, source, preimage, **kwargs) -> Result:
        result = run_tool(target, source, preimage=preimage, **kwargs)
        self.assertEqual(result.exit, 0, result.out)
        self.assertIsNotNone(result.report["plan_sha256"])
        return result

    def apply(self, target, source, preimage, token, **kwargs) -> Result:
        return run_tool(target, source, preimage=preimage, apply_hash=token, **kwargs)

    # assertions -------------------------------------------------------------
    def assert_clean(self, result: Result) -> None:
        """No path, temporary directory or exception text may appear in what the tool printed."""
        for text in (result.out, result.err):
            self.assertNotIn(str(_shared_root()), text)
            self.assertNotIn("learnpilot-updater-", text)
            self.assertNotIn("Traceback", text)
            self.assertNotIn("sqlite3.", text)

    def assert_refused(self, result: Result, exit_code: int, code: str, *, status: str = "refused",
                       row_codes=()) -> None:
        self.assertEqual((result.exit, result.report["status"], result.report["run_code"]),
                         (exit_code, status, code), result.out)
        by_code = (result.report.get("conflicts") or {}).get("by_code", {})
        for row_code in row_codes:
            self.assertIn(row_code, by_code, result.out)
        self.assert_clean(result)

    def assert_untouched(self, target, before: dict) -> None:
        self.assertEqual(db_snapshot(target), before)


def stdlib_modules_only(source: str) -> bool:
    """True when ``source`` imports nothing that is not standard library or a repository package."""
    import ast
    allowed = {"src", "web", "tests", "__future__"}
    for node in ast.walk(ast.parse(source)):
        names = []
        if isinstance(node, ast.Import):
            names = [alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names = [node.module.split(".")[0]]
        for name in names:
            if name not in allowed and name not in sys_stdlib():
                return False
    return True


def sys_stdlib() -> frozenset:
    import sys
    return frozenset(getattr(sys, "stdlib_module_names", ()))
