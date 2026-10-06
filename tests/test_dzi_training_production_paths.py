import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.support import ROOT, IsolatedAppTestCase, build_test_database, web_app


class TrainingGeneratorPathTest(unittest.TestCase):
    def test_explicit_paths_generate_outside_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "questions.db"
            build_test_database(db)
            vault = root / "vault"
            vault.mkdir()
            output = vault / "Generated/DZI-Training/sets"
            cmd = [sys.executable, "-B", "src/generate_dzi_training_set.py",
                   "--db", str(db), "--config", str(ROOT / "deploy/dzi-training-class-mix.json"),
                   "--data-output-dir", str(output), "--vault-output-dir", str(output),
                   "--count", "1", "--seed", "path-test", "--name", "Path test"]
            env = {**os.environ, "DZI_VAULT": str(vault), "PYTHONDONTWRITEBYTECODE": "1"}
            result = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(len(list(output.glob("*.md"))), 1)
            metadata = json.loads(next(output.glob("*.json")).read_text())
            self.assertTrue(Path(metadata["markdown_path"]).is_relative_to(vault.resolve()))
            self.assertEqual(metadata["requested_count"], 1)

            missing_cmd = cmd.copy()
            missing_cmd[missing_cmd.index("--config") + 1] = str(root / "missing.json")
            missing = subprocess.run(missing_cmd, cwd=ROOT, env=env,
                                     capture_output=True, text=True)
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn("Missing DZI training config", missing.stderr)

            checkout_db_cmd = cmd.copy()
            checkout_db_cmd[checkout_db_cmd.index("--db") + 1] = str(ROOT / "data/questions.db")
            rejected = subprocess.run(checkout_db_cmd, cwd=ROOT, env=env,
                                      capture_output=True, text=True)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("database must be outside the checkout", rejected.stderr)


class TrainingRoutePathTest(IsolatedAppTestCase):
    def test_teacher_route_passes_configured_external_paths(self):
        client = web_app.app.test_client()
        with client.session_transaction() as sess:
            sess["admin_authenticated"] = True
            sess["ui_profile"] = "admin"
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "ok", "")) as run:
            response = client.post("/teacher/dzi-training", data={"count": "1"})
        self.assertEqual(response.status_code, 200)
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[cmd.index("--db") + 1], str(Path(web_app.app.config["DB_PATH"]).resolve()))
        self.assertEqual(cmd[cmd.index("--config") + 1],
                         str((ROOT / "deploy/dzi-training-class-mix.json").resolve()))
        for option in ("--data-output-dir", "--vault-output-dir"):
            self.assertTrue(Path(cmd[cmd.index(option) + 1]).is_relative_to(web_app.QUIZ_VAULT_PATH))
