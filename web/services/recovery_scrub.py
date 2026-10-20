"""Password recovery operations (8.1), standard library only.

Logical retention only: NULL/deletion does not erase free pages, unused page
space, WAL, earlier snapshots or copies below the application. Callers own the
transaction and, for cleanup, the writer lock and clock check. No issuer lives
here. A future column must update this allow-list together with its scrub.
"""

import sqlite3
from dataclasses import dataclass
from datetime import timedelta

from web.services.registration_scrub import stamp

MIGRATION = "025_password_recoveries.sql"
SCRUBBED_COLUMNS = ("email", "user_id", "session_key_digest", "code_hash", "code_expires_at")
KNOWN_COLUMNS = frozenset((
    "id", "binding_hash", "state", "is_shadow", *SCRUBBED_COLUMNS,
    "code_send_id", "code_attempts", "send_count", "resend_after",
    "created_at", "expires_at", "closed_at",
))
SCRUB_ASSIGNMENTS = ", ".join(f"{column} = NULL" for column in SCRUBBED_COLUMNS)


class UnsupportedRecoverySchema(Exception):
    """Recovery storage cannot be scrubbed by this build."""


def has_recovery_storage(conn: sqlite3.Connection) -> bool:
    """Accept historical absence only; reject missing or unknown columns.

    table_xinfo includes generated/hidden columns, which must not escape the
    allow-list. A view with the table's name is not supported storage either.
    Errors never interpolate database values or unknown column names.
    """
    try:
        obj = conn.execute("SELECT type FROM sqlite_master WHERE name = 'password_recoveries'").fetchone()
        if obj is None:
            bookkeeping = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
            ).fetchone()
            if bookkeeping and conn.execute(
                "SELECT 1 FROM schema_migrations WHERE filename = ?", (MIGRATION,)
            ).fetchone():
                raise UnsupportedRecoverySchema("password_recoveries missing despite recorded migration")
            return False
        columns = {row[1] for row in conn.execute("PRAGMA table_xinfo(password_recoveries)")}
        if obj[0] != "table" or columns != KNOWN_COLUMNS:
            raise UnsupportedRecoverySchema("password_recoveries has unsupported columns or object type")
        return True
    except sqlite3.Error as exc:
        raise UnsupportedRecoverySchema("password_recoveries schema cannot be validated") from exc


def _require_transaction(conn):
    if not conn.in_transaction:
        raise RuntimeError("recovery scrub requires the caller's transaction")


def void_pending(conn, now) -> int:
    """One statement; caller commits or rolls back the entire operation."""
    _require_transaction(conn)
    if not has_recovery_storage(conn):
        return 0
    return conn.execute(
        f"UPDATE password_recoveries SET state = 'void', closed_at = ?, {SCRUB_ASSIGNMENTS} "
        "WHERE state = 'pending'", (stamp(now),),
    ).rowcount


@dataclass(frozen=True)
class CleanupResult:
    scrubbed: int = 0
    deleted: int = 0


def cleanup(conn, now) -> CleanupResult:
    """Expire at 30 minutes; delete final rows at 24 hours since creation.

    Caller must first run registration.cleanup with this same lock-time clock,
    and refuse clock_behind. That call also raises the shared high-water mark
    and deletes both purposes' ledger rows at 48 hours. Holds do not block it.
    """
    _require_transaction(conn)
    if not has_recovery_storage(conn):
        return CleanupResult()
    scrubbed = conn.execute(
        f"UPDATE password_recoveries SET state = 'expired', closed_at = ?, {SCRUB_ASSIGNMENTS} "
        "WHERE state = 'pending' AND (expires_at <= ? OR created_at > ?)",
        (stamp(now), stamp(now), stamp(now + timedelta(seconds=300))),
    ).rowcount
    deleted = conn.execute(
        "DELETE FROM password_recoveries WHERE state != 'pending' AND created_at <= ?",
        (stamp(now - timedelta(hours=24)),),
    ).rowcount
    return CleanupResult(scrubbed, deleted)


def pending_count(conn, *, older_than=None) -> int:
    if not has_recovery_storage(conn):
        return 0
    predicate = " AND created_at < ?" if older_than is not None else ""
    return conn.execute(
        "SELECT COUNT(*) FROM password_recoveries WHERE state = 'pending'" + predicate,
        (stamp(older_than),) if older_than is not None else (),
    ).fetchone()[0]
