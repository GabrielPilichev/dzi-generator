"""RN4–6: isolated backup scrub, fail closed, order, failure cleanup."""
import hashlib
import sqlite3
from unittest.mock import patch

from src import backup_sqlite as backup
from tests.recovery_support import RecoveryTestCase, add_recovery
from tests.registration_support import T0
from web.services import recovery_scrub as scrub


class ScrubTest(RecoveryTestCase):
    def test_scrubbed_and_known_columns_and_no_caller_transaction_no_write(self):
        self.assertEqual(scrub.SCRUBBED_COLUMNS,
                         ("email", "user_id", "session_key_digest", "code_hash", "code_expires_at"))
        self.assertEqual(scrub.KNOWN_COLUMNS,
                         {r[1] for r in self.conn.execute("PRAGMA table_info(password_recoveries)")})
        add_recovery(self.conn)
        self.conn.commit()
        for function in (scrub.void_pending, scrub.cleanup):
            with self.assertRaises(RuntimeError):
                function(self.conn, T0)
        self.assertEqual(self.conn.execute("SELECT state FROM password_recoveries").fetchone()[0], "pending")

    def test_backup_order_source_preserved_and_published_hash(self):
        add_recovery(self.conn)
        self.conn.commit()
        before = self.path.read_bytes()
        target = self.root / "backup.db"
        events = []
        real_scrub, real_connect, real_fsync, real_link = (backup.scrub_pending_recoveries,
            sqlite3.connect, backup._fsync, backup.os.link)
        class Observed(sqlite3.Connection):
            def execute(inner, sql, *args):
                if sql == "PRAGMA quick_check":
                    events.append("check")
                    self.assertEqual(inner.execute("SELECT COUNT(*) FROM password_recoveries WHERE state='pending'").fetchone()[0], 0)
                return super().execute(sql, *args)
        def connect(*args, **kwargs):
            return real_connect(*args, factory=Observed, **kwargs)
        def do_scrub(conn):
            events.append("scrub")
            return real_scrub(conn)
        def fsync(path):
            events.append("fsync")
            return real_fsync(path)
        def link(a,b):
            events.append("link")
            return real_link(a,b)
        with patch.object(backup.sqlite3, "connect", side_effect=connect), \
                patch.object(backup, "scrub_pending_recoveries", side_effect=do_scrub), \
                patch.object(backup, "_fsync", side_effect=fsync), patch.object(backup.os, "link", side_effect=link):
            result = backup.backup_database(self.path, target)
        self.assertLess(events.index("scrub"), events.index("check"))
        self.assertLess(events.index("check"), events.index("fsync"))
        self.assertLess(events.index("fsync"), events.index("link"))
        self.assertEqual(result["recoveries_scrubbed"], 1)
        self.assertEqual(result["sha256"], hashlib.sha256(target.read_bytes()).hexdigest())
        self.assertEqual(self.path.read_bytes(), before)
        with sqlite3.connect(target) as conn:
            self.assertEqual(conn.execute("SELECT state, " + ", ".join(scrub.SCRUBBED_COLUMNS) +
                                          " FROM password_recoveries").fetchall(), [("void", None, None, None, None, None)])

    def test_schema_rejections_publish_nothing_and_remove_partial(self):
        for mutation in ("DROP TABLE password_recoveries",
                         "ALTER TABLE password_recoveries ADD COLUMN unknown_secret TEXT",
                         "ALTER TABLE password_recoveries RENAME COLUMN code_hash TO other_hash",
                         "ALTER TABLE password_recoveries ADD COLUMN derived TEXT GENERATED ALWAYS AS (email) VIRTUAL"):
            source = self.root / (str(len(mutation))+".db")
            with sqlite3.connect(source) as conn:
                self.conn.backup(conn)
                conn.execute(mutation)
                conn.commit()
            before = source.read_bytes()
            target = self.root / "failed.db"
            with self.subTest(mutation=mutation), self.assertRaises(backup.BackupError) as error:
                backup.backup_database(source, target)
            self.assertIn("password_recoveries", str(error.exception))
            self.assertNotIn("unknown_secret", str(error.exception))
            self.assertFalse(target.exists())
            self.assertFalse(list(self.root.glob(".failed.db*")))
            self.assertEqual(before, source.read_bytes())

    def test_supported_historical_absence_and_injected_failure(self):
        self.conn.execute("DROP TABLE password_recoveries")
        self.conn.execute("DELETE FROM schema_migrations WHERE filename=?", (scrub.MIGRATION,))
        self.conn.commit()
        self.assertFalse(scrub.has_recovery_storage(self.conn))
        result = backup.backup_database(self.path, self.root / "historical.db")
        self.assertEqual(result["recoveries_scrubbed"], 0)
        with patch.object(backup, "scrub_pending_recoveries", side_effect=sqlite3.OperationalError("injected")):
            with self.assertRaises(sqlite3.OperationalError):
                backup.backup_database(self.path, self.root / "failed.db")
        self.assertFalse((self.root / "failed.db").exists())
        self.assertFalse(list(self.root.glob(".failed.db*")))
