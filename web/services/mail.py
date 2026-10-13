"""Mail delivery interface and backends
(docs/reviews/EMAIL_REGISTRATION_DESIGN.md, section 4).

Independent of Flask, ``web.app``, the blueprints and the database: standard library
only. Nothing here has a caller yet.

* ``OutboundEmail``, ``SendOutcome`` and ``Mailer`` are the interface. ``send`` never
  raises for a delivery problem and never retries: one call starts at most one sender.
* ``SmtpMailer`` runs ``web/services/mail_sender.py`` as a short-lived child process and
  decides the outcome from what that process wrote and how it ended (section 4.2).
  ``NOT_SENT`` needs positive evidence; whatever is not established is ``UNKNOWN``.
  ``ACCEPTED`` means the server took the message for the recipient. It never means the
  message reached a mailbox.
* A process whose ``SIGCHLD`` is ignored cannot learn how a child ended, so ``send``
  starts none there (``NOT_SENT``, ``sender_error error=sigchld``). The check is made
  once, before the child is created: nothing may change the ``SIGCHLD`` disposition of
  the process while a send is running.
* ``sender_busy()`` is what a caller reads **before it admits anything**: while this
  process has a sender it killed and could not reap, it starts no other.
* ``ConsoleMailer`` and ``FileMailer`` are development backends that show the message
  in clear. ``settings_problems`` and ``build_mailer`` refuse them for production.
  ``InMemoryMailer`` is the test backend.
* ``settings_problems`` and ``load_settings`` read the ``DZI_MAIL_*`` / ``DZI_SMTP_*``
  settings. No backend configured means mail is disabled, which is not an error. The
  sender address comes from ``DZI_MAIL_FROM`` only: no default exists.

What is bounded, exactly: the exchange with the child, from ``Popen`` returning to the
decision to kill it, by one monotonic deadline; the input by 16 KiB; the output read by
16 bytes; the reaping after a kill by its own bound. Process creation is not bounded.
One process runs one sender at a time only because the deployment's worker is
synchronous with one thread (``deploy/gunicorn.conf.py``) and so calls ``send`` from one
thread; calls from several threads, as a threaded development server makes, may overlap.
Senders of different processes are not excluded from each other.

Never logged, at any level: the recipient, the subject or body, the sender settings,
credentials. Log lines hold an event name and, for a failure, a fixed word or an
exception class name: ``sender_unreaped`` and ``sender_error error=<word>``, both safe to
pass on to whatever logging a later caller sets up.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import secrets
import selectors
import signal
import subprocess
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Mapping, Protocol, Union

logger = logging.getLogger("learnpilot.registration")

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
# Resolved once, from the location of this file. Never taken from a setting or a caller.
SENDER_SCRIPT = str(Path(__file__).resolve().parent / "mail_sender.py")

COMMUNICATION_DEADLINE = 8.0  # seconds on the monotonic clock, from Popen returning
REAP_SECONDS = 2.0  # the wait after SIGKILL
MAX_INPUT_BYTES = 16 * 1024
MAX_OUTPUT_BYTES = 16
MARKER = b"R"
EXIT_ACCEPTED = 0
EXIT_REFUSED = 20
EXIT_NOT_SENT = range(10, 20)
# Named for the record only: like every status not listed above, the outcome is UNKNOWN,
# with the marker or without it. 30: a failure after the marker that is not a refusal.
# 31: the server answered 421 at any stage.
EXIT_AFTER_MARKER = 30
EXIT_SERVICE_UNAVAILABLE = 31

MAX_ADDRESS_LENGTH = 254
MAX_SUBJECT_LENGTH = 200
MAX_TEXT_LENGTH = 2000

BACKEND_ENV = "DZI_MAIL_BACKEND"
FROM_ENV = "DZI_MAIL_FROM"
SMTP_HOST_ENV = "DZI_SMTP_HOST"
SMTP_PORT_ENV = "DZI_SMTP_PORT"
SMTP_SECURITY_ENV = "DZI_SMTP_SECURITY"
SMTP_USERNAME_ENV = "DZI_SMTP_USERNAME"
SMTP_PASSWORD_ENV = "DZI_SMTP_PASSWORD"
SPOOL_DIR_ENV = "DZI_MAIL_SPOOL_DIR"
HOURLY_CAP_ENV = "DZI_MAIL_HOURLY_CAP"
SETTING_NAMES = (
    BACKEND_ENV, FROM_ENV, SMTP_HOST_ENV, SMTP_PORT_ENV, SMTP_SECURITY_ENV,
    SMTP_USERNAME_ENV, SMTP_PASSWORD_ENV, SPOOL_DIR_ENV, HOURLY_CAP_ENV,
)

BACKEND_SMTP = "smtp"
BACKEND_CONSOLE = "console"
BACKEND_FILE = "file"
BACKENDS = (BACKEND_SMTP, BACKEND_CONSOLE, BACKEND_FILE)
DEVELOPMENT_BACKENDS = (BACKEND_CONSOLE, BACKEND_FILE)
SECURITY_MODES = ("starttls", "tls", "none")
MAX_HOURLY_CAP = 300
MAX_SENDER_NAME_LENGTH = 78
MAX_CREDENTIAL_LENGTH = 255

# What the child may see of the parent's environment: the locale and the certificate
# store overrides. Everything else, every DZI_* value included, is left out.
CHILD_ENVIRONMENT_NAMES = ("LANG", "LC_ALL", "LC_CTYPE", "SSL_CERT_FILE", "SSL_CERT_DIR")

# Structural only: one ASCII address with nothing that could end a header or a command.
# Which addresses the product accepts is the form's rule (design 1.3), not this one.
_RECIPIENT = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,253}", re.ASCII)
_SENDER_LOCAL = re.compile(r"[A-Za-z0-9_%+-]+(?:\.[A-Za-z0-9_%+-]+)*", re.ASCII)
_HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", re.ASCII)
_FROM_WITH_NAME = re.compile(r"(?P<name>[^<>]*)<(?P<address>[^<>\s]*)>")


class SendOutcome(Enum):
    ACCEPTED = "accepted"  # the server accepted the message for the recipient
    NOT_SENT = "not_sent"  # established: nothing was given to the server for any recipient
    REFUSED = "refused"  # a definite refusal of the recipient or the data
    UNKNOWN = "unknown"  # everything that is not established


def _is_control(char: str, *, line_feed: bool = False) -> bool:
    if line_feed and char == "\n":
        return False
    return unicodedata.category(char) in ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp")


@dataclass(frozen=True)
class OutboundEmail:
    """One plain-text message for one recipient. Refused at construction, before
    anything is sent, when a value could break a header: the errors never hold a value."""

    to: str = field(repr=False)  # one normalised address
    subject: str
    text: str = field(repr=False)  # plain text only

    def __post_init__(self):
        if not isinstance(self.to, str) or len(self.to) > MAX_ADDRESS_LENGTH or not _RECIPIENT.fullmatch(self.to):
            raise ValueError("OutboundEmail.to must be one plain ASCII address")
        if (not isinstance(self.subject, str) or not 1 <= len(self.subject) <= MAX_SUBJECT_LENGTH
                or any(_is_control(char) for char in self.subject)):
            raise ValueError("OutboundEmail.subject must be one line of text")
        if (not isinstance(self.text, str) or not 1 <= len(self.text) <= MAX_TEXT_LENGTH
                or any(_is_control(char, line_feed=True) for char in self.text)):
            raise ValueError("OutboundEmail.text must be plain text")


class Mailer(Protocol):
    def send(self, message: OutboundEmail) -> SendOutcome: ...  # never raises for delivery


# --------------------------------------------------------------------------- settings


class MailConfigurationError(RuntimeError):
    """A backend was asked for that the settings or the environment do not allow."""


@dataclass(frozen=True)
class MailSettings:
    backend: str
    sender_address: str
    sender_name: str | None
    hourly_cap: int = MAX_HOURLY_CAP
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_security: str | None = None
    smtp_username: str | None = field(default=None, repr=False)
    smtp_password: str | None = field(default=None, repr=False)
    spool_dir: str | None = None


def _is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_host(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        pass
    if not value.isascii() or len(value) > 253:
        return False
    return all(_HOST_LABEL.fullmatch(label) for label in value.split("."))


def _parse_sender(raw: str) -> tuple[str, str | None] | None:
    """``address`` or ``Name <address>`` → (address, name), or ``None`` when malformed."""
    value = raw.strip()
    match = _FROM_WITH_NAME.fullmatch(value)
    if match:
        name, address = match.group("name").strip(), match.group("address")
    else:
        name, address = "", value
    if len(address) > MAX_ADDRESS_LENGTH or address.count("@") != 1:
        return None
    local, domain = address.split("@")
    labels = domain.split(".")
    if (len(local) > 64 or not _SENDER_LOCAL.fullmatch(local) or len(domain) > 253 or len(labels) < 2
            or not all(_HOST_LABEL.fullmatch(label) for label in labels)
            or len(labels[-1]) < 2 or not labels[-1].isalpha()):
        return None
    if len(name) > MAX_SENDER_NAME_LENGTH or '"' in name or any(_is_control(char) for char in name):
        return None
    return address, (name or None)


def _is_credential(value: str) -> bool:
    return 1 <= len(value) <= MAX_CREDENTIAL_LENGTH and all(32 <= ord(char) <= 126 for char in value)


def _outside_checkout(path: Path) -> bool:
    resolved = path.resolve()
    return resolved != PROJECT_ROOT and PROJECT_ROOT not in resolved.parents


def _read(environ: Mapping[str, str], *, production: bool) -> tuple[MailSettings | None, list[str]]:
    backend = (environ.get(BACKEND_ENV) or "").strip().lower()
    if not backend:
        return None, []  # disabled, which is not an error
    problems: list[str] = []
    if backend not in BACKENDS:
        problems.append(f"{BACKEND_ENV} must be one of: {', '.join(BACKENDS)}")
    elif production and backend in DEVELOPMENT_BACKENDS:
        problems.append(
            f"{BACKEND_ENV} must be {BACKEND_SMTP} in production "
            f"(the {' and '.join(DEVELOPMENT_BACKENDS)} backends show the message in clear)")

    sender = None
    if not environ.get(FROM_ENV):
        problems.append(f"{FROM_ENV} is not set (there is no default sender address)")
    else:
        sender = _parse_sender(environ[FROM_ENV])
        if sender is None:
            problems.append(f"{FROM_ENV} must be an address or 'Name <address>'")

    hourly_cap = MAX_HOURLY_CAP
    raw_cap = (environ.get(HOURLY_CAP_ENV) or "").strip()
    if raw_cap:
        if not (raw_cap.isascii() and raw_cap.isdigit() and len(raw_cap) <= 4 and 1 <= int(raw_cap) <= MAX_HOURLY_CAP):
            problems.append(f"{HOURLY_CAP_ENV} must be a whole number from 1 to {MAX_HOURLY_CAP}")
        else:
            hourly_cap = int(raw_cap)

    # The SMTP values are checked whenever they are set, and required for the smtp backend.
    smtp = backend == BACKEND_SMTP
    host = (environ.get(SMTP_HOST_ENV) or "").strip() or None
    if host is None:
        if smtp:
            problems.append(f"{SMTP_HOST_ENV} is not set")
    elif not _is_host(host):
        problems.append(f"{SMTP_HOST_ENV} must be a host name or an IP address, without scheme or port")
        host = None

    port = None
    raw_port = (environ.get(SMTP_PORT_ENV) or "").strip()
    if not raw_port:
        if smtp:
            problems.append(f"{SMTP_PORT_ENV} is not set")
    elif not (raw_port.isascii() and raw_port.isdigit() and len(raw_port) <= 5 and 1 <= int(raw_port) <= 65535):
        problems.append(f"{SMTP_PORT_ENV} must be a port number from 1 to 65535")
    else:
        port = int(raw_port)

    security = (environ.get(SMTP_SECURITY_ENV) or "").strip().lower() or None
    if security is None:
        if smtp:
            problems.append(f"{SMTP_SECURITY_ENV} is not set")
    elif security not in SECURITY_MODES:
        problems.append(f"{SMTP_SECURITY_ENV} must be one of: {', '.join(SECURITY_MODES)}")
        security = None
    elif security == "none" and host is not None and not _is_loopback_host(host):
        problems.append(
            f"{SMTP_SECURITY_ENV} may be none only when {SMTP_HOST_ENV} is a loopback host "
            "(localhost, 127.0.0.0/8 or ::1)")

    username = environ.get(SMTP_USERNAME_ENV) or None
    password = environ.get(SMTP_PASSWORD_ENV) or None
    if (username is None) != (password is None):
        problems.append(f"{SMTP_USERNAME_ENV} and {SMTP_PASSWORD_ENV} must be set together or not at all")
    for name, value in ((SMTP_USERNAME_ENV, username), (SMTP_PASSWORD_ENV, password)):
        if value is not None and not _is_credential(value):
            problems.append(f"{name} must be 1 to {MAX_CREDENTIAL_LENGTH} printable ASCII characters")

    spool_dir = environ.get(SPOOL_DIR_ENV) or None
    if spool_dir is None:
        if backend == BACKEND_FILE:
            problems.append(f"{SPOOL_DIR_ENV} is not set")
    else:
        path = Path(spool_dir)
        if not path.is_absolute():
            problems.append(f"{SPOOL_DIR_ENV} must be an absolute path")
        elif not _outside_checkout(path):
            problems.append(f"{SPOOL_DIR_ENV} must be outside the application checkout")
        elif not path.is_dir():
            problems.append(f"{SPOOL_DIR_ENV} must be an existing directory")
        elif not os.access(path, os.W_OK | os.X_OK):
            problems.append(f"{SPOOL_DIR_ENV} must be writable")

    if problems:
        return None, problems
    return MailSettings(
        backend=backend, sender_address=sender[0], sender_name=sender[1], hourly_cap=hourly_cap,
        smtp_host=host, smtp_port=port, smtp_security=security,
        smtp_username=username, smtp_password=password, spool_dir=spool_dir,
    ), []


def settings_problems(environ: Mapping[str, str], *, production: bool) -> list[str]:
    """Why the mail settings cannot be used; empty when they can, and when no backend
    is configured. Each problem names a setting and never holds a value."""
    return _read(environ, production=production)[1]


def load_settings(environ: Mapping[str, str], *, production: bool) -> MailSettings | None:
    """The usable settings, or ``None``: no backend configured, or any problem at all
    (a missing or malformed sender address included). ``None`` means mail is disabled."""
    return _read(environ, production=production)[0]


def build_mailer(settings: MailSettings, *, production: bool) -> Mailer:
    """The backend the settings name. A development backend is refused for production
    here as well as by ``settings_problems``."""
    if settings.backend == BACKEND_SMTP:
        return SmtpMailer(settings)
    if settings.backend in DEVELOPMENT_BACKENDS and production:
        raise MailConfigurationError(f"{BACKEND_ENV}: the {settings.backend} backend is development-only")
    if settings.backend == BACKEND_CONSOLE:
        return ConsoleMailer(settings)
    if settings.backend == BACKEND_FILE:
        return FileMailer(settings)
    raise MailConfigurationError(f"{BACKEND_ENV}: unknown backend")


# ----------------------------------------------------------- development and test backends


def _rendered(settings: MailSettings, message: OutboundEmail) -> str:
    sender = f"{settings.sender_name} <{settings.sender_address}>" if settings.sender_name else settings.sender_address
    return f"From: {sender}\nTo: {message.to}\nSubject: {message.subject}\n\n{message.text}\n"


class ConsoleMailer:
    """Development only: writes the message, code included, to the terminal."""

    def __init__(self, settings: MailSettings, stream=None):
        if settings.backend != BACKEND_CONSOLE:
            raise MailConfigurationError("ConsoleMailer needs the console backend")
        self._settings = settings
        self._stream = stream

    def send(self, message: OutboundEmail) -> SendOutcome:
        stream = self._stream if self._stream is not None else sys.stderr
        try:
            stream.write(
                "---- LearnPilot mail (console backend, development only) ----\n"
                + _rendered(self._settings, message) + "----\n")
            stream.flush()
        except Exception as exc:
            logger.warning("sender_error error=%s", type(exc).__name__)
            return SendOutcome.NOT_SENT  # no server is involved
        return SendOutcome.ACCEPTED


class FileMailer:
    """Development only: writes each message, code included, to a new file in the
    spool directory (outside the checkout, mode 0600)."""

    def __init__(self, settings: MailSettings):
        if settings.backend != BACKEND_FILE or not settings.spool_dir:
            raise MailConfigurationError("FileMailer needs the file backend and a spool directory")
        self._settings = settings

    def send(self, message: OutboundEmail) -> SendOutcome:
        name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(8) + ".txt"
        try:
            descriptor = os.open(
                os.path.join(self._settings.spool_dir, name), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(_rendered(self._settings, message))
        except Exception as exc:
            logger.warning("sender_error error=%s", type(exc).__name__)
            return SendOutcome.NOT_SENT  # no server is involved
        return SendOutcome.ACCEPTED


class InMemoryMailer:
    """Tests: records every message and sends nothing.

    ``outcome`` is a ``SendOutcome`` or a callable taking the message and returning one
    (per message kind, or raising, as a test needs). ``delay`` is a number of seconds or
    a callable taking the message; it is handed to ``sleep`` (a test's clock), which by
    default does nothing.
    """

    def __init__(self, outcome: Union[SendOutcome, Callable[[OutboundEmail], SendOutcome]] = SendOutcome.ACCEPTED,
                 delay: Union[float, Callable[[OutboundEmail], float]] = 0.0,
                 sleep: Callable[[float], None] | None = None):
        self.outcome = outcome
        self.delay = delay
        self.sleep = sleep
        self.messages: list[OutboundEmail] = []

    def send(self, message: OutboundEmail) -> SendOutcome:
        self.messages.append(message)
        delay = self.delay(message) if callable(self.delay) else self.delay
        if delay and self.sleep is not None:
            self.sleep(delay)
        return self.outcome(message) if callable(self.outcome) else self.outcome


# ------------------------------------------------------------------ the sender process

# Senders this process killed and could not reap within REAP_SECONDS.
_unreaped: list[subprocess.Popen] = []
_unreaped_lock = threading.Lock()


def sender_busy() -> bool:
    """True while this process still has a sender it could not reap.

    Read it before anything is admitted (design 3.5): while it is true no send is
    tried, so no attempt may be charged. It polls the children it knows, never blocks,
    and becomes false once they are gone.
    """
    with _unreaped_lock:
        _unreaped[:] = [proc for proc in _unreaped if proc.poll() is None]
        return bool(_unreaped)


def _child_environment(environ: Mapping[str, str]) -> dict[str, str]:
    return {name: environ[name] for name in CHILD_ENVIRONMENT_NAMES if environ.get(name)}


def _payload(settings: MailSettings, message: OutboundEmail) -> bytes:
    return json.dumps({
        "host": settings.smtp_host, "port": settings.smtp_port, "security": settings.smtp_security,
        "username": settings.smtp_username, "password": settings.smtp_password,
        "sender_address": settings.sender_address, "sender_name": settings.sender_name,
        "to": message.to, "subject": message.subject, "text": message.text,
    }, ensure_ascii=False).encode("utf-8")


def _outcome(output: bytes, end_of_file: bool, returncode: int) -> SendOutcome:
    """The outcome of an exchange that ended by itself, inside the deadline."""
    if not end_of_file:
        return SendOutcome.UNKNOWN
    if output == b"" and returncode in EXIT_NOT_SENT:
        return SendOutcome.NOT_SENT
    if output == MARKER and returncode == EXIT_ACCEPTED:
        return SendOutcome.ACCEPTED
    if output == MARKER and returncode == EXIT_REFUSED:
        return SendOutcome.REFUSED
    return SendOutcome.UNKNOWN


def _exchange(proc: subprocess.Popen, payload: bytes) -> SendOutcome:
    """Write the input and read the output without blocking, under one deadline.

    Returns ``UNKNOWN`` as soon as the deadline passes; the caller then kills the
    child. At most ``MAX_OUTPUT_BYTES`` are ever read, and nothing is read after the
    cap or after end-of-file.
    """
    deadline = time.monotonic() + COMMUNICATION_DEADLINE
    stdin_fd, stdout_fd = proc.stdin.fileno(), proc.stdout.fileno()
    os.set_blocking(stdin_fd, False)
    os.set_blocking(stdout_fd, False)
    pending = memoryview(payload)
    output = bytearray()
    end_of_file = False
    selector = selectors.DefaultSelector()
    try:
        selector.register(stdin_fd, selectors.EVENT_WRITE)
        selector.register(stdout_fd, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return SendOutcome.UNKNOWN
            for key, _events in selector.select(remaining):
                if key.fd == stdin_fd:
                    try:
                        pending = pending[os.write(stdin_fd, pending):]
                    except BlockingIOError:
                        continue
                    if not pending:
                        selector.unregister(stdin_fd)
                        proc.stdin.close()
                else:
                    try:
                        chunk = os.read(stdout_fd, MAX_OUTPUT_BYTES - len(output))
                    except BlockingIOError:
                        continue
                    output += chunk
                    if not chunk:
                        end_of_file = True
                    if end_of_file or len(output) >= MAX_OUTPUT_BYTES:
                        selector.unregister(stdout_fd)
        try:
            returncode = proc.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            return SendOutcome.UNKNOWN
    finally:
        selector.close()
    return _outcome(bytes(output), end_of_file, returncode)


def _end(proc: subprocess.Popen) -> None:
    """Leave no running child and no open pipe behind, whatever happened before.

    A child that has not ended is killed and waited for within ``REAP_SECONDS``. One
    that is still not reaped is remembered, so that ``sender_busy`` reports it.
    Nothing is read from the output here.
    """
    try:
        if proc.returncode is None:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=REAP_SECONDS)
            except subprocess.TimeoutExpired:
                pass
    finally:
        for pipe in (proc.stdin, proc.stdout):
            try:
                pipe.close()
            except OSError:
                pass
        if proc.returncode is None:
            with _unreaped_lock:
                _unreaped.append(proc)
            logger.warning("sender_unreaped")


class SmtpMailer:
    """Sends through the isolated sender process. One call, at most one process."""

    def __init__(self, settings: MailSettings):
        if (settings.backend != BACKEND_SMTP or not settings.smtp_host or not settings.smtp_port
                or settings.smtp_security not in SECURITY_MODES):
            raise MailConfigurationError("SmtpMailer needs complete smtp settings")
        self._settings = settings

    def send(self, message: OutboundEmail) -> SendOutcome:
        if not isinstance(message, OutboundEmail):
            raise TypeError("send takes an OutboundEmail")
        # Each early return below is positive evidence: no process was created.
        if sender_busy():
            return SendOutcome.NOT_SENT
        # With SIGCHLD ignored the kernel reaps the child itself and the interpreter
        # reports an exit status of 0 it never saw, which would read as "accepted".
        # The disposition is only read, never changed. Callers must not change it
        # while a send is running.
        if signal.getsignal(signal.SIGCHLD) == signal.SIG_IGN:
            logger.warning("sender_error error=sigchld")
            return SendOutcome.NOT_SENT
        try:
            payload = _payload(self._settings, message)
        except Exception as exc:
            logger.warning("sender_error error=%s", type(exc).__name__)
            return SendOutcome.NOT_SENT
        if (len(payload) > MAX_INPUT_BYTES or not os.path.isabs(sys.executable or "")
                or not os.path.isabs(SENDER_SCRIPT)):
            logger.warning("sender_error error=input")
            return SendOutcome.NOT_SENT

        proc = None
        outcome = SendOutcome.UNKNOWN
        try:
            try:
                proc = subprocess.Popen(
                    [sys.executable, "-I", "-S", SENDER_SCRIPT],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    env=_child_environment(os.environ), close_fds=True, shell=False, bufsize=0,
                )
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                logger.warning("sender_error error=%s", type(exc).__name__)
                return SendOutcome.NOT_SENT
            outcome = _exchange(proc, payload)
        except Exception as exc:
            # A broken input pipe included: the child existed, so nothing is established.
            logger.warning("sender_error error=%s", type(exc).__name__)
            outcome = SendOutcome.UNKNOWN
        finally:
            if proc is not None:
                _end(proc)
                if proc.returncode is None:
                    outcome = SendOutcome.UNKNOWN
        return outcome
