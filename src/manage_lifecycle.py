#!/usr/bin/env python3
"""Operator CLI for the assignment lifecycle (docs/ASSIGNMENT_LIFECYCLE_DESIGN.md, PR #326).

The reconciliation command of the design. Besides ``web/services/lifecycle.py`` it
is the only code that assigns a lifecycle column, and the only code that writes
``lifecycle_hold``, ``retention_base_at`` or an operator-confirmed clock row.

    python3 -m src.manage_lifecycle --db "$DZI_DB" clock-status
    python3 -m src.manage_lifecycle --db "$DZI_DB" clock-inspect --verified-utc "2026-10-08 12:05:00"
    python3 -m src.manage_lifecycle --db "$DZI_DB" clock-recover --verified-utc "2026-10-08 12:05:00" \\
        --operator NAME --audit-log /var/lib/learnpilot/lifecycle-operator.log
    python3 -m src.manage_lifecycle --db "$DZI_DB" hold-list
    python3 -m src.manage_lifecycle --db "$DZI_DB" hold-resolve --assignment ID --reason clock_review \\
        --evidence "what was checked" --operator NAME --audit-log FILE
    python3 -m src.manage_lifecycle --db "$DZI_DB" restore-hold [--pre-restore-db OLD.db] --operator NAME --audit-log FILE
    python3 -m src.manage_lifecycle --db "$DZI_DB" restore-status [--expect-held N|all]
    python3 -m src.manage_lifecycle --db "$DZI_DB" reconcile --pre-restore-db OLD.db --snapshot-db SNAPSHOT.db \\
        --evidence-note "why OLD.db is trusted" --operator NAME --audit-log FILE
    python3 -m src.manage_lifecycle --db "$DZI_DB" verify [--hold-exceptions --operator NAME --audit-log FILE]
    python3 -m src.manage_lifecycle --db "$DZI_DB" exposure-audit --from "..." --to "..." [--snapshot-db SNAPSHOT.db]

After a restore these are run, in order and each as a gate, by
``deploy/learnpilot-restore.sh``; it stops at the first one that does not succeed.
The order is restore-hold, reconcile, clock-recover, verify: recovery comes after
reconciliation because it has to classify the deadlines reconciliation brings in.
That script is also what settles where the pre-restore database comes from (the
path the service is configured to use, read in place). Run by hand, ``reconcile``
checks what the file is, and cannot check where it came from.

What no command here does, by design:

* close an assignment, end its submissions or stamp an observation marker from a
  clock value (recovery manufactures no closure: it holds what is uncertain);
* clear or change a terminal marker, lower a retention floor, or change the
  deadline of a row that carries a terminal marker;
* write ``lifecycle_hold`` as a whole: a reason is added, or one reason is
  resolved, and every other reason stays;
* release a held assignment without the evidence its reason requires. A restore
  reason is resolved by ``reconcile`` alone, from the database the restore
  replaced. That database has to qualify as a whole first: the same rows as the
  restored copy and not merely the same ids, and a lifecycle clock that ran on
  after the snapshot. A copy of the snapshot (with or without unrelated changes),
  an earlier backup and another database are refused. Nothing is resolved from
  application log lines
  or from what an operator states: ``reconcile-partial`` and
  ``reconcile-record`` exist only to say so, and change nothing. An assignment
  whose state cannot be established stays held;
* delete anything. Retention cleanup is PR #328 and does not exist.

The database must be named explicitly (``--db`` or ``DZI_DB``); it is never
defaulted, created or migrated. Every command refuses a database whose applied
migrations differ from the migration files this build ships. Commands that write
need ``--operator`` and ``--audit-log``: each addition and each resolution is
appended there as one JSON line, with the operator, the instant and the evidence
relied on, before the transaction commits.

Exit status. Only 0 means that the command did all it is for:

    0  done; for a check, nothing to report
    1  refused by a lifecycle rule, or a check has findings; nothing was written
    2  an operator, input or database problem; nothing was written
    3  done and committed, but recovery is incomplete: assignments remain held
    4  a recovery operation this build does not provide; nothing was read or written
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import TextIO

try:
    from web.services import lifecycle
except ModuleNotFoundError:  # run as `python3 src/manage_lifecycle.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from web.services import lifecycle

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = PROJECT_ROOT / "web" / "migrations"

HOLD_COLUMNS = "id, " + ", ".join(lifecycle.LIFECYCLE_COLUMNS)
RESTORE_REASONS = (lifecycle.HOLD_RESTORE_PENDING, lifecycle.HOLD_RESTORE_UNRECONCILED)

EXIT_DONE = 0
EXIT_REFUSED = 1       # refused by a lifecycle rule, or a check has findings
EXIT_ERROR = 2         # operator, input or database problem
EXIT_INCOMPLETE = 3    # committed, and assignments remain held: recovery is not complete
EXIT_UNSUPPORTED = 4   # a recovery operation this build does not provide


class OperatorError(Exception):
    """An expected operator, input or database problem: reported without a traceback (exit 2)."""


class Refused(Exception):
    """The command was understood and refused by a lifecycle rule; nothing was written (exit 1)."""


# ---------------------------------------------------------------------------
# Database, schema guard, audit log
# ---------------------------------------------------------------------------

def open_database(path: Path, *, read_only: bool) -> sqlite3.Connection:
    if not path.is_file():
        raise OperatorError(f"database not found: {path}")
    if read_only:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def shipped_migrations(migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    return sorted(path.name for path in migrations_dir.glob("*.sql") if path.is_file())


def schema_problems(conn: sqlite3.Connection, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    """Why this build must not touch the database; empty when schema and build agree."""
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'").fetchone()
    applied = {row[0] for row in conn.execute("SELECT filename FROM schema_migrations")} if has_table else set()
    shipped = set(shipped_migrations(migrations_dir))
    problems = []
    if shipped - applied:
        problems.append("pending migrations: " + ", ".join(sorted(shipped - applied)))
    if applied - shipped:
        # A tool from an older build on a newer schema: a later phase may require
        # more of every lifecycle write than this build knows about.
        problems.append("applied migrations this build does not ship: " + ", ".join(sorted(applied - shipped)))
    return problems


def require_matching_schema(conn: sqlite3.Connection) -> None:
    problems = schema_problems(conn)
    if problems:
        raise OperatorError("this build does not match the database schema (" + "; ".join(problems) + ")")


class AuditLog:
    """Append-only JSON lines: one per operator action, flushed to disk when written."""

    def __init__(self, path: Path, operator: str, database: Path, out: TextIO):
        self.path, self.operator, self.database, self.out = path, operator, database, out

    def record(self, action: str, **details) -> None:
        entry = {
            "at": lifecycle.format_timestamp(lifecycle.wall_reading()),
            "action": action,
            "operator": self.operator,
            "database": str(self.database),
            **details,
        }
        line = json.dumps(entry, ensure_ascii=False, sort_keys=True)
        try:
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise OperatorError(f"cannot write the audit log {self.path}: {exc.strerror}") from None
        print(f"audit: {action}", file=self.out)


def parse_utc(text: str, option: str) -> datetime:
    try:
        return lifecycle.parse_timestamp(text)
    except lifecycle.TimestampError:
        raise OperatorError(f'{option} must be UTC as "YYYY-MM-DD HH:MM:SS"') from None


def parse_ids(text: str) -> list[int]:
    try:
        ids = sorted({int(part) for part in text.split(",") if part.strip()})
    except ValueError:
        raise OperatorError("assignment ids must be integers separated by commas") from None
    if any(value <= 0 for value in ids):
        raise OperatorError("assignment ids must be positive")
    return ids


# ---------------------------------------------------------------------------
# Holds: a set of reasons. The only two statements that write lifecycle_hold
# are below: one adds to the value that was read, one resolves a single reason.
# ---------------------------------------------------------------------------

def hold_tokens(raw: str | None) -> list[str]:
    return [] if raw is None else raw.split(",")


def hold_is_readable(raw: object) -> bool:
    """NULL, or the known reasons, each once, in the fixed order. Anything else
    (an unknown reason, a repeated one, an empty token as in ``clock_review,``,
    a value that is not text) is unreadable: it still holds, and no command
    resolves anything from it."""
    return lifecycle.parse_hold(raw)[1]


def _stored_hold(conn: sqlite3.Connection, assignment_id: int):
    row = conn.execute("SELECT lifecycle_hold FROM quiz_assignments WHERE id = ?", (assignment_id,)).fetchone()
    if row is None:
        raise OperatorError(f"assignment {assignment_id} does not exist")
    return row[0]


def _store_added_hold(conn: sqlite3.Connection, assignment_id: int, current: str | None, updated: str) -> None:
    """The one statement that adds to a hold. It replaces exactly the value that was read."""
    cursor = conn.execute(
        "UPDATE quiz_assignments SET lifecycle_hold = ? WHERE id = ? AND lifecycle_hold IS ?",
        (updated, assignment_id, current))
    if cursor.rowcount != 1:
        raise OperatorError(f"assignment {assignment_id} changed while its hold was being written")


def _add_hold_tokens(conn: sqlite3.Connection, assignment_id: int, tokens: list[str]) -> bool:
    """Add known reasons to the assignment's hold; ``False`` if all were there already.

    Adding never removes. A readable set stays in the fixed order. An unreadable
    stored value is kept exactly as it is and only appended to, so what this
    build cannot read is neither dropped nor tidied into something readable. A
    stored value that is not text is left untouched: it holds as it is.
    """
    unknown = [token for token in tokens if token not in lifecycle.HOLD_REASONS]
    if unknown:
        raise OperatorError("unknown hold reason: " + ", ".join(map(repr, unknown)))
    current = _stored_hold(conn, assignment_id)
    if current is not None and not isinstance(current, str):
        return False
    present = hold_tokens(current)
    missing = [token for token in dict.fromkeys(tokens) if token not in present]
    if not missing:
        return False
    if hold_is_readable(current):
        updated = ",".join(name for name in lifecycle.HOLD_REASONS if name in present or name in missing)
    else:
        updated = ",".join([current, *missing])
    _store_added_hold(conn, assignment_id, current, updated)
    return True


def add_hold_reason(conn: sqlite3.Connection, assignment_id: int, reason: str) -> bool:
    """Add one known reason to the assignment's set; ``False`` if it was already there."""
    return _add_hold_tokens(conn, assignment_id, [reason])


def _contains_run(tokens: list[str], run: list[str]) -> bool:
    """Whether ``run`` occurs in ``tokens`` as one uninterrupted, ordered stretch."""
    return any(tokens[start:start + len(run)] == run for start in range(len(tokens) - len(run) + 1))


def carry_hold(conn: sqlite3.Connection, assignment_id: int, raw: object) -> bool:
    """Merge a hold read from another copy of the database (a restore) into this one;
    ``False`` if this one already holds all of it.

    The incoming value is judged as it is stored, before anything is split,
    merged or deduplicated.

    * A readable value is a set of known reasons and is merged as one: a restore
      weakens no hold.
    * An unreadable value is not a set of anything this build understands.
      ``clock_review,clock_review`` is not "clock_review, twice": it is text
      nobody can vouch for. It is carried as that exact text, appended as one
      comma-delimited stretch, with nothing dropped, reordered, trimmed or
      deduplicated. The stored value is unreadable from then on, whatever it was
      before, so no command resolves anything from it and the assignment stays
      held. Carrying the same text again changes nothing.
    * A value that is not text cannot be carried as text at all. The database it
      comes from does not qualify as a source of holds, and the command stops.
    """
    if raw is None:
        return False
    if not isinstance(raw, str):
        raise OperatorError(
            f"assignment {assignment_id}: the incoming lifecycle_hold is not text ({type(raw).__name__}); "
            "it cannot be carried over exactly, so the database it comes from does not qualify as a source "
            "of holds")
    if hold_is_readable(raw):
        return _add_hold_tokens(conn, assignment_id, hold_tokens(raw))
    current = _stored_hold(conn, assignment_id)
    if current is None:
        updated = raw
    elif not isinstance(current, str) or _contains_run(hold_tokens(current), hold_tokens(raw)):
        return False
    else:
        updated = current + "," + raw
    _store_added_hold(conn, assignment_id, current, updated)
    return True


def resolve_hold_reason(conn: sqlite3.Connection, assignment_id: int, reason: str) -> None:
    """Resolve exactly one reason; the others stay and keep the assignment held.

    The caller has established the condition of that reason. Resolving never
    touches a terminal marker or a retention floor. An unreadable stored value is
    refused outright: removing one token from it could release the assignment
    while discarding whatever the rest of the value was meant to say.
    """
    current = _stored_hold(conn, assignment_id)
    if not hold_is_readable(current):
        raise Refused(
            f"assignment {assignment_id}: lifecycle_hold {current!r} is unreadable; nothing is resolved from "
            "it and it stays held until an operator repairs the stored value explicitly")
    tokens = hold_tokens(current)
    if reason not in tokens:
        raise Refused(f"assignment {assignment_id} is not held for {reason}")
    remaining = ",".join(token for token in tokens if token != reason) or None
    cursor = conn.execute(
        "UPDATE quiz_assignments SET lifecycle_hold = ? WHERE id = ? AND lifecycle_hold IS ?",
        (remaining, assignment_id, current))
    if cursor.rowcount != 1:
        raise OperatorError(f"assignment {assignment_id} changed while its hold was being written")


def raise_retention_floor(conn: sqlite3.Connection, assignment_id: int, floor: datetime) -> bool:
    """Raise ``retention_base_at`` to ``floor``. It can only be raised, never lowered."""
    row = conn.execute("SELECT retention_base_at FROM quiz_assignments WHERE id = ?", (assignment_id,)).fetchone()
    current = row[0]
    if current is not None:
        try:
            if lifecycle.parse_timestamp(current) >= floor:
                return False
        except lifecycle.TimestampError:
            return False  # unreadable: left alone and reported by `verify`; it blocks cleanup either way
    cursor = conn.execute(
        "UPDATE quiz_assignments SET retention_base_at = ? WHERE id = ? AND retention_base_at IS ?",
        (lifecycle.format_timestamp(floor), assignment_id, current))
    return cursor.rowcount == 1


# ---------------------------------------------------------------------------
# Clock: status, inspection and the operator confirmation (3.7)
# ---------------------------------------------------------------------------

def _clock_row_or_none(conn: sqlite3.Connection):
    try:
        return lifecycle.read_clock_row(conn), None
    except lifecycle.ClockRowError as exc:
        return None, str(exc)


def _factual_values(conn: sqlite3.Connection) -> dict[int, list[tuple[str, object]]]:
    """Per assignment: every factual event timestamp and terminal marker of the
    assignment and of its attempts, as ``(label, stored value)``. A scheduled
    deadline is not among them (3.9)."""
    values: dict[int, list[tuple[str, object]]] = {}
    for row in conn.execute("SELECT id, closed_at, expiry_observed_at, submissions_closed_at FROM quiz_assignments"):
        values[int(row["id"])] = [(column, row[column]) for column in lifecycle.TERMINAL_MARKER_COLUMNS
                                  if row[column] is not None]
    for row in conn.execute("SELECT id, assignment_id, started_at, submitted_at FROM quiz_attempts"):
        bucket = values.setdefault(int(row["assignment_id"]), [])
        for column in ("started_at", "submitted_at"):
            if row[column] is not None:
                bucket.append((f"attempt {row['id']} {column}", row[column]))
    return values


def clock_lists(conn: sqlite3.Connection, verified: datetime, old_high_water: datetime | None,
                suspect_from: datetime | None, suspect_to: datetime | None,
                local_clock: datetime | None = None) -> tuple[dict, dict]:
    """The two lists of 3.7 step 3: ``{assignment_id: [why, ...]}`` each.

    Everything is classified against the independently **verified** time, never
    against the local clock or a mix of the two.

    (a) a factual event timestamp or terminal marker later than the verified
        time, inside the declared suspect interval, or unreadable;
    (b) a deadline, or an ``E + G``, after the verified time and at or before the
        old high-water mark or the local clock's own reading, with no persisted
        marker for the difference: the state computed at the verified time
        differs from the state another reading of the clock gives. With an
        unreadable old mark every such future instant counts.

    A scheduled deadline in the future is, by itself, never a reason.
    """
    list_a: dict[int, list[str]] = {}
    for assignment_id, pairs in _factual_values(conn).items():
        for label, raw in pairs:
            try:
                value = lifecycle.parse_timestamp(raw)
            except lifecycle.TimestampError:
                list_a.setdefault(assignment_id, []).append(f"{label} is unreadable")
                continue
            if value > verified:
                list_a.setdefault(assignment_id, []).append(f"{label} {raw} is after the verified time")
            elif suspect_from is not None and suspect_from <= value <= suspect_to:
                list_a.setdefault(assignment_id, []).append(f"{label} {raw} is inside the suspect interval")

    # The latest reading against which an unrecorded instant may already count as passed.
    readings = [reading for reading in (old_high_water, local_clock) if reading is not None]
    horizon = None if old_high_water is None else max(readings)

    def uncertain(instant: datetime | None) -> bool:
        return instant is not None and instant > verified and (horizon is None or instant <= horizon)

    list_b: dict[int, list[str]] = {}
    for row in conn.execute(f"SELECT {HOLD_COLUMNS} FROM quiz_assignments"):
        view = lifecycle.evaluate(row, verified)
        if view.malformed:
            list_a.setdefault(int(row["id"]), []).append(
                "unreadable lifecycle timestamps: " + ", ".join(view.malformed))
            continue
        if view.closed_at is None and view.expiry_observed_at is None and uncertain(view.deadline_at):
            list_b.setdefault(int(row["id"]), []).append(
                f"deadline {row['deadline_at']} lies after the verified time and was never recorded as passed")
        if view.submissions_closed_at is None and uncertain(view.submit_cutoff):
            list_b.setdefault(int(row["id"]), []).append(
                "the end of its grace window lies after the verified time and was never recorded")
    return list_a, list_b


# A DZI Part 1 simulation attempt (migration 027, C3) is open while none of its three terminal
# timestamps is set: the open-state predicate of that table's unique index, restated.
_PART1_OPEN_SQL = "submitted_at IS NULL AND expired_at IS NULL AND abandoned_at IS NULL"


def count_open_part1_attempts(conn: sqlite3.Connection) -> int:
    """Open DZI Part 1 attempts, any account, any age: what clock-recover would abandon (read-only).

    Every command has already passed :func:`require_matching_schema`, so the table exists.
    """
    return int(conn.execute(f"SELECT COUNT(*) FROM dzi_part1_attempts WHERE {_PART1_OPEN_SQL}").fetchone()[0])


def abandon_open_part1_attempts(conn: sqlite3.Connection, verified_text: str) -> int:
    """Stamp every still-open Part 1 attempt abandoned at the verified time; return how many.

    A row stamped during a forward clock fault cannot be told from a healthy one once lifecycle
    time reaches its false stamp, so recovery does not try: the marking does not depend on a
    suspect interval or on whether the verified time precedes the row's start (``abandoned_at``
    has no ordering CHECK). The statement is guarded on the open state, so a submitted, expired
    or abandoned row and every answer row are never touched. An honest open attempt is
    abandoned too (design 7.4, "Cost").
    """
    cursor = conn.execute(
        f"UPDATE dzi_part1_attempts SET abandoned_at = ? WHERE {_PART1_OPEN_SQL}", (verified_text,))
    return cursor.rowcount


def clock_agreement_problem(local_clock: datetime, verified: datetime) -> str | None:
    """Why the local clock does not agree well enough with the verified time for
    the state to return to ``ok``, or ``None``.

    Lifecycle time after the confirmation is the local clock, floored at the
    verified time (the new high-water mark). A local clock behind the verified
    time by more than the regression tolerance would be refused by the very next
    lifecycle write, and one that is further behind would, without the floor,
    still call a deadline open that the verified time has passed. A local clock
    ahead by more than the step tolerance closes assignments visibly early.
    Either way the clock has to be corrected first.
    """
    ahead = int((local_clock - verified).total_seconds())
    if -ahead > lifecycle.CLOCK_REGRESSION_TOLERANCE_SECONDS:
        return (f"the local clock is {-ahead} s behind the verified time "
                f"(at most {lifecycle.CLOCK_REGRESSION_TOLERANCE_SECONDS} s is accepted)")
    if ahead > lifecycle.CLOCK_STEP_TOLERANCE_SECONDS:
        return (f"the local clock is {ahead} s ahead of the verified time "
                f"(at most {lifecycle.CLOCK_STEP_TOLERANCE_SECONDS} s is accepted)")
    return None


def _is_closed_at(conn: sqlite3.Connection, assignment_id: int, instant: datetime) -> bool:
    row = conn.execute(f"SELECT {HOLD_COLUMNS} FROM quiz_assignments WHERE id = ?", (assignment_id,)).fetchone()
    return (any(row[column] is not None for column in lifecycle.TERMINAL_MARKER_COLUMNS)
            or lifecycle.evaluate(row, instant).is_closed)


def _suspect_interval(args) -> tuple[datetime | None, datetime | None]:
    if (args.suspect_from is None) != (args.suspect_to is None):
        raise OperatorError("--suspect-from and --suspect-to are given together")
    if args.suspect_from is None:
        return None, None
    start, end = parse_utc(args.suspect_from, "--suspect-from"), parse_utc(args.suspect_to, "--suspect-to")
    if end < start:
        raise OperatorError("--suspect-to is before --suspect-from")
    return start, end


def _print_lists(list_a: dict, list_b: dict, out: TextIO) -> None:
    print(f"list (a), factual timestamps or terminal markers in doubt: {len(list_a)} assignment(s)", file=out)
    for assignment_id in sorted(list_a):
        for why in list_a[assignment_id]:
            print(f"  assignment {assignment_id}: {why}", file=out)
    print(f"list (b), state differs between the verified time and the old high-water mark: "
          f"{len(list_b)} assignment(s)", file=out)
    for assignment_id in sorted(list_b):
        for why in list_b[assignment_id]:
            print(f"  assignment {assignment_id}: {why}", file=out)


def cmd_clock_status(conn, args, out) -> int:
    row, problem = _clock_row_or_none(conn)
    environment = lifecycle.clock_environment()
    wall = lifecycle.wall_reading()
    print(f"local clock (UTC): {lifecycle.format_timestamp(wall)}", file=out)
    if row is None:
        print(f"clock row: UNUSABLE ({problem})", file=out)
        print("lifecycle writes are refused; repair it with clock-recover, never by re-running a seed", file=out)
    else:
        print(f"state: {row.state}", file=out)
        print(f"high-water mark: {lifecycle.format_timestamp(row.high_water_at)}", file=out)
        print(f"last fault: {row.state_reason or 'none'}"
              + (f" (clock read {row.state_changed_at})" if row.state_changed_at else ""), file=out)
        print("boot anchor: " + ("none" if not row.has_anchor else
                                 f"{lifecycle.format_timestamp(row.ref_wall_at)} on boot {row.ref_boot_id}"), file=out)
    synchronized = environment.synchronized()
    print(f"host evidence ({environment.name}): boot id "
          f"{'available' if environment.boot_id() else 'unavailable'}, boot-time clock "
          f"{'available' if environment.boottime_ms() is not None else 'unavailable'}, "
          f"clock synchronised: {'unreadable' if synchronized is None else ('yes' if synchronized else 'no')}",
          file=out)
    return 0 if row is not None and row.state == lifecycle.CLOCK_STATE_OK else 1


def cmd_clock_inspect(conn, args, out) -> int:
    verified = parse_utc(args.verified_utc, "--verified-utc")
    suspect_from, suspect_to = _suspect_interval(args)
    wall = lifecycle.wall_reading()
    row, problem = _clock_row_or_none(conn)
    if row is None:
        print(f"clock row: UNUSABLE ({problem}); the old high-water mark is unknown", file=out)
    else:
        print(f"state: {row.state}; last fault: {row.state_reason or 'none'}"
              + (f" (clock read {row.state_changed_at})" if row.state_changed_at else ""), file=out)
        print(f"old high-water mark: {lifecycle.format_timestamp(row.high_water_at)}", file=out)
    print(f"local clock (UTC): {lifecycle.format_timestamp(wall)}; verified: "
          f"{lifecycle.format_timestamp(verified)}", file=out)
    disagreement = clock_agreement_problem(wall, verified)
    if disagreement:
        print(f"clock-recover would be REFUSED: {disagreement}; correct the clock first", file=out)
    list_a, list_b = clock_lists(conn, verified, None if row is None else row.high_water_at,
                                 suspect_from, suspect_to, local_clock=wall)
    _print_lists(list_a, list_b, out)
    print(f"open DZI Part 1 attempts: {count_open_part1_attempts(conn)} "
          "(clock-recover would mark every one of them abandoned)", file=out)
    print("nothing was changed; clock-recover would hold these assignments with clock_review", file=out)
    return 0


def cmd_clock_recover(conn, args, out, audit: AuditLog) -> int:
    """Steps 5 and 6 of 3.7 in one transaction: hold what is uncertain, then confirm.

    The independently verified time decides everything: which assignments are
    held, the retention floors and the new high-water mark. The local clock only
    has to agree with it (:func:`clock_agreement_problem`); if it does not, the
    state stays ``suspect`` and nothing is written.

    The state cannot return to ``ok`` while an affected assignment is unheld,
    because both happen in the same commit. No closure marker of any kind is
    written: the high-water mark is one reading of a clock known to have been
    wrong, and says nothing about any particular assignment.

    In the same transaction every still-open DZI Part 1 simulation attempt is marked abandoned
    at the verified time (:func:`abandon_open_part1_attempts`); the count is printed and audited.
    """
    verified = parse_utc(args.verified_utc, "--verified-utc")
    suspect_from, suspect_to = _suspect_interval(args)
    conn.execute("BEGIN IMMEDIATE")
    wall = lifecycle.wall_reading()
    disagreement = clock_agreement_problem(wall, verified)
    if disagreement:
        conn.rollback()
        raise Refused(f"{disagreement}: correct the clock first; the clock state is unchanged")

    row, problem = _clock_row_or_none(conn)
    list_a, list_b = clock_lists(conn, verified, None if row is None else row.high_water_at,
                                 suspect_from, suspect_to, local_clock=wall)
    held, floors = {}, []
    for assignment_id in sorted(set(list_a) | set(list_b)):
        added = add_hold_reason(conn, assignment_id, lifecycle.HOLD_CLOCK_REVIEW)
        held[assignment_id] = {"added": added, "why": list_a.get(assignment_id, []) + list_b.get(assignment_id, [])}
    for assignment_id in sorted(list_a):
        # A mis-stamped closure can never shorten retention.
        if _is_closed_at(conn, assignment_id, verified) and raise_retention_floor(conn, assignment_id, verified):
            floors.append(assignment_id)

    # The mark is the verified time: lifecycle time can never again be earlier
    # than it. The boot anchor ties the local clock's own reading to boot time, so
    # steps of that clock from here on are measured against it.
    environment = lifecycle.clock_environment()
    boot_id, boottime_ms = environment.boot_id(), environment.boottime_ms()
    anchor = ((lifecycle.format_timestamp(wall), boot_id, boottime_ms)
              if boot_id is not None and boottime_ms is not None else (None, None, None))
    verified_text = lifecycle.format_timestamp(verified)
    # Same transaction, after the verified-clock checks above: every open DZI Part 1 attempt is
    # abandoned at the verified time, however the suspect interval was declared.
    part1_abandoned = abandon_open_part1_attempts(conn, verified_text)
    if row is None:
        conn.execute("DELETE FROM lifecycle_clock")
        conn.execute(
            "INSERT INTO lifecycle_clock (id, high_water_at, state, ref_wall_at, ref_boot_id, ref_boottime_ms) "
            "VALUES (1, ?, 'ok', ?, ?, ?)", (verified_text, *anchor))
    else:
        conn.execute(
            "UPDATE lifecycle_clock SET state = 'ok', high_water_at = ?, ref_wall_at = ?, ref_boot_id = ?, "
            "ref_boottime_ms = ? WHERE id = 1", (verified_text, *anchor))

    audit.record(
        "clock_recover", verified_utc=verified_text, local_clock=lifecycle.format_timestamp(wall),
        new_high_water=verified_text,
        previous={"usable": row is not None, "problem": problem,
                  "state": None if row is None else row.state,
                  "reason": None if row is None else row.state_reason,
                  "fault_reading": None if row is None else row.state_changed_at,
                  "high_water": None if row is None else lifecycle.format_timestamp(row.high_water_at)},
        suspect_interval=[args.suspect_from, args.suspect_to],
        held={str(key): value for key, value in held.items()}, retention_floor_raised=floors,
        boot_anchor_written=anchor[1] is not None, dzi_part1_abandoned=part1_abandoned)
    conn.commit()

    _print_lists(list_a, list_b, out)
    print(f"held with clock_review: {len(held)} assignment(s); retention floor raised: {len(floors)}", file=out)
    print(f"DZI Part 1 attempts abandoned: {part1_abandoned}", file=out)
    print(f"clock confirmed: state ok, high-water mark {verified_text} (the verified time)"
          + ("" if anchor[1] is not None else "; no boot anchor (this host has no boot identity)"), file=out)
    print("no closure marker was written; passed deadlines are recorded by ordinary observation", file=out)
    return 0


# ---------------------------------------------------------------------------
# Holds: listing and the resolution of clock_review
# ---------------------------------------------------------------------------

def cmd_hold_list(conn, args, out) -> int:
    rows = conn.execute(
        "SELECT id, title_bg, lifecycle_hold FROM quiz_assignments WHERE lifecycle_hold IS NOT NULL ORDER BY id"
    ).fetchall()
    print(f"held assignments: {len(rows)}", file=out)
    unreadable = 0
    for row in rows:
        well_formed = hold_is_readable(row["lifecycle_hold"])
        unreadable += not well_formed
        print(f"  assignment {row['id']}: {row['lifecycle_hold']!r}"
              + ("" if well_formed else "  UNREADABLE: it stays held; no command resolves anything from it"),
              file=out)
    return 1 if unreadable else 0


def cmd_hold_resolve(conn, args, out, audit: AuditLog) -> int:
    """Resolve ``clock_review`` for one assignment, after review (3.7 step 7).

    Only once lifecycle time, by a clock that passes the health check, has passed
    every factual timestamp and terminal marker of the assignment and its
    attempts. Until then it stays held: that is what keeps a closure stamped in
    the future from opening a grace window that lasts until that future arrives.
    A restore reason is resolved by ``reconcile`` from the replaced database,
    never here: what an operator states is not evidence of an assignment's state.
    """
    if args.reason != lifecycle.HOLD_CLOCK_REVIEW:
        raise Refused(f"{args.reason} is resolved only by `reconcile`, from the database the restore replaced; "
                      "hold-resolve never releases it, whatever is stated as evidence")
    if not args.evidence.strip():
        raise OperatorError("--evidence states what was reviewed; it cannot be empty")
    try:
        with lifecycle.write_transaction(conn) as transaction:
            row = conn.execute(f"SELECT {HOLD_COLUMNS} FROM quiz_assignments WHERE id = ?",
                               (args.assignment,)).fetchone()
            if row is None:
                raise OperatorError(f"assignment {args.assignment} does not exist")
            if not hold_is_readable(row["lifecycle_hold"]):
                transaction.discard()
                raise Refused(
                    f"assignment {args.assignment}: lifecycle_hold {row['lifecycle_hold']!r} is unreadable; "
                    "nothing is resolved from it and it stays held until an operator repairs the stored "
                    "value explicitly")
            pending = []
            for label, raw in _factual_values(conn).get(args.assignment, []):
                try:
                    if lifecycle.parse_timestamp(raw) > transaction.now:
                        pending.append(f"{label} {raw} is still after lifecycle time {transaction.now_text}")
                except lifecycle.TimestampError:
                    pending.append(f"{label} is unreadable")
            if lifecycle.evaluate(row, transaction.now).malformed:
                pending.append("the assignment has unreadable lifecycle timestamps")
            if pending:
                transaction.discard()
                raise Refused(f"assignment {args.assignment} stays held: " + "; ".join(pending))
            transaction.admit()
            resolve_hold_reason(conn, args.assignment, lifecycle.HOLD_CLOCK_REVIEW)
            remaining = conn.execute("SELECT lifecycle_hold FROM quiz_assignments WHERE id = ?",
                                     (args.assignment,)).fetchone()[0]
            audit.record("hold_resolve", assignment_id=args.assignment, reason=lifecycle.HOLD_CLOCK_REVIEW,
                         evidence=args.evidence, lifecycle_time=transaction.now_text, remaining=remaining)
    except lifecycle.ClockUnavailable as exc:
        raise Refused(f"the clock is not usable ({exc.reason}); complete clock-recover first") from None
    print(f"assignment {args.assignment}: clock_review resolved; "
          + ("no reason remains, it is served again" if remaining is None else f"still held for {remaining}"),
          file=out)
    return 0


# ---------------------------------------------------------------------------
# Restore (13.4): hold everything first, reconcile from the replaced database
# afterwards. That database is the only evidence this build accepts.
# ---------------------------------------------------------------------------

def _open_evidence(conn: sqlite3.Connection, path_text: str, label: str, option: str) -> sqlite3.Connection:
    path = Path(path_text)
    restored = conn.execute("PRAGMA database_list").fetchone()[2]
    if restored and path.exists() and os.path.samefile(restored, path):
        raise OperatorError(f"{option} is the restored database itself; a database is not evidence "
                            "about its own past")
    try:
        evidence = open_database(path, read_only=True)
        if evidence.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise OperatorError(f"{label} fails PRAGMA integrity_check: {path}")
        evidence.execute("SELECT COUNT(*) FROM quiz_assignments").fetchone()
    except sqlite3.Error as exc:
        raise OperatorError(f"{label} cannot be read: {path} ({exc})") from None
    return evidence


def _open_pre_restore(conn: sqlite3.Connection, path_text: str) -> sqlite3.Connection:
    """The database the restore replaces, read-only, checked as evidence A requires."""
    pre = _open_evidence(conn, path_text, "pre-restore database", "--pre-restore-db")
    columns = {row[1] for row in pre.execute("PRAGMA table_info(quiz_assignments)")}
    if not set(lifecycle.LIFECYCLE_COLUMNS) <= columns:
        pre.close()
        raise OperatorError(f"pre-restore database has no lifecycle columns: {path_text}")
    return pre


# ---------------------------------------------------------------------------
# Whether a database can be evidence at all (13.4, evidence A).
#
# Evidence A is "the database the restore replaces": the one that was live, with
# everything that happened after the snapshot. A file does not say that about
# itself, and nothing below proves it. What can be checked is what such a
# database necessarily is, and anything that is not is refused as a whole:
#
#   * it is the same database, later: every assignment and every attempt of the
#     restored copy is in it, as the same row (not merely under the same id);
#   * its lifecycle went on after the snapshot: its lifecycle clock, which every
#     lifecycle write advances, stands later than the snapshot's. A copy of the
#     snapshot passes every other check and says nothing about what happened
#     since. Neither does a copy in which something else was changed: a new
#     account, a sign-in or an edited title is not lifecycle history, and is not
#     counted as any.
#
# Freshness that cannot be shown this way is not assumed. A database that really
# was live, but in which no lifecycle write was made after the snapshot (a quiet
# night, a release that was installed and never used), cannot be told from a copy
# of the snapshot and is refused like one: the restore holds stay.
#
# Where the file comes from is settled outside this tool: the restore script
# reads the database at the path the service is configured to use, in place,
# with the service stopped. Used by hand, the caller answers for that.
# ---------------------------------------------------------------------------

# Integer ids repeat from one database to the next; these columns do not. The
# first group never changes once a row exists. The second was added by later
# releases or is written once (ownership, the public token, a submission): NULL
# in the older copy may have been filled in since, anything else has to match.
ASSIGNMENT_FIXED = ("section_id", "question_count", "created_at", "question_plan_json")
ASSIGNMENT_SET_ONCE = ("owner_user_id", "public_token")
ATTEMPT_FIXED = ("assignment_id", "student_name", "seed", "question_ids_json", "started_at", "user_id")
ATTEMPT_SET_ONCE = ("submitted_at",)


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]


def _same_row_problem(older: sqlite3.Row, newer: sqlite3.Row, fixed, set_once, shared: set[str]) -> str | None:
    """Why ``newer`` is not the row ``older`` at a later time, or ``None``."""
    for column in fixed:
        if column in shared and older[column] != newer[column]:
            return f"{column} differs"
    for column in set_once:
        if column in shared and older[column] is not None and older[column] != newer[column]:
            return f"{column} differs"
    return None


def lineage_problem(older: sqlite3.Connection, newer: sqlite3.Connection, newer_name: str) -> str | None:
    """Why ``newer`` is not a later state of the database ``older`` is, or ``None``.

    Every assignment and every attempt of the older copy must be in the newer
    one as the same row. Neither is ever deleted, so a missing or different row
    means another database, or an earlier one.
    """
    for table, fixed, set_once, noun in (
            ("quiz_assignments", ASSIGNMENT_FIXED, ASSIGNMENT_SET_ONCE, "assignment"),
            ("quiz_attempts", ATTEMPT_FIXED, ATTEMPT_SET_ONCE, "attempt")):
        shared = set(_columns(older, table)) & set(_columns(newer, table))
        columns = ", ".join(["id", *(c for c in (*fixed, *set_once) if c in shared)])
        later = {int(row["id"]): row for row in newer.execute(f"SELECT {columns} FROM {table}")}
        for row in older.execute(f"SELECT {columns} FROM {table} ORDER BY id"):
            other = later.get(int(row["id"]))
            if other is None:
                return f"{noun} {row['id']} is not in {newer_name}"
            problem = _same_row_problem(row, other, fixed, set_once, shared)
            if problem:
                return f"{noun} {row['id']} is a different {noun} in {newer_name} ({problem})"
    return None


def _clock_mark(conn: sqlite3.Connection) -> datetime | None:
    """The high-water mark of a database's clock row; ``None`` without a lifecycle schema."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'lifecycle_clock'").fetchone():
        return None
    return lifecycle.read_clock_row(conn).high_water_at


def require_later_state(conn: sqlite3.Connection, pre: sqlite3.Connection, snapshot: sqlite3.Connection) -> str:
    """Refuse (``OperatorError``) evidence that cannot be the later state of the restored database.

    ``conn`` is the restored copy, ``snapshot`` the untouched snapshot it was made
    from, ``pre`` the evidence. Returns what shows that the evidence's lifecycle
    went on after the snapshot, for the audit log. There is one thing that shows
    it: the lifecycle clock. Whatever else differs between the two databases is
    not looked at, because it is not lifecycle history.
    """
    def refuse(why: str):
        raise OperatorError("the pre-restore database does not qualify as evidence: " + why
                            + ". Nothing was reconciled and every assignment stays held")

    problems = schema_problems(pre)
    if problems:
        refuse("it is not at the schema of this build (" + "; ".join(problems) + "), so it is not the "
               "database this build was serving; if it is, apply the pending migrations to it first")
    try:
        evidence_mark = _clock_mark(pre)
        snapshot_mark = _clock_mark(snapshot)
    except lifecycle.ClockRowError as exc:
        evidence_mark = snapshot_mark = None
        refuse(f"a lifecycle clock row cannot be read ({exc})")
    if evidence_mark is None:
        refuse("it has no lifecycle clock")

    # The snapshot named has to be the one this copy was made from, or the
    # comparisons below are made against the wrong point in time.
    for older, newer, name in ((snapshot, conn, "the restored database"), (conn, snapshot, "the snapshot")):
        problem = lineage_problem(older, newer, name)
        if problem:
            raise OperatorError(f"--snapshot-db is not the snapshot this database was restored from: {problem}")

    problem = lineage_problem(conn, pre, "the pre-restore database")
    if problem:
        refuse(f"it is not a later state of this database ({problem}). The same ids are not the same rows")

    # A snapshot from before the lifecycle existed has no clock: every lifecycle
    # write there ever was came after it. The seed is where a clock stands that
    # has never admitted one.
    reference = lifecycle.parse_timestamp(lifecycle.CLOCK_SEED) if snapshot_mark is None else snapshot_mark
    stood = ("had not started: the snapshot is from before the lifecycle existed" if snapshot_mark is None
             else f"stood at {lifecycle.format_timestamp(snapshot_mark)}")
    if evidence_mark < reference:
        refuse(f"it is older than the snapshot (its lifecycle clock stopped at "
               f"{lifecycle.format_timestamp(evidence_mark)}, where the snapshot's {stood})")
    if evidence_mark == reference:
        refuse(f"it shows no lifecycle activity after the snapshot: its lifecycle clock stands at "
               f"{lifecycle.format_timestamp(evidence_mark)}, exactly where the snapshot's {stood}. A copy of "
               "the snapshot looks like this, and so does one in which something else was changed since: an "
               "account, a sign-in or an edited title says nothing about deadlines and closures. If this is "
               "the database that was live, no test was started, submitted, created, edited or closed in it "
               "after the snapshot, and that cannot be told from a copy")
    return f"its lifecycle clock ran to {lifecycle.format_timestamp(evidence_mark)}; the snapshot's {stood}"


def _carry_clock(conn: sqlite3.Connection, pre_clock, wall: datetime) -> dict:
    """Leave the restored clock ``suspect``, knowing everything the replaced database's clock knew.

    The high-water mark becomes the later of the two: recovery classifies every
    deadline up to it. An incident that was open before the restore is still
    open after it, with its reason and reading.
    """
    current, _problem = _clock_row_or_none(conn)
    marks = [mark for mark in (None if current is None else current.high_water_at,
                               None if pre_clock is None else pre_clock.high_water_at) if mark is not None]
    high_water = lifecycle.format_timestamp(max(marks) if marks else wall)
    if pre_clock is not None and pre_clock.state == lifecycle.CLOCK_STATE_SUSPECT:
        reason, reading = pre_clock.state_reason, pre_clock.state_changed_at
    elif current is not None and current.state == lifecycle.CLOCK_STATE_SUSPECT:
        reason, reading = current.state_reason, current.state_changed_at
    else:
        reason, reading = lifecycle.CLOCK_REASON_RESTORE, lifecycle.format_timestamp(wall)
    conn.execute("DELETE FROM lifecycle_clock")
    conn.execute(
        "INSERT INTO lifecycle_clock (id, high_water_at, state, state_reason, state_changed_at) "
        "VALUES (1, ?, 'suspect', ?, ?)", (high_water, reason, reading))
    return {"state": "suspect", "reason": reason, "fault_reading": reading, "high_water": high_water}


def cmd_restore_hold(conn, args, out, audit: AuditLog) -> int:
    """Run immediately after a restore, before the service is started.

    Every assignment gets ``restore_pending`` in addition to the reasons it has.
    The clock state becomes ``suspect``. When the pre-restore database is
    readable, its holds and its clock incident are merged in: a restore weakens
    no hold and forgets no incident. A hold of that database which this build
    cannot read is carried as the exact text it is (:func:`carry_hold`). A hold
    is carried only onto the same assignment, never onto another one that
    happens to have the same id. Running it again changes nothing.
    """
    pre = _open_pre_restore(conn, args.pre_restore_db) if args.pre_restore_db else None
    try:
        conn.execute("BEGIN IMMEDIATE")
        ids = [int(row[0]) for row in conn.execute("SELECT id FROM quiz_assignments ORDER BY id")]
        added = sum(add_hold_reason(conn, assignment_id, lifecycle.HOLD_RESTORE_PENDING) for assignment_id in ids)

        carried, strangers = {}, []
        pre_clock = None
        if pre is not None:
            shared = set(_columns(conn, "quiz_assignments")) & set(_columns(pre, "quiz_assignments"))
            identity = ", ".join(["id", *(c for c in (*ASSIGNMENT_FIXED, *ASSIGNMENT_SET_ONCE) if c in shared)])
            here = {int(row["id"]): row for row in conn.execute(f"SELECT {identity} FROM quiz_assignments")}
            for row in pre.execute(
                    f"SELECT {identity}, lifecycle_hold FROM quiz_assignments "
                    "WHERE lifecycle_hold IS NOT NULL ORDER BY id"):
                ours = here.get(int(row["id"]))
                if ours is None:
                    continue
                if _same_row_problem(ours, row, ASSIGNMENT_FIXED, ASSIGNMENT_SET_ONCE, shared):
                    strangers.append(int(row["id"]))   # the same number, another assignment
                    continue
                # The stored value is handed over as it is: carry_hold judges it before
                # it is split, and an unreadable one arrives byte for byte.
                if carry_hold(conn, int(row["id"]), row["lifecycle_hold"]):
                    carried[int(row["id"])] = row["lifecycle_hold"]
            pre_clock, _ = _clock_row_or_none(pre)

        clock = _carry_clock(conn, pre_clock, lifecycle.wall_reading())
        audit.record("restore_hold", assignments=len(ids), restore_pending_added=added,
                     pre_restore_db=args.pre_restore_db,
                     reasons_carried_over={str(key): value for key, value in carried.items()},
                     not_the_same_assignment=strangers, clock=clock)
        conn.commit()
    finally:
        if pre is not None:
            pre.close()
    print(f"restore_pending on {len(ids)} assignment(s) ({added} newly added)", file=out)
    print(f"assignments whose holds were carried over from the pre-restore database: {len(carried)}", file=out)
    if strangers:
        print("held there under the same id, but a different assignment (nothing carried): "
              + ", ".join(map(str, strangers)), file=out)
    print(f"clock state: suspect ({clock['reason']}); reconcile, then complete clock-recover", file=out)
    print("no assignment is served until its hold set is empty", file=out)
    return 0


def _strict_rows(conn: sqlite3.Connection, label: str) -> dict[int, sqlite3.Row]:
    rows = {}
    for row in conn.execute(f"SELECT {HOLD_COLUMNS} FROM quiz_assignments ORDER BY id"):
        for column in lifecycle.TIMESTAMP_COLUMNS:
            if row[column] is not None:
                try:
                    lifecycle.parse_timestamp(row[column])
                except lifecycle.TimestampError:
                    raise OperatorError(
                        f"{label}: assignment {row['id']} has an unreadable {column}; "
                        "its lifecycle columns do not qualify as evidence") from None
        if row["lifecycle_hold"] is not None and not isinstance(row["lifecycle_hold"], str):
            raise OperatorError(
                f"{label}: assignment {row['id']} has a lifecycle_hold that is not text; "
                "its lifecycle columns do not qualify as evidence")
        rows[int(row["id"])] = row
    return rows


def cmd_reconcile(conn, args, out, audit: AuditLog) -> int:
    """Reconcile from evidence A: the pre-restore database (13.4).

    It is the authoritative latest state, in either direction: a lost deadline
    extension is restored as much as a lost closure. Subject to the rules a
    restore can never break: no terminal marker is cleared or changed, the
    deadline of a row that carries a terminal marker is not touched, the
    retention floor only rises, hold reasons are merged, and only
    ``restore_pending`` is resolved. Evidence that contradicts the restored row
    leaves that assignment held with ``restore_unreconciled`` and changes nothing
    else of it.

    The evidence has to qualify first, as a whole (:func:`require_later_state`):
    the same database, and one whose lifecycle clock ran on after the snapshot.
    Evidence that does not is refused and nothing is released.

    It runs between ``restore-hold`` and ``clock-recover``, while the clock is
    still ``suspect``, and it hands the replaced database's clock on: recovery
    then classifies the reconciled deadlines and markers against everything that
    clock had reached. Run after recovery, it would bring in a deadline that
    recovery never saw, and is refused.

    This is the only command that resolves a restore reason. An assignment it
    leaves held stays held: no other evidence is accepted (the lifecycle event
    log of I5 is deferred, and application logs are not evidence). The exit
    status is 0 only when it left none; 3 says that it committed what it could
    and recovery is incomplete.
    """
    if not args.evidence_note.strip():
        raise OperatorError("--evidence-note records why the pre-restore database is trusted; it cannot be empty")
    pre = _open_pre_restore(conn, args.pre_restore_db)
    snapshot = None
    try:
        snapshot = _open_evidence(conn, args.snapshot_db, "snapshot database", "--snapshot-db")
        if os.path.samefile(args.snapshot_db, args.pre_restore_db):
            raise OperatorError("--pre-restore-db is the snapshot itself: a snapshot says nothing about what "
                                "happened after it was taken. Nothing was reconciled")
        evidence = _strict_rows(pre, "pre-restore database")
        try:
            pre_clock = lifecycle.read_clock_row(pre)
        except lifecycle.ClockRowError as exc:
            raise OperatorError(f"pre-restore database: its lifecycle clock row cannot be read ({exc}); "
                                "its lifecycle columns do not qualify as evidence") from None
        conn.execute("BEGIN IMMEDIATE")
        clock, problem = _clock_row_or_none(conn)
        if clock is None:
            raise OperatorError(f"the lifecycle clock row of the restored database is unusable ({problem}); "
                                "run restore-hold first")
        if clock.state != lifecycle.CLOCK_STATE_SUSPECT:
            raise Refused(
                "the clock of the restored database is already confirmed. reconcile runs after restore-hold and "
                "before clock-recover, because recovery has to classify the deadlines and markers that "
                "reconciliation brings in. Run restore-hold again, then reconcile, then clock-recover")
        later_because = require_later_state(conn, pre, snapshot)

        known_users = {int(row[0]) for row in conn.execute("SELECT id FROM users")}
        report = {"reconciled": [], "unreconciled": {}, "skipped": [], "unreadable_hold": []}
        for row in conn.execute(f"SELECT {HOLD_COLUMNS} FROM quiz_assignments ORDER BY id").fetchall():
            assignment_id = int(row["id"])
            if not hold_is_readable(row["lifecycle_hold"]):
                report["unreadable_hold"].append(assignment_id)  # nothing is resolved from it
                continue
            if lifecycle.HOLD_RESTORE_PENDING not in hold_tokens(row["lifecycle_hold"]):
                report["skipped"].append(assignment_id)
                continue
            source = evidence[assignment_id]   # present, and the same assignment: the lineage check
            conflicts = []
            if not hold_is_readable(source["lifecycle_hold"]):
                # Carried as the exact text it is (restore-hold normally did so already).
                carry_hold(conn, assignment_id, source["lifecycle_hold"])
                conflicts.append("the evidence row carries an unreadable hold, carried over as it is")
            elif any(token in RESTORE_REASONS for token in hold_tokens(source["lifecycle_hold"])):
                conflicts.append("the evidence row is itself unreconciled after an earlier restore")
            elif lifecycle.evaluate(row, lifecycle.wall_reading()).malformed:
                conflicts.append("the restored row has unreadable lifecycle timestamps")
            else:
                marked = any(row[column] is not None for column in lifecycle.TERMINAL_MARKER_COLUMNS)
                for column in lifecycle.TERMINAL_MARKER_COLUMNS:
                    if row[column] is not None and source[column] != row[column]:
                        conflicts.append(f"{column} is {row[column]} here and {source[column]} in the evidence")
                if marked and (row["deadline_at"] != source["deadline_at"]
                               or row["display_timezone"] != source["display_timezone"]):
                    conflicts.append("deadline or zone differs on a row that carries a terminal marker")
            if conflicts:
                add_hold_reason(conn, assignment_id, lifecycle.HOLD_RESTORE_UNRECONCILED)
                report["unreconciled"][str(assignment_id)] = conflicts
                continue

            if not any(row[column] is not None for column in lifecycle.TERMINAL_MARKER_COLUMNS):
                conn.execute(
                    "UPDATE quiz_assignments SET deadline_at = ?, display_timezone = ? "
                    "WHERE id = ? AND closed_at IS NULL AND expiry_observed_at IS NULL "
                    "AND submissions_closed_at IS NULL",
                    (source["deadline_at"], source["display_timezone"], assignment_id))
            if row["closed_at"] is None and source["closed_at"] is not None:
                closer = source["closed_by_user_id"]
                conn.execute(
                    "UPDATE quiz_assignments SET closed_at = ?, closed_by_user_id = ? "
                    "WHERE id = ? AND closed_at IS NULL",
                    (source["closed_at"], closer if closer in known_users else None, assignment_id))
            if row["expiry_observed_at"] is None and source["expiry_observed_at"] is not None:
                conn.execute(
                    "UPDATE quiz_assignments SET expiry_observed_at = ? WHERE id = ? AND expiry_observed_at IS NULL",
                    (source["expiry_observed_at"], assignment_id))
            if row["submissions_closed_at"] is None and source["submissions_closed_at"] is not None:
                conn.execute(
                    "UPDATE quiz_assignments SET submissions_closed_at = ? "
                    "WHERE id = ? AND submissions_closed_at IS NULL",
                    (source["submissions_closed_at"], assignment_id))
            if source["retention_base_at"] is not None:
                raise_retention_floor(conn, assignment_id, lifecycle.parse_timestamp(source["retention_base_at"]))
            for token in hold_tokens(source["lifecycle_hold"]):
                if token in lifecycle.HOLD_REASONS and token != lifecycle.HOLD_RESTORE_PENDING:
                    add_hold_reason(conn, assignment_id, token)
            resolve_hold_reason(conn, assignment_id, lifecycle.HOLD_RESTORE_PENDING)
            report["reconciled"].append(assignment_id)

        # The replaced database's clock is part of what is reconciled: its mark and
        # its open incident, so that clock-recover sees them.
        clock_carried = _carry_clock(conn, pre_clock, lifecycle.wall_reading())
        audit.record("reconcile", evidence="pre_restore_database", pre_restore_db=args.pre_restore_db,
                     snapshot_db=args.snapshot_db, evidence_note=args.evidence_note,
                     evidence_is_later_because=later_because, clock=clock_carried, **report)
        conn.commit()
    finally:
        pre.close()
        if snapshot is not None:
            snapshot.close()
    print(f"the pre-restore database qualifies as the later state: {later_because}", file=out)
    print(f"reconciled from the pre-restore database: {len(report['reconciled'])} assignment(s)", file=out)
    print(f"contradictory evidence, held with restore_unreconciled: {len(report['unreconciled'])}", file=out)
    for assignment_id, conflicts in report["unreconciled"].items():
        for conflict in conflicts:
            print(f"  assignment {assignment_id}: {conflict}", file=out)
    print(f"not pending, left untouched: {len(report['skipped'])}", file=out)
    if report["unreadable_hold"]:
        print("unreadable hold, left untouched and still held: assignment(s) "
              + ", ".join(map(str, report["unreadable_hold"])), file=out)
    print(f"clock state: suspect ({clock_carried['reason']}), high-water mark {clock_carried['high_water']}; "
          "complete clock-recover next: it classifies what was reconciled", file=out)
    print("an assignment is served only when no hold reason remains", file=out)
    left_held = len(report["unreconciled"]) + len(report["unreadable_hold"])
    if left_held:
        print(f"RECOVERY INCOMPLETE: {left_held} assignment(s) could not be reconciled and stay held. "
              "No other command releases them.", file=out)
        return EXIT_INCOMPLETE
    return EXIT_DONE


# Recovery from partial evidence (13.4, "when evidence is incomplete") is not
# provided. Both paths of the design rested on application log lines: releasing
# the assignments a log does not mention, and entering one assignment's state
# from the lines about it. A log is diagnostic. It cannot show that it is
# complete (a missing closure looks exactly like no closure), that its lines are
# genuine (they are text anyone can write), or in which order two commits of the
# same second happened. The commands remain as names, so that an operator who
# reaches for one is told why, and they open nothing.
UNSUPPORTED_COMMANDS = {
    "reconcile-partial": "releasing the assignments that an application log does not mention",
    "reconcile-record": "entering one assignment's state from application log lines",
}


def unsupported_explanation(command: str) -> str:
    return "\n".join((
        f"unsupported: {command} ({UNSUPPORTED_COMMANDS[command]}) is not available in this build.",
        "Nothing was read and nothing was written; every hold is exactly as it was.",
        "Application logs are diagnostic, not evidence of an assignment's state. A line can be missing,",
        "delayed, out of order or written by hand. A digest of the supplied text shows that the text was",
        "not changed afterwards, never that it is genuine, complete or in commit order. \"The log shows no",
        "closure\" does not establish that the assignment was not closed, and an assignment that the log",
        "does not mention is not thereby unaffected.",
        "A restore hold is resolved only by `reconcile --pre-restore-db`, from the database the restore",
        "replaced. An assignment that cannot be reconciled from it stays held and is not served. That is",
        "the intended outcome, not a failure to work around: do not clear a hold by hand to restore",
        "availability. The teacher's remedy is a new assignment made on the normal creation form.",
        "(The durable lifecycle event log that could be such evidence is decision I5 of",
        "docs/ASSIGNMENT_LIFECYCLE_DESIGN.md. It is deferred and does not exist.)",
    ))


def restore_held(conn: sqlite3.Connection) -> tuple[int, list[tuple[int, object]], list[tuple[int, object]]]:
    """``(assignments, held for the restore, held otherwise)`` as ``(id, stored hold)`` pairs.

    Held for the restore: the hold names a restore reason, or cannot be read.
    An unreadable hold counts because nothing establishes that it is not one.
    """
    rows = conn.execute("SELECT id, lifecycle_hold FROM quiz_assignments ORDER BY id").fetchall()
    for_restore, otherwise = [], []
    for row in rows:
        raw = row["lifecycle_hold"]
        if raw is None:
            continue
        if not hold_is_readable(raw) or any(token in RESTORE_REASONS for token in hold_tokens(raw)):
            for_restore.append((int(row["id"]), raw))
        else:
            otherwise.append((int(row["id"]), raw))
    return len(rows), for_restore, otherwise


def cmd_restore_status(conn, args, out) -> int:
    """Count the assignments a restore left held, and compare with what is expected. Read-only.

    ``--expect-held all``: the check after ``restore-hold``. Every assignment
    must be held for the restore; one that is not means the hold step did not
    cover the database, and nothing may be served from it.

    ``--expect-held N`` (default 0): the check before traffic is reopened. It
    succeeds only when exactly ``N`` assignments are still held for the restore.
    ``N`` above zero is an operator's acknowledgement that those assignments
    stay closed to everyone; it releases nothing, and a count that differs from
    the acknowledged one is refused.
    """
    total, for_restore, otherwise = restore_held(conn)
    print(f"assignments: {total}; held for the restore: {len(for_restore)}; "
          f"held for another reason only: {len(otherwise)}", file=out)
    for assignment_id, raw in for_restore:
        print(f"  assignment {assignment_id}: {raw!r}"
              + ("" if hold_is_readable(raw) else "  UNREADABLE"), file=out)

    if args.expect_held == "all":
        covered = {assignment_id for assignment_id, _raw in for_restore}
        uncovered = [int(row[0]) for row in conn.execute("SELECT id FROM quiz_assignments ORDER BY id")
                     if int(row[0]) not in covered]
        if uncovered:
            print("NOT HELD for the restore: assignment(s) " + ", ".join(map(str, uncovered))
                  + ". The restore-hold step did not cover this database; do not serve it.", file=out)
            return EXIT_REFUSED
        print("every assignment is held for the restore", file=out)
        return EXIT_DONE

    if len(for_restore) != args.expect_held:
        if for_restore:
            print(f"RECOVERY INCOMPLETE: {len(for_restore)} assignment(s) are still held for the restore "
                  f"(expected: {args.expect_held}). No command reopens them; they stay closed to everyone "
                  "until an authoritative record of their state exists.", file=out)
            return EXIT_INCOMPLETE
        print(f"no assignment is held for the restore, but {args.expect_held} were expected", file=out)
        return EXIT_REFUSED
    if for_restore:
        print(f"ACKNOWLEDGED, NOT RECOVERED: {len(for_restore)} assignment(s) stay held and are served to "
              "nobody. Nothing was released.", file=out)
    else:
        print("no assignment is held for the restore", file=out)
    return EXIT_DONE


# ---------------------------------------------------------------------------
# Verification before traffic is reopened (13.5)
# ---------------------------------------------------------------------------

def cmd_verify(conn, args, out, audit: AuditLog | None) -> int:
    """Read-only unless ``--hold-exceptions`` is given, which holds (with
    ``clock_review``) every assignment that has a factual timestamp or terminal
    marker later than the clock. A deadline in the future is expected; only one
    further ahead than validation could ever have accepted is reported, as a data
    anomaly, and it never changes the clock state."""
    findings: list[str] = []
    notes: list[str] = []

    if conn.execute("PRAGMA foreign_key_check").fetchall():
        findings.append("PRAGMA foreign_key_check reports violations")

    row, problem = _clock_row_or_none(conn)
    if row is None:
        findings.append(f"lifecycle clock row is unusable: {problem}")
    elif row.state != lifecycle.CLOCK_STATE_OK:
        findings.append(f"lifecycle clock state is {row.state} ({row.state_reason}); complete clock-recover")

    now = lifecycle.wall_reading()
    exceptions: dict[int, list[str]] = {}
    for assignment_id, pairs in _factual_values(conn).items():
        for label, raw in pairs:
            try:
                if lifecycle.parse_timestamp(raw) > now:
                    exceptions.setdefault(assignment_id, []).append(f"{label} {raw} is later than the clock")
            except lifecycle.TimestampError:
                findings.append(f"assignment {assignment_id}: {label} does not parse")

    anomaly_after = now + timedelta(days=lifecycle.DEADLINE_ANOMALY_DAYS)
    held = unreadable_holds = 0
    hold_summary: dict[str, int] = {}
    for assignment in conn.execute(f"SELECT {HOLD_COLUMNS} FROM quiz_assignments ORDER BY id"):
        assignment_id = int(assignment["id"])
        view = lifecycle.evaluate(assignment, now)
        for column in view.malformed:
            findings.append(f"assignment {assignment_id}: {column} does not parse")
        if view.deadline_at is not None and view.deadline_at > anomaly_after:
            notes.append(f"data anomaly: assignment {assignment_id} has a deadline {assignment['deadline_at']}, "
                         "further ahead than validation could have accepted")
        if assignment["closed_by_user_id"] is not None and assignment["closed_at"] is None:
            findings.append(f"assignment {assignment_id}: closed_by_user_id without closed_at")
        if assignment["lifecycle_hold"] is not None:
            held += 1
            hold_summary[assignment["lifecycle_hold"]] = hold_summary.get(assignment["lifecycle_hold"], 0) + 1
            if not lifecycle.parse_hold(assignment["lifecycle_hold"])[1]:
                unreadable_holds += 1
                findings.append(f"assignment {assignment_id}: lifecycle_hold is unreadable (it stays held)")

    try:
        known = lifecycle.deadline_cutoff(datetime(2026, 4, 3).date(), lifecycle.DEFAULT_DEADLINE_TIME,
                                          lifecycle.DEFAULT_TIMEZONE, typed=True)
        if lifecycle.format_timestamp(known) != "2026-04-03 21:00:00" or len(lifecycle.timezone_names()) < 100:
            findings.append("the timezone database gives unexpected results")
    except Exception as exc:  # zone data missing or broken
        findings.append(f"the timezone database is not available ({type(exc).__name__})")

    unheld = {assignment_id: reasons for assignment_id, reasons in exceptions.items()
              if conn.execute("SELECT lifecycle_hold FROM quiz_assignments WHERE id = ?",
                              (assignment_id,)).fetchone()[0] is None}
    if args.hold_exceptions and unheld:
        conn.execute("BEGIN IMMEDIATE")
        for assignment_id in sorted(unheld):
            add_hold_reason(conn, assignment_id, lifecycle.HOLD_CLOCK_REVIEW)
        audit.record("verify_hold_exceptions",
                     held={str(key): value for key, value in sorted(unheld.items())})
        conn.commit()
        held += len(unheld)
        unheld = {}

    for assignment_id in sorted(exceptions):
        for reason in exceptions[assignment_id]:
            state = "NOT HELD" if assignment_id in unheld else "held"
            print(f"exception ({state}): assignment {assignment_id}: {reason}", file=out)
    for assignment_id in sorted(unheld):
        findings.append(f"assignment {assignment_id} has a factual timestamp ahead of the clock and is not held "
                        "(rerun with --hold-exceptions)")
    for note in notes:
        print(note, file=out)
    print(f"held assignments: {held}"
          + ("" if not hold_summary else " (" + "; ".join(
              f"{count} x {reasons}" for reasons, count in
              sorted(hold_summary.items(), key=lambda item: str(item[0]))) + ")"), file=out)
    for finding in findings:
        print(f"FAIL: {finding}", file=out)
    if findings:
        return 1
    print("OK: lifecycle state verifies. Still to do by hand: verify the clock against an independent "
          "source, and smoke-test that a closed assignment refuses a start and an edit.", file=out)
    return 0


# ---------------------------------------------------------------------------
# Audit after accidental exposure to pre-lifecycle code (13.3). Read-only.
# ---------------------------------------------------------------------------

def cmd_exposure_audit(conn, args, out) -> int:
    """List what non-enforcing code may have let through. Nothing is modified."""
    start, end = parse_utc(args.exposure_from, "--from"), parse_utc(args.exposure_to, "--to")
    if end < start:
        raise OperatorError("--to is before --from")
    grace = timedelta(seconds=lifecycle.GRACE_SECONDS)
    lines: list[str] = []
    for assignment in conn.execute(f"SELECT {HOLD_COLUMNS}, title_bg, time_limit_minutes FROM quiz_assignments"):
        view = lifecycle.evaluate(assignment, end)
        closure = view.effective_closure
        if closure is None:
            continue
        assignment_id = int(assignment["id"])
        manual = view.closed_at is not None and (view.deadline_at is None or view.closed_at < view.deadline_at)
        for attempt in conn.execute(
                "SELECT id, started_at, submitted_at FROM quiz_attempts WHERE assignment_id = ? ORDER BY id",
                (assignment_id,)):
            try:
                started = lifecycle.parse_timestamp(attempt["started_at"])
            except lifecycle.TimestampError:
                lines.append(f"attempt {attempt['id']} (assignment {assignment_id}): started_at does not parse")
                continue
            if manual and started == closure:
                lines.append(f"AMBIGUOUS attempt {attempt['id']} (assignment {assignment_id}): started in the "
                             f"same second as the manual close {assignment['closed_at']}")
            elif not lifecycle.attempt_in_flight(view, attempt["started_at"]):
                lines.append(f"attempt {attempt['id']} (assignment {assignment_id}): started "
                             f"{attempt['started_at']}, after the closure")
            if attempt["submitted_at"] is None:
                continue
            try:
                submitted = lifecycle.parse_timestamp(attempt["submitted_at"])
            except lifecycle.TimestampError:
                lines.append(f"attempt {attempt['id']} (assignment {assignment_id}): submitted_at does not parse")
                continue
            if submitted >= closure + grace:
                lines.append(f"attempt {attempt['id']} (assignment {assignment_id}): submitted "
                             f"{attempt['submitted_at']}, at or after the cutoff")
            elif closure <= submitted and start <= submitted <= end:
                lines.append(f"attempt {attempt['id']} (assignment {assignment_id}): submitted "
                             f"{attempt['submitted_at']} inside the exposure interval on a closed assignment "
                             "(in its grace window; listed for completeness)")
        cutoff_text = lifecycle.format_timestamp(closure + grace)
        for upload in conn.execute(
                "SELECT psf.id, psf.uploaded_at, ps.quiz_attempt_id FROM practical_submission_files psf "
                "JOIN practical_submissions ps ON ps.id = psf.practical_submission_id "
                "JOIN quiz_attempts qt ON qt.id = ps.quiz_attempt_id "
                "WHERE qt.assignment_id = ? AND psf.uploaded_at >= ? ORDER BY psf.id",
                (assignment_id, cutoff_text)):
            lines.append(f"practical file {upload['id']} (attempt {upload['quiz_attempt_id']}, assignment "
                         f"{assignment_id}): uploaded {upload['uploaded_at']}, at or after the cutoff")

    if args.snapshot_db:
        snapshot = open_database(Path(args.snapshot_db), read_only=True)
        try:
            before = {int(row["id"]): row for row in snapshot.execute(
                "SELECT id, title_bg, time_limit_minutes FROM quiz_assignments")}
            for assignment in conn.execute(f"SELECT {HOLD_COLUMNS}, title_bg, time_limit_minutes FROM quiz_assignments"):
                old = before.get(int(assignment["id"]))
                if old is None or not lifecycle.evaluate(assignment, start).is_closed:
                    continue
                if (old["title_bg"], old["time_limit_minutes"]) != (assignment["title_bg"],
                                                                     assignment["time_limit_minutes"]):
                    lines.append(f"assignment {assignment['id']}: title or time limit differs from the "
                                 "pre-exposure snapshot although it was closed")
            snapshot_attempts = {int(row["id"]): row for row in snapshot.execute(
                "SELECT id, submitted_at, score_correct, score_total FROM quiz_attempts")}
            for attempt in conn.execute("SELECT id, submitted_at, score_correct, score_total FROM quiz_attempts"):
                old = snapshot_attempts.get(int(attempt["id"]))
                if old is not None and old["submitted_at"] is not None and tuple(old) != tuple(attempt):
                    lines.append(f"attempt {attempt['id']}: its submission differs from the pre-exposure "
                                 "snapshot (possibly overwritten by a second POST)")
        finally:
            snapshot.close()
    else:
        print("no --snapshot-db: edits to closed assignments and overwritten submissions cannot be detected "
              "from the live data alone", file=out)

    print(f"exposure audit {args.exposure_from} .. {args.exposure_to}: {len(lines)} finding(s)", file=out)
    for line in lines:
        print(f"  {line}", file=out)
    print("nothing was modified; what to do with this work is decided by the teacher and the operator", file=out)
    return 1 if lines else 0


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="manage_lifecycle",
        description="Operator tooling for the assignment lifecycle: clock recovery, holds, restore "
                    "reconciliation and verification. Closes nothing and deletes nothing.")
    parser.add_argument("--db", help="SQLite database path (or set DZI_DB). Never defaulted.")
    commands = parser.add_subparsers(dest="command", required=True)

    def writer(sub):
        sub.add_argument("--operator", required=True, help="Who runs this; recorded in the audit log.")
        sub.add_argument("--audit-log", required=True, help="Append-only JSON-lines file of operator actions.")

    def clock_options(sub):
        sub.add_argument("--verified-utc", required=True,
                         help='Current UTC time read from a source outside this host, "YYYY-MM-DD HH:MM:SS".')
        sub.add_argument("--suspect-from", help="Start (UTC) of the interval in which the clock was wrong.")
        sub.add_argument("--suspect-to", help="End (UTC) of that interval.")

    commands.add_parser("clock-status", help="Show the clock row and the host's clock evidence.")
    clock_options(commands.add_parser("clock-inspect", help="List what clock-recover would hold; writes nothing."))
    recover = commands.add_parser("clock-recover", help="Hold what is uncertain and confirm the clock.")
    clock_options(recover)
    writer(recover)

    commands.add_parser("hold-list", help="List held assignments and their reasons.")
    resolve = commands.add_parser("hold-resolve", help="Resolve clock_review for one assignment after review.")
    resolve.add_argument("--assignment", type=int, required=True)
    resolve.add_argument("--reason", required=True, choices=lifecycle.HOLD_REASONS)
    resolve.add_argument("--evidence", required=True, help="What was reviewed and against which clock.")
    writer(resolve)

    restore = commands.add_parser("restore-hold", help="After a restore, before the service starts: hold everything.")
    restore.add_argument("--pre-restore-db", help="The database the restore replaces, if readable.")
    writer(restore)

    reconcile = commands.add_parser(
        "reconcile", help="Reconcile from the pre-restore database (evidence A); the only release of a restore hold.")
    reconcile.add_argument("--pre-restore-db", required=True,
                           help="The database the restore replaces: the one that was live.")
    reconcile.add_argument("--snapshot-db", required=True,
                           help="The untouched snapshot the restored database was made from; evidence that is "
                                "older than it, or shows nothing later than it, is refused.")
    reconcile.add_argument("--evidence-note", required=True,
                           help="Why the reason for the restore does not put its lifecycle columns in doubt.")
    writer(reconcile)

    def expected_held(text):
        if text == "all":
            return text
        try:
            value = int(text)
        except ValueError:
            value = -1
        if value < 0:
            raise argparse.ArgumentTypeError('a number of assignments (0 or more), or "all"')
        return value

    status = commands.add_parser(
        "restore-status", help="Count the assignments a restore left held; succeeds only on the expected number.")
    status.add_argument("--expect-held", type=expected_held, default=0, metavar="N|all",
                        help='How many assignments may still be held for the restore (default 0), or "all" '
                             "directly after restore-hold.")

    for name, what in UNSUPPORTED_COMMANDS.items():
        commands.add_parser(name, help=f"Not available ({what}): explains why and changes nothing.")

    verify = commands.add_parser("verify", help="Lifecycle checks before traffic is reopened.")
    verify.add_argument("--hold-exceptions", action="store_true",
                        help="Hold assignments that have a factual timestamp ahead of the clock.")
    verify.add_argument("--operator")
    verify.add_argument("--audit-log")

    exposure = commands.add_parser("exposure-audit", help="Report what non-enforcing code may have let through.")
    exposure.add_argument("--from", dest="exposure_from", required=True)
    exposure.add_argument("--to", dest="exposure_to", required=True)
    exposure.add_argument("--snapshot-db", help="The last snapshot taken before the exposure, for comparison.")
    return parser


READ_ONLY_COMMANDS = {
    "clock-status": cmd_clock_status,
    "clock-inspect": cmd_clock_inspect,
    "hold-list": cmd_hold_list,
    "restore-status": cmd_restore_status,
    "exposure-audit": cmd_exposure_audit,
}
WRITING_COMMANDS = {
    "clock-recover": cmd_clock_recover,
    "hold-resolve": cmd_hold_resolve,
    "restore-hold": cmd_restore_hold,
    "reconcile": cmd_reconcile,
}


def main(argv: list[str] | None = None, *, out: TextIO | None = None, err: TextIO | None = None) -> int:
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    parser = build_parser()
    args, extra = parser.parse_known_args(argv)
    if args.command in UNSUPPORTED_COMMANDS:
        # Whatever it was given: no database is opened and no file is read.
        print(unsupported_explanation(args.command), file=err)
        return EXIT_UNSUPPORTED
    if extra:
        parser.error("unrecognized arguments: " + " ".join(extra))
    target = args.db or os.environ.get("DZI_DB")
    conn = None
    try:
        if not target:
            raise OperatorError("no database given: pass --db or set DZI_DB")
        database = Path(target)
        writes = args.command in WRITING_COMMANDS or (args.command == "verify" and args.hold_exceptions)
        audit = None
        if writes:
            if not (args.operator or "").strip() or not args.audit_log:
                raise OperatorError("a command that writes needs --operator and --audit-log")
            audit = AuditLog(Path(args.audit_log), args.operator.strip(), database, out)
        conn = open_database(database, read_only=not writes)
        require_matching_schema(conn)
        if args.command in READ_ONLY_COMMANDS:
            return READ_ONLY_COMMANDS[args.command](conn, args, out)
        if args.command == "verify":
            return cmd_verify(conn, args, out, audit)
        return WRITING_COMMANDS[args.command](conn, args, out, audit)
    except Refused as exc:
        print(f"refused: {exc}", file=err)
        return EXIT_REFUSED
    except OperatorError as exc:
        print(f"error: {exc}", file=err)
        return EXIT_ERROR
    except sqlite3.Error as exc:
        print(f"error: database problem: {exc}", file=err)
        return EXIT_ERROR
    finally:
        if conn is not None:
            if conn.in_transaction:
                conn.rollback()
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
