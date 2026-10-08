#!/usr/bin/env python3
"""Operator CLI: give existing assignments their public token (migration 017).

Migration 017 adds ``quiz_assignments.public_token`` as a nullable column, so
assignments created before it have no student link (``/quiz/s/<public_token>``)
until this command fills them in. New assignments get a token when they are created;
the application never runs this command and never fills a token at startup.

    python3 -m src.manage_assignment_tokens --db "$DZI_DB" --dry-run
    python3 -m src.manage_assignment_tokens --db "$DZI_DB"

The target database must be named explicitly (``--db`` or ``DZI_DB``); it is never
defaulted to ``data/questions.db``, never created and never migrated. ``--dry-run``
only reads: the database is opened read-only and query-only, no token is generated,
and no transaction or writer lock is taken. Only NULL
tokens are written: an existing token is never replaced, and no other column or
table is touched. The whole run is one transaction, so an interrupted run changes
nothing and running it again continues with whatever is still NULL.

Tokens come from ``secrets.token_urlsafe(32)`` (see ``web.assignment_tokens``). The
report contains counts and assignment ids only, never a token. Exit status: 0 when
every assignment has a unique valid token (with ``--dry-run``: would have one), 1
when it does not, 2 for an operator or database problem.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path
from typing import NamedTuple, TextIO

try:
    from web import assignment_tokens
except ModuleNotFoundError:  # run as `python3 src/manage_assignment_tokens.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from web import assignment_tokens

MAX_REPORTED_IDS = 20


class OperatorError(Exception):
    """An expected operator/input/database problem: reported without a traceback."""


class TokenState(NamedTuple):
    total: int
    missing_ids: list[int]
    invalid_ids: list[int]
    duplicate_ids: list[int]

    @property
    def valid(self) -> int:
        return self.total - len(self.missing_ids) - len(self.invalid_ids)

    @property
    def complete(self) -> bool:
        return not (self.missing_ids or self.invalid_ids or self.duplicate_ids)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="manage_assignment_tokens",
        description="Fill missing public tokens of existing quiz assignments.",
    )
    parser.add_argument("--db", help="SQLite database path (or set DZI_DB). Never defaulted.")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report what would change and write nothing.",
    )
    return parser


def open_database(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    """Open an existing database (never creating it) and require migration 017.

    Read-write by default. ``read_only`` (the dry run) opens the file read-only and
    sets ``query_only``, so no statement on the connection can write or take a
    writer lock.
    """
    if not path.is_file():
        raise OperatorError(f"database not found: {path}")
    try:
        # Autocommit: the apply transaction is opened and closed explicitly.
        mode = "ro" if read_only else "rw"
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode={mode}", uri=True, isolation_level=None)
        if read_only:
            conn.execute("PRAGMA query_only = ON")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(quiz_assignments)")}
    except sqlite3.Error as exc:
        raise OperatorError(f"cannot open database {path}: {exc}") from exc
    if "public_token" not in columns:
        conn.close()
        raise OperatorError(
            "database has no quiz_assignments.public_token (migration 017 required); "
            "apply migrations with src.manage_migrations first"
        )
    return conn


def read_state(conn: sqlite3.Connection) -> TokenState:
    rows = conn.execute("SELECT id, public_token FROM quiz_assignments ORDER BY id").fetchall()
    missing, invalid, duplicates = [], [], []
    seen: set[str] = set()
    for assignment_id, token in rows:
        if token is None:
            missing.append(assignment_id)
        elif not assignment_tokens.is_valid_public_token(token):
            invalid.append(assignment_id)
        elif token in seen:
            # The unique index forbids this; reported in case the index is missing.
            duplicates.append(assignment_id)
        else:
            seen.add(token)
    return TokenState(len(rows), missing, invalid, duplicates)


def fill_missing(conn: sqlite3.Connection, assignment_ids: list[int]) -> int:
    """Give each listed assignment a token if it is still NULL; return how many were set."""
    filled = 0
    for assignment_id in assignment_ids:
        def write(token: str, assignment_id: int = assignment_id) -> int:
            return conn.execute(
                "UPDATE quiz_assignments SET public_token = ? WHERE id = ? AND public_token IS NULL",
                (token, assignment_id),
            ).rowcount

        filled += assignment_tokens.write_with_new_public_token(conn, write)
    return filled


def _ids(ids: list[int]) -> str:
    shown = ", ".join(str(i) for i in ids[:MAX_REPORTED_IDS])
    return shown + (f", ... ({len(ids)} in total)" if len(ids) > MAX_REPORTED_IDS else "")


def report_state(state: TokenState, out: TextIO) -> bool:
    """Print the counts; ``False`` (with the ids) if an existing token is not valid."""
    print(f"assignments: {state.total}", file=out)
    print(f"with a valid token: {state.valid}", file=out)
    print(f"without a token: {len(state.missing_ids)}", file=out)
    print(f"with an invalid token: {len(state.invalid_ids)}", file=out)
    if not (state.invalid_ids or state.duplicate_ids):
        return True
    # Unexpected state is never repaired or overwritten here.
    if state.invalid_ids:
        print(f"invalid token on assignment ids: {_ids(state.invalid_ids)}", file=out)
    if state.duplicate_ids:
        print(f"duplicate token on assignment ids: {_ids(state.duplicate_ids)}", file=out)
    print("FAIL: existing tokens are not valid; nothing was changed", file=out)
    return False


def dry_run(conn: sqlite3.Connection, *, out: TextIO) -> int:
    """Inspect only: one SELECT, no transaction, no token, no write, no writer lock."""
    state = read_state(conn)
    if not report_state(state, out):
        return 1
    print(f"dry run: {len(state.missing_ids)} token(s) would be generated; nothing was changed", file=out)
    return 0


def run(conn: sqlite3.Connection, *, out: TextIO) -> int:
    conn.execute("BEGIN IMMEDIATE")
    try:
        before = read_state(conn)
        if not report_state(before, out):
            conn.execute("ROLLBACK")
            return 1

        filled = fill_missing(conn, before.missing_ids)
        after = read_state(conn)
        if filled != len(before.missing_ids) or not after.complete or after.total != before.total:
            conn.execute("ROLLBACK")
            print("FAIL: verification after the backfill did not pass; nothing was changed", file=out)
            return 1
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    print(f"tokens generated: {filled}", file=out)
    print(f"OK: all {after.total} assignment(s) have a unique valid token", file=out)
    return 0


def main(argv: list[str] | None = None, *, out: TextIO | None = None, err: TextIO | None = None) -> int:
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    args = build_parser().parse_args(argv)
    target = args.db or os.environ.get("DZI_DB")
    try:
        if not target:
            raise OperatorError("no database given: pass --db or set DZI_DB")
        conn = open_database(Path(target), read_only=args.dry_run)
        try:
            return dry_run(conn, out=out) if args.dry_run else run(conn, out=out)
        finally:
            conn.close()
    except (OperatorError, assignment_tokens.PublicTokenError) as exc:
        print(f"error: {exc}", file=err)
        return 2
    except sqlite3.Error as exc:
        print(f"error: database problem: {exc}", file=err)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
