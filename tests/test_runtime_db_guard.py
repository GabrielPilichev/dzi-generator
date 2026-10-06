"""The connect_db guard for the local runtime database, using only temp files."""

import tempfile
import unittest
from pathlib import Path

from tests import runtime_db_guard
from tests.runtime_db_guard import ROOT, RuntimeDatabaseAccess, guard_connect_db


class GuardedFactoryTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="runtime-guard-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.protected = self.root / "runtime.db"
        self.protected.write_bytes(b"runtime bytes")
        self.calls = []

        def fake_connect_db(path, **kwargs):
            self.calls.append((path, kwargs))
            return "connection"

        self.guarded = guard_connect_db(fake_connect_db, self.protected)

    def test_other_databases_pass_through_unchanged(self):
        other = self.root / "other.db"
        self.assertEqual(self.guarded(other, query_only=True), "connection")
        self.assertEqual(self.guarded(":memory:"), "connection")
        self.assertEqual(self.calls, [(other, {"query_only": True}), (":memory:", {})])

    def test_protected_database_is_refused_however_it_is_spelled(self):
        link = self.root / "link.db"
        link.symlink_to(self.protected)
        for spelling in (self.protected, str(self.protected), self.root / "." / "runtime.db", link):
            for query_only in (False, True):
                with self.subTest(spelling=str(spelling), query_only=query_only):
                    with self.assertRaises(RuntimeDatabaseAccess):
                        self.guarded(spelling, query_only=query_only)
        self.assertEqual(self.calls, [])

    def test_a_missing_runtime_database_is_still_refused(self):
        self.protected.unlink()
        with self.assertRaises(RuntimeDatabaseAccess):
            self.guarded(self.protected)

    def test_the_default_protected_path_is_the_ignored_runtime_database(self):
        self.assertEqual(runtime_db_guard.RUNTIME_DB, ROOT / "data" / "questions.db")
        with self.assertRaises(RuntimeDatabaseAccess):
            guard_connect_db(lambda path, **kwargs: "never")(ROOT / "data" / "questions.db")


if __name__ == "__main__":
    unittest.main()
