"""Assignment lifecycle: deadlines, manual closure, the grace window and clock health.

PR #326 of ``docs/ASSIGNMENT_LIFECYCLE_DESIGN.md`` (section numbers below refer
to it). This is *the lifecycle module*: apart from the operator command
``src/manage_lifecycle.py``, it is the only place that assigns a lifecycle column
of ``quiz_assignments`` or a column of ``lifecycle_clock``, and nothing here ever
sets a terminal marker back to NULL.

What it owns:

* the strict timestamp serialiser and parser (3.4);
* the one pure state function, :func:`evaluate` (5.7);
* IANA timezone validation and the local-date arithmetic of deadlines (5.1-5.4);
* clock health: the shared ``lifecycle_clock`` row, the boot anchor, the checks
  of every lifecycle writer transaction and the single clock-fault bookkeeping
  write (3.6);
* the guarded writes: a new assignment's deadline, edit, manual close and the
  observation markers (6.1, 6.3, 6.6).

Attempt start, submission and practical upload live with their own data access
(``web.services.quiz``, ``web.blueprints.practical``) and take their transaction,
their ``now`` and their predicate from here.

Standard library only; never imports ``web.app`` or a blueprint. Nothing in this
module deletes anything: retention cleanup is PR #328 and does not exist yet.
"""

from __future__ import annotations

import functools
import logging
import os
import re
import sqlite3
import sys
import time as _time
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable, Iterable, NamedTuple
from zoneinfo import ZoneInfo, available_timezones

logger = logging.getLogger("learnpilot.lifecycle")

# ---------------------------------------------------------------------------
# Durations and limits
# ---------------------------------------------------------------------------

# G: the in-flight submission grace window (approved, P1). The only definition of
# the duration; every boundary and every notice derives from this name.
GRACE_SECONDS = 15 * 60
# R: the minimum retention of detailed attempt data after effective closure
# (decision C). Nothing in PR #326 deletes; the value only feeds the computed
# ``retention_eligible_at``.
RETENTION_SECONDS = 30 * 24 * 60 * 60

DEFAULT_TIMEZONE = "Europe/Sofia"          # editable preselection (I2)
DEFAULT_DEADLINE_DAYS = 14                 # calendar days ahead (P2)
DEFAULT_DEADLINE_TIME = time(23, 59)       # last usable minute shown to people (I4)
MAX_DEADLINE_DAYS = 180                    # from the date the deadline is set or edited (P3, I3)
# A stored deadline further ahead than validation could ever have accepted (the
# maximum plus the cutoff convention and the zone offset) is a data anomaly (3.9).
DEADLINE_ANOMALY_DAYS = MAX_DEADLINE_DAYS + 2

# Detection thresholds of the clock checks. They are not bounds on the clock's error.
CLOCK_REGRESSION_TOLERANCE_SECONDS = 5
CLOCK_STEP_TOLERANCE_SECONDS = 60
# How long after boot a missing synchronisation report is only refused; after it
# the absence is recorded as a clock fault.
CLOCK_SYNC_WAIT_SECONDS = 10 * 60
CLOCK_SEED = "1970-01-01 00:00:00"

# Fixed order of the hold reasons known to this phase (3.8). ``restore_reclean``
# belongs to PR #328 and is deliberately absent.
HOLD_CLOCK_REVIEW = "clock_review"
HOLD_RESTORE_PENDING = "restore_pending"
HOLD_RESTORE_UNRECONCILED = "restore_unreconciled"
HOLD_REASONS = (HOLD_CLOCK_REVIEW, HOLD_RESTORE_PENDING, HOLD_RESTORE_UNRECONCILED)

# While a restore is being carried out, or was left unfinished, a directory of
# this name sits next to the database: deploy/learnpilot-restore.sh makes it
# before it stops the service and removes it when the restore has finished or
# was undone. The database is neither served nor backed up while it is there.
# It only ever forbids; nothing takes its presence, or anything in it, as proof
# that a step was carried out.
RESTORE_LOCK_NAME = "restore-in-progress"


def restore_lock_path(db_path) -> str:
    """Where the restore lock of the database at ``db_path`` is, present or not."""
    return os.path.join(os.path.dirname(os.path.abspath(os.fspath(db_path))), RESTORE_LOCK_NAME)


def restore_lock_present(db_path) -> bool:
    return os.path.lexists(restore_lock_path(db_path))

TIMESTAMP_COLUMNS = (
    "deadline_at", "closed_at", "expiry_observed_at", "submissions_closed_at", "retention_base_at",
)
# Write-once facts that end something (3.3). ``closed_at`` is also a factual event.
TERMINAL_MARKER_COLUMNS = ("closed_at", "expiry_observed_at", "submissions_closed_at")
LIFECYCLE_COLUMNS = (
    "deadline_at", "display_timezone", "closed_at", "closed_by_user_id",
    "expiry_observed_at", "submissions_closed_at", "retention_base_at", "lifecycle_hold",
)

STATE_OPEN = "open"
STATE_CLOSED_MANUALLY = "closed_manually"
STATE_EXPIRED = "expired"
# Closed by a marker or a malformed value that names no cause (fail-safe, 3.4 rule 7).
STATE_CLOSED = "closed"

STATE_LABELS_BG = {
    STATE_OPEN: "отворен",
    STATE_CLOSED_MANUALLY: "затворен от учителя",
    STATE_EXPIRED: "изтекъл срок",
    STATE_CLOSED: "затворен",
}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class LifecycleUnavailable(RuntimeError):
    """The request cannot be served now; the web layer answers 503 (3.6, 3.8).

    ``reason`` is a short code for logs and operators, never shown to users.
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class ClockUnavailable(LifecycleUnavailable):
    """The clock row is missing, malformed or ``suspect``, a fault was just
    detected, or a new boot has no synchronisation evidence yet."""


class AssignmentHeld(LifecycleUnavailable):
    """``lifecycle_hold`` is not NULL: the assignment is not served."""

    def __init__(self):
        super().__init__("assignment_held")


class LifecycleInvariantError(RuntimeError):
    """A guarded write contradicted the row it was guarded by: a bug, never a state."""


class TimestampError(ValueError):
    """A value is not a strict lifecycle timestamp."""


class DeadlineError(ValueError):
    """A deadline or timezone input was refused; ``code`` selects the message."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


DEADLINE_ERROR_MESSAGES_BG = {
    "deadline_choice_required": "Избери краен срок или „без краен срок“.",
    "timezone_invalid": "Избери валидна часова зона от списъка.",
    "deadline_invalid": "Невалиден краен срок. Въведи дата и час във формат ГГГГ-ММ-ДД и ЧЧ:ММ.",
    "deadline_nonexistent": "Този час не съществува в избраната часова зона (смяна на лятно време). Избери друг час.",
    "deadline_in_past": "Крайният срок трябва да е в бъдещето.",
    "deadline_too_far": f"Крайният срок може да е най-много {MAX_DEADLINE_DAYS} дни напред.",
}


# ---------------------------------------------------------------------------
# Timestamps (3.4 rules 5 and 6): one serialiser, one strict parser
# ---------------------------------------------------------------------------

_TIMESTAMP_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}")


def format_timestamp(value: datetime) -> str:
    """``value`` as ``YYYY-MM-DD HH:MM:SS``: UTC, whole seconds, no ``T``, no offset."""
    if not isinstance(value, datetime):
        raise TypeError("a lifecycle timestamp is a datetime")
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return (f"{value.year:04d}-{value.month:02d}-{value.day:02d} "
            f"{value.hour:02d}:{value.minute:02d}:{value.second:02d}")


def parse_timestamp(text: object) -> datetime:
    """The naive UTC datetime of a stored lifecycle timestamp.

    Accepts exactly the format :func:`format_timestamp` writes and only
    calendar-valid values. The lenient ``quiz_parse_timestamp_utc`` is never used
    for a lifecycle decision.
    """
    if not isinstance(text, str) or _TIMESTAMP_RE.fullmatch(text) is None:
        raise TimestampError("not a lifecycle timestamp")
    try:
        return datetime(int(text[0:4]), int(text[5:7]), int(text[8:10]),
                        int(text[11:13]), int(text[14:16]), int(text[17:19]))
    except ValueError:
        raise TimestampError("not a calendar-valid lifecycle timestamp") from None


def _parse_optional(text: object) -> tuple[datetime | None, bool]:
    """``(value, ok)`` of a nullable stored timestamp; NULL is ``(None, True)``."""
    if text is None:
        return None, True
    try:
        return parse_timestamp(text), True
    except TimestampError:
        return None, False


# ---------------------------------------------------------------------------
# Timezones and deadlines (5.1-5.4). The server's own zone is never consulted.
# ---------------------------------------------------------------------------

# Names the zone database may list that are not a place: the first is whatever
# the host is set to, which is exactly what must never be used.
_NON_ZONE_NAMES = frozenset({"localtime", "posixrules", "Factory"})
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_TIME_RE = re.compile(r"[0-9]{2}:[0-9]{2}")

DEADLINE_CHOICE_DEADLINE = "deadline"
DEADLINE_CHOICE_NONE = "none"


class ZoneDataUnavailable(RuntimeError):
    """The IANA zone database cannot be read: forms fail, nothing falls back to UTC."""


@functools.lru_cache(maxsize=1)
def timezone_names() -> tuple[str, ...]:
    """Every selectable IANA zone name, sorted."""
    names = sorted(name for name in available_timezones() if name not in _NON_ZONE_NAMES)
    if not names:
        raise ZoneDataUnavailable("the IANA timezone database is not available")
    return tuple(names)


def is_valid_timezone(name: object) -> bool:
    return isinstance(name, str) and name in timezone_names()


def validate_timezone(name: object) -> str:
    if not is_valid_timezone(name):
        raise DeadlineError("timezone_invalid")
    return str(name)


@functools.lru_cache(maxsize=64)
def _zone(name: str) -> ZoneInfo:
    return ZoneInfo(name)


def _to_local(instant: datetime, zone_name: str) -> datetime:
    """The zone's wall-clock reading (naive) of a naive UTC instant."""
    return instant.replace(tzinfo=timezone.utc).astimezone(_zone(zone_name)).replace(tzinfo=None)


def local_today(now: datetime, zone_name: str) -> date:
    return _to_local(now, zone_name).date()


def _local_instants(local: datetime, zone_name: str) -> list[datetime]:
    """UTC instants at which the zone's clock reads ``local``, earliest first.

    Empty for a time that does not exist (spring gap), two for one that occurs
    twice (autumn overlap), otherwise one.
    """
    zone = _zone(zone_name)
    instants: list[datetime] = []
    for fold in (0, 1):
        instant = local.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc)
        if instant.astimezone(zone).replace(tzinfo=None) == local:
            naive = instant.replace(tzinfo=None)
            if naive not in instants:
                instants.append(naive)
    return sorted(instants)


def _first_instant_after_gap(local: datetime, zone_name: str) -> datetime:
    """The first valid instant after the gap that swallowed ``local``."""
    zone = _zone(zone_name)
    candidates = sorted(
        local.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc) for fold in (0, 1)
    )
    low, high = candidates[0], candidates[1]
    after = high.astimezone(zone).utcoffset()
    # Smallest whole second in (low, high] that already has the offset after the change.
    span = int((high - low).total_seconds())
    lo, hi = 0, span
    while lo < hi:
        mid = (lo + hi) // 2
        if (low + timedelta(seconds=mid)).astimezone(zone).utcoffset() == after:
            hi = mid
        else:
            lo = mid + 1
    return (low + timedelta(seconds=lo)).replace(tzinfo=None)


def deadline_cutoff(displayed_date: date, displayed_time: time, zone_name: str, *, typed: bool) -> datetime:
    """The exclusive UTC cutoff of a deadline shown as "through HH:MM" (I4).

    The displayed minute is the last usable one. The cutoff is the local
    wall-clock time one minute later, constructed directly: "through 23:59" on a
    date is local midnight starting the next date. Only that local time is then
    converted to UTC: one that occurs twice (autumn overlap) is the earlier
    instant, one that does not exist (spring gap) moves forward to the first valid
    instant.

    The cutoff is never derived by converting the displayed minute and adding an
    elapsed minute. Where the clock is set back at midnight (Africa/Cairo in
    October) 23:59 occurs twice, and a minute after its first occurrence is an
    hour before the next local midnight.

    A displayed time that does not exist is refused when a person typed it.
    """
    displayed = datetime.combine(displayed_date, displayed_time)
    if typed and not _local_instants(displayed, zone_name):
        raise DeadlineError("deadline_nonexistent")
    cutoff_local = displayed + timedelta(minutes=1)  # wall-clock arithmetic, not elapsed time
    instants = _local_instants(cutoff_local, zone_name)
    if instants:
        return instants[0]
    return _first_instant_after_gap(cutoff_local, zone_name)


def default_displayed_deadline(now: datetime, zone_name: str) -> tuple[date, time]:
    """23:59 on today's local date + 14 calendar days: date arithmetic, never ``now + 336 h``."""
    return local_today(now, zone_name) + timedelta(days=DEFAULT_DEADLINE_DAYS), DEFAULT_DEADLINE_TIME


def displayed_deadline(cutoff: datetime, zone_name: str) -> datetime:
    """The last usable local minute of a stored cutoff (naive, in the zone)."""
    return _to_local(cutoff - timedelta(minutes=1), zone_name)


class DeadlineRequest(NamedTuple):
    """A validated-in-shape deadline choice; not yet checked against ``now``."""

    timezone: str
    has_deadline: bool
    displayed_date: date | None = None
    displayed_time: time | None = None


def parse_deadline_request(form) -> DeadlineRequest:
    """The explicit deadline choice of a create, duplicate or edit form (5.4).

    Reads exactly four fields. The choice must be stated: there is no server
    fallback, a missing or unknown choice is an error. A zone is always required.
    Date and time parse strictly (no offset, no ``T``, no seconds, real calendar
    values only).
    """
    choice = form.get("deadline_choice")
    if choice not in (DEADLINE_CHOICE_DEADLINE, DEADLINE_CHOICE_NONE):
        raise DeadlineError("deadline_choice_required")
    zone_name = validate_timezone(form.get("display_timezone"))
    if choice == DEADLINE_CHOICE_NONE:
        return DeadlineRequest(zone_name, False)

    raw_date, raw_time = form.get("deadline_date"), form.get("deadline_time")
    if (not isinstance(raw_date, str) or _DATE_RE.fullmatch(raw_date) is None
            or not isinstance(raw_time, str) or _TIME_RE.fullmatch(raw_time) is None):
        raise DeadlineError("deadline_invalid")
    try:
        displayed_date = date(int(raw_date[0:4]), int(raw_date[5:7]), int(raw_date[8:10]))
        displayed_time = time(int(raw_time[0:2]), int(raw_time[3:5]))
    except ValueError:
        raise DeadlineError("deadline_invalid") from None
    return DeadlineRequest(zone_name, True, displayed_date, displayed_time)


def resolve_deadline(request: DeadlineRequest, now: datetime) -> str | None:
    """The ``deadline_at`` to store for ``request`` at lifecycle time ``now``, or ``None``.

    Upper bound: the local date of the displayed deadline is at most today's
    local date + 180 calendar days, compared before any conversion to UTC, so a
    DST change cannot move the boundary. Lower bound: the stored cutoff is
    strictly later than ``now``. "No deadline" is subject to neither.
    """
    if not request.has_deadline:
        return None
    try:
        latest = local_today(now, request.timezone) + timedelta(days=MAX_DEADLINE_DAYS)
        if request.displayed_date > latest:
            raise DeadlineError("deadline_too_far")
        cutoff = deadline_cutoff(request.displayed_date, request.displayed_time,
                                 request.timezone, typed=True)
    except OverflowError:
        raise DeadlineError("deadline_invalid") from None
    if cutoff <= now:
        raise DeadlineError("deadline_in_past")
    return format_timestamp(cutoff)


# ---------------------------------------------------------------------------
# Holds (3.8): a set of reasons in one column; any non-NULL value holds
# ---------------------------------------------------------------------------

def parse_hold(raw: object) -> tuple[tuple[str, ...], bool]:
    """``(reasons, well_formed)`` of a stored ``lifecycle_hold``.

    ``well_formed`` is false for anything that is not the known reasons, each
    once, in the fixed order. Such a value still holds its assignment.
    """
    if raw is None:
        return (), True
    if not isinstance(raw, str):
        return (), False
    reasons = tuple(raw.split(","))
    known = tuple(reason for reason in HOLD_REASONS if reason in reasons)
    return reasons, bool(reasons) and reasons == known


def is_held(row) -> bool:
    return row["lifecycle_hold"] is not None


def require_served(row) -> None:
    """Refuse a held assignment. Call it after authorization, never before (3.8)."""
    raw = row["lifecycle_hold"]
    if raw is None:
        return
    _reasons, well_formed = parse_hold(raw)
    if not well_formed:
        logger.error("lifecycle: assignment %s has an unreadable lifecycle_hold; it stays held", row["id"])
    raise AssignmentHeld()


# ---------------------------------------------------------------------------
# State (3.2, 3.3): the one pure function every route, template and tool uses
# ---------------------------------------------------------------------------

class LifecycleView(NamedTuple):
    """Everything derived from one assignment row at lifecycle time ``now``."""

    now: datetime
    state: str
    is_closed: bool
    in_grace: bool                 # closed, submissions not ended: now < E + G
    submissions_ended: bool        # closed and no submission can be accepted any more
    is_held: bool
    malformed: tuple[str, ...]     # timestamp columns that do not parse (fail safe)
    deadline_at: datetime | None
    closed_at: datetime | None
    expiry_observed_at: datetime | None
    submissions_closed_at: datetime | None
    retention_base_at: datetime | None
    effective_closure: datetime | None       # E
    submit_cutoff: datetime | None           # E + G
    retention_eligible_at: datetime | None   # max(E, retention_base_at) + R, by the server's clock
    display_timezone: str | None


def evaluate(row, now: datetime) -> LifecycleView:
    """The lifecycle state of an assignment row at ``now``. Pure: no clock, no database.

    ``E`` is the earliest of ``deadline_at`` and ``closed_at``; neither
    observation marker is an input to it. A timestamp that does not parse makes
    the assignment closed for students, frozen for edits and never eligible for
    cleanup (3.4 rule 7).
    """
    parsed: dict[str, datetime | None] = {}
    malformed = []
    for column in TIMESTAMP_COLUMNS:
        value, ok = _parse_optional(row[column])
        parsed[column] = value
        if not ok:
            malformed.append(column)

    deadline_at, closed_at = parsed["deadline_at"], parsed["closed_at"]
    candidates = [value for value in (deadline_at, closed_at) if value is not None]
    effective_closure = min(candidates) if candidates and not malformed else None

    expired = row["expiry_observed_at"] is not None or (deadline_at is not None and now >= deadline_at)
    if malformed:
        state = STATE_CLOSED
    elif row["closed_at"] is not None:
        state = STATE_CLOSED_MANUALLY
    elif expired:
        state = STATE_EXPIRED
    elif row["submissions_closed_at"] is not None:
        state = STATE_CLOSED
    else:
        state = STATE_OPEN
    is_closed = state != STATE_OPEN

    submit_cutoff = retention_eligible_at = None
    if effective_closure is not None:
        submit_cutoff = effective_closure + timedelta(seconds=GRACE_SECONDS)
        base = parsed["retention_base_at"]
        start = effective_closure if base is None else max(effective_closure, base)
        retention_eligible_at = start + timedelta(seconds=RETENTION_SECONDS)

    in_grace = (
        is_closed and not malformed
        and row["submissions_closed_at"] is None
        and submit_cutoff is not None and now < submit_cutoff
    )
    return LifecycleView(
        now=now,
        state=state,
        is_closed=is_closed,
        in_grace=in_grace,
        submissions_ended=is_closed and not in_grace,
        is_held=row["lifecycle_hold"] is not None,
        malformed=tuple(malformed),
        deadline_at=deadline_at,
        closed_at=closed_at,
        expiry_observed_at=parsed["expiry_observed_at"],
        submissions_closed_at=parsed["submissions_closed_at"],
        retention_base_at=parsed["retention_base_at"],
        effective_closure=effective_closure,
        submit_cutoff=submit_cutoff,
        retention_eligible_at=retention_eligible_at,
        display_timezone=row["display_timezone"],
    )


def attempt_in_flight(view: LifecycleView, started_at: object) -> bool:
    """Whether an attempt was started before the closure of its assignment (7.1).

    Decided by the order of the two writer transactions; the stored values only
    record it: ``started_at < deadline_at`` for a deadline closure,
    ``started_at <= closed_at`` for a manual one. An equal second with a manual
    close can only mean "started first". A malformed ``started_at`` is never in
    flight. No stored time is adjusted to make either comparison come out.
    """
    try:
        started = parse_timestamp(started_at)
    except TimestampError:
        return False
    closure = view.effective_closure
    if closure is None:
        return False
    manual = view.closed_at is not None and (view.deadline_at is None or view.closed_at < view.deadline_at)
    return started <= closure if manual else started < closure


def attempt_submittable(view: LifecycleView, attempt) -> bool:
    """The submittable rule of 3.3 for an attempt row, evaluated at ``view.now``.

    Says nothing about holds or clock health: those refuse the request before
    this is asked.
    """
    if attempt["submitted_at"] is not None:
        return False
    if not view.is_closed:
        return True
    return view.in_grace and attempt_in_flight(view, attempt["started_at"])


def needs_observation(view: LifecycleView) -> bool:
    """Whether a writer transaction would have a marker to stamp for this view (6.3)."""
    if view.is_held or view.malformed:
        return False
    expiry_unstamped = (
        view.state == STATE_EXPIRED and view.closed_at is None and view.expiry_observed_at is None
    )
    grace_end_unstamped = (
        view.submit_cutoff is not None and view.submissions_closed_at is None
        and view.now >= view.submit_cutoff
    )
    return expiry_unstamped or grace_end_unstamped


# ---------------------------------------------------------------------------
# Presentation: lifecycle times in the assignment's stored zone, the zone named
# ---------------------------------------------------------------------------

def display_zone(view_or_name) -> tuple[str, bool]:
    """``(zone, is_stored)``: the stored zone, or the application default for a row
    created before migration 018 (or whose zone the database no longer knows)."""
    name = view_or_name.display_timezone if isinstance(view_or_name, LifecycleView) else view_or_name
    if is_valid_timezone(name):
        return str(name), True
    return DEFAULT_TIMEZONE, False


def _local_text(instant: datetime | None, zone_name: str) -> str | None:
    if instant is None:
        return None
    return _to_local(instant, zone_name).strftime("%d.%m.%Y %H:%M")


def describe(view: LifecycleView) -> dict:
    """What templates show about a lifecycle view. Display only; decides nothing."""
    zone_name, zone_is_stored = display_zone(view)
    deadline = None
    if view.deadline_at is not None:
        shown = displayed_deadline(view.deadline_at, zone_name)
        deadline = {
            "text": f"до {shown.strftime('%H:%M')} на {shown.strftime('%d.%m.%Y')}",
            "form_date": shown.strftime("%Y-%m-%d"),
            "form_time": shown.strftime("%H:%M"),
        }
    return {
        "state": view.state,
        "state_label": STATE_LABELS_BG[view.state],
        "is_open": not view.is_closed,
        "is_closed": view.is_closed,
        "in_grace": view.in_grace,
        "submissions_ended": view.submissions_ended,
        "malformed": bool(view.malformed),
        "zone": zone_name,
        "zone_is_stored": zone_is_stored,
        "deadline": deadline,
        "closed_at": _local_text(view.closed_at, zone_name),
        "effective_closure": _local_text(view.effective_closure, zone_name),
        "submit_cutoff": _local_text(view.submit_cutoff, zone_name),
        "grace_minutes": GRACE_SECONDS // 60,
    }


def default_deadline_form(now: datetime, zone_name: str) -> dict:
    """The prefilled deadline of a create or duplicate form: an offer, not a fallback."""
    shown_date, shown_time = default_displayed_deadline(now, zone_name)
    return {
        "choice": DEADLINE_CHOICE_DEADLINE,
        "form_date": shown_date.strftime("%Y-%m-%d"),
        "form_time": shown_time.strftime("%H:%M"),
        "zone": zone_name,
        "max_days": MAX_DEADLINE_DAYS,
    }


def preselected_timezone(conn: sqlite3.Connection, user_id: int | None) -> str:
    """The zone of the account's most recent assignment, otherwise the default (I2)."""
    if user_id is not None:
        row = conn.execute(
            "SELECT display_timezone FROM quiz_assignments "
            "WHERE owner_user_id = ? AND display_timezone IS NOT NULL "
            "ORDER BY created_at DESC, id DESC LIMIT 1", (user_id,)).fetchone()
        if row is not None and is_valid_timezone(row[0]):
            return str(row[0])
    return DEFAULT_TIMEZONE


# ---------------------------------------------------------------------------
# Clock evidence that does not come from the wall clock itself (3.6)
# ---------------------------------------------------------------------------

class ClockEnvironment:
    """The host's boot identity, boot-time clock and synchronisation report.

    ``boot_id`` and ``boottime_ms`` let a check compare the wall clock with
    elapsed real time since the boot anchor. ``synchronized`` is the operating
    system's own claim that the clock is held against external servers: ``True``,
    ``False``, or ``None`` when it cannot be read. It is required before the
    first lifecycle write of a boot and is never proof of correct UTC.
    """

    name = "abstract"

    def boot_id(self) -> str | None:
        raise NotImplementedError

    def boottime_ms(self) -> int | None:
        raise NotImplementedError

    def synchronized(self) -> bool | None:
        raise NotImplementedError


def _linux_ntp_synchronized() -> bool | None:
    """``adjtimex(2)`` read-only: what ``timedatectl`` reports as "System clock synchronized".

    No privilege, no subprocess and no D-Bus is needed, so the probe works under
    the service unit's sandboxing. ``None`` when the call is unavailable or fails.
    """
    try:
        import ctypes
        import ctypes.util

        class _TimexHead(ctypes.Structure):
            _fields_ = [("modes", ctypes.c_uint), ("offset", ctypes.c_long),
                        ("freq", ctypes.c_long), ("maxerror", ctypes.c_long)]

        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
        # Larger than any struct timex, zeroed: modes = 0 reads and changes nothing.
        buffer = ctypes.create_string_buffer(1024)
        libc.adjtimex.argtypes = [ctypes.c_void_p]
        libc.adjtimex.restype = ctypes.c_int
        if libc.adjtimex(buffer) < 0:
            return None
        # The kernel reports the maximum error as 16 s while unsynchronised.
        return _TimexHead.from_buffer(buffer).maxerror < 16_000_000
    except Exception:  # an unreadable probe is "cannot be read", never a crash
        return None


class SystemClockEnvironment(ClockEnvironment):
    """The real host. Complete on Linux (the production platform); elsewhere the
    boot identity and the synchronisation report are unavailable, so lifecycle
    writes need the operator confirmation of 3.7."""

    name = "system"
    BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"

    def boot_id(self) -> str | None:
        try:
            with open(self.BOOT_ID_PATH, encoding="ascii") as handle:
                value = handle.read().strip()
        except (OSError, ValueError):
            return None
        return value or None

    def boottime_ms(self) -> int | None:
        clock = getattr(_time, "CLOCK_BOOTTIME", None)
        if clock is None:
            return None
        try:
            return int(_time.clock_gettime(clock) * 1000)
        except OSError:
            return None

    def synchronized(self) -> bool | None:
        if not sys.platform.startswith("linux"):
            return None
        return _linux_ntp_synchronized()


class DevelopmentClockEnvironment(SystemClockEnvironment):
    """Development and manual smoke testing only (``web.app`` imported directly).

    A developer machine has no synchronisation report to read, so the absence of
    one does not stop a local run. Regression and, where the platform has a boot
    identity, step detection still apply. Production never uses this:
    ``web.production.create_production_app`` installs the system environment.
    """

    name = "development"

    def synchronized(self) -> bool | None:
        return True


def naive_utc_now() -> datetime:
    """The current UTC time as a naive ``datetime``, microseconds kept.

    The default wall clock, and the unfrozen clock of the application and its tests.
    The instant is sampled once, with the zone named explicitly, and only then is
    ``tzinfo`` dropped: the value is UTC, never server-local time.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


_environment: ClockEnvironment = SystemClockEnvironment()
_wall_clock: Callable[[], datetime] = naive_utc_now


def configure_clock_environment(environment: ClockEnvironment) -> None:
    global _environment
    _environment = environment


def clock_environment() -> ClockEnvironment:
    return _environment


def configure_wall_clock(reader: Callable[[], datetime]) -> None:
    """Register the application clock (naive UTC), evaluated on every use.

    ``web.services.quiz`` registers ``quiz_current_timestamp`` so that lifecycle
    time and the rest of the quiz code read one clock.
    """
    global _wall_clock
    _wall_clock = reader


def wall_reading() -> datetime:
    """The wall clock as it reads now: naive UTC, whole seconds. Not yet lifecycle time."""
    value = _wall_clock()
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.replace(microsecond=0)


# ---------------------------------------------------------------------------
# The shared clock row
# ---------------------------------------------------------------------------

CLOCK_STATE_OK = "ok"
CLOCK_STATE_SUSPECT = "suspect"

CLOCK_REASON_REGRESSION = "clock_regression"
CLOCK_REASON_STEP = "clock_step"
CLOCK_REASON_FUTURE_TIMESTAMP = "future_timestamp"
CLOCK_REASON_NO_SYNC = "no_sync_evidence"
CLOCK_REASON_RESTORE = "restore"


class ClockRowError(RuntimeError):
    """``lifecycle_clock`` has no row, more than one, or a malformed one."""


class ClockRow(NamedTuple):
    high_water_at: datetime
    state: str
    state_reason: str | None
    state_changed_at: str | None
    ref_wall_at: datetime | None
    ref_boot_id: str | None
    ref_boottime_ms: int | None

    @property
    def has_anchor(self) -> bool:
        return self.ref_boot_id is not None


def read_clock_row(conn: sqlite3.Connection) -> ClockRow:
    """The single clock-health row, or :class:`ClockRowError`.

    A missing row, a second row or a malformed value is itself a clock-health
    failure. It is never repaired here: only the operator confirmation of 3.7
    writes a clock row.
    """
    try:
        rows = conn.execute(
            "SELECT id, high_water_at, state, state_reason, state_changed_at, "
            "ref_wall_at, ref_boot_id, ref_boottime_ms FROM lifecycle_clock").fetchall()
    except sqlite3.Error as exc:
        raise ClockRowError(f"lifecycle_clock cannot be read ({type(exc).__name__})") from None
    if len(rows) != 1:
        raise ClockRowError(f"lifecycle_clock has {len(rows)} rows, expected exactly one")
    row_id, high_water, state, reason, changed_at, ref_wall, ref_boot, ref_boottime = tuple(rows[0])
    if row_id != 1:
        raise ClockRowError("lifecycle_clock row has an unexpected id")
    try:
        high_water_at = parse_timestamp(high_water)
        ref_wall_at = None if ref_wall is None else parse_timestamp(ref_wall)
        if changed_at is not None:
            parse_timestamp(changed_at)
    except TimestampError:
        raise ClockRowError("lifecycle_clock holds a malformed timestamp") from None
    if state not in (CLOCK_STATE_OK, CLOCK_STATE_SUSPECT):
        raise ClockRowError("lifecycle_clock holds an unknown state")
    anchor_parts = (ref_wall is not None, ref_boot is not None, ref_boottime is not None)
    if any(anchor_parts) and not all(anchor_parts):
        raise ClockRowError("lifecycle_clock holds an incomplete boot anchor")
    if all(anchor_parts) and (
        not isinstance(ref_boot, str) or not ref_boot
        or isinstance(ref_boottime, bool) or not isinstance(ref_boottime, int) or ref_boottime < 0
    ):
        raise ClockRowError("lifecycle_clock holds a malformed boot anchor")
    return ClockRow(high_water_at, state, reason, changed_at, ref_wall_at, ref_boot, ref_boottime)


def clock_row_problem(conn: sqlite3.Connection) -> str | None:
    """Why the clock row is unusable, or ``None``. Read-only; for start-up and reports."""
    try:
        read_clock_row(conn)
    except ClockRowError as exc:
        return str(exc)
    return None


class ReadClock(NamedTuple):
    """Lifecycle time for a read-only page.

    ``now`` never runs behind the high-water mark, so a page cannot show an
    assignment as open again after the clock moved back. ``healthy`` is false
    whenever a lifecycle write would currently be refused for the clock; pages
    then show persisted state and offer no write.
    """

    now: datetime
    healthy: bool


def read_clock(conn: sqlite3.Connection) -> ReadClock:
    wall = wall_reading()
    try:
        row = read_clock_row(conn)
    except ClockRowError:
        return ReadClock(wall, False)
    behind = (row.high_water_at - wall).total_seconds()
    return ReadClock(max(wall, row.high_water_at),
                     row.state == CLOCK_STATE_OK and behind <= CLOCK_REGRESSION_TOLERANCE_SECONDS)


@contextmanager
def read_snapshot(conn: sqlite3.Connection):
    """All reads of one response in one read transaction (11.7).

    Under WAL that is one snapshot: the response is entirely before or entirely
    after any concurrent commit, authorization included. It takes no writer lock
    and commits nothing.
    """
    conn.execute("BEGIN")
    try:
        yield
    finally:
        try:
            if conn.in_transaction:
                conn.rollback()
        except sqlite3.ProgrammingError:  # the caller already closed the connection
            pass


# ---------------------------------------------------------------------------
# Lifecycle writer transactions (3.4, 3.6, 11.1)
# ---------------------------------------------------------------------------

def _record_clock_fault(conn: sqlite3.Connection, reason: str, wall: datetime, *, clean: bool) -> None:
    """The one write permitted after a clock fault is found (3.6).

    One statement on one row: state, reason and the wall reading as observed. It
    writes no ``quiz_*`` row, no high-water mark, no anchor, no marker and no
    hold, and changes nothing once the state is ``suspect``. Anything the
    detecting transaction wrote before is rolled back first.
    """
    try:
        if not clean:
            conn.rollback()
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE lifecycle_clock SET state = 'suspect', state_reason = ?, state_changed_at = ? "
            "WHERE id = 1 AND state = 'ok'", (reason, format_timestamp(wall)))
        conn.commit()
    except sqlite3.Error as exc:
        # The request is refused either way; the next lifecycle transaction
        # detects the fault again if it persists.
        if conn.in_transaction:
            conn.rollback()
        logger.error("lifecycle: clock fault %s could not be recorded (%s)", reason, type(exc).__name__)
    logger.error("lifecycle: clock fault detected: %s; lifecycle writes are refused until an "
                 "operator confirms the clock", reason)


class LifecycleWrite:
    """One lifecycle writer transaction: the writer lock is held and ``now`` was
    sampled after it was taken, by a clock that passed the health check.

    Nothing may be written before :meth:`admit` (or :meth:`examine`, which calls
    it): until then a refusal leaves no trace, and a clock fault commits nothing
    but its own bookkeeping.
    """

    def __init__(self, conn: sqlite3.Connection, now: datetime, wall: datetime, new_anchor):
        self.conn = conn
        self.now = now
        self.now_text = format_timestamp(now)
        self._wall = wall
        self._new_anchor = new_anchor
        self._admitted = False
        self.discarded = False

    def refuse_if_held(self, assignment_row) -> None:
        """Step two of every writer transaction: a held assignment is refused
        before its own timestamps are compared with the clock."""
        if assignment_row["lifecycle_hold"] is None:
            return
        self.conn.rollback()
        require_served(assignment_row)

    def check_factual(self, values: Iterable[object]) -> None:
        """Step three: no factual event timestamp or terminal marker of the rows
        being acted on may be later than ``now``. A scheduled deadline is never
        passed here; one in the future is the normal state of an open assignment.
        Values that do not parse are not a clock finding (they fail safe as closed).
        """
        for raw in values:
            value, ok = _parse_optional(raw)
            if ok and value is not None and value > self.now:
                _record_clock_fault(self.conn, CLOCK_REASON_FUTURE_TIMESTAMP, self._wall,
                                    clean=not self._admitted)
                raise ClockUnavailable(CLOCK_REASON_FUTURE_TIMESTAMP)

    def admit(self) -> None:
        """Raise the high-water mark to ``now`` and, on the first lifecycle write
        of a boot, write the boot anchor. An ordinary write never moves the anchor."""
        if self._admitted:
            return
        if self._new_anchor is None:
            self.conn.execute(
                "UPDATE lifecycle_clock SET high_water_at = ? WHERE id = 1 AND state = 'ok'",
                (self.now_text,))
        else:
            boot_id, boottime_ms = self._new_anchor
            self.conn.execute(
                "UPDATE lifecycle_clock SET high_water_at = ?, ref_wall_at = ?, ref_boot_id = ?, "
                "ref_boottime_ms = ? WHERE id = 1 AND state = 'ok'",
                (self.now_text, format_timestamp(self._wall), boot_id, boottime_ms))
        self._admitted = True

    def examine(self, assignment_row, *, attempt_row=None, factual: Iterable[object] = (),
                stamp: bool = True) -> LifecycleView:
        """Hold, factual timestamps, admission, state: in that fixed order (3.6).

        Then, unless ``stamp`` is false, the observation markers: a passed
        deadline and an ended grace window are recorded by this transaction,
        whatever it goes on to decide. A submission or an upload that is
        *accepted* in the grace window has observed the expiry just as much as a
        refused one, and the marker commits atomically with what it wrote. Without
        it the expiry would exist only as a comparison with the clock, and a later
        clock recovery could compute the assignment as open again.

        The returned view describes the row as it was read, before the stamps. A
        caller that ends with ``discard()`` discards the stamps too.
        """
        self.refuse_if_held(assignment_row)
        values = [assignment_row[column] for column in TERMINAL_MARKER_COLUMNS]
        if attempt_row is not None:
            values += [attempt_row["started_at"], attempt_row["submitted_at"]]
        values += list(factual)
        self.check_factual(values)
        self.admit()
        view = evaluate(assignment_row, self.now)
        if view.malformed:
            logger.error("lifecycle: assignment %s has malformed lifecycle timestamps (%s); "
                         "it is treated as closed", assignment_row["id"], ", ".join(view.malformed))
        elif stamp:
            stamp_observations(self, assignment_row["id"], view)
        return view

    def discard(self) -> None:
        """End the transaction without committing anything, the high-water mark included."""
        self.discarded = True


def _begin_write(conn: sqlite3.Connection) -> LifecycleWrite:
    if conn.in_transaction:
        raise RuntimeError("a lifecycle write must start outside a transaction")
    conn.execute("BEGIN IMMEDIATE")
    try:
        return _check_clock(conn)
    except BaseException:
        # Refused or failed: the writer lock is released and nothing stays open.
        # (A recorded clock fault has already committed its own bookkeeping.)
        if conn.in_transaction:
            conn.rollback()
        raise


def _check_clock(conn: sqlite3.Connection) -> LifecycleWrite:
    """The clock-health check of 3.6, under the writer lock. First of the three
    fixed steps of every writer transaction; the hold and the factual timestamps
    follow in :meth:`LifecycleWrite.examine`."""
    try:
        row = read_clock_row(conn)
    except ClockRowError as exc:
        conn.rollback()
        logger.error("lifecycle: %s; lifecycle writes are refused", exc)
        raise ClockUnavailable("clock_row_invalid") from None
    if row.state != CLOCK_STATE_OK:
        conn.rollback()
        raise ClockUnavailable("clock_suspect")

    wall = wall_reading()  # sampled after the lock: waiting for it counts against the request
    behind = (row.high_water_at - wall).total_seconds()
    if behind > CLOCK_REGRESSION_TOLERANCE_SECONDS:
        _record_clock_fault(conn, CLOCK_REASON_REGRESSION, wall, clean=True)
        raise ClockUnavailable(CLOCK_REASON_REGRESSION)
    now = max(wall, row.high_water_at)

    environment = _environment
    boot_id, boottime_ms = environment.boot_id(), environment.boottime_ms()
    new_anchor = None
    if row.has_anchor and boot_id is not None and boottime_ms is not None and row.ref_boot_id == boot_id:
        # Same boot: the wall clock against the anchor plus elapsed boot time. The
        # difference is the sum of every step since the anchor, in either direction.
        elapsed_ms = boottime_ms - row.ref_boottime_ms
        expected = row.ref_wall_at + timedelta(milliseconds=elapsed_ms)
        if elapsed_ms < 0 or abs((wall - expected).total_seconds()) > CLOCK_STEP_TOLERANCE_SECONDS:
            _record_clock_fault(conn, CLOCK_REASON_STEP, wall, clean=True)
            raise ClockUnavailable(CLOCK_REASON_STEP)
    else:
        # A new boot, or no anchor yet: no monotonic comparison is possible, so the
        # operating system's synchronisation report is required first.
        if environment.synchronized() is not True:
            if boottime_ms is not None and boottime_ms > CLOCK_SYNC_WAIT_SECONDS * 1000:
                _record_clock_fault(conn, CLOCK_REASON_NO_SYNC, wall, clean=True)
            else:
                conn.rollback()
            raise ClockUnavailable(CLOCK_REASON_NO_SYNC)
        if boot_id is not None and boottime_ms is not None:
            new_anchor = (boot_id, boottime_ms)
    return LifecycleWrite(conn, now, wall, new_anchor)


def begin_write(conn: sqlite3.Connection) -> LifecycleWrite:
    """``BEGIN IMMEDIATE`` and the clock-health check, leaving the transaction open.

    For the one caller that must keep the commit with *its* caller
    (``quiz_create_assignment`` without a ``transaction``): the writer lock is held and
    ``now`` was sampled after it, and whoever called must commit or roll back. Everything
    else uses :func:`write_transaction`, which ends the transaction itself.
    """
    return _begin_write(conn)


@contextmanager
def write_transaction(conn: sqlite3.Connection):
    """``BEGIN IMMEDIATE``, the clock-health check, then the caller's guarded writes.

    Commits on a normal exit, unless the caller called ``discard()`` or already
    ended the transaction; rolls back on any exception. Raises
    :class:`ClockUnavailable` before yielding when the clock cannot be used.
    """
    transaction = _begin_write(conn)
    try:
        yield transaction
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    if conn.in_transaction:
        if transaction.discarded:
            conn.rollback()
        else:
            conn.commit()


# ---------------------------------------------------------------------------
# Guarded writes. Every statement restates its precondition in its WHERE clause.
# ---------------------------------------------------------------------------

# OPEN(:now), written once (11.1). ``lifecycle_hold IS NULL`` travels with it so
# that no write can land on a held assignment.
#
# The part of it that says "not closed, not expired", without the hold, is written here too
# and only here: the creation quota counts a held assignment that is otherwise open as open
# (docs/reviews/EMAIL_REGISTRATION_PR7_SCOPE.md, I2). OPEN_PREDICATE_SQL is that text with
# the hold test in front, unchanged.
UNCLOSED_STATE_SQL = (
    "closed_at IS NULL"
    " AND expiry_observed_at IS NULL"
    " AND submissions_closed_at IS NULL"
    " AND (deadline_at IS NULL OR deadline_at > :now)"
)
OPEN_PREDICATE_SQL = "lifecycle_hold IS NULL AND " + UNCLOSED_STATE_SQL

_LIFECYCLE_ROW_SQL = "SELECT id, " + ", ".join(LIFECYCLE_COLUMNS) + " FROM quiz_assignments WHERE id = ?"


def fetch_lifecycle_row(conn: sqlite3.Connection, assignment_id: int) -> dict | None:
    """The assignment's id and lifecycle columns by name, whatever row factory
    the connection uses (the application's, an operator tool's, a test's)."""
    row = conn.execute(_LIFECYCLE_ROW_SQL, (assignment_id,)).fetchone()
    if row is None:
        return None
    return dict(zip(("id", *LIFECYCLE_COLUMNS), tuple(row)))


def count_unclosed_owned(conn: sqlite3.Connection, owner_user_id: int, now_text: str) -> int:
    """How many assignments of one account are not closed and not expired at ``now_text``.

    A held assignment that is otherwise open is counted (``UNCLOSED_STATE_SQL`` has no hold
    test). Rows with no owner belong to no account and are never counted. Reads only; the
    caller holds the writer lock when the answer decides a write.
    """
    row = conn.execute(
        "SELECT COUNT(*) FROM quiz_assignments WHERE owner_user_id = :owner AND " + UNCLOSED_STATE_SQL,
        {"owner": owner_user_id, "now": now_text}).fetchone()
    return int(row[0])


def set_new_assignment_deadline(conn: sqlite3.Connection, assignment_id: int, *,
                                deadline_at: str | None, display_timezone: str) -> None:
    """Give a row inserted in this transaction its validated deadline and zone.

    Both values come from the serialiser and the zone validator, never from raw
    form text. Applies only to a row that has no lifecycle state at all.
    """
    if deadline_at is not None:
        parse_timestamp(deadline_at)
    validate_timezone(display_timezone)
    cursor = conn.execute(
        "UPDATE quiz_assignments SET deadline_at = ?, display_timezone = ? "
        "WHERE id = ? AND deadline_at IS NULL AND display_timezone IS NULL AND closed_at IS NULL "
        "AND expiry_observed_at IS NULL AND submissions_closed_at IS NULL "
        "AND retention_base_at IS NULL AND lifecycle_hold IS NULL",
        (deadline_at, display_timezone, assignment_id))
    if cursor.rowcount != 1:
        raise LifecycleInvariantError("a new assignment already carried lifecycle state")


def stamp_observations(transaction: LifecycleWrite, assignment_id: int, view: LifecycleView) -> None:
    """Record what this writer transaction observed (6.3): a passed deadline, an
    ended grace window. Idempotent; takes no user input; never an input to ``E``.

    :meth:`LifecycleWrite.examine` calls it for every writer transaction that
    evaluates an assignment, so the clock state is ``ok`` and the assignment is
    not held. Nothing is stamped for a row whose timestamps do not parse.
    """
    if view.malformed:
        return
    conn, now_text = transaction.conn, transaction.now_text
    if view.deadline_at is not None and view.deadline_at <= transaction.now:
        conn.execute(
            "UPDATE quiz_assignments SET expiry_observed_at = :now "
            "WHERE id = :id AND lifecycle_hold IS NULL AND closed_at IS NULL "
            "AND expiry_observed_at IS NULL AND deadline_at IS NOT NULL AND deadline_at <= :now",
            {"now": now_text, "id": assignment_id})
    if view.submit_cutoff is not None and view.submit_cutoff <= transaction.now:
        conn.execute(
            "UPDATE quiz_assignments SET submissions_closed_at = :now "
            "WHERE id = :id AND lifecycle_hold IS NULL AND submissions_closed_at IS NULL "
            "AND :cutoff <= :now",
            {"now": now_text, "id": assignment_id, "cutoff": format_timestamp(view.submit_cutoff)})


def observe(conn: sqlite3.Connection, assignment_id: int) -> bool:
    """Best-effort observation stamp for a request that only read (6.3).

    A short writer transaction of its own. If the lock is busy, the clock is not
    usable or the assignment is held, it is skipped: the next observer retries,
    and the current response was already decided by the comparison. Never raises.
    """
    if conn.in_transaction:
        return False
    timeout_ms = None
    try:
        timeout_ms = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        conn.execute("PRAGMA busy_timeout = 0")  # never make a page wait for a stamp
        with write_transaction(conn) as transaction:
            row = fetch_lifecycle_row(conn, assignment_id)
            if row is None:
                transaction.discard()
                return False
            transaction.examine(row)  # stamps what it observes
        return True
    except (LifecycleUnavailable, sqlite3.Error):
        return False
    finally:
        if timeout_ms is not None:
            try:
                conn.execute(f"PRAGMA busy_timeout = {int(timeout_ms)}")
            except sqlite3.Error:
                pass


def observe_if_needed(conn: sqlite3.Connection, assignment_id: int, view: LifecycleView) -> None:
    if needs_observation(view):
        observe(conn, assignment_id)


CLOSE_CLOSED = "closed"                  # this request closed it
CLOSE_ALREADY_CLOSED = "already_closed"  # closed earlier; nothing written
CLOSE_EXPIRED = "expired"                # the deadline had passed; the observation was stamped


def _close_update(conn: sqlite3.Connection, assignment_id: int, now_text: str, user_id: int) -> int:
    cursor = conn.execute(
        "UPDATE quiz_assignments SET closed_at = :now, closed_by_user_id = :user_id "
        "WHERE id = :id AND " + OPEN_PREDICATE_SQL,
        {"now": now_text, "user_id": user_id, "id": assignment_id})
    return cursor.rowcount


def close_assignment(conn: sqlite3.Connection, assignment_id: int, *, principal_user_id: int) -> str:
    """Manual close (6.1). Returns one of the ``CLOSE_*`` outcomes.

    ``closed_at`` is the sampled ``now``, unchanged: never moved past an
    attempt's ``started_at`` and never later than the clock. A zero-row update is
    never answered with "closed" without looking: the row is re-read in the same
    transaction and classified. Raises :class:`AssignmentHeld`,
    :class:`ClockUnavailable`, ``LookupError`` for a missing row, and
    :class:`LifecycleInvariantError` if the update matched nothing although the
    row is open (the transaction is then rolled back).
    """
    if not isinstance(principal_user_id, int) or isinstance(principal_user_id, bool):
        raise ValueError("a manual close needs the acting account id")
    closed_at = None
    with write_transaction(conn) as transaction:
        row = fetch_lifecycle_row(conn, assignment_id)
        if row is None:
            raise LookupError(f"assignment {assignment_id} does not exist")
        # The rows acted on include every attempt's start: one ahead of the clock
        # stops the request as a clock fault before anything is closed.
        started = [r[0] for r in conn.execute(
            "SELECT started_at FROM quiz_attempts WHERE assignment_id = ?", (assignment_id,))]
        # stamp=False: an assignment that was already closed is answered without
        # writing anything; the one zero-row case that records an expiry does so below.
        view = transaction.examine(row, factual=started, stamp=False)
        if view.malformed:
            outcome = CLOSE_ALREADY_CLOSED
        elif _close_update(conn, assignment_id, transaction.now_text, principal_user_id) == 1:
            outcome, closed_at = CLOSE_CLOSED, transaction.now_text
        else:
            current = fetch_lifecycle_row(conn, assignment_id)
            if current["lifecycle_hold"] is not None:
                transaction.refuse_if_held(current)
            if any(current[column] is not None for column in TERMINAL_MARKER_COLUMNS):
                transaction.discard()
                outcome = CLOSE_ALREADY_CLOSED
            elif current["deadline_at"] is not None and current["deadline_at"] <= transaction.now_text:
                stamp_observations(transaction, assignment_id, evaluate(current, transaction.now))
                outcome = CLOSE_EXPIRED
            else:
                conn.rollback()
                logger.error("lifecycle: close of assignment %s matched no row although it is open",
                             assignment_id)
                raise LifecycleInvariantError("manual close matched no row on an open assignment")
    if closed_at is not None:
        # After the commit, for diagnosis only. An application log is not a record of the
        # lifecycle: it can lose, delay or reorder lines, and no tool reads a state from it.
        logger.info("lifecycle event=close assignment_id=%s closed_at=%r actor_user_id=%s",
                    assignment_id, closed_at, principal_user_id)
    return outcome


EDIT_UPDATED = "updated"
EDIT_CLOSED = "closed"      # frozen: 409, nothing changed
EDIT_INVALID = "invalid"    # 400, nothing changed


class EditResult(NamedTuple):
    outcome: str
    error: str | None = None


def edit_assignment(conn: sqlite3.Connection, assignment_id: int, *, title: str,
                    time_limit_minutes: int | None, deadline: DeadlineRequest | None,
                    input_error: str | None = None, actor_user_id: int | None = None) -> EditResult:
    """Edit title, time limit, deadline and zone of an open assignment (6.6).

    The whitelist is the signature: no other column can be named. Closure is
    decided first, under the writer lock: a closed assignment is frozen whatever
    was submitted, and the passed deadline is stamped. ``input_error`` is a
    validation error the caller found in the form before the transaction; it is
    reported only for an assignment that is still open. ``actor_user_id`` goes
    to the diagnostic log line only.
    """
    event = None
    with write_transaction(conn) as transaction:
        row = fetch_lifecycle_row(conn, assignment_id)
        if row is None:
            raise LookupError(f"assignment {assignment_id} does not exist")
        view = transaction.examine(row)  # stamps a passed deadline that nothing had recorded yet
        if view.is_closed:
            return EditResult(EDIT_CLOSED)
        if input_error is not None or deadline is None:
            return EditResult(EDIT_INVALID, input_error or "deadline_choice_required")
        try:
            deadline_at = resolve_deadline(deadline, transaction.now)
        except DeadlineError as exc:
            return EditResult(EDIT_INVALID, exc.code)

        cursor = conn.execute(
            "UPDATE quiz_assignments SET title_bg = :title, time_limit_minutes = :limit, "
            "deadline_at = :deadline, display_timezone = :zone "
            "WHERE id = :id AND " + OPEN_PREDICATE_SQL,
            {"title": title, "limit": time_limit_minutes, "deadline": deadline_at,
             "zone": deadline.timezone, "id": assignment_id, "now": transaction.now_text})
        if cursor.rowcount != 1:
            conn.rollback()
            logger.error("lifecycle: edit of assignment %s matched no row although it is open", assignment_id)
            raise LifecycleInvariantError("edit matched no row on an open assignment")
        event = (assignment_id, transaction.now_text, deadline_at, deadline.timezone, actor_user_id)
    # Diagnostic only, like the line of a close: two edits of one second are not ordered by it.
    logger.info("lifecycle event=edit assignment_id=%s at=%r deadline_at=%r display_timezone=%r "
                "actor_user_id=%s", *event)
    return EditResult(EDIT_UPDATED)
