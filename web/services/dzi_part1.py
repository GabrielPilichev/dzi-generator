"""DZI Part 1 simulation pools and the account-owned start/resume service (C3.2).

Governed by docs/DZI_PART1_SIMULATION_DESIGN.md (sections 3-7, 9 and 10) and the scope
docs/reviews/dzi-part1-start-service-scope.md, over the runtime tables of migration 027.
This module decides and writes the *start* of a timed attempt and nothing else: there is
no route, template, submit, grader, result loader, timer or exposure flag here, and no
existing caller, shared eligibility rule or public rendering rule is changed. Nothing
imports it yet, so it is dormant.

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
"""

from __future__ import annotations

import json
import random
import secrets
import sqlite3
from datetime import datetime, timedelta
from typing import NamedTuple

from src.dzi_open_questions import is_open_question_generation_ready
from src.subject_content import IT, canonical_subject, sql_chunks
from web.access import Principal, account_principal, can_take_quizzes
from web.services import dzi_exams, lifecycle
from web.services.question_visuals import QUESTION_VISUAL_STATIC_PREFIX, fetch_question_visual_assets
from web.services.quiz import (
    QUIZ_APPROVED_FILTER,
    _quiz_candidate_grading_mode,
    filter_renderable_attempt_question_ids,
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
    prompts = {}
    for chunk in sql_chunks(stored.question_ids):
        marks = ",".join("?" for _ in chunk)
        # SAFE: placeholders only; values use ? params.
        for row in conn.execute(f"SELECT id, prompt FROM questions WHERE id IN ({marks})", chunk):
            prompts[int(row["id"])] = row["prompt"]
    slots = []
    for number, (question_id, slot) in enumerate(zip(stored.question_ids, stored.slots), start=1):
        if question_id not in renderable:
            continue
        if slot.kind == KIND_MULTIPLE_CHOICE:
            if types.get(question_id) != KIND_MULTIPLE_CHOICE:
                continue
            # The shared helper also counts a path the loader never attaches; the same loader-based
            # check as at creation, for this one item (no pool, no exclusion list).
            if quiz_prompt_needs_visual(prompts.get(question_id)) and not _resolved_visual_exists(conn, question_id):
                continue
        else:
            if types.get(question_id) not in SHORT_ANSWER_QUESTION_TYPES \
                    or not part_weights_valid(parts.get(question_id, []), slot.points):
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
