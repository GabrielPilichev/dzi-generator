#!/usr/bin/env python3
"""Migration planner/runner for numbered SQLite migrations.

An ordinary migration runs with foreign keys enforced. A migration that must
rebuild a table other tables reference (create the new table, copy, drop the
old one, rename) opts in with this exact comment line before its first SQL
statement:

    -- learnpilot-migration: table-rebuild

Such a file runs with foreign keys off, which SQLite only allows to be chosen
outside a transaction, and is committed together with its schema_migrations
row only after PRAGMA foreign_key_check is clean. See docs/DEPLOYMENT.md
section 9.
"""

from __future__ import annotations

import argparse
import os
import re
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
SQLITE_SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")

TABLE_REBUILD_DIRECTIVE = "-- learnpilot-migration: table-rebuild"
# Every comment line that starts with "learnpilot" is reserved for directives,
# so a misspelt one is an error instead of a silently ordinary migration.
DIRECTIVE_LIKE_LINE = re.compile(r"--\s*learnpilot\b", re.IGNORECASE)
# SQLite authorizer actions refused while a table-rebuild file runs, so that its
# SQL cannot leave the runner's transaction or reach another database file.
# SQLite reports END as COMMIT and RELEASE / ROLLBACK TO as savepoint actions.
REBUILD_DENIED_ACTIONS = {
    sqlite3.SQLITE_TRANSACTION: "transaction control",
    sqlite3.SQLITE_SAVEPOINT: "a savepoint",
    sqlite3.SQLITE_ATTACH: "ATTACH",
    sqlite3.SQLITE_DETACH: "DETACH",
}


class BaselineRequired(RuntimeError):
    """A non-empty database must adopt 000 with --baseline-apply, not run it."""


class MigrationError(RuntimeError):
    """A migration file, its validation or the pre-migration backup was refused."""


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


def _remove_with_sidecars(path: Path) -> None:
    for candidate in (path, *(Path(f"{path}{suffix}") for suffix in SQLITE_SIDECAR_SUFFIXES)):
        try:
            candidate.unlink()
        except (FileNotFoundError, NotADirectoryError):
            pass


def _unused_backup_path(backup_dir: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = backup_dir / f"questions.db.backup-{timestamp}"
    counter = 1
    while backup_path.exists():
        backup_path = backup_dir / f"questions.db.backup-{timestamp}-{counter}"
        counter += 1
    return backup_path


def create_backup_for_default_db(db_path: Path) -> Path:
    """Snapshot the repository database before the runner writes to it.

    Uses the SQLite backup API, never a file copy: a copy of a WAL-mode
    database misses every committed page still in the -wal file. The result is
    one self-contained rollback-journal file that passed quick_check. Any
    failure raises, so no migration runs without its backup. Production uses
    the full snapshot of docs/DEPLOYMENT.md section 16, not this.
    """
    backup_dir = PROJECT_ROOT / BACKUP_DIR_NAME
    backup_path = _unused_backup_path(backup_dir)
    partial = backup_dir / f".{backup_path.name}.partial-{os.getpid()}"
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        _remove_with_sidecars(partial)
        # mode=rw never creates a missing database but lets SQLite read the WAL;
        # query_only forbids writes through this connection.
        source = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=rw", uri=True, timeout=30)
        try:
            source.execute("PRAGMA query_only = ON")
            target = sqlite3.connect(str(partial))
            try:
                source.backup(target)  # one step, one consistent snapshot
                target.execute("PRAGMA journal_mode = DELETE").fetchone()
                result = [row[0] for row in target.execute("PRAGMA quick_check")]
                if result != ["ok"]:
                    raise MigrationError(f"backup failed quick_check: {result[:5]}")
            finally:
                target.close()
        finally:
            source.close()
        os.chmod(partial, 0o600)
        os.replace(partial, backup_path)
    except (OSError, sqlite3.Error) as exc:
        raise MigrationError(f"backup of {db_path} failed, nothing was migrated: {exc}") from exc
    finally:
        _remove_with_sidecars(partial)
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


def foreign_keys_enabled(conn: sqlite3.Connection) -> bool:
    return bool(conn.execute("PRAGMA foreign_keys").fetchone()[0])


def set_foreign_keys(conn: sqlite3.Connection, enabled: bool) -> None:
    """Switch enforcement and confirm it took effect.

    SQLite silently ignores this pragma inside a transaction, so it is refused
    there and the resulting state is read back instead of assumed.
    """
    state = "ON" if enabled else "OFF"
    if conn.in_transaction:
        raise MigrationError(f"cannot set foreign_keys = {state} inside a transaction")
    conn.execute(f"PRAGMA foreign_keys = {state}")
    if foreign_keys_enabled(conn) != enabled:
        raise MigrationError(f"SQLite did not apply foreign_keys = {state}")


def is_table_rebuild_migration(filename: str, sql: str) -> bool:
    """True when the file opts in to table-rebuild execution.

    The only directive is TABLE_REBUILD_DIRECTIVE, alone on a line, once,
    before the first SQL statement. Anything else that looks like a directive
    is refused.
    """
    found = False
    in_header = True
    for number, raw_line in enumerate(sql.splitlines(), start=1):
        line = raw_line.strip()
        if DIRECTIVE_LIKE_LINE.match(line):
            where = f"{filename}:{number}"
            if line != TABLE_REBUILD_DIRECTIVE:
                raise MigrationError(
                    f"{where}: unsupported migration directive {line!r}; "
                    f"the only directive is {TABLE_REBUILD_DIRECTIVE!r}"
                )
            if not in_header:
                raise MigrationError(f"{where}: the directive must come before the first SQL statement")
            if found:
                raise MigrationError(f"{where}: duplicate migration directive")
            found = True
        elif line and not line.startswith("--"):
            in_header = False
    return found


def read_migration_sql(path: Path) -> str:
    # utf-8-sig drops a leading byte order mark, which would otherwise hide the
    # directive on the first line and turn a rebuild into an ordinary migration.
    return path.read_text(encoding="utf-8-sig")


def table_rebuild_migrations(migrations_dir: Path, filenames: list[str]) -> set[str]:
    return {
        filename
        for filename in filenames
        if is_table_rebuild_migration(filename, read_migration_sql(migrations_dir / filename))
    }


def migration_label(filename: str, rebuilds: set[str]) -> str:
    return f"{filename} (table-rebuild)" if filename in rebuilds else filename


def _allow_every_action(*_args: object) -> int:
    return sqlite3.SQLITE_OK


def remove_authorizer(conn: sqlite3.Connection) -> None:
    # set_authorizer(None) removes the callback only from Python 3.11 on;
    # before that it denies every statement, so an allow-all callback stands in.
    conn.set_authorizer(None if sys.version_info >= (3, 11) else _allow_every_action)


def execute_in_runner_transaction(conn: sqlite3.Connection, filename: str, sql: str) -> None:
    """BEGIN IMMEDIATE, then the file's SQL, leaving the transaction open.

    BEGIN is part of the script because executescript() first commits any
    transaction opened through the connection. While the script runs, an
    authorizer lets SQLite's own parser refuse every statement that could end
    or replace that transaction: the first BEGIN is the runner's and the only
    transaction statement allowed. The authorizer is removed before returning,
    whatever happened, so the runner's COMMIT or ROLLBACK is not affected.
    """
    runner_began = False
    denied: list[str] = []

    def authorize(action: int, arg1: str | None, *_rest: object) -> int:
        nonlocal runner_began
        if action == sqlite3.SQLITE_TRANSACTION and arg1 == "BEGIN" and not runner_began:
            runner_began = True
            return sqlite3.SQLITE_OK
        if action in REBUILD_DENIED_ACTIONS:
            denied.append(REBUILD_DENIED_ACTIONS[action])
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    conn.set_authorizer(authorize)
    try:
        conn.executescript(f"BEGIN IMMEDIATE;\n{sql}\n")
    except sqlite3.DatabaseError as exc:
        if denied:
            raise MigrationError(
                f"{filename} rolled back: {denied[0]} statement is not allowed in a "
                "table-rebuild migration; the runner owns the transaction"
            ) from exc
        raise
    finally:
        remove_authorizer(conn)


def apply_table_rebuild_migration(conn: sqlite3.Connection, filename: str, sql: str) -> None:
    """Run one opted-in rebuild migration as a single checked transaction.

    Foreign keys are switched off before BEGIN, because with them on DROP TABLE
    deletes the rows first and cascades into every referencing table. The
    file's SQL, PRAGMA foreign_key_check and the schema_migrations row then
    share one transaction: any error or any violation rolls all of it back.
    Enforcement is switched back on, and confirmed, on every way out.
    """
    try:
        set_foreign_keys(conn, False)
        execute_in_runner_transaction(conn, filename, sql)
        if not conn.in_transaction or foreign_keys_enabled(conn):
            raise MigrationError(
                f"{filename} ended the runner's transaction or re-enabled foreign keys; "
                "it was not recorded and the database may be changed: restore the backup"
            )
        fk_rows = foreign_key_check_rows(conn)
        if fk_rows:
            shown = ", ".join(str(tuple(row)) for row in fk_rows[:5])
            raise MigrationError(
                f"{filename} rolled back: foreign_key_check found {len(fk_rows)} "
                f"violation(s) (table, rowid, parent, fk): {shown}"
            )
        conn.execute("INSERT INTO schema_migrations (filename) VALUES (?)", (filename,))
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        set_foreign_keys(conn, True)


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


def print_plan(
    db_path: Path, migrations_dir: Path, applied: list[str], pending: list[str], rebuilds: set[str], out: TextIO
) -> None:
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
            print(f"  - {migration_label(filename, rebuilds)}", file=out)
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
    print_plan(db_path, migrations_dir, applied, pending, table_rebuild_migrations(migrations_dir, pending), out)


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
    # Read every directive first: a malformed file stops the run before the
    # backup and before any earlier migration is applied.
    rebuilds = table_rebuild_migrations(migrations_dir, all_migrations)
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
            sql = read_migration_sql(migrations_dir / filename)
            if filename in rebuilds:
                apply_table_rebuild_migration(conn, filename, sql)
                print(f"  - {migration_label(filename, rebuilds)}", file=out)
                continue
            if not foreign_keys_enabled(conn):
                raise MigrationError(f"foreign keys are not enforced; refusing to run {filename}")
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
    except (FileNotFoundError, BaselineRequired, MigrationError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=err)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
