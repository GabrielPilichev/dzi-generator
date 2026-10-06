"""get_db(), quiz_db() and the health check on the shared web.db policy."""

import hashlib
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from tests.seed_guard import ROOT, SeedDatabaseAccess
from tests.support import IsolatedAppTestCase, web_app
from web import db as web_db


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def database_file(conn):
    return Path(conn.execute("PRAGMA database_list").fetchone()[2])


class SpyFactory:
    """Records every call while delegating to the real (guarded) connect_db."""

    def __init__(self):
        self.calls = []
        self.real = web_db.connect_db

    def __call__(self, path, **kwargs):
        self.calls.append((Path(path), kwargs))
        return self.real(path, **kwargs)


class WebConnectionPathsUseSharedPolicyTest(IsolatedAppTestCase):
    def setUp(self):
        self.path = Path(web_app.app.config["DB_PATH"])
        self.spy = SpyFactory()
        patcher = patch.object(web_db, "connect_db", self.spy)
        patcher.start()
        self.addCleanup(patcher.stop)

    def assert_common_policy(self, conn):
        self.assertIs(conn.row_factory, sqlite3.Row)
        self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 5000)

    def test_get_db_uses_the_factory_with_the_configured_path(self):
        with web_app.app.app_context():
            conn = web_app.get_db()
            self.assert_common_policy(conn)
            self.assertEqual(conn.execute("PRAGMA query_only").fetchone()[0], 0)
            self.assertEqual(database_file(conn), self.path)
        self.assertEqual(self.spy.calls, [(self.path, {})])

    def test_quiz_db_uses_the_factory_with_the_quiz_path(self):
        with closing(web_app.quiz_db()) as conn:
            self.assert_common_policy(conn)
            self.assertEqual(database_file(conn), self.path)
        self.assertEqual(self.spy.calls, [(self.path, {})])

    def test_health_connection_is_a_query_only_factory_connection(self):
        with closing(web_app.open_existing_database_read_only(self.path, timeout=2.0)) as conn:
            self.assertIs(conn.row_factory, sqlite3.Row)
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 2000)
            self.assertEqual(conn.execute("PRAGMA query_only").fetchone()[0], 1)
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("CREATE TABLE health_write_probe (x)")
        self.assertEqual(self.spy.calls, [(self.path, {"query_only": True, "timeout": 2.0})])
        self.assertEqual(web_app.app.test_client().get("/healthz").status_code, 200)

    def test_missing_database_is_not_created_by_the_health_connection(self):
        missing = self.isolation_root / "missing.db"
        with self.assertRaises(sqlite3.OperationalError):
            web_app.open_existing_database_read_only(missing)
        self.assertFalse(missing.exists())


class ConnectionLifecycleTest(IsolatedAppTestCase):
    def test_get_db_is_reused_in_one_context_and_closed_at_teardown(self):
        with web_app.app.app_context():
            first = web_app.get_db()
            self.assertIs(web_app.get_db(), first)
            first.execute("SELECT 1")
        with self.assertRaises(sqlite3.ProgrammingError):
            first.execute("SELECT 1")  # closed by teardown_appcontext
        with web_app.app.app_context():
            self.assertIsNot(web_app.get_db(), first)

    def test_request_connection_is_closed_after_the_request(self):
        seen = []
        real = web_db.connect_db

        def remember(path, **kwargs):
            seen.append(real(path, **kwargs))
            return seen[-1]

        with patch.object(web_db, "connect_db", remember):
            self.assertEqual(web_app.app.test_client().get("/").status_code, 200)
        self.assertTrue(seen)
        for conn in seen:  # the request connection and any cold browse-cache loaders
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute("SELECT 1")

    def test_quiz_db_is_a_separate_connection_per_call_owned_by_the_caller(self):
        first, second = web_app.quiz_db(), web_app.quiz_db()
        try:
            self.assertIsNot(first, second)
            first.close()
            self.assertEqual(second.execute("SELECT 1").fetchone()[0], 1)  # independent
        finally:
            first.close()
            second.close()
        with self.assertRaises(sqlite3.ProgrammingError):
            second.execute("SELECT 1")

    def test_quiz_db_stays_independent_of_the_request_connection(self):
        with web_app.app.app_context():
            request_conn = web_app.get_db()
            with closing(web_app.quiz_db()) as quiz_conn:
                self.assertIsNot(quiz_conn, request_conn)
            self.assertEqual(request_conn.execute("SELECT 1").fetchone()[0], 1)


class ConfiguredPathsAreRespectedTest(IsolatedAppTestCase):
    def make_marked_database(self, name):
        path = self.isolation_root / name
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("CREATE TABLE marker (name TEXT)")
            conn.execute("INSERT INTO marker VALUES (?)", (name,))
            conn.commit()
        return path

    def test_get_db_and_quiz_db_follow_their_own_configured_paths(self):
        request_db = self.make_marked_database("request.db")
        quiz_db = self.make_marked_database("quiz.db")
        with patch.dict(web_app.app.config, {"DB_PATH": str(request_db)}), \
                patch.object(web_app, "QUIZ_DB_PATH", quiz_db):
            with web_app.app.app_context():
                self.assertEqual(database_file(web_app.get_db()), request_db)
                self.assertEqual(
                    web_app.get_db().execute("SELECT name FROM marker").fetchone()[0], "request.db")
            with closing(web_app.quiz_db()) as conn:
                self.assertEqual(conn.execute("SELECT name FROM marker").fetchone()[0], "quiz.db")

    def test_default_path_semantics_are_unchanged(self):
        with patch.dict("os.environ", clear=False) as environment:
            environment.pop("DZI_DB", None)
            self.assertEqual(web_app.configured_db_path(), ROOT / "data" / "questions.db")
            environment["DZI_DB"] = "/var/lib/learnpilot/data/questions.db"
            self.assertEqual(web_app.configured_db_path(),
                             Path("/var/lib/learnpilot/data/questions.db"))


class RequestsNeverChangeJournalModeTest(IsolatedAppTestCase):
    def test_serving_requests_leaves_the_database_file_byte_identical(self):
        path = Path(web_app.app.config["DB_PATH"])
        with closing(sqlite3.connect(path)) as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        before = sha256(path)
        client = web_app.app.test_client()
        for url in ("/", "/healthz"):
            self.assertEqual(client.get(url).status_code, 200, url)
        self.assertEqual(sha256(path), before)
        with closing(sqlite3.connect(path)) as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        self.assertFalse(Path(str(path) + "-wal").exists())

    def test_no_journal_mode_statement_runs_on_any_request_connection(self):
        statements = []
        real = sqlite3.connect

        def traced(*args, **kwargs):
            conn = real(*args, **kwargs)
            conn.set_trace_callback(statements.append)
            return conn

        with patch.object(sqlite3, "connect", traced):
            self.assertEqual(web_app.app.test_client().get("/").status_code, 200)
        self.assertTrue(statements)
        self.assertEqual([s for s in statements if "journal_mode" in s.lower()], [])


class SeedDatabaseTripwireTest(IsolatedAppTestCase):
    """Configuration leaking back to the repository seed fails instead of opening it."""

    def test_leaked_request_path_is_refused_before_sqlite_is_called(self):
        with patch.dict(web_app.app.config, {"DB_PATH": str(ROOT / "data" / "questions.db")}):
            with web_app.app.app_context(), patch.object(sqlite3, "connect") as connect:
                with self.assertRaises(SeedDatabaseAccess):
                    web_app.get_db()
                connect.assert_not_called()

    def test_leaked_quiz_path_is_refused_before_sqlite_is_called(self):
        with patch.object(web_app, "QUIZ_DB_PATH", ROOT / "data" / "questions.db"), \
                patch.object(sqlite3, "connect") as connect:
            with self.assertRaises(SeedDatabaseAccess):
                web_app.quiz_db()
            connect.assert_not_called()

    def test_temporary_database_is_not_affected(self):
        with closing(web_app.quiz_db()) as conn:
            self.assertTrue(database_file(conn).is_relative_to(self.isolation_root))


if __name__ == "__main__":
    unittest.main()
