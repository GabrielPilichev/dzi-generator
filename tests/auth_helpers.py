"""Account session helpers for tests (Accounts PR 3).

Creates a real ``users`` row and injects the approved session shape
(``uid``, ``sk``, ``iat``) into a test client, bypassing the login POST (and its
password hashing). Role and active state are never written into the cookie; the
request's principal loader reads them from the temporary test database.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing

import pyotp

from tests.support import ROOT
from web.services import accounts, totp


def add_users_schema(conn: sqlite3.Connection) -> None:
    """Create the real ``users`` table (migration 012) in a hand-built minimal-schema test DB."""
    conn.executescript((ROOT / "web" / "migrations" / "012_users.sql").read_text(encoding="utf-8"))


def create_account(db_path, role: str = "student", *, display_name: str = "Тест Потребител",
                   is_active: bool = True, email: str | None = None,
                   password: str | None = None) -> dict:
    """Insert an account (no email or password unless given); return its id and session key."""
    with closing(sqlite3.connect(str(db_path))) as conn:
        user_id = accounts.create_user(conn, display_name=display_name, role=role, is_active=is_active,
                                       email=email, password=password)
        conn.commit()
        session_key = conn.execute(
            "SELECT session_key FROM users WHERE id = ?", (user_id,)
        ).fetchone()[0]
    return {"id": user_id, "session_key": session_key}


def set_account_session(client, user_id, session_key, *, iat=None) -> None:
    """Write the account identity keys into the client's session, nothing else."""
    with client.session_transaction() as sess:
        sess["uid"] = user_id
        sess["sk"] = session_key
        sess["iat"] = int(time.time()) if iat is None else iat


def login_as(client, db_path, role: str = "student", **account_options) -> dict:
    """Create an account of ``role`` and sign the client in as it."""
    account = create_account(db_path, role, **account_options)
    set_account_session(client, account["id"], account["session_key"])
    return account


def sign_in_account(client, db_path, role: str = "student", **account_options) -> dict:
    """Alias of :func:`login_as`: sign ``client`` in as a new account of ``role``."""
    return login_as(client, db_path, role, **account_options)


def sign_in_student(client, db_path, **account_options) -> dict:
    return login_as(client, db_path, "student", **account_options)


def sign_in_teacher(client, db_path, **account_options) -> dict:
    return login_as(client, db_path, "teacher", **account_options)


def sign_in_admin(client, db_path, **account_options) -> dict:
    return login_as(client, db_path, "admin", **account_options)


def enroll_totp(db_path, user_id: int, key, confirmed_at) -> dict:
    """Give an account a confirmed TOTP credential with a fresh synthetic secret.

    Confirmed with the code of ``confirmed_at`` (a ``datetime``), whose step is then
    used up. Enrolment and confirmation rotate the session key; the new one is
    returned with the secret and the one-time recovery codes.
    """
    with closing(sqlite3.connect(str(db_path))) as conn:
        uri = totp.enroll(conn, user_id, key, f"user{user_id}@example.bg")
        secret = pyotp.parse_uri(uri).secret
        recovery_codes = totp.confirm(conn, user_id, key, pyotp.TOTP(secret).at(confirmed_at), confirmed_at)
        conn.commit()
        session_key = conn.execute("SELECT session_key FROM users WHERE id = ?", (user_id,)).fetchone()[0]
    return {"secret": secret, "recovery_codes": recovery_codes, "session_key": session_key}
