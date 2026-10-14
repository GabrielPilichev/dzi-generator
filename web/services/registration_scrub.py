"""The logical scrub of a pending registration, shared by the registration service and the
snapshot step of the backup (docs/reviews/EMAIL_REGISTRATION_DESIGN.md, 6.5 and 3.9; PR 5).

Standard library only, so ``src.backup_sqlite`` can use it without importing the
application. One definition of "every column of the 6.1 CHECK" for every caller: the
cleanup, the cancel, the restore gate and the snapshot copy cannot disagree about which
columns carry a person's data or a credential.

**Logical, not physical.** A scrub sets columns to ``NULL`` in the statement that changes
the state. It does not erase: the old bytes can remain on free pages, in unused space
inside pages that are still in use, in the write-ahead log and in every earlier copy of
the file. Nothing here changes a ``secure_delete`` or checkpoint setting, rebuilds the
file or claims a bound on physical remnants (design 6.5).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

# Every personal or credential column of ``pending_registrations`` (the table CHECK of
# migration 021): NULL in any state but ``pending``.
SCRUBBED_COLUMNS = (
    "email", "first_name", "last_name", "grade", "school_name", "subjects_json",
    "password_hash", "code_hash", "code_expires_at",
)
SCRUB_ASSIGNMENTS = ", ".join(f"{column} = NULL" for column in SCRUBBED_COLUMNS)

# Columns ``void_pending`` reads and writes besides the scrubbed ones.
_REQUIRED_COLUMNS = frozenset(SCRUBBED_COLUMNS) | {"state", "closed_at"}
_STAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


def stamp(moment: datetime) -> str:
    """SQLite's CURRENT_TIMESTAMP layout in UTC, so stored values compare as text."""
    return moment.astimezone(timezone.utc).strftime(_STAMP_FORMAT)


def has_pending_storage(conn: sqlite3.Connection) -> bool:
    """Whether the database has the table of pending registrations with the columns of
    migration 021. A database from before it has nothing to scrub."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(pending_registrations)")}
    return _REQUIRED_COLUMNS <= columns


def void_pending(conn: sqlite3.Connection, now: datetime) -> int:
    """Turn every ``pending`` registration into ``void`` and scrub it, in one statement.

    Returns how many rows changed. Touches no other row and no other table: not the send
    ledger, not the control row, not an account or a profile. Needs the caller's
    transaction (it commits nothing)."""
    return conn.execute(
        f"UPDATE pending_registrations SET state = 'void', closed_at = ?, {SCRUB_ASSIGNMENTS} "
        "WHERE state = 'pending'",
        (stamp(now),),
    ).rowcount
