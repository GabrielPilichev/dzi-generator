"""The assignment creation function exactly as it was before registration PR 7.

A verbatim copy of ``quiz_create_assignment`` from ``web/services/quiz.py`` at commit
``ede0c0b36ee3d163e317a93da744ff146b2c7c1e`` (git blob ``8d45d3f0ca9d2c52de364fbca3f2b9e1307c81a8``
of that file), with nothing changed but the module it lives in. It stands for any build that
keeps migration 024 and not the quota admission (a rollback or recovery build): it inserts an
owned assignment and records nothing in ``assignment_creations``. Tests run it against the PR 7
schema to prove that the history it leaves is recovered, never forgiven.
``tests.test_assignment_accounting`` compares it with git when the base commit is available.
"""

from __future__ import annotations

from web.access import Principal
from web.assignment_tokens import write_with_new_public_token
from web.services import lifecycle

def quiz_create_assignment(
    conn, *, principal: Principal, section_id: int, title_bg: str,
    question_count: int, time_limit_minutes: int | None = None,
    question_plan_json: str | None = None,
    deadline_at: str | None = None,
    display_timezone: str = lifecycle.DEFAULT_TIMEZONE,
) -> int:
    """Insert with the creator's stable account id as the owner and a new public token.

    The caller supplies the current Principal and owns the transaction. An
    admin's authorization bypass does not change who owns the new row. Every
    assignment created here, a duplicate included, gets its own
    ``public_token``; nothing updates it afterwards.

    ``deadline_at`` is the validated exclusive cutoff (``lifecycle.resolve_deadline``)
    or ``None`` for "no deadline"; ``display_timezone`` is stored either way. A
    duplicate passes its own choice: nothing of the source's lifecycle is copied.
    """
    if not principal.is_teacher:
        raise ValueError("Assignment creation requires teacher capability")

    def insert(public_token: str) -> int:
        cursor = conn.execute("""
            INSERT INTO quiz_assignments (
                section_id, title_bg, question_count, time_limit_minutes,
                question_plan_json, owner_user_id, public_token
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (section_id, title_bg, question_count, time_limit_minutes,
              question_plan_json, principal.user_id, public_token))
        return int(cursor.lastrowid)

    assignment_id = write_with_new_public_token(conn, insert)
    lifecycle.set_new_assignment_deadline(
        conn, assignment_id, deadline_at=deadline_at, display_timezone=display_timezone)
    return assignment_id
