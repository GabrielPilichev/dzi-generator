"""Shared fixtures for the assignment lifecycle tests (PR #326).

Every test gets its own temporary database, built from migrations + canonical
content, and runs at a fixed lifecycle time: ``lifecycle_time`` moves the
application clock, the boot-time clock and the database's clock row together.
Rows are created directly with explicit values where a test needs a precise
state; the application's own forms are used where the form is what is tested.

Case ids (``L27``) refer to the acceptance matrix in
``docs/ASSIGNMENT_LIFECYCLE_DESIGN.md``, section 15.1.
"""

from __future__ import annotations

import io
import json
import sqlite3
import unittest
from contextlib import ExitStack, closing
from datetime import datetime, timedelta
from pathlib import Path

from src import manage_lifecycle
from tests.auth_helpers import create_account, set_account_session
from tests.support import isolated_app, lifecycle_time, student_entry_path, web_app
from web.assignment_tokens import new_public_token
from web.services import lifecycle
from web.services import quiz as quiz_service

SAME_ORIGIN = {"Origin": "http://localhost"}
ts = lifecycle.format_timestamp
GRACE = timedelta(seconds=lifecycle.GRACE_SECONDS)

_section_cache: dict = {}


def eligible_section(conn) -> tuple[int, list[int]]:
    """A canonical section with at least three eligible questions, and three of them."""
    if "section" not in _section_cache:
        for row in conn.execute("SELECT id FROM curriculum_sections ORDER BY id"):
            ids = quiz_service.quiz_section_question_ids(conn, int(row["id"]))
            if len(ids) >= 3:
                _section_cache["section"] = (int(row["id"]), [int(qid) for qid in ids[:3]])
                break
        else:
            raise AssertionError("no canonical section with three eligible questions")
    return _section_cache["section"]


class LifecycleTestCase(unittest.TestCase):
    """A fresh database and a frozen lifecycle clock for every test."""

    # 2026-03-02 09:00 UTC: 11:00 in Europe/Sofia (UTC+2), four weeks before DST starts.
    T0 = datetime(2026, 3, 2, 9, 0, 0)
    ZONE = "Europe/Sofia"

    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.root = stack.enter_context(isolated_app())
        web_app.app.config.update(TESTING=True)
        self.db_path = Path(web_app.app.config["DB_PATH"])
        self.clock = stack.enter_context(lifecycle_time(self.T0, self.db_path))
        with closing(web_app.quiz_db()) as conn:
            self.section_id, self.question_ids = eligible_section(conn)
        self.audit_log = self.root / "lifecycle-operator.log"

    # ----------------------------------------------------------------- database

    def query(self, sql, params=()):
        with closing(web_app.quiz_db()) as conn:
            return conn.execute(sql, params).fetchall()

    def execute(self, sql, params=()):
        with closing(sqlite3.connect(str(self.db_path))) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            cursor = conn.execute(sql, params)
            conn.commit()
            return cursor.lastrowid

    def row(self, assignment_id):
        return self.query("SELECT * FROM quiz_assignments WHERE id = ?", (assignment_id,))[0]

    def lifecycle_columns(self, assignment_id) -> dict:
        row = self.row(assignment_id)
        return {column: row[column] for column in lifecycle.LIFECYCLE_COLUMNS}

    def clock_row(self):
        return self.query("SELECT * FROM lifecycle_clock")[0]

    def attempt_row(self, attempt_id):
        return self.query("SELECT * FROM quiz_attempts WHERE id = ?", (attempt_id,))[0]

    def attempts(self, assignment_id):
        return self.query("SELECT * FROM quiz_attempts WHERE assignment_id = ? ORDER BY id", (assignment_id,))

    def answer_count(self, attempt_id) -> int:
        return self.query("SELECT COUNT(*) FROM quiz_answers WHERE attempt_id = ?", (attempt_id,))[0][0]

    def table_counts(self) -> dict:
        tables = ("quiz_assignments", "quiz_attempts", "quiz_answers", "quiz_text_answers",
                  "practical_submissions", "practical_submission_files")
        return {table: self.query(f"SELECT COUNT(*) FROM {table}")[0][0] for table in tables}

    # ----------------------------------------------------------------- accounts

    def account(self, role="student", **options) -> dict:
        return create_account(self.db_path, role, **options)

    def client(self, account=None):
        client = web_app.app.test_client()
        if account:
            set_account_session(client, account["id"], account["session_key"])
        return client

    # ----------------------------------------------------------------- fixtures

    def assignment(self, owner=None, *, deadline_at=None, zone="Europe/Sofia", title="Тест за срокове",
                   time_limit=None, **columns) -> int:
        """An assignment row with an explicit lifecycle state. Datetimes are serialised
        strictly; a string is stored as given, so a test can write a malformed value."""
        def stored(value):
            return ts(value) if isinstance(value, datetime) else value

        names = ["section_id", "title_bg", "question_count", "time_limit_minutes", "created_at",
                 "owner_user_id", "public_token", "deadline_at", "display_timezone"]
        values = [self.section_id, title, 3, time_limit, ts(self.T0 - timedelta(days=1)),
                  None if owner is None else owner["id"], new_public_token(), stored(deadline_at), zone]
        for name, value in columns.items():
            names.append(name)
            values.append(stored(value))
        placeholders = ", ".join("?" for _ in names)
        return int(self.execute(
            f"INSERT INTO quiz_assignments ({', '.join(names)}) VALUES ({placeholders})", values))

    def attempt(self, assignment_id, account=None, *, started_at, submitted_at=None, name="Ученик") -> int:
        """An attempt row with explicit timestamps; without an account it is historical."""
        def stored(value):
            return ts(value) if isinstance(value, datetime) else value

        return int(self.execute(
            "INSERT INTO quiz_attempts (assignment_id, user_id, student_name, seed, question_ids_json, "
            "started_at, submitted_at, score_correct, score_total) VALUES (?, ?, ?, 'seed', ?, ?, ?, ?, ?)",
            (assignment_id, None if account is None else account["id"], name, json.dumps(self.question_ids),
             stored(started_at), stored(submitted_at), 0 if submitted_at else None, len(self.question_ids))))

    def entry(self, assignment_id) -> str:
        return student_entry_path(assignment_id)

    def start(self, client, assignment_id):
        return client.post(self.entry(assignment_id), headers=SAME_ORIGIN)

    def answers(self, attempt_id, letter="A") -> dict:
        """One answered question of the attempt's own plan (a started attempt picks its questions)."""
        plan = json.loads(self.attempt_row(attempt_id)["question_ids_json"])
        question_ids = plan["question_ids"] if isinstance(plan, dict) else plan
        return {f"q_{int(question_ids[0])}": letter}

    def submit(self, client, attempt_id, data=None):
        return client.post(f"/quiz/attempt/{attempt_id}",
                           data=self.answers(attempt_id) if data is None else data, headers=SAME_ORIGIN)

    @staticmethod
    def attempt_id_of(response) -> int:
        return int(response.headers["Location"].rsplit("/", 1)[1])

    # ----------------------------------------------------------------- forms

    def deadline_form(self, day, clock="23:59", zone="Europe/Sofia") -> dict:
        return {"deadline_choice": "deadline", "deadline_date": day, "deadline_time": clock,
                "display_timezone": zone}

    def no_deadline_form(self, zone="Europe/Sofia") -> dict:
        return {"deadline_choice": "none", "display_timezone": zone}

    def new_assignment_form(self, **fields) -> dict:
        return {"section_id": self.section_id, "question_count": 3, **fields}

    def create(self, client, **fields):
        return client.post("/teacher/new", data=self.new_assignment_form(**fields), headers=SAME_ORIGIN)

    @staticmethod
    def created_id(response) -> int:
        return int(response.headers["Location"].rsplit("/", 1)[1])

    # ----------------------------------------------------------------- operator CLI

    def cli(self, *args, writer=False, db=None):
        """Run ``src.manage_lifecycle`` in-process: ``(exit code, stdout, stderr)``."""
        argv = ["--db", str(db or self.db_path), *map(str, args)]
        if writer:
            argv += ["--operator", "тест оператор", "--audit-log", str(self.audit_log)]
        out, err = io.StringIO(), io.StringIO()
        code = manage_lifecycle.main(argv, out=out, err=err)
        return code, out.getvalue(), err.getvalue()

    def audit_entries(self) -> list[dict]:
        if not self.audit_log.exists():
            return []
        return [json.loads(line) for line in self.audit_log.read_text(encoding="utf-8").splitlines()]
