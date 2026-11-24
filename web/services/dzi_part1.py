"""DZI Part 1 simulation pools, the account-owned start/resume service (C3.2) and the
submit / grading service with its bounded-request helper (C3.3) and the saved-result loader (C3.4).

Governed by docs/DZI_PART1_SIMULATION_DESIGN.md (sections 3-7, 9 and 10) and the scopes
docs/reviews/dzi-part1-start-service-scope.md, docs/reviews/dzi-part1-submit-service-scope.md and
docs/reviews/dzi-part1-result-loader-scope.md, over the runtime tables of migration 027. This module
decides and writes the *start* of a timed attempt (:func:`start_or_resume_part1`) and the *submission and
grading* of one (:func:`submit_part1`, with :func:`read_bounded_form` as the request boundary in front of it),
and it *reads* the saved result of a submitted one (:func:`load_part1_result`, read-only).
There is no route, template, attempt view, timer or exposure flag here, and no existing caller,
shared eligibility rule, shared grader or public rendering rule is changed. Nothing imports it
yet, so it is dormant: the submit service is documented in "Submission (C3.3)" and the loader in
"Result (C3.4)" at the end.

Contract
--------
:func:`start_or_resume_part1` takes a caller-owned connection configured as
``web.db.connect_db`` configures it (``sqlite3.Row`` rows, foreign keys on, outside any
transaction) and the trusted principal of the signed-in account (never a form value). It
runs one lifecycle writer transaction (``lifecycle.write_transaction``) and returns a
:class:`Part1Start` whose ``outcome`` is exactly one of:

``created``
    A new open attempt was inserted: the active blueprint validated, both pools were
    large enough, one cryptographic seed drove one seeded draw. ``started_at``,
    ``deadline_at`` and ``submit_cutoff_at`` come from the one admitted lifecycle ``now``.
    A lapsed or future-stamped attempt of the same account that was marked first is
    named by ``expired_attempt_id`` / ``abandoned_attempt_id``.
``resumed``
    The account's open attempt is returned unchanged and nothing was written (the
    high-water mark included): either it is inside its working time and still presents an
    item (``state == "open"``), or it is inside the submit grace (``state == "grace"``),
    where no item is judged at all.
``blueprint_unavailable`` / ``pool_insufficient``
    No attempt was created. A terminal marking made first (an expired lapsed attempt, an
    abandoned inconsistent or empty one) is still committed, because it is true whatever
    the blueprint or the pools say.
``account_unavailable``
    The account is missing, inactive or of an unknown role, read under the writer lock.
    Nothing was written.

Exceptions (never an outcome): :class:`Part1AccessError` for a principal that is not an
account, ``RuntimeError`` for a connection that is not configured or already inside a
transaction, :class:`Part1PlanError` / :class:`Part1StateError` for stored state the
service refuses to interpret (nothing is written and the row stays as it was), and
``web.services.lifecycle.LifecycleUnavailable`` when the clock cannot be used (the
caller answers 503; only the lifecycle layer's own clock-fault record can be stored).
Any other database error propagates and rolls the whole transaction back.

Order
-----
1. The account is rechecked under the writer lock and every decision and write is
   limited to it.
2. The account's open row (none of ``submitted_at``, ``expired_at``, ``abandoned_at``)
   is read **before any blueprint or pool query**. Its stored plan and slot format are
   validated as a pair; a malformed or inconsistent pair fails closed. The state is a
   pure function of its three stored timestamps and the one lifecycle ``now``
   (:func:`attempt_state`): the active blueprint and the grace constant never enter it.

   * ``now`` before ``started_at`` (an inconsistent row, design 7.4): abandoned.
   * ``started_at <= now < deadline_at``: resumed when at least one stored item is still
     presentable (judged by its *stored* kind and points and the *current* per-item
     rendering and readiness, with no pool rebuilt and no later change of the stimulus
     exclusion list applied), otherwise abandoned.
   * ``deadline_at <= now < submit_cutoff_at`` (grace): the same attempt, before any
     stale-item decision.
   * ``now >= submit_cutoff_at``: expired.

   Only a marking or the insert admits (``LifecycleWrite.admit``), once and before the
   first write. A read-only exit (resume, grace, ``account_unavailable``, a refused
   creation with nothing marked) discards the transaction, so the high-water mark does
   not move. ``check_factual`` is never called on a Part 1 row (it would put the one
   shared clock into ``suspect`` for every assignment because of one student's row).
   Each marking is guarded on id, owner and open state and its row count is verified.
3. Only now, and only to create, the active blueprint is validated and both pools are
   composed (below).
4. The insert stores the flat question ids in slot order and, separately, the slot
   format, the planned maximum and the duration.

Blueprint
---------
The active IT DZI blueprint of the repository format (``dzi_exams.DZI_FORMAT_VERSION``).
Its ``exam_part = 1`` slots must be the 25 contiguous slots 1..25 in the supported
pattern (slots 1-15 ``multiple_choice`` at 1 point, slots 16-25 ``short_answer`` at
3), none with a topic, section, topic-area or required-asset constraint, and
``part1_minutes`` an integer from 1 to :data:`MAX_PART1_MINUTES` **minutes** (the bound
and its unit; ``duration_seconds`` is minutes times 60). Slot counts, the slot format,
the planned maximum and the duration are derived from those validated rows. The whole
paper's ``total_points``, ``part2_minutes`` and the Part 2 slots play no part.

Timing (app timing, not official): 90 minutes today (the blueprint's ``part1_minutes``)
and :data:`SUBMIT_GRACE_SECONDS`, stored per attempt as ``submit_cutoff_at`` so that
changing either affects only attempts started afterwards.

Pools
-----
Candidates are *unique ids by kind* from **primary** links to tasks 1-25 of a DZI exam
of the blueprint's format, where the exam and the question are IT (canonical subject),
the question is at level DZI, belongs to that very exam and has its year, is approved
(``QUIZ_APPROVED_FILTER``) and is not AI-generated. Task number never decides the kind:
the question's own type does (``multiple_choice``, or ``fill_in`` / ``short_answer`` for
a short answer), and every candidate may fill any slot of its kind. The primary link must be
to a task whose ``exam_part`` is 1 (a task numbered 1-25 stored as part 2 is not Part 1). A question
that also has any link, of any role, to a Part 2 task (numbered above 25, or whose ``exam_part`` is not
1 whatever its number) is a Part 2 item, a contradictory cross-link included, and never a candidate.
There is no AI,
secondary-link, legacy unlinked, other-subject or repeated candidate and no borrowing
between the kinds.

* multiple choice: the existing per-item renderability rule, and a prompt that needs a
  visual needs one that the existing visual loader resolves (its two image asset types
  under its allowed root) *and* that exists as a file;
* short answer: the existing per-item rule and the existing generation-readiness check,
  and positive integer part weights that sum to the slot's points, all equal when the
  existing grading mode is order independent;
* a question id in :data:`MISSING_STIMULUS_QUESTION_IDS` is excluded when pools are
  composed, whatever its kind: the prompt refers to material nobody can see. This is a
  service-local list; ``VISUAL_DEPENDENT_PATTERNS`` and every other shared rule are not
  touched, and the list is never applied to an attempt already stored.

The 105 / 53 pool sizes of design 4.2 are evidence about today's canonical catalog,
pinned by the tests. They are **not** conditions of the service: it asks only for as
many distinct candidates per kind as the validated blueprint has slots of that kind.

Draw
----
``seed = secrets.token_hex(16)``; each pool sorted by id; one ``random.Random(seed)``
samples the multiple-choice items first and then the short-answer items, without
replacement; each kind fills its slots in slot order (:func:`draw_plan`). The stored
plan, never a recomputation, stays authoritative.

Submission (C3.3)
-----------------
:func:`read_bounded_form` bounds a request *before* any field is parsed (below);
:func:`submit_part1` takes a caller-owned connection, the trusted account principal, the **path**
attempt id and the bounded fields, and runs one lifecycle writer transaction. Its ``outcome`` is
exactly one of ``submitted``, ``already_submitted``, ``expired``, ``abandoned``, ``future_start``,
``not_found``, ``stale``, ``empty``, ``invalid_input`` or ``account_unavailable``; the decisions
and their order, all under the writer lock, are:

1. the account is active, else ``account_unavailable``; the attempt is read by **id and owner**
   together, so a foreign and an absent row are the same ``not_found``;
2. a stored terminal state answers from the row alone: ``submitted_at`` -> ``already_submitted``
   (with the stored score), ``abandoned_at`` -> ``abandoned``, ``expired_at`` -> ``expired``. The
   row is never recalculated from a grace constant or from the clock, so an earlier abandonment
   stays an abandonment even when the wall clock later passes its (false) stamp;
3. an unmarked row is judged from its three **stored** instants and the one lifecycle ``now``
   *before its plan is parsed*: ``now`` before ``started_at`` -> ``future_start`` (nothing graded,
   nothing written); ``now >= submit_cutoff_at`` (the cutoff is exclusive) -> the row is marked
   ``expired_at`` once and no answer is stored; otherwise (working time or grace) go on;
4. the stored plan and slot format are validated as a pair (a malformed pair raises and writes
   nothing); items still presentable now are those the resume path accepts
   (:func:`_presentable_slots`: stored kind and points, current rendering, readiness and part
   weights). None -> ``stale``;
5. the fields are matched, by the fixed names ``q_<question id>`` (the stored option key) and
   ``a_<question id>_<part>`` (text), against the *stored* plan only: an id outside the plan, a
   name of another shape, a part that is not a current stored part of that planned short-answer
   item and a field sent twice are ignored or unanswered. A recognized short-answer value longer
   than :data:`MAX_ANSWER_CHARACTERS` code points or containing NUL refuses the whole submission,
   ``invalid_input``, with nothing written; the bound is checked on every value of a recognized
   field, so a duplicate holding one bad value refuses too, before the duplicate counts as
   unanswered. It is checked only after the terminal, future-start, lapsed and stale decisions,
   which keep their own no-write outcomes. Nothing the client sends names an owner, a plan, a
   seed, a time, a weight or a mark;
6. no usable answer (an option key that is a current key of a presentable multiple-choice item,
   or a non-blank text for a presentable short-answer part) -> ``empty``, the attempt stays open;
7. otherwise ``admit()`` once, then one row per presentable multiple-choice slot and per
   short-answer part, then ``UPDATE ... WHERE id AND owner AND open`` with the row count verified.
   Any failure raises and rolls back the rows, the terminal state and the clock admission together.

Every outcome except ``submitted`` and the one marking above is read-only: the transaction is
discarded, so the high-water mark does not move and a retry changes no table. ``check_factual`` is
never called. Scoring: a multiple-choice row is worth the slot's *stored* points; a short-answer
row is worth the current stored weight of its part (the weights of one item sum to the stored slot
points, checked by presentability), matched with the existing normalizer and pure matcher in the
slot's existing ordered / order-independent mode. ``score_max`` is the sum of the possible points
of the presentable items, ``score_points`` the sum of the awarded ones; unanswered presentable
parts keep a zero verdict with their possible points. Neither correct keys nor accepted answers
are ever stored. Both numbers and every row are persisted and never recomputed.

Request bound (:func:`read_bounded_form`, design 6.4). With ``request.content_length`` (``None``
when absent or chunked, ``0`` when malformed): ``None`` -> 411 and over :data:`MAX_BODY_BYTES` ->
413, both before the body is read; then the per-request cap is set, the body is read **once** into
Werkzeug's cache and its byte count must equal the declared length (else 400) before
``request.form`` is read. When the declared length is exactly the cap, a terminated stream that
delivers more is read only up to the declared bytes: the excess is never seen, parsed, graded or
stored. That is stated, not hidden: over-delivery past the cap is not always detected.

Result (C3.4)
-------------
:func:`load_part1_result` takes a caller-owned connection, the trusted account principal and the **path**
attempt id, and returns a :class:`Part1Result` whose ``outcome`` is ``loaded``, ``not_found`` or
``account_unavailable``. A missing, foreign, open, expired and abandoned attempt are one ``not_found``
(the owner is part of the lookup; a teacher or admin principal owns only its own rows). It **never writes,
admits, stamps or reads a clock**, and it does not look at the active blueprint: a submitted result stays
readable whatever the blueprint, the grace constant or the clock say.

*One snapshot.* The account, the attempt, its saved answers and every current-content read happen in one
read transaction: the caller's own, reused and left exactly as it was on success and on error, otherwise a
deferred ``BEGIN`` that is ended with an explicit ``ROLLBACK`` (so an ``autocommit=True`` connection is
released too). A ``query_only`` connection works. A content edit on another connection is either wholly
before or wholly after the read.

*What is authoritative.* The saved verdicts, awarded and possible points, the raw typed text, the chosen
option key and the stored score (``score_points`` / ``score_max``) are returned as saved. Nothing is
regraded, repaired or derived from a current key, a saved maximum is never replaced by the planned one
(``max_points_planned`` is returned beside it) and no row is invented for an item that was skipped at grading.
The multiple-choice and short-answer subtotals are sums of the saved rows. ``score_max`` can be below the
planned maximum (an item was unpresentable when it was graded); that is reported, not corrected.

*Consistency, fail closed* (:class:`Part1StateError`, :class:`Part1PlanError`; nothing is repaired). Checked
against the saved flat plan and slot format: only the submitted state, a readable ``submitted_at`` between
the start and the cutoff, a score of whole numbers with ``0 <= points <= max`` and ``max`` no larger than the
planned maximum; every answer row names a slot of the plan, the plan's question id and the slot's kind;
a multiple-choice slot has exactly one row worth the slot's saved points; a short-answer slot has
distinct positive parts whose saved weights sum to the slot's saved points; an unanswered part is wrong; every
award is all or nothing; one grader version; and the row sums equal the stored score and maximum.

*Review rows.* Only slots that have saved rows are reviewed, in the original slot order (gaps stay gaps).
A saved slot is reviewable when its item still passes the per-item rule that the start and submit
services use (:func:`_presentable_slots`: stored kind and points, current rendering and readiness, current
part weights summing to the stored points) and, for a short answer, still has exactly the parts that were
graded. Reviewable items show the **current** prompt, options (in the attempt's seeded order, with the
saved key), correct key, accepted answers, visuals and explanations through the existing read helpers; the
saved verdict is shown as saved, so after a content change a review can disagree with it. Saved items that
are not reviewable are counted (``unavailable_slot_numbers``); planned items that have no saved row are
counted separately (``omitted_slot_numbers``). No pool is composed and the stimulus list is not consulted.
An empty review still returns the whole saved result (:attr:`Part1Result.only_saved_score`).

*Carried to activation, unchanged.* The inherited order-independent matcher (a repeated accepted answer
earns every part of such an item) and the clock limits of the start and submit services are not touched here.
"""

from __future__ import annotations

import json
import random
import re
import secrets
import sqlite3
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import NamedTuple

from werkzeug.exceptions import HTTPException, RequestEntityTooLarge

from src.dzi_open_questions import is_open_question_generation_ready
from src.subject_content import IT, canonical_subject, sql_chunks
from web.access import Principal, account_principal, can_take_quizzes
from web.services import dzi_exams, lifecycle
from web.services.question_visuals import QUESTION_VISUAL_STATIC_PREFIX, fetch_question_visual_assets
from web.services.quiz import (
    QUIZ_APPROVED_FILTER,
    QUIZ_NO_EXPLANATION_MESSAGE,
    _quiz_candidate_grading_mode,
    _quiz_subquestion_accepted_answers,
    fetch_option_explanations,
    fetch_question_explanations,
    filter_renderable_attempt_question_ids,
    grade_quiz_text_answers,
    quiz_correct_mc_letters_by_id,
    quiz_load_questions,
    quiz_normalize_text_answer,
    quiz_path_exists,
    quiz_prompt_needs_visual,
    quiz_question_types_by_id,
)

# --------------------------------------------------------------------------- constants

KIND_MULTIPLE_CHOICE = "multiple_choice"
KIND_SHORT_ANSWER = "short_answer"
SLOT_KINDS = (KIND_MULTIPLE_CHOICE, KIND_SHORT_ANSWER)
# The question types a short-answer slot accepts (the existing open-question types).
SHORT_ANSWER_QUESTION_TYPES = ("fill_in", "short_answer")

# The supported Part 1 pattern of the repository format: (kind, points each, slots).
PART1_PATTERN = ((KIND_MULTIPLE_CHOICE, 1, 15), (KIND_SHORT_ANSWER, 3, 10))
PART1_SLOT_COUNT = sum(count for _kind, _points, count in PART1_PATTERN)  # 25

# App timing, not official timing (design 7.1). The grace is stored per attempt as
# ``submit_cutoff_at`` at start; this constant is read only to compute it for a new attempt.
SUBMIT_GRACE_SECONDS = 60
# The duration bound, in minutes: ``part1_minutes`` must be an integer in [1, 240].
MAX_PART1_MINUTES = 240

# Reviewed questions whose prompt refers to material that is stored nowhere (design 4.2):
# 131 (aug_2024_v2 task 16, a spreadsheet), 67 (may_2022_v1 task 24, HTML and CSS code),
# 202 (aug_2022_v2 task 25, incomplete code). Applied when pools are composed, never to an
# attempt already stored; lifted for an id only by a separately approved content repair.
MISSING_STIMULUS_QUESTION_IDS = frozenset({67, 131, 202})

OUTCOME_CREATED = "created"
OUTCOME_RESUMED = "resumed"
OUTCOME_BLUEPRINT_UNAVAILABLE = "blueprint_unavailable"
OUTCOME_POOL_INSUFFICIENT = "pool_insufficient"
OUTCOME_ACCOUNT_UNAVAILABLE = "account_unavailable"

OUTCOME_SUBMITTED = "submitted"
OUTCOME_ALREADY_SUBMITTED = "already_submitted"
OUTCOME_EXPIRED = "expired"
OUTCOME_ABANDONED = "abandoned"
OUTCOME_FUTURE_START = "future_start"
OUTCOME_NOT_FOUND = "not_found"
OUTCOME_STALE = "stale"
OUTCOME_EMPTY = "empty"
OUTCOME_INVALID_INPUT = "invalid_input"
OUTCOME_LOADED = "loaded"  # the saved result (C3.4); a refusal there is OUTCOME_NOT_FOUND / OUTCOME_ACCOUNT_UNAVAILABLE

# Submit bounds (design 6.4): the body a submit may carry and the text of one short-answer value.
MAX_BODY_BYTES = 64 * 1024
MAX_ANSWER_CHARACTERS = 200
# Written on every stored answer row; a change of grading rules is a new value, never a rewrite.
GRADER_VERSION = "dzi_part1_v1"

STATE_FUTURE_START = "future_start"
STATE_OPEN = "open"
STATE_GRACE = "grace"
STATE_LAPSED = "lapsed"

_SQLITE_MAX_INTEGER = 2 ** 63 - 1
# The open-state predicate of the unique index of migration 027, restated.
_OPEN_SQL = "submitted_at IS NULL AND expired_at IS NULL AND abandoned_at IS NULL"


# --------------------------------------------------------------------------- types

class Part1Start(NamedTuple):
    """The result of a start; ``outcome`` is one of the ``OUTCOME_*`` values.

    ``attempt_id``, ``question_ids`` (the stored flat plan, slot = position + 1), ``seed``,
    ``started_at``, ``deadline_at`` and ``submit_cutoff_at`` are set for ``created`` and
    ``resumed``. ``state`` is ``"open"`` or ``"grace"`` for ``resumed`` (``None`` for
    ``created``). ``presentable_slots`` lists the 1-based slots whose item is presentable
    now for a ``resumed`` attempt in working time; it is ``None`` in grace, where no item is
    judged. ``expired_attempt_id`` / ``abandoned_attempt_id`` name the row this call marked
    (on ``created``, ``blueprint_unavailable`` and ``pool_insufficient``).
    """

    outcome: str
    attempt_id: int | None = None
    question_ids: tuple[int, ...] = ()
    seed: str | None = None
    started_at: str | None = None
    deadline_at: str | None = None
    submit_cutoff_at: str | None = None
    state: str | None = None
    presentable_slots: tuple[int, ...] | None = None
    expired_attempt_id: int | None = None
    abandoned_attempt_id: int | None = None

    @property
    def created(self) -> bool:
        return self.outcome == OUTCOME_CREATED

    @property
    def resumed(self) -> bool:
        return self.outcome == OUTCOME_RESUMED


class SlotFormat(NamedTuple):
    kind: str
    points: int


class Part1Blueprint(NamedTuple):
    """The validated Part 1 format, derived from the active blueprint's rows."""

    blueprint_id: int
    slug: str
    duration_minutes: int
    slots: tuple[SlotFormat, ...]

    @property
    def duration_seconds(self) -> int:
        return self.duration_minutes * 60

    @property
    def max_points(self) -> int:
        return sum(slot.points for slot in self.slots)

    def slot_count(self, kind: str) -> int:
        return sum(1 for slot in self.slots if slot.kind == kind)

    def points_of(self, kind: str) -> int:
        return next(slot.points for slot in self.slots if slot.kind == kind)

    def slot_format_json(self) -> str:
        return json.dumps([{"kind": slot.kind, "points": slot.points} for slot in self.slots])


class Pools(NamedTuple):
    """Distinct eligible question ids per kind, each ascending."""

    multiple_choice: tuple[int, ...]
    short_answer: tuple[int, ...]


class StoredAttempt(NamedTuple):
    """A validated open row: everything the state and a resume read, nothing else."""

    attempt_id: int
    seed: str
    question_ids: tuple[int, ...]
    slots: tuple[SlotFormat, ...]
    started_at: datetime
    deadline_at: datetime
    submit_cutoff_at: datetime
    started_text: str
    deadline_text: str
    cutoff_text: str


class Part1AccessError(PermissionError):
    """The principal is not an account principal: it cannot own a Part 1 attempt."""


class Part1StateError(RuntimeError):
    """Stored Part 1 state the service refuses to interpret; nothing was written."""


class Part1PlanError(Part1StateError):
    """An open attempt stores a malformed or inconsistent plan / slot-format pair."""

    def __init__(self, attempt_id: int, reason: str):
        super().__init__(f"DZI Part 1 attempt {attempt_id} stores a malformed plan: {reason}")
        self.attempt_id = attempt_id


# --------------------------------------------------------------------------- guards

def _account_id(principal: object) -> int:
    if not isinstance(principal, Principal) or type(principal.user_id) is not int:
        raise Part1AccessError("a Part 1 attempt requires a signed-in account")
    if not can_take_quizzes(principal) or not 0 < principal.user_id <= _SQLITE_MAX_INTEGER:
        raise Part1AccessError("a Part 1 attempt requires a signed-in account")
    return principal.user_id


def _require_usable_connection(conn: sqlite3.Connection) -> None:
    if getattr(conn, "autocommit", None) is True:
        # The lifecycle layer ends its transaction with conn.commit() / conn.rollback(), which do not
        # complete an explicit BEGIN IMMEDIATE on a Python 3.12 connection opened with autocommit=True.
        raise RuntimeError("a Part 1 operation needs a connection that is not in autocommit mode")
    if conn.in_transaction:
        raise RuntimeError("a Part 1 operation must begin outside a transaction")
    if conn.row_factory is not sqlite3.Row or conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise RuntimeError("the connection is not configured (sqlite3.Row rows, foreign keys on)")


def _account_is_active(conn: sqlite3.Connection, user_id: int) -> bool:
    account = conn.execute("SELECT role, is_active FROM users WHERE id = ?", (user_id,)).fetchone()
    return (account is not None and account["is_active"] == 1
            and can_take_quizzes(account_principal(user_id, account["role"], "")))


# --------------------------------------------------------------------------- state

def attempt_state(started_at: datetime, deadline_at: datetime, submit_cutoff_at: datetime,
                  now: datetime) -> str:
    """The state of an open row: a pure function of its three stored instants and ``now``.

    Every upper bound is exclusive: ``deadline_at`` itself is grace and
    ``submit_cutoff_at`` itself is lapsed. Neither the active blueprint nor
    :data:`SUBMIT_GRACE_SECONDS` is an input, so a stored row keeps its state.
    """
    if now < started_at:
        return STATE_FUTURE_START
    if now < deadline_at:
        return STATE_OPEN
    if now < submit_cutoff_at:
        return STATE_GRACE
    return STATE_LAPSED


# --------------------------------------------------------------------------- blueprint

def read_active_blueprint(conn: sqlite3.Connection) -> Part1Blueprint | None:
    """The validated Part 1 format of the active repository blueprint, or ``None``.

    ``None`` fails closed for anything unexpected (no active blueprint, a tampered slot
    kind, count, points, gap or constraint, an unusable duration). Reads only.
    """
    blueprint = conn.execute(
        "SELECT id, blueprint_slug, part1_minutes FROM dzi_blueprints "
        "WHERE blueprint_slug = ? AND is_active = 1", (dzi_exams.DZI_FORMAT_VERSION,)).fetchone()
    if blueprint is None:
        return None
    blueprint_id, slug, minutes = blueprint["id"], blueprint["blueprint_slug"], blueprint["part1_minutes"]
    if type(blueprint_id) is not int or blueprint_id <= 0 or type(slug) is not str:
        return None  # a legacy id of 0 or below would only fail late, at the 027 insert
    if type(minutes) is not int or not 1 <= minutes <= MAX_PART1_MINUTES:
        return None

    rows = conn.execute(
        "SELECT slot_number, exam_part, task_kind, points, topic_area, topic_id, section_id, "
        "required_asset_type FROM dzi_blueprint_slots WHERE blueprint_id = ? AND exam_part = 1 "
        "ORDER BY slot_number", (blueprint_id,)).fetchall()
    expected = [kind for kind, _points, count in PART1_PATTERN for _ in range(count)]
    expected_points = [points for _kind, points, count in PART1_PATTERN for _ in range(count)]
    if len(rows) != PART1_SLOT_COUNT:
        return None
    slots = []
    for index, row in enumerate(rows):
        if type(row["slot_number"]) is not int or row["slot_number"] != index + 1:
            return None  # a gap, a duplicate or an offset: not the contiguous 1..25
        if row["task_kind"] != expected[index] or type(row["points"]) is not int \
                or row["points"] != expected_points[index]:
            return None
        if any(row[column] is not None for column in ("topic_area", "topic_id", "section_id",
                                                      "required_asset_type")):
            return None  # an unsupported slot constraint is refused, never ignored
        slots.append(SlotFormat(row["task_kind"], row["points"]))
    return Part1Blueprint(blueprint_id, slug, minutes, tuple(slots))


# --------------------------------------------------------------------------- pools

def part_weights_valid(subquestions, slot_points: int) -> bool:
    """Positive integer part numbers and weights, the weights summing to the slot's points (all
    equal when the existing grading mode is order independent). ``subquestions`` are rows with
    ``subquestion_number``, ``points``, ``correct_answer`` and ``answer_alternatives``."""
    rows = list(subquestions)
    if not rows:
        return False
    # The legacy table accepts any part number; answer storage needs positive integers.
    numbers = [row["subquestion_number"] for row in rows]
    if any(type(number) is not int or number < 1 for number in numbers) or len(set(numbers)) != len(numbers):
        return False
    weights = [row["points"] for row in rows]
    if any(type(weight) is not int or weight <= 0 for weight in weights):
        return False
    if sum(weights) != slot_points:
        return False
    if _quiz_candidate_grading_mode([dict(row) for row in rows]) == "order_independent" \
            and len(set(weights)) != 1:
        return False
    return True


def _subquestions_by_question(conn: sqlite3.Connection, question_ids) -> dict[int, list[dict]]:
    found: dict[int, list[dict]] = {}
    for chunk in sql_chunks(sorted(set(question_ids))):
        marks = ",".join("?" for _ in chunk)
        # SAFE: placeholders only; values use ? params.
        for row in conn.execute(
            f"SELECT question_id, id, subquestion_number, correct_answer, answer_alternatives, points "
            f"FROM fill_in_subquestions WHERE question_id IN ({marks}) "
            f"ORDER BY question_id, subquestion_number, id", chunk):
            record = dict(row)
            found.setdefault(record.pop("question_id"), []).append(record)
    return found


def _resolved_visual_exists(conn: sqlite3.Connection, question_id: int) -> bool:
    """A visual the existing loader resolves (image asset types, allowed root) whose file exists."""
    assets = fetch_question_visual_assets(conn, [question_id]).get(question_id, [])
    return any(quiz_path_exists(QUESTION_VISUAL_STATIC_PREFIX + asset["static_filename"]) for asset in assets)


def compose_pools(conn: sqlite3.Connection, blueprint: Part1Blueprint) -> Pools:
    """The unique candidate pools by kind (module docstring, "Pools"). Reads only.

    ``blueprint`` supplies the format slug and the short-answer slot value the weight rule
    compares with. The result is data about the current catalog; no threshold is applied here.
    """
    # SAFE: QUIZ_APPROVED_FILTER is a static fragment over the alias q; values use ? params.
    candidates = conn.execute(f"""
        SELECT q.id, q.prompt, q.question_type, q.source_number, q.subject AS question_subject,
               e.subject AS exam_subject, q.has_image, q.image_path
        FROM questions q
        JOIN exams e ON e.id = q.exam_id
        WHERE e.level = 'DZI' AND e.format_version = ?
          AND q.level = 'DZI' AND q.year = e.year
          AND {QUIZ_APPROVED_FILTER} AND COALESCE(q.is_ai_generated, 0) = 0
          AND q.question_type IN ('multiple_choice', 'fill_in', 'short_answer')
          AND EXISTS (SELECT 1 FROM exam_task_questions etq
                      JOIN exam_tasks et ON et.id = etq.task_id
                      WHERE etq.question_id = q.id AND etq.role = 'primary'
                        AND et.exam_id = e.id AND et.exam_part = 1 AND et.task_number BETWEEN 1 AND ?)
          AND NOT EXISTS (SELECT 1 FROM exam_task_questions part2
                          JOIN exam_tasks t2 ON t2.id = part2.task_id
                          WHERE part2.question_id = q.id AND (t2.task_number > ? OR t2.exam_part <> 1))
        ORDER BY q.id
    """, (blueprint.slug, PART1_SLOT_COUNT, PART1_SLOT_COUNT)).fetchall()
    owned = [row for row in candidates
             if canonical_subject(row["question_subject"]) == IT
             and canonical_subject(row["exam_subject"]) == IT
             and int(row["id"]) not in MISSING_STIMULUS_QUESTION_IDS]
    open_ids = [int(row["id"]) for row in owned if row["question_type"] in SHORT_ANSWER_QUESTION_TYPES]
    renderable, _skipped = filter_renderable_attempt_question_ids(
        conn, [int(row["id"]) for row in owned], open_question_ids=open_ids)
    renderable = set(renderable)

    subquestions = _subquestions_by_question(conn, open_ids)
    short_points = blueprint.points_of(KIND_SHORT_ANSWER)
    multiple_choice, short_answer = [], []
    for row in owned:
        question_id = int(row["id"])
        if question_id not in renderable:
            continue
        if row["question_type"] == KIND_MULTIPLE_CHOICE:
            if quiz_prompt_needs_visual(row["prompt"]) and not _resolved_visual_exists(conn, question_id):
                continue
            multiple_choice.append(question_id)
        else:
            parts = subquestions.get(question_id, [])
            if is_open_question_generation_ready(row, parts) and part_weights_valid(parts, short_points):
                short_answer.append(question_id)
    return Pools(tuple(multiple_choice), tuple(short_answer))


def draw_plan(seed: str, slots: tuple[SlotFormat, ...], pools: Pools) -> list[int]:
    """The flat plan (slot = position + 1): one seeded draw over both sorted pools.

    ``random.Random(seed)`` samples the multiple-choice items first, then the short-answer
    items, each without replacement; each kind fills the slots of its kind in slot order.
    Raises ``ValueError`` when a pool is smaller than the slots of its kind.
    """
    wanted = {kind: sum(1 for slot in slots if slot.kind == kind) for kind in SLOT_KINDS}
    rng = random.Random(seed)
    drawn = {
        KIND_MULTIPLE_CHOICE: iter(rng.sample(sorted(pools.multiple_choice), wanted[KIND_MULTIPLE_CHOICE])),
        KIND_SHORT_ANSWER: iter(rng.sample(sorted(pools.short_answer), wanted[KIND_SHORT_ANSWER])),
    }
    return [next(drawn[slot.kind]) for slot in slots]


# --------------------------------------------------------------------------- stored row

def _json_list(raw: object, attempt_id: int, what: str) -> list:
    if type(raw) is not str:
        raise Part1PlanError(attempt_id, f"{what} is not text")
    try:
        parsed = json.loads(raw)
    except (ValueError, RecursionError):
        raise Part1PlanError(attempt_id, f"{what} is not JSON") from None
    if type(parsed) is not list:
        raise Part1PlanError(attempt_id, f"{what} is not an array")
    return parsed


def parse_stored_attempt(row) -> StoredAttempt:
    """Validate an open row as the pair the service itself wrote, or fail closed.

    Raises :class:`Part1PlanError` for the plan / slot-format pair and
    :class:`Part1StateError` for the saved maximum, the duration or the three instants.
    """
    attempt_id = row["id"]
    ids = _json_list(row["question_ids_json"], attempt_id, "question_ids_json")
    if len(ids) != PART1_SLOT_COUNT:
        raise Part1PlanError(attempt_id, f"{len(ids)} question ids, expected {PART1_SLOT_COUNT}")
    if any(type(item) is not int or not 0 < item <= _SQLITE_MAX_INTEGER for item in ids):
        raise Part1PlanError(attempt_id, "question ids are not positive integers")
    if len(set(ids)) != len(ids):
        raise Part1PlanError(attempt_id, "duplicate question id")

    entries = _json_list(row["slot_format_json"], attempt_id, "slot_format_json")
    if len(entries) != PART1_SLOT_COUNT:
        raise Part1PlanError(attempt_id, f"{len(entries)} slot formats, expected {PART1_SLOT_COUNT}")
    slots = []
    for entry in entries:
        if type(entry) is not dict or set(entry) != {"kind", "points"}:
            raise Part1PlanError(attempt_id, "a slot format is not exactly {kind, points}")
        kind, points = entry["kind"], entry["points"]
        if type(kind) is not str or kind not in SLOT_KINDS:
            raise Part1PlanError(attempt_id, "a slot format has an unsupported kind")
        if type(points) is not int or not 0 < points <= _SQLITE_MAX_INTEGER:
            raise Part1PlanError(attempt_id, "a slot format has non-positive points")
        slots.append(SlotFormat(kind, points))

    planned, duration, seed = row["max_points_planned"], row["duration_seconds"], row["seed"]
    if type(planned) is not int or planned != sum(slot.points for slot in slots):
        raise Part1StateError(f"DZI Part 1 attempt {attempt_id}: the saved maximum is not the slot points")
    if type(duration) is not int or duration <= 0:
        raise Part1StateError(f"DZI Part 1 attempt {attempt_id}: the saved duration is not positive")
    if type(seed) is not str or not seed:
        raise Part1StateError(f"DZI Part 1 attempt {attempt_id}: the seed is not text")
    try:
        started = lifecycle.parse_timestamp(row["started_at"])
        deadline = lifecycle.parse_timestamp(row["deadline_at"])
        cutoff = lifecycle.parse_timestamp(row["submit_cutoff_at"])
    except lifecycle.TimestampError:
        raise Part1StateError(f"DZI Part 1 attempt {attempt_id}: a stored instant is unreadable") from None
    # Compared after parsing, never by building a timedelta from the stored number: the table accepts
    # any integer there, and a huge one must be the defined bad state, not an OverflowError.
    if not started < deadline < cutoff or (deadline - started).total_seconds() != duration:
        raise Part1StateError(
            f"DZI Part 1 attempt {attempt_id}: the stored instants do not follow the stored duration")
    return StoredAttempt(attempt_id, seed, tuple(ids), tuple(slots), started, deadline, cutoff,
                         row["started_at"], row["deadline_at"], row["submit_cutoff_at"])


def _presentable_slots(conn: sqlite3.Connection, stored: StoredAttempt) -> tuple[int, ...]:
    """The 1-based slots whose stored item is presentable now.

    Judged by the *stored* kind and points and the *current* per-item rendering and
    readiness only: no pool is rebuilt and the stimulus exclusion list is not consulted.
    """
    open_ids = [question_id for question_id, slot in zip(stored.question_ids, stored.slots)
                if slot.kind == KIND_SHORT_ANSWER]
    renderable, _skipped = filter_renderable_attempt_question_ids(
        conn, list(stored.question_ids), open_question_ids=open_ids)
    renderable = set(renderable)
    types = quiz_question_types_by_id(conn, list(stored.question_ids))
    parts = _subquestions_by_question(conn, open_ids)
    questions = {}
    for chunk in sql_chunks(stored.question_ids):
        marks = ",".join("?" for _ in chunk)
        # SAFE: placeholders only; values use ? params.
        for row in conn.execute(
                f"SELECT id, prompt, question_type, source_number FROM questions WHERE id IN ({marks})", chunk):
            questions[int(row["id"])] = row
    slots = []
    for number, (question_id, slot) in enumerate(zip(stored.question_ids, stored.slots), start=1):
        if question_id not in renderable:
            continue
        if slot.kind == KIND_MULTIPLE_CHOICE:
            if types.get(question_id) != KIND_MULTIPLE_CHOICE:
                continue
            # The shared helper also counts a path the loader never attaches; the same loader-based
            # check as at creation, for this one item (no pool, no exclusion list).
            if quiz_prompt_needs_visual(questions[question_id]["prompt"]) \
                    and not _resolved_visual_exists(conn, question_id):
                continue
        else:
            # The shared render filter reads no source number, so the readiness rule is applied here in full.
            item_parts = parts.get(question_id, [])
            if types.get(question_id) not in SHORT_ANSWER_QUESTION_TYPES \
                    or not is_open_question_generation_ready(questions[question_id], item_parts) \
                    or not part_weights_valid(item_parts, slot.points):
                continue
        slots.append(number)
    return tuple(slots)


def _open_attempt(conn: sqlite3.Connection, user_id: int):
    rows = conn.execute(
        "SELECT id, seed, question_ids_json, slot_format_json, max_points_planned, duration_seconds, "
        f"started_at, deadline_at, submit_cutoff_at FROM dzi_part1_attempts "
        f"WHERE user_id = ? AND {_OPEN_SQL} ORDER BY id LIMIT 2", (user_id,)).fetchall()
    if len(rows) > 1:  # the unique index of migration 027 makes this impossible when present
        raise Part1StateError("more than one open DZI Part 1 attempt for one account")
    return rows[0] if rows else None


def _mark(conn: sqlite3.Connection, attempt_id: int, user_id: int, column: str, now_text: str) -> None:
    """Stamp one terminal column, guarded on id, owner and open state; the count is verified."""
    if column not in ("expired_at", "abandoned_at"):
        raise ValueError(column)
    # SAFE: ``column`` is one of the two literals above; values use ? params.
    cursor = conn.execute(
        f"UPDATE dzi_part1_attempts SET {column} = ? WHERE id = ? AND user_id = ? AND {_OPEN_SQL}",
        (now_text, attempt_id, user_id))
    if cursor.rowcount != 1:
        raise Part1StateError(f"DZI Part 1 attempt {attempt_id} could not be marked")


def _resumed(stored: StoredAttempt, state: str, presentable: tuple[int, ...] | None) -> Part1Start:
    return Part1Start(OUTCOME_RESUMED, stored.attempt_id, stored.question_ids, stored.seed,
                      stored.started_text, stored.deadline_text, stored.cutoff_text, state, presentable)


# --------------------------------------------------------------------------- start

def start_or_resume_part1(conn: sqlite3.Connection, principal: Principal) -> Part1Start:
    """Start, resume, expire or abandon the account's Part 1 attempt (module docstring)."""
    user_id = _account_id(principal)
    _require_usable_connection(conn)
    with lifecycle.write_transaction(conn) as transaction:
        result, wrote = _decide(conn, transaction, user_id)
        if not wrote:
            transaction.discard()  # nothing written, the high-water mark included
    return result


def _decide(conn: sqlite3.Connection, transaction: lifecycle.LifecycleWrite,
            user_id: int) -> tuple[Part1Start, bool]:
    """The decision under the writer lock: the result and whether it wrote anything."""
    if not _account_is_active(conn, user_id):
        return Part1Start(OUTCOME_ACCOUNT_UNAVAILABLE), False

    now, now_text = transaction.now, transaction.now_text
    expired_id = abandoned_id = None
    admitted = False  # one admission, before the first write of this transaction, never a second call
    existing = _open_attempt(conn, user_id)
    if existing is not None:
        stored = parse_stored_attempt(existing)
        state = attempt_state(stored.started_at, stored.deadline_at, stored.submit_cutoff_at, now)
        if state == STATE_GRACE:
            # Throughout grace the same attempt, before any stale-item decision.
            return _resumed(stored, STATE_GRACE, None), False
        if state == STATE_OPEN:
            presentable = _presentable_slots(conn, stored)
            if presentable:
                return _resumed(stored, STATE_OPEN, presentable), False
        # future-start (inconsistent row), lapsed, or open and presenting nothing: a marking.
        transaction.admit()
        admitted = True
        if state == STATE_LAPSED:
            _mark(conn, stored.attempt_id, user_id, "expired_at", now_text)
            expired_id = stored.attempt_id
        else:
            _mark(conn, stored.attempt_id, user_id, "abandoned_at", now_text)
            abandoned_id = stored.attempt_id
    marked = expired_id is not None or abandoned_id is not None

    # Only now, and only to create, the blueprint and the pools.
    blueprint = read_active_blueprint(conn)
    if blueprint is None:
        return Part1Start(OUTCOME_BLUEPRINT_UNAVAILABLE, expired_attempt_id=expired_id,
                          abandoned_attempt_id=abandoned_id), marked
    pools = compose_pools(conn, blueprint)
    if (len(pools.multiple_choice) < blueprint.slot_count(KIND_MULTIPLE_CHOICE)
            or len(pools.short_answer) < blueprint.slot_count(KIND_SHORT_ANSWER)):
        return Part1Start(OUTCOME_POOL_INSUFFICIENT, expired_attempt_id=expired_id,
                          abandoned_attempt_id=abandoned_id), marked

    if not admitted:
        transaction.admit()
    seed = secrets.token_hex(16)
    plan = draw_plan(seed, blueprint.slots, pools)
    deadline_text = lifecycle.format_timestamp(now + timedelta(seconds=blueprint.duration_seconds))
    cutoff_text = lifecycle.format_timestamp(
        now + timedelta(seconds=blueprint.duration_seconds + SUBMIT_GRACE_SECONDS))
    cursor = conn.execute(
        "INSERT INTO dzi_part1_attempts (user_id, blueprint_id, seed, question_ids_json, slot_format_json, "
        "max_points_planned, duration_seconds, started_at, deadline_at, submit_cutoff_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, blueprint.blueprint_id, seed, json.dumps(plan), blueprint.slot_format_json(),
         blueprint.max_points, blueprint.duration_seconds, now_text, deadline_text, cutoff_text))
    if cursor.rowcount != 1 or cursor.lastrowid is None:
        raise Part1StateError("the DZI Part 1 attempt was not inserted")
    return Part1Start(OUTCOME_CREATED, int(cursor.lastrowid), tuple(plan), seed, now_text, deadline_text,
                      cutoff_text, None, None, expired_id, abandoned_id), True


# =========================================================================== submit (C3.3)

# The fixed field names of the attempt form (design 6.4). Ids and parts are canonical positive
# decimals of at most 19 digits: a longer or odd spelling is not a field of this form, and no
# integer conversion ever sees an unbounded digit string.
_CHOICE_FIELD = re.compile(r"q_([1-9][0-9]{0,18})", re.ASCII)
_PART_FIELD = re.compile(r"a_([1-9][0-9]{0,18})_([1-9][0-9]{0,18})", re.ASCII)


class Part1Answer(NamedTuple):
    """One stored answer row: a multiple-choice slot (``subquestion_number`` 0) or one short-answer part.

    ``chosen_letter`` / ``raw_answer`` are ``None`` when the part was left unanswered. No correct key
    and no accepted answer is part of it.
    """

    slot_number: int
    subquestion_number: int
    question_id: int
    kind: str
    chosen_letter: str | None
    raw_answer: str | None
    is_correct: bool
    points_possible: int
    points_awarded: int


class Part1Submission(NamedTuple):
    """The result of a submit; ``outcome`` is one of the ``OUTCOME_*`` values.

    ``attempt_id`` is set for every outcome except ``not_found`` and ``account_unavailable``.
    ``score_points``, ``score_max`` and ``submitted_at`` are the persisted values, set for
    ``submitted`` and ``already_submitted`` only; ``answers`` are the rows stored by *this* call
    (``submitted`` only).
    """

    outcome: str
    attempt_id: int | None = None
    score_points: int | None = None
    score_max: int | None = None
    submitted_at: str | None = None
    answers: tuple[Part1Answer, ...] = ()

    @property
    def submitted(self) -> bool:
        return self.outcome == OUTCOME_SUBMITTED


def _stored_instants(row) -> tuple[datetime, datetime, datetime]:
    """The three stored instants of an unmarked row, checked against the stored duration.

    Read before the plan is parsed (a lapsed row is expired from its time alone). Compared after
    parsing, never by building a timedelta from the stored number.
    """
    attempt_id, duration = row["id"], row["duration_seconds"]
    if type(duration) is not int or duration <= 0:
        raise Part1StateError(f"DZI Part 1 attempt {attempt_id}: the saved duration is not positive")
    try:
        started = lifecycle.parse_timestamp(row["started_at"])
        deadline = lifecycle.parse_timestamp(row["deadline_at"])
        cutoff = lifecycle.parse_timestamp(row["submit_cutoff_at"])
    except lifecycle.TimestampError:
        raise Part1StateError(f"DZI Part 1 attempt {attempt_id}: a stored instant is unreadable") from None
    if not started < deadline < cutoff or (deadline - started).total_seconds() != duration:
        raise Part1StateError(
            f"DZI Part 1 attempt {attempt_id}: the stored instants do not follow the stored duration")
    return started, deadline, cutoff


def _field_values(fields: object) -> dict[str, tuple]:
    """Every field name with all of its values, from a ``getlist`` object (a Werkzeug ``MultiDict``) or a
    mapping of name to value(s). A plain dict-style read of a ``MultiDict`` would show one value per
    name and hide a duplicate, so that shape is read through ``getlist``."""
    if hasattr(fields, "getlist"):
        return {name: tuple(fields.getlist(name)) for name in fields.keys()}
    if not isinstance(fields, Mapping):
        raise ValueError("fields must be a mapping of field name to value(s)")
    found = {}
    for name, values in fields.items():
        if isinstance(values, (str, bytes)):
            found[name] = (values,)
        elif isinstance(values, (list, tuple)):
            found[name] = tuple(values)
        else:
            found[name] = (None, None)  # not a usable value: unanswered, like a field sent twice
    return found


def _option_keys(conn: sqlite3.Connection, question_ids: list[int]) -> dict[int, set[str]]:
    """The current stored option keys of each multiple-choice question."""
    keys: dict[int, set[str]] = {question_id: set() for question_id in question_ids}
    for chunk in sql_chunks(sorted(set(question_ids))):
        marks = ",".join("?" for _ in chunk)
        # SAFE: placeholders only; the ids are bound parameters.
        for row in conn.execute(
                f"SELECT question_id, option_letter FROM multiple_choice_options WHERE question_id IN ({marks})",
                chunk):
            keys[int(row["question_id"])].add(row["option_letter"])
    return keys


class _Fields(NamedTuple):
    """The recognized, single-valued fields of a submission: what the stored plan can accept."""

    choices: dict[int, str]               # question id -> submitted option key (multiple choice)
    texts: dict[tuple[int, int], str]     # (question id, part number) -> submitted text (short answer)
    invalid: bool                         # a recognized short-answer value breaks the bound


def _match_fields(stored: StoredAttempt, parts: dict[int, list[dict]], fields: dict[str, tuple]) -> _Fields:
    """Match field names against the *stored* plan only (never the request's own claims).

    ``q_<id>`` is recognized for a planned multiple-choice item; ``a_<id>_<n>`` for a planned
    short-answer item whose current stored parts include ``n``. Anything else is ignored. The text
    bound is applied to EVERY value of a recognized text field first; only then is a field sent
    twice, or with a value that is not text, unanswered.
    """
    kind_of = {question_id: slot.kind for question_id, slot in zip(stored.question_ids, stored.slots)}
    stored_parts = {question_id: {row["subquestion_number"] for row in rows} for question_id, rows in parts.items()}
    choices: dict[int, str] = {}
    texts: dict[tuple[int, int], str] = {}
    invalid = False
    for name, values in fields.items():
        if type(name) is not str:
            continue
        choice = _CHOICE_FIELD.fullmatch(name)
        if choice is not None:
            question_id = int(choice.group(1))
            if kind_of.get(question_id) == KIND_MULTIPLE_CHOICE and len(values) == 1 and type(values[0]) is str:
                choices[question_id] = values[0]
            continue
        part = _PART_FIELD.fullmatch(name)
        if part is None:
            continue
        question_id, number = int(part.group(1)), int(part.group(2))
        if kind_of.get(question_id) != KIND_SHORT_ANSWER or number not in stored_parts.get(question_id, ()):
            continue
        # The bound applies to EVERY text value of a recognized field, before a duplicate is treated as
        # unanswered: one over-long or NUL value among legal duplicates still refuses the whole submit.
        for value in values:
            if type(value) is str and (len(value) > MAX_ANSWER_CHARACTERS or "\x00" in value):
                invalid = True
        if len(values) == 1 and type(values[0]) is str:
            texts[(question_id, number)] = values[0]
    return _Fields(choices, texts, invalid)


def _grade_submission(conn: sqlite3.Connection, stored: StoredAttempt, presentable: set[int],
                      parts: dict[int, list[dict]], fields: _Fields) -> tuple[list[Part1Answer], bool]:
    """A verdict row per presentable multiple-choice slot and short-answer part, and whether any
    answer was usable. Reads only; nothing here knows a transaction."""
    mc_ids = [question_id for number, (question_id, slot) in enumerate(zip(stored.question_ids, stored.slots), 1)
              if number in presentable and slot.kind == KIND_MULTIPLE_CHOICE]
    keys = _option_keys(conn, mc_ids)
    correct = quiz_correct_mc_letters_by_id(conn, mc_ids)

    answers: list[Part1Answer] = []
    usable = False
    for number, (question_id, slot) in enumerate(zip(stored.question_ids, stored.slots), start=1):
        if number not in presentable:
            continue  # an unpresentable item is neither graded nor part of the effective maximum
        if slot.kind == KIND_MULTIPLE_CHOICE:
            submitted = fields.choices.get(question_id)
            chosen = submitted if submitted in keys[question_id] else None
            correct_key = correct.get(question_id)
            if correct_key is None:  # unreachable for a renderable item: exactly one correct option
                raise Part1StateError(f"question {question_id} has no correct option")
            is_correct = chosen is not None and chosen == correct_key
            usable = usable or chosen is not None
            # The slot's *stored* points: a re-seeded blueprint changes nothing here.
            answers.append(Part1Answer(number, 0, question_id, KIND_MULTIPLE_CHOICE, chosen, None, is_correct,
                                       slot.points, slot.points if is_correct else 0))
            continue

        rows = sorted(parts[question_id], key=lambda row: row["subquestion_number"])
        submitted_by_part = {row["subquestion_number"]: fields.texts.get((question_id, row["subquestion_number"]), "")
                             for row in rows}
        graded = grade_quiz_text_answers(
            submitted_by_part,
            {row["subquestion_number"]: _quiz_subquestion_accepted_answers(row) for row in rows},
            grading_mode=_quiz_candidate_grading_mode(rows))
        verdict = {result["subquestion_number"]: bool(result["is_correct"]) for result in graded}
        for row in rows:
            part_number, weight = row["subquestion_number"], row["points"]
            text = submitted_by_part[part_number]
            answered = bool(quiz_normalize_text_answer(text))
            usable = usable or answered
            is_correct = verdict[part_number]
            answers.append(Part1Answer(number, part_number, question_id, KIND_SHORT_ANSWER, None,
                                       text if answered else None, is_correct, weight, weight if is_correct else 0))
    return answers, usable


def _store_submission(conn: sqlite3.Connection, attempt_id: int, user_id: int, now_text: str,
                      answers: list[Part1Answer]) -> tuple[int, int]:
    """Insert every answer row and finalize the attempt; the caller holds the writer lock and has admitted.

    Returns the persisted ``(score_points, score_max)``. A row count that is not exactly one raises.
    """
    for answer in answers:
        cursor = conn.execute(
            "INSERT INTO dzi_part1_answers (attempt_id, slot_number, subquestion_number, question_id, kind, "
            "chosen_letter, raw_answer, is_correct, points_possible, points_awarded, grader_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (attempt_id, answer.slot_number, answer.subquestion_number, answer.question_id, answer.kind,
             answer.chosen_letter, answer.raw_answer, int(answer.is_correct), answer.points_possible,
             answer.points_awarded, GRADER_VERSION))
        if cursor.rowcount != 1:
            raise Part1StateError(f"an answer of DZI Part 1 attempt {attempt_id} was not stored")
    score_points = sum(answer.points_awarded for answer in answers)
    score_max = sum(answer.points_possible for answer in answers)
    # SAFE: _OPEN_SQL is a static fragment; values use ? params.
    cursor = conn.execute(
        "UPDATE dzi_part1_attempts SET submitted_at = ?, score_points = ?, score_max = ? "
        f"WHERE id = ? AND user_id = ? AND {_OPEN_SQL}",
        (now_text, score_points, score_max, attempt_id, user_id))
    if cursor.rowcount != 1:
        raise Part1StateError(f"DZI Part 1 attempt {attempt_id} could not be submitted")
    return score_points, score_max


def _decide_submission(conn: sqlite3.Connection, transaction: lifecycle.LifecycleWrite, user_id: int,
                       attempt_id: int, raw_fields: dict[str, tuple]) -> tuple[Part1Submission, bool]:
    """The submit decision under the writer lock: the result and whether it wrote anything."""
    if not _account_is_active(conn, user_id):
        return Part1Submission(OUTCOME_ACCOUNT_UNAVAILABLE), False
    if not 0 < attempt_id <= _SQLITE_MAX_INTEGER:
        return Part1Submission(OUTCOME_NOT_FOUND), False
    # The owner is part of the lookup: a missing and a foreign attempt are the same empty result.
    row = conn.execute(
        "SELECT id, seed, question_ids_json, slot_format_json, max_points_planned, duration_seconds, "
        "started_at, deadline_at, submit_cutoff_at, submitted_at, expired_at, abandoned_at, score_points, "
        "score_max FROM dzi_part1_attempts WHERE id = ? AND user_id = ?", (attempt_id, user_id)).fetchone()
    if row is None:
        return Part1Submission(OUTCOME_NOT_FOUND), False

    terminal = [column for column in ("submitted_at", "expired_at", "abandoned_at") if row[column] is not None]
    if len(terminal) > 1:
        raise Part1StateError(f"DZI Part 1 attempt {attempt_id} has more than one terminal state")
    if terminal == ["submitted_at"]:
        if type(row["score_points"]) is not int or type(row["score_max"]) is not int:
            raise Part1StateError(f"DZI Part 1 attempt {attempt_id} is submitted without a stored score")
        return Part1Submission(OUTCOME_ALREADY_SUBMITTED, attempt_id, row["score_points"], row["score_max"],
                               row["submitted_at"]), False
    if terminal == ["abandoned_at"]:
        return Part1Submission(OUTCOME_ABANDONED, attempt_id), False
    if terminal == ["expired_at"]:
        return Part1Submission(OUTCOME_EXPIRED, attempt_id), False

    # Unmarked: the stored instants and the one lifecycle now decide, before the plan is parsed.
    now = transaction.now
    started, deadline, cutoff = _stored_instants(row)
    state = attempt_state(started, deadline, cutoff, now)
    if state == STATE_FUTURE_START:
        return Part1Submission(OUTCOME_FUTURE_START, attempt_id), False  # left as it is: nothing graded or written
    if state == STATE_LAPSED:
        transaction.admit()
        _mark(conn, attempt_id, user_id, "expired_at", transaction.now_text)
        return Part1Submission(OUTCOME_EXPIRED, attempt_id), True

    stored = parse_stored_attempt(row)
    if conn.execute("SELECT 1 FROM dzi_part1_answers WHERE attempt_id = ? LIMIT 1", (attempt_id,)).fetchone():
        raise Part1StateError(f"open DZI Part 1 attempt {attempt_id} already has stored answers")
    presentable = set(_presentable_slots(conn, stored))
    if not presentable:
        return Part1Submission(OUTCOME_STALE, attempt_id), False

    open_ids = [question_id for question_id, slot in zip(stored.question_ids, stored.slots)
                if slot.kind == KIND_SHORT_ANSWER]
    parts = _subquestions_by_question(conn, open_ids)
    fields = _match_fields(stored, parts, raw_fields)
    if fields.invalid:
        return Part1Submission(OUTCOME_INVALID_INPUT, attempt_id), False
    answers, usable = _grade_submission(conn, stored, presentable, parts, fields)
    if not usable:
        return Part1Submission(OUTCOME_EMPTY, attempt_id), False

    transaction.admit()  # once, after every read and before the first write
    score_points, score_max = _store_submission(conn, attempt_id, user_id, transaction.now_text, answers)
    return Part1Submission(OUTCOME_SUBMITTED, attempt_id, score_points, score_max, transaction.now_text,
                           tuple(answers)), True


def submit_part1(conn: sqlite3.Connection, principal: Principal, attempt_id: int, fields: object) -> Part1Submission:
    """Grade and finalize the principal's own open Part 1 attempt (module docstring, "Submission (C3.3)").

    ``fields`` is what :func:`read_bounded_form` returned (a mapping of field name to its values), or any
    ``getlist`` object. The principal is trusted and never read from ``fields``; ``attempt_id`` is the path id.
    Raises :class:`Part1AccessError` for a principal that is not an account, ``ValueError`` for an
    ``attempt_id`` that is not an integer or ``fields`` of the wrong shape, ``RuntimeError`` for an unusable
    connection, :class:`Part1StateError` / :class:`Part1PlanError` for stored state it refuses to interpret
    (nothing is written) and ``web.services.lifecycle.LifecycleUnavailable`` for an unusable clock.
    """
    user_id = _account_id(principal)
    if type(attempt_id) is not int:
        raise ValueError("attempt_id must be an integer")
    raw_fields = _field_values(fields)
    _require_usable_connection(conn)
    with lifecycle.write_transaction(conn) as transaction:
        result, wrote = _decide_submission(conn, transaction, user_id, attempt_id, raw_fields)
        if not wrote:
            transaction.discard()  # nothing written, the high-water mark included
    return result


# --------------------------------------------------------------------------- bounded request

REASON_OK = "ok"
REASON_LENGTH_REQUIRED = "length_required"
REASON_TOO_LARGE = "too_large"
REASON_LENGTH_MISMATCH = "length_mismatch"
REASON_UNREADABLE = "unreadable"


class BoundedForm(NamedTuple):
    """What :func:`read_bounded_form` decided: an HTTP ``status`` (200, 400, 411 or 413) and a stable ``reason``.

    ``fields`` is set only for status 200 (a plain mapping of every field name to a tuple of all of its
    values, so a duplicate stays visible). ``declared_length`` and ``received_length`` are for tests and logs.
    """

    status: int
    reason: str
    fields: dict[str, tuple[str, ...]] | None = None
    declared_length: int | None = None
    received_length: int | None = None

    @property
    def ok(self) -> bool:
        return self.status == 200


def read_bounded_form(request) -> BoundedForm:
    """Bound a submit request before any field is parsed (module docstring, "Request bound").

    ``request`` is a Werkzeug / Flask request; nothing here reads the application, a connection or the
    clock, and nothing is written. Call it before anything else touches the body or ``request.form``
    (a body read earlier makes the byte count differ and the request is refused 400, which fails closed).
    The order is fixed: 411, 413, the bounded read and the length equality, then ``request.form``.
    """
    declared = request.content_length  # None: absent or chunked; 0: malformed (Werkzeug 3.1)
    if declared is None:
        return BoundedForm(411, REASON_LENGTH_REQUIRED)
    if declared > MAX_BODY_BYTES:
        return BoundedForm(413, REASON_TOO_LARGE, declared_length=declared)
    request.max_content_length = MAX_BODY_BYTES
    try:
        data = request.get_data(cache=True)
    except RequestEntityTooLarge:
        return BoundedForm(413, REASON_TOO_LARGE, declared_length=declared)
    except HTTPException:
        return BoundedForm(400, REASON_UNREADABLE, declared_length=declared)
    if len(data) != declared:
        return BoundedForm(400, REASON_LENGTH_MISMATCH, declared_length=declared, received_length=len(data))
    try:
        form = request.form  # parsed from the cached bytes: exactly the bounded prefix
        fields = {name: tuple(form.getlist(name)) for name in form.keys()}
    except RequestEntityTooLarge:
        return BoundedForm(413, REASON_TOO_LARGE, declared_length=declared, received_length=len(data))
    except HTTPException:
        return BoundedForm(400, REASON_UNREADABLE, declared_length=declared, received_length=len(data))
    return BoundedForm(200, REASON_OK, fields, declared, len(data))


# =========================================================================== result (C3.4)

class _SavedPart(NamedTuple):
    """One saved answer row, validated against the plan and the slot format."""

    subquestion_number: int
    chosen_letter: str | None
    raw_answer: str | None
    is_correct: bool
    points_possible: int
    points_awarded: int


class _SavedAnswers(NamedTuple):
    by_slot: dict[int, tuple[_SavedPart, ...]]  # slot number -> saved rows, only slots that have any
    grader_version: str
    multiple_choice_points: int
    multiple_choice_max: int
    short_answer_points: int
    short_answer_max: int


class Part1Result(NamedTuple):
    """The saved result of a submitted attempt; ``outcome`` is ``loaded``, ``not_found`` or ``account_unavailable``.

    Everything but ``outcome`` is filled in for ``loaded`` only (module docstring, "Result (C3.4)").

    ``score_points`` / ``score_max`` and the four subtotals are the *saved* numbers (the subtotals are sums of
    the saved answer rows); ``max_points_planned`` is the planned maximum, which ``score_max`` never replaces.
    ``questions`` are the multiple-choice review rows for the shared partial and ``short_answers`` the
    short-answer review rows, both in original slot order and both limited to ``reviewed_slot_numbers``.
    ``planned_slot_count`` counts the 25 planned slots; ``saved_slot_numbers`` those with saved rows;
    ``omitted_slot_numbers`` the planned slots without any saved row (not presentable when graded; never
    invented); ``unavailable_slot_numbers`` the saved slots that cannot be reviewed now.
    """

    outcome: str
    attempt_id: int | None = None
    submitted_at: str | None = None
    started_at: str | None = None
    duration_seconds: int | None = None
    score_points: int | None = None
    score_max: int | None = None
    max_points_planned: int | None = None
    multiple_choice_points: int | None = None
    multiple_choice_max: int | None = None
    short_answer_points: int | None = None
    short_answer_max: int | None = None
    grader_version: str | None = None
    planned_slot_count: int = 0
    saved_slot_numbers: tuple[int, ...] = ()
    omitted_slot_numbers: tuple[int, ...] = ()
    reviewed_slot_numbers: tuple[int, ...] = ()
    unavailable_slot_numbers: tuple[int, ...] = ()
    questions: tuple[dict, ...] = ()
    short_answers: tuple[dict, ...] = ()

    @property
    def loaded(self) -> bool:
        return self.outcome == OUTCOME_LOADED

    @property
    def only_saved_score(self) -> bool:
        """The score was saved but no item can be reviewed now."""
        return self.loaded and not self.reviewed_slot_numbers

    @property
    def below_planned_maximum(self) -> bool:
        """The saved maximum is under the planned one: some planned item was not graded."""
        return self.loaded and self.score_max < self.max_points_planned

    def review_context(self) -> dict:
        """The template variables the shared multiple-choice partial reads. Each row carries ``saved_slot_number``,
        which is what makes the partial number its cards by the saved slot instead of by position."""
        return {"questions": list(self.questions), "stale_attempt_message": None}


def _require_configured_connection(conn: sqlite3.Connection) -> None:
    """Like the writers' check, but a caller transaction (and autocommit) is fine: a load reuses it."""
    if conn.row_factory is not sqlite3.Row or conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise RuntimeError("the connection is not configured (sqlite3.Row rows, foreign keys on)")


def _in_read_snapshot(conn: sqlite3.Connection, read: Callable[[], Part1Result]) -> Part1Result:
    """Run ``read`` in one read snapshot: the caller's transaction if any, else our own.

    A caller transaction is reused and left exactly as it was, on success and on error. Our own is ended with
    explicit SQL (``ROLLBACK``: nothing was written), so a connection opened with ``autocommit=True``, where
    ``Connection.rollback()`` does nothing, is released too.
    """
    if conn.in_transaction:
        return read()
    conn.execute("BEGIN")  # deferred: the first read fixes the snapshot, no writer lock is taken
    try:
        result = read()
        conn.execute("ROLLBACK")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return result


def _invalid_result(attempt_id: int, reason: str) -> Part1StateError:
    return Part1StateError(f"DZI Part 1 attempt {attempt_id} stores an inconsistent result: {reason}")


def _whole(value: object) -> bool:
    return type(value) is int and 0 <= value <= _SQLITE_MAX_INTEGER


def _saved_answers(conn: sqlite3.Connection, stored: StoredAttempt, score_points: int, score_max: int) -> _SavedAnswers:
    """The saved rows of a submitted attempt, confirmed against its plan, slot format and score; never repaired."""
    attempt_id = stored.attempt_id
    by_slot: dict[int, list[_SavedPart]] = {}
    versions: set[str] = set()
    for row in conn.execute(
            "SELECT slot_number, subquestion_number, question_id, kind, chosen_letter, raw_answer, is_correct, "
            "points_possible, points_awarded, grader_version FROM dzi_part1_answers WHERE attempt_id = ? "
            "ORDER BY slot_number, subquestion_number", (attempt_id,)).fetchall():
        slot_number, part, question_id = row["slot_number"], row["subquestion_number"], row["question_id"]
        if not _whole(slot_number) or not 1 <= slot_number <= PART1_SLOT_COUNT:
            raise _invalid_result(attempt_id, "an answer for a slot outside the plan")
        slot = stored.slots[slot_number - 1]
        if question_id != stored.question_ids[slot_number - 1]:
            raise _invalid_result(attempt_id, "an answer for a question other than the planned one")
        if row["kind"] != slot.kind:
            raise _invalid_result(attempt_id, "an answer whose kind is not the slot's kind")
        letter, raw, verdict = row["chosen_letter"], row["raw_answer"], row["is_correct"]
        possible, awarded, version = row["points_possible"], row["points_awarded"], row["grader_version"]
        if type(verdict) is not int or verdict not in (0, 1):
            raise _invalid_result(attempt_id, "a verdict that is not 0 or 1")
        if not _whole(possible) or possible <= 0 or not _whole(awarded) or awarded != (possible if verdict else 0):
            raise _invalid_result(attempt_id, "an award that is not all or nothing of the possible points")
        if type(version) is not str or not version:
            raise _invalid_result(attempt_id, "a grader version that is not text")
        versions.add(version)
        if slot.kind == KIND_MULTIPLE_CHOICE:
            if part != 0 or raw is not None or (letter is not None and type(letter) is not str):
                raise _invalid_result(attempt_id, "a malformed multiple-choice row")
            if letter is None and verdict:
                raise _invalid_result(attempt_id, "a correct verdict without a chosen key")
            if possible != slot.points:
                raise _invalid_result(attempt_id, "a multiple-choice row not worth the slot's saved points")
        else:
            if not _whole(part) or part < 1 or letter is not None or (raw is not None and type(raw) is not str):
                raise _invalid_result(attempt_id, "a malformed short-answer row")
            if raw is None and verdict:
                raise _invalid_result(attempt_id, "a correct verdict for an unanswered part")
        saved = by_slot.setdefault(slot_number, [])
        if any(earlier.subquestion_number == part for earlier in saved):
            raise _invalid_result(attempt_id, "a duplicate answer row")
        saved.append(_SavedPart(part, letter, raw, verdict == 1, possible, awarded))
    if not by_slot:
        raise _invalid_result(attempt_id, "no saved answer rows")
    if len(versions) != 1:
        raise _invalid_result(attempt_id, "more than one grader version")

    totals = {KIND_MULTIPLE_CHOICE: [0, 0], KIND_SHORT_ANSWER: [0, 0]}
    for slot_number, parts in by_slot.items():
        slot = stored.slots[slot_number - 1]
        if slot.kind == KIND_MULTIPLE_CHOICE and len(parts) != 1:
            raise _invalid_result(attempt_id, "a multiple-choice slot with more than one row")
        if sum(part.points_possible for part in parts) != slot.points:
            raise _invalid_result(attempt_id, "saved weights that do not sum to the slot's saved points")
        totals[slot.kind][0] += sum(part.points_awarded for part in parts)
        totals[slot.kind][1] += sum(part.points_possible for part in parts)
    if totals[KIND_MULTIPLE_CHOICE][0] + totals[KIND_SHORT_ANSWER][0] != score_points:
        raise _invalid_result(attempt_id, "the saved awards differ from the stored score")
    if totals[KIND_MULTIPLE_CHOICE][1] + totals[KIND_SHORT_ANSWER][1] != score_max:
        raise _invalid_result(attempt_id, "the saved possible points differ from the stored maximum")
    return _SavedAnswers({slot_number: tuple(parts) for slot_number, parts in sorted(by_slot.items())},
                         versions.pop(), *totals[KIND_MULTIPLE_CHOICE], *totals[KIND_SHORT_ANSWER])


def _review_slots(conn: sqlite3.Connection, stored: StoredAttempt, saved: _SavedAnswers) -> tuple[int, ...]:
    """The saved slots whose item can be reviewed now, ascending: presentable by the services' per-item rule
    and, for a short answer, still made of exactly the parts that were graded."""
    presentable = set(_presentable_slots(conn, stored))
    short_ids = [stored.question_ids[number - 1] for number in saved.by_slot
                 if stored.slots[number - 1].kind == KIND_SHORT_ANSWER and number in presentable]
    current_parts = _subquestions_by_question(conn, short_ids)
    reviewable = []
    for number, parts in saved.by_slot.items():
        if number not in presentable:
            continue
        if stored.slots[number - 1].kind == KIND_SHORT_ANSWER:
            now = {row["subquestion_number"] for row in current_parts.get(stored.question_ids[number - 1], [])}
            if now != {part.subquestion_number for part in parts}:
                continue
        reviewable.append(number)
    return tuple(reviewable)


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _review_rows(conn: sqlite3.Connection, stored: StoredAttempt, saved: _SavedAnswers,
                 slots: tuple[int, ...]) -> tuple[tuple[dict, ...], tuple[dict, ...], tuple[int, ...]]:
    """Current content around the saved verdicts: multiple-choice rows for the shared partial, short-answer rows,
    and the slots that were actually loaded (a snapshot that lost an item between two reads would drop it)."""
    ids = [stored.question_ids[number - 1] for number in slots]
    loaded = {int(question["id"]): question for question in quiz_load_questions(conn, ids, stored.seed, include_correct=True)}
    question_explanations = fetch_question_explanations(conn, ids)
    option_explanations = fetch_option_explanations(conn, ids)
    short_ids = [question_id for number, question_id in zip(slots, ids) if stored.slots[number - 1].kind == KIND_SHORT_ANSWER]
    current_parts = _subquestions_by_question(conn, short_ids)

    multiple_choice, short_answers, shown = [], [], []
    for number, question_id in zip(slots, ids):
        question = loaded.get(question_id)
        if question is None:
            continue
        shown.append(number)
        parts = saved.by_slot[number]
        slot = stored.slots[number - 1]
        explanation = question_explanations.get(question_id)
        question["saved_slot_number"] = number  # the shared partial numbers its card by this key when a row has it
        question["kind"] = slot.kind
        question["points_possible"] = sum(part.points_possible for part in parts)
        question["points_awarded"] = sum(part.points_awarded for part in parts)
        if slot.kind == KIND_MULTIPLE_CHOICE:
            part = parts[0]
            chosen_text = correct_text = None
            for option in question["options"]:
                if option["option_letter"] == part.chosen_letter:
                    chosen_text = option["option_text"]
                if int(option.get("is_correct") or 0) == 1:
                    correct_text = option["option_text"]
            question["chosen_letter"] = part.chosen_letter
            question["chosen_text"] = chosen_text
            question["correct_text"] = correct_text or "—"
            question["is_correct"] = part.is_correct  # the saved verdict, never a comparison with current content
            question["feedback_explanation"] = (
                option_explanations.get((question_id, part.chosen_letter)) or explanation or QUIZ_NO_EXPLANATION_MESSAGE)
            multiple_choice.append(question)
            continue
        rows = {row["subquestion_number"]: row for row in current_parts[question_id]}
        question["grading_mode"] = _quiz_candidate_grading_mode(list(rows.values()))
        question["feedback_explanation"] = explanation or QUIZ_NO_EXPLANATION_MESSAGE
        question["parts"] = [
            {"subquestion_number": part.subquestion_number, "raw_answer": part.raw_answer,
             "is_correct": part.is_correct, "points_possible": part.points_possible,
             "points_awarded": part.points_awarded,
             "accepted_answers": _unique(_quiz_subquestion_accepted_answers(rows[part.subquestion_number]))}
            for part in parts]
        short_answers.append(question)
    return tuple(multiple_choice), tuple(short_answers), tuple(shown)


def _load_result(conn: sqlite3.Connection, user_id: int, attempt_id: int) -> Part1Result:
    """The account's own submitted attempt, inside the read snapshot."""
    if not _account_is_active(conn, user_id):
        return Part1Result(OUTCOME_ACCOUNT_UNAVAILABLE)
    if not 0 < attempt_id <= _SQLITE_MAX_INTEGER:
        return Part1Result(OUTCOME_NOT_FOUND)
    # The owner is part of the lookup: a missing and a foreign attempt are the same empty result.
    row = conn.execute(
        "SELECT id, seed, question_ids_json, slot_format_json, max_points_planned, duration_seconds, "
        "started_at, deadline_at, submit_cutoff_at, submitted_at, expired_at, abandoned_at, score_points, "
        "score_max FROM dzi_part1_attempts WHERE id = ? AND user_id = ?", (attempt_id, user_id)).fetchone()
    if row is None or row["submitted_at"] is None:  # missing, foreign, open, expired or abandoned
        return Part1Result(OUTCOME_NOT_FOUND)
    if row["expired_at"] is not None or row["abandoned_at"] is not None:
        raise _invalid_result(attempt_id, "submitted and also expired or abandoned")

    stored = parse_stored_attempt(row)  # plan, slot format, planned maximum, duration and instants, fail closed
    try:
        submitted = lifecycle.parse_timestamp(row["submitted_at"])
    except lifecycle.TimestampError:
        raise _invalid_result(attempt_id, "an unreadable submission time") from None
    if not stored.started_at <= submitted < stored.submit_cutoff_at:
        raise _invalid_result(attempt_id, "a submission time outside the attempt's stored window")
    score_points, score_max = row["score_points"], row["score_max"]
    if not _whole(score_points) or not _whole(score_max) or not 0 <= score_points <= score_max or score_max <= 0:
        raise _invalid_result(attempt_id, "a score that is not whole and bounded")
    if score_max > row["max_points_planned"]:
        raise _invalid_result(attempt_id, "a saved maximum above the planned maximum")

    saved = _saved_answers(conn, stored, score_points, score_max)
    slots = _review_slots(conn, stored, saved)
    multiple_choice, short_answers, reviewed = _review_rows(conn, stored, saved, slots)
    saved_numbers = tuple(saved.by_slot)
    return Part1Result(
        OUTCOME_LOADED, attempt_id, row["submitted_at"], row["started_at"], row["duration_seconds"],
        score_points, score_max, row["max_points_planned"], saved.multiple_choice_points, saved.multiple_choice_max,
        saved.short_answer_points, saved.short_answer_max, saved.grader_version, PART1_SLOT_COUNT, saved_numbers,
        tuple(number for number in range(1, PART1_SLOT_COUNT + 1) if number not in saved.by_slot), reviewed,
        tuple(number for number in saved_numbers if number not in reviewed), multiple_choice, short_answers)


def load_part1_result(conn: sqlite3.Connection, principal: Principal, attempt_id: int) -> Part1Result:
    """Load the principal's own submitted Part 1 attempt for review (module docstring, "Result (C3.4)").

    Read-only: no admission, clock read, marking or other write, and no transaction is left open. Raises
    :class:`Part1AccessError` for a principal that is not an account, ``ValueError`` for an ``attempt_id`` that is
    not an integer, ``RuntimeError`` for an unconfigured connection and :class:`Part1StateError` /
    :class:`Part1PlanError` for stored state it refuses to interpret. Any other database error propagates.
    """
    user_id = _account_id(principal)
    if type(attempt_id) is not int:
        raise ValueError("attempt_id must be an integer")
    _require_configured_connection(conn)
    return _in_read_snapshot(conn, lambda: _load_result(conn, user_id, attempt_id))
