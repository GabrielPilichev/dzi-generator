"""The trusted public base URL (``https://<host>``) that shared links are built on.

A link a teacher hands out (the student link and the QR code that encodes it) must
not be shaped by the ``Host`` header of whichever request rendered it. The base is
therefore configuration:

* production (``web.wsgi``): always ``https://`` + ``DZI_PUBLIC_HOST``, set by
  :func:`web.production.create_production_app`. :func:`normalize_public_host` is
  the one definition of a usable ``DZI_PUBLIC_HOST``: startup validation and the
  base URL are both built on it, so a host startup accepts always yields a base;
* any other run: ``DZI_PUBLIC_BASE_URL`` when set (a tunnel or LAN address), else
  the loopback address the request itself arrived on, so a plain local run works
  with no setup. A request for any non-loopback host gets no base at all.

Standard library and Flask's request only; no database, no other web module.
"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlsplit

from flask import current_app, request

PUBLIC_BASE_URL_CONFIG = "PUBLIC_BASE_URL"
PUBLIC_BASE_URL_ENV = "DZI_PUBLIC_BASE_URL"
LOOPBACK_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1"})

# A hostname label; no dots, spaces, slashes, "@" or anything else URL-significant.
_HOSTNAME_CHARACTERS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-.")


def _base_url(scheme: str, hostname: str | None, port: int | None) -> str | None:
    """``scheme://host[:port]`` rebuilt from parsed parts, or ``None`` if unusable."""
    if scheme not in ("http", "https") or not hostname:
        return None
    if ":" in hostname:  # IPv6 literal
        if not set(hostname) <= set("0123456789abcdef:"):
            return None
        host = f"[{hostname}]"
    elif set(hostname) <= _HOSTNAME_CHARACTERS and not hostname.startswith((".", "-")):
        host = hostname
    else:
        return None
    return f"{scheme}://{host}" if port is None else f"{scheme}://{host}:{port}"


def normalize_public_base_url(value: object) -> str | None:
    """A configured base URL in canonical form, or ``None`` if it is not one.

    Accepted: ``http``/``https``, a hostname or IP literal and an optional port.
    Refused: credentials, a path, a query, a fragment, whitespace and control
    characters. The result has no trailing slash.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
        return None
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        return None
    if "@" in parts.netloc or parts.path not in ("", "/") or parts.query or parts.fragment:
        return None
    if "?" in text or "#" in text:
        return None
    return _base_url(parts.scheme.lower(), (parts.hostname or "").lower(), port)


_DNS_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_PUBLIC_HOSTNAME = re.compile(rf"{_DNS_LABEL}(?:\.{_DNS_LABEL})+")
# A last label URL parsers read as a number makes the whole host an IPv4 address
# ("127.1", "0x7f.1"), or no host at all ("999.999.999.999").
_NUMERIC_LABEL = re.compile(r"[0-9]+|0x[0-9a-f]*")
PUBLIC_HOSTNAME_MAX_LENGTH = 253


def _is_idna_a_label(label: str) -> bool:
    """Whether an ``xn--`` label is well-formed Punycode for a non-ASCII name."""
    try:
        decoded = label[4:].encode("ascii").decode("punycode")
        if decoded.encode("punycode").decode("ascii") != label[4:]:
            return False
    except (UnicodeError, ValueError):
        return False
    return not decoded.isascii() and not any(unicodedata.category(ch) == "Cc" for ch in decoded)


def normalize_public_host(value: object) -> str | None:
    """A ``DZI_PUBLIC_HOST`` in canonical (lower-case ASCII) form, or ``None``.

    Accepted: a DNS hostname of at least two labels, each 1-63 ASCII letters,
    digits and inner hyphens, at most 253 characters in all; an internationalized
    name only in its ``xn--`` form. Refused: anything not ASCII (case folding must
    not turn other letters into ASCII ones), a scheme, a port, a path, a wildcard,
    a trailing dot, a single label, an IP address or other numeric-looking host and
    a malformed ``xn--`` label.
    """
    if not isinstance(value, str):
        return None
    host = value.strip()
    if not host.isascii():
        return None
    host = host.lower()
    if len(host) > PUBLIC_HOSTNAME_MAX_LENGTH or not _PUBLIC_HOSTNAME.fullmatch(host):
        return None
    labels = host.split(".")
    if _NUMERIC_LABEL.fullmatch(labels[-1]):
        return None
    if any(label.startswith("xn--") and not _is_idna_a_label(label) for label in labels):
        return None
    return host


def public_base_url_for_host(host: object) -> str | None:
    """The production base URL (always HTTPS) of ``DZI_PUBLIC_HOST``, or ``None``
    when :func:`normalize_public_host` refuses the host."""
    public_host = normalize_public_host(host)
    return None if public_host is None else f"https://{public_host}"


def _loopback_request_base_url() -> str | None:
    """The base of the current request when it was made to this machine itself."""
    try:
        parts = urlsplit(f"//{request.host}")
        port = parts.port
    except ValueError:
        return None
    hostname = (parts.hostname or "").lower()
    if "@" in parts.netloc or parts.path or hostname not in LOOPBACK_HOSTNAMES:
        return None
    return _base_url(request.scheme, hostname, port)


def trusted_public_base_url() -> str | None:
    """The base URL shared links may be built on, or ``None`` when there is none.

    A configured value is used as it is and a malformed one yields ``None``; it is
    never replaced by request data. Only with nothing configured does a request
    made to a loopback host get that loopback address. Callers fail closed on
    ``None``.
    """
    if PUBLIC_BASE_URL_CONFIG in current_app.config:
        return normalize_public_base_url(current_app.config[PUBLIC_BASE_URL_CONFIG])
    return _loopback_request_base_url()
