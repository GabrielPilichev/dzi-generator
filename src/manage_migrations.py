#!/usr/bin/env python3
"""Migration planner/runner for numbered SQLite migrations."""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import TextIO


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = Path("data/questions.db")
DEFAULT_MIGRATIONS_DIR = Path("web/migrations")
BACKUP_DIR_NAME = "local_backups"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plan or apply SQLite migrations.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite DB path.")
    parser.add_argument("--migrations-dir", default=str(DEFAULT_MIGRATIONS_DIR), help="Directory with *.sql migrations.")
    parser.add_argument("--dry-run", action="store_true", help="Print pending migrations without applying changes.")
    parser.add_argument("--apply", action="store_true", help="Apply pending migrations.")
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


def open_writable_db(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def default_real_db_path() -> Path:
    return (PROJECT_ROOT / DEFAULT_DB_PATH).resolve()


def is_default_real_db_path(db_path: Path) -> bool:
    return db_path.resolve() == default_real_db_path()


def create_backup_for_default_db(db_path: Path) -> Path:
    backup_dir = PROJECT_ROOT / BACKUP_DIR_NAME
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = backup_dir / f"questions.db.backup-{timestamp}"
    shutil.copy2(db_path, backup_path)
    if not backup_path.exists():
        raise RuntimeError(f"Backup was not created: {backup_path}")
    return backup_path


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


def ensure_schema_migrations(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            filename TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)


def foreign_key_check_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("PRAGMA foreign_key_check").fetchall()


def sql_string_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


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


def apply_migrations(db_path: Path, migrations_dir: Path, out: TextIO = sys.stdout) -> int:
    all_migrations = migration_filenames(migrations_dir)
    if is_default_real_db_path(db_path):
        backup_path = create_backup_for_default_db(db_path)
        print(f"backup path: {backup_path}", file=out)

    conn = open_writable_db(db_path)
    try:
        ensure_schema_migrations(conn)
        conn.commit()

        applied = applied_migrations(conn)
        pending = pending_migrations(all_migrations, applied)

        print(f"database path: {db_path}", file=out)
        print(f"migration directory: {migrations_dir}", file=out)
        print("skipped migrations:", file=out)
        skipped = [filename for filename in all_migrations if filename in set(applied)]
        if skipped:
            for filename in skipped:
                print(f"  - {filename}", file=out)
        else:
            print("  - none", file=out)

        print("applied migrations:", file=out)
        if not pending:
            print("  - none", file=out)

        for filename in pending:
            sql = (migrations_dir / filename).read_text(encoding="utf-8")
            script = f"""
BEGIN;
{sql}
INSERT INTO schema_migrations (filename, applied_at)
VALUES ({sql_string_literal(filename)}, CURRENT_TIMESTAMP);
COMMIT;
"""
            try:
                conn.executescript(script)
            except sqlite3.Error:
                conn.rollback()
                raise
            print(f"  - {filename}", file=out)

        fk_rows = foreign_key_check_rows(conn)
        if fk_rows:
            print(f"foreign_key_check failed: {len(fk_rows)} row(s)", file=out)
            for row in fk_rows:
                print(f"  - {tuple(row)}", file=out)
            return 1

        print("foreign_key_check: ok", file=out)
        return 0
    finally:
        conn.close()


def main(argv: list[str] | None = None, out: TextIO = sys.stdout, err: TextIO = sys.stderr) -> int:
    args = parse_args(argv)

    try:
        if args.apply:
            return apply_migrations(Path(args.db), Path(args.migrations_dir), out)
        dry_run(Path(args.db), Path(args.migrations_dir), out)
    except (FileNotFoundError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=err)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
