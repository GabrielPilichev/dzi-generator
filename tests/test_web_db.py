"""web.db connection policy, exercised only against temporary databases."""

import hashlib
import re
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.subject_browse_cache import BrowseResultCache
from tests.seed_guard import ROOT
from web import db as web_db


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class TemporaryDatabaseTestCase(unittest.TestCase):
    def make_database(self, journal_mode="DELETE"):
        directory = tempfile.TemporaryDirectory(prefix="web-db-")
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "policy.db"
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("PRAGMA journal_mode=" + journal_mode)
            conn.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
            conn.execute("CREATE TABLE child (id INTEGER PRIMARY KEY, "
                         "parent_id INTEGER NOT NULL REFERENCES parent(id))")
            conn.execute("INSERT INTO parent VALUES (1)")
            conn.commit()
        return path


class ConnectionPolicyTest(TemporaryDatabaseTestCase):
    def test_every_connection_gets_the_common_settings(self):
        path = self.make_database()
        for query_only in (False, True):
            with self.subTest(query_only=query_only), \
                    closing(web_db.connect_db(path, query_only=query_only)) as conn:
                self.assertIs(conn.row_factory, sqlite3.Row)
                self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
                self.assertEqual(conn.execute("SELECT id FROM parent").fetchone()["id"], 1)

    def test_timeout_is_a_per_call_choice(self):
        with closing(web_db.connect_db(self.make_database(), timeout=2.0)) as conn:
            self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 2000)

    def test_writable_connection_enforces_foreign_keys(self):
        with closing(web_db.connect_db(self.make_database())) as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO child (parent_id) VALUES (999)")
            conn.execute("INSERT INTO child (parent_id) VALUES (1)")
            conn.commit()

    def test_query_only_connection_refuses_writes_and_changes_nothing(self):
        path = self.make_database()
        before = sha256(path)
        with closing(web_db.connect_db(path, query_only=True)) as conn:
            self.assertEqual(conn.execute("PRAGMA query_only").fetchone()[0], 1)
            with self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
                conn.execute("INSERT INTO parent VALUES (2)")
        self.assertEqual(sha256(path), before)
        with closing(web_db.connect_db(path)) as conn:
            self.assertEqual(conn.execute("PRAGMA query_only").fetchone()[0], 0)

    def test_query_only_connection_never_creates_a_missing_database(self):
        missing = Path(tempfile.mkdtemp(prefix="web-db-")) / "absent.db"
        self.addCleanup(missing.parent.rmdir)
        with self.assertRaises(sqlite3.OperationalError):
            web_db.connect_db(missing, query_only=True)
        self.assertFalse(missing.exists())

    def test_in_memory_database_still_connects(self):
        with closing(web_db.connect_db(":memory:")) as conn:
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_failed_setup_closes_the_connection(self):
        conn = MagicMock()
        conn.execute.side_effect = sqlite3.OperationalError("setup failed")
        with patch.object(web_db.sqlite3, "connect", return_value=conn):
            with self.assertRaisesRegex(sqlite3.OperationalError, "setup failed"):
                web_db.connect_db("ignored.db")
        conn.close.assert_called_once_with()

    def test_db_module_does_not_resolve_a_configured_path(self):
        # Callers pass their own configured path; resolving one here would bypass
        # the DB_PATH / QUIZ_DB_PATH values that tests patch.
        public = {name for name in vars(web_db) if not name.startswith("_")}
        self.assertEqual(public, {"sqlite3", "Path", "DEFAULT_TIMEOUT_SECONDS", "connect_db"})


class JournalModeIsNeverChangedTest(TemporaryDatabaseTestCase):
    def traced_statements(self, **kwargs):
        path = self.make_database()
        statements = []
        connect = sqlite3.connect

        def traced(*args, **options):
            conn = connect(*args, **options)
            conn.set_trace_callback(statements.append)
            return conn

        with patch.object(sqlite3, "connect", traced):
            web_db.connect_db(path, **kwargs).close()
        return [sql.strip().upper() for sql in statements]

    def test_connecting_issues_only_connection_local_pragmas(self):
        self.assertEqual(self.traced_statements(), ["PRAGMA FOREIGN_KEYS=ON"])
        self.assertEqual(self.traced_statements(query_only=True),
                         ["PRAGMA FOREIGN_KEYS=ON", "PRAGMA QUERY_ONLY=ON"])

    def test_opening_and_reading_leaves_a_rollback_journal_database_byte_identical(self):
        path = self.make_database("DELETE")
        before = sha256(path)
        for query_only in (False, True):
            with closing(web_db.connect_db(path, query_only=query_only)) as conn:
                conn.execute("SELECT count(*) FROM parent").fetchone()
        self.assertEqual(sha256(path), before)
        with closing(sqlite3.connect(path)) as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "delete")

    def test_an_existing_wal_database_stays_wal_and_a_rollback_one_is_not_converted(self):
        for mode in ("WAL", "DELETE"):
            with self.subTest(mode=mode):
                path = self.make_database(mode)
                for query_only in (False, True):
                    with closing(web_db.connect_db(path, query_only=query_only)) as conn:
                        self.assertEqual(
                            conn.execute("PRAGMA journal_mode").fetchone()[0], mode.lower())

    def test_web_code_never_sets_a_journal_mode(self):
        offenders = [
            str(path.relative_to(ROOT))
            for path in [*sorted((ROOT / "web").rglob("*.py")),
                         ROOT / "src" / "subject_browse_cache.py"]
            if re.search(r"journal_mode|wal_checkpoint", path.read_text(encoding="utf-8"), re.I)
            and path.name != "db.py"
        ]
        self.assertEqual(offenders, [])


class SharedFactoryOwnershipTest(unittest.TestCase):
    def test_only_web_db_opens_sqlite_connections_for_the_web_app(self):
        files = [*sorted((ROOT / "web").rglob("*.py")), ROOT / "src" / "subject_browse_cache.py"]
        offenders = [
            str(path.relative_to(ROOT)) for path in files
            if path.name != "db.py" and "sqlite3.connect(" in path.read_text(encoding="utf-8")
        ]
        self.assertEqual(offenders, [])
        self.assertIn("sqlite3.connect(", (ROOT / "web" / "db.py").read_text(encoding="utf-8"))

    def test_web_db_depends_on_nothing_above_it(self):
        source = (ROOT / "web" / "db.py").read_text(encoding="utf-8")
        imports = re.findall(r"^(?:from|import)\s+(\S+)", source, re.M)
        self.assertEqual(sorted(imports), ["pathlib", "sqlite3"])

    def test_browse_cache_loads_through_a_query_only_factory_connection(self):
        with tempfile.TemporaryDirectory(prefix="web-db-") as directory:
            path = Path(directory) / "browse.db"
            with closing(sqlite3.connect(path)) as conn:
                conn.execute("CREATE TABLE content (value TEXT)")
                conn.commit()
            calls = []
            real = web_db.connect_db

            def spy(target, **kwargs):
                calls.append((Path(target), kwargs))
                return real(target, **kwargs)

            with patch.object(web_db, "connect_db", spy):
                modes = BrowseResultCache().get(
                    path, "modes",
                    lambda conn: (conn.execute("PRAGMA query_only").fetchone()[0],
                                  conn.execute("PRAGMA foreign_keys").fetchone()[0]))
            self.assertEqual(calls, [(path, {"query_only": True})])
            self.assertEqual(modes, (1, 1))


if __name__ == "__main__":
    unittest.main()
