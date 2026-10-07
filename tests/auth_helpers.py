"""Account session helpers for tests (Accounts PR 3).

Creates a real ``users`` row and injects the approved session shape
(``uid``, ``sk``, ``iat``) into a test client, without going through ``/login``
(which costs a scrypt verification per call). PR 5 moves the legacy per-file
helpers onto this module.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing

from web.services import accounts


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
