"""src.manage_assignment_tokens: the explicit backfill of quiz_assignments.public_token.

Runs against temporary databases built by the migration runner. No token is ever
expected in the command's output.
"""

import io
import re
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from src import manage_assignment_tokens as cli
from src import manage_migrations
from tests.support import ROOT
from web import assignment_tokens

MIGRATIONS = ROOT / "web" / "migrations"
EXISTING_TOKEN = "E" * 43
COLLIDING_TOKEN = "C" * 43
FRESH_TOKEN = "F" * 43

POPULATE_SQL = """
    INSERT INTO curriculum_sections (id, section_slug, title_bg, class, section_type)
        VALUES (11, 'legacy', 'Стар раздел', 12, 'section');
    INSERT INTO users (id, display_name, role, session_key) VALUES
        (51, 'Учител', 'teacher', '12345678901234567890123456789012'),
        (52, 'Ученик', 'student', '22345678901234567890123456789012');
    INSERT INTO quiz_assignments (id, section_id, title_bg, question_count, question_plan_json, owner_user_id)
        VALUES (71, 11, 'Първи', 2, '{"question_ids":[1]}', 51),
               (72, 11, 'Втори', 1, NULL, NULL),
               (73, 11, 'Трети', 1, NULL, 51);
    INSERT INTO quiz_attempts (id, assignment_id, user_id, student_name, seed, question_ids_json)
        VALUES (91, 71, 52, 'Ученик', 'seed', '[1]');
"""


class BackfillTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        directory = tempfile.TemporaryDirectory(prefix="assignment-token-backfill-")
        cls.addClassCleanup(directory.cleanup)
        cls.class_root = Path(directory.name)
        cls.template = cls.class_root / "template.db"
        sqlite3.connect(cls.template).close()
        if manage_migrations.apply_migrations(cls.template, MIGRATIONS, out=io.StringIO()) != 0:
            raise AssertionError("could not build the template database")
        with closing(sqlite3.connect(cls.template)) as conn:
            conn.executescript(POPULATE_SQL)
            conn.commit()

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="assignment-token-backfill-")
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "runtime.db"
        shutil.copyfile(self.template, self.path)

    def run_cli(self, *args, path=None):
        out, err = io.StringIO(), io.StringIO()
        with patch.dict("os.environ", {}, clear=False) as environ:
            environ.pop("DZI_DB", None)
            code = cli.main(["--db", str(path or self.path), *args], out=out, err=err)
        return code, out.getvalue(), err.getvalue()

    def rows(self, sql, params=()):
        with closing(sqlite3.connect(self.path)) as conn:
            return conn.execute(sql, params).fetchall()

    def execute(self, sql, params=()):
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute(sql, params)
            conn.commit()

    def tokens(self):
        return dict(self.rows("SELECT id, public_token FROM quiz_assignments ORDER BY id"))

    def everything_else(self):
        """Every column and table the backfill must leave alone."""
        with closing(sqlite3.connect(self.path)) as conn:
            tables = [name for (name,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name != 'quiz_assignments'")]
            state = {name: conn.execute(f'SELECT * FROM "{name}" ORDER BY 1').fetchall() for name in tables}
            state["quiz_assignments"] = conn.execute(
                "SELECT id, section_id, title_bg, question_count, time_limit_minutes, created_at,"
                " question_plan_json, owner_user_id FROM quiz_assignments ORDER BY id").fetchall()
        return state

    def assert_no_token_printed(self, *texts):
        for token in (*self.tokens().values(), EXISTING_TOKEN, COLLIDING_TOKEN, FRESH_TOKEN):
            for text in texts:
                if token:
                    self.assertNotIn(token, text)


class BackfillTest(BackfillTestCase):
    def test_dry_run_reports_and_changes_nothing(self):
        before = (self.tokens(), self.everything_else(), self.path.read_bytes())
        code, out, err = self.run_cli("--dry-run")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("assignments: 3\n", out)
        self.assertIn("without a token: 3\n", out)
        self.assertIn("dry run: 3 token(s) would be generated; nothing was changed", out)
        self.assertNotIn("OK:", out)
        self.assertEqual((self.tokens(), self.everything_else(), self.path.read_bytes()), before)

    def test_null_rows_receive_valid_unique_tokens_and_nothing_else_changes(self):
        before = self.everything_else()
        code, out, err = self.run_cli()
        self.assertEqual((code, err), (0, ""))
        tokens = self.tokens()
        self.assertEqual(sorted(tokens), [71, 72, 73])
        for token in tokens.values():
            self.assertTrue(assignment_tokens.is_valid_public_token(token))
        self.assertEqual(len(set(tokens.values())), 3)
        self.assertEqual(self.everything_else(), before)
        self.assertIn("with a valid token: 0\n", out)
        self.assertIn("without a token: 3\n", out)
        self.assertIn("with an invalid token: 0\n", out)
        self.assertIn("tokens generated: 3\n", out)
        self.assertIn("OK: all 3 assignment(s) have a unique valid token", out)
        self.assert_no_token_printed(out, err)
        self.assertEqual(self.rows("PRAGMA foreign_key_check"), [])

    def test_tokens_come_from_the_secrets_module(self):
        with patch.object(assignment_tokens.secrets, "token_urlsafe",
                          side_effect=["a" * 43, "b" * 43, "c" * 43]) as token_urlsafe:
            self.assertEqual(self.run_cli()[0], 0)
        self.assertEqual([call.args for call in token_urlsafe.call_args_list], [(32,)] * 3)
        self.assertEqual(self.tokens(), {71: "a" * 43, 72: "b" * 43, 73: "c" * 43})

    def test_repeated_run_is_idempotent(self):
        self.assertEqual(self.run_cli()[0], 0)
        first = self.tokens()
        code, out, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual(self.tokens(), first)
        self.assertIn("with a valid token: 3\n", out)
        self.assertIn("tokens generated: 0\n", out)
        self.assertIn("OK: all 3 assignment(s)", out)

    def test_existing_tokens_are_kept_and_only_nulls_are_filled(self):
        self.execute("UPDATE quiz_assignments SET public_token = ? WHERE id = 72", (EXISTING_TOKEN,))
        code, out, _ = self.run_cli()
        self.assertEqual(code, 0)
        tokens = self.tokens()
        self.assertEqual(tokens[72], EXISTING_TOKEN)
        self.assertNotIn(EXISTING_TOKEN, (tokens[71], tokens[73]))
        self.assertIn("with a valid token: 1\n", out)
        self.assertIn("tokens generated: 2\n", out)

    def test_interrupted_run_changes_nothing_and_the_next_run_completes(self):
        calls = []

        def failing_generator():
            calls.append(1)
            if len(calls) == 3:
                raise KeyboardInterrupt
            return f"{len(calls)}" * 43

        with patch.object(assignment_tokens, "new_public_token", side_effect=failing_generator):
            with self.assertRaises(KeyboardInterrupt):
                self.run_cli()
        # One transaction: the two tokens written before the interruption are gone.
        self.assertEqual(self.tokens(), {71: None, 72: None, 73: None})
        code, out, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("tokens generated: 3\n", out)
        self.assertTrue(all(assignment_tokens.is_valid_public_token(t) for t in self.tokens().values()))

    def test_partially_filled_database_is_resumed(self):
        # What an earlier, completed partial run (or newly created assignments) leaves.
        self.execute("UPDATE quiz_assignments SET public_token = ? WHERE id = 71", (EXISTING_TOKEN,))
        code, out, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual(self.tokens()[71], EXISTING_TOKEN)
        self.assertIn("without a token: 2\n", out)
        self.assertIn("tokens generated: 2\n", out)

    def test_collision_with_an_existing_token_is_retried(self):
        self.execute("UPDATE quiz_assignments SET public_token = ? WHERE id = 71", (COLLIDING_TOKEN,))
        with patch.object(assignment_tokens, "new_public_token",
                          side_effect=[COLLIDING_TOKEN, FRESH_TOKEN, "G" * 43]):
            code, out, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual(self.tokens(), {71: COLLIDING_TOKEN, 72: FRESH_TOKEN, 73: "G" * 43})

    def test_exhausted_collisions_fail_and_change_nothing(self):
        self.execute("UPDATE quiz_assignments SET public_token = ? WHERE id = 71", (COLLIDING_TOKEN,))
        with patch.object(assignment_tokens, "new_public_token", return_value=COLLIDING_TOKEN):
            code, out, err = self.run_cli()
        self.assertEqual(code, 2)
        self.assertIn("could not generate an unused public assignment token", err)
        self.assertEqual(self.tokens(), {71: COLLIDING_TOKEN, 72: None, 73: None})

    def test_invalid_existing_tokens_are_reported_by_id_and_nothing_changes(self):
        invalid_values = {71: "short", 72: "", 73: "A" * 42 + "!"}
        for assignment_id, value in invalid_values.items():
            with self.subTest(value=value):
                shutil.copyfile(self.template, self.path)
                self.execute("UPDATE quiz_assignments SET public_token = ? WHERE id = ?", (value, assignment_id))
                before = self.tokens()
                for args in ((), ("--dry-run",)):
                    code, out, err = self.run_cli(*args)
                    self.assertEqual(code, 1)
                    self.assertIn("with an invalid token: 1\n", out)
                    self.assertIn(f"invalid token on assignment ids: {assignment_id}\n", out)
                    self.assertIn("FAIL: existing tokens are not valid; nothing was changed", out)
                    if value:
                        self.assertNotIn(value, out + err)
                    self.assertEqual(self.tokens(), before)

    def test_non_text_token_is_invalid(self):
        self.execute("UPDATE quiz_assignments SET public_token = 12345 WHERE id = 71")
        self.assertEqual(self.run_cli()[0], 1)

    def test_empty_table_is_complete(self):
        self.execute("DELETE FROM quiz_attempts")
        self.execute("DELETE FROM quiz_assignments")
        code, out, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("OK: all 0 assignment(s)", out)


class DryRunIsReadOnlyTest(BackfillTestCase):
    """--dry-run inspects: it cannot write, generates nothing and takes no writer lock."""

    WRITE_STATEMENT = re.compile(
        r"^\s*(INSERT|UPDATE|DELETE|REPLACE|BEGIN|COMMIT|ROLLBACK|SAVEPOINT|CREATE|DROP|ALTER|VACUUM)\b", re.I)

    def siblings(self):
        return sorted(path.name for path in self.path.parent.iterdir())

    def traced_dry_run(self, on_statement=None):
        """Run the CLI's dry run, recording how it opened the database and every statement."""
        opened, statements = [], []
        real_connect = sqlite3.connect

        def connect(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            opened.append((args, kwargs))

            def trace(statement):
                statements.append(statement)
                if on_statement:
                    on_statement()

            conn.set_trace_callback(trace)
            return conn

        with patch.object(cli.sqlite3, "connect", side_effect=connect):
            result = self.run_cli("--dry-run")
        return result, opened, statements

    def test_database_is_opened_read_only_and_only_read(self):
        siblings_seen = set()
        before = (self.path.read_bytes(), self.path.stat().st_mtime_ns, self.siblings())
        (code, out, err), opened, statements = self.traced_dry_run(
            on_statement=lambda: siblings_seen.add(tuple(self.siblings())))
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(len(opened), 1)
        (uri,), kwargs = opened[0]
        self.assertTrue(uri.endswith("?mode=ro"), uri)
        self.assertTrue(kwargs["uri"])
        # query_only is switched on before anything is read; nothing but reads follows.
        self.assertEqual(statements[0], "PRAGMA query_only = ON")
        self.assertEqual([s for s in statements if self.WRITE_STATEMENT.match(s)], [])
        self.assertEqual(sum(s.lstrip().upper().startswith("SELECT") for s in statements), 1)
        # No rollback journal, WAL or lock file appeared at any point, and the file is untouched.
        self.assertEqual(siblings_seen, {tuple(before[2])})
        self.assertEqual((self.path.read_bytes(), self.path.stat().st_mtime_ns, self.siblings()), before)

    def test_inspection_works_on_a_query_only_connection(self):
        with closing(sqlite3.connect(f"{self.path.as_uri()}?mode=ro", uri=True, isolation_level=None)) as conn:
            conn.execute("PRAGMA query_only = ON")
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("UPDATE quiz_assignments SET title_bg = title_bg")
            out = io.StringIO()
            self.assertEqual(cli.dry_run(conn, out=out), 0)
            self.assertFalse(conn.in_transaction)
        self.assertIn("dry run: 3 token(s) would be generated; nothing was changed", out.getvalue())

    def test_read_only_file_is_enough(self):
        self.path.chmod(0o444)
        self.addCleanup(self.path.chmod, 0o644)
        code, out, err = self.run_cli("--dry-run")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("without a token: 3\n", out)

    def test_no_token_is_generated(self):
        with patch.object(assignment_tokens, "new_public_token", side_effect=AssertionError("generated")) as new, \
                patch.object(assignment_tokens.secrets, "token_urlsafe", side_effect=AssertionError("generated")) as raw, \
                patch.object(cli, "fill_missing", side_effect=AssertionError("filled")) as fill:
            code, out, err = self.run_cli("--dry-run")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual((new.call_count, raw.call_count, fill.call_count), (0, 0, 0))
        self.assertEqual(self.tokens(), {71: None, 72: None, 73: None})

    def test_another_connection_can_write_while_the_dry_run_inspects(self):
        real_read_state = cli.read_state
        written = []

        def read_then_let_a_writer_in(conn):
            state = real_read_state(conn)
            # The dry run's connection is still open here. timeout=0: a held lock fails at once.
            with closing(sqlite3.connect(self.path, timeout=0, isolation_level=None)) as writer:
                writer.execute("BEGIN IMMEDIATE")
                writer.execute("UPDATE quiz_assignments SET title_bg = 'Променен' WHERE id = 72")
                writer.execute("COMMIT")
            written.append(True)
            return state

        with patch.object(cli, "read_state", side_effect=read_then_let_a_writer_in):
            code, out, err = self.run_cli("--dry-run")
        self.assertEqual((code, err, written), (0, "", [True]))
        self.assertEqual(self.rows("SELECT title_bg FROM quiz_assignments WHERE id = 72"), [("Променен",)])

    def test_dry_run_succeeds_while_another_connection_holds_the_writer_lock(self):
        with closing(sqlite3.connect(self.path, timeout=0, isolation_level=None)) as writer:
            writer.execute("BEGIN IMMEDIATE")
            writer.execute("UPDATE quiz_assignments SET title_bg = 'Незаписан' WHERE id = 72")
            code, out, err = self.run_cli("--dry-run")
            # Apply mode needs the writer lock and so cannot run now; the dry run can.
            self.assertEqual((code, err), (0, ""))
            self.assertIn("without a token: 3\n", out)
            writer.execute("ROLLBACK")

    def test_counts_follow_the_stored_tokens(self):
        self.execute("UPDATE quiz_assignments SET public_token = ? WHERE id = 71", (EXISTING_TOKEN,))
        code, out, _ = self.run_cli("--dry-run")
        self.assertEqual(code, 0)
        for line in ("assignments: 3\n", "with a valid token: 1\n", "without a token: 2\n",
                     "with an invalid token: 0\n", "dry run: 2 token(s) would be generated; nothing was changed"):
            self.assertIn(line, out)
        self.assertNotIn(EXISTING_TOKEN, out)

        self.execute("UPDATE quiz_assignments SET public_token = ? WHERE id = 72", (FRESH_TOKEN,))
        self.execute("UPDATE quiz_assignments SET public_token = ? WHERE id = 73", (COLLIDING_TOKEN,))
        code, out, _ = self.run_cli("--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("without a token: 0\n", out)
        self.assertIn("dry run: 0 token(s) would be generated", out)

    def test_invalid_existing_token_fails_without_any_write(self):
        self.execute("UPDATE quiz_assignments SET public_token = 'not-a-token' WHERE id = 72")
        before = (self.path.read_bytes(), self.siblings())
        with patch.object(assignment_tokens, "new_public_token", side_effect=AssertionError("generated")):
            (code, out, err), _, statements = self.traced_dry_run()
        self.assertEqual((code, err), (1, ""))
        self.assertIn("with an invalid token: 1\n", out)
        self.assertIn("without a token: 2\n", out)
        self.assertIn("invalid token on assignment ids: 72\n", out)
        self.assertIn("FAIL: existing tokens are not valid; nothing was changed", out)
        self.assertNotIn("not-a-token", out)
        self.assertNotIn("dry run:", out)
        self.assertEqual([s for s in statements if self.WRITE_STATEMENT.match(s)], [])
        self.assertEqual((self.path.read_bytes(), self.siblings()), before)

    def test_missing_database_is_not_created(self):
        missing = self.path.parent / "missing.db"
        before = self.siblings()
        code, out, err = self.run_cli("--dry-run", path=missing)
        self.assertEqual((code, out), (2, ""))
        self.assertIn("database not found", err)
        self.assertFalse(missing.exists())
        self.assertEqual(self.siblings(), before)

    def test_apply_mode_still_writes_in_one_immediate_transaction(self):
        real_connect = sqlite3.connect
        statements, opened = [], []

        def connect(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            opened.append(args[0])
            conn.set_trace_callback(statements.append)
            return conn

        with patch.object(cli.sqlite3, "connect", side_effect=connect):
            code, _, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertTrue(opened[0].endswith("?mode=rw"))
        self.assertNotIn("PRAGMA query_only = ON", statements)
        self.assertEqual(statements.count("BEGIN IMMEDIATE"), 1)
        self.assertEqual(statements[-1], "COMMIT")
        self.assertEqual(sum(s.lstrip().upper().startswith("UPDATE") for s in statements), 3)


class BackfillTargetTest(BackfillTestCase):
    def test_database_must_be_named_and_is_never_created(self):
        out, err = io.StringIO(), io.StringIO()
        with patch.dict("os.environ", {}, clear=False) as environ:
            environ.pop("DZI_DB", None)
            self.assertEqual(cli.main([], out=out, err=err), 2)
        self.assertIn("no database given", err.getvalue())

        missing = self.path.parent / "missing.db"
        code, _, err_text = self.run_cli(path=missing)
        self.assertEqual(code, 2)
        self.assertIn("database not found", err_text)
        self.assertFalse(missing.exists())

    def test_dzi_db_names_the_target(self):
        out = io.StringIO()
        with patch.dict("os.environ", {"DZI_DB": str(self.path)}):
            self.assertEqual(cli.main(["--dry-run"], out=out, err=io.StringIO()), 0)
        self.assertIn("without a token: 3", out.getvalue())

    def test_database_without_migration_017_is_refused_unchanged(self):
        old = self.path.parent / "old.db"
        sqlite3.connect(old).close()
        previous = self.path.parent / "migrations"
        previous.mkdir()
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name < "017_":
                shutil.copyfile(path, previous / path.name)
        self.assertEqual(manage_migrations.apply_migrations(old, previous, out=io.StringIO()), 0)
        before = old.read_bytes()
        code, _, err = self.run_cli(path=old)
        self.assertEqual(code, 2)
        self.assertIn("migration 017 required", err)
        self.assertEqual(old.read_bytes(), before)

    def test_application_never_runs_the_backfill(self):
        for relative in ("web/app.py", "web/wsgi.py", "web/production.py", "web/services/quiz.py",
                         "web/blueprints/quiz.py", "web/blueprints/teacher.py"):
            with self.subTest(module=relative):
                self.assertNotIn("manage_assignment_tokens", (ROOT / relative).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
