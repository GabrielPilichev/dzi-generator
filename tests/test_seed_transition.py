"""G2: what is still allowed to touch the tracked seed before H, pinned explicitly.

The seed (``data/questions.db``) is still tracked until H, but no normal test or developer
database construction depends on it. The only remaining copies of its bytes are the transition
tests listed here: schema parity, canonical parity/G1 equivalence, baseline adoption and the byte
copy helper's own tests. Anything else that starts copying the seed fails this inventory.
"""

import ast
import hashlib
import re
import unittest
from pathlib import Path

from src import canonical_reconcile as reconcile
from tests.support import ROOT

SEED = ROOT / "data" / "questions.db"
SIDECARS = [Path(str(SEED) + suffix) for suffix in ("-wal", "-shm", "-journal")]

# test module -> why it still copies the seed (all intentionally transitional until H)
SEED_COPY_ALLOWED = {
    "test_canonical_content.py": "G1 equivalence: canonical rebuild vs the legacy seed, seed immutability",
    "test_schema_baseline.py": "000_baseline + migrations must equal the seed's schema (baseline adoption)",
    "test_production_deployment.py": "the baseline-adoption startup test needs a database that predates 000",
    "test_seed_database_copy.py": "unit tests of the byte-copy helper itself",
    "test_isolation.py": "unit tests of the byte-copy helper (changed-while-copying, WAL)",
    "test_seed_transition.py": "this inventory",
}


def python_files(directory):
    return sorted(path for path in Path(directory).rglob("*.py") if "__pycache__" not in path.parts)


class SeedStateTest(unittest.TestCase):
    def test_seed_is_the_audited_g1_seed_and_unchanged(self):
        self.assertEqual(hashlib.sha256(SEED.read_bytes()).hexdigest(), reconcile.SEED_SHA256)
        self.assertEqual(hashlib.sha256(SEED.read_bytes()).hexdigest(),
                         "47dc58348b08b59ce2c4e1081fcd6d99cdda1b6003eddfbd81fd4f65d20cb91e")

    def test_seed_has_no_sidecars(self):
        for sidecar in SIDECARS:
            self.assertFalse(sidecar.exists(), sidecar.name)

    def test_seed_is_still_tracked_h_has_not_happened(self):
        self.assertTrue(SEED.is_file(), "G2 must not untrack the seed; that is H")


class SeedCopyInventoryTest(unittest.TestCase):
    def callers_of_copy_seed_database(self):
        found = {}
        for path in python_files(ROOT / "tests"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    name = getattr(node.func, "id", getattr(node.func, "attr", None))
                    if name == "copy_seed_database":
                        found.setdefault(path.name, []).append(node.lineno)
        return found

    def test_only_listed_transition_tests_copy_the_seed(self):
        callers = set(self.callers_of_copy_seed_database())
        self.assertEqual(callers - {"support.py"}, callers & set(SEED_COPY_ALLOWED), callers)

    def test_every_listed_exception_still_exists_and_is_documented(self):
        for name, reason in SEED_COPY_ALLOWED.items():
            self.assertTrue((ROOT / "tests" / name).is_file(), name)
            self.assertTrue(reason)

    def test_support_helpers_used_by_normal_tests_never_call_the_seed_copy(self):
        tree = ast.parse((ROOT / "tests" / "support.py").read_text(encoding="utf-8"))
        offenders = []
        for function in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
            if function.name == "copy_seed_database":
                continue
            for node in ast.walk(function):
                if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "copy_seed_database":
                    offenders.append(function.name)
        self.assertEqual(offenders, [])

    def test_no_test_copies_the_seed_by_other_means(self):
        pattern = re.compile(r"(shutil\.copy\w*|copyfile|copy2)\([^)]*questions\.db")
        offenders = [path.name for path in python_files(ROOT / "tests")
                     if path.name != "test_seed_transition.py"
                     and pattern.search(path.read_text(encoding="utf-8"))]
        self.assertEqual(offenders, [])

    def test_source_code_never_copies_the_seed_to_build_a_database(self):
        for path in python_files(ROOT / "src") + python_files(ROOT / "web"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("copy_seed_database", text, path.name)


if __name__ == "__main__":
    unittest.main()
