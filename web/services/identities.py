"""External sign-in identity storage (docs/ACCOUNTS_DESIGN.md, sections 5 and 13).

Storage only: no route, sign-in flow or other module uses it yet. Token verification,
linking policy, account creation and sessions belong to the later OIDC integration.

Independent of Flask, ``web.app`` and the blueprints. Every function takes an open
``sqlite3`` connection and never commits or rolls back: the caller owns the
transaction. A write attempt may begin one implicitly: ``sqlite3`` opens a
transaction before an INSERT or UPDATE, and it stays open when that statement fails
or changes no row (:class:`IdentityConflict`, an unknown identity id). The caller
finishes it, by committing or rolling back, after a failure as after a success.

* An identity is the exact pair ``(issuer, subject)``. Both are stored and compared
  as given, byte for byte: never lower-cased, trimmed or normalized. A malformed key
  is refused, never repaired. Well-formed is narrower than OpenID Connect requires:
  see the LOCAL restrictions at ``MAX_ISSUER_LENGTH``.
* One identity belongs to one user for good. Nothing here updates ``user_id``,
  replaces a row or deletes one.
* ``email_at_link`` is metadata. No function selects, matches or merges by it.

Issuers, subjects and emails are never logged or put into exception messages.
"""

from __future__ import annotations

import sqlite3

# The providers migration 019 allows (its CHECK constraint).
PROVIDERS = ("google", "microsoft")
# OpenID Connect Core limits ``sub`` to 255 ASCII characters: that is the subject's
# bound. It sets no length for ``iss`` (an https URL), so 255 for the issuer is a
# LOCAL defensive restriction. So is accepting only visible ASCII in either key
# (``_is_identity_key``): the specification's ASCII for ``sub`` includes the space and
# the control characters, which are refused here.
MAX_ISSUER_LENGTH = 255
MAX_SUBJECT_LENGTH = 255
# The bound of users.email. email_at_link is not an account email and is never one.
MAX_EMAIL_AT_LINK_LENGTH = 254

_IDENTITY_COLUMNS = "id, user_id, issuer, subject, provider, email_at_link, created_at, last_login_at"


class IdentityConflict(Exception):
    """The ``(issuer, subject)`` pair is already stored, for this user or another one.

    Nothing was written and the stored row is unchanged. The helper neither commits
    nor rolls back, so what the caller wrote earlier in its transaction is preserved.
    If no transaction was open, the refused INSERT may have begun one. Either way the
    caller finishes it: by committing, which keeps the earlier writes, or by rolling
    back.
    """


def _is_row_id(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value < 2**63


def _is_identity_key(value, max_length: int) -> bool:
    """A non-empty string of visible ASCII characters (no space, no control character).

    Visible ASCII only is a LOCAL defensive restriction, not a rule of OpenID Connect.
    """
    return (
        isinstance(value, str)
        and 0 < len(value) <= max_length
        and all("!" <= ch <= "~" for ch in value)
    )


def _is_email_at_link(value) -> bool:
    return value is None or (
        isinstance(value, str)
        and 0 < len(value) <= MAX_EMAIL_AT_LINK_LENGTH
        and value.isprintable()
        and not any(ch.isspace() for ch in value)
    )


def _rows(cursor: sqlite3.Cursor) -> list[dict]:
    columns = [col[0] for col in cursor.description]
    return [dict(zip(columns, tuple(row))) for row in cursor.fetchall()]


def find_identity(conn: sqlite3.Connection, issuer, subject) -> dict | None:
    """The identity stored for exactly this ``(issuer, subject)`` pair, or ``None``.

    Case-sensitive and exact. A malformed key simply finds nothing. The email plays no
    part: this is the only lookup that may lead from a provider sign-in to a user.
    """
    if not _is_identity_key(issuer, MAX_ISSUER_LENGTH) or not _is_identity_key(subject, MAX_SUBJECT_LENGTH):
        return None
    found = _rows(conn.execute(
        f"SELECT {_IDENTITY_COLUMNS} FROM user_identities WHERE issuer = ? AND subject = ?",
        (issuer, subject),
    ))
    return found[0] if found else None


def list_identities(conn: sqlite3.Connection, user_id) -> list[dict]:
    """Every identity of one user, oldest first; empty for an unknown or malformed id."""
    if not _is_row_id(user_id):
        return []
    return _rows(conn.execute(
        f"SELECT {_IDENTITY_COLUMNS} FROM user_identities WHERE user_id = ? ORDER BY id", (user_id,)
    ))


def add_identity(
    conn: sqlite3.Connection,
    user_id: int,
    *,
    issuer: str,
    subject: str,
    provider: str,
    email_at_link: str | None = None,
) -> int:
    """Store an identity for an existing user and return its id.

    ``ValueError`` for malformed input, ``LookupError`` when the user does not exist,
    :class:`IdentityConflict` when the pair is already stored. The values are written
    exactly as given. An existing row is never replaced or given to another user.
    Whether this user may be linked (an active account, a signed-in owner) is the
    caller's decision, not this function's.
    """
    if not _is_row_id(user_id):
        raise ValueError("Невалиден потребител.")
    if not _is_identity_key(issuer, MAX_ISSUER_LENGTH):
        raise ValueError("Невалиден издател (issuer).")
    if not _is_identity_key(subject, MAX_SUBJECT_LENGTH):
        raise ValueError("Невалиден субект (subject).")
    if provider not in PROVIDERS:
        raise ValueError("Невалиден доставчик.")
    if not _is_email_at_link(email_at_link):
        raise ValueError("Невалиден имейл при свързване.")
    # Checked here, not left to the foreign key: that is enforced only on a connection
    # that turned foreign_keys on.
    if conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is None:
        raise LookupError(f"Няма потребител с id {user_id}.")
    try:
        # A plain INSERT: UNIQUE (issuer, subject) decides, so there is no window
        # between a check and the write.
        cursor = conn.execute(
            "INSERT INTO user_identities (user_id, issuer, subject, provider, email_at_link) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, issuer, subject, provider, email_at_link),
        )
    except sqlite3.IntegrityError as exc:
        if find_identity(conn, issuer, subject) is not None:
            raise IdentityConflict("Тази външна самоличност вече е свързана с акаунт.") from exc
        raise
    return cursor.lastrowid


def record_identity_login(conn: sqlite3.Connection, identity_id: int) -> None:
    """Stamp ``last_login_at`` of one identity; ``LookupError`` when it does not exist.

    For an unknown id no row is changed, but the UPDATE may still have begun a
    transaction: the caller finishes it.
    """
    if not _is_row_id(identity_id):
        raise ValueError("Невалидна външна самоличност.")
    cursor = conn.execute(
        "UPDATE user_identities SET last_login_at = CURRENT_TIMESTAMP WHERE id = ?", (identity_id,)
    )
    if cursor.rowcount != 1:
        raise LookupError(f"Няма външна самоличност с id {identity_id}.")
