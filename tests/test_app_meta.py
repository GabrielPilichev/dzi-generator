import os
import tempfile
import unittest
from pathlib import Path

from tests.support import IsolatedAppTestCase, web_app


class AppMetaContextTest(IsolatedAppTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.project_root = Path(self.temp_dir.name)

        self.old_project_root = web_app.PROJECT_ROOT
        self.old_db_path = web_app.app.config["DB_PATH"]
        self.old_source_url = os.environ.get("DZI_SOURCE_URL")
        self.addCleanup(self._restore_app_meta_state)

        web_app.PROJECT_ROOT = self.project_root

    def _restore_app_meta_state(self):
        web_app.PROJECT_ROOT = self.old_project_root
        web_app.app.config["DB_PATH"] = self.old_db_path
        if self.old_source_url is None:
            os.environ.pop("DZI_SOURCE_URL", None)
        else:
            os.environ["DZI_SOURCE_URL"] = self.old_source_url

    def test_inject_app_meta_uses_configured_db_and_vault_mtime(self):
        db_path = self.project_root / "questions.db"
        db_path.write_bytes(b"sqlite fixture")
        vault_path = self.project_root / "vault"
        vault_path.mkdir()
        os.utime(db_path, (1_700_000_000, 1_700_000_000))
        os.utime(vault_path, (1_700_000_600, 1_700_000_600))
        web_app.app.config["DB_PATH"] = str(db_path)
        os.environ["DZI_SOURCE_URL"] = "https://example.test/source"

        meta = web_app.inject_app_meta()

        self.assertEqual(meta["app_version"], "UI pass 9")
        self.assertEqual(
            meta["last_sync_label"],
            web_app.datetime.fromtimestamp(1_700_000_600).strftime("%Y-%m-%d %H:%M"),
        )
        self.assertEqual(meta["source_url"], "https://example.test/source")

    def test_inject_app_meta_handles_missing_db_without_broad_exception(self):
        web_app.app.config["DB_PATH"] = str(self.project_root / "missing.db")

        meta = web_app.inject_app_meta()

        self.assertIsNone(meta["last_sync_label"])
        self.assertEqual(meta["app_version"], "UI pass 9")


if __name__ == "__main__":
    unittest.main()
