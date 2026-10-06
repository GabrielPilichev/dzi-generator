"""Local account storage and credential rules (Accounts PR 1).

Independent of Flask, ``web.app`` and the blueprints. Every function takes an open
``sqlite3`` connection and never commits or rolls back: the caller owns the
transaction. Passwords and hashes are never logged or returned by lookups.
"""

from __future__ import annotations

import secrets
import sqlite3
import unicodedata

from werkzeug.security import check_password_hash, generate_password_hash

ROLES = ("student", "teacher", "admin")
PASSWORD_METHOD = "scrypt"
MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 256
MAX_EMAIL_LENGTH = 254
MIN_DISPLAY_NAME_LENGTH = 2
MAX_DISPLAY_NAME_LENGTH = 80

# Never includes password_hash; ``has_password`` tells whether a credential exists.
_USER_COLUMNS = (
    "id, email, display_name, role, is_active, session_key, "
    "password_hash IS NOT NULL AS has_password, "
    "password_changed_at, last_login_at, created_at, updated_at"
)


def _fold(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().lower()


def normalize_email(value: str) -> str:
    """Return the stored form of an email or raise ``ValueError``.

    NFKC, trim, lower-case only: no dot removal and no plus-tag stripping.
    """
    if not isinstance(value, str):
        raise ValueError("Невалиден имейл.")
    email = _fold(value)
    if (
        not email
        or len(email) > MAX_EMAIL_LENGTH
        or email.count("@") != 1
        or any(ch.isspace() or unicodedata.category(ch).startswith("C") for ch in email)
    ):
        raise ValueError("Невалиден имейл.")
    local, domain = email.split("@")
    if not local or not domain:
        raise ValueError("Невалиден имейл.")
    return email


def password_problem(password: str, email: str | None = None) -> str | None:
    """Return a Bulgarian problem description, or ``None`` when the password is acceptable."""
    if not isinstance(password, str):
        return "Невалидна парола."
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Паролата трябва да е поне {MIN_PASSWORD_LENGTH} символа."
    if len(password) > MAX_PASSWORD_LENGTH:
        return f"Паролата трябва да е най-много {MAX_PASSWORD_LENGTH} символа."
    if email is not None and _fold(password) == _fold(email):
        return "Паролата не може да съвпада с имейла."
    return None


def _new_session_key() -> str:
    return secrets.token_urlsafe(32)


def _hash_password(password: str, email: str | None) -> str:
    problem = password_problem(password, email)
    if problem:
        raise ValueError(problem)
    return generate_password_hash(password, method=PASSWORD_METHOD)


def _validate_role(role: str) -> str:
    if role not in ROLES:
        raise ValueError(f"Невалидна роля: {role!r}.")
    return role


def _validate_display_name(display_name: str) -> str:
    name = display_name.strip() if isinstance(display_name, str) else ""
    if not MIN_DISPLAY_NAME_LENGTH <= len(name) <= MAX_DISPLAY_NAME_LENGTH:
        raise ValueError(
            f"Името трябва да е между {MIN_DISPLAY_NAME_LENGTH} и {MAX_DISPLAY_NAME_LENGTH} символа."
        )
    return name


def _fetch_user(conn: sqlite3.Connection, where: str, value) -> dict | None:
    cursor = conn.execute(f"SELECT {_USER_COLUMNS} FROM users WHERE {where} = ?", (value,))
    row = cursor.fetchone()
    if row is None:
        return None
    return dict(zip([col[0] for col in cursor.description], tuple(row)))


def get_user_by_id(conn: sqlite3.Connection, user_id: int) -> dict | None:
    return _fetch_user(conn, "id", user_id)


def get_user_by_email(conn: sqlite3.Connection, email: str) -> dict | None:
    """Look up by email, normalizing first; a malformed email simply finds nobody."""
    try:
        normalized = normalize_email(email)
    except ValueError:
        return None
    return _fetch_user(conn, "email", normalized)


def create_user(
    conn: sqlite3.Connection,
    *,
    display_name: str,
    email: str | None = None,
    password: str | None = None,
    role: str = "student",
    is_active: bool = True,
) -> int:
    """Insert a user and return its id. Duplicate emails raise ``sqlite3.IntegrityError``."""
    name = _validate_display_name(display_name)
    _validate_role(role)
    normalized = normalize_email(email) if email is not None else None
    password_hash = None
    if password is not None:
        if normalized is None:
            raise ValueError("Парола изисква имейл.")
        password_hash = _hash_password(password, normalized)
    cursor = conn.execute(
        "INSERT INTO users (email, display_name, role, password_hash, is_active, session_key, "
        "password_changed_at) VALUES (?, ?, ?, ?, ?, ?, "
        "CASE WHEN ? IS NULL THEN NULL ELSE CURRENT_TIMESTAMP END)",
        (normalized, name, role, password_hash, 1 if is_active else 0, _new_session_key(), password_hash),
    )
    return cursor.lastrowid


def verify_password(conn: sqlite3.Connection, user_id: int, password: str) -> bool:
    """True only for an existing user with a password hash that matches."""
    row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None or not row[0] or not isinstance(password, str):
        return False
    return check_password_hash(row[0], password)


def _update_user(conn: sqlite3.Connection, user_id: int, assignments: str, params: tuple) -> str:
    """Apply ``assignments`` (may be empty) plus a fresh session key; return that key."""
    session_key = _new_session_key()
    prefix = f"{assignments}, " if assignments else ""
    cursor = conn.execute(
        f"UPDATE users SET {prefix}session_key = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (*params, session_key, user_id),
    )
    if cursor.rowcount != 1:
        raise LookupError(f"Няма потребител с id {user_id}.")
    return session_key


def set_password(conn: sqlite3.Connection, user_id: int, password: str) -> str:
    """Set a new password and rotate the session key; return the new key."""
    row = conn.execute("SELECT email FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        raise LookupError(f"Няма потребител с id {user_id}.")
    if row[0] is None:
        raise ValueError("Парола изисква имейл.")
    password_hash = _hash_password(password, row[0])
    return _update_user(
        conn, user_id, "password_hash = ?, password_changed_at = CURRENT_TIMESTAMP", (password_hash,)
    )


def set_role(conn: sqlite3.Connection, user_id: int, role: str) -> str:
    """Change the role and rotate the session key; return the new key."""
    return _update_user(conn, user_id, "role = ?", (_validate_role(role),))


def set_active(conn: sqlite3.Connection, user_id: int, active: bool) -> str:
    """Enable or disable the account and rotate the session key; return the new key."""
    if not isinstance(active, (bool, int)) or active not in (0, 1):
        raise ValueError("active трябва да е булева стойност.")
    return _update_user(conn, user_id, "is_active = ?", (1 if active else 0,))


def rotate_session_key(conn: sqlite3.Connection, user_id: int) -> str:
    """Invalidate existing sessions; return the new key."""
    return _update_user(conn, user_id, "", ())
