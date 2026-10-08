"""Student-side access to a quiz attempt.

Shared by the quiz and practical controllers so neither imports the other. The
decision itself is :func:`web.access.can_access_attempt`; this module supplies
its inputs (the attempt row, the request's validated principal and the browser
marker) and answers 404 when it says no.

An account-owned attempt (``quiz_attempts.user_id`` set) belongs to the account
that started it (student, teacher or admin) and is reachable from any browser
that account signs in on. Nobody else reaches it: there is no teacher or admin
bypass here, review of other people's answers is the teacher pages' business. The browser
marker below exists only for historical attempts (``user_id`` NULL), which were
started before accounts and stay bound to the browser that started them. It is
no longer written: every new attempt is account-owned.
"""

from __future__ import annotations

from flask import abort, session

from web.access import can_access_attempt, current_principal


def quiz_attempt_owner_session_key(attempt_id: int) -> str:
    return f"quiz_attempt_owner:{int(attempt_id)}"


def has_quiz_attempt_marker(attempt_id: int) -> bool:
    """Whether this browser session started the (historical) attempt."""
    return session.get(quiz_attempt_owner_session_key(attempt_id)) is True


def require_quiz_attempt_access(conn, attempt_id: int):
    """The attempt row, if the current request may use it; otherwise 404.

    A missing attempt, another account's attempt and a historical attempt
    without this browser's marker are indistinguishable. Call it before
    reading or changing anything that belongs to the attempt.
    """
    attempt = conn.execute("SELECT * FROM quiz_attempts WHERE id = ?", (attempt_id,)).fetchone()
    if attempt is None or not can_access_attempt(
        current_principal(), attempt["user_id"], has_quiz_attempt_marker(attempt_id)
    ):
        abort(404)
    return attempt
