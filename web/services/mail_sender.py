"""Standalone sender process of the mail service: one message, one attempt, then exit.

NEVER IMPORT THIS FILE. Its first statements arm an alarm that ends the process
that runs them. It is started only by ``web.services.mail`` as
``[sys.executable, "-I", "-S", "<absolute path>/mail_sender.py"]``
(docs/reviews/EMAIL_REGISTRATION_DESIGN.md, section 4.2).

Standard library only; nothing from ``web`` or ``src``; no database; no file opened
other than the three standard streams. The contract with the parent:

* Standard input: one JSON document of at most 16 KiB (settings and message). At most
  16 KiB plus one byte is read. Longer, truncated or malformed input, an unknown or
  missing key, or a value that fails its check ends the process before any network call.
* Standard output: at most one byte, ``R``, written with one unbuffered ``os.write``
  before the recipient command may be transmitted. Nothing else is ever written.
* Exit status: ``0`` the server accepted the message. ``10``-``19`` ended before ``R``,
  nothing was given to the server for any recipient. ``20`` a refusal after ``R``: a
  5xx reply to the recipient command, the data command or the message, or a 4xx reply
  other than 421 to the recipient command. ``31`` the server answered 421 ("closing the
  channel") at any stage, before ``R`` or after it. Any other status has no meaning of
  its own. The parent reads ``31`` and every other unlisted status as "unknown": a 4xx
  reply to the data command or to the message, a reply that cannot be read, and a lost
  connection after ``R`` all end with ``30``.

Every socket operation has a 3-second timeout. That is not the bound: a server that
answers one line at a time, and name resolution, are bounded by the parent's deadline
and by the alarm below, which counts from the three statements that follow and not
from process creation. Nothing is logged or printed, and ``smtplib`` debug output is
never enabled.
"""

import signal

signal.signal(signal.SIGALRM, signal.SIG_DFL)
signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGALRM})
signal.alarm(9)

import ipaddress
import json
import os
import re
import smtplib
import ssl
from datetime import datetime, timezone
from email.headerregistry import Address
from email.message import EmailMessage
from email.policy import SMTP as SMTP_POLICY
from email.utils import format_datetime, make_msgid

MAX_INPUT_BYTES = 16 * 1024
SOCKET_TIMEOUT = 3.0
MARKER = b"R"

EXIT_ACCEPTED = 0
EXIT_INPUT = 10
EXIT_CONNECT = 11  # name lookup, connection, greeting
EXIT_TLS = 12
EXIT_HELLO = 13
EXIT_AUTH = 14
EXIT_SENDER_REFUSED = 15
EXIT_BEFORE_MARKER = 19  # anything else before the marker, a failed marker write included
EXIT_REFUSED = 20
EXIT_AFTER_MARKER = 30  # no meaning of its own: the parent reads it as "unknown"
# The server answered 421 at any stage. Deliberately outside 10-19: without ``R`` it must
# not become "not sent". It says nothing about whether a recipient was transmitted.
EXIT_SERVICE_UNAVAILABLE = 31
SERVICE_UNAVAILABLE = 421

SECURITY_MODES = ("starttls", "tls", "none")
KEYS = frozenset({
    "host", "port", "security", "username", "password",
    "sender_address", "sender_name", "to", "subject", "text",
})

_ADDRESS = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,253}", re.ASCII)
_HOST = re.compile(r"[A-Za-z0-9.:-]{1,253}", re.ASCII)


class _Stop(Exception):
    def __init__(self, status):
        super().__init__()
        self.status = status


def _read_input():
    """Standard input up to end-of-file; ``None`` once it exceeds the cap."""
    data = b""
    while len(data) <= MAX_INPUT_BYTES:
        chunk = os.read(0, MAX_INPUT_BYTES + 1 - len(data))
        if not chunk:
            return data
        data += chunk
    return None


def _has_control(value):
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


def _text(document, key, maximum, *, optional=False):
    value = document[key]
    if value is None and optional:
        return None
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise ValueError(key)
    return value


def _is_loopback(host):
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _parse(data):
    """The checked settings and the finished message bytes. No network is involved."""
    document = json.loads(data.decode("utf-8"))
    if not isinstance(document, dict) or set(document) != KEYS:
        raise ValueError("keys")
    host = _text(document, "host", 253)
    port = document["port"]
    security = document["security"]
    username = _text(document, "username", 255, optional=True)
    password = _text(document, "password", 255, optional=True)
    sender_address = _text(document, "sender_address", 254)
    sender_name = _text(document, "sender_name", 78, optional=True)
    recipient = _text(document, "to", 254)
    subject = _text(document, "subject", 200)
    text = _text(document, "text", 4000)

    if not _HOST.fullmatch(host):
        raise ValueError("host")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("port")
    if security not in SECURITY_MODES:
        raise ValueError("security")
    if security == "none" and not _is_loopback(host):
        raise ValueError("security")
    if (username is None) != (password is None):
        raise ValueError("credentials")
    for value in (username, password, sender_name, subject):
        if value is not None and _has_control(value):
            raise ValueError("control character")
    if any(_has_control(char) and char != "\n" for char in text):
        raise ValueError("control character")
    if not _ADDRESS.fullmatch(sender_address) or not _ADDRESS.fullmatch(recipient):
        raise ValueError("address")

    message = EmailMessage(policy=SMTP_POLICY)
    local, _, domain = sender_address.partition("@")
    message["From"] = Address(display_name=sender_name or "", username=local, domain=domain)
    message["To"] = recipient
    message["Subject"] = subject
    message["Date"] = format_datetime(datetime.now(timezone.utc))
    message["Message-ID"] = make_msgid(domain=domain)
    message.set_content(text, subtype="plain", charset="utf-8", cte="base64")
    settings = {
        "host": host, "port": port, "security": security,
        "username": username, "password": password,
        "sender_address": sender_address, "to": recipient,
    }
    return settings, message.as_bytes()


def _tls_context():
    """Certificate and host name verification, as ``ssl.create_default_context`` sets them."""
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def _accepted(code):
    return 200 <= code <= 299


def _permanent(code):
    return 500 <= code <= 599


def _transient(code):
    return 400 <= code <= 499


def _stop(default, code=None, error=None):
    """The stop for a failure before the marker: ``default``, unless the server said 421."""
    if code is None and error is not None:
        code = getattr(error, "smtp_code", None)
    return _Stop(EXIT_SERVICE_UNAVAILABLE if code == SERVICE_UNAVAILABLE else default)


def _before_marker(settings):
    """Connect, greet, secure, authenticate and name the sender. Returns the connection."""
    host, port, security = settings["host"], settings["port"], settings["security"]
    try:
        if security == "tls":
            server = smtplib.SMTP_SSL(host, port, timeout=SOCKET_TIMEOUT, context=_tls_context())
        else:
            server = smtplib.SMTP(host, port, timeout=SOCKET_TIMEOUT)
    except ssl.SSLError:
        raise _Stop(EXIT_TLS)
    except Exception as error:
        raise _stop(EXIT_CONNECT, error=error)
    try:
        try:
            code, _ = server.ehlo()
        except Exception as error:
            raise _stop(EXIT_HELLO, error=error)
        if not _accepted(code):
            raise _stop(EXIT_HELLO, code)
        if security == "starttls":
            try:
                # Raises when the extension is not offered: there is no plain fallback.
                server.starttls(context=_tls_context())
                code, _ = server.ehlo()
            except Exception as error:
                raise _stop(EXIT_TLS, error=error)
            if not _accepted(code):
                raise _stop(EXIT_HELLO, code)
        if settings["username"] is not None:
            try:
                server.login(settings["username"], settings["password"])
            except Exception as error:
                raise _stop(EXIT_AUTH, error=error)
        try:
            # No options: nothing given to the server before the marker depends on
            # the recipient or on the message (no SIZE in particular).
            code, _ = server.mail(settings["sender_address"])
        except Exception as error:
            raise _stop(EXIT_SENDER_REFUSED, error=error)
        if not _accepted(code):
            raise _stop(EXIT_SENDER_REFUSED, code)
    except BaseException:
        _close(server)
        raise
    return server


def _close(server):
    try:
        server.close()
    except Exception:
        pass


def _recipient_status(code):
    """A refusal is a 5xx reply, or a 4xx reply other than 421."""
    if code == SERVICE_UNAVAILABLE:
        return EXIT_SERVICE_UNAVAILABLE
    return EXIT_REFUSED if _permanent(code) or _transient(code) else EXIT_AFTER_MARKER


def _data_status(code):
    """A refusal is a 5xx reply only: a 4xx reply to the data or the message establishes nothing."""
    if code == SERVICE_UNAVAILABLE:
        return EXIT_SERVICE_UNAVAILABLE
    return EXIT_REFUSED if _permanent(code) else EXIT_AFTER_MARKER


def _after_marker(server, settings, message):
    """Recipient, data and message, under the reply policy of the two functions above."""
    try:
        code, _ = server.rcpt(settings["to"])
        if not _accepted(code):
            return _recipient_status(code)
        code, _ = server.data(message)
        if not _accepted(code):
            return _data_status(code)
    except smtplib.SMTPDataError as error:
        return _data_status(error.smtp_code)
    except Exception:
        return EXIT_AFTER_MARKER
    try:
        server.quit()
    except Exception:
        pass
    return EXIT_ACCEPTED


def run(data):
    """The exit status for one input document (``None`` when it was too long)."""
    if data is None:
        return EXIT_INPUT
    try:
        settings, message = _parse(data)
    except Exception:
        return EXIT_INPUT
    try:
        server = _before_marker(settings)
    except _Stop as stop:
        return stop.status
    except Exception:
        return EXIT_BEFORE_MARKER
    try:
        try:
            written = os.write(1, MARKER)
        except Exception:
            written = 0
        if written != len(MARKER):
            return EXIT_BEFORE_MARKER
        # From here on the recipient may be on the wire.
        try:
            return _after_marker(server, settings, message)
        except Exception:
            return EXIT_AFTER_MARKER
    finally:
        _close(server)


def main():
    try:
        status = run(_read_input())
    except BaseException:
        # Reached only for a failure of the input read or of the wrappers above;
        # the status is deliberately one the parent gives no meaning to.
        status = EXIT_AFTER_MARKER
    os._exit(status)


if __name__ == "__main__":
    main()
