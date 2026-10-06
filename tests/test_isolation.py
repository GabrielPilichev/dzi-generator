import os
import subprocess
import sys
import unittest
from contextlib import closing
from pathlib import Path

from tests.runtime_fixtures import add_submitted_attempt
from tests.support import ROOT, IsolatedAppTestCase, isolated_app, web_app
from web.services import quiz as quiz_service


class IsolationTest(IsolatedAppTestCase):
    def test_fresh_discovery_never_connects_to_repository_database(self):
        # A fresh interpreter catches import-time migration paths, which an
        # already imported app in the parent process would hide.
        script = '''
import os, sqlite3, unittest
from pathlib import Path
from urllib.parse import unquote, urlsplit
from unittest.mock import patch
real = (Path.cwd() / "data/questions.db").resolve()
connect = sqlite3.connect
connections = []
def guarded(database, *args, **kwargs):
    raw = os.fspath(database)
    path = unquote(urlsplit(raw).path) if raw.startswith("file:") else raw
    if Path(path).resolve() == real:
        raise AssertionError("test discovery connected to repository database")
    connections.append(raw)
    return connect(database, *args, **kwargs)
with patch("sqlite3.connect", guarded):
    loader = unittest.TestLoader()
    loader.discover("tests")
    assert not loader.errors, loader.errors
    assert connections == [], connections  # importing web.app opens no database
'''
        environment = dict(os.environ, DZI_DB=str(ROOT / "data/questions.db"))
        result = subprocess.run(
            [sys.executable, "-B", "-c", script], cwd=ROOT, env=environment,
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_both_database_helpers_write_only_the_same_temporary_database(self):
        path = Path(web_app.app.config["DB_PATH"])
        self.assertTrue(path.is_relative_to(self.isolation_root))
        self.assertEqual(path, web_app.QUIZ_DB_PATH)
        self.assertEqual(str(path), os.environ["DZI_DB"])
        conn = web_app.quiz_db()
        try:
            conn.execute("CREATE TABLE isolation_probe (value TEXT)")
            conn.execute("INSERT INTO isolation_probe VALUES ('temporary')")
            conn.commit()
        finally:
            conn.close()
        with web_app.app.app_context():
            conn = web_app.get_db()
            self.assertEqual(conn.execute("SELECT value FROM isolation_probe").fetchone()[0], "temporary")
            self.assertEqual(Path(conn.execute("PRAGMA database_list").fetchone()[2]), path)

    def test_real_note_writers_use_temporary_vault(self):
        conn = web_app.quiz_db()
        try:
            # A canonical-built database has no runtime rows: create the attempt explicitly.
            with conn:
                add_submitted_attempt(conn)
            attempt = conn.execute(
                "SELECT id, assignment_id FROM quiz_attempts WHERE submitted_at IS NOT NULL LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(attempt, "the explicit fixture should have created a submitted attempt")
        with web_app.app.test_request_context("/"):
            web_app.quiz_write_assignment_note(attempt["assignment_id"], "http://localhost/")
            quiz_service.quiz_write_attempt_note(attempt["id"])
        generated = web_app.QUIZ_VAULT_PATH / "Generated" / "Quizzes"
        self.assertTrue(generated.is_relative_to(self.isolation_root))
        self.assertTrue(list(generated.rglob("*.md")))
        self.assertTrue(list((generated / "attempts").glob("*.md")))

    def test_nested_fixture_restores_state_and_removes_files_after_exception(self):
        previous_config = dict(web_app.app.config)
        previous_environment = dict(os.environ)
        previous_paths = (web_app.DB_PATH, web_app.QUIZ_DB_PATH, web_app.QUIZ_VAULT_PATH)
        with self.assertRaisesRegex(RuntimeError, "fixture failure"):
            with isolated_app() as root:
                web_app.app.config["ISOLATION_SENTINEL"] = True
                os.environ["DZI_DB"] = "changed-by-test"
                artifact = web_app.QUIZ_VAULT_PATH / "probe.md"
                artifact.write_text("temporary", encoding="utf-8")
                self.assertNotEqual(web_app.QUIZ_DB_PATH, previous_paths[1])
                raise RuntimeError("fixture failure")
        self.assertFalse(root.exists())
        self.assertEqual(dict(web_app.app.config), previous_config)
        self.assertEqual(dict(os.environ), previous_environment)
        self.assertEqual((web_app.DB_PATH, web_app.QUIZ_DB_PATH, web_app.QUIZ_VAULT_PATH), previous_paths)

    def test_failed_class_setup_cleans_up_fixture(self):
        roots = []
        previous_path = web_app.QUIZ_DB_PATH

        class BrokenSetup(IsolatedAppTestCase):
            @classmethod
            def setUpClass(cls):
                super().setUpClass()
                roots.append(cls.isolation_root)
                raise RuntimeError("expected setup failure")

            def test_unreachable(self):
                raise AssertionError("must not run")

        result = unittest.TestResult()
        unittest.defaultTestLoader.loadTestsFromTestCase(BrokenSetup).run(result)
        self.assertEqual(len(result.errors), 1)
        self.assertIn("expected setup failure", result.errors[0][1])
        self.assertFalse(roots[0].exists())
        self.assertEqual(web_app.QUIZ_DB_PATH, previous_path)

    def test_class_writes_do_not_leak_into_next_class_fixture(self):
        with isolated_app() as first_root:
            with closing(web_app.quiz_db()) as conn:
                conn.execute("CREATE TABLE class_only_probe (value TEXT)")
                conn.commit()
            (web_app.QUIZ_VAULT_PATH / "class-only.md").write_text("fixture", encoding="utf-8")
        with isolated_app() as second_root:
            self.assertNotEqual(first_root, second_root)
            with closing(web_app.quiz_db()) as conn:
                self.assertIsNone(conn.execute(
                    "SELECT name FROM sqlite_master WHERE name='class_only_probe'"
                ).fetchone())
            self.assertFalse((web_app.QUIZ_VAULT_PATH / "class-only.md").exists())
        self.assertFalse(first_root.exists())
        self.assertFalse(second_root.exists())
