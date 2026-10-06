"""H: data/questions.db is ignored runtime state, not a tracked source of truth.

Schema comes from web/migrations, content from data/canonical, and a fresh database from
``python3 -m src.build_database --output PATH``. These checks pin that contract without
needing the file (or git) to exist.
"""

import fnmatch
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support import ROOT

RUNTIME_DB = "data/questions.db"
SIDECARS = [RUNTIME_DB + suffix for suffix in ("-wal", "-shm", "-journal")]


def ignore_patterns():
    lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith(("#", "!"))]


def is_ignored(path):
    for pattern in ignore_patterns():
        pattern = pattern.lstrip("/")
        if "/" not in pattern.rstrip("/"):
            if fnmatch.fnmatch(Path(path).name, pattern.rstrip("/")):
                return True
        elif fnmatch.fnmatch(path, pattern.rstrip("/")) or path.startswith(pattern.rstrip("/") + "/"):
            return True
    return False


class IgnoreRulesTest(unittest.TestCase):
    def test_runtime_database_and_sqlite_sidecars_are_ignored(self):
        for path in (RUNTIME_DB, *SIDECARS):
            self.assertTrue(is_ignored(path), path)

    def test_canonical_content_and_migrations_are_not_ignored(self):
        tracked = [p.relative_to(ROOT).as_posix() for p in (ROOT / "data" / "canonical").iterdir()]
        tracked += [p.relative_to(ROOT).as_posix() for p in (ROOT / "web" / "migrations").glob("*.sql")]
        self.assertIn("data/canonical/manifest.json", tracked)
        self.assertIn("data/canonical/questions.jsonl", tracked)
        for path in tracked:
            self.assertFalse(is_ignored(path), path)

    def test_the_runtime_database_is_not_tracked_by_git(self):
        if shutil.which("git") is None or not (ROOT / ".git").exists():
            self.skipTest("not a git checkout")
        result = subprocess.run(["git", "ls-files", "--", "data/*.db", "data/*.sqlite*"],
                                cwd=ROOT, capture_output=True, text=True)
        if result.returncode != 0:
            self.skipTest("git ls-files unavailable")
        self.assertEqual(result.stdout.split(), [])


class NoSeedDependencyTest(unittest.TestCase):
    def test_test_infrastructure_has_no_seed_copy_or_seed_hash(self):
        offenders = []
        pattern = re.compile(r"copy_seed_database|SEED_SHA256|seed_guard|install_seed_tripwire|adopt_baseline")
        for path in sorted((ROOT / "tests").rglob("*.py")):
            if path.name != Path(__file__).name and pattern.search(path.read_text(encoding="utf-8")):
                offenders.append(path.name)
        self.assertEqual(offenders, [])

    def test_the_seed_transition_machinery_is_gone(self):
        for relative in ("tests/seed_guard.py", "tests/test_seed_transition.py",
                         "tests/test_seed_database_copy.py", "tests/test_audit_dzi_state_seed_safety.py",
                         "src/canonical_reconcile.py"):
            self.assertFalse((ROOT / relative).exists(), relative)

    def test_test_discovery_imports_without_the_runtime_database(self):
        with tempfile.TemporaryDirectory(prefix="learnpilot-discovery-") as directory:
            env = dict(os.environ, DZI_DB=str(Path(directory) / "absent.db"))
            script = ("import unittest\nloader = unittest.TestLoader()\nloader.discover('tests')\n"
                      "assert not loader.errors, loader.errors\n")
            result = subprocess.run([sys.executable, "-B", "-c", script], cwd=ROOT, env=env,
                                    capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(list(Path(directory).iterdir()), [])


class NoAutoBuildTest(unittest.TestCase):
    def test_importing_the_application_never_creates_a_database(self):
        with tempfile.TemporaryDirectory(prefix="learnpilot-noautobuild-") as directory:
            missing = Path(directory) / "questions.db"
            env = dict(os.environ, DZI_DB=str(missing))
            result = subprocess.run([sys.executable, "-B", "-c", "import web.app"], cwd=ROOT, env=env,
                                    capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse(missing.exists())
            self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
