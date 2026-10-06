import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from tests.support import ROOT, copy_seed_database


def make_wal_source(directory: Path, *, wal_rows: int) -> Path:
    """A WAL-mode DB; `wal_rows` rows are committed only in the -wal file."""
    live = directory / "live.db"
    conn = sqlite3.connect(live)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        conn.execute("INSERT INTO t (v) VALUES ('in-main')")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        for n in range(wal_rows):
            conn.execute("INSERT INTO t (v) VALUES (?)", (f"in-wal-{n}",))
        conn.commit()
        # Snapshot the files while the writer is still open so the WAL is real.
        source = directory / "source.db"
        shutil.copyfile(live, source)
        if (directory / "live.db-wal").exists():
            shutil.copyfile(directory / "live.db-wal", directory / "source.db-wal")
    finally:
        conn.close()
    return source


class SeedDatabaseCopyTest(unittest.TestCase):
    def copy_and_read(self, source: Path, destination: Path):
        copy_seed_database(source, destination)
        self.assertFalse(Path(str(destination) + "-wal").exists())
        self.assertFalse(Path(str(destination) + "-shm").exists())
        # Same read-only open the DZI generator uses; must work with no sidecars.
        with closing(sqlite3.connect(destination.as_uri() + "?mode=ro", uri=True)) as conn:
            return [row[0] for row in conn.execute("SELECT v FROM t ORDER BY id")]

    def test_committed_wal_content_is_preserved_without_sidecars(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = make_wal_source(root, wal_rows=2)
            self.assertGreater((root / "source.db-wal").stat().st_size, 0)
            self.assertEqual(self.copy_and_read(source, root / "copy.db"),
                             ["in-main", "in-wal-0", "in-wal-1"])

    def test_source_without_wal_sidecar_is_openable_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = make_wal_source(root, wal_rows=0)
            for stray in root.glob("*.db-*"):
                stray.unlink()
            self.assertEqual(self.copy_and_read(source, root / "copy.db"), ["in-main"])

    def test_source_with_empty_wal_sidecar_is_openable_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = make_wal_source(root, wal_rows=0)
            for stray in root.glob("*.db-*"):
                stray.unlink()
            Path(str(source) + "-wal").touch()
            self.assertEqual(self.copy_and_read(source, root / "copy.db"), ["in-main"])

    def test_tracked_seed_copy_is_standalone_and_source_is_untouched(self):
        seed = ROOT / "data/questions.db"
        sidecars = [Path(str(seed) + s) for s in ("-wal", "-shm")]
        before = {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in [seed, *sidecars] if p.exists()}
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "questions.db"
            copy_seed_database(seed, destination)
            self.assertEqual(list(Path(directory).glob("questions.db-*")), [])
            with closing(sqlite3.connect(destination.as_uri() + "?mode=ro", uri=True)) as conn:
                self.assertGreater(conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0], 0)
        after = {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in [seed, *sidecars] if p.exists()}
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
