"""Local account storage and credential rules (Accounts PR 1).

Independent of Flask, ``web.app`` and the blueprints. Every function takes an open
``sqlite3`` connection and never commits or rolls back: the caller owns the
transaction. Passwords and hashes are never logged or returned by lookups.
"""

from __future__ import annotations

import hmac
import secrets
import sqlite3
import unicodedata

from werkzeug.security import check_password_hash, generate_password_hash

ROLES = ("student", "teacher", "admin")
PASSWORD_METHOD = "scrypt"
MIN_PASSWORD_LENGTH = 10
ADMIN_MIN_PASSWORD_LENGTH = 14
MAX_PASSWORD_LENGTH = 256
MAX_EMAIL_LENGTH = 254
MIN_DISPLAY_NAME_LENGTH = 2
MAX_DISPLAY_NAME_LENGTH = 80

# Absolute account-session lifetime, measured from the session's ``iat`` (design §6).
SESSION_LIFETIME_SECONDS = {
    "student": 30 * 24 * 3600,
    "teacher": 30 * 24 * 3600,
    "admin": 12 * 3600,
}
# An ``iat`` further in the future than this is not a session this server issued now.
SESSION_CLOCK_SKEW_SECONDS = 300

# Verified instead of a real hash when no account or no password exists, so an unknown
# email costs the same scrypt work as a known one. Generated once per process from a
# discarded random password; it matches nothing and is never stored.
_DUMMY_PASSWORD_HASH = generate_password_hash(secrets.token_urlsafe(32), method=PASSWORD_METHOD)
# Werkzeug's "method:params" prefix for hashes created now, e.g. ``scrypt:32768:8:1``.
_CURRENT_HASH_METHOD = _DUMMY_PASSWORD_HASH.split("$", 1)[0]

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


def throttle_identifier(value) -> str:
    """What a submitted login identifier is throttled under.

    The normalized email, or for malformed input a bounded folded form behind a NUL
    marker that no valid email can equal. Malformed identifiers are thereby throttled
    like unknown ones instead of bypassing the limit.
    """
    try:
        return normalize_email(value)
    except ValueError:
        folded = _fold(value) if isinstance(value, str) else ""
        return "\0malformed\0" + folded[:MAX_EMAIL_LENGTH]


def password_problem(password: str, email: str | None = None, role: str | None = None) -> str | None:
    """Return a Bulgarian problem description, or ``None`` when the password is acceptable.

    With ``role="admin"`` the longer administrator minimum applies.
    """
    if not isinstance(password, str):
        return "Невалидна парола."
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Паролата трябва да е поне {MIN_PASSWORD_LENGTH} символа."
    if role == "admin" and len(password) < ADMIN_MIN_PASSWORD_LENGTH:
        return f"Паролата на администратор трябва да е поне {ADMIN_MIN_PASSWORD_LENGTH} символа."
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


def load_session_user(conn: sqlite3.Connection, user_id, session_key) -> dict | None:
    """The active user a session identity names, or ``None``.

    One bounded lookup by primary key. ``None`` for a malformed identity, an
    unknown or disabled user, and a key that does not match the stored one
    (compared in constant time). Neither key is logged or returned in an error.
    """
    if (
        not isinstance(user_id, int)
        or isinstance(user_id, bool)
        or not 0 < user_id < 2**63
        or not isinstance(session_key, str)
    ):
        return None
    user = get_user_by_id(conn, user_id)
    if user is None or not user["is_active"] or not user["session_key"]:
        return None
    if not hmac.compare_digest(session_key.encode("utf-8"), user["session_key"].encode("utf-8")):
        return None
    return user


def session_expired(role, issued_at, now) -> bool:
    """Has an account session issued at ``issued_at`` (epoch seconds) outlived its role?

    Fails closed: a missing or malformed ``iat``, one from the future, and an unknown
    role are all expired. ``role`` must come from the users row, never from the cookie.
    """
    lifetime = SESSION_LIFETIME_SECONDS.get(role)
    if lifetime is None or not isinstance(issued_at, int) or isinstance(issued_at, bool):
        return True
    if issued_at <= 0 or issued_at > now + SESSION_CLOCK_SKEW_SECONDS:
        return True
    return now - issued_at >= lifetime


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


def authenticate(conn: sqlite3.Connection, email, password) -> dict | None:
    """The active user these credentials belong to, or ``None``.

    Unknown email, wrong password, disabled account and account without a password are
    indistinguishable: each returns ``None`` after exactly one scrypt verification (the
    dummy hash stands in when there is no real one). On success a hash made with an
    older method or parameters is replaced; the session key is left alone because the
    password did not change. The caller commits.
    """
    row = None
    try:
        normalized = normalize_email(email)
    except ValueError:
        normalized = None
    if normalized is not None:
        cursor = conn.execute(
            f"SELECT {_USER_COLUMNS}, password_hash FROM users WHERE email = ?", (normalized,)
        )
        fetched = cursor.fetchone()
        if fetched is not None:
            row = dict(zip([col[0] for col in cursor.description], tuple(fetched)))
    stored_hash = row.pop("password_hash") if row is not None else None
    usable = isinstance(password, str) and len(password) <= MAX_PASSWORD_LENGTH
    matches = check_password_hash(stored_hash or _DUMMY_PASSWORD_HASH, password if usable else "")
    if row is None or not stored_hash or not usable or not matches or not row["is_active"]:
        return None
    if stored_hash.split("$", 1)[0] != _CURRENT_HASH_METHOD:
        conn.execute(
            "UPDATE users SET password_hash = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (generate_password_hash(password, method=PASSWORD_METHOD), row["id"]),
        )
    return row


def record_login(conn: sqlite3.Connection, user_id: int) -> None:
    conn.execute("UPDATE users SET last_login_at = CURRENT_TIMESTAMP WHERE id = ?", (user_id,))


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
