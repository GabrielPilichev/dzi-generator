"""tests.browser_support itself: when browser tests skip and how a broken browser fails.

No real browser is needed; fake executables stand in for one.
"""

import os
import shutil
import signal
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from tests import browser_support
from tests.browser_support import ORIGIN, Browser, BrowserError, PipeError, chromium_path

CACHE = "Library/Caches/ms-playwright/chromium_headless_shell-{}/chrome-headless-shell-mac-arm64/chrome-headless-shell"

LARGE = 4 << 20   # far more than a pipe holds

# Starts like Chromium, then answers Page.navigate with one paused request for /large and
# reads the fulfilment as FAKE_BROWSER_MODE says. It exits by itself after 10 seconds, so
# a harness that blocks on the write fails (broken pipe) instead of hanging the suite.
FAKE_BROWSER = r"""
import base64, json, os, sys, time
mode, log, buffer = os.environ["FAKE_BROWSER_MODE"], os.environ["FAKE_BROWSER_LOG"], b""
end = time.monotonic() + 10

def read():
    global buffer
    while b"\0" not in buffer:
        chunk = os.read(3, 1 << 16)
        if not chunk:
            sys.exit(0)
        buffer += chunk
    raw, buffer = buffer.split(b"\0", 1)
    return json.loads(raw)

def write(message):
    os.write(4, json.dumps(message).encode() + b"\0")

for result in ({"targetId": "target"}, {"sessionId": "session"}, {}, {}):
    write({"id": read()["id"], "result": result})
navigate = read()
write({"method": "Fetch.requestPaused", "sessionId": "session", "params": {"requestId": "request",
       "request": {"method": "GET", "url": "http://localhost/large", "headers": {}}}})
if mode == "exit":
    sys.exit(0)
received = 0
while time.monotonic() < end:
    if mode == "stall":
        time.sleep(0.05)
    elif mode == "trickle":                    # always some progress, never enough
        received += len(os.read(3, 512))
        with open(log + ".new", "w") as out:
            out.write(str(received))
        os.replace(log + ".new", log)          # whole, even if killed meanwhile
        time.sleep(0.01)
    else:                                      # "read": everything, then answer
        fulfilment = read()
        body = base64.b64decode(fulfilment["params"]["body"])
        write({"id": navigate["id"], "result": {"bytes": len(body), "xs": body.count(b"x")}})
        write({"id": read()["id"], "result": {}})   # Browser.close
        sys.exit(0)
"""


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

    def fake_browser(self, mode):
        """A started fake browser whose next ``send`` brings a request for a 4 MiB page."""
        script = self.home / "fake_browser.py"
        script.write_text(FAKE_BROWSER, encoding="utf-8")
        os.environ.update(FAKE_BROWSER_MODE=mode, FAKE_BROWSER_LOG=str(self.home / "received"),
                          LEARNPILOT_CHROMIUM=self.executable("fake", f'exec "{sys.executable}" "{script}"'))
        app = Flask("large")
        app.add_url_rule("/large", "large", lambda: b"x" * LARGE)
        browser = Browser(app.test_client(), timeout=1)
        self.addCleanup(browser.close)
        return browser

    def assert_times_out(self, browser, pattern):
        started = time.monotonic()
        with self.assertRaisesRegex(PipeError, pattern):
            browser.send("Page.navigate", {"url": ORIGIN + "/large"})
        self.assertLess(time.monotonic() - started, 3)   # 1s deadline; the fake lives 10s
        self.assertEqual(browser.requests, [("GET", "/large")])
        process, profile = browser._process, browser._profile
        started = time.monotonic()
        browser.close()
        self.assertLess(time.monotonic() - started, 2)   # killed at once: no Browser.close
        self.assertIsNotNone(process.poll())
        self.assertFalse(os.path.exists(profile))

    def test_a_large_response_is_written_in_full_to_a_reading_browser(self):
        browser = self.fake_browser("read")
        self.assertEqual(browser.send("Page.navigate", {"url": ORIGIN + "/large"}), {"bytes": LARGE, "xs": LARGE})

    def test_a_browser_that_stops_reading_a_large_response_times_out(self):
        browser = self.fake_browser("stall")
        self.assert_times_out(browser, r"the browser stopped reading: \d+ of \d+ bytes unsent within 1s")
        with self.assertRaisesRegex(PipeError, "not used again"):
            browser.send("Runtime.evaluate", {"expression": "0"})

    def test_signals_neither_fail_nor_extend_a_wait_to_write(self):
        browser = self.fake_browser("stall")
        previous = signal.signal(signal.SIGALRM, lambda *_: None)
        self.addCleanup(signal.signal, signal.SIGALRM, previous)
        signal.setitimer(signal.ITIMER_REAL, 0.05, 0.05)   # interrupts every wait, again and again
        self.addCleanup(signal.setitimer, signal.ITIMER_REAL, 0)
        self.assert_times_out(browser, r"the browser stopped reading: \d+ of \d+ bytes unsent within 1s")

    def test_a_browser_reading_too_slowly_times_out_at_the_first_deadline(self):
        browser = self.fake_browser("trickle")
        self.assert_times_out(browser, "the browser stopped reading")
        self.assertGreater(int((self.home / "received").read_text()), 0)   # progress was made

    def test_a_browser_that_closes_the_pipe_mid_response_fails_clearly(self):
        browser = self.fake_browser("exit")
        self.assert_times_out(browser, "the browser closed the DevTools pipe")


if __name__ == "__main__":
    unittest.main()
