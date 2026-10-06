"""The seed-database tripwire and the connect_db guard, using only temp files."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests import seed_guard
from tests.seed_guard import (
    ROOT, SeedDatabaseAccess, allow_seed_inspection, guard_connect_db,
    seed_changes, seed_fingerprint,
)


class TemporarySeedTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="seed-guard-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.seed = self.root / "seed.db"
        self.seed.write_bytes(b"seed bytes")


class FingerprintTest(TemporarySeedTestCase):
    def test_unchanged_seed_reports_no_changes(self):
        baseline = seed_fingerprint(self.seed)
        self.assertEqual(seed_changes(baseline, self.seed), [])
        self.assertEqual(baseline["seed.db-wal"], None)

    def test_changed_database_or_new_wal_is_reported(self):
        baseline = seed_fingerprint(self.seed)
        self.seed.write_bytes(b"changed")
        self.assertEqual(len(seed_changes(baseline, self.seed)), 1)
        self.seed.write_bytes(b"seed bytes")
        (self.root / "seed.db-wal").write_bytes(b"committed frames")
        self.assertIn("seed.db-wal", seed_changes(baseline, self.seed)[0])

    def test_removed_database_is_reported(self):
        baseline = seed_fingerprint(self.seed)
        self.seed.unlink()
        self.assertEqual(len(seed_changes(baseline, self.seed)), 1)


class TripwireProcessTest(TemporarySeedTestCase):
    def run_suite_stand_in(self, mutation):
        script = (
            "import sys\nfrom pathlib import Path\n"
            "from tests.seed_guard import install_seed_tripwire\n"
            "seed = Path(sys.argv[1])\ninstall_seed_tripwire(seed)\n" + mutation
        )
        return subprocess.run(
            [sys.executable, "-B", "-c", script, str(self.seed)], cwd=ROOT,
            capture_output=True, text=True,
        )

    def test_clean_run_exits_normally(self):
        result = self.run_suite_stand_in("pass\n")
        self.assertEqual((result.returncode, result.stderr), (0, ""))

    def test_modified_seed_fails_the_run_even_though_nothing_else_failed(self):
        result = self.run_suite_stand_in("seed.write_bytes(b'dirtied by a test')\n")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("SEED DATABASE TRIPWIRE", result.stderr)

    def test_a_sidecar_appearing_fails_the_run(self):
        result = self.run_suite_stand_in(
            "Path(str(seed) + '-wal').write_bytes(b'frames')\n")
        self.assertEqual(result.returncode, 1, result.stderr)

    def test_installing_twice_keeps_the_first_baseline(self):
        result = self.run_suite_stand_in(
            "seed.write_bytes(b'dirtied')\ninstall_seed_tripwire(seed)\n")
        self.assertEqual(result.returncode, 1, result.stderr)


class GuardedFactoryTest(TemporarySeedTestCase):
    def setUp(self):
        super().setUp()
        self.calls = []

        def fake_connect_db(path, **kwargs):
            self.calls.append((path, kwargs))
            return "connection"

        self.guarded = guard_connect_db(fake_connect_db, self.seed)

    def test_other_databases_pass_through_unchanged(self):
        other = self.root / "other.db"
        self.assertEqual(self.guarded(other, query_only=True), "connection")
        self.assertEqual(self.guarded(":memory:"), "connection")
        self.assertEqual(self.calls, [(other, {"query_only": True}), (":memory:", {})])

    def test_seed_is_refused_however_it_is_spelled(self):
        link = self.root / "link.db"
        link.symlink_to(self.seed)
        for spelling in (self.seed, str(self.seed), self.root / "." / "seed.db", link):
            for query_only in (False, True):
                with self.subTest(spelling=str(spelling), query_only=query_only):
                    with self.assertRaises(SeedDatabaseAccess):
                        self.guarded(spelling, query_only=query_only)
        self.assertEqual(self.calls, [])

    def test_explicit_inspection_is_allowed_only_inside_the_block(self):
        with allow_seed_inspection():
            self.assertEqual(self.guarded(self.seed, query_only=True), "connection")
        with self.assertRaises(SeedDatabaseAccess):
            self.guarded(self.seed)
        self.assertEqual(self.calls, [(self.seed, {"query_only": True})])

    def test_the_real_seed_is_the_protected_default(self):
        self.assertEqual(seed_guard.SEED_DB, ROOT / "data" / "questions.db")
        with self.assertRaises(SeedDatabaseAccess):
            guard_connect_db(lambda path, **kwargs: "never")(ROOT / "data" / "questions.db")


if __name__ == "__main__":
    unittest.main()
