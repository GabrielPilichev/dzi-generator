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

try:
    from src.schema_introspection import shape_requirement_problems, table_shape, user_table_names
except ModuleNotFoundError:  # run as `python3 src/manage_migrations.py`
    from schema_introspection import shape_requirement_problems, table_shape, user_table_names


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = Path("data/questions.db")
DEFAULT_MIGRATIONS_DIR = Path("web/migrations")
BACKUP_DIR_NAME = "local_backups"
BASELINE_FILENAME = "000_baseline.sql"
MIGRATION_BOOKKEEPING_TABLES = ("schema_migrations", "sqlite_sequence")


class BaselineRequired(RuntimeError):
    """A non-empty database must adopt 000 with --baseline-apply, not run it."""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plan or apply SQLite migrations.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite DB path.")
    parser.add_argument("--migrations-dir", default=str(DEFAULT_MIGRATIONS_DIR), help="Directory with *.sql migrations.")
    parser.add_argument("--dry-run", action="store_true", help="Print pending migrations without applying changes.")
    parser.add_argument("--baseline-dry-run", action="store_true", help="Plan adoption of existing migrations without writing.")
    parser.add_argument("--baseline-apply", action="store_true", help="Record baselineable existing migrations (including 000) without executing SQL.")
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


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute("""
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table'
          AND name = ?
    """, (table_name,)).fetchone()
    return row is not None


def index_exists(conn: sqlite3.Connection, index_name: str) -> bool:
    row = conn.execute("""
        SELECT 1
        FROM sqlite_master
        WHERE type = 'index'
          AND name = ?
    """, (index_name,)).fetchone()
    return row is not None


def columns_exist(conn: sqlite3.Connection, table_name: str, column_names: list[str]) -> bool:
    if not table_exists(conn, table_name):
        return False
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    existing = {str(row["name"]) for row in rows}
    return all(column_name in existing for column_name in column_names)


def database_is_empty(conn: sqlite3.Connection) -> bool:
    """True when the database holds nothing but migration bookkeeping."""
    placeholders = ", ".join("?" for _ in MIGRATION_BOOKKEEPING_TABLES)
    row = conn.execute(
        f"SELECT COUNT(*) FROM sqlite_master WHERE tbl_name NOT IN ({placeholders})",
        MIGRATION_BOOKKEEPING_TABLES,
    ).fetchone()
    return row[0] == 0


def baseline_schema_problems(conn: sqlite3.Connection, baseline_sql: str) -> list[str]:
    """Why ``conn`` does not already contain the schema that 000 would create.

    The expected schema is whatever 000 itself builds in a scratch in-memory
    database, so the check cannot drift from the file. Every baseline table must
    exist with its columns (type, NOT NULL, default, primary key), foreign keys,
    indexes, CHECK constraints and AUTOINCREMENT. Extra columns and objects are
    fine: a database that already ran 001-010 has them.
    """
    scratch = sqlite3.connect(":memory:")
    try:
        scratch.executescript(baseline_sql)
        problems = []
        for name in user_table_names(scratch):
            if not table_exists(conn, name):
                problems.append(f"table {name} is missing")
                continue
            expected, actual = table_shape(scratch, name), table_shape(conn, name)
            problems.extend(f"{name}: {problem}" for problem in shape_requirement_problems(expected, actual))
        return problems
    finally:
        scratch.close()


def baseline_file_status(conn: sqlite3.Connection, migrations_dir: Path | None) -> tuple[str, str]:
    path = (migrations_dir or DEFAULT_MIGRATIONS_DIR) / BASELINE_FILENAME
    if not path.is_file():
        return "manual review required", f"{BASELINE_FILENAME} not found"
    problems = baseline_schema_problems(conn, path.read_text(encoding="utf-8"))
    if not problems:
        return "baselineable", "pre-001 core schema is already present"
    if database_is_empty(conn):
        return "not baselineable", "database is empty; 000 runs on normal --apply"
    shown = "; ".join(problems[:3])
    more = f"; +{len(problems) - 3} more" if len(problems) > 3 else ""
    return "not baselineable", f"baseline schema incomplete: {shown}{more}"


def baseline_status(
    conn: sqlite3.Connection, filename: str, migrations_dir: Path | None = None
) -> tuple[str, str]:
    if filename == BASELINE_FILENAME:
        return baseline_file_status(conn, migrations_dir)

    if filename == "001_quiz_tables.sql":
        if table_exists(conn, "quiz_attempts") and table_exists(conn, "quiz_answers"):
            return "baselineable", "quiz_attempts and quiz_answers exist"
        return "not baselineable", "quiz_attempts or quiz_answers missing"

    if filename == "002_curriculum_section_provenance.sql":
        expected_columns = [
            "source_url",
            "source_title",
            "source_authority",
            "dzi_relevance_verified",
            "dzi_relevance_notes",
        ]
        if columns_exist(conn, "curriculum_sections", expected_columns):
            return "baselineable", "curriculum_sections provenance columns exist"
        return "not baselineable", "curriculum_sections provenance columns missing"

    if filename == "003_dzi_tasks_assets_blueprint.sql":
        expected_tables = ["dzi_blueprints", "dzi_blueprint_slots", "assets", "asset_links"]
        missing = [table_name for table_name in expected_tables if not table_exists(conn, table_name)]
        if not missing:
            return "baselineable", "DZI blueprint and asset tables exist"
        return "not baselineable", f"missing objects: {', '.join(missing)}"

    if filename == "004_dzi_safety_constraints.sql":
        if index_exists(conn, "uniq_questions_source_exam_number"):
            return "baselineable", "uniq_questions_source_exam_number exists"
        return "manual review required", "safety constraint not safely detectable"

    if filename == "005_quiz_text_answers.sql":
        if table_exists(conn, "quiz_text_answers"):
            return "baselineable", "quiz_text_answers exists"
        return "not baselineable", "quiz_text_answers absent; leave pending"

    if filename == "006_assignment_question_plan.sql":
        if columns_exist(conn, "quiz_assignments", ["question_plan_json"]):
            return "baselineable", "quiz_assignments.question_plan_json exists"
        return "not baselineable", "quiz_assignments.question_plan_json absent; leave pending"

    return "manual review required", "no baseline rule defined"


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


def baseline_dry_run(db_path: Path, migrations_dir: Path, out: TextIO = sys.stdout) -> None:
    all_migrations = migration_filenames(migrations_dir)
    conn = open_read_only_db(db_path)
    try:
        applied = applied_migrations(conn)
        pending = pending_migrations(all_migrations, applied)
        print(f"database path: {db_path}", file=out)
        print(f"migration directory: {migrations_dir}", file=out)
        print("baseline candidates:", file=out)
        if pending:
            for filename in pending:
                status, reason = baseline_status(conn, filename, migrations_dir)
                print(f"  - {filename}: {status} ({reason})", file=out)
        else:
            print("  - none", file=out)
        print("remaining pending migrations:", file=out)
        remaining = [
            filename
            for filename in pending
            if baseline_status(conn, filename, migrations_dir)[0] != "baselineable"
        ]
        if remaining:
            for filename in remaining:
                print(f"  - {filename}", file=out)
        else:
            print("  - none", file=out)
        print("BASELINE DRY RUN ONLY: no changes applied", file=out)
    finally:
        conn.close()


def baseline_apply(db_path: Path, migrations_dir: Path, out: TextIO = sys.stdout) -> int:
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
        baselineable = []
        remaining = []
        for filename in pending:
            status, _reason = baseline_status(conn, filename, migrations_dir)
            if status == "baselineable":
                baselineable.append(filename)
            else:
                remaining.append(filename)

        print(f"database path: {db_path}", file=out)
        print(f"migration directory: {migrations_dir}", file=out)
        print("recorded baseline migrations:", file=out)
        if baselineable:
            for filename in baselineable:
                conn.execute(
                    "INSERT INTO schema_migrations (filename, applied_at) VALUES (?, CURRENT_TIMESTAMP)",
                    (filename,),
                )
                print(f"  - {filename}", file=out)
            conn.commit()
        else:
            print("  - none", file=out)

        print("remaining pending migrations:", file=out)
        if remaining:
            for filename in remaining:
                print(f"  - {filename}", file=out)
        else:
            print("  - none", file=out)

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


def require_empty_database_for_baseline(db_path: Path, all_migrations: list[str]) -> None:
    """Run 000 only on an empty database; adopt it on an existing one.

    CREATE ... IF NOT EXISTS would let 000 "succeed" on a partial schema and
    record it as applied, so a non-empty database must go through the checked
    --baseline-dry-run / --baseline-apply path instead. Reads only.
    """
    if BASELINE_FILENAME not in all_migrations:
        return
    conn = open_read_only_db(db_path)
    try:
        if BASELINE_FILENAME in applied_migrations(conn) or database_is_empty(conn):
            return
    finally:
        conn.close()
    raise BaselineRequired(
        f"{db_path} already has tables but has not recorded {BASELINE_FILENAME}. "
        "Back it up, then run --baseline-dry-run and --baseline-apply; "
        "--apply only runs the baseline on an empty database."
    )


def apply_migrations(db_path: Path, migrations_dir: Path, out: TextIO = sys.stdout) -> int:
    all_migrations = migration_filenames(migrations_dir)
    require_empty_database_for_baseline(db_path, all_migrations)
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
        if args.baseline_dry_run:
            baseline_dry_run(Path(args.db), Path(args.migrations_dir), out)
            return 0
        if args.baseline_apply:
            return baseline_apply(Path(args.db), Path(args.migrations_dir), out)
        if args.apply:
            return apply_migrations(Path(args.db), Path(args.migrations_dir), out)
        dry_run(Path(args.db), Path(args.migrations_dir), out)
    except (FileNotFoundError, BaselineRequired, sqlite3.Error) as exc:
        print(f"error: {exc}", file=err)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
