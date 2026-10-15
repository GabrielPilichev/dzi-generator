import contextlib
import gc
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
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


class RestoreLockTest(unittest.TestCase):
    """No snapshot of a state that a restore has only partly replaced (docs/DEPLOYMENT.md, 17)."""

    def test_no_snapshot_is_taken_while_a_restore_is_in_progress_or_unfinished(self):
        from web.services import lifecycle

        layout = _Layout(self)
        lock = Path(lifecycle.restore_lock_path(layout.db))
        self.assertEqual(lock, layout.root / lifecycle.RESTORE_LOCK_NAME)   # next to the database
        lock.mkdir()
        (lock / "pid").write_text("1\n")
        # Whatever else is the matter (here: the uploads are being moved), the lock is what is reported.
        layout.uploads.rename(layout.root / "uploads-set-aside")
        with self.assertRaises(BackupError) as caught:
            layout.run()
        self.assertIn("a restore of this database is in progress or was left unfinished", str(caught.exception))
        self.assertIn("no snapshot is taken of a partly replaced state", str(caught.exception))
        self.assertEqual(layout.artifacts(), [])                             # nothing published, nothing staged
        code = subprocess.run(
            [sys.executable, "-B", "-m", "src.backup_application", "--db", str(layout.db), "--uploads",
             str(layout.root / "uploads-set-aside"), "--vault", str(layout.vault), "--output-dir", str(layout.out)],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
        self.assertEqual(code.returncode, 1)
        self.assertIn("LearnPilot backup failed: a restore of this database is in progress", code.stderr)
        # The lock gone and the uploads back: the same call makes a snapshot.
        (lock / "pid").unlink()
        lock.rmdir()
        (layout.root / "uploads-set-aside").rename(layout.uploads)
        self.assertTrue((layout.run() / "questions.db").is_file())

    def test_a_restore_that_begins_while_the_snapshot_is_being_made_publishes_nothing(self):
        # Claude review, N2. The lock was looked for once, before anything was read. A backup that
        # had passed that look when the restore took the lock went on to publish what it read
        # while the restore was replacing it.
        from web.services import lifecycle

        layout = _Layout(self)
        lock = Path(lifecycle.restore_lock_path(layout.db))
        copy_the_database = backup_module.backup_database

        def and_a_restore_begins(source, target):
            copy_the_database(source, target)
            lock.mkdir()

        with patch.object(backup_module, "backup_database", and_a_restore_begins):
            with self.assertRaises(BackupError) as caught:
                layout.run()
        self.assertIn("a restore of this database began while the snapshot was being made", str(caught.exception))
        self.assertIn("nothing was published", str(caught.exception))
        self.assertEqual(layout.artifacts(), [])                             # nothing published, nothing staged
        lock.rmdir()
        self.assertTrue((layout.run() / "questions.db").is_file())


class ReplacedStateTest(unittest.TestCase):
    """Independent review of PR #326, low 1. A backup started by hand, outside systemd, can span a
    whole restore: the lock is absent when it looks first and absent again when it looks last. It
    then published the database from before the restore with the uploads and the vault from after
    it, as one complete snapshot. What it reads is now identified as objects, not as paths."""

    def setUp(self):
        from web.services import lifecycle

        self.layout = _Layout(self)
        self.lock = Path(lifecycle.restore_lock_path(self.layout.db))
        self.aside = self.layout.root / "replaced-20261001T000000Z"
        self.descriptors = self.open_descriptors()
        self.working_directory = os.getcwd()
        self.addCleanup(self.assert_nothing_left_open_or_changed)

    @staticmethod
    def open_descriptors():
        gc.collect()        # what an earlier test left to be collected is not this one's to count
        return len(os.listdir("/dev/fd"))

    def assert_nothing_left_open_or_changed(self):
        self.assertEqual(self.open_descriptors(), self.descriptors)       # whichever way the backup ended
        self.assertEqual(os.getcwd(), self.working_directory)

    # --- what a restore does to the state, step by step (deploy/learnpilot-restore.sh) ---

    def set_aside(self, path, name):
        self.aside.mkdir(exist_ok=True)
        path.rename(self.aside / name)

    def replace_uploads(self):
        self.set_aside(self.layout.uploads, "uploads")
        self.layout.uploads.mkdir()
        (self.layout.uploads / "restored.csv").write_bytes(b"from the snapshot that was restored")

    def replace_vault(self):
        self.set_aside(self.layout.vault, "vault")
        (self.layout.vault / "Generated").mkdir(parents=True)
        (self.layout.vault / "Generated" / "restored.md").write_text("restored")

    def replace_database(self):
        staged = self.layout.root / "staged.db"
        with contextlib.closing(sqlite3.connect(staged)) as conn, conn:
            conn.execute("CREATE TABLE t (v TEXT)")
            conn.execute("INSERT INTO t VALUES ('restored')")
        self.aside.mkdir(exist_ok=True)
        os.link(self.layout.db, self.aside / "questions.db")       # the one that was live keeps a name,
        os.replace(staged, self.layout.db)                          # and loses its place in one rename

    def a_whole_restore(self):
        self.lock.mkdir()
        self.replace_uploads()
        self.replace_vault()
        self.replace_database()
        self.lock.rmdir()

    # --- when it happens: after the database was copied, or after the files were archived too ---

    def after_the_database_was_copied(self, happens):
        copy_the_database = backup_module.backup_database

        def copy_and_then(source, target):
            result = copy_the_database(source, target)
            happens()
            return result

        return patch.object(backup_module, "backup_database", copy_and_then)

    def before_the_last_look(self, happens):
        """Just before the lock is looked for the second time: everything has been read by then."""
        look = backup_module.restore_lock_present
        looks = []

        def look_again(db):
            looks.append(db)
            if len(looks) == 2:
                happens()
            return look(db)

        return patch.object(backup_module, "restore_lock_present", look_again)

    def members(self, snapshot):
        with tarfile.open(snapshot / "files.tar.gz") as archive:
            return {member.name for member in archive.getmembers() if member.isfile()}

    def rows(self, snapshot):
        with contextlib.closing(sqlite3.connect(snapshot / "questions.db")) as conn:
            return [row[0] for row in conn.execute("SELECT v FROM t")]

    def assert_refused(self, *because):
        with self.assertRaises(BackupError) as caught:
            self.layout.run()
        message = str(caught.exception)
        self.assertIn("the state changed hands while the snapshot was being made", message)
        self.assertIn("nothing was published", message)
        for text in because:
            self.assertIn(text, message)
        self.assertEqual(self.layout.artifacts(), [])               # nothing published, nothing staged
        return message

    def test_a_backup_that_spans_a_whole_restore_publishes_nothing(self):
        # The database is copied; then a restore begins, replaces everything and finishes. Both
        # looks for the lock find none.
        with self.after_the_database_was_copied(self.a_whole_restore):
            message = self.assert_refused(
                f"the database was replaced by another ({self.layout.db})",
                f"the uploads directory was replaced by another ({self.layout.uploads})",
                f"the vault directory was replaced by another ({self.layout.vault})")
        self.assertFalse(self.lock.exists())
        self.assertNotIn("a restore of this database", message)     # it was not the lock that stopped it
        # The same backup afterwards is a snapshot of the restored state, all of it.
        snapshot = self.layout.run()
        self.assertEqual(self.rows(snapshot), ["restored"])
        self.assertEqual(self.members(snapshot), {"uploads/practical/restored.csv", "vault/Generated/restored.md"})

    def test_the_same_when_the_restore_falls_after_everything_was_read(self):
        with self.before_the_last_look(self.a_whole_restore):
            self.assert_refused("the database was replaced", "the uploads directory was replaced",
                                "the vault directory was replaced")

    def test_each_of_the_three_is_told_apart_by_what_it_is(self):
        def remake(path):
            shutil.rmtree(path)
            path.mkdir()

        def rewrite(database):
            database.unlink()
            with contextlib.closing(sqlite3.connect(database)) as conn, conn:
                conn.execute("CREATE TABLE t (v TEXT)")

        def a_link_in_place_of(path):
            elsewhere = path.with_name("elsewhere")
            elsewhere.mkdir()
            self.set_aside(path, path.name)
            path.symlink_to(elsewhere)

        replaced, gone = "was replaced by another", "is no longer there"
        cases = {
            "the database replaced by a rename": ("database", replaced, lambda path: self.replace_database()),
            "the database deleted and written again": ("database", replaced, rewrite),
            "the database gone": ("database", gone, lambda path: self.set_aside(path, "questions.db")),
            "the uploads replaced": ("uploads directory", replaced, lambda path: self.replace_uploads()),
            "the uploads deleted and made again": ("uploads directory", replaced, remake),
            "the uploads gone": ("uploads directory", gone, lambda path: self.set_aside(path, "uploads")),
            "a link where the uploads were": ("uploads directory", replaced, a_link_in_place_of),
            "the vault replaced": ("vault directory", replaced, lambda path: self.replace_vault()),
            "the vault deleted and made again": ("vault directory", replaced, remake),
            "the vault gone": ("vault directory", gone, lambda path: self.set_aside(path, "vault")),
            "a link where the vault was": ("vault directory", replaced, a_link_in_place_of),
        }
        for label, (name, what, change) in cases.items():
            with self.subTest(changed=label):
                self.layout = layout = _Layout(self)
                self.aside = layout.root / "replaced-20261001T000000Z"
                path = {"database": layout.db, "uploads directory": layout.uploads,
                        "vault directory": layout.vault}[name]
                with self.before_the_last_look(lambda: change(path)):
                    message = self.assert_refused()
                # That one is named, with its path, and neither of the other two.
                found = message.split("being made: ", 1)[1].split(". The copy may hold", 1)[0]
                self.assertEqual(found, f"the {name} {what} ({path})")

    def test_a_restore_that_is_taken_back_leaves_a_snapshot_of_one_state(self):
        # The restore replaces the uploads and the vault, fails at a later gate and moves everything
        # back, and the files are archived exactly while the other directories are in place. At the
        # end every path leads where it led at the start, so a comparison finds nothing. The archive
        # is therefore read from the directories the backup began with, wherever they are.
        def begins():
            self.lock.mkdir()
            self.replace_uploads()
            self.replace_vault()

        def is_taken_back():
            for name in ("uploads", "vault"):
                path = self.layout.root / name
                path.rename(self.layout.root / f"staged-{name}")
                (self.aside / name).rename(path)
            self.lock.rmdir()

        with self.after_the_database_was_copied(begins), self.before_the_last_look(is_taken_back):
            snapshot = self.layout.run()
        self.assertEqual(self.rows(snapshot), ["row"])
        self.assertEqual(self.members(snapshot), {"uploads/practical/answer.csv", "vault/Generated/note.md"})

    def test_a_backup_while_the_service_writes_is_published_as_before(self):
        # What changes inside the database and the directories is not a replacement of them.
        def the_service_writes():
            with contextlib.closing(sqlite3.connect(self.layout.db)) as conn, conn:
                conn.execute("INSERT INTO t VALUES ('written during the backup')")
            (self.layout.uploads / "class-12").mkdir()
            (self.layout.uploads / "class-12" / "late.zip").write_bytes(b"z" * 100)
            (self.layout.uploads / "answer.csv").unlink()
            (self.layout.vault / "Generated" / "later.md").write_text("later")
            os.chmod(self.layout.uploads, 0o750)

        with self.after_the_database_was_copied(the_service_writes):
            snapshot = self.layout.run()
        self.assertEqual(self.rows(snapshot), ["row"])                       # the database as it was copied,
        self.assertEqual(self.members(snapshot),                             # the files slightly newer
                         {"uploads/practical/class-12/late.zip", "vault/Generated/note.md", "vault/Generated/later.md"})
        self.assertEqual(self.layout.artifacts(), [snapshot.name])

    def test_paths_given_through_links_are_followed_once_and_asked_again_at_the_end(self):
        # DZI_UPLOAD_ROOT may be a link to a volume. The link is followed when the backup starts.
        link = self.layout.root / "uploads-by-link"
        link.symlink_to(self.layout.uploads)
        arguments = (self.layout.db, link, self.layout.vault, self.layout.out)
        snapshot = backup_application(*arguments, stamp="20261001T010203Z")
        self.assertEqual(self.members(snapshot), {"uploads/practical/answer.csv", "vault/Generated/note.md"})

        # Pointed at another directory while the backup runs: what was archived is no longer what
        # the name stands for.
        def repointed():
            other = self.layout.root / "other-uploads"
            other.mkdir()
            link.unlink()
            link.symlink_to(other)

        with self.before_the_last_look(repointed):
            with self.assertRaises(BackupError) as caught:
                backup_application(*arguments, stamp="20261001T020304Z")
        self.assertIn(f"the uploads directory was replaced by another ({link})", str(caught.exception))
        self.assertEqual(self.layout.artifacts(), [snapshot.name])

    def test_a_database_that_is_not_there_is_reported_as_before(self):
        self.layout.db.unlink()
        with self.assertRaisesRegex(BackupError, "source database not found"):
            self.layout.run()
        self.assertEqual(self.layout.artifacts(), [])

    def test_the_database_file_is_never_opened_in_a_way_that_could_drop_its_locks(self):
        # Closing an ordinary descriptor of the database would release the POSIX locks of every
        # SQLite connection this process has on it. It is held with O_PATH or not at all.
        opened = []
        real_open = os.open

        def recording(path, flags, *args, **kwargs):
            opened.append((os.fspath(path), flags))
            return real_open(path, flags, *args, **kwargs)

        with patch.object(backup_module.os, "open", recording):
            self.layout.run()
        of_the_database = [flags for path, flags in opened if path == str(self.layout.db)]
        for flags in of_the_database:
            self.assertTrue(hasattr(os, "O_PATH") and flags & os.O_PATH, flags)
        self.assertEqual(len(of_the_database), 1 if hasattr(os, "O_PATH") else 0)
        self.assertEqual(sorted(path for path, _flags in opened if path in (str(self.layout.uploads), str(self.layout.vault))),
                         sorted([str(self.layout.uploads), str(self.layout.vault)]))


class ArchiveThroughDescriptorsTest(unittest.TestCase):
    """The uploads and the vault are archived from open directories, not from paths (see
    ``ReplacedStateTest``). The archive has to be the one ``TarFile.add`` wrote before."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        tree = self.tree = self.root / "practical"
        (tree / "class 12" / "Задача 26").mkdir(parents=True)
        (tree / "empty").mkdir()
        (tree / "class 12" / "answer.xlsx").write_bytes(os.urandom(70_000))
        (tree / "class 12" / "Задача 26" / "решение.html").write_text("<p>решение</p>", encoding="utf-8")
        (tree / "empty.txt").write_bytes(b"")
        os.link(tree / "class 12" / "answer.xlsx", tree / "the-same-answer.xlsx")      # a hard link
        (tree / "link-to-a-file").symlink_to("class 12/answer.xlsx")
        (tree / "link-to-a-directory").symlink_to("class 12")
        (tree / "link-to-nothing").symlink_to("nowhere")
        os.mkfifo(tree / "a-pipe")
        os.chmod(tree / "class 12", 0o750)
        os.chmod(tree / "empty.txt", 0o640)
        os.utime(tree / "empty.txt", (1_700_000_000, 1_700_000_123.5))

    def described(self, path):
        with tarfile.open(path) as archive:
            return [(member.name, member.type, member.mode, member.uid, member.gid, member.size, member.mtime,
                     member.linkname, member.uname, member.gname, member.devmajor, member.devminor,
                     archive.extractfile(member).read() if member.isreg() else None)
                    for member in archive.getmembers()]

    def by_path(self, tree, name, **options):
        with tarfile.open(self.root / name, "w:gz") as tar:
            tar.add(tree, arcname="uploads/practical", **options)
        return self.described(self.root / name)

    def by_descriptor(self, descriptor, name, **options):
        with tarfile.open(self.root / name, "w:gz") as tar:
            backup_module._add_directory(tar, descriptor, "uploads/practical", **options)
        return self.described(self.root / name)

    def test_the_archive_is_member_for_member_the_one_tarfile_add_writes(self):
        expected = self.by_path(self.tree, "by-path.tar.gz")
        kinds = {member[1] for member in expected}
        self.assertEqual(kinds, {tarfile.DIRTYPE, tarfile.REGTYPE, tarfile.LNKTYPE, tarfile.SYMTYPE, tarfile.FIFOTYPE})
        self.assertEqual(len(expected), 12)
        gc.collect()
        before = len(os.listdir("/dev/fd"))
        descriptor = os.open(self.tree, os.O_RDONLY)
        try:
            self.assertEqual(self.by_descriptor(descriptor, "by-descriptor.tar.gz"), expected)
            # A link to a directory is archived as the link it is, and not walked into.
            self.assertNotIn("uploads/practical/link-to-a-directory/answer.xlsx", [member[0] for member in expected])
            # The top directory alone, as for the vault.
            self.assertEqual(self.by_descriptor(descriptor, "top-by-descriptor.tar.gz", recursive=False),
                             self.by_path(self.tree, "top-by-path.tar.gz", recursive=False))
        finally:
            os.close(descriptor)
        self.assertEqual(len(os.listdir("/dev/fd")), before)        # every descriptor it opened is closed again

    def test_what_is_archived_is_the_directory_that_was_opened_wherever_it_is_by_now(self):
        expected = self.by_path(self.tree, "by-path.tar.gz")
        descriptor = os.open(self.tree, os.O_RDONLY)
        try:
            # Set aside, and another directory put at its path, as a restore does.
            aside = self.root / "replaced-20261001T000000Z"
            aside.mkdir()
            self.tree.rename(aside / "practical")
            self.tree.mkdir()
            (self.tree / "restored.csv").write_bytes(b"from another state")
            self.assertEqual(self.by_descriptor(descriptor, "by-descriptor.tar.gz"), expected)
        finally:
            os.close(descriptor)
        self.assertEqual([member[0] for member in self.by_path(self.tree, "the-other.tar.gz")],
                         ["uploads/practical", "uploads/practical/restored.csv"])

    def test_an_entry_that_stops_being_a_file_while_it_is_read_stops_the_backup(self):
        # Between the look at an entry and its opening a link is put in its place: it is not followed.
        outside = self.root / "outside.txt"
        outside.write_text("not part of the uploads")
        real_stat = os.stat

        def and_then_a_link(name, *args, **kwargs):
            result = real_stat(name, *args, **kwargs)
            if name == "empty.txt" and kwargs.get("dir_fd") is not None:
                (self.tree / "empty.txt").unlink()
                (self.tree / "empty.txt").symlink_to(outside)
            return result

        descriptor = os.open(self.tree, os.O_RDONLY)
        try:
            with patch.object(backup_module.os, "stat", and_then_a_link):
                with self.assertRaises(OSError):
                    self.by_descriptor(descriptor, "by-descriptor.tar.gz")
        finally:
            os.close(descriptor)


class ChangedEntryTest(unittest.TestCase):
    """Follow-up to the independent review of PR #326. A file in the uploads can become something
    else between the look at it and its opening. Whatever it became, the backup ends, publishes
    nothing, and leaves nothing open and nothing staged."""

    PATIENCE = 10       # seconds; a backup of these few files takes a small fraction of one

    def setUp(self):
        self.layout = _Layout(self)
        self.entry = self.layout.uploads / "answer.csv"
        self.descriptors = ReplacedStateTest.open_descriptors()

    def becomes(self, put_in_its_place):
        """The entry is looked at as the file it is; when it is opened, something else has its name."""
        real_stat = os.stat

        def and_then_something_else(name, *args, **kwargs):
            result = real_stat(name, *args, **kwargs)
            if name == self.entry.name and kwargs.get("dir_fd") is not None:
                self.entry.unlink()
                put_in_its_place(self.entry)
            return result

        return patch.object(backup_module.os, "stat", and_then_something_else)

    def outcome(self):
        """What the backup ends with. It runs aside, so that one that waits fails this test
        instead of stopping the suite."""
        ended = []

        def run():
            try:
                ended.append(self.layout.run())
            except BaseException as raised:      # the test looks at it
                ended.append(raised)

        backup = threading.Thread(target=run, daemon=True)
        backup.start()
        backup.join(self.PATIENCE)
        if backup.is_alive():
            # A writer lets the opening return, and the suite goes on to report this.
            writer = os.open(self.entry, os.O_WRONLY | os.O_NONBLOCK)
            backup.join(self.PATIENCE)
            os.close(writer)
            self.fail("the backup waited for a writer on the pipe")
        return ended[0]

    def assert_nothing_published_staged_or_left_open(self):
        self.assertEqual(self.layout.artifacts(), [])               # no snapshot-*, no .snapshot-* staging
        self.assertEqual(ReplacedStateTest.open_descriptors(), self.descriptors)

    def test_a_file_that_became_a_directory_leaves_no_descriptor_open(self):
        # A directory opens for reading as a file does, and it is ``os.fdopen`` that refuses it. The
        # descriptor it had been handed stayed open then, one more for every entry met like this.
        with self.becomes(Path.mkdir):
            with self.assertRaisesRegex(BackupError, "file archive failed.*no snapshot was published") as caught:
                self.layout.run()
        self.assertIsInstance(caught.exception.__cause__, IsADirectoryError)    # refused there, closed once
        self.assertTrue(self.entry.is_dir())
        self.assert_nothing_published_staged_or_left_open()

    def test_a_file_that_became_a_pipe_is_refused_without_being_waited_for(self):
        # Opening a pipe for reading waits for a writer, for ever when there is none, and the backup
        # would stand still with its staging directory in place. It is opened with O_NONBLOCK, found
        # to be no file any more, and refused.
        with self.becomes(os.mkfifo):
            ended = self.outcome()
        self.assertIsInstance(ended, BackupError, ended)
        self.assertIn("file archive failed (changed while it was being archived: uploads/practical/answer.csv)",
                      str(ended))
        self.assertIn("no snapshot was published", str(ended))
        self.assertTrue(stat.S_ISFIFO(self.entry.lstat().st_mode))  # it was the pipe that was met
        self.assert_nothing_published_staged_or_left_open()
        # A pipe that is there when it is looked at is archived as the pipe it is, as before.
        with tarfile.open(self.layout.run() / "files.tar.gz") as archive:
            self.assertTrue(archive.getmember("uploads/practical/answer.csv").isfifo())


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

        # Every piece of state is restored before the service is started again. The restore is the
        # script the guide names (its gates are tested in tests/test_lifecycle_restore_script.py).
        self.assertIn("deploy/learnpilot-restore.sh", restore)
        script = (Path(__file__).resolve().parents[1] / "deploy/learnpilot-restore.sh").read_text(encoding="utf-8")
        stop = script.index('gate "stop the service" "${AS_OWNER[@]}" systemctl stop "$SERVICE"')
        database = script.index('"$SNAPSHOT/questions.db" "$STAGED_DB"')
        extract = script.index('-xzf "$SNAPSHOT/files.tar.gz"')
        files_in_place = script.index('gate "put the restored $tree in place"')
        database_in_place = script.index('gate "replace the live database by the copy"')
        start = script.index('systemctl start "$SERVICE"')
        self.assertLess(stop, database)
        self.assertLess(database, extract)
        self.assertLess(extract, files_in_place)
        self.assertLess(files_in_place, database_in_place)
        self.assertLess(database_in_place, start)
        self.assertEqual(script.count('systemctl start "$SERVICE"'), 1)
        self.assertNotIn("systemctl start", restore)   # the guide gives no start to type after a restore
        self.assertNotIn("files-<stamp>", guide)

    def test_guide_states_one_worker_requirement_and_single_host(self):
        guide = (Path(__file__).resolve().parents[1] / "docs/DEPLOYMENT.md").read_text(encoding="utf-8")
        for expected in ("One worker is a validated requirement", "BEGIN IMMEDIATE",
                         "5-second busy timeout", "Do not raise\n`workers`", "exactly **one** trusted hostname",
                         "no database-only fallback", "not enough free space", "systemctl is-failed learnpilot-backup.service"):
            self.assertIn(expected, guide)
