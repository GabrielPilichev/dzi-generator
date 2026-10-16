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

Admin accounts are recorded and not refused (approved by Gabriel): the ledger still holds their
creations, so they count if the account's role changes. One constant, :data:`EXEMPT_ROLES`,
is the whole policy.

**The ledger is checked, and repaired where it can be, at every admission.** Code that does not
know the ledger (a rollback or recovery build that kept migration 024 but not this module) inserts
owned assignments without a ledger row, and an operator can delete one. So :func:`enforce` first
looks, under the same writer lock, for the acting account's assignments that have no ledger row
(:func:`reconcile`) and records them from the ``created_at`` they carry, by the rules of
:func:`normalize_legacy_timestamp`. A row it cannot place with confidence (NULL, malformed,
ambiguous, implausible, or dated more than a day ahead), or a ledger row that disagrees with the
assignment's owner, makes the account's creation **fail closed** (``REASON_ACCOUNTING``) until an
operator corrects the data; nothing is written for that attempt. What cannot be recovered: an
assignment row that was deleted before it was ever recorded. Migration 024 itself writes no
history; the first admission of each account performs the same reconciliation, so there is one
set of rules and no second copy of them in SQL.

Independent of Flask. Never commits and never rolls back: the caller owns the transaction.
"""

from __future__ import annotations

import re
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
REASON_ACCOUNTING = "quota_accounting"

MESSAGES_BG = {
    REASON_DAILY: (
        f"Достигна лимита от {DAILY_CREATION_LIMIT} нови теста за 24 часа. "
        "Затварянето на тест не връща този лимит. Опитай отново по-късно."
    ),
    REASON_OPEN: (
        f"Имаш {OPEN_ASSIGNMENT_LIMIT} отворени теста, колкото е позволено. "
        "Затвори някой от тях, за да създадеш нов."
    ),
    REASON_ACCOUNTING: (
        "Създаването на нови тестове е временно спряно за този профил, докато се проверят "
        "данните за досегашните му тестове. Обърни се към администратор."
    ),
}

# 429 for a rate that has to cool down, 409 for a state the teacher can change.
STATUS_CODES = {REASON_DAILY: 429, REASON_OPEN: 409, REASON_ACCOUNTING: 503}

# Legacy ``created_at`` values (see normalize_legacy_timestamp).
LEGACY_FUTURE_TOLERANCE = timedelta(days=1)
LEGACY_EARLIEST = datetime(2000, 1, 1)
_LEGACY_TIMESTAMP = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})"
    r"(?:[ T](\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:?\d{2})?)?"
)


class QuotaExceeded(Exception):
    """A creation was refused by a limit. Nothing was written. ``reason`` selects the message."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def normalize_legacy_timestamp(value, now: datetime) -> str | None:
    """A legacy ``quiz_assignments.created_at`` as ``YYYY-MM-DD HH:MM:SS`` UTC, or ``None``.

    The only writer of that column was its default, ``CURRENT_TIMESTAMP``: UTC, in the
    canonical layout. The other shapes are accepted because rows were also written by hand
    and by tools, and each keeps the age it has:

    * canonical ``YYYY-MM-DD HH:MM:SS``, and the ISO form with ``T``, with optional fractional
      seconds (cut to the second);
    * an explicit offset (``Z``, ``+02:00``, ``+0200``, up to 14 hours) is converted to UTC;
    * **a time with no offset is UTC**, as the default wrote it. That is an assumption, stated;
    * **a date alone is the end of that day, 23:59:59 UTC**: the latest instant it can mean in
      UTC, so a recent date-only row is never counted as older than it is;
    * nothing else: not NULL, not an empty or padded string, not a number, not another layout,
      not an impossible calendar date or offset, not a value before 2000-01-01 and **not a value
      more than a day after** ``now`` (``LEGACY_FUTURE_TOLERANCE``, which absorbs the end-of-day
      rule and clock differences). These return ``None`` and are never treated as old.

    ``now`` is naive UTC (the lifecycle writer clock). A value up to a day ahead is kept as it
    is, so it counts in the window until it is a day old: the count is deliberately conservative
    there, and there only.
    """
    if not isinstance(value, str):
        return None
    found = _LEGACY_TIMESTAMP.fullmatch(value)
    if found is None:
        return None
    year, month, day, hour, minute, second, fraction, offset = found.groups()
    try:
        if hour is None:
            moment = datetime(int(year), int(month), int(day), 23, 59, 59)
        else:
            moment = datetime(int(year), int(month), int(day), int(hour), int(minute), int(second))
            if offset is not None and offset != "Z":
                digits = offset[1:].replace(":", "")
                hours, minutes = int(digits[:2]), int(digits[2:])
                if minutes > 59 or hours > 14 or (hours == 14 and minutes):
                    return None
                delta = timedelta(hours=hours, minutes=minutes)
                moment = moment - delta if offset[0] == "+" else moment + delta
    except (ValueError, OverflowError):
        return None
    if moment < LEGACY_EARLIEST or moment > now + LEGACY_FUTURE_TOLERANCE:
        return None
    return lifecycle.format_timestamp(moment)


def unrecorded_assignments(conn: sqlite3.Connection, user_id: int | None = None) -> list[tuple]:
    """``(id, owner_user_id, created_at)`` of owned assignments that have no ledger row."""
    where = "" if user_id is None else "AND a.owner_user_id = ? "
    return [tuple(row) for row in conn.execute(
        "SELECT a.id, a.owner_user_id, a.created_at FROM quiz_assignments a "
        "LEFT JOIN assignment_creations c ON c.assignment_id = a.id "
        "WHERE a.owner_user_id IS NOT NULL AND c.id IS NULL " + where + "ORDER BY a.id",
        () if user_id is None else (user_id,))]


def inconsistent_records(conn: sqlite3.Connection, user_id: int | None = None) -> int:
    """Ledger rows whose assignment still exists but belongs to someone else (or to nobody)."""
    where = "" if user_id is None else "AND (c.owner_user_id = ?1 OR a.owner_user_id = ?1) "
    row = conn.execute(
        "SELECT COUNT(*) FROM assignment_creations c JOIN quiz_assignments a ON a.id = c.assignment_id "
        "WHERE a.owner_user_id IS NOT c.owner_user_id " + where,
        () if user_id is None else (user_id,)).fetchone()
    return int(row[0])


def reconcile(conn: sqlite3.Connection, user_id: int, now: datetime) -> int:
    """Record the account's unrecorded assignments; ``QuotaExceeded(REASON_ACCOUNTING)`` if any cannot be placed.

    Run inside the writer transaction that will decide. Either every unrecorded assignment of
    the account is recorded with its normalized age, or nothing is written and the creation
    is refused: an account is never admitted on part of its history. Returns the number of
    rows recorded. See :func:`normalize_legacy_timestamp` for what is recovered.
    """
    if inconsistent_records(conn, user_id):
        raise QuotaExceeded(REASON_ACCOUNTING)
    pending = []
    for assignment_id, _owner, created_at in unrecorded_assignments(conn, user_id):
        normalized = normalize_legacy_timestamp(created_at, now)
        if normalized is None:
            raise QuotaExceeded(REASON_ACCOUNTING)
        pending.append((user_id, assignment_id, normalized))
    conn.executemany(
        "INSERT INTO assignment_creations (owner_user_id, assignment_id, created_at) VALUES (?, ?, ?)", pending)
    return len(pending)


def accounting_report(conn: sqlite3.Connection, now: datetime) -> dict:
    """Counts for an operator or a start-up warning; reads only. Never an id, a name or a value."""
    unrecorded = unrecorded_assignments(conn)
    unplaceable = sum(1 for _id, _owner, created_at in unrecorded
                      if normalize_legacy_timestamp(created_at, now) is None)
    return {"unrecorded": len(unrecorded), "unplaceable": unplaceable,
            "inconsistent": inconsistent_records(conn)}


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

    The ledger is reconciled first, for every account including an exempt one (an admin's
    history belongs to the account after a role change). The daily limit is checked before the
    open limit: when both are used up, closing a test would not help (scope, I3). Writes only
    the reconciliation; call it inside the writer transaction that will insert.
    """
    reconcile(conn, user_id, now)
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
