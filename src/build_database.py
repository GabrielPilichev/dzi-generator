#!/usr/bin/env python3
"""Build a fresh LearnPilot database from tracked sources only (G2).

    python3 -m src.build_database --output /path/to/new.db

    empty file -> migrations 000..current (``manage_migrations.apply_migrations``)
               -> explicit-id canonical load (``canonical_load.load_canonical``)
               -> validation -> atomic move to the requested destination

The builder never reads or copies ``data/questions.db``. The output holds the
canonical content with its stable ids and the migrated schema; runtime and
operational tables are empty (tests and tools create the runtime state they
need explicitly). It is an explicit provisioning/developer/test operation:
application start-up never calls it, and it never overwrites an existing file.

The database is built at a temporary sibling path and moved into place only
after validation succeeds, so a failed build leaves no destination that looks
valid. ``build_database`` is the reusable API; ``main`` is the CLI.
"""

from __future__ import annotations

import argparse
import io
import os
import shutil
import sqlite3
import sys
import tempfile
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

try:
    from src import canonical_export as export
    from src import canonical_load as load
    from src import canonical_parity as parity
    from src import canonical_tables as ct
except ModuleNotFoundError:  # pragma: no cover - script-style import
    import canonical_export as export
    import canonical_load as load
    import canonical_parity as parity
    import canonical_tables as ct

SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


class BuildError(RuntimeError):
    """The database could not be built or failed validation; nothing was written."""


@dataclass
class BuildResult:
    path: Path
    migration_level: str
    tables: dict[str, tuple[int, str]] = field(default_factory=dict)  # canonical table -> (rows, sha256)
    sequences: dict[str, int] = field(default_factory=dict)

    @property
    def canonical_rows(self) -> int:
        return sum(rows for rows, _ in self.tables.values())


# ----------------------------------------------------------------------------- validation

def validation_problems(db_path: Path, canonical_dir: Path = ct.CANONICAL_DIR) -> list[str]:
    """Everything a freshly built database must satisfy (empty list means ready).

    Reuses the canonical parity comparator (schema, counts, hashes, ids, FK graph,
    sequences, id references, runtime/operational tables empty) and adds the builder's
    own checks: migration level, no stray sequences, foreign_keys, integrity.
    """
    problems: list[str] = []
    manifest = export.load_manifest(canonical_dir)
    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            problems.append("foreign_keys could not be enabled")
        problems += parity.compare_to_canonical(conn, canonical_dir)
        files = export.migration_files()
        applied = [row[0] for row in conn.execute("SELECT filename FROM schema_migrations ORDER BY filename")]
        if applied != files:
            problems.append(f"applied migrations {applied} differ from the repository's {files}")
        if applied and applied[-1] != manifest["schema"]["migration_level"]:
            problems.append(f"migration level {applied[-1]} differs from the manifest's "
                            f"{manifest['schema']['migration_level']}")
        expected_sequences = {table: entry["sqlite_sequence"] for table, entry in manifest["tables"].items()
                              if entry["sqlite_sequence"] is not None}
        actual_sequences = dict(conn.execute("SELECT name, seq FROM sqlite_sequence").fetchall()) \
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'sqlite_sequence'").fetchone() else {}
        stray = sorted(set(actual_sequences) - set(expected_sequences))
        if stray:
            problems.append(f"sqlite_sequence has entries for non-canonical tables: {stray}")
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            problems.append("PRAGMA integrity_check failed")
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        if mode != "delete":
            problems.append(f"journal_mode is {mode}; a built database is a plain rollback-journal file")
    return problems


def logical_fingerprint(db_path: Path) -> dict:
    """Logical identity of a database: equal fingerprints mean equal builds.

    Covers schema, migration level (filenames only: ``applied_at`` is a timestamp),
    canonical per-table counts/hashes (which include every explicit id), sequences and
    the row counts of every non-canonical table. SQLite file bytes are never compared.
    """
    with closing(sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True)) as conn:
        tables = ct.application_tables(conn)
        non_canonical = [t for t in tables if t not in set(ct.tables_of(ct.CANONICAL)) | {"sqlite_sequence"}]
        sequences = (dict(conn.execute("SELECT name, seq FROM sqlite_sequence ORDER BY name").fetchall())
                     if "sqlite_sequence" in tables else {})
        return {
            "schema": [tuple(row) for row in conn.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
                "ORDER BY type, name")],
            "migrations": [row[0] for row in conn.execute("SELECT filename FROM schema_migrations ORDER BY filename")],
            "canonical": {table: ct.table_digest(conn, table) for table in ct.tables_of(ct.CANONICAL)},
            "sequences": sequences,
            "non_canonical_rows": {
                table: conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                for table in non_canonical if table != "schema_migrations"},
            "foreign_key_violations": len(conn.execute("PRAGMA foreign_key_check").fetchall()),
        }


# ----------------------------------------------------------------------------- build

def _publish(temp_db: Path, destination: Path) -> None:
    """Move the validated database into place without ever replacing an existing file."""
    try:
        os.link(temp_db, destination)  # fails if the destination appeared meanwhile
    except FileExistsError:
        raise BuildError(f"{destination} appeared while building; refusing to overwrite it") from None
    except OSError:  # no hard links on this file system; the existence check above already ran
        os.rename(temp_db, destination)
    else:
        temp_db.unlink()


def build_database(output: Path, canonical_dir: Path = ct.CANONICAL_DIR,
                   out: TextIO | None = None) -> BuildResult:
    """Build ``output`` from migrations + canonical content, or raise ``BuildError``.

    Refuses an existing destination (file, directory, symlink or stale sidecar).
    Nothing is created at ``output`` unless the build and its validation succeed.
    """
    destination = Path(output)
    canonical_dir = Path(canonical_dir)
    for candidate in (destination, *(Path(str(destination) + s) for s in SIDECAR_SUFFIXES)):
        if os.path.lexists(candidate):
            raise BuildError(f"{candidate} already exists; refusing to overwrite it")
    destination.parent.mkdir(parents=True, exist_ok=True)

    work = Path(tempfile.mkdtemp(prefix=f".{destination.name}.build-", dir=destination.parent))
    try:
        temp_db = work / destination.name
        try:
            load.migrated_empty_database(temp_db)
            load.load_canonical(temp_db, canonical_dir)
        except (ct.CanonicalError, sqlite3.Error, OSError, ValueError) as exc:
            raise BuildError(f"build failed: {exc}") from exc
        problems = validation_problems(temp_db, canonical_dir)
        if problems:
            raise BuildError("validation failed:\n  - " + "\n  - ".join(problems))
        leftovers = sorted(path.name for path in work.iterdir() if path.name != temp_db.name)
        if leftovers:
            raise BuildError(f"unexpected sidecar files after the build: {leftovers}")
        result = _result(temp_db, destination)
        _publish(temp_db, destination)
        return result
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _result(built: Path, destination: Path) -> BuildResult:
    with closing(sqlite3.connect(f"{built.resolve().as_uri()}?mode=ro", uri=True)) as conn:
        level = conn.execute("SELECT MAX(filename) FROM schema_migrations").fetchone()[0]
        tables = {table: ct.table_digest(conn, table) for table in ct.tables_of(ct.CANONICAL)}
        sequences = dict(conn.execute("SELECT name, seq FROM sqlite_sequence ORDER BY name").fetchall())
    return BuildResult(destination, level, tables, sequences)


# ----------------------------------------------------------------------------- CLI

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a fresh database from migrations + tracked canonical content "
                    "(never copies data/questions.db, never overwrites).")
    parser.add_argument("--output", required=True, help="Destination database path (must not exist).")
    parser.add_argument("--canonical-dir", default=str(ct.CANONICAL_DIR),
                        help="Canonical content directory (default: data/canonical).")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, out: TextIO = sys.stdout, err: TextIO = sys.stderr) -> int:
    args = parse_args(argv)
    try:
        result = build_database(Path(args.output), Path(args.canonical_dir))
    except (BuildError, ct.CanonicalError) as exc:
        print(f"error: {exc}", file=err)
        return 1
    print(f"built {result.path}", file=out)
    print(f"  migration level: {result.migration_level}", file=out)
    print(f"  canonical tables: {len(result.tables)}, rows: {result.canonical_rows}", file=out)
    for table in ("curricula", "curriculum_sections", "curriculum_topics", "questions",
                  "multiple_choice_options", "exams", "exam_tasks"):
        if table in result.tables:
            print(f"    {table}: {result.tables[table][0]}", file=out)
    print("  validation: schema, hashes, ids, sequences, foreign keys, runtime/operational empty: ok", file=out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
