"""Mail interface, settings, backends and the SMTP exchange of the sender script
(EMAIL_REGISTRATION PR 2; test plan C1-C4, C9, C10, C12, C14, C15).

No socket is opened, no message is sent and no real address, host or credential
appears: the sender script is executed in this process, with its three signal
statements intercepted, against the real ``smtplib`` whose socket is a scripted fake.
What crosses the process boundary is tested in ``tests.test_mail_sender_deadline``.
"""

import ast
import email
import email.policy
import io
import json
import logging
import os
import signal
import smtplib
import socket
import ssl
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

from tests.support import ROOT
from web.services import mail

SENDER = "no-reply@example.org"
RECIPIENT = "pupil@example.org"
SMTP_PASSWORD = "synthetic-smtp-password-7Q"
CODE_SUBJECT = "Код за потвърждение в LearnPilot"
CODE_TEXT = "Кодът ти за потвърждение е: 000000\nВалиден е 10 минути.\n.начало с точка"
NOTICE_SUBJECT = "Опит за регистрация в LearnPilot"
NOTICE_TEXT = "Някой се опита да създаде профил в LearnPilot с този имейл, но такъв вече съществува. " * 3

SMTP_ENV = {
    mail.BACKEND_ENV: "smtp", mail.FROM_ENV: f"LearnPilot <{SENDER}>",
    mail.SMTP_HOST_ENV: "relay.example.org", mail.SMTP_PORT_ENV: "587", mail.SMTP_SECURITY_ENV: "starttls",
    mail.SMTP_USERNAME_ENV: "synthetic-user", mail.SMTP_PASSWORD_ENV: SMTP_PASSWORD,
}


def load_sender_script():
    """The script's module object and the signal calls its first statements made.

    The script must never be imported: it arms an alarm. Here the three calls are
    replaced for the time its body runs, and no bytecode file is written.
    """
    source = Path(mail.SENDER_SCRIPT).read_text(encoding="utf-8")
    module = types.ModuleType("_learnpilot_mail_sender_under_test")
    module.__file__ = mail.SENDER_SCRIPT
    recorder = Mock()
    with patch.object(signal, "signal", recorder.signal), \
            patch.object(signal, "pthread_sigmask", recorder.pthread_sigmask), \
            patch.object(signal, "alarm", recorder.alarm):
        exec(compile(source, mail.SENDER_SCRIPT, "exec"), module.__dict__)
    return module, recorder.mock_calls


SCRIPT, SIGNAL_CALLS = load_sender_script()


def document(**changes):
    values = {
        "host": "relay.example.org", "port": 587, "security": "starttls",
        "username": "synthetic-user", "password": SMTP_PASSWORD,
        "sender_address": SENDER, "sender_name": "LearnPilot",
        "to": RECIPIENT, "subject": CODE_SUBJECT, "text": CODE_TEXT,
    }
    values.update(changes)
    return values


def encoded(**changes):
    return json.dumps(document(**changes), ensure_ascii=False).encode("utf-8")


class FakeServer:
    """A scripted SMTP peer under the real ``smtplib``. Records every event in order."""

    EHLO = b"250-relay.example.org\r\n250-SIZE 35882577\r\n250-STARTTLS\r\n250 AUTH PLAIN LOGIN\r\n"

    def __init__(self, replies=None, raises=None, connect_error=None, tls_error=None):
        self.replies = {
            "GREETING": b"220 relay.example.org ESMTP\r\n", "EHLO": self.EHLO, "STARTTLS": b"220 go ahead\r\n",
            "AUTH": b"235 ok\r\n", "MAIL": b"250 ok\r\n", "RCPT": b"250 ok\r\n", "DATA": b"354 go ahead\r\n",
            "MESSAGE": b"250 queued\r\n", "QUIT": b"221 bye\r\n",
        }
        self.replies.update(replies or {})
        self.raises = raises or {}
        self.connect_error = connect_error
        self.tls_error = tls_error
        self.events = []
        self._buffer = b""
        self._in_data = False

    # the socket the patched ``_get_socket`` returns
    def connect(self, host, port, timeout):
        self.events.append(("connect", host, port, timeout))
        if self.connect_error is not None:
            raise self.connect_error
        self._buffer += self.replies["GREETING"] or b""
        return self

    def sendall(self, data):
        verb = "MESSAGE" if self._in_data else bytes(data).split()[0].upper().decode("ascii")
        self._in_data = False
        self.events.append(("sent", verb, bytes(data)))
        if verb in self.raises:
            raise self.raises[verb]
        reply = self.replies[verb]
        if reply is None:
            return  # the peer goes silent: the next read is an end-of-file
        self._buffer += reply
        self._in_data = verb == "DATA" and reply.startswith(b"354")

    def makefile(self, mode):
        return self

    def readline(self, limit=-1):
        line, separator, rest = self._buffer.partition(b"\n")
        self._buffer = rest
        return line + separator

    def close(self):
        self.events.append(("close",))

    def settimeout(self, value):
        pass

    # helpers for the assertions
    def sent(self, verb):
        return [data for kind, *rest in self.events if kind == "sent" for v, data in [rest] if v == verb]

    def verbs(self):
        return [event[1] for event in self.events if event[0] == "sent"]

    def index(self, kind, verb=None):
        for position, event in enumerate(self.events):
            if event[0] == kind and (verb is None or event[1] == verb):
                return position
        raise AssertionError(f"no {kind} {verb} event in {[e[:2] for e in self.events]}")


def envelope(command, name):
    """The rest of an envelope command after its name, byte for byte.

    Only the command name is compared without regard to case (``smtplib`` has written
    it in different cases across Python versions; SMTP command names are
    case-insensitive). Everything after it, the address and any option, is exact.
    """
    name = name.encode("ascii")
    assert command[:len(name)].upper() == name.upper(), command
    return command[len(name):]


def run_script(data, server=None, marker_result=None):
    """``run(data)`` of the sender script against a fake peer: (status, server)."""
    server = server or FakeServer()

    def wrap_socket(context, sock, server_hostname=None, **_kwargs):
        server.events.append(("tls", context.verify_mode, context.check_hostname,
                              context.minimum_version, server_hostname))
        if server.tls_error is not None:
            raise server.tls_error
        return sock

    def write(descriptor, payload):
        server.events.append(("marker", descriptor, bytes(payload)))
        if isinstance(marker_result, BaseException):
            raise marker_result
        return len(payload) if marker_result is None else marker_result

    sendmail = Mock(side_effect=AssertionError("sendmail must never be called"))
    with patch.object(smtplib.SMTP, "_get_socket", lambda self, host, port, timeout: server.connect(host, port, timeout)), \
            patch.object(ssl.SSLContext, "wrap_socket", wrap_socket), \
            patch.object(socket, "getfqdn", lambda *args: "client.example.org"), \
            patch.object(smtplib.SMTP, "sendmail", sendmail), \
            patch.object(smtplib.SMTP, "send_message", sendmail), \
            patch.object(os, "write", write):
        status = SCRIPT.run(data)
    assert not sendmail.called
    return status, server


class InterfaceTest(unittest.TestCase):
    def test_outcomes_are_the_four_of_the_design(self):
        self.assertEqual({outcome.value for outcome in mail.SendOutcome},
                         {"accepted", "not_sent", "refused", "unknown"})

    def test_a_message_holds_one_address_a_subject_and_plain_text(self):
        message = mail.OutboundEmail(to=RECIPIENT, subject=CODE_SUBJECT, text=CODE_TEXT)
        self.assertEqual((message.to, message.subject, message.text), (RECIPIENT, CODE_SUBJECT, CODE_TEXT))
        with self.assertRaises(Exception):
            message.to = "other@example.org"

    def test_repr_shows_neither_the_recipient_nor_the_body(self):
        shown = repr(mail.OutboundEmail(to=RECIPIENT, subject=CODE_SUBJECT, text=CODE_TEXT))
        self.assertNotIn(RECIPIENT, shown)
        self.assertNotIn("000000", shown)

    def test_line_breaks_and_control_characters_raise_before_anything_is_sent(self):
        # C2. The errors never hold the value.
        bad_addresses = ["a@example.org\nBcc: x@example.org", "a@example.org\r\n", "a b@example.org", "a@example.org,b@example.org",
                         "<a@example.org>", "a@@example.org", "a", "", "я@example.org", "a@example.org\x00",
                         "a" * 250 + "@example.org", None, 5]
        for value in bad_addresses:
            with self.subTest(to=value), self.assertRaises(ValueError) as caught:
                mail.OutboundEmail(to=value, subject=CODE_SUBJECT, text=CODE_TEXT)
            if isinstance(value, str) and len(value) > 8:
                self.assertNotIn(value, str(caught.exception))
        for value in ["a\nb", "a\rb", "a\x00b", "a\x85b", "a b", "a‮b", "", "x" * 201, None]:
            with self.subTest(subject=value), self.assertRaises(ValueError):
                mail.OutboundEmail(to=RECIPIENT, subject=value, text=CODE_TEXT)
        for value in ["a\rb", "a\x00b", "a\x1bb", "", "x" * 2001, "\ud800", None]:
            with self.subTest(text=value), self.assertRaises(ValueError):
                mail.OutboundEmail(to=RECIPIENT, subject=CODE_SUBJECT, text=value)

    def test_a_hyphen_edge_domain_is_not_this_modules_rule(self):
        # Design 1.3: such an address passes the form; delivery may fail, and no
        # particular outcome is assigned to it here.
        mail.OutboundEmail(to="a@-x.bg", subject=CODE_SUBJECT, text=CODE_TEXT)

    def test_every_message_fits_the_input_cap(self):
        settings = mail.MailSettings(
            backend="smtp", sender_address="s" * 64 + "@" + "d" * 185 + ".org", sender_name="\U0001F600" * 78,
            smtp_host="h" * 253, smtp_port=65535, smtp_security="starttls",
            smtp_username='"' * 255, smtp_password="\\" * 255)
        for filler in ("\U0001F600", "\n", "я"):
            message = mail.OutboundEmail(to="r" * 64 + "@" + "d" * 185 + ".org", subject=filler.strip() and filler * 200 or "s" * 200,
                                         text=filler * mail.MAX_TEXT_LENGTH)
            self.assertLessEqual(len(mail._payload(settings, message)), mail.MAX_INPUT_BYTES)


class InMemoryBackendTest(unittest.TestCase):
    def message(self, subject=CODE_SUBJECT):
        return mail.OutboundEmail(to=RECIPIENT, subject=subject, text=CODE_TEXT)

    def test_records_and_accepts_by_default(self):
        backend = mail.InMemoryMailer()
        self.assertIs(backend.send(self.message()), mail.SendOutcome.ACCEPTED)
        self.assertEqual(backend.messages, [self.message()])

    def test_any_outcome_per_message_kind_and_a_stated_time(self):
        slept = []
        backend = mail.InMemoryMailer(
            outcome=lambda m: mail.SendOutcome.REFUSED if m.subject == NOTICE_SUBJECT else mail.SendOutcome.UNKNOWN,
            delay=lambda m: 2.5 if m.subject == NOTICE_SUBJECT else 0.5, sleep=slept.append)
        self.assertIs(backend.send(self.message()), mail.SendOutcome.UNKNOWN)
        self.assertIs(backend.send(self.message(NOTICE_SUBJECT)), mail.SendOutcome.REFUSED)
        self.assertEqual(slept, [0.5, 2.5])
        for outcome in mail.SendOutcome:
            backend.outcome = outcome
            self.assertIs(backend.send(self.message()), outcome)

    def test_it_never_sleeps_for_real(self):
        with patch("time.sleep", side_effect=AssertionError("no real sleep")):
            mail.InMemoryMailer(delay=30.0).send(self.message())


class SettingsTest(unittest.TestCase):
    def problems(self, production=True, **changes):
        env = {**SMTP_ENV, **changes}
        env = {name: value for name, value in env.items() if value is not None}
        problems = mail.settings_problems(env, production=production)
        loaded = mail.load_settings(env, production=production)
        self.assertEqual(loaded is None, bool(problems), "settings are usable exactly when there is no problem")
        return problems

    def assert_one_problem(self, names, **changes):
        problems = self.problems(**changes)
        self.assertEqual(len(problems), 1, problems)
        for name in [names] if isinstance(names, str) else names:
            self.assertIn(name, problems[0])
        for value in changes.values():
            if value and len(value) > 8:
                self.assertNotIn(value, problems[0], "a problem names the setting, never its value")
        self.assertNotIn(SMTP_PASSWORD, problems[0])

    def test_no_backend_means_disabled_and_is_not_an_error(self):
        # C9: whatever else is set.
        for env in ({}, {mail.BACKEND_ENV: ""}, {mail.BACKEND_ENV: "  "},
                    {name: value for name, value in SMTP_ENV.items() if name != mail.BACKEND_ENV},
                    {mail.FROM_ENV: "not an address", mail.HOURLY_CAP_ENV: "9000"}):
            for production in (True, False):
                with self.subTest(env=sorted(env), production=production):
                    self.assertEqual(mail.settings_problems(env, production=production), [])
                    self.assertIsNone(mail.load_settings(env, production=production))

    def test_complete_smtp_settings_load(self):
        settings = mail.load_settings(SMTP_ENV, production=True)
        self.assertEqual(
            (settings.backend, settings.sender_address, settings.sender_name, settings.smtp_host, settings.smtp_port,
             settings.smtp_security, settings.smtp_username, settings.smtp_password, settings.hourly_cap),
            ("smtp", SENDER, "LearnPilot", "relay.example.org", 587, "starttls", "synthetic-user", SMTP_PASSWORD, 300))
        self.assertNotIn(SMTP_PASSWORD, repr(settings))
        self.assertNotIn("synthetic-user", repr(settings))

    def test_development_backends_are_refused_for_production(self):
        # C9.
        with tempfile.TemporaryDirectory() as spool:
            for backend, extra in (("console", {}), ("file", {mail.SPOOL_DIR_ENV: spool}), ("CONSOLE", {})):
                env = {mail.BACKEND_ENV: backend, mail.FROM_ENV: SENDER, **extra}
                with self.subTest(backend=backend):
                    problems = mail.settings_problems(env, production=True)
                    self.assertEqual(len(problems), 1)
                    self.assertIn(mail.BACKEND_ENV, problems[0])
                    self.assertIsNone(mail.load_settings(env, production=True))
                    development = mail.load_settings(env, production=False)
                    self.assertEqual(mail.settings_problems(env, production=False), [])
                    self.assertIsInstance(mail.build_mailer(development, production=False),
                                          (mail.ConsoleMailer, mail.FileMailer))
                    with self.assertRaises(mail.MailConfigurationError):
                        mail.build_mailer(development, production=True)

    def test_the_production_flag_has_no_default(self):
        for function in (mail.settings_problems, mail.load_settings):
            with self.assertRaises(TypeError):
                function(SMTP_ENV)
        with self.assertRaises(TypeError):
            mail.build_mailer(mail.load_settings(SMTP_ENV, production=True))

    def test_unknown_backend(self):
        self.assert_one_problem(mail.BACKEND_ENV, **{mail.BACKEND_ENV: "sendgrid-api"})
        self.assert_one_problem(mail.BACKEND_ENV, **{mail.BACKEND_ENV: "memory"})

    def test_a_missing_or_malformed_sender_disables_mail(self):
        # C10, D17: there is no default sender.
        self.assert_one_problem(mail.FROM_ENV, **{mail.FROM_ENV: None})
        self.assert_one_problem(mail.FROM_ENV, **{mail.FROM_ENV: ""})
        malformed = ["no-reply", "no-reply@", "@example.org", "no-reply@localhost", "no-reply@example", "a b@example.org",
                     "no-reply@example.org\nBcc: x@example.org", "Name <no-reply@example.org", "Name no-reply@example.org>",
                     "<>", "a@b@example.org", ".a@example.org", "a..b@example.org", "a.@example.org", "a@-example.org",
                     "a@example-.org", "a@example.o1", "a@example.c", "я@example.org", "a@пример.бг",
                     'LearnPilot "quoted" <no-reply@example.org>', "Learn‮Pilot <no-reply@example.org>",
                     "x" * 79 + " <no-reply@example.org>", "a@example.org, b@example.org", "a" * 65 + "@example.org"]
        for value in malformed:
            with self.subTest(value=value):
                self.assert_one_problem(mail.FROM_ENV, **{mail.FROM_ENV: value})

    def test_sender_forms_that_are_accepted(self):
        for value, expected in (
                (SENDER, (SENDER, None)), (f"<{SENDER}>", (SENDER, None)), (f"  LearnPilot   <{SENDER}> ", (SENDER, "LearnPilot")),
                (f"ЛърнПайлът <{SENDER}>", (SENDER, "ЛърнПайлът")), ("No.Reply+x@Mail.Example.ORG", ("No.Reply+x@Mail.Example.ORG", None))):
            with self.subTest(value=value):
                settings = mail.load_settings({**SMTP_ENV, mail.FROM_ENV: value}, production=True)
                self.assertEqual((settings.sender_address, settings.sender_name), expected)

    def test_no_sender_address_exists_in_the_code(self):
        for path in (mail.__file__, mail.SENDER_SCRIPT):
            for node in ast.walk(ast.parse(Path(path).read_text(encoding="utf-8"))):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and not _is_docstring_like(node.value):
                    self.assertNotRegex(node.value, r"[A-Za-z0-9]@[A-Za-z0-9-]+\.[A-Za-z]", path)

    def test_hourly_cap(self):
        for value in ("301", "0", "-1", "3.5", "abc", "1e2", "٣", "99999", "+5"):
            with self.subTest(value=value):
                self.assert_one_problem(mail.HOURLY_CAP_ENV, **{mail.HOURLY_CAP_ENV: value})
        for value, expected in (("300", 300), ("1", 1), (" 25 ", 25), (None, 300)):
            settings = mail.load_settings({k: v for k, v in {**SMTP_ENV, mail.HOURLY_CAP_ENV: value}.items() if v is not None},
                                          production=True)
            self.assertEqual(settings.hourly_cap, expected)

    def test_smtp_values_each_reported_by_name(self):
        for name in (mail.SMTP_HOST_ENV, mail.SMTP_PORT_ENV, mail.SMTP_SECURITY_ENV):
            with self.subTest(missing=name):
                self.assert_one_problem(name, **{name: None})
        for value in ("smtp://relay.example.org", "relay.example.org:587", "relay example.org", "relay_.example.org",
                      "-relay.example.org", "relay..example.org", "x" * 300, "релей.бг"):
            with self.subTest(host=value):
                self.assert_one_problem(mail.SMTP_HOST_ENV, **{mail.SMTP_HOST_ENV: value})
        for value in ("0", "65536", "smtp", "58 7", "-587", "5.87", "999999"):
            with self.subTest(port=value):
                self.assert_one_problem(mail.SMTP_PORT_ENV, **{mail.SMTP_PORT_ENV: value})
        for value in ("ssl", "plain", "starttls,none", "true"):
            with self.subTest(security=value):
                self.assert_one_problem(mail.SMTP_SECURITY_ENV, **{mail.SMTP_SECURITY_ENV: value})

    def test_credentials_together_and_never_echoed(self):
        self.assert_one_problem((mail.SMTP_USERNAME_ENV, mail.SMTP_PASSWORD_ENV), **{mail.SMTP_USERNAME_ENV: None})
        self.assert_one_problem((mail.SMTP_USERNAME_ENV, mail.SMTP_PASSWORD_ENV), **{mail.SMTP_PASSWORD_ENV: None})
        for value in ("sec\nret-value", "sec\x00ret-value", "парола-парола", "x" * 256):
            with self.subTest(length=len(value)):
                self.assert_one_problem(mail.SMTP_PASSWORD_ENV, **{mail.SMTP_PASSWORD_ENV: value})
                self.assert_one_problem(mail.SMTP_USERNAME_ENV, **{mail.SMTP_USERNAME_ENV: value})
        settings = mail.load_settings({**SMTP_ENV, mail.SMTP_USERNAME_ENV: None, mail.SMTP_PASSWORD_ENV: None}
                                      | {mail.SMTP_USERNAME_ENV: "", mail.SMTP_PASSWORD_ENV: ""}, production=True)
        self.assertEqual((settings.smtp_username, settings.smtp_password), (None, None))

    def test_plain_transport_only_to_a_loopback_host(self):
        # C3.
        for host in ("relay.example.org", "192.0.2.10", "2001:db8::1", "localhost.example.org", "0.0.0.0"):
            with self.subTest(host=host):
                self.assert_one_problem(mail.SMTP_SECURITY_ENV,
                                        **{mail.SMTP_HOST_ENV: host, mail.SMTP_SECURITY_ENV: "none"})
        for host in ("localhost", "LOCALHOST", "127.0.0.1", "127.8.9.10", "::1"):
            with self.subTest(host=host):
                self.assertEqual(self.problems(**{mail.SMTP_HOST_ENV: host, mail.SMTP_SECURITY_ENV: "none"}), [])
        for security in ("starttls", "tls", "TLS"):
            self.assertEqual(self.problems(**{mail.SMTP_SECURITY_ENV: security}), [])

    def test_spool_directory(self):
        base = {mail.BACKEND_ENV: "file", mail.FROM_ENV: SENDER}
        with tempfile.TemporaryDirectory() as spool:
            cases = ((None, False), ("relative/spool", False), (str(ROOT / "data"), False), (str(ROOT), False),
                     (os.path.join(spool, "missing"), False), (spool, True))
            for value, usable in cases:
                env = dict(base) if value is None else {**base, mail.SPOOL_DIR_ENV: value}
                with self.subTest(value=value):
                    problems = mail.settings_problems(env, production=False)
                    self.assertEqual(problems == [], usable, problems)
                    if not usable:
                        self.assertEqual(len(problems), 1)
                        self.assertIn(mail.SPOOL_DIR_ENV, problems[0])

    def test_several_problems_are_all_reported(self):
        problems = mail.settings_problems({mail.BACKEND_ENV: "smtp", mail.SMTP_PASSWORD_ENV: SMTP_PASSWORD}, production=True)
        for name in (mail.FROM_ENV, mail.SMTP_HOST_ENV, mail.SMTP_PORT_ENV, mail.SMTP_SECURITY_ENV, mail.SMTP_USERNAME_ENV):
            self.assertTrue(any(name in problem for problem in problems), name)
        self.assertNotIn(SMTP_PASSWORD, " ".join(problems))

    def test_setting_names_are_the_designs(self):
        self.assertEqual(set(mail.SETTING_NAMES), {
            "DZI_MAIL_BACKEND", "DZI_MAIL_FROM", "DZI_SMTP_HOST", "DZI_SMTP_PORT", "DZI_SMTP_SECURITY",
            "DZI_SMTP_USERNAME", "DZI_SMTP_PASSWORD", "DZI_MAIL_SPOOL_DIR", "DZI_MAIL_HOURLY_CAP"})

    def test_mailers_refuse_settings_of_another_backend(self):
        console = mail.load_settings({mail.BACKEND_ENV: "console", mail.FROM_ENV: SENDER}, production=False)
        smtp = mail.load_settings(SMTP_ENV, production=True)
        for mailer, settings in ((mail.SmtpMailer, console), (mail.ConsoleMailer, smtp), (mail.FileMailer, smtp)):
            with self.assertRaises(mail.MailConfigurationError):
                mailer(settings)
        self.assertIsInstance(mail.build_mailer(smtp, production=True), mail.SmtpMailer)


def _is_docstring_like(value):
    return "\n" in value or len(value) > 120


class DevelopmentBackendsTest(unittest.TestCase):
    MESSAGE = mail.OutboundEmail(to=RECIPIENT, subject=CODE_SUBJECT, text=CODE_TEXT)

    def test_console_writes_the_message_to_its_stream(self):
        settings = mail.load_settings({mail.BACKEND_ENV: "console", mail.FROM_ENV: f"LearnPilot <{SENDER}>"}, production=False)
        stream = io.StringIO()
        self.assertIs(mail.ConsoleMailer(settings, stream).send(self.MESSAGE), mail.SendOutcome.ACCEPTED)
        for expected in (RECIPIENT, SENDER, CODE_SUBJECT, "000000", "development only"):
            self.assertIn(expected, stream.getvalue())

    def test_console_defaults_to_standard_error(self):
        settings = mail.load_settings({mail.BACKEND_ENV: "console", mail.FROM_ENV: SENDER}, production=False)
        with patch.object(sys, "stderr", io.StringIO()) as stream:
            mail.build_mailer(settings, production=False).send(self.MESSAGE)
            self.assertIn("000000", stream.getvalue())

    def test_a_failing_stream_is_not_sent_and_never_raises(self):
        settings = mail.load_settings({mail.BACKEND_ENV: "console", mail.FROM_ENV: SENDER}, production=False)
        stream = Mock()
        stream.write.side_effect = OSError("closed")
        with self.assertLogs("learnpilot.registration", "WARNING") as logs:
            self.assertIs(mail.ConsoleMailer(settings, stream).send(self.MESSAGE), mail.SendOutcome.NOT_SENT)
        self.assertEqual(logs.output, ["WARNING:learnpilot.registration:sender_error error=OSError"])

    def test_file_backend_writes_one_private_file_per_message(self):
        with tempfile.TemporaryDirectory() as spool:
            settings = mail.load_settings({mail.BACKEND_ENV: "file", mail.FROM_ENV: SENDER, mail.SPOOL_DIR_ENV: spool},
                                          production=False)
            backend = mail.build_mailer(settings, production=False)
            self.assertIs(backend.send(self.MESSAGE), mail.SendOutcome.ACCEPTED)
            self.assertIs(backend.send(self.MESSAGE), mail.SendOutcome.ACCEPTED)
            names = sorted(os.listdir(spool))
            self.assertEqual(len(names), 2)
            for name in names:
                path = os.path.join(spool, name)
                self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
                content = Path(path).read_text(encoding="utf-8")
                self.assertIn(f"To: {RECIPIENT}", content)
                self.assertIn("000000", content)
                self.assertNotIn(RECIPIENT, name)

    def test_file_backend_failure_is_not_sent(self):
        with tempfile.TemporaryDirectory() as spool:
            settings = mail.load_settings({mail.BACKEND_ENV: "file", mail.FROM_ENV: SENDER, mail.SPOOL_DIR_ENV: spool},
                                          production=False)
        with self.assertLogs("learnpilot.registration", "WARNING"):
            self.assertIs(mail.FileMailer(settings).send(self.MESSAGE), mail.SendOutcome.NOT_SENT)


class SenderScriptStaticTest(unittest.TestCase):
    """C12, C19 (static half), C15 (static half)."""

    @classmethod
    def setUpClass(cls):
        cls.source = Path(mail.SENDER_SCRIPT).read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source)

    def test_the_path_is_absolute_exists_and_sits_beside_the_mail_module(self):
        self.assertTrue(os.path.isabs(mail.SENDER_SCRIPT))
        self.assertTrue(os.path.isfile(mail.SENDER_SCRIPT))
        self.assertEqual(Path(mail.SENDER_SCRIPT), Path(mail.__file__).resolve().parent / "mail_sender.py")

    def test_first_statements_reset_unblock_and_arm_before_any_other_import(self):
        body = self.tree.body
        self.assertIsInstance(body[0], ast.Expr)
        self.assertIsInstance(body[0].value, ast.Constant, "a docstring, which executes nothing")
        self.assertEqual([ast.unparse(node) for node in body[1:5]], [
            "import signal",
            "signal.signal(signal.SIGALRM, signal.SIG_DFL)",
            "signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGALRM})",
            "signal.alarm(9)",
        ])
        self.assertTrue(all(isinstance(node, (ast.Import, ast.ImportFrom)) for node in body[5:8]))

    def test_the_statements_run_in_that_order(self):
        self.assertEqual(SIGNAL_CALLS, [
            call.signal(signal.SIGALRM, signal.SIG_DFL),
            call.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGALRM}),
            call.alarm(9),
        ])

    def test_the_alarm_outlasts_the_communication_deadline(self):
        self.assertEqual(mail.COMMUNICATION_DEADLINE, 8.0)
        self.assertEqual(mail.REAP_SECONDS, 2.0)
        self.assertGreater(9, mail.COMMUNICATION_DEADLINE)

    def test_imports_are_standard_library_only_and_nothing_of_the_application(self):
        names = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0, "no relative import")
                names.add(node.module.split(".")[0])
        self.assertTrue(names)
        self.assertLessEqual(names, set(sys.stdlib_module_names))
        self.assertFalse(names & {"web", "src", "site", "sqlite3", "flask", "logging", "subprocess"})

    def test_no_debug_output_printing_or_file_access(self):
        calls = {ast.unparse(node.func) for node in ast.walk(self.tree) if isinstance(node, ast.Call)}
        self.assertFalse(calls & {"print", "open", "os.open", "io.open", "server.set_debuglevel", "server.sendmail",
                                  "server.send_message", "os.read" if False else "input", "sys.stdout.write"})
        self.assertNotIn("debuglevel", self.source)
        self.assertNotIn("sendmail", self.source)
        self.assertNotIn("argv", self.source)
        self.assertNotIn("environ", self.source)

    def test_the_marker_is_one_unbuffered_write_of_one_byte(self):
        writes = [ast.unparse(node) for node in ast.walk(self.tree)
                  if isinstance(node, ast.Call) and ast.unparse(node.func) == "os.write"]
        self.assertEqual(writes, ["os.write(1, MARKER)"])
        self.assertEqual(SCRIPT.MARKER, b"R")

    def test_nothing_in_the_application_imports_the_script(self):
        for folder in ("web", "src", "deploy"):
            for path in (ROOT / folder).rglob("*.py"):
                if path.resolve() == Path(mail.SENDER_SCRIPT):
                    continue
                for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                    if isinstance(node, ast.Import):
                        imported = [alias.name for alias in node.names]
                    elif isinstance(node, ast.ImportFrom):
                        imported = [f"{node.module}.{alias.name}" for alias in node.names] + [node.module or ""]
                    else:
                        continue
                    self.assertFalse([name for name in imported if name.split(".")[-1] == "mail_sender"], str(path))

    def test_the_mail_module_is_standard_library_only(self):
        names = set()
        for node in ast.walk(ast.parse(Path(mail.__file__).read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.add(node.module.split(".")[0])
        self.assertLessEqual(names - {"__future__"}, set(sys.stdlib_module_names))
        self.assertFalse(names & {"web", "src", "flask", "sqlite3", "smtplib", "ssl"})

    def test_the_script_and_the_parent_agree_on_the_protocol(self):
        self.assertEqual(SCRIPT.MAX_INPUT_BYTES, mail.MAX_INPUT_BYTES)
        self.assertEqual(SCRIPT.MAX_INPUT_BYTES, 16 * 1024)
        self.assertEqual(mail.MAX_OUTPUT_BYTES, 16)
        self.assertEqual(SCRIPT.MARKER, mail.MARKER)
        self.assertEqual((SCRIPT.EXIT_ACCEPTED, SCRIPT.EXIT_REFUSED), (mail.EXIT_ACCEPTED, mail.EXIT_REFUSED))
        before = [SCRIPT.EXIT_INPUT, SCRIPT.EXIT_CONNECT, SCRIPT.EXIT_TLS, SCRIPT.EXIT_HELLO, SCRIPT.EXIT_AUTH,
                  SCRIPT.EXIT_SENDER_REFUSED, SCRIPT.EXIT_BEFORE_MARKER]
        self.assertTrue(all(status in mail.EXIT_NOT_SENT for status in before))
        self.assertEqual(list(mail.EXIT_NOT_SENT), list(range(10, 20)))
        listed = (0, 20, *range(10, 20))
        self.assertNotIn(SCRIPT.EXIT_AFTER_MARKER, listed)
        self.assertNotIn(SCRIPT.EXIT_SERVICE_UNAVAILABLE, listed, "421 without the marker must not read as not sent")
        self.assertEqual((SCRIPT.EXIT_AFTER_MARKER, SCRIPT.EXIT_SERVICE_UNAVAILABLE),
                         (mail.EXIT_AFTER_MARKER, mail.EXIT_SERVICE_UNAVAILABLE))
        self.assertEqual((mail.EXIT_AFTER_MARKER, mail.EXIT_SERVICE_UNAVAILABLE), (30, 31))
        for output in (b"", b"R"):
            for status in (30, 31):
                self.assertIs(mail._outcome(output, True, status), mail.SendOutcome.UNKNOWN)
        self.assertEqual(set(json.loads(mail._payload(
            mail.load_settings(SMTP_ENV, production=True),
            mail.OutboundEmail(to=RECIPIENT, subject=CODE_SUBJECT, text=CODE_TEXT)))), set(SCRIPT.KEYS))
        self.assertEqual(SCRIPT.SOCKET_TIMEOUT, 3.0)


class SenderScriptExchangeTest(unittest.TestCase):
    """C1, C3, C4: the script's SMTP conversation, by exit status and marker."""

    def assert_nothing_for_any_recipient(self, server):
        self.assertNotIn("RCPT", server.verbs())
        self.assertNotIn("DATA", server.verbs())
        self.assertFalse([event for event in server.events if event[0] == "marker"])

    def test_clean_exchange_is_zero_after_the_marker(self):
        status, server = run_script(encoded())
        self.assertEqual(status, 0)
        self.assertEqual(server.verbs(), ["EHLO", "STARTTLS", "EHLO", "AUTH", "MAIL", "RCPT", "DATA", "MESSAGE", "QUIT"])
        self.assertEqual([event for event in server.events if event[0] == "marker"], [("marker", 1, b"R")])
        self.assertEqual(server.events[0], ("connect", "relay.example.org", 587, 3.0))
        (recipient_command,) = server.sent("RCPT")
        self.assertEqual(envelope(recipient_command, "RCPT TO"), f":<{RECIPIENT}>\r\n".encode())

    def test_the_marker_precedes_the_recipient_in_every_path_that_issues_it(self):
        # C4: the write of ``R`` has returned before the recipient command is issued.
        paths = (
            {}, {"replies": {"RCPT": b"550 no such user\r\n"}}, {"replies": {"RCPT": b"451 try later\r\n"}},
            {"replies": {"DATA": b"554 no\r\n"}}, {"replies": {"MESSAGE": b"552 too big\r\n"}},
            {"replies": {"RCPT": None}}, {"raises": {"RCPT": TimeoutError()}}, {"raises": {"MESSAGE": ConnectionResetError()}},
        )
        for security, port in (("starttls", 587), ("tls", 465)):
            for path in paths:
                with self.subTest(security=security, path=path):
                    _status, server = run_script(encoded(security=security, port=port), FakeServer(**path))
                    self.assertLess(server.index("sent", "MAIL"), server.index("marker"))
                    self.assertLess(server.index("marker"), server.index("sent", "RCPT"))
                    self.assertEqual(len([e for e in server.events if e[0] == "marker"]), 1)
                    self.assertEqual(len(server.sent("RCPT")), 1, "one recipient, one attempt")

    def test_failures_before_the_marker_are_10_to_19_with_no_output(self):
        # C4: refused connection, TLS failure, AUTH failure, refused sender.
        cases = {
            "refused connection": (FakeServer(connect_error=ConnectionRefusedError()), 11),
            "name lookup": (FakeServer(connect_error=socket.gaierror(8, "synthetic")), 11),
            "connect timeout": (FakeServer(connect_error=TimeoutError()), 11),
            "bad greeting": (FakeServer(replies={"GREETING": b"554 go away\r\n"}), 11),
            "silent greeting": (FakeServer(replies={"GREETING": None}), 11),
            "ehlo refused": (FakeServer(replies={"EHLO": b"502 no\r\n"}), 13),
            "starttls not offered": (FakeServer(replies={"EHLO": b"250-relay\r\n250 AUTH PLAIN\r\n"}), 12),
            "starttls refused": (FakeServer(replies={"STARTTLS": b"454 not now\r\n"}), 12),
            "certificate": (FakeServer(tls_error=ssl.SSLCertVerificationError(1, "synthetic")), 12),
            "handshake": (FakeServer(tls_error=ssl.SSLError(1, "synthetic")), 12),
            "auth refused": (FakeServer(replies={"AUTH": b"535 bad credentials\r\n"}), 14),
            "auth not offered": (FakeServer(replies={"EHLO": b"250-relay\r\n250 STARTTLS\r\n"}), 14),
            "auth timeout": (FakeServer(raises={"AUTH": TimeoutError()}), 14),
            "sender refused": (FakeServer(replies={"MAIL": b"550 sender rejected\r\n"}), 15),
            "sender deferred": (FakeServer(replies={"MAIL": b"451 later\r\n"}), 15),
            "sender timeout": (FakeServer(raises={"MAIL": TimeoutError()}), 15),
            "sender disconnect": (FakeServer(replies={"MAIL": None}), 15),
        }
        for name, (server, expected) in cases.items():
            with self.subTest(case=name):
                status, server = run_script(encoded(), server)
                self.assertEqual(status, expected)
                self.assertIn(status, mail.EXIT_NOT_SENT)
                self.assert_nothing_for_any_recipient(server)

    def test_implicit_tls_failure_is_before_the_marker(self):
        status, server = run_script(encoded(security="tls", port=465),
                                    FakeServer(tls_error=ssl.SSLCertVerificationError(1, "synthetic")))
        self.assertEqual(status, 12)
        self.assertEqual(server.verbs(), [])

    def reply_status(self, verb, reply, **changes):
        status, server = run_script(encoded(**changes), FakeServer(replies={verb: reply}))
        return status, server

    def test_a_permanent_refusal_after_the_marker_is_20(self):
        # Policy approved by Gabriel (design 4.2): 5xx to the recipient, the data command or the message.
        for verb, reply in (("RCPT", b"550 no such user\r\n"), ("RCPT", b"553 bad mailbox\r\n"), ("RCPT", b"500 ?\r\n"),
                            ("RCPT", b"599 x\r\n"), ("DATA", b"554 no\r\n"), ("DATA", b"503 bad sequence\r\n"),
                            ("MESSAGE", b"552 too big\r\n"), ("MESSAGE", b"554 rejected\r\n"), ("MESSAGE", b"550 spam\r\n")):
            with self.subTest(verb=verb, reply=reply):
                status, server = self.reply_status(verb, reply)
                self.assertEqual(status, 20)
                self.assertLess(server.index("marker"), server.index("sent", "RCPT"))

    def test_a_transient_refusal_of_the_recipient_is_20(self):
        for reply in (b"450 greylisted\r\n", b"451 try later\r\n", b"452 too many recipients\r\n", b"455 no\r\n",
                      b"400 x\r\n", b"499 x\r\n", b"450-first line\r\n450 second line\r\n"):
            with self.subTest(reply=reply):
                status, server = self.reply_status("RCPT", reply)
                self.assertEqual(status, 20)
                self.assertNotIn("DATA", server.verbs())

    def test_a_transient_reply_to_the_data_or_the_message_establishes_nothing(self):
        # Not a refusal: status 30, which the parent reads as unknown.
        for verb in ("DATA", "MESSAGE"):
            for reply in (b"450 later\r\n", b"451 local error\r\n", b"452 insufficient storage\r\n", b"400 x\r\n", b"499 x\r\n"):
                with self.subTest(verb=verb, reply=reply):
                    status, server = self.reply_status(verb, reply)
                    self.assertEqual(status, SCRIPT.EXIT_AFTER_MARKER)
                    self.assertNotIn(status, (0, 20, *mail.EXIT_NOT_SENT))
                    self.assertEqual([e for e in server.events if e[0] == "marker"], [("marker", 1, b"R")])

    def test_421_after_the_marker_is_its_own_status_never_a_refusal(self):
        for verb in ("RCPT", "DATA", "MESSAGE"):
            for reply in (b"421 closing channel\r\n", b"421-busy\r\n421 closing\r\n"):
                with self.subTest(verb=verb, reply=reply):
                    status, server = self.reply_status(verb, reply)
                    self.assertEqual(status, SCRIPT.EXIT_SERVICE_UNAVAILABLE)
                    self.assertEqual([e for e in server.events if e[0] == "marker"], [("marker", 1, b"R")])

    def test_421_before_the_marker_is_not_a_10_to_19_status(self):
        # Without ``R`` a 10-19 status would be read as "not sent". 421 at any stage is unknown.
        for security, port in (("starttls", 587), ("tls", 465), ("none", 25)):
            for verb in ("GREETING", "EHLO", "STARTTLS", "AUTH", "MAIL"):
                if verb == "STARTTLS" and security != "starttls":
                    continue
                host = "localhost" if security == "none" else "relay.example.org"
                with self.subTest(security=security, stage=verb):
                    status, server = self.reply_status(verb, b"421 service not available, closing channel\r\n",
                                                       security=security, port=port, host=host)
                    self.assertEqual(status, SCRIPT.EXIT_SERVICE_UNAVAILABLE)
                    self.assertNotIn(status, mail.EXIT_NOT_SENT)
                    self.assert_nothing_for_any_recipient(server)
                    # Nothing follows the command that was answered 421.
                    self.assertEqual(server.verbs()[-1:], [] if verb == "GREETING" else [verb])
                    self.assertIs(mail._outcome(b"", True, status), mail.SendOutcome.UNKNOWN)

    def test_other_replies_before_the_marker_keep_their_10_to_19_status(self):
        # Only 421 is singled out: 420, 422, 450 and 5xx at these stages are unchanged.
        for verb, expected in (("GREETING", 11), ("EHLO", 13), ("STARTTLS", 12), ("AUTH", 14), ("MAIL", 15)):
            for reply in (b"420 x\r\n", b"422 x\r\n", b"450 later\r\n", b"451 later\r\n", b"550 no\r\n", b"554 no\r\n"):
                with self.subTest(stage=verb, reply=reply):
                    status, server = self.reply_status(verb, reply)
                    self.assertEqual(status, expected)
                    self.assert_nothing_for_any_recipient(server)

    def test_an_exchange_that_breaks_after_the_marker_has_no_meaning(self):
        # Neither accepted nor refused: the parent reads the status as unknown.
        for path in ({"replies": {"RCPT": None}}, {"raises": {"RCPT": TimeoutError()}}, {"replies": {"DATA": None}},
                     {"raises": {"DATA": ConnectionResetError()}}, {"replies": {"MESSAGE": None}},
                     {"raises": {"MESSAGE": TimeoutError()}}, {"replies": {"RCPT": b"garbage\r\n"}},
                     {"replies": {"RCPT": b"354 what\r\n"}}, {"replies": {"DATA": b"garbage\r\n"}},
                     {"replies": {"MESSAGE": b"garbage\r\n"}}, {"replies": {"MESSAGE": b"354 again\r\n"}}):
            with self.subTest(path=path):
                status, _server = run_script(encoded(), FakeServer(**path))
                self.assertEqual(status, SCRIPT.EXIT_AFTER_MARKER)

    def test_a_failed_quit_does_not_change_an_accepted_message(self):
        for path in ({"replies": {"QUIT": None}}, {"raises": {"QUIT": TimeoutError()}}, {"replies": {"QUIT": b"500 ?\r\n"}}):
            self.assertEqual(run_script(encoded(), FakeServer(**path))[0], 0)

    def test_a_marker_that_cannot_be_written_stops_before_the_recipient(self):
        for failure in (BrokenPipeError(), OSError(), 0):
            with self.subTest(failure=failure):
                status, server = run_script(encoded(), marker_result=failure)
                self.assertEqual(status, 19)
                self.assertNotIn("RCPT", server.verbs())

    def test_the_sender_command_has_no_options_and_does_not_depend_on_the_message(self):
        # C4: no SIZE, whether the server advertises it or not; identical for both messages.
        commands = set()
        for ehlo in (FakeServer.EHLO, b"250-relay\r\n250-STARTTLS\r\n250-8BITMIME\r\n250-SMTPUTF8\r\n250 AUTH PLAIN\r\n"):
            for subject, text, to in ((CODE_SUBJECT, CODE_TEXT, RECIPIENT), (NOTICE_SUBJECT, NOTICE_TEXT, "teacher@example.org")):
                status, server = run_script(encoded(subject=subject, text=text, to=to), FakeServer(replies={"EHLO": ehlo}))
                self.assertEqual(status, 0)
                commands.update(server.sent("MAIL"))
                before_marker = b"".join(e[2] for e in server.events[:server.index("marker")] if e[0] == "sent")
                self.assertNotIn(to.encode(), before_marker)
        self.assertEqual(len(commands), 1, "byte-identical for a code and for a notice, SIZE advertised or not")
        (command,) = commands
        self.assertEqual(envelope(command, "MAIL FROM"), f":<{SENDER}>\r\n".encode(), "no option follows the address")
        self.assertNotIn(b"SIZE", command.upper())

    def test_the_message_has_one_recipient_the_configured_sender_and_one_utf8_text_part(self):
        # C1.
        status, server = run_script(encoded())
        self.assertEqual(status, 0)
        (raw,) = server.sent("MESSAGE")
        self.assertTrue(raw.endswith(b"\r\n.\r\n"))
        self.assertTrue(raw.isascii(), "7-bit clean: no 8BITMIME or SMTPUTF8 is needed")
        parsed = email.message_from_bytes(raw[:-3].replace(b"\r\n..", b"\r\n."), policy=email.policy.default)
        self.assertEqual(parsed["To"], RECIPIENT)
        self.assertEqual(parsed["From"].addresses[0].addr_spec, SENDER)
        self.assertEqual(parsed["From"].addresses[0].display_name, "LearnPilot")
        self.assertEqual(parsed["Subject"], CODE_SUBJECT)
        self.assertFalse(parsed.is_multipart())
        self.assertEqual(parsed.get_content_type(), "text/plain")
        self.assertEqual(parsed.get_content_charset(), "utf-8")
        self.assertEqual(parsed["Content-Transfer-Encoding"], "base64")
        self.assertEqual(parsed.get_content().replace("\r\n", "\n").rstrip("\n"), CODE_TEXT)
        self.assertEqual(sorted(key.lower() for key in parsed.keys()),
                         ["content-transfer-encoding", "content-type", "date", "from", "message-id", "mime-version",
                          "subject", "to"])
        self.assertTrue(parsed["Message-ID"].endswith("@example.org>"))
        self.assertNotIn("Bcc", parsed)

    def test_a_sender_without_a_display_name(self):
        _status, server = run_script(encoded(sender_name=None))
        parsed = email.message_from_bytes(server.sent("MESSAGE")[0], policy=email.policy.default)
        self.assertEqual(str(parsed["From"]), SENDER)

    def test_tls_modes_verify_the_certificate_and_the_host_name(self):
        # C3.
        context = SCRIPT._tls_context()
        self.assertIs(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertGreaterEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)
        for security, port in (("starttls", 587), ("tls", 465)):
            with self.subTest(security=security):
                status, server = run_script(encoded(security=security, port=port))
                self.assertEqual(status, 0)
                tls = [event for event in server.events if event[0] == "tls"]
                self.assertEqual(tls, [("tls", ssl.CERT_REQUIRED, True, ssl.TLSVersion.TLSv1_2, "relay.example.org")])
                self.assertLess(server.index("tls"), server.index("sent", "AUTH"), "credentials only after TLS")
                self.assertLess(server.index("tls"), server.index("sent", "MAIL"))
        self.assertEqual(run_script(encoded(security="tls", port=465))[1].verbs()[0], "EHLO")

    def test_plain_transport_is_loopback_only_and_refused_before_any_network_call(self):
        for host in ("relay.example.org", "192.0.2.10", "2001:db8::1"):
            status, server = run_script(encoded(host=host, security="none"))
            self.assertEqual((status, server.events), (10, []))
        for host in ("localhost", "127.0.0.1", "::1"):
            status, server = run_script(encoded(host=host, security="none", port=25))
            self.assertEqual(status, 0)
            self.assertFalse([event for event in server.events if event[0] == "tls"])
            self.assertNotIn("STARTTLS", server.verbs())

    def test_without_credentials_no_auth_is_attempted(self):
        status, server = run_script(encoded(username=None, password=None))
        self.assertEqual(status, 0)
        self.assertNotIn("AUTH", server.verbs())

    def test_no_file_is_opened(self):
        # C12 (instrumented open).
        opened = Mock(side_effect=AssertionError("the sender opens no file"))
        with patch("builtins.open", opened), patch("io.open", opened), patch("os.open", opened):
            self.assertEqual(run_script(encoded())[0], 0)
            self.assertEqual(run_script(encoded(security="tls", port=465))[0], 0)
            self.assertEqual(run_script(b"{")[0], 10)
        opened.assert_not_called()


class SenderScriptInputTest(unittest.TestCase):
    """C14: bounded, exact input; anything else ends before any network call."""

    def assert_input_error(self, data):
        status, server = run_script(data)
        self.assertEqual(status, 10)
        self.assertEqual(server.events, [], "no network call")

    def test_too_long_truncated_and_malformed_input(self):
        self.assert_input_error(None)  # what the reader returns past the cap
        valid = encoded()
        for data in (b"", b"{", valid[:-1], valid[: len(valid) // 2], b"[]", b"null", b'"text"', valid + b"x",
                     b"\xff\xfe", valid.replace(b"{", b"[", 1)):
            with self.subTest(data=data[:12]):
                self.assert_input_error(data)

    def test_unknown_and_missing_keys(self):
        self.assert_input_error(json.dumps({**document(), "extra": 1}).encode())
        self.assert_input_error(json.dumps({**document(), "bcc": "x@example.org"}).encode())
        for key in document():
            values = document()
            del values[key]
            with self.subTest(missing=key):
                self.assert_input_error(json.dumps(values).encode())

    def test_values_that_fail_their_check(self):
        cases = [
            {"port": "587"}, {"port": 0}, {"port": 65536}, {"port": True}, {"port": 587.0}, {"security": "ssl"},
            {"security": None}, {"host": ""}, {"host": "relay example.org"}, {"host": None}, {"host": "relay.example.org\r\n"},
            {"username": None}, {"password": None}, {"username": "a\r\nb"}, {"password": 5},
            {"to": "a@example.org\r\nRCPT TO:<b@example.org>"}, {"to": "a@example.org b@example.org"}, {"to": ["a@example.org"]},
            {"to": ""}, {"to": "<a@example.org>"}, {"to": "я@example.org"}, {"sender_address": "no-reply"},
            {"sender_address": "a@example.org\nx"}, {"sender_name": "Learn\nPilot"}, {"sender_name": ""},
            {"subject": "a\nBcc: x@example.org"}, {"subject": "a\rb"}, {"subject": ""}, {"subject": "x" * 201},
            {"text": ""}, {"text": "a\x00b"}, {"text": "a\rb"}, {"text": None}, {"text": "\ud800"}, {"text": "x" * 4001},
        ]
        for change in cases:
            with self.subTest(change=change):
                self.assert_input_error(json.dumps(document(**change)).encode())

    def test_exactly_the_cap_is_read_and_one_byte_more_is_refused(self):
        def reader(total):
            stream = io.BytesIO(b" " * total)
            requested = []

            def read(descriptor, size):
                self.assertEqual(descriptor, 0)
                requested.append(size)
                return stream.read(min(size, 5000))  # short reads, as a pipe gives

            with patch.object(os, "read", read):
                return SCRIPT._read_input(), stream.tell(), requested

        data, consumed, requested = reader(16 * 1024)
        self.assertEqual((len(data), consumed), (16 * 1024, 16 * 1024))
        data, consumed, requested = reader(16 * 1024 + 1)
        self.assertEqual((data, consumed), (None, 16 * 1024 + 1))
        data, consumed, requested = reader(1024 * 1024)
        self.assertEqual((data, consumed), (None, 16 * 1024 + 1), "at most 16 KiB plus one byte is read")
        self.assertLessEqual(max(requested), 16 * 1024 + 1)
        self.assertEqual(reader(0)[0], b"")

    def test_a_document_of_exactly_the_cap_is_processed(self):
        valid = encoded()
        status, server = run_script(valid + b" " * (16 * 1024 - len(valid)))
        self.assertEqual(status, 0)
        self.assertIn("RCPT", server.verbs())


class NothingSensitiveIsLoggedTest(unittest.TestCase):
    """C15, for everything that runs in this process."""

    def test_the_script_logs_and_prints_nothing(self):
        records = []
        handler = logging.Handler()
        handler.emit = records.append
        root = logging.getLogger()
        root.addHandler(handler)
        previous = root.level
        root.setLevel(logging.DEBUG)
        try:
            with patch.object(sys, "stdout", io.StringIO()) as out, patch.object(sys, "stderr", io.StringIO()) as err:
                for server in (FakeServer(), FakeServer(replies={"AUTH": b"535 no\r\n"}), FakeServer(replies={"RCPT": None}),
                               FakeServer(tls_error=ssl.SSLError(1, "synthetic"))):
                    run_script(encoded(), server)
                run_script(b"{")
                printed = out.getvalue() + err.getvalue()
        finally:
            root.setLevel(previous)
            root.removeHandler(handler)
        self.assertEqual(records, [])
        self.assertEqual(printed, "")

    def test_smtplib_debug_output_stays_off(self):
        self.assertEqual(smtplib.SMTP.debuglevel, 0)
        self.assertNotIn("debuglevel", Path(mail.__file__).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
