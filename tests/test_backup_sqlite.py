"""Online SQLite backup helper, exercised only on temporary databases."""

import io
import os
import re
import sqlite3
import stat
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from src import backup_sqlite
from tests.support import ROOT


class BackupSqliteTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="learnpilot-backup-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.out = self.root / "backups"
        self.out.mkdir()
        self.source = self.root / "live.db"
        # A live WAL database whose newest committed row exists only in the WAL.
        self.live = sqlite3.connect(self.source)
        self.addCleanup(self.live.close)
        self.live.execute("PRAGMA journal_mode=WAL")
        self.live.execute("PRAGMA wal_autocheckpoint=0")
        self.live.execute("CREATE TABLE probe (value TEXT)")
        self.live.execute("INSERT INTO probe VALUES ('checkpointed')")
        self.live.commit()
        self.live.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.live.execute("INSERT INTO probe VALUES ('only in wal')")
        self.live.commit()

    def data_files(self):
        return {p.name: p.read_bytes() for p in (self.source, Path(f"{self.source}-wal"))}

    def test_backup_captures_wal_content_as_one_verified_private_file(self):
        before = self.data_files()
        destination = self.out / "snapshot.db"
        summary = backup_sqlite.backup_database(self.source, destination)

        self.assertEqual(summary["quick_check"], "ok")
        self.assertEqual(summary["destination"], str(destination))
        self.assertEqual(summary["bytes"], destination.stat().st_size)
        self.assertRegex(summary["sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["snapshot.db"])
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
        header = destination.read_bytes()[:20]
        self.assertEqual((header[18], header[19]), (1, 1))  # rollback journal, no sidecars needed
        with closing(sqlite3.connect(f"{destination.as_uri()}?mode=ro", uri=True)) as copy:
            rows = [r[0] for r in copy.execute("SELECT value FROM probe ORDER BY rowid")]
        self.assertEqual(rows, ["checkpointed", "only in wal"])
        # The live database and WAL contents are untouched (SHM is SQLite's shared index).
        self.assertEqual(self.data_files(), before)

    def test_cli_output_dir_creates_timestamped_backup(self):
        out, err = io.StringIO(), io.StringIO()
        code = backup_sqlite.main(["--db", str(self.source), "--output-dir", str(self.out)], out, err)
        self.assertEqual(code, 0, err.getvalue())
        created = list(self.out.iterdir())
        self.assertEqual(len(created), 1)
        self.assertRegex(created[0].name, r"^questions-\d{8}T\d{6}Z\.db$")
        self.assertIn("quick_check: ok", out.getvalue())

    def test_cli_requires_explicit_source_and_destination(self):
        for argv in ([], ["--db", str(self.source)], ["--output-dir", str(self.out)],
                     ["--db", str(self.source), "--output", "x.db", "--output-dir", str(self.out)]):
            with self.subTest(argv=argv), patch("sys.stderr", io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    backup_sqlite.parse_args(argv)
                self.assertEqual(raised.exception.code, 2)

    def test_unsafe_requests_fail_without_creating_files(self):
        existing = self.out / "existing.db"
        existing.write_bytes(b"keep me")
        inside_checkout = ROOT / "data" / f"backup-test-{os.getpid()}.db"
        cases = {
            "missing source": (self.root / "missing.db", self.out / "a.db"),
            "existing destination": (self.source, existing),
            "missing directory": (self.source, self.root / "absent" / "a.db"),
            "inside checkout": (self.source, inside_checkout),
            "same file": (self.source, self.source),
        }
        for label, (source, destination) in cases.items():
            with self.subTest(label):
                with self.assertRaises(backup_sqlite.BackupError):
                    backup_sqlite.backup_database(source, destination)
        self.assertEqual(existing.read_bytes(), b"keep me")
        self.assertFalse(inside_checkout.exists())
        self.assertFalse((self.root / "missing.db").exists())
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["existing.db"])

        err = io.StringIO()
        code = backup_sqlite.main(["--db", str(self.root / "missing.db"),
                                   "--output", str(self.out / "b.db")], io.StringIO(), err)
        self.assertEqual(code, 1)
        self.assertIn("backup failed: source database not found", err.getvalue())

    def test_failures_after_writing_leave_no_partial_files(self):
        not_a_database = self.root / "corrupt.db"
        not_a_database.write_bytes(b"definitely not sqlite" * 100)
        with self.assertRaises(sqlite3.DatabaseError):
            backup_sqlite.backup_database(not_a_database, self.out / "corrupt-copy.db")
        self.assertEqual(list(self.out.iterdir()), [])

        with patch.object(backup_sqlite, "_fsync", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                backup_sqlite.backup_database(self.source, self.out / "late-failure.db")
        self.assertEqual(list(self.out.iterdir()), [])

    def test_partial_names_never_match_backup_pattern(self):
        # Hidden, PID-suffixed partial files cannot be mistaken for finished backups.
        partial = f".questions-20260101T000000Z.db.partial-{os.getpid()}"
        self.assertIsNone(re.match(r"^questions-\d{8}T\d{6}Z\.db$", partial))


if __name__ == "__main__":
    unittest.main()
