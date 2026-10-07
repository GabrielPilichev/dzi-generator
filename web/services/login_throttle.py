"""Per-identifier login throttle (Accounts PR 4, design §14).

Ten failures inside fifteen minutes block an identifier for fifteen minutes, whether
or not an account exists for it. Rows are keyed by ``HMAC-SHA256(SECRET_KEY,
identifier)``, so an attempted email is never stored or logged in clear.

Independent of Flask, ``web.app`` and the blueprints. Every function takes an open
``sqlite3`` connection and the current UTC time, and never commits: the caller owns
the transaction and the clock.
"""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
from datetime import datetime, timedelta, timezone

MAX_FAILURES = 10
WINDOW = timedelta(minutes=15)
BLOCK_DURATION = timedelta(minutes=15)


def identifier_hash(secret_key, identifier: str) -> str:
    """The 64-character hex key an identifier is stored under."""
    key = secret_key if isinstance(secret_key, bytes) else str(secret_key).encode("utf-8")
    return hmac.new(key, identifier.encode("utf-8", errors="surrogatepass"), hashlib.sha256).hexdigest()


def _timestamp(moment: datetime) -> str:
    # SQLite's CURRENT_TIMESTAMP layout in UTC, so stored values compare as text.
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def is_blocked(conn: sqlite3.Connection, key: str, now: datetime) -> bool:
    """One primary-key read; never writes."""
    row = conn.execute(
        "SELECT blocked_until FROM login_throttle WHERE identifier_hash = ?", (key,)
    ).fetchone()
    return row is not None and row[0] is not None and row[0] > _timestamp(now)


def record_failure(conn: sqlite3.Connection, key: str, now: datetime) -> None:
    """Count one failed attempt, first deleting every row whose window and block are over.

    A row is over once its window has elapsed and it is not (or no longer) blocking,
    which is also what restarts an identifier's own window.
    """
    current = _timestamp(now)
    conn.execute(
        "DELETE FROM login_throttle WHERE window_started_at <= ? "
        "AND (blocked_until IS NULL OR blocked_until <= ?)",
        (_timestamp(now - WINDOW), current),
    )
    conn.execute(
        "INSERT INTO login_throttle (identifier_hash, failure_count, window_started_at) "
        "VALUES (?, 1, ?) "
        "ON CONFLICT(identifier_hash) DO UPDATE SET failure_count = failure_count + 1",
        (key, current),
    )
    conn.execute(
        "UPDATE login_throttle SET blocked_until = ? "
        "WHERE identifier_hash = ? AND failure_count >= ? AND blocked_until IS NULL",
        (_timestamp(now + BLOCK_DURATION), key, MAX_FAILURES),
    )


def clear(conn: sqlite3.Connection, key: str) -> None:
    """Forget an identifier's failures (after a successful authentication)."""
    conn.execute("DELETE FROM login_throttle WHERE identifier_hash = ?", (key,))
