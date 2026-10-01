"""The DZI tooling fixture stays repo-relative on every supported Python.

``tempfile.mkdtemp(dir=".")`` returned a relative path before Python 3.12 and an
absolute path since. Both behaviours are emulated here, so a run on either
interpreter proves the fixture (and the tools' normal path) for the other one.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.validate_confirmed_dzi_image_files import validate_image_dir
from tests.repo_relative_tempdir import RepoRelativeTemporaryDirectory

_real_mkdtemp = tempfile.mkdtemp


def _mkdtemp_absolute(suffix=None, prefix=None, dir=None):
    """Python >= 3.12 behaviour."""
    return os.path.abspath(_real_mkdtemp(suffix, prefix, dir))


def _mkdtemp_relative(suffix=None, prefix=None, dir=None):
    """Python < 3.12 behaviour for a relative ``dir``."""
    return os.path.join(dir, os.path.basename(_real_mkdtemp(suffix, prefix, dir)))


class RepoRelativeTemporaryDirectoryTest(unittest.TestCase):
    def assert_repo_relative_fixture(self):
        fixture = RepoRelativeTemporaryDirectory()
        try:
            with fixture as name:
                path = Path(name)
                self.assertEqual(name, fixture.relative_name)
                self.assertFalse(path.is_absolute())
                self.assertNotIn("..", path.parts)
                self.assertTrue(path.is_dir())
                self.assertEqual(path.resolve().parent, Path.cwd().resolve())
                image_dir = path / "web" / "static" / "img" / "questions" / "dzi"
                self.assertEqual(validate_image_dir(image_dir), [])
            self.assertFalse(path.exists())  # cleaned up through the original name
        finally:
            fixture.cleanup()

    def test_relative_when_tempfile_returns_an_absolute_path(self):
        with patch.object(tempfile, "mkdtemp", _mkdtemp_absolute):
            raw = tempfile.TemporaryDirectory(dir=".")
            self.addCleanup(raw.cleanup)
            self.assertTrue(os.path.isabs(raw.name))  # the stale-fixture condition
            self.assertIn("image-dir must be repo-relative", validate_image_dir(Path(raw.name)))
            self.assert_repo_relative_fixture()

    def test_relative_when_tempfile_returns_a_relative_path(self):
        with patch.object(tempfile, "mkdtemp", _mkdtemp_relative):
            raw = tempfile.TemporaryDirectory(dir=".")
            self.addCleanup(raw.cleanup)
            self.assertFalse(os.path.isabs(raw.name))
            self.assert_repo_relative_fixture()

    def test_relative_on_the_running_interpreter(self):
        self.assert_repo_relative_fixture()


if __name__ == "__main__":
    unittest.main()
