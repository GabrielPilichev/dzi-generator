"""Which quiz attempts this browser session started.

Shared by the quiz and practical controllers so neither imports the other. This
records ownership of an attempt in the signed session; it is not an access
policy (see :mod:`web.access`).
"""

from __future__ import annotations

from flask import abort, session


def quiz_attempt_owner_session_key(attempt_id: int) -> str:
    return f"quiz_attempt_owner:{int(attempt_id)}"


def mark_quiz_attempt_owner(attempt_id: int) -> None:
    session[quiz_attempt_owner_session_key(attempt_id)] = True


def has_quiz_attempt_access(attempt_id: int) -> bool:
    return session.get(quiz_attempt_owner_session_key(attempt_id)) is True


def require_quiz_attempt_access(attempt_id: int) -> None:
    if not has_quiz_attempt_access(attempt_id):
        abort(404)
