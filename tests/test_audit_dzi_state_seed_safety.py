"""The DZI audit must treat the tracked seed as immutable: no sidecars, no byte changes."""

import hashlib
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "data" / "questions.db"
SIDECARS = [Path(str(SEED) + suffix) for suffix in ("-wal", "-shm", "-journal")]


class AuditSeedSafetyTest(unittest.TestCase):
    def test_default_audit_leaves_no_sidecars_and_does_not_change_the_seed(self):
        for sidecar in SIDECARS:
            self.assertFalse(sidecar.exists(), f"{sidecar.name} existed before the audit")
        before = hashlib.sha256(SEED.read_bytes()).hexdigest()
        result = subprocess.run(
            [sys.executable, str(ROOT / "src" / "audit_dzi_state.py")],
            cwd=ROOT, capture_output=True, text=True, timeout=120,
        )
        # Exit status 1 is the audit's own finding (a missing asset file), not a failure here.
        self.assertIn(result.returncode, (0, 1), result.stderr)
        self.assertIn("foreign key check rows", result.stdout)
        for sidecar in SIDECARS:
            self.assertFalse(sidecar.exists(), f"audit created {sidecar.name}")
        self.assertEqual(hashlib.sha256(SEED.read_bytes()).hexdigest(), before)


if __name__ == "__main__":
    unittest.main()
