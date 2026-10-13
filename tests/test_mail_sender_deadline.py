"""The sender process boundary of ``web.services.mail``
(EMAIL_REGISTRATION PR 2; test plan C5-C8, C11-C14, C17-C21).

Stand-in children replace the sender script: short standard-library scripts written to
a temporary directory and started exactly as the real one is (``-I -S``, by absolute
path). Every stand-in arms its own alarm, so a failing test leaves no process behind.
These tests measure real elapsed time with the deadline, the reaping bound and the
alarm shortened; the one exception is the real script's own alarm (nine seconds, three
children at once). No message is sent, and the only socket is the unconnected listening
socket whose descriptor the child must not inherit (C13).

Nothing here asserts a bound on process creation, on a whole request or on the number
of senders across processes: the design claims none (C16).
"""

import ast
import contextlib
import errno
import json
import logging
import os
import selectors
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from web.services import mail

ROOT = Path(__file__).resolve().parent.parent
REAL_POPEN = subprocess.Popen
RECIPIENT = "pupil@example.org"
BODY_TOKEN = "SYNTHETIC-BODY-918273"
SMTP_PASSWORD = "synthetic-smtp-password-7Q"
SETTINGS = mail.MailSettings(
    backend="smtp", sender_address="no-reply@example.org", sender_name="LearnPilot",
    smtp_host="relay.example.org", smtp_port=587, smtp_security="starttls",
    smtp_username="synthetic-user", smtp_password=SMTP_PASSWORD)
MESSAGE = mail.OutboundEmail(to=RECIPIENT, subject="Synthetic subject", text=BODY_TOKEN)

SAFETY = "import signal\nsignal.alarm(30)\n"
READ_INPUT = "import os\nwhile os.read(0, 65536):\n    pass\n"
ACCEPT = READ_INPUT + "os.write(1, b'R')\nos._exit(0)\n"
BLOCK = "import time\ntime.sleep(60)\n"
MARGIN = 1.5  # scheduling slack on top of the deadline and the reaping bound

UNKNOWN, NOT_SENT, ACCEPTED, REFUSED = (
    mail.SendOutcome.UNKNOWN, mail.SendOutcome.NOT_SENT, mail.SendOutcome.ACCEPTED, mail.SendOutcome.REFUSED)


def pid_exists(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_until(condition, seconds):
    limit = time.monotonic() + seconds
    while time.monotonic() < limit:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


class SenderProcessCase(unittest.TestCase):
    DEADLINE = 5.0  # long: these children end by themselves and must never be cut short
    REAP = 0.5

    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="learnpilot-mail-test-")
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        self.processes = []
        self.popen_calls = []
        self.scripts = 0
        case = self

        class Recording(REAL_POPEN):
            def __init__(self, *args, **kwargs):
                case.popen_calls.append((args, kwargs))
                super().__init__(*args, **kwargs)
                self.stdin_fd = self.stdin.fileno() if self.stdin else None
                self.stdout_fd = self.stdout.fileno() if self.stdout else None
                case.processes.append(self)

        self.Recording = Recording
        self.popen_class = Recording
        for target, name, value in (
                (subprocess, "Popen", lambda *args, **kwargs: case.popen_class(*args, **kwargs)),
                (mail, "COMMUNICATION_DEADLINE", self.DEADLINE), (mail, "REAP_SECONDS", self.REAP)):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        mail._unreaped.clear()
        self.addCleanup(mail._unreaped.clear)
        self.addCleanup(self.kill_leftovers)

    def kill_leftovers(self):
        for proc in self.processes:
            if REAL_POPEN.poll(proc) is None:
                with contextlib.suppress(OSError):
                    os.kill(proc.pid, signal.SIGKILL)
                with contextlib.suppress(Exception):
                    REAL_POPEN.wait(proc, timeout=5)

    def script(self, body):
        self.scripts += 1
        path = os.path.join(self.directory, f"stand_in_{self.scripts}.py")
        Path(path).write_text(SAFETY + textwrap.dedent(body), encoding="utf-8")
        return path

    def path(self, name):
        return os.path.join(self.directory, name)

    def send(self, script, message=MESSAGE):
        """One ``send`` against a stand-in; records the outcome's elapsed time."""
        path = script if os.path.isabs(script) and "\n" not in script else self.script(script)
        started = time.monotonic()
        try:
            with patch.object(mail, "SENDER_SCRIPT", path):
                return mail.SmtpMailer(SETTINGS).send(message)
        finally:
            self.elapsed = time.monotonic() - started

    def assert_nothing_left(self, count=None):
        if count is not None:
            self.assertEqual(len(self.popen_calls), count, "one call creates at most one process")
        for proc in self.processes:
            self.assertIsNotNone(proc.returncode, "the child was reaped")
            self.assertFalse(pid_exists(proc.pid), "the child's pid no longer exists")
            self.assertTrue(proc.stdin.closed and proc.stdout.closed, "no pipe is left open")
        self.assertFalse(mail.sender_busy())

    def record_reads(self):
        """Every ``os.read`` on a child's output: (requested, returned)."""
        real_read, reads, case = os.read, [], self

        def read(descriptor, size):
            data = real_read(descriptor, size)
            if case.processes and descriptor == case.processes[-1].stdout_fd:
                reads.append((size, len(data)))
            return data

        patcher = patch.object(os, "read", read)
        patcher.start()
        self.addCleanup(patcher.stop)
        return reads


class CommunicationDeadlineTest(SenderProcessCase):
    """C5: one deadline for both pipes; every one of these is ``unknown``."""

    DEADLINE = 0.5

    def assert_ended_at_the_deadline(self, outcome):
        self.assertIs(outcome, UNKNOWN)
        self.assertGreaterEqual(self.elapsed, self.DEADLINE)
        self.assertLess(self.elapsed, self.DEADLINE + self.REAP + MARGIN)
        self.assert_nothing_left(1)
        self.assertEqual(self.processes[0].returncode, -signal.SIGKILL)

    def test_a_child_that_writes_nothing_and_blocks_is_unknown_not_not_sent(self):
        self.assert_ended_at_the_deadline(self.send(BLOCK))

    def test_a_child_that_reads_its_input_then_blocks(self):
        self.assert_ended_at_the_deadline(self.send(READ_INPUT + BLOCK))

    def test_a_child_that_writes_the_marker_and_then_blocks(self):
        reads = self.record_reads()
        self.assert_ended_at_the_deadline(self.send("import os\nos.write(1, b'R')\n" + BLOCK))
        self.assertEqual(reads, [(16, 1)], "the marker was read; a timeout still never yields accepted")

    def test_a_child_that_ignores_sigterm(self):
        self.assert_ended_at_the_deadline(self.send(
            "import signal\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nsignal.signal(signal.SIGINT, signal.SIG_IGN)\n"
            "signal.signal(signal.SIGHUP, signal.SIG_IGN)\n" + BLOCK))

    def test_a_child_that_floods_its_output(self):
        reads = self.record_reads()
        self.assert_ended_at_the_deadline(self.send("import os\nwhile True:\n    os.write(1, b'x' * 65536)\n"))
        self.assertEqual(sum(returned for _requested, returned in reads), 16, "at most 16 bytes are ever read")
        self.assertTrue(all(requested <= 16 for requested, _returned in reads))
        self.assertEqual(sum(returned for _requested, returned in reads[:-1]) + reads[-1][1], 16)
        self.assertGreater(reads[-1][1], 0, "the last read is the one that reached the cap: none follows it, none after the kill")

    def test_a_child_that_never_reads_its_input(self):
        # Larger than any pipe buffer, so a blocking write would hang the parent.
        with patch.object(mail, "_payload", lambda settings, message: b"x" * (1 << 20)), \
                patch.object(mail, "MAX_INPUT_BYTES", 1 << 21):
            self.assert_ended_at_the_deadline(self.send(BLOCK))

    def test_a_child_that_closes_its_output_and_keeps_running(self):
        # C20: waited for only for the time that remains, then killed.
        reads = self.record_reads()
        self.assert_ended_at_the_deadline(self.send(READ_INPUT + "os.close(1)\n" + BLOCK))
        self.assertEqual(reads, [(16, 0)], "end-of-file was seen and nothing was read after it")

    def test_marker_and_status_zero_without_end_of_file_is_unknown(self):
        # C6: a grandchild keeps the output open after the sender exited 0.
        outcome = self.send(READ_INPUT + (
            "import subprocess, sys\n"
            "subprocess.Popen([sys.executable, '-I', '-S', '-c', 'import time; time.sleep(%r)'])\n"
            "os.write(1, b'R')\nos._exit(0)\n" % (self.DEADLINE + 1.0)))
        self.assertIs(outcome, UNKNOWN)
        self.assertGreaterEqual(self.elapsed, self.DEADLINE)
        self.assert_nothing_left(1)

    def test_the_deadline_is_monotonic_not_wall_clock(self):
        # C20: a wall clock that jumps an hour ahead, or back, changes nothing.
        real_time = time.time
        for step in (3600.0, -3600.0):
            self.processes.clear()
            self.popen_calls.clear()
            calls = []

            def stepped():
                calls.append(1)
                return real_time() + step * len(calls)

            with patch.object(time, "time", stepped):
                self.assert_ended_at_the_deadline(self.send(BLOCK))
        self.assertNotIn("time.time", Path(mail.__file__).read_text(encoding="utf-8"))

    def test_send_does_not_raise_and_does_not_retry(self):
        # C11.
        for body in (BLOCK, "raise SystemExit(1)\n", "import os\nos.write(1, b'R')\n" + BLOCK):
            self.processes.clear()
            self.popen_calls.clear()
            self.assertIs(self.send(body), UNKNOWN)
            self.assert_nothing_left(1)


class OutcomeRuleTest(SenderProcessCase):
    """C6 and C20, row by row. These children end by themselves."""

    def outcome(self, body, expected):
        self.processes.clear()
        self.popen_calls.clear()
        outcome = self.send(READ_INPUT + body)
        self.assertIs(outcome, expected)
        self.assertLess(self.elapsed, self.DEADLINE, "returned at once, not at the deadline")
        self.assert_nothing_left(1)
        return self.processes[0]

    def test_exit_10_to_19_with_no_output_is_not_sent(self):
        for status in (10, 11, 12, 15, 19):
            with self.subTest(status=status):
                self.assertEqual(self.outcome(f"os._exit({status})\n", NOT_SENT).returncode, status)

    def test_marker_and_exit_zero_is_accepted(self):
        reads = self.record_reads()
        self.outcome("os.write(1, b'R')\nos._exit(0)\n", ACCEPTED)
        self.assertEqual(sum(returned for _requested, returned in reads), 1)
        self.assertEqual(reads[-1][1], 0, "end-of-file was seen")

    def test_marker_and_exit_20_is_refused(self):
        self.outcome("os.write(1, b'R')\nos._exit(20)\n", REFUSED)

    def test_everything_else_is_unknown(self):
        rows = {
            "exit 0 without the marker": "os._exit(0)\n",
            "exit 20 without the marker": "os._exit(20)\n",
            "exit 10 after the marker": "os.write(1, b'R')\nos._exit(10)\n",
            "exit 19 after the marker": "os.write(1, b'R')\nos._exit(19)\n",
            "unlisted status 1": "os._exit(1)\n",
            "unlisted status 9": "os._exit(9)\n",
            "unlisted status 21 after the marker": "os.write(1, b'R')\nos._exit(21)\n",
            "unlisted status 30 after the marker": "os.write(1, b'R')\nos._exit(30)\n",
            "status 30 without the marker": "os._exit(30)\n",
            "421 before the marker (status 31, no output)": "os._exit(31)\n",
            "421 after the marker (status 31)": "os.write(1, b'R')\nos._exit(31)\n",
            "unlisted status 255": "os._exit(255)\n",
            "an uncaught exception": "raise RuntimeError('synthetic')\n",
            "killed by a signal before the marker": "os.kill(os.getpid(), 9)\n",
            "killed by a signal after the marker": "os.write(1, b'R')\nos.kill(os.getpid(), 9)\n",
            "ended by SIGTERM after the marker": "os.write(1, b'R')\nos.kill(os.getpid(), 15)\n",
            "another byte, exit 0": "os.write(1, b'X')\nos._exit(0)\n",
            "a lower-case marker": "os.write(1, b'r')\nos._exit(0)\n",
            "another byte, exit 10": "os.write(1, b'X')\nos._exit(10)\n",
            "two markers": "os.write(1, b'RR')\nos._exit(0)\n",
            "two markers in two writes": "os.write(1, b'R')\nos.write(1, b'R')\nos._exit(0)\n",
            "marker and a line feed": "os.write(1, b'R\\n')\nos._exit(0)\n",
            "marker and an unexpected byte, exit 20": "os.write(1, b'R')\nos.write(1, b'!')\nos._exit(20)\n",
            "marker then bytes up to the cap": "os.write(1, b'R' + b'x' * 15)\nos._exit(0)\n",
            "marker then bytes past the cap": "os.write(1, b'R' + b'x' * 400)\nos._exit(0)\n",
            "sixteen bytes and exit 10": "os.write(1, b' ' * 16)\nos._exit(10)\n",
        }
        for name, body in rows.items():
            with self.subTest(row=name):
                self.outcome(body, UNKNOWN)

    def test_the_decision_table_itself(self):
        decide = mail._outcome
        self.assertIs(decide(b"", True, 10), NOT_SENT)
        self.assertIs(decide(b"R", True, 0), ACCEPTED)
        self.assertIs(decide(b"R", True, 20), REFUSED)
        for output in (b"", b"R", b"RR", b"X", b"R" + b"x" * 15):
            for status in (*range(-15, 0), *range(0, 40), 127, 255):
                expected = {(b"R", 0): ACCEPTED, (b"R", 20): REFUSED}.get((output, status), UNKNOWN)
                if output == b"" and 10 <= status <= 19:
                    expected = NOT_SENT
                self.assertIs(decide(output, True, status), expected, (output, status))
                self.assertIs(decide(output, False, status), UNKNOWN, "never without end-of-file")

    def test_a_child_killed_between_the_marker_and_the_recipient_is_unknown(self):
        # C7: the marker is written first, so the parent holds ``R`` whatever happened next.
        side_channel = self.path("recipient-transmitted")
        self.outcome(
            "os.write(1, b'R')\n"
            f"open({side_channel!r}, 'w').write('recipient transmitted')\n"
            "os.kill(os.getpid(), 9)\n", UNKNOWN)
        self.assertEqual(Path(side_channel).read_text(), "recipient transmitted")


class ProcessCreationTest(SenderProcessCase):
    """C6 (process creation), C11, C12, C13."""

    def test_with_sigchld_ignored_no_process_is_created_and_nothing_is_accepted(self):
        # With SIGCHLD ignored the interpreter can report exit status 0 for a child it
        # could not wait for: marker plus exit 30 would then read as accepted.
        would_be_misread = READ_INPUT + "os.write(1, b'R')\nos._exit(30)\n"
        self.assertIs(self.send(would_be_misread), UNKNOWN, "the ordinary reading of that child")
        self.popen_calls.clear()
        self.processes.clear()
        previous = signal.signal(signal.SIGCHLD, signal.SIG_IGN)
        try:
            for body in (would_be_misread, ACCEPT, READ_INPUT + "os._exit(10)\n"):
                with self.assertLogs("learnpilot.registration", "WARNING") as logs:
                    outcome = self.send(body)
                self.assertIs(outcome, NOT_SENT)
                self.assertEqual(logs.output, ["WARNING:learnpilot.registration:sender_error error=sigchld"])
            self.assertEqual(signal.getsignal(signal.SIGCHLD), signal.SIG_IGN, "the disposition is read, never changed")
        finally:
            signal.signal(signal.SIGCHLD, previous)
        self.assertEqual(self.popen_calls, [], "no process was created")
        self.assertEqual(self.processes, [])
        self.assertEqual(signal.getsignal(signal.SIGCHLD), previous)
        self.assertIs(self.send(ACCEPT), ACCEPTED, "a later send works once the disposition is back")

    def test_a_sigchld_handler_or_the_default_action_does_not_stop_a_send(self):
        seen = []
        previous = signal.signal(signal.SIGCHLD, lambda number, frame: seen.append(number))
        try:
            self.assertIs(self.send(ACCEPT), ACCEPTED)
        finally:
            signal.signal(signal.SIGCHLD, previous)
        self.assertEqual(len(self.popen_calls), 1)

    def test_a_missing_interpreter_is_not_sent_and_no_process(self):
        with patch.object(sys, "executable", self.path("missing-python")), \
                self.assertLogs("learnpilot.registration", "WARNING") as logs:
            self.assertIs(self.send(ACCEPT), NOT_SENT)
        self.assertEqual(len(self.popen_calls), 1)
        self.assertEqual(self.processes, [])
        self.assertEqual(logs.output, ["WARNING:learnpilot.registration:sender_error error=FileNotFoundError"])
        self.assertIs(self.send(ACCEPT), ACCEPTED, "a later send works")

    def test_an_interpreter_that_is_not_an_absolute_path_starts_nothing(self):
        for value in ("python3", "", None):
            with patch.object(sys, "executable", value), self.assertLogs("learnpilot.registration", "WARNING"):
                self.assertIs(self.send(ACCEPT), NOT_SENT)
        with patch.object(mail, "SmtpMailer", mail.SmtpMailer), self.assertLogs("learnpilot.registration", "WARNING"):
            started = len(self.popen_calls)
            with patch.object(mail, "SENDER_SCRIPT", "mail_sender.py"):
                self.assertIs(mail.SmtpMailer(SETTINGS).send(MESSAGE), NOT_SENT)
            self.assertEqual(len(self.popen_calls), started)
        self.assertEqual(self.popen_calls, [])

    def test_creation_errors(self):
        def raising(error):
            def create(*args, **kwargs):
                self.popen_calls.append((args, kwargs))
                raise error
            return create

        for error in (OSError(errno.EAGAIN, "synthetic"), PermissionError(), ValueError("synthetic"),
                      subprocess.SubprocessError("synthetic")):
            with self.subTest(error=type(error).__name__):
                self.popen_class = raising(error)
                with self.assertLogs("learnpilot.registration", "WARNING"):
                    self.assertIs(self.send(ACCEPT), NOT_SENT)
        # Not known to mean "no process": nothing is established.
        self.popen_class = raising(MemoryError())
        with self.assertLogs("learnpilot.registration", "WARNING"):
            self.assertIs(self.send(ACCEPT), UNKNOWN)
        for error in (KeyboardInterrupt(), SystemExit(1)):
            self.popen_class = raising(error)
            with self.assertRaises(type(error)):
                self.send(ACCEPT)
        self.assertEqual(len(self.popen_calls), 7, "never a second attempt")
        self.assertEqual(self.processes, [])

    def test_the_command_is_the_interpreter_isolated_without_site_and_the_absolute_script(self):
        # C12, C13: no setting, address or secret in the arguments.
        report = self.path("report.json")
        outcome = self.send(READ_INPUT + (
            "import json, sys\n"
            f"json.dump({{'argv': sys.orig_argv, 'isolated': sys.flags.isolated, 'no_site': sys.flags.no_site,\n"
            f"           'site': 'site' in sys.modules, 'cwd': os.getcwd()}}, open({report!r}, 'w'))\n"
            "os._exit(10)\n"))
        self.assertIs(outcome, NOT_SENT)
        (args, kwargs), = self.popen_calls
        script = self.path("stand_in_1.py")
        self.assertEqual(args, ([sys.executable, "-I", "-S", script],))
        self.assertEqual(kwargs["stdin"], subprocess.PIPE)
        self.assertEqual(kwargs["stdout"], subprocess.PIPE)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        self.assertIs(kwargs["close_fds"], True)
        self.assertIs(kwargs["shell"], False)
        self.assertNotIn("preexec_fn", kwargs)
        self.assertNotIn("pass_fds", kwargs)
        seen = json.loads(Path(report).read_text())
        self.assertEqual(seen["argv"][1:], ["-I", "-S", script])
        self.assertEqual((seen["isolated"], seen["no_site"], seen["site"]), (1, 1, False))
        # The real command: the script beside the mail module.
        self.popen_calls.clear()
        with patch.object(mail, "_exchange", lambda proc, payload: (_ for _ in ()).throw(RuntimeError("stop"))), \
                self.assertLogs("learnpilot.registration", "WARNING"):
            self.assertIs(mail.SmtpMailer(SETTINGS).send(MESSAGE), UNKNOWN)
        self.assertEqual(self.popen_calls[0][0], ([sys.executable, "-I", "-S", mail.SENDER_SCRIPT],))
        self.assert_nothing_left()

    def test_the_environment_is_built_not_inherited(self):
        # C13.
        report = self.path("environment.json")
        secrets = {
            "DZI_SECRET_KEY": "synthetic-secret-key-value-0123456789", "DZI_TOTP_KEY": "synthetic-totp-key-value",
            "DZI_GOOGLE_CLIENT_ID": "synthetic.apps.googleusercontent.com", "DZI_GOOGLE_CLIENT_SECRET": "synthetic-google-secret",
            "DZI_SMTP_PASSWORD": SMTP_PASSWORD, "DZI_SMTP_USERNAME": "synthetic-user", "DZI_SMTP_HOST": "relay.example.org",
            "DZI_MAIL_FROM": "no-reply@example.org", "DZI_MAIL_BACKEND": "smtp", "DZI_DB": "/synthetic/state/questions.db",
            "DZI_UPLOAD_ROOT": "/synthetic/state/uploads", "DZI_VAULT": "/synthetic/state/vault",
            "DZI_REFERENCE_ROOT": "/synthetic/state/reference", "DZI_MAIL_SPOOL_DIR": "/synthetic/spool",
            "PYTHONPATH": "/synthetic/path", "PYTHONSTARTUP": "/synthetic/startup.py", "HOME": "/synthetic/home",
            "PATH": "/synthetic/bin", "TMPDIR": self.directory, "NOTIFY_SOCKET": "/synthetic/notify",
        }
        allowed = {"LANG": "bg_BG.UTF-8", "LC_ALL": "bg_BG.UTF-8", "SSL_CERT_FILE": "/synthetic/ca.pem",
                   "SSL_CERT_DIR": "/synthetic/certs"}
        body = READ_INPUT + f"import json\njson.dump(dict(os.environ), open({report!r}, 'w'))\nos._exit(10)\n"
        with patch.dict(os.environ, {**secrets, **allowed}, clear=True):
            self.assertIs(self.send(body), NOT_SENT)
        self.assertEqual(self.popen_calls[0][1]["env"], allowed)
        seen = json.loads(Path(report).read_text())
        # The platform may add these two to a starting interpreter by itself.
        self.assertLessEqual(set(seen), set(mail.CHILD_ENVIRONMENT_NAMES) | {"__CF_USER_TEXT_ENCODING", "LC_CTYPE"})
        for name, value in secrets.items():
            self.assertNotIn(name, seen)
            self.assertNotIn(value, json.dumps(seen))
        for name, value in allowed.items():
            self.assertEqual(seen[name], value)
        self.assertEqual(mail.CHILD_ENVIRONMENT_NAMES, ("LANG", "LC_ALL", "LC_CTYPE", "SSL_CERT_FILE", "SSL_CERT_DIR"))

        with patch.dict(os.environ, {**secrets, "LANG": ""}, clear=True):
            self.assertIs(self.send(body), NOT_SENT)
        self.assertEqual(self.popen_calls[1][1]["env"], {})
        self.assertLessEqual(set(json.loads(Path(report).read_text())), {"__CF_USER_TEXT_ENCODING", "LC_CTYPE"})

    def test_the_child_holds_only_its_three_standard_descriptors(self):
        # C13: the parent holds an open SQLite connection and a listening socket.
        report = self.path("descriptors.json")
        body = (
            "import json, os, stat\n"
            "found = []\n"
            "for descriptor in range(256):\n"
            "    try:\n        os.fstat(descriptor)\n    except OSError:\n        continue\n"
            "    found.append(descriptor)\n"
            "null = os.stat(os.devnull)\nerr = os.fstat(2)\n"
            f"json.dump({{'open': found, 'stderr_is_null': stat.S_ISCHR(err.st_mode) and err.st_rdev == null.st_rdev,\n"
            f"           'stdin_is_pipe': stat.S_ISFIFO(os.fstat(0).st_mode), 'stdout_is_pipe': stat.S_ISFIFO(os.fstat(1).st_mode)}},\n"
            f"          open({report!r}, 'w'))\n"
            "os._exit(10)\n")
        connection = sqlite3.connect(self.path("held.db"))
        self.addCleanup(connection.close)
        connection.execute("CREATE TABLE held (value TEXT)")
        connection.execute("BEGIN IMMEDIATE")
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.set_inheritable(True)
        held = os.open(self.path("held.db"), os.O_RDONLY)
        self.addCleanup(os.close, held)
        os.set_inheritable(held, True)

        self.assertIs(self.send(body), NOT_SENT)
        seen = json.loads(Path(report).read_text())
        self.assertEqual(seen, {"open": [0, 1, 2], "stderr_is_null": True, "stdin_is_pipe": True, "stdout_is_pipe": True})

        # The control: without close_fds the same stand-in does see them, so the check can fail.
        control = REAL_POPEN([sys.executable, "-I", "-S", self.path("stand_in_1.py")], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=False)
        self.assertEqual(control.wait(timeout=30), 10)
        leaked = json.loads(Path(report).read_text())["open"]
        self.assertIn(listener.fileno(), leaked)
        self.assertIn(held, leaked)

    def test_a_traceback_in_the_child_reaches_nothing(self):
        # C15: standard error is the null device, and the parent logs no child output.
        captured = tempfile.TemporaryFile()
        self.addCleanup(captured.close)
        records = []
        handler = logging.Handler()
        handler.emit = records.append
        root = logging.getLogger()
        root.addHandler(handler)
        self.addCleanup(root.removeHandler, handler)
        saved = os.dup(2)
        try:
            os.dup2(captured.fileno(), 2)
            outcome = self.send(f"raise RuntimeError({RECIPIENT + ' ' + BODY_TOKEN!r})\n")
        finally:
            os.dup2(saved, 2)
            os.close(saved)
        self.assertIs(outcome, UNKNOWN)
        captured.seek(0)
        self.assertEqual(captured.read(), b"")
        self.assertEqual(records, [])


class ParentLogsNothingSensitiveTest(SenderProcessCase):
    """C15, for the parent: an event name and a class name, never a value."""

    DEADLINE = 0.3

    def test_no_log_record_at_any_level_holds_the_recipient_the_body_or_a_credential(self):
        records = []
        handler = logging.Handler()
        handler.emit = records.append
        root, registration = logging.getLogger(), logging.getLogger("learnpilot.registration")
        levels = (root.level, registration.level)
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        registration.setLevel(logging.DEBUG)
        try:
            self.send(ACCEPT)
            self.send(READ_INPUT + "os.write(1, b'R')\nos._exit(20)\n")
            self.send(READ_INPUT + "os._exit(11)\n")
            self.send(BLOCK)
            self.send("import os\nos.write(1, b'x' * 100)\n" + BLOCK)
            with patch.object(sys, "executable", self.path("missing-python")):
                self.send(ACCEPT)
            with patch.object(mail, "_exchange", lambda proc, payload: (_ for _ in ()).throw(
                    RuntimeError(f"{RECIPIENT} {BODY_TOKEN} {SMTP_PASSWORD}"))):
                self.send(BLOCK)
        finally:
            root.removeHandler(handler)
            root.setLevel(levels[0])
            registration.setLevel(levels[1])
        lines = [record.getMessage() for record in records]
        self.assertEqual(lines, ["sender_error error=FileNotFoundError", "sender_error error=RuntimeError"])
        for record in records:
            text = logging.Formatter("%(name)s %(levelname)s %(message)s").format(record)
            for secret in (RECIPIENT, BODY_TOKEN, SMTP_PASSWORD, "synthetic-user", "relay.example.org", "no-reply@example.org"):
                self.assertNotIn(secret, text)
            self.assertIsNone(record.exc_info, "no traceback is attached")


class InputAndOutputCapsTest(SenderProcessCase):
    """C14: exact byte caps on both pipes."""

    def test_exactly_16_kib_of_input_is_written_and_one_byte_more_starts_nothing(self):
        count = self.path("count")
        body = (
            "import os\ntotal = 0\n"
            "while True:\n    chunk = os.read(0, 65536)\n    if not chunk:\n        break\n    total += len(chunk)\n"
            f"open({count!r}, 'w').write(str(total))\nos._exit(10)\n")
        with patch.object(mail, "_payload", lambda settings, message: b"x" * (16 * 1024)):
            self.assertIs(self.send(body), NOT_SENT)
        self.assertEqual(Path(count).read_text(), str(16 * 1024))
        self.assertEqual(len(self.popen_calls), 1)
        with patch.object(mail, "_payload", lambda settings, message: b"x" * (16 * 1024 + 1)), \
                self.assertLogs("learnpilot.registration", "WARNING") as logs:
            self.assertIs(self.send(body), NOT_SENT)
        self.assertEqual(len(self.popen_calls), 1, "no process for an input over the cap")
        self.assertEqual(logs.output, ["WARNING:learnpilot.registration:sender_error error=input"])
        self.assertEqual(mail.MAX_INPUT_BYTES, 16 * 1024)

    def test_the_input_is_the_settings_and_the_message_and_arrives_whole(self):
        received = self.path("received.json")
        body = (
            "import os\ndata = b''\n"
            "while True:\n    chunk = os.read(0, 65536)\n    if not chunk:\n        break\n    data += chunk\n"
            f"open({received!r}, 'wb').write(data)\nos.write(1, b'R')\nos._exit(0)\n")
        self.assertIs(self.send(body), ACCEPTED)
        self.assertEqual(json.loads(Path(received).read_text(encoding="utf-8")), {
            "host": "relay.example.org", "port": 587, "security": "starttls", "username": "synthetic-user",
            "password": SMTP_PASSWORD, "sender_address": "no-reply@example.org", "sender_name": "LearnPilot",
            "to": RECIPIENT, "subject": "Synthetic subject", "text": BODY_TOKEN})

    def test_the_output_read_stops_at_16_bytes_and_is_never_drained(self):
        for extra, expected_total in ((15, 16), (16, 16), (5000, 16), (0, 1), (1, 2)):
            with self.subTest(extra=extra):
                self.processes.clear()
                reads = self.record_reads()
                outcome = self.send(READ_INPUT + f"os.write(1, b'R' + b'x' * {extra})\nos._exit(0)\n")
                self.assertIs(outcome, ACCEPTED if extra == 0 else UNKNOWN)
                self.assertEqual(sum(returned for _requested, returned in reads), expected_total)
                already = 0
                for requested, returned in reads:
                    self.assertEqual(requested, 16 - already, "never asks for more than is missing to 16")
                    already += returned
                if expected_total == 16:
                    self.assertNotEqual(reads[-1][1], 0, "no read follows the one that reached the cap")
                self.assert_nothing_left()
        self.assertEqual(mail.MAX_OUTPUT_BYTES, 16)


class ParentCleanupTest(SenderProcessCase):
    """C8: the child is gone and the pipes are closed on every parent exit path."""

    MAPPED = (BrokenPipeError(), MemoryError(), OSError(errno.EIO, "synthetic"), RuntimeError("synthetic"))
    PROPAGATED = (KeyboardInterrupt(), SystemExit(1))

    def run_with(self, injection, body, error):
        self.processes.clear()
        self.popen_calls.clear()
        with injection(error):
            if isinstance(error, Exception):
                with self.assertLogs("learnpilot.registration", "WARNING") as logs:
                    self.assertIs(self.send(body), UNKNOWN)
                self.assertEqual(logs.output, [f"WARNING:learnpilot.registration:sender_error error={type(error).__name__}"])
            else:
                with self.assertRaises(type(error)):
                    self.send(body)
        self.assert_nothing_left(1)
        self.assertLess(self.elapsed, self.REAP + MARGIN, "ended at once, not at the deadline")
        self.assertIs(self.send(ACCEPT), ACCEPTED, "a later send works")
        self.assert_nothing_left(2)

    @contextlib.contextmanager
    def failing_write(self, error):
        real_write, case = os.write, self

        def write(descriptor, data):
            if case.processes and descriptor == case.processes[-1].stdin_fd:
                raise error
            return real_write(descriptor, data)

        with patch.object(os, "write", write):
            yield

    @contextlib.contextmanager
    def failing_read(self, error):
        real_read, case = os.read, self

        def read(descriptor, size):
            if case.processes and descriptor == case.processes[-1].stdout_fd:
                raise error
            return real_read(descriptor, size)

        with patch.object(os, "read", read):
            yield

    @contextlib.contextmanager
    def failing_select(self, error):
        class Selector(selectors.DefaultSelector):
            def select(self, timeout=None):
                raise error

        with patch.object(selectors, "DefaultSelector", Selector):
            yield

    @contextlib.contextmanager
    def failing_register(self, error):
        class Selector(selectors.DefaultSelector):
            def register(self, fileobj, events, data=None):
                raise error

        with patch.object(selectors, "DefaultSelector", Selector):
            yield

    def test_exception_while_writing_the_input(self):
        for error in (*self.MAPPED, *self.PROPAGATED):
            with self.subTest(error=type(error).__name__):
                self.run_with(self.failing_write, BLOCK, error)

    def test_exception_while_reading_the_output(self):
        for error in (*self.MAPPED, *self.PROPAGATED):
            with self.subTest(error=type(error).__name__):
                self.run_with(self.failing_read, "import os\nos.write(1, b'R')\n" + BLOCK, error)

    def test_exception_from_the_selector(self):
        for injection in (self.failing_select, self.failing_register):
            for error in (*self.MAPPED, *self.PROPAGATED):
                with self.subTest(point=injection.__name__, error=type(error).__name__):
                    self.run_with(injection, BLOCK, error)

    def test_exception_after_a_clean_exchange_still_leaves_nothing(self):
        @contextlib.contextmanager
        def failing_decision(error):
            def decide(*args):
                raise error
            with patch.object(mail, "_outcome", decide):
                yield

        for error in (RuntimeError("synthetic"), KeyboardInterrupt()):
            self.run_with(failing_decision, ACCEPT, error)

    def test_a_broken_input_pipe_is_unknown_even_when_the_child_exited_10_to_19(self):
        # The child is gone before the megabyte can be written: a real EPIPE.
        with patch.object(mail, "_payload", lambda settings, message: b"x" * (1 << 20)), \
                patch.object(mail, "MAX_INPUT_BYTES", 1 << 21), \
                self.assertLogs("learnpilot.registration", "WARNING") as logs:
            outcome = self.send("import os\nos._exit(12)\n")
        self.assertIs(outcome, UNKNOWN)
        self.assertEqual(logs.output, ["WARNING:learnpilot.registration:sender_error error=BrokenPipeError"])
        self.assertLess(self.elapsed, self.DEADLINE)
        self.assert_nothing_left(1)
        self.assertEqual(self.processes[0].returncode, 12)

    def test_an_interrupted_reaping_still_closes_the_pipes_and_remembers_the_child(self):
        case = self

        class Interrupted(self.Recording):
            def wait(self, timeout=None):
                if timeout == case.REAP:
                    raise KeyboardInterrupt()
                return super().wait(timeout)

        self.popen_class = Interrupted
        with patch.object(mail, "COMMUNICATION_DEADLINE", 0.3), self.assertLogs("learnpilot.registration", "WARNING") as logs, \
                self.assertRaises(KeyboardInterrupt):
            self.send(BLOCK)
        proc = self.processes[0]
        self.assertTrue(proc.stdin.closed and proc.stdout.closed)
        self.assertEqual(logs.output, ["WARNING:learnpilot.registration:sender_unreaped"])
        self.assertTrue(wait_until(lambda: not mail.sender_busy(), 5), "the killed child is reaped by the next poll")
        self.assertEqual(proc.returncode, -signal.SIGKILL)


class UnreapedSenderTest(SenderProcessCase):
    """C17 (the mail half): the state later callers read before admission."""

    DEADLINE = 0.3
    REAP = 0.3

    def test_an_unreaped_sender_is_reported_and_no_further_process_is_created(self):
        case = self

        class Unreapable(self.Recording):
            stuck = True

            def wait(self, timeout=None):
                if type(self).stuck:
                    case.wait_timeouts.append(timeout)
                    raise subprocess.TimeoutExpired(self.args, timeout)
                return super().wait(timeout)

            def poll(self):
                return None if type(self).stuck else super().poll()

        self.wait_timeouts = []
        self.popen_class = Unreapable
        self.assertFalse(mail.sender_busy())
        with self.assertLogs("learnpilot.registration", "WARNING") as logs:
            self.assertIs(self.send(BLOCK), UNKNOWN)
        self.assertEqual(logs.output, ["WARNING:learnpilot.registration:sender_unreaped"])
        self.assertEqual(self.wait_timeouts, [self.REAP], "one bounded wait after the kill")
        self.assertLess(self.elapsed, self.DEADLINE + self.REAP + MARGIN)
        stuck = self.processes[0]
        self.assertTrue(stuck.stdin.closed and stuck.stdout.closed, "the pipes are closed all the same")

        self.assertTrue(mail.sender_busy())
        self.assertTrue(mail.sender_busy(), "reading the state does not clear it")
        with self.assertNoLogs("learnpilot.registration", "WARNING"):
            for _ in range(3):
                self.assertIs(self.send(ACCEPT), NOT_SENT)
        self.assertEqual(len(self.popen_calls), 1, "send creates no process while a sender is unreaped")
        self.assertLess(self.elapsed, 0.2)

        Unreapable.stuck = False  # the child, killed earlier, can now be reaped
        # SIGKILL was sent; the kernel ends the child soon, not at once, so poll may
        # still report it running for a moment. The wait is bounded.
        self.assertTrue(wait_until(lambda: not mail.sender_busy(), 5))
        self.assertEqual(stuck.returncode, -signal.SIGKILL)
        self.assertFalse(pid_exists(stuck.pid))
        self.assertIs(self.send(ACCEPT), ACCEPTED)
        self.assertEqual(len(self.popen_calls), 2)

    def test_busy_is_false_when_nothing_was_ever_sent_and_after_ordinary_sends(self):
        self.assertFalse(mail.sender_busy())
        self.assertIs(self.send(ACCEPT), ACCEPTED)
        self.assertIs(self.send(BLOCK), UNKNOWN)
        self.assertFalse(mail.sender_busy())

    def test_the_state_belongs_to_the_process_not_to_a_mailer(self):
        self.assertIs(mail.sender_busy.__module__, mail.__name__)
        marker = REAL_POPEN([sys.executable, "-I", "-S", "-c", "import time; time.sleep(30)"])
        self.addCleanup(marker.wait)
        self.addCleanup(marker.kill)
        mail._unreaped.append(marker)
        self.assertTrue(mail.sender_busy())
        self.assertIs(mail.SmtpMailer(SETTINGS).send(MESSAGE), NOT_SENT)
        self.assertEqual(self.popen_calls, [])
        marker.kill()
        self.assertTrue(wait_until(lambda: not mail.sender_busy(), 5))


@contextlib.contextmanager
def inherited_sigalrm(state):
    """Start children with SIGALRM ignored or blocked, as an unlucky parent would."""
    if state == "ignored":
        previous = signal.signal(signal.SIGALRM, signal.SIG_IGN)
        try:
            yield
        finally:
            signal.signal(signal.SIGALRM, previous)
    elif state == "blocked":
        previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
        try:
            yield
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous)
    else:
        yield


ARMED = (
    "import signal\n"
    "signal.signal(signal.SIGALRM, signal.SIG_DFL)\n"
    "signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGALRM})\n"
    "signal.setitimer(signal.ITIMER_REAL, %r)\n")


class AlarmTest(unittest.TestCase):
    """C18, C19: what ends a sender whose parent does not."""

    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="learnpilot-mail-test-")
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        self.children = []
        self.addCleanup(self.end_children)

    def end_children(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=10)
            for pipe in (child.stdin, child.stdout):
                if pipe is not None:
                    pipe.close()

    def start(self, command, state=None, **kwargs):
        with inherited_sigalrm(state):
            child = REAL_POPEN(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **kwargs)
        self.children.append(child)
        return child

    def write(self, name, source):
        path = os.path.join(self.directory, name)
        Path(path).write_text(source, encoding="utf-8")
        return path

    def test_the_real_script_ends_by_itself_whatever_signal_state_it_inherits(self):
        # Nobody kills it and its peer never answers: the input pipe stays open and
        # empty. All three run at once, so this costs one alarm, about nine seconds.
        started = time.monotonic()
        children = {state: self.start([sys.executable, "-I", "-S", mail.SENDER_SCRIPT], state)
                    for state in ("default", "ignored", "blocked")}
        for state, child in children.items():
            with self.subTest(state=state):
                self.assertEqual(child.wait(timeout=20), -signal.SIGALRM)
                self.assertEqual(child.stdout.read(), b"")
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 8.5)
        self.assertLess(elapsed, 13.0)

    def test_without_the_reset_and_the_unblock_an_inherited_state_defeats_the_alarm(self):
        # Why the first three statements exist, with the alarm shortened.
        plain = self.write("plain.py", "import signal\nsignal.setitimer(signal.ITIMER_REAL, 0.3)\nimport time\ntime.sleep(1.5)\n")
        armed = self.write("armed.py", ARMED % 0.3 + "import time\ntime.sleep(30)\n")
        for state in ("ignored", "blocked"):
            with self.subTest(state=state):
                unprotected = self.start([sys.executable, "-I", "-S", plain], state)
                protected = self.start([sys.executable, "-I", "-S", armed], state)
                started = time.monotonic()
                self.assertEqual(protected.wait(timeout=20), -signal.SIGALRM)
                self.assertLess(time.monotonic() - started, 5.0)
                self.assertEqual(unprotected.wait(timeout=20), 0, "it outlived its alarm and ended only when its work did")
        self.assertEqual(self.start([sys.executable, "-I", "-S", plain]).wait(timeout=20), -signal.SIGALRM)

    def test_the_alarm_counts_from_the_statements_not_from_process_creation(self):
        # The documented gap: whatever runs before them, interpreter start-up above all, is not covered.
        slow = self.write("slow.py", "import time\ntime.sleep(0.6)\n" + ARMED % 0.4 + "time.sleep(30)\n")
        started = time.monotonic()
        child = self.start([sys.executable, "-I", "-S", slow])
        self.assertEqual(child.wait(timeout=20), -signal.SIGALRM)
        self.assertGreaterEqual(time.monotonic() - started, 1.0)

    def test_a_sender_orphaned_by_a_killed_worker_ends_at_its_alarm_while_another_runs(self):
        # C18. Asserts what the design states and no more: the two may overlap; the
        # orphan ends by its alarm; the second send completes normally.
        pid_file = os.path.join(self.directory, "orphan.pid")
        orphan_script = self.write("orphan.py", ARMED % 2.5 + (
            "import os, time\n"
            f"open({pid_file + '.tmp'!r}, 'w').write(str(os.getpid()))\n"
            f"os.rename({pid_file + '.tmp'!r}, {pid_file!r})\n"
            "time.sleep(60)\n"))
        accept_script = self.write("accept.py", SAFETY + ACCEPT)
        driver = (
            "import sys\n"
            "from web.services import mail\n"
            "mail.SENDER_SCRIPT = sys.argv[1]\n"
            "settings = mail.MailSettings(backend='smtp', sender_address='no-reply@example.org', sender_name=None,\n"
            "                             smtp_host='relay.example.org', smtp_port=587, smtp_security='starttls')\n"
            "mail.SmtpMailer(settings).send(mail.OutboundEmail(to='pupil@example.org', subject='s', text='t'))\n")
        worker = self.start([sys.executable, "-B", "-c", driver, orphan_script], cwd=ROOT,
                            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertTrue(wait_until(lambda: os.path.exists(pid_file), 20), "the first sender started")
        armed_at = time.monotonic()
        orphan = int(Path(pid_file).read_text())
        worker.kill()  # outright: no cleanup runs in the worker
        self.assertEqual(worker.wait(timeout=10), -signal.SIGKILL)
        self.assertTrue(pid_exists(orphan), "nothing ended the orphan when its worker died")

        with patch.object(mail, "SENDER_SCRIPT", accept_script):
            self.assertIs(mail.SmtpMailer(SETTINGS).send(MESSAGE), ACCEPTED)

        self.assertTrue(wait_until(lambda: not pid_exists(orphan), 2.5 + 5.0), "the orphan ended by its own alarm")
        self.assertGreaterEqual(time.monotonic() - armed_at, 1.5, "and not before it")


class RealScriptProcessTest(unittest.TestCase):
    """C12, C14: the real script as a process, with input that ends it before any network call."""

    def run_script(self, data, command=None, cwd=None):
        child = REAL_POPEN(command or [sys.executable, "-I", "-S", mail.SENDER_SCRIPT], stdin=subprocess.PIPE,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd, env={})
        try:
            output, errors = child.communicate(data, timeout=30)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
        return child.returncode, output, errors

    def test_it_starts_from_another_working_directory(self):
        with tempfile.TemporaryDirectory() as elsewhere:
            self.assertEqual(self.run_script(b"", cwd=elsewhere), (10, b"", b""))

    def test_bad_input_ends_with_10_and_writes_nothing(self):
        for data in (b"", b"{", b"x" * (16 * 1024), b"x" * (16 * 1024 + 1), b" " * (1 << 20), b'{"host": "relay.example.org"}',
                     json.dumps({"host": "relay.example.org", "port": 25, "security": "none", "username": None,
                                 "password": None, "sender_address": "no-reply@example.org", "sender_name": None,
                                 "to": RECIPIENT, "subject": "s", "text": "t"}).encode()):
            with self.subTest(length=len(data)):
                self.assertEqual(self.run_script(data), (10, b"", b""))

    def test_by_module_name_it_cannot_start_in_isolated_mode(self):
        # Why the script is run by path: the checkout is not on an isolated interpreter's path.
        status, output, errors = self.run_script(b"", [sys.executable, "-I", "-S", "-m", "web.services.mail_sender"], cwd=ROOT)
        self.assertEqual((status, output), (1, b""))
        self.assertIn(b"No module named", errors)

    def test_site_and_the_application_are_not_loaded(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = os.path.join(directory, "harness.py")
            Path(harness).write_text(textwrap.dedent(f"""
                import os, runpy, sys
                real_exit = os._exit
                def report(status):
                    names = sorted(name for name in sys.modules
                                   if name.split(".")[0] in ("site", "sqlite3", "flask", "werkzeug", "web", "src", "subprocess"))
                    os.write(1, repr((status, names, sys.flags.isolated, sys.flags.no_site)).encode())
                    real_exit(0)
                os._exit = report
                runpy.run_path({mail.SENDER_SCRIPT!r}, run_name="__main__")
            """), encoding="utf-8")
            status, output, errors = self.run_script(b"", [sys.executable, "-I", "-S", harness])
        self.assertEqual((status, output.decode(), errors), (0, repr((10, [], 1, 1)), b""))

    def test_without_site_only_the_standard_library_is_importable(self):
        status, output, _errors = self.run_script(
            b"", [sys.executable, "-I", "-S", "-c", "import sys\nprint(any('site-packages' in p for p in sys.path))"])
        self.assertEqual((status, output.strip()), (0, b"False"))


class NoBlockingReadsTest(unittest.TestCase):
    """C21, static."""

    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse(Path(mail.__file__).read_text(encoding="utf-8"))

    def names(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Attribute):
                yield node.attr
            elif isinstance(node, ast.Name):
                yield node.id

    def test_communicate_is_not_used(self):
        names = set(self.names())
        self.assertFalse(names & {"communicate", "check_output", "check_call", "getoutput", "run", "call", "readline",
                                  "readlines", "readall", "readinto", "system", "popen"})

    def test_the_only_read_is_the_capped_one(self):
        calls = [ast.unparse(node) for node in ast.walk(self.tree)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("read", "recv")]
        self.assertEqual(calls, ["os.read(stdout_fd, MAX_OUTPUT_BYTES - len(output))"])

    def test_one_process_creation_and_no_shell(self):
        creations = [node for node in ast.walk(self.tree)
                     if isinstance(node, ast.Call) and ast.unparse(node.func) == "subprocess.Popen"]
        self.assertEqual(len(creations), 1)
        keywords = {keyword.arg: ast.unparse(keyword.value) for keyword in creations[0].keywords}
        self.assertEqual(keywords, {
            "stdin": "subprocess.PIPE", "stdout": "subprocess.PIPE", "stderr": "subprocess.DEVNULL",
            "env": "_child_environment(os.environ)", "close_fds": "True", "shell": "False", "bufsize": "0"})
        self.assertEqual(ast.unparse(creations[0].args[0]), "[sys.executable, '-I', '-S', SENDER_SCRIPT]")

    def test_everything_after_creation_is_inside_one_try_finally(self):
        send = next(node for node in ast.walk(self.tree) if isinstance(node, ast.FunctionDef) and node.name == "send"
                    and any(ast.unparse(call.func) == "subprocess.Popen" for call in ast.walk(node) if isinstance(call, ast.Call)))
        outer = next(node for node in send.body if isinstance(node, ast.Try) and node.finalbody)
        self.assertTrue(outer.finalbody)
        self.assertIn("_end(proc)", ast.unparse(outer.finalbody))
        self.assertIn("subprocess.Popen", ast.unparse(outer.body))
        self.assertIn("_exchange(proc, payload)", ast.unparse(outer.body))
        self.assertIsInstance(send.body[-1], ast.Return, "nothing but the return follows it")
        self.assertIs(send.body[-2], outer)


if __name__ == "__main__":
    unittest.main()
