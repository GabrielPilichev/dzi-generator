"""Explicit-ID canonical loader (G1); ``src/build_database.py`` (G2) is built on it.

It loads the rows of ``data/canonical`` into a database that already has the migrated
schema (the builder adds the temporary-path/validation/atomic-move provisioning around it):

- explicit ids are inserted as given and never remapped;
- foreign keys are ON and tables load in FK-topological order;
- everything happens in one transaction, which is rolled back on any failure;
- a row whose primary key already exists must be identical, otherwise the
  load fails; a unique-constraint clash fails it too;
- only canonical tables (and their ``sqlite_sequence`` entries) are written -
  runtime and operational tables are never touched.
"""

from __future__ import annotations

import io
import sqlite3
from contextlib import closing
from pathlib import Path

try:
    from src import canonical_export as export
    from src import canonical_tables as ct
    from src import manage_migrations
except ModuleNotFoundError:  # pragma: no cover - script-style import
    import canonical_export as export
    import canonical_tables as ct
    import manage_migrations


def insert_rows(conn: sqlite3.Connection, table: str, columns: list[str], rows: list[tuple]) -> int:
    """Insert explicit-id rows; identical existing rows are skipped, differing ones refused."""
    keys = ct.primary_key_columns(conn, table)
    key_positions = [columns.index(key) for key in keys]
    where = " AND ".join(f'"{key}" = ?' for key in keys)
    select = ", ".join(f'"{column}"' for column in columns)
    insert = (f'INSERT INTO "{table}" ({select}) VALUES ({", ".join("?" for _ in columns)})')
    inserted = 0
    for values in rows:
        existing = conn.execute(f'SELECT {select} FROM "{table}" WHERE {where}',
                                [values[i] for i in key_positions]).fetchone()
        if existing is not None:
            if tuple(existing) != tuple(values):
                raise ct.CanonicalError(
                    f"{table}: key {[values[i] for i in key_positions]} already exists with different content")
            continue
        conn.execute(insert, values)
        inserted += 1
    return inserted


def apply_sequence(conn: sqlite3.Connection, table: str, expected: int | None) -> None:
    """Raise the AUTOINCREMENT high-water mark to ``expected``; never lower it."""
    if expected is None:
        return
    current = ct.stored_sequence(conn, table)
    if current is not None and current >= expected:
        return
    if current is None:
        conn.execute("INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)", (table, expected))
    else:
        conn.execute("UPDATE sqlite_sequence SET seq = ? WHERE name = ?", (expected, table))


def load_canonical(db_path: Path, canonical_dir: Path = ct.CANONICAL_DIR) -> dict[str, int]:
    """Load every canonical table into the migrated database at ``db_path``.

    Returns rows inserted per table. The directory is verified against its
    manifest first; a database that does not match the schema is refused.
    """
    canonical_dir = Path(canonical_dir)
    problems = export.check_directory(canonical_dir)
    if problems:
        raise ct.CanonicalError("canonical data is inconsistent:\n  - " + "\n  - ".join(problems))
    manifest = export.load_manifest(canonical_dir)
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        schema = export.schema_problems(conn)
        if schema:
            raise ct.CanonicalError("target database is not at the migrated schema: " + "; ".join(schema))
        if manifest["canonical_tables"] != ct.canonical_load_order(conn):
            raise ct.CanonicalError("manifest load order differs from the schema's foreign-key order")
        inserted = {}
        conn.execute("BEGIN IMMEDIATE")
        try:
            for table in manifest["canonical_tables"]:
                entry = manifest["tables"][table]
                columns = entry["columns"]
                rows = [tuple(row[column] for column in columns)
                        for row in ct.read_table_rows(canonical_dir / entry["file"])]
                inserted[table] = insert_rows(conn, table, columns, rows)
                apply_sequence(conn, table, entry["sqlite_sequence"])
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise ct.CanonicalError(f"foreign key violations after load: {violations[:3]}")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        return inserted
    finally:
        conn.close()


def migrated_empty_database(db_path: Path) -> None:
    """Create ``db_path`` as an empty database with 000 + the numbered migrations."""
    path = Path(db_path)
    if path.exists():
        raise ct.CanonicalError(f"{path} already exists")
    sqlite3.connect(path).close()
    status = manage_migrations.apply_migrations(path, ct.MIGRATIONS_DIR, out=io.StringIO())
    if status != 0:
        raise ct.CanonicalError("migrations failed on the empty database")


def rebuild_for_parity(db_path: Path, canonical_dir: Path = ct.CANONICAL_DIR) -> dict[str, int]:
    """Empty database -> migrations -> canonical load, without the builder's validation or atomic move."""
    migrated_empty_database(db_path)
    return load_canonical(db_path, canonical_dir)


def canonical_digests(db_path: Path) -> dict[str, tuple[int, str]]:
    """(rows, sha256) per canonical table of an existing database."""
    with closing(sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True)) as conn:
        return {table: ct.table_digest(conn, table) for table in ct.tables_of(ct.CANONICAL)}
