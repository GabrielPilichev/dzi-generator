#!/usr/bin/env python3
"""Dry-run-only migration planner for numbered SQLite migrations."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import TextIO


DEFAULT_DB_PATH = Path("data/questions.db")
DEFAULT_MIGRATIONS_DIR = Path("web/migrations")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plan SQLite migrations without applying them.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite DB path.")
    parser.add_argument("--migrations-dir", default=str(DEFAULT_MIGRATIONS_DIR), help="Directory with *.sql migrations.")
    parser.add_argument("--dry-run", action="store_true", help="Print pending migrations without applying changes.")
    parser.add_argument("--apply", action="store_true", help="Not implemented yet.")
    return parser.parse_args(argv)


def migration_filenames(migrations_dir: Path) -> list[str]:
    if not migrations_dir.exists():
        raise FileNotFoundError(f"Migration directory not found: {migrations_dir}")
    return sorted(path.name for path in migrations_dir.glob("*.sql") if path.is_file())


def open_read_only_db(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def schema_migrations_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute("""
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table'
          AND name = 'schema_migrations'
    """).fetchone()
    return row is not None


def applied_migrations(conn: sqlite3.Connection) -> list[str]:
    if not schema_migrations_exists(conn):
        return []
    rows = conn.execute("""
        SELECT filename
        FROM schema_migrations
        ORDER BY filename
    """).fetchall()
    return [str(row["filename"]) for row in rows]


def pending_migrations(all_migrations: list[str], applied: list[str]) -> list[str]:
    applied_set = set(applied)
    return [filename for filename in all_migrations if filename not in applied_set]


def print_plan(db_path: Path, migrations_dir: Path, applied: list[str], pending: list[str], out: TextIO) -> None:
    print(f"database path: {db_path}", file=out)
    print(f"migration directory: {migrations_dir}", file=out)
    print("applied migrations:", file=out)
    if applied:
        for filename in applied:
            print(f"  - {filename}", file=out)
    else:
        print("  - none", file=out)
    print("pending migrations:", file=out)
    if pending:
        for filename in pending:
            print(f"  - {filename}", file=out)
    else:
        print("  - none", file=out)
    print("DRY RUN ONLY: no changes applied", file=out)


def dry_run(db_path: Path, migrations_dir: Path, out: TextIO = sys.stdout) -> None:
    all_migrations = migration_filenames(migrations_dir)
    conn = open_read_only_db(db_path)
    try:
        applied = applied_migrations(conn)
    finally:
        conn.close()
    pending = pending_migrations(all_migrations, applied)
    print_plan(db_path, migrations_dir, applied, pending, out)


def main(argv: list[str] | None = None, out: TextIO = sys.stdout, err: TextIO = sys.stderr) -> int:
    args = parse_args(argv)
    if args.apply:
        print("--apply is not implemented yet", file=err)
        return 2

    try:
        dry_run(Path(args.db), Path(args.migrations_dir), out)
    except (FileNotFoundError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=err)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
