import contextlib
import os
import sqlite3
import stat
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import src.backup_application as backup_module
from src.backup_application import backup_application, estimate_snapshot_bytes
from src.backup_sqlite import BackupError


class ApplicationBackupTest(unittest.TestCase):
    def test_snapshot_contains_wal_commits_uploads_and_only_generated_vault(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "questions.db"
            uploads = root / "uploads"
            vault = root / "vault"
            out = root / "backups"
            for path in (uploads, vault / "Generated/Quizzes", out):
                path.mkdir(parents=True)
            (uploads / "answer.xlsx").write_bytes(b"answer")
            (vault / "Generated/Quizzes/attempt.md").write_text("result")
            (vault / "server-secret.env").write_text("must not back up")
            writer = sqlite3.connect(db)
            try:
                writer.execute("PRAGMA journal_mode=WAL")
                writer.execute("CREATE TABLE answers (answer TEXT)")
                writer.execute("INSERT INTO answers VALUES ('committed in WAL')")
                writer.commit()
                snapshot = backup_application(db, uploads, vault, out, stamp="20261001T010203Z")
                self.assertEqual(snapshot.name, "snapshot-20261001T010203Z")
                with sqlite3.connect(snapshot / "questions.db") as copy:
                    self.assertEqual(copy.execute("SELECT answer FROM answers").fetchone()[0],
                                     "committed in WAL")
                with tarfile.open(snapshot / "files.tar.gz") as archive:
                    names = archive.getnames()
                    self.assertIn("uploads/practical/answer.xlsx", names)
                    self.assertIn("vault/Generated/Quizzes/attempt.md", names)
                    self.assertNotIn("vault/server-secret.env", names)
                    self.assertNotIn("questions.db", names)
                    self.assertEqual(archive.extractfile("uploads/practical/answer.xlsx").read(), b"answer")
                self.assertEqual(stat.S_IMODE(snapshot.stat().st_mode), 0o700)
                for filename in ("questions.db", "files.tar.gz"):
                    self.assertEqual(stat.S_IMODE((snapshot / filename).stat().st_mode), 0o600)
                with self.assertRaises(BackupError):
                    backup_application(db, uploads, vault, out, stamp="20261001T010203Z")
            finally:
                writer.close()

    def test_missing_recovery_directory_fails_without_partial_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "questions.db"
            db.write_bytes(b"unused")
            (root / "uploads").mkdir()
            (root / "backups").mkdir()
            with self.assertRaises(BackupError):
                backup_application(db, root / "uploads", root / "missing-vault",
                                   root / "backups", stamp="20261001T010203Z")
            self.assertEqual(list((root / "backups").iterdir()), [])


class _Layout:
    """Temp state directory: db, uploads, vault/Generated, backups."""

    def __init__(self, test):
        tmp = tempfile.TemporaryDirectory()
        test.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.db = self.root / "questions.db"
        self.uploads = self.root / "uploads"
        self.vault = self.root / "vault"
        self.out = self.root / "backups"
        for path in (self.uploads, self.vault / "Generated", self.out):
            path.mkdir(parents=True)
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE t (v TEXT)")
            conn.execute("INSERT INTO t VALUES ('row')")
        (self.uploads / "answer.csv").write_bytes(b"a" * 1000)
        (self.vault / "Generated" / "note.md").write_text("note")

    def run(self, **kwargs):
        return backup_application(self.db, self.uploads, self.vault, self.out,
                                  stamp="20261001T010203Z", **kwargs)

    def artifacts(self):
        return sorted(path.name for path in self.out.iterdir())


class FreeSpaceAndFailureTest(unittest.TestCase):
    def setUp(self):
        self.layout = _Layout(self)

    def test_estimate_counts_only_archived_scope_with_overhead(self):
        base = estimate_snapshot_bytes(self.layout.db, self.layout.uploads, self.layout.vault)
        floor = backup_module.FREE_SPACE_HEADROOM_BYTES + backup_module.TAR_FIXED_OVERHEAD_BYTES
        self.assertGreater(base, floor + 1000)  # includes real bytes, overhead and headroom
        # Out-of-scope data does not change the estimate.
        (self.layout.vault / "unrelated-big.bin").write_bytes(b"x" * 5_000_000)
        (self.layout.vault / "server-secret.env").write_text("secret")
        (self.layout.root / "elsewhere.bin").write_bytes(b"x" * 5_000_000)
        Path(f"{self.layout.db}-shm").write_bytes(b"s" * 5_000_000)
        self.assertEqual(estimate_snapshot_bytes(self.layout.db, self.layout.uploads, self.layout.vault), base)
        # In-scope data does.
        (self.layout.uploads / "more.zip").write_bytes(b"z" * 2_000_000)
        self.assertGreater(estimate_snapshot_bytes(self.layout.db, self.layout.uploads, self.layout.vault),
                           base + 2_000_000)

    def test_estimate_does_not_follow_symlinks(self):
        outside = self.layout.root / "outside.bin"
        outside.write_bytes(b"x" * 5_000_000)
        base = estimate_snapshot_bytes(self.layout.db, self.layout.uploads, self.layout.vault)
        (self.layout.uploads / "link").symlink_to(outside)
        self.assertLess(estimate_snapshot_bytes(self.layout.db, self.layout.uploads, self.layout.vault),
                        base + 1_000_000)

    def test_insufficient_free_space_fails_before_any_work_or_publication(self):
        required = estimate_snapshot_bytes(self.layout.db, self.layout.uploads, self.layout.vault)
        with patch.object(backup_module.shutil, "disk_usage",
                          return_value=SimpleNamespace(free=required - 1)), \
                patch.object(backup_module, "backup_database") as database_backup:
            with self.assertRaisesRegex(BackupError, "not enough free space.*No snapshot was created"):
                self.layout.run()
        database_backup.assert_not_called()
        self.assertEqual(self.layout.artifacts(), [])

    def test_exactly_enough_free_space_succeeds(self):
        required = estimate_snapshot_bytes(self.layout.db, self.layout.uploads, self.layout.vault)
        with patch.object(backup_module.shutil, "disk_usage", return_value=SimpleNamespace(free=required)):
            snapshot = self.layout.run()
        self.assertEqual(self.layout.artifacts(), [snapshot.name])

    def test_archive_failure_leaves_no_snapshot_and_no_partial_database(self):
        with patch.object(backup_module.tarfile, "open", side_effect=OSError("No space left on device")):
            with self.assertRaisesRegex(BackupError, "file archive failed.*no snapshot was published"):
                self.layout.run()
        self.assertEqual(self.layout.artifacts(), [])  # no snapshot-*, no .snapshot-* staging

    def test_cli_reports_failure_with_nonzero_exit(self):
        argv = ["--db", str(self.layout.db), "--uploads", str(self.layout.uploads),
                "--vault", str(self.layout.vault), "--output-dir", str(self.layout.out)]
        with patch.object(backup_module.tarfile, "open", side_effect=OSError("boom")):
            with self.assertRaises(SystemExit) as raised:
                backup_module.main(argv)
        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(self.layout.artifacts(), [])

    def test_normal_snapshot_is_wal_safe_complete_and_never_overwritten(self):
        writer = sqlite3.connect(self.layout.db)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT INTO t VALUES ('only in wal')")
        writer.commit()
        snapshot = self.layout.run()
        self.assertEqual(sorted(path.name for path in snapshot.iterdir()), ["files.tar.gz", "questions.db"])
        with sqlite3.connect(snapshot / "questions.db") as copy:
            self.assertEqual([r[0] for r in copy.execute("SELECT v FROM t ORDER BY rowid")], ["row", "only in wal"])
        before = {path.name: path.read_bytes() for path in snapshot.iterdir()}
        with self.assertRaisesRegex(BackupError, "already exists"):
            self.layout.run()
        self.assertEqual({path.name: path.read_bytes() for path in snapshot.iterdir()}, before)
        self.assertEqual(self.layout.artifacts(), [snapshot.name])


class DestinationOverlapTest(unittest.TestCase):
    def setUp(self):
        self.layout = _Layout(self)

    def assert_rejected(self, db=None, uploads=None, vault=None, out=None):
        layout = self.layout
        with self.assertRaises(BackupError):
            backup_application(db or layout.db, uploads or layout.uploads, vault or layout.vault,
                               out or layout.out, stamp="20261001T010203Z")

    def test_output_inside_a_source_tree_is_rejected(self):
        for parent in (self.layout.uploads, self.layout.vault, self.layout.vault / "Generated"):
            inside = parent / "backups"
            inside.mkdir(exist_ok=True)
            with self.subTest(parent=parent.name):
                self.assert_rejected(out=inside)
                self.assertEqual(list(inside.iterdir()), [])

    def test_output_equal_to_or_containing_a_source_tree_is_rejected(self):
        self.assert_rejected(out=self.layout.uploads)
        self.assert_rejected(out=self.layout.root)  # ancestor of uploads, vault and the database

    def test_sources_may_not_overlap_each_other(self):
        nested = self.layout.vault / "uploads-here"
        nested.mkdir()
        self.assert_rejected(uploads=nested)
        self.assert_rejected(uploads=self.layout.vault)

    def test_database_inside_an_archived_tree_is_rejected(self):
        for tree in ("uploads", "vault"):
            moved = getattr(self.layout, tree) / "questions.db"
            moved.write_bytes(self.layout.db.read_bytes())
            with self.subTest(tree=tree):
                self.assert_rejected(db=moved)

    def test_dotdot_and_symlink_aliases_do_not_bypass_the_check(self):
        alias = self.layout.root / "out-alias"
        alias.symlink_to(self.layout.uploads)
        self.assert_rejected(out=alias)
        self.assert_rejected(out=self.layout.out / ".." / "uploads")
        self.assertEqual(list(self.layout.uploads.iterdir()), [self.layout.uploads / "answer.csv"])

    def test_separate_sibling_directories_are_accepted(self):
        snapshot = self.layout.root / "backups" / "snapshot-20261001T010203Z"
        self.assertEqual(backup_application(self.layout.db, self.layout.uploads, self.layout.vault,
                                            self.layout.out, stamp="20261001T010203Z"), snapshot)


class RestoreDocumentationTest(unittest.TestCase):
    def test_documented_layout_and_order_match_the_produced_snapshot(self):
        layout = _Layout(self)
        snapshot = layout.run()
        guide = (Path(__file__).resolve().parents[1] / "docs/DEPLOYMENT.md").read_text(encoding="utf-8")
        restore = guide[guide.index("## 17. Restore"):guide.index("## 18. Logs")]

        # Extracting the archive under a state root recreates exactly the documented paths.
        state = layout.root / "state"
        state.mkdir()
        with tarfile.open(snapshot / "files.tar.gz") as archive:
            self.assertTrue(all(name.split("/")[0] in {"uploads", "vault"} for name in archive.getnames()))
            archive.extractall(state)
        self.assertEqual((state / "uploads/practical/answer.csv").read_bytes(), b"a" * 1000)
        self.assertEqual((state / "vault/Generated/note.md").read_text(), "note")
        for documented in ("uploads/practical/", "vault/", "snapshot-<stamp>/questions.db", "files.tar.gz"):
            self.assertIn(documented, guide)

        # Every piece of state is restored before the service is started again.
        stop = restore.index("systemctl stop learnpilot")
        database = restore.index("questions.db' ") if "questions.db' " in restore else restore.index('"$SNAP/questions.db" /var')
        extract = restore.index("xzf")
        start = restore.index("systemctl start learnpilot")
        self.assertLess(stop, database)
        self.assertLess(database, extract)
        self.assertLess(extract, start)
        self.assertEqual(restore.count("systemctl start learnpilot"), 1)
        self.assertNotIn("files-<stamp>", guide)

    def test_guide_states_one_worker_requirement_and_single_host(self):
        guide = (Path(__file__).resolve().parents[1] / "docs/DEPLOYMENT.md").read_text(encoding="utf-8")
        for expected in ("One worker is a validated requirement", "BEGIN IMMEDIATE",
                         "5-second busy timeout", "Do not raise\n`workers`", "exactly **one** trusted hostname",
                         "no database-only fallback", "not enough free space", "systemctl is-failed learnpilot-backup.service"):
            self.assertIn(expected, guide)
