"""Per-account limits on creating assignments (docs/reviews/EMAIL_REGISTRATION_PR7_SCOPE.md;
registration PR 7, decision D5).

Two limits, per account:

* at most :data:`DAILY_CREATION_LIMIT` (20) creations in a rolling :data:`CREATION_WINDOW`
  (24 hours), counted in the ledger ``assignment_creations`` (migration 024);
* at most :data:`OPEN_ASSIGNMENT_LIMIT` (10) open assignments, counted in ``quiz_assignments``
  itself, so that closing one, by hand or by its deadline, frees the slot.

They are limits of an *account*. They are not a limit of a person: someone with several
addresses has several allowances.

**The decision is made where the insert is made.** ``quiz_create_assignment`` calls
:func:`enforce` and :func:`record` inside the same lifecycle writer transaction as its
``INSERT``, so two requests cannot both take the last slot: the second waits for the writer
lock and then counts the first one's committed row. ``now`` is the transaction's own clock,
sampled after the lock was taken. A refusal raises :class:`QuotaExceeded` before anything is
written; the ledger row is written after the assignment and commits or rolls back with it.

**Closing does not refund a creation.** The ledger row is not linked to the assignment's
state, and its ``assignment_id`` has no foreign key, so neither closing, expiry nor deleting
the assignment row changes it. Nothing in the application updates or deletes a ledger row
(a test pins that this module is the only writer).

Admin accounts are recorded and not refused (scope, I6): the limits are "per teacher", no
admin limit was approved, and exempting is not silent because the ledger still holds their
creations. One constant, :data:`EXEMPT_ROLES`, is the whole policy.

Independent of Flask. Never commits and never rolls back: the caller owns the transaction.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from web.services import lifecycle

DAILY_CREATION_LIMIT = 20
OPEN_ASSIGNMENT_LIMIT = 10
CREATION_WINDOW = timedelta(hours=24)

# Roles whose creations are recorded but never refused. See the scope, I6.
EXEMPT_ROLES = frozenset({"admin"})

REASON_DAILY = "quota_daily"
REASON_OPEN = "quota_open"

MESSAGES_BG = {
    REASON_DAILY: (
        f"Достигна лимита от {DAILY_CREATION_LIMIT} нови теста за 24 часа. "
        "Затварянето на тест не връща този лимит. Опитай отново по-късно."
    ),
    REASON_OPEN: (
        f"Имаш {OPEN_ASSIGNMENT_LIMIT} отворени теста, колкото е позволено. "
        "Затвори някой от тях, за да създадеш нов."
    ),
}

# 429 for a rate that has to cool down, 409 for a state the teacher can change.
STATUS_CODES = {REASON_DAILY: 429, REASON_OPEN: 409}


class QuotaExceeded(Exception):
    """A creation was refused by a limit. Nothing was written. ``reason`` selects the message."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def window_start_text(now: datetime) -> str:
    """The exclusive lower end of the rolling window ending at ``now``, in the stored layout."""
    return lifecycle.format_timestamp(now - CREATION_WINDOW)


def creations_in_window(conn: sqlite3.Connection, user_id: int, now: datetime) -> int:
    """Creations of one account in (now - 24 h, now]. A row dated later than ``now`` counts too."""
    row = conn.execute(
        "SELECT COUNT(*) FROM assignment_creations WHERE owner_user_id = ? AND created_at > ?",
        (user_id, window_start_text(now))).fetchone()
    return int(row[0])


def open_assignments(conn: sqlite3.Connection, user_id: int, now: datetime) -> int:
    """Open assignments of one account at ``now`` (a held one that is otherwise open counts)."""
    return lifecycle.count_unclosed_owned(conn, user_id, lifecycle.format_timestamp(now))


def is_exempt(conn: sqlite3.Connection, user_id: int) -> bool:
    """Whether the account's role *now* (read under the caller's lock) is never refused."""
    row = conn.execute("SELECT role FROM users WHERE id = ?", (user_id,)).fetchone()
    return row is not None and row[0] in EXEMPT_ROLES


def enforce(conn: sqlite3.Connection, user_id: int, now: datetime) -> None:
    """Raise :class:`QuotaExceeded` if one more creation by this account is not allowed at ``now``.

    The daily limit is checked first: when both are used up, closing a test would not help
    (scope, I3). Reads only. Call it inside the writer transaction that will insert.
    """
    if is_exempt(conn, user_id):
        return
    if creations_in_window(conn, user_id, now) >= DAILY_CREATION_LIMIT:
        raise QuotaExceeded(REASON_DAILY)
    if open_assignments(conn, user_id, now) >= OPEN_ASSIGNMENT_LIMIT:
        raise QuotaExceeded(REASON_OPEN)


def record(conn: sqlite3.Connection, user_id: int, assignment_id: int, created_at: str) -> None:
    """Write the ledger row of a creation, in the transaction that inserted the assignment.

    ``created_at`` is the transaction's ``now_text``. The UNIQUE ``assignment_id`` makes a
    second row for the same assignment an error, which rolls the creation back.
    """
    conn.execute(
        "INSERT INTO assignment_creations (owner_user_id, assignment_id, created_at) VALUES (?, ?, ?)",
        (user_id, assignment_id, created_at))
