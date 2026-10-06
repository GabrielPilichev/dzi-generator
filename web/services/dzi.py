"""The one definition of "is this an official DZI question?".

An official DZI question is exam content published by the ministry, as opposed
to curriculum, classroom-test, AI-generated or reviewed subject questions. This
module owns only that content-identity predicate. Whether a given viewer may see
such a question is access policy and belongs to a later layer; this module never
reads a session or a request.

A question is official DZI content when either holds:

* it is linked to an exam task through ``exam_task_questions`` (the
  ``exam_tasks`` skeleton, any link role); or
* it is a legacy parser question: it has a ``source_number``, is not
  AI-generated, and its ``exam_id`` points at an ``exams`` row whose ``level``
  is ``'DZI'``. This keeps the ``aug_2023``, ``may_2023`` and ``jun_2022``
  questions, which have no task links.

Both branches are expressed once, as the SQL fragment from
:func:`dzi_question_sql`. :func:`is_dzi_question` evaluates that same fragment, so
the Python and SQL forms cannot disagree.

``source_number`` alone is not enough: AI-generated sets and reviewed subject
questions (Physics, Biology) also carry a ``source_exam``/``source_number`` pair
but have no DZI ``exam_id``.

Standard library only; never ``web.app`` or the quiz service.
"""

from __future__ import annotations

import re
import sqlite3

DZI_EXAM_LEVEL = "DZI"

_ALIAS_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def dzi_question_sql(alias: str = "q") -> str:
    """SQL boolean expression, true for official DZI rows of ``questions``.

    ``alias`` names the ``questions`` table in the caller's query. It is
    validated as a plain identifier so the fragment is always safe to embed; the
    exam level is bound by the constant above, never by caller input.
    """
    if not _ALIAS_PATTERN.fullmatch(alias):
        raise ValueError(f"Invalid SQL alias: {alias!r}")
    # SAFE: alias is a validated identifier; DZI_EXAM_LEVEL is a module constant.
    return f"""(
        EXISTS (
            SELECT 1 FROM exam_task_questions dzi_etq
            WHERE dzi_etq.question_id = {alias}.id
        )
        OR (
            {alias}.source_number IS NOT NULL
            AND COALESCE({alias}.is_ai_generated, 0) = 0
            AND EXISTS (
                SELECT 1 FROM exams dzi_exam
                WHERE dzi_exam.id = {alias}.exam_id
                  AND dzi_exam.level = '{DZI_EXAM_LEVEL}'
            )
        )
    )"""


def is_dzi_question(conn: sqlite3.Connection, question_id: int) -> bool:
    """Whether ``question_id`` is an official DZI question (False if absent)."""
    row = conn.execute(
        f"SELECT 1 FROM questions q WHERE q.id = ? AND {dzi_question_sql('q')}",
        (question_id,),
    ).fetchone()
    return row is not None
