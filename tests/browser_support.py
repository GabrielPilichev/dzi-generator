"""A headless Chromium for layout and keyboard tests, with no package to install.

The browser speaks the DevTools protocol over ``--remote-debugging-pipe`` (file
descriptors 3 and 4, NUL-separated JSON), so the standard library is enough. Every
request to ``http://localhost/`` is paused by the browser and answered by a Flask test
client: pages, static files and form posts run the real application on the test's
temporary database, and no server or port is opened.

Requests to any other host are refused (``Fetch.failRequest``) and listed in
``blocked``, so a page can neither reach the network nor silently depend on it.

Discovery: ``LEARNPILOT_CHROMIUM`` names the executable (any Chromium or Chrome with
``--headless`` and ``--remote-debugging-pipe``); otherwise the newest Playwright
``chromium_headless_shell`` build in the user's cache. Only when the variable is unset
and no cached build exists is ``chromium_path()`` ``None`` and the browser tests skip.
A configured or found browser that does not start, crashes or stops answering raises
``BrowserError`` (with its exit status and last stderr lines): a failure, never a skip.

Each protocol call has one deadline, ``timeout`` seconds (20 by default) from its start,
for writing the request, reading the answer and serving the requests that arrive
meanwhile. Writes are non-blocking, so a browser that stops reading a large response
cannot hold the test past it, and partial progress never extends it. A missed deadline
or a closed pipe raises ``PipeError``, after which the pipe is not used again: a message
may be half written. ``close()`` asks a working browser to quit, kills it after 5 seconds
(at once after a ``PipeError``), and removes its temporary profile. If
the test process is killed instead, the browser sees the pipe close and exits by itself;
only its ``learnpilot-chromium-*`` profile is then left in the temporary directory.
Browser zoom is emulated, not driven through the browser's zoom control: 200% of a
1280px window is a 640 CSS px viewport at device scale 2. POSIX only (``fcntl``).
"""

from __future__ import annotations

import base64
import fcntl
import glob
import json
import os
import re
import select
import shutil
import subprocess
import tempfile
import time
from urllib.parse import urlsplit

ORIGIN = "http://localhost"   # the Flask test client's cookie host; nothing listens here

_CACHES = (
    "~/Library/Caches/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell",
    "~/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell",
)

_KEYS = {"Tab": 9, "Enter": 13, "Escape": 27}


def chromium_path() -> str | None:
    """The configured browser as given (a wrong path then fails the tests, it does not
    skip them), else the newest cached headless shell, else ``None``."""
    configured = os.environ.get("LEARNPILOT_CHROMIUM")
    if configured:
        return configured
    for pattern in _CACHES:
        found = glob.glob(os.path.expanduser(pattern))
        if found:
            return max(found, key=lambda path: int(re.search(r"chromium_headless_shell-(\d+)", path).group(1)))
    return None


def _above_four(descriptor: int) -> int:
    # dup2 onto its own number would leave a descriptor close-on-exec in the child.
    moved = fcntl.fcntl(descriptor, fcntl.F_DUPFD_CLOEXEC, 10)
    os.close(descriptor)
    return moved


class BrowserError(RuntimeError):
    pass


class PipeError(BrowserError):
    """The DevTools pipe timed out or closed; the browser is not usable any more."""


class Browser:
    """One headless Chromium with one page; ``client`` answers its requests and may be
    replaced between pages (another account, the same browser)."""

    def __init__(self, client, *, timeout: float = 20.0):
        self.client = client
        self.timeout = timeout
        self.requests: list[tuple[str, str]] = []
        self.blocked: list[str] = []
        self._buffer = b""
        self._next_id = 0
        self._session = None
        self._broken = False
        self._profile = tempfile.mkdtemp(prefix="learnpilot-chromium-")
        self._stderr = open(os.path.join(self._profile, "stderr.log"), "wb")
        to_browser_read, self._to_browser = os.pipe()
        self._from_browser, from_browser_write = os.pipe()
        to_browser_read, from_browser_write = _above_four(to_browser_read), _above_four(from_browser_write)

        def descriptors():
            os.dup2(to_browser_read, 3)
            os.dup2(from_browser_write, 4)

        executable = chromium_path()
        try:
            self._process = subprocess.Popen(
                [executable, "--headless", "--remote-debugging-pipe", f"--user-data-dir={self._profile}",
                 "--no-first-run", "--no-default-browser-check", "--disable-extensions",
                 "--force-color-profile=srgb", "--hide-scrollbars", "about:blank"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=self._stderr,
                preexec_fn=descriptors, close_fds=False)   # the tests are single-threaded
        except (OSError, TypeError) as error:
            self._process = None
            self.close()
            raise BrowserError(f"cannot start browser {executable!r}: {error}") from error
        finally:
            os.close(to_browser_read)
            os.close(from_browser_write)
        os.set_blocking(self._to_browser, False)
        try:
            target = self._call("Target.createTarget", {"url": "about:blank"})["targetId"]
            self._session = self._call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
            self.send("Page.enable")
            self.send("Fetch.enable", {"patterns": [{"urlPattern": "*"}]})
        except BrowserError as error:
            detail = self._diagnostics()
            self.close()
            raise BrowserError(f"browser {executable!r} did not start: {error}{detail}") from error

    # -- protocol ---------------------------------------------------------------------

    def _failed(self, message: str) -> PipeError:
        self._broken = True
        return PipeError(message)

    def _read(self, deadline: float) -> dict:
        while b"\0" not in self._buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self._from_browser], [], [], remaining)[0]:
                raise self._failed(f"no answer within {self.timeout}s")
            chunk = os.read(self._from_browser, 1 << 16)
            if not chunk:
                raise self._failed("the browser closed the DevTools pipe" + self._diagnostics())
            self._buffer += chunk
        raw, self._buffer = self._buffer.split(b"\0", 1)
        return json.loads(raw)

    def _write(self, message: dict, deadline: float) -> None:
        """All of ``message`` before ``deadline``, as fast as the browser reads it."""
        if self._broken:
            raise PipeError("the DevTools pipe failed earlier and is not used again")
        data = memoryview(json.dumps(message).encode() + b"\0")
        size = len(data)
        while data:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([], [self._to_browser], [], remaining)[1]:
                raise self._failed(f"the browser stopped reading: {len(data)} of {size} bytes "
                                   f"unsent within {self.timeout}s")
            try:
                data = data[os.write(self._to_browser, data):]
            except (BlockingIOError, InterruptedError):   # full again, or a signal: wait again
                pass
            except BrokenPipeError:
                raise self._failed("the browser closed the DevTools pipe" + self._diagnostics()) from None

    def _call(self, method: str, params: dict | None = None, session: str | None = None) -> dict:
        deadline = time.monotonic() + self.timeout   # before writing: a write can wait too
        self._next_id += 1
        message_id = self._next_id
        message = {"id": message_id, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        self._write(message, deadline)
        while True:
            message = self._read(deadline)
            if message.get("id") == message_id:
                if "error" in message:
                    raise BrowserError(f"{method}: {message['error']}")
                return message.get("result", {})
            if message.get("method") == "Fetch.requestPaused":
                self._answer(message["params"], deadline)

    def send(self, method: str, params: dict | None = None) -> dict:
        return self._call(method, params, self._session)

    def _diagnostics(self) -> str:
        time.sleep(0.2)
        status = self._process.poll() if self._process else None
        try:
            with open(os.path.join(self._profile, "stderr.log"), "rb") as log:
                tail = log.read()[-600:].decode("utf-8", "replace").strip()
        except OSError:
            tail = ""
        return f" (exit status {status}; stderr: {tail or 'empty'})"

    def _answer(self, paused: dict, deadline: float) -> None:
        """Serve one paused request from the Flask test client (its cookies, not the browser's)."""
        request = paused["request"]
        url = urlsplit(request["url"])
        if f"{url.scheme}://{url.netloc}" != ORIGIN:
            self.blocked.append(request["url"])
            self._write({"id": 0, "sessionId": self._session, "method": "Fetch.failRequest",
                         "params": {"requestId": paused["requestId"], "errorReason": "BlockedByClient"}},
                        deadline)
            return
        path = url.path + (f"?{url.query}" if url.query else "")
        self.requests.append((request["method"], path))
        headers = {name: value for name, value in request.get("headers", {}).items()
                   if name.lower() not in ("cookie", "host", "content-length")}
        body = "".join(base64.b64decode(entry.get("bytes", "")).decode("utf-8")
                       for entry in request.get("postDataEntries", ()))
        response = self.client.open(path, method=request["method"], headers=headers,
                                    data=body.encode("utf-8") if body else None, base_url=ORIGIN)
        try:   # closed (its teardown callbacks run) even when the browser cannot take it
            reply = [{"name": name, "value": value} for name, value in response.headers.items()
                     if name.lower() != "content-length"]
            # Sent without waiting: its acknowledgement is skipped like any unrelated message.
            self._write({"id": 0, "sessionId": self._session, "method": "Fetch.fulfillRequest", "params": {
                "requestId": paused["requestId"], "responseCode": response.status_code,
                "responseHeaders": reply, "body": base64.b64encode(response.get_data()).decode()}},
                deadline)
        finally:
            response.close()

    # -- page -------------------------------------------------------------------------

    def viewport(self, width: int, height: int, scale: float = 1) -> None:
        self.send("Emulation.setDeviceMetricsOverride",
                  {"width": width, "height": height, "deviceScaleFactor": scale, "mobile": False})

    def media(self, **features) -> None:
        """``media(prefers_reduced_motion="reduce")``; no arguments resets."""
        self.send("Emulation.setEmulatedMedia", {"features": [
            {"name": name.replace("_", "-"), "value": value} for name, value in features.items()]})

    def scripts(self, enabled: bool) -> None:
        """Page scripts on or off, from the next document on. ``eval`` still works."""
        self.send("Emulation.setScriptExecutionDisabled", {"value": not enabled})

    def goto(self, path: str) -> None:
        self.send("Page.navigate", {"url": ORIGIN + path})
        self.wait_for_load(url=ORIGIN + path)

    def document_id(self) -> float:
        """Different for every document; pass it to ``wait_for_load(after=...)``."""
        return self.eval("performance.timeOrigin")

    def wait_for_load(self, *, url: str | None = None, after: float | None = None) -> None:
        """Until the document at ``url``, or any document other than ``after``, has loaded."""
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            try:
                state = self.eval("[document.readyState, location.href, performance.timeOrigin]")
            except PipeError:
                raise
            except BrowserError:   # the previous document is going away
                state = None
            if state and state[0] == "complete" and (state[1] == url if url else state[2] != after):
                self.settle()
                return
            time.sleep(0.02)
        raise BrowserError(f"no page loaded: {url or 'after a navigation'}")

    def settle(self, seconds: float = 0.05) -> None:
        """Let observers and frames run. Waits from here, so it works with scripts off."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.send("Runtime.evaluate", {"expression": "0"})
            time.sleep(0.01)

    def eval(self, expression: str):
        result = self.send("Runtime.evaluate", {"expression": expression, "returnByValue": True})
        if "exceptionDetails" in result:
            details = result["exceptionDetails"]
            raise BrowserError(details.get("exception", {}).get("description") or details.get("text"))
        return result["result"].get("value")

    def key(self, key: str, *, shift: bool = False) -> None:
        code = _KEYS[key]
        event = {"key": key, "code": key, "windowsVirtualKeyCode": code, "nativeVirtualKeyCode": code,
                 "modifiers": 8 if shift else 0}
        self.send("Input.dispatchKeyEvent", {"type": "rawKeyDown", **event})
        if key == "Enter":
            self.send("Input.dispatchKeyEvent", {"type": "char", **event, "text": "\r"})
        self.send("Input.dispatchKeyEvent", {"type": "keyUp", **event})
        self.settle()

    def click(self, x: float, y: float) -> None:
        for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
            self.send("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y, "button": "left",
                                                   "clickCount": 0 if kind == "mouseMoved" else 1})
        self.settle()

    def click_selector(self, selector: str, index: int = 0) -> None:
        """A real click at the centre of the ``index``-th match, after scrolling it into view
        as a user would."""
        point = self.eval(f"""(() => {{
            const element = document.querySelectorAll({json.dumps(selector)})[{index}];
            if (!element) return null;
            element.scrollIntoView({{block: "nearest", inline: "nearest"}});
            const box = element.getBoundingClientRect();
            return [box.left + box.width / 2, box.top + box.height / 2];
        }})()""")
        if point is None:
            raise BrowserError(f"no element {selector} [{index}]")
        self.click(*point)

    def close(self) -> None:
        try:
            if self._process is not None and self._process.poll() is None:
                if not self._broken:
                    try:
                        self._call("Browser.close")
                        self._process.wait(5)
                    except (BrowserError, OSError, subprocess.TimeoutExpired):
                        pass
                if self._process.poll() is None:
                    self._process.kill()
                self._process.wait(5)
        finally:
            for descriptor in (self._to_browser, self._from_browser):
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            self._stderr.close()
            shutil.rmtree(self._profile, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
