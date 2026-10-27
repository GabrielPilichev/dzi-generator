"""tests.browser_support itself: when browser tests skip and how a broken browser fails.

No real browser is needed; fake executables stand in for one.
"""

import os
import shutil
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import browser_support
from tests.browser_support import Browser, BrowserError, chromium_path

CACHE = "Library/Caches/ms-playwright/chromium_headless_shell-{}/chrome-headless-shell-mac-arm64/chrome-headless-shell"


class BrowserSupportTest(unittest.TestCase):

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="learnpilot-browser-home-"))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        environ = {key: value for key, value in os.environ.items() if key != "LEARNPILOT_CHROMIUM"}
        environ["HOME"] = str(self.home)
        patcher = patch.dict(os.environ, environ, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def executable(self, name, script):
        path = self.home / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n" + script + "\n", encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return str(path)

    def test_no_configured_or_cached_browser_is_the_only_skip(self):
        self.assertIsNone(chromium_path())

    def test_the_newest_cached_build_is_chosen(self):
        for version in ("999", "1223", "1100"):
            self.executable(CACHE.format(version), "exit 0")
        self.assertIn("chromium_headless_shell-1223/", chromium_path())

    def test_a_configured_browser_is_used_even_when_it_is_wrong(self):
        self.executable(CACHE.format("1223"), "exit 0")
        os.environ["LEARNPILOT_CHROMIUM"] = "/nonexistent/chromium"
        self.assertEqual(chromium_path(), "/nonexistent/chromium")
        with self.assertRaisesRegex(BrowserError, "cannot start browser '/nonexistent/chromium'"):
            Browser(client=None)

    def test_a_browser_that_exits_fails_with_its_status_and_stderr(self):
        os.environ["LEARNPILOT_CHROMIUM"] = self.executable("crash", "echo 'GPU process exploded' >&2; exit 3")
        with self.assertRaises(BrowserError) as raised:
            Browser(client=None)
        message = str(raised.exception)
        self.assertIn("did not start", message)
        self.assertIn("exit status 3", message)
        self.assertIn("GPU process exploded", message)

    def test_a_silent_browser_times_out_and_is_cleaned_up(self):
        os.environ["LEARNPILOT_CHROMIUM"] = self.executable("silent", "exec sleep 60")
        profiles = set(Path(tempfile.gettempdir()).glob("learnpilot-chromium-*"))
        processes = []
        real_popen = browser_support.subprocess.Popen

        def popen(*args, **kwargs):
            processes.append(real_popen(*args, **kwargs))
            return processes[-1]

        started = time.monotonic()
        with patch.object(browser_support.subprocess, "Popen", popen), \
                self.assertRaisesRegex(BrowserError, r"no answer within 1(\.0)?s"):
            Browser(client=None, timeout=1)
        self.assertLess(time.monotonic() - started, 15)
        self.assertIsNotNone(processes[0].poll())                     # killed, not left running
        self.assertEqual(set(Path(tempfile.gettempdir()).glob("learnpilot-chromium-*")), profiles)


if __name__ == "__main__":
    unittest.main()
