"""Public assignment tokens: the unguessable part of a student link.

``/quiz/s/<public_token>`` replaces the sequential ``/quiz/<assignment_id>`` as the
link a teacher shares. A token is ``secrets.token_urlsafe(32)``: 256 bits from the
operating system's CSPRNG, written as 43 unpadded URL-safe base64 characters. It
identifies an assignment and nothing else: it is not a credential, and starting an
attempt still needs a signed-in account.

Standard library only, so the application and ``src.manage_assignment_tokens`` share
one definition of what a token is.
"""

from __future__ import annotations

import re
import secrets
import sqlite3

PUBLIC_TOKEN_BYTES = 32
PUBLIC_TOKEN_LENGTH = 43
# A real collision is a 2^-256 event; the bound only keeps a broken generator from looping.
PUBLIC_TOKEN_MAX_ATTEMPTS = 5

_PUBLIC_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{%d}" % PUBLIC_TOKEN_LENGTH)


class PublicTokenError(RuntimeError):
    """No unused token was found within :data:`PUBLIC_TOKEN_MAX_ATTEMPTS`."""


def new_public_token() -> str:
    return secrets.token_urlsafe(PUBLIC_TOKEN_BYTES)


def is_valid_public_token(value: object) -> bool:
    """Whether ``value`` has exactly the shape :func:`new_public_token` produces."""
    return isinstance(value, str) and _PUBLIC_TOKEN_RE.fullmatch(value) is not None


def public_token_exists(conn: sqlite3.Connection, token: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM quiz_assignments WHERE public_token = ?", (token,)
    ).fetchone() is not None


def write_with_new_public_token(conn: sqlite3.Connection, write):
    """Run ``write(token)`` with a fresh token, retrying only on a token collision.

    ``write`` executes one INSERT or UPDATE that stores the token. An
    ``sqlite3.IntegrityError`` counts as a collision only if the token is in fact
    already stored; any other constraint failure propagates unchanged. A failed
    statement leaves the caller's transaction open and otherwise untouched, so the
    retry stays inside it and the caller still owns commit and rollback.
    """
    for _ in range(PUBLIC_TOKEN_MAX_ATTEMPTS):
        token = new_public_token()
        try:
            return write(token)
        except sqlite3.IntegrityError:
            if not public_token_exists(conn, token):
                raise
    raise PublicTokenError("could not generate an unused public assignment token")
