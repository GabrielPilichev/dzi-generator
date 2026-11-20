"""Start, resume or replace an account's section or topic practice session (C2).

Governed by docs/STUDENT_PRACTICE_DESIGN.md (sections 2, 4 and 5) over the
runtime tables of migration 026. This module only decides and writes the *start*
of a session: there is no submit, scoring, answer row, route, template or
progress query here.

Contract
--------
:func:`start_or_resume_practice` takes a caller-owned connection configured as
``web.db.connect_db`` configures it (``sqlite3.Row`` rows, foreign keys on), the
trusted principal of the signed-in account (never a form value), a positive
section id and an optional positive topic id. It returns a :class:`PracticeStart`
whose ``outcome`` is exactly one of:

``created``
    A new open session was inserted: at least ``MIN_QUESTIONS`` distinct eligible
    questions existed and ``min(MAX_QUESTIONS, eligible)`` of them were sampled
    without replacement. When the account's previous open session of this scope
    had gone stale it was abandoned in the same transaction
    (``abandoned_session_id``). Repeating after a submission also lands here, with
    a fresh seed and a fresh sample.
``resumed``
    The account's open session of this scope still presents at least one
    question that is renderable *and* in the current eligible pool. Nothing was
    written: id, stored plan, seed and start time are as they were, however thin
    the pool for a *new* session has become. ``question_ids`` are the stored ids,
    in stored order, that it presents now.
``empty_pool``
    No session was created because fewer than ``MIN_QUESTIONS`` eligible
    questions exist. If a stale open session was found it was abandoned (the one
    write this outcome can carry; ``abandoned_session_id``); otherwise nothing was
    written.
``scope_unavailable``
    The section does not exist, has no single known subject, is a DZI training
    section, or the requested topic is not a member of the section or has no
    consistent subject. Nothing was written, an open session included.
``account_unavailable``
    The account row is missing, inactive or of an unknown role when read under the
    writer lock. Nothing was written.

Exceptions (never an outcome): ``ValueError`` for an id that is not a positive
integer, :class:`PracticeAccessError` for a principal that is not an account
(anonymous, or a pending second factor), ``RuntimeError`` for a connection that
is not configured or already inside a transaction, and :class:`PracticeStateError`
(:class:`PracticePlanError` for a plan) for stored state the service refuses to
interpret. Any other database error propagates unchanged.

Stored plans
------------
``question_ids_json`` holds a JSON array of distinct positive integers (each at
most SQLite's largest integer) of length 0..``MAX_QUESTIONS``. Anything else is
*malformed*: unparseable JSON, a non-array, a non-integer (boolean, float, string,
null), a non-positive or oversized integer, a duplicate, or too many ids. A
malformed plan fails closed with :class:`PracticePlanError`: the transaction is
rolled back, nothing is written, the row stays open and is never read as an empty
pool, abandoned or replaced. A well-formed plan (``[]`` included) whose ids are
missing, unrenderable or out of the current scope is merely *stale*.

Transactions
------------
The decision is made inside ``BEGIN IMMEDIATE`` taken here; the service rejects a
connection that is already in a transaction, so every transaction it ends is its
own: it commits when it wrote (the abandonment and the replacement together) and
rolls back when it did not, and rolls back on any error, with explicit ``COMMIT`` and
``ROLLBACK`` statements so that a connection in Python's ``autocommit=True`` mode ends it too. The
connection itself is never closed. The timestamp is sampled while the lock is held, from the pure UTC
helpers of ``web.services.lifecycle``. This module has no part in the teacher
assignment lifecycle: it does not use its writer transaction, clock row,
high-water mark, quotas or profiles, and writes nothing outside
``practice_sessions``.

Eligibility
-----------
No eligibility rule lives here. The pool is ``quiz_section_question_ids`` for an
``AssessmentScope`` (approved, multiple choice, one consistent subject, four
valid options, usable visual), minus official DZI rows
(``web.services.dzi.dzi_question_sql``), and for a topic restricted to
``questions.topic_id``. Stored ids are checked with the existing
``filter_renderable_attempt_question_ids`` and then intersected with that pool, so
changed content can never introduce an out-of-scope question into a session.
"""

from __future__ import annotations

import json
import random
import secrets
import sqlite3
from typing import NamedTuple

from src.subject_assessment import AssessmentScope
from src.subject_content import sql_chunks, topic_ownership
from web.access import Principal, account_principal, can_take_quizzes
from web.services import lifecycle
from web.services.dzi import dzi_question_sql
from web.services.quiz import filter_renderable_attempt_question_ids, quiz_section_question_ids

# A new session needs at least this many distinct eligible questions...
MIN_QUESTIONS = 5
# ...and samples at most this many.
MAX_QUESTIONS = 10

OUTCOME_CREATED = "created"
OUTCOME_RESUMED = "resumed"
OUTCOME_EMPTY_POOL = "empty_pool"
OUTCOME_SCOPE_UNAVAILABLE = "scope_unavailable"
OUTCOME_ACCOUNT_UNAVAILABLE = "account_unavailable"

_SQLITE_MAX_INTEGER = 2 ** 63 - 1
_DZI_TRAINING_SECTION_TYPE = "dzi_training"

# The open-state predicate of the unique index of migration 026, restated.
_OPEN_SQL = "submitted_at IS NULL AND abandoned_at IS NULL"


class PracticeStart(NamedTuple):
    """The result of a start; ``outcome`` is one of the ``OUTCOME_*`` values.

    ``session_id``, ``question_ids`` and ``seed`` are set for ``created`` and
    ``resumed`` only (``question_ids`` in presentation order); the stored plan of
    a resumed session is never rewritten. ``abandoned_session_id`` names the
    stale session this call abandoned, on ``created`` and ``empty_pool``.
    """

    outcome: str
    session_id: int | None = None
    question_ids: tuple[int, ...] = ()
    seed: str | None = None
    abandoned_session_id: int | None = None

    @property
    def created(self) -> bool:
        return self.outcome == OUTCOME_CREATED

    @property
    def resumed(self) -> bool:
        return self.outcome == OUTCOME_RESUMED


class PracticeAccessError(PermissionError):
    """The principal is not an account principal: it cannot own a practice session."""


class PracticeStateError(RuntimeError):
    """Stored practice state the service refuses to interpret; nothing was written."""


class PracticePlanError(PracticeStateError):
    """An open session stores a malformed plan (see the module docstring)."""

    def __init__(self, session_id: int, reason: str):
        super().__init__(f"practice session {session_id} stores a malformed plan: {reason}")
        self.session_id = session_id


def _positive_id(value: object, name: str) -> int:
    if type(value) is not int or not 0 < value <= _SQLITE_MAX_INTEGER:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _account_id(principal: object) -> int:
    if not isinstance(principal, Principal) or type(principal.user_id) is not int:
        raise PracticeAccessError("practice requires a signed-in account")
    if not can_take_quizzes(principal) or not 0 < principal.user_id <= _SQLITE_MAX_INTEGER:
        raise PracticeAccessError("practice requires a signed-in account")
    return principal.user_id


def _require_usable_connection(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        raise RuntimeError("a practice start must begin outside a transaction")
    if conn.row_factory is not sqlite3.Row or conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise RuntimeError("the connection is not configured (sqlite3.Row rows, foreign keys on)")


def _new_seed() -> str:
    return secrets.token_hex(16)


def _utc_now_text() -> str:
    return lifecycle.format_timestamp(lifecycle.naive_utc_now())


def _parse_stored_plan(raw: object, session_id: int) -> list[int]:
    """The ids of a stored plan, or :class:`PracticePlanError` if it is malformed."""
    if type(raw) is not str:
        raise PracticePlanError(session_id, "not text")
    try:
        parsed = json.loads(raw)
    except (ValueError, RecursionError):
        raise PracticePlanError(session_id, "not JSON") from None
    if type(parsed) is not list:
        raise PracticePlanError(session_id, "not an array")
    if len(parsed) > MAX_QUESTIONS:
        raise PracticePlanError(session_id, "too many questions")
    if any(type(item) is not int or not 0 < item <= _SQLITE_MAX_INTEGER for item in parsed):
        raise PracticePlanError(session_id, "not positive integers")
    if len(set(parsed)) != len(parsed):
        raise PracticePlanError(session_id, "duplicate question")
    return parsed


def _eligible_question_ids(conn: sqlite3.Connection, scope: AssessmentScope, topic_id: int | None) -> list[int]:
    """Distinct usable, non-DZI question ids of the scope, in ascending id order."""
    ids = list(dict.fromkeys(quiz_section_question_ids(conn, scope.section_id, scope=scope)))
    allowed: set[int] = set()
    topic_sql = "" if topic_id is None else " AND q.topic_id = ?"
    for chunk in sql_chunks(ids):
        marks = ",".join("?" for _ in chunk)
        # SAFE: placeholders only, a validated alias and static fragments; values use ? params.
        rows = conn.execute(
            f"SELECT q.id FROM questions q WHERE q.id IN ({marks}){topic_sql} "
            f"AND NOT {dzi_question_sql('q')}",
            chunk + ([] if topic_id is None else [topic_id]),
        )
        allowed.update(int(row[0]) for row in rows)
    return [question_id for question_id in ids if question_id in allowed]


def _resolve_scope(conn: sqlite3.Connection, section_id: int, topic_id: int | None) -> AssessmentScope | None:
    """The scope of an available section (and topic), or ``None``."""
    try:
        scope = AssessmentScope(conn, section_id)
    except ValueError:  # missing section, or no single known subject
        return None
    if scope.section_type == _DZI_TRAINING_SECTION_TYPE:
        return None
    if topic_id is not None:
        member = conn.execute(
            "SELECT 1 FROM topic_section_assignments WHERE topic_id = ? AND section_id = ?",
            (topic_id, section_id),
        ).fetchone()
        owner = topic_ownership(conn, topic_id)
        if member is None or not owner.is_consistent or owner.subject != scope.subject:
            return None
    return scope


def _open_session(conn: sqlite3.Connection, user_id: int, section_id: int, topic_id: int | None):
    rows = conn.execute(
        "SELECT id, seed, question_ids_json FROM practice_sessions "
        "WHERE user_id = :user_id AND section_id = :section_id AND topic_id IS :topic_id "
        f"AND {_OPEN_SQL} ORDER BY id LIMIT 2",
        {"user_id": user_id, "section_id": section_id, "topic_id": topic_id},
    ).fetchall()
    if len(rows) > 1:  # the unique index of migration 026 makes this impossible when present
        raise PracticeStateError("more than one open practice session for one account and scope")
    return rows[0] if rows else None


def _abandon(conn: sqlite3.Connection, session_id: int, user_id: int, now_text: str) -> None:
    cursor = conn.execute(
        "UPDATE practice_sessions SET abandoned_at = ? "
        f"WHERE id = ? AND user_id = ? AND {_OPEN_SQL}",
        (now_text, session_id, user_id),
    )
    if cursor.rowcount != 1:
        raise PracticeStateError(f"practice session {session_id} could not be abandoned")


def _create(conn: sqlite3.Connection, user_id: int, section_id: int, topic_id: int | None,
            eligible: list[int], now_text: str) -> PracticeStart:
    seed = _new_seed()
    plan = random.Random(seed).sample(eligible, min(MAX_QUESTIONS, len(eligible)))
    cursor = conn.execute(
        "INSERT INTO practice_sessions (user_id, section_id, topic_id, seed, question_ids_json, started_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, section_id, topic_id, seed, json.dumps(plan), now_text),
    )
    if cursor.rowcount != 1 or cursor.lastrowid is None:
        raise PracticeStateError("the practice session was not inserted")
    return PracticeStart(OUTCOME_CREATED, int(cursor.lastrowid), tuple(plan), seed)


def _decide(conn: sqlite3.Connection, user_id: int, section_id: int,
            topic_id: int | None) -> tuple[PracticeStart, bool]:
    """The decision, under the writer lock. Returns the result and whether it wrote."""
    account = conn.execute("SELECT role, is_active FROM users WHERE id = ?", (user_id,)).fetchone()
    if (account is None or account["is_active"] != 1
            or not can_take_quizzes(account_principal(user_id, account["role"], ""))):
        return PracticeStart(OUTCOME_ACCOUNT_UNAVAILABLE), False

    scope = _resolve_scope(conn, section_id, topic_id)
    if scope is None:
        return PracticeStart(OUTCOME_SCOPE_UNAVAILABLE), False

    eligible = _eligible_question_ids(conn, scope, topic_id)
    existing = _open_session(conn, user_id, section_id, topic_id)

    abandoned_id = None
    now_text = None
    if existing is not None:
        session_id = int(existing["id"])
        stored = _parse_stored_plan(existing["question_ids_json"], session_id)
        renderable, _skipped = filter_renderable_attempt_question_ids(conn, stored)
        current = set(eligible)
        presented = tuple(question_id for question_id in renderable if question_id in current)
        if presented:
            return PracticeStart(OUTCOME_RESUMED, session_id, presented, existing["seed"]), False
        now_text = _utc_now_text()
        _abandon(conn, session_id, user_id, now_text)
        abandoned_id = session_id

    if len(eligible) < MIN_QUESTIONS:
        return PracticeStart(OUTCOME_EMPTY_POOL, abandoned_session_id=abandoned_id), abandoned_id is not None

    started = _create(conn, user_id, section_id, topic_id, eligible, now_text or _utc_now_text())
    return started._replace(abandoned_session_id=abandoned_id), True


def start_or_resume_practice(conn: sqlite3.Connection, principal: Principal, section_id: int,
                             topic_id: int | None = None) -> PracticeStart:
    """Start, resume or replace the principal's practice session (module docstring)."""
    user_id = _account_id(principal)
    section_id = _positive_id(section_id, "section_id")
    if topic_id is not None:
        topic_id = _positive_id(topic_id, "topic_id")
    _require_usable_connection(conn)

    conn.execute("BEGIN IMMEDIATE")
    try:
        result, wrote = _decide(conn, user_id, section_id, topic_id)
        # Explicit SQL, not conn.commit()/rollback(): under Python 3.12's autocommit=True those two
        # are no-ops even after a manual BEGIN, which would leave the writer lock held.
        conn.execute("COMMIT" if wrote else "ROLLBACK")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return result
