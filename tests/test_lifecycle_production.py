"""Assignment lifecycle and production start-up (docs/ASSIGNMENT_LIFECYCLE_DESIGN.md, 12.6, 13.2).

A lifecycle-aware build must fail closed instead of running without enforcement:
it refuses a schema newer than itself, a schema older than itself, an unusable
clock row and a missing timezone database. It still starts with a ``suspect``
clock, where read pages work and lifecycle writes are refused. Each start-up runs
in a fresh interpreter. Case ids refer to section 15.1 (L94, L95, L96, L108-L110).
"""

import json
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import timedelta
from pathlib import Path

from src import manage_migrations
from tests.lifecycle_support import GRACE, LifecycleTestCase, ts
from tests.support import ROOT
from tests.test_production_deployment import FAILING_STARTUP_PROBE, _run_python, _sha256, _state_dir
from web import production
from web.services import lifecycle

MIGRATIONS = ROOT / "web" / "migrations"

RUNNING_PROBE = r'''
import json
from web.wsgi import app
from web.services import lifecycle
proxy = {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "learnpilot.example.com",
         "X-Forwarded-For": "203.0.113.7"}
client = app.test_client()
health = client.get("/healthz", headers=proxy)
home = client.get("/", headers=proxy)
print(json.dumps({"environment": lifecycle.clock_environment().name,
                  "health": [health.status_code, health.get_data(as_text=True)], "home": home.status_code}))
'''

DEVELOPMENT_PROBE = r'''
from web.services import lifecycle
before = lifecycle.clock_environment().name
from web import app
print(before, lifecycle.clock_environment().name)
'''


class ProductionLifecycleStartupTest(unittest.TestCase):
    def assert_refuses(self, env, *expected):
        db = Path(env["DZI_DB"])
        before = _sha256(db)
        result = _run_python(FAILING_STARTUP_PROBE, env)
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 3, output)
        self.assertIn("ProductionConfigError", output)
        for text in expected:
            self.assertIn(text, output)
        self.assertIn("db unchanged: True", output)
        self.assertEqual(_sha256(db), before)  # start-up never repairs anything
        return output

    def execute(self, env, *statements, ignore_checks=False):
        with closing(sqlite3.connect(env["DZI_DB"])) as conn:
            if ignore_checks:
                conn.execute("PRAGMA ignore_check_constraints = ON")
            for statement in statements:
                conn.execute(statement)
            conn.commit()

    def test_l94_a_schema_newer_than_the_build_stops_startup(self):
        env = _state_dir(self, populated=True)
        self.execute(env, "INSERT INTO schema_migrations (filename) VALUES ('019_history.sql')",
                     "INSERT INTO schema_migrations (filename) VALUES ('020_cleanup.sql')")
        output = self.assert_refuses(
            env, "applied migrations this build does not ship: 019_history.sql, 020_cleanup.sql",
            "older than the schema")
        self.assertIn("must never serve this database", output)
        self.assertEqual(
            production.unknown_schema_migrations(env["DZI_DB"], lambda path: sqlite3.connect(path)),
            ["019_history.sql", "020_cleanup.sql"])

    def test_l95_a_schema_older_than_the_build_stops_startup(self):
        env = _state_dir(self, populated=False)
        db = Path(env["DZI_DB"])
        db.unlink()
        sqlite3.connect(db).close()
        with tempfile.TemporaryDirectory() as tmp:
            for path in sorted(MIGRATIONS.glob("*.sql")):
                if path.name < "018_":
                    shutil.copyfile(path, Path(tmp) / path.name)
            self.assertEqual(manage_migrations.apply_migrations(db, Path(tmp), out=__import__("io").StringIO()), 0)
        self.assert_refuses(env, "pending migrations: 018_assignment_lifecycle.sql", "src.manage_migrations")

    def test_l109_a_missing_clock_row_stops_startup_and_is_not_reseeded(self):
        env = _state_dir(self, populated=True)
        self.execute(env, "DELETE FROM lifecycle_clock")
        self.assert_refuses(env, "lifecycle clock row is unusable", "0 rows", "clock-recover",
                            "never by re-running a migration or a seed")
        with closing(sqlite3.connect(env["DZI_DB"])) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM lifecycle_clock").fetchone()[0], 0)

    def test_l110_a_malformed_clock_row_stops_startup(self):
        cases = {
            "UPDATE lifecycle_clock SET high_water_at = 'not a time'": "malformed timestamp",
            "UPDATE lifecycle_clock SET state = 'fine'": "unknown state",
            "UPDATE lifecycle_clock SET ref_boot_id = 'boot'": "incomplete boot anchor",
            "INSERT INTO lifecycle_clock (id, high_water_at) VALUES (2, '2026-01-01 00:00:00')": "2 rows",
        }
        for statement, problem in cases.items():
            with self.subTest(statement=statement):
                env = _state_dir(self, populated=True)
                self.execute(env, statement, ignore_checks=True)
                self.assert_refuses(env, "lifecycle clock row is unusable", problem)

    def test_a_restore_that_is_in_progress_or_unfinished_stops_startup(self):
        # Claude review of PR #326, H1: after a restore was interrupted, systemd started the enabled
        # unit again (a reboot is enough) on a live state that was only partly replaced.
        env = _state_dir(self, populated=True)
        lock = Path(lifecycle.restore_lock_path(env["DZI_DB"]))
        self.assertEqual((lock.parent, lock.name), (Path(env["DZI_DB"]).parent, "restore-in-progress"))
        self.assertIsNone(production.restore_lock_error(env))
        lock.mkdir()
        output = self.assert_refuses(
            env, "a restore of this database is in progress or was left unfinished", str(lock),
            "run it again from the beginning", "Do not remove the lock by hand")
        self.assertNotIn("Traceback (most recent call last):\n  File \"<string>\", line 1", output.replace("\r", ""))
        # It is the first thing looked at: the rest of the state may be half replaced.
        shutil.rmtree(env["DZI_UPLOAD_ROOT"])
        self.assertIn("is in progress or was left unfinished", production.restore_lock_error(env))
        with self.assertRaises(production.ProductionConfigError) as caught:
            production.create_production_app(env)
        self.assertIn("a restore of this database is in progress", str(caught.exception))
        self.assertNotIn("DZI_UPLOAD_ROOT", str(caught.exception))
        # An empty file, a dangling link: whatever is there under that name forbids the start.
        lock.rmdir()
        lock.symlink_to(lock.parent / "nothing-here")
        self.assertIsNotNone(production.restore_lock_error(env))
        lock.unlink()
        Path(env["DZI_UPLOAD_ROOT"]).mkdir()
        # The lock gone, the same database starts.
        self.assertIsNone(production.restore_lock_error(env))
        result = _run_python(RUNNING_PROBE, env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout.strip().splitlines()[-1])["health"], [200, "ok\n"])

    def test_l108_a_suspect_clock_still_starts_and_reports_degraded(self):
        env = _state_dir(self, populated=True)
        self.execute(env, "UPDATE lifecycle_clock SET state = 'suspect', state_reason = 'clock_step', "
                          "state_changed_at = '2026-03-02 09:00:00'")
        result = _run_python(RUNNING_PROBE, env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(report["health"], [503, "degraded\n"])
        self.assertEqual(report["home"], 200)  # read pages keep working
        with closing(sqlite3.connect(env["DZI_DB"])) as conn:  # and nothing cleared the state
            self.assertEqual(conn.execute("SELECT state FROM lifecycle_clock").fetchone()[0], "suspect")

    def test_production_uses_the_hosts_real_clock_evidence(self):
        env = _state_dir(self, populated=True)
        result = _run_python(RUNNING_PROBE, env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(report["environment"], "system")
        self.assertEqual(report["health"], [200, "ok\n"])
        # Only a direct import of web.app (development, manual smoke tests) relaxes it.
        development = _run_python(DEVELOPMENT_PROBE, {"DZI_DB": env["DZI_DB"]})
        self.assertEqual(development.stdout.strip().splitlines()[-1], "system development", development.stderr)

    def test_the_timezone_database_is_required(self):
        self.assertIsNone(production.timezone_data_problem())
        from unittest.mock import patch
        with patch.object(lifecycle, "timezone_names", return_value=("UTC",)):
            self.assertIn("does not know Europe/Sofia", production.timezone_data_problem())
        with patch.object(lifecycle, "timezone_names", side_effect=lifecycle.ZoneDataUnavailable("none")):
            self.assertIn("not available", production.timezone_data_problem())


class LifecycleLoggingTest(unittest.TestCase):
    def test_production_sends_lifecycle_events_to_the_journal_once(self):
        import logging
        logger = logging.getLogger(production.LIFECYCLE_LOGGER)
        saved = (list(logger.handlers), logger.level, logger.propagate)
        try:
            logger.handlers = []
            production.configure_lifecycle_logging()
            production.configure_lifecycle_logging()  # idempotent
            self.assertEqual(len(logger.handlers), 1)
            self.assertEqual((logger.level, logger.propagate), (logging.INFO, False))
            self.assertIs(lifecycle.logger, logger)
        finally:
            logger.handlers, logger.level, logger.propagate = saved


class SystemClockEnvironmentTest(unittest.TestCase):
    def test_unreadable_evidence_is_reported_as_unknown_never_as_synchronised(self):
        environment = lifecycle.SystemClockEnvironment()
        environment.BOOT_ID_PATH = "/nonexistent/boot_id"
        self.assertIsNone(environment.boot_id())
        self.assertIn(environment.synchronized(), (None, True, False))
        import sys
        if not sys.platform.startswith("linux"):
            self.assertIsNone(environment.synchronized())  # no probe: lifecycle writes need the operator
        self.assertEqual(lifecycle.DevelopmentClockEnvironment().synchronized(), True)
        self.assertEqual((environment.name, lifecycle.DevelopmentClockEnvironment.name), ("system", "development"))

    def test_the_boot_id_is_read_and_stripped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "boot_id"
            path.write_text("0f1e2d3c-aaaa-bbbb-cccc-1234567890ab\n", encoding="ascii")
            environment = lifecycle.SystemClockEnvironment()
            environment.BOOT_ID_PATH = str(path)
            self.assertEqual(environment.boot_id(), "0f1e2d3c-aaaa-bbbb-cccc-1234567890ab")
            path.write_text("\n", encoding="ascii")
            self.assertIsNone(environment.boot_id())


class PreLifecycleCodeTest(LifecycleTestCase):
    """L96. What code from before PR #326 does on the migrated schema: it runs, and
    it ignores closure. That it runs is a hazard, not a rollback path, which is why
    section 13.2 forbids such a build from ever serving this database again."""

    # The statements of the release before PR #326 (web/services/quiz.py at 8c443b7).
    OLD_START = ("INSERT INTO quiz_attempts (assignment_id, user_id, student_name, seed, question_ids_json, "
                 "score_total) VALUES (?, ?, ?, ?, ?, ?)")
    OLD_SUBMIT = "UPDATE quiz_attempts SET submitted_at = CURRENT_TIMESTAMP, score_correct = ?, score_total = ? WHERE id = ?"
    OLD_EDIT = "UPDATE quiz_assignments SET title_bg = ?, time_limit_minutes = ? WHERE id = ?"

    def test_l96_pre_lifecycle_statements_run_on_the_new_schema_and_ignore_closure(self):
        teacher, student = self.account("teacher"), self.account()
        closed_at = self.T0 - timedelta(hours=5)
        assignment_id = self.assignment(teacher, closed_at=closed_at, submissions_closed_at=closed_at + GRACE,
                                        title="Затворен")
        with closing(sqlite3.connect(str(self.db_path))) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            attempt_id = conn.execute(self.OLD_START, (assignment_id, student["id"], "Ученик", "seed", "[]", 0)).lastrowid
            conn.execute(self.OLD_SUBMIT, (0, 0, attempt_id))
            conn.execute(self.OLD_EDIT, ("Променен след затваряне", 5, assignment_id))
            conn.commit()
        # Nothing in the schema stopped any of it: a start, a submission and an edit on a closed assignment.
        self.assertEqual(self.row(assignment_id)["title_bg"], "Променен след затваряне")
        self.assertIsNotNone(self.attempt_row(attempt_id)["submitted_at"])
        self.assertEqual(self.row(assignment_id)["closed_at"], ts(closed_at))  # it cannot touch what it does not know

        # Enforcing code refuses the same three things ...
        self.assertEqual(self.start(self.client(self.account()), assignment_id).status_code, 409)
        owner = self.client(teacher)
        edit = owner.post(f"/teacher/assignment/{assignment_id}/edit",
                          data={"title_bg": "Още веднъж", **self.no_deadline_form()},
                          headers={"Origin": "http://localhost"})
        self.assertEqual(edit.status_code, 409)
        # ... and the exposure audit finds what the old code let through.
        code, out, _err = self.cli("exposure-audit", "--from", ts(closed_at), "--to", ts(self.T0))
        self.assertEqual(code, 1)
        self.assertIn(f"attempt {attempt_id} (assignment {assignment_id}): started", out)
        self.assertIn("after the closure", out)


if __name__ == "__main__":
    unittest.main()
