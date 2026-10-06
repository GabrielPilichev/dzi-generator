#!/usr/bin/env python3
"""Deterministic export, manifest and update-safety checks for canonical content (G1).

    # export a database's canonical tables into a fresh directory (default workflow)
    python3 -m src.canonical_export --db PATH --out DIR

    # replace the tracked data/canonical after the stable-identity safety checks
    python3 -m src.canonical_export --db PATH --replace-canonical

    # verify the tracked files against their manifest and the current schema
    python3 -m src.canonical_export --check

Only tables classified CANONICAL are exported; runtime and operational tables
never are. The tracked directory is never written unless ``--replace-canonical``
is given, and stable identities (ids, natural-key-to-id mappings, option
letters, sequence high-water marks) may not be removed or changed by it.
Application start-up never imports this module.
"""

from __future__ import annotations

import argparse
import io
import json
import shutil
import sqlite3
import sys
import tempfile
from contextlib import closing
from functools import lru_cache
from pathlib import Path

try:
    from src import canonical_tables as ct
    from src import manage_migrations
except ModuleNotFoundError:  # run as `python3 src/canonical_export.py`
    import canonical_tables as ct
    import manage_migrations


def open_snapshot(db_path: Path) -> sqlite3.Connection:
    """Read-only, immutable view: SQLite creates no -wal/-shm and never touches the file."""
    uri = Path(db_path).resolve().as_uri() + "?mode=ro&immutable=1"
    return sqlite3.connect(uri, uri=True)


def migration_files(migrations_dir: Path = ct.MIGRATIONS_DIR) -> list[str]:
    return manage_migrations.migration_filenames(Path(migrations_dir))


@lru_cache(maxsize=4)
def reference_columns(migrations_dir: str = str(ct.MIGRATIONS_DIR)) -> dict[str, list[str]]:
    """Column lists of every table after 000 + numbered migrations on an empty database."""
    with tempfile.TemporaryDirectory(prefix="learnpilot-reference-") as directory:
        path = Path(directory) / "reference.db"
        sqlite3.connect(path).close()
        if manage_migrations.apply_migrations(path, Path(migrations_dir), out=io.StringIO()) != 0:
            raise ct.CanonicalError("could not build the reference schema from the migrations")
        with closing(sqlite3.connect(path)) as conn:
            return {table: ct.table_columns(conn, table) for table in ct.application_tables(conn)}


def schema_problems(conn: sqlite3.Connection, migrations_dir: Path = ct.MIGRATIONS_DIR) -> list[str]:
    """Why ``conn`` cannot be exported at the repository's migration level."""
    problems = list(ct.classification_problems(conn))
    reference = reference_columns(str(migrations_dir))
    for table in ct.tables_of(ct.CANONICAL):
        actual = ct.table_columns(conn, table) if table in ct.application_tables(conn) else None
        if actual != reference.get(table):
            problems.append(f"table {table} columns differ from the migrated schema")
    files = migration_files(migrations_dir)
    if "schema_migrations" in ct.application_tables(conn):
        applied = [row[0] for row in conn.execute("SELECT filename FROM schema_migrations ORDER BY filename")]
        unknown = sorted(set(applied) - set(files))
        if unknown:
            problems.append(f"database recorded migrations the repository lacks: {unknown}")
        elif not applied or applied[-1] != files[-1]:
            problems.append(f"database is not at migration level {files[-1]}")
    return problems


def table_entry(conn: sqlite3.Connection, table: str, count: int, digest: str) -> dict:
    keys = ct.primary_key_columns(conn, table)
    return {
        "file": f"{table}.jsonl",
        "rows": count,
        "columns": ct.table_columns(conn, table),
        "primary_key": keys,
        "autoincrement": ct.is_autoincrement(conn, table),
        "max_id": ct.max_id(conn, table),
        "sqlite_sequence": ct.stored_sequence(conn, table) if ct.is_autoincrement(conn, table) else None,
        "natural_key": list(ct.NATURAL_KEYS[table]) if ct.NATURAL_KEYS.get(table) else None,
        "sha256": digest,
    }


def build_manifest(conn: sqlite3.Connection, entries: dict[str, dict], sources: dict | None,
                   migrations_dir: Path = ct.MIGRATIONS_DIR) -> dict:
    files = migration_files(migrations_dir)
    return {
        "format": ct.FORMAT_NAME,
        "format_version": ct.FORMAT_VERSION,
        "schema": {"migration_level": files[-1], "migrations": files},
        "classification": {kind: ct.tables_of(kind) for kind in (ct.CANONICAL, ct.RUNTIME, ct.OPERATIONAL)},
        "canonical_tables": ct.canonical_load_order(conn),
        "runtime_exclusions": ct.tables_of(ct.RUNTIME),
        "operational_exclusions": ct.tables_of(ct.OPERATIONAL),
        "tables": {table: entries[table] for table in ct.canonical_load_order(conn)},
        "sources": sources or {},
    }


def manifest_text(manifest: dict) -> str:
    return json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"


def export_database(conn: sqlite3.Connection, out_dir: Path, *, sources: dict | None = None,
                    migrations_dir: Path = ct.MIGRATIONS_DIR) -> dict:
    """Write every canonical table plus manifest.json into ``out_dir``; return the manifest."""
    problems = schema_problems(conn, migrations_dir)
    if problems:
        raise ct.CanonicalError("cannot export: " + "; ".join(problems))
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    entries = {}
    for table in ct.canonical_load_order(conn):
        count = 0
        digest_lines = []
        with open(out_dir / f"{table}.jsonl", "w", encoding="utf-8", newline="\n") as handle:
            for line in ct.iter_table_lines(conn, table):
                handle.write(line + "\n")
                digest_lines.append(line)
                count += 1
        count, digest = ct.hash_lines(digest_lines)
        entries[table] = table_entry(conn, table, count, digest)
    manifest = build_manifest(conn, entries, sources, migrations_dir)
    (out_dir / ct.MANIFEST_NAME).write_text(manifest_text(manifest), encoding="utf-8", newline="\n")
    return manifest


# ----------------------------------------------------------------------------- verification

def load_manifest(directory: Path) -> dict:
    path = Path(directory) / ct.MANIFEST_NAME
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ct.CanonicalError(f"cannot read {path}: {exc}") from exc


def check_directory(directory: Path, migrations_dir: Path = ct.MIGRATIONS_DIR) -> list[str]:
    """Problems between a canonical directory, its manifest, the classification and the schema."""
    directory = Path(directory)
    problems: list[str] = []
    try:
        manifest = load_manifest(directory)
    except ct.CanonicalError as exc:
        return [str(exc)]
    if manifest.get("format") != ct.FORMAT_NAME or manifest.get("format_version") != ct.FORMAT_VERSION:
        problems.append("unsupported manifest format/version")
    canonical = ct.tables_of(ct.CANONICAL)
    if sorted(manifest.get("canonical_tables", [])) != canonical or sorted(manifest.get("tables", {})) != canonical:
        problems.append("manifest canonical table list differs from the classification")
    expected_classification = {kind: ct.tables_of(kind) for kind in (ct.CANONICAL, ct.RUNTIME, ct.OPERATIONAL)}
    if manifest.get("classification") != expected_classification:
        problems.append("manifest classification differs from the current table classification")
    if (manifest.get("runtime_exclusions") != ct.tables_of(ct.RUNTIME)
            or manifest.get("operational_exclusions") != ct.tables_of(ct.OPERATIONAL)):
        problems.append("manifest exclusion lists differ from the classification")
    files = migration_files(migrations_dir)
    schema = manifest.get("schema", {})
    if schema.get("migrations") != files or schema.get("migration_level") != files[-1]:
        problems.append("manifest migration level differs from the repository migrations")
    unexpected = sorted(path.name for path in directory.iterdir()
                        if path.name != ct.MANIFEST_NAME and path.name not in {f"{t}.jsonl" for t in canonical})
    problems += [f"unexpected file in canonical directory: {name}" for name in unexpected]
    reference = reference_columns(str(migrations_dir))
    for table, entry in manifest.get("tables", {}).items():
        path = directory / entry.get("file", f"{table}.jsonl")
        if not path.is_file():
            problems.append(f"{table}: canonical file is missing")
            continue
        if entry.get("columns") != reference.get(table):
            problems.append(f"{table}: manifest columns differ from the migrated schema")
        try:
            count, digest = ct.file_digest(path)
            rows = list(ct.read_table_rows(path))
        except (ct.CanonicalError, ValueError) as exc:
            problems.append(f"{table}: {exc}")
            continue
        if count != entry.get("rows"):
            problems.append(f"{table}: {count} rows, manifest says {entry.get('rows')}")
        if digest != entry.get("sha256"):
            problems.append(f"{table}: sha256 differs from the manifest")
        if any(list(row) != entry.get("columns") for row in rows):
            problems.append(f"{table}: a row does not list exactly the schema columns in order")
            continue
        keys = entry.get("primary_key", [])
        if len({tuple(row[k] for k in keys) for row in rows}) != len(rows):
            problems.append(f"{table}: duplicate primary key")
        ids = [row[keys[0]] for row in rows] if len(keys) == 1 and rows and isinstance(rows[0][keys[0]], int) else []
        if (max(ids) if ids else None) != entry.get("max_id"):
            problems.append(f"{table}: manifest max_id differs from the data")
        sequence = entry.get("sqlite_sequence")
        if entry.get("autoincrement") and ids and (sequence is None or sequence < max(ids)):
            problems.append(f"{table}: sqlite_sequence {sequence} is below max id {max(ids)}")
        if not entry.get("autoincrement") and sequence is not None:
            problems.append(f"{table}: sequence recorded for a table without AUTOINCREMENT")
        if ct.NATURAL_KEYS.get(table):
            natural = [tuple(row[k] for k in ct.NATURAL_KEYS[table]) for row in rows]
            natural = [key for key in natural if None not in key]
            if len(set(natural)) != len(natural):
                problems.append(f"{table}: duplicate natural key {list(ct.NATURAL_KEYS[table])}")
    return problems


# ----------------------------------------------------------------------------- update safety

def _rows_by_key(directory: Path, table: str, keys: list[str]) -> dict[tuple, dict]:
    path = Path(directory) / f"{table}.jsonl"
    return {tuple(row[k] for k in keys): row for row in ct.read_table_rows(path)} if path.is_file() else {}


def identity_problems(old_dir: Path, new_dir: Path) -> list[str]:
    """Stable-identity violations of replacing ``old_dir`` by ``new_dir`` (empty means safe)."""
    old_dir, new_dir = Path(old_dir), Path(new_dir)
    if not (old_dir / ct.MANIFEST_NAME).is_file():
        return []
    old, new = load_manifest(old_dir), load_manifest(new_dir)
    problems = []
    runtime = set(ct.tables_of(ct.RUNTIME)) | set(ct.tables_of(ct.OPERATIONAL))
    leaked = sorted(runtime & (set(new.get("tables", {})) | {p.stem for p in new_dir.glob("*.jsonl")}))
    problems += [f"runtime/operational table {name} is included in the export" for name in leaked]
    for table, old_entry in old["tables"].items():
        new_entry = new["tables"].get(table)
        if new_entry is None:
            problems.append(f"{table}: table removed from the export")
            continue
        keys = old_entry["primary_key"]
        before, after = _rows_by_key(old_dir, table, keys), _rows_by_key(new_dir, table, keys)
        if len(after) < len(before):
            problems.append(f"{table}: row count dropped from {len(before)} to {len(after)}")
        removed = sorted(set(before) - set(after))
        if removed:
            problems.append(f"{table}: {len(removed)} rows removed, first key {removed[0]}")
        natural = ct.NATURAL_KEYS.get(table)
        if natural and keys == ["id"]:
            def mapping(rows):
                return {tuple(r[k] for k in natural): r["id"] for r in rows.values()
                        if None not in tuple(r[k] for k in natural)}
            old_map, new_map = mapping(before), mapping(after)
            changed = sorted(key for key in old_map if key in new_map and old_map[key] != new_map[key])
            if changed:
                problems.append(f"{table}: {len(changed)} natural keys map to a different id, first {changed[0]}")
            reused = sorted(i for i, row in before.items() if i in after and tuple(
                row[k] for k in natural) != tuple(after[i][k] for k in natural)
                and None not in tuple(row[k] for k in natural))
            if reused:
                problems.append(f"{table}: {len(reused)} ids were reassigned to a different natural key, first {reused[0]}")
        old_seq, new_seq = old_entry.get("sqlite_sequence"), new_entry.get("sqlite_sequence")
        if old_seq is not None and (new_seq is None or new_seq < old_seq):
            problems.append(f"{table}: sqlite_sequence fell from {old_seq} to {new_seq}; ids would be recycled")
    return problems


def replace_canonical(conn: sqlite3.Connection, target: Path = ct.CANONICAL_DIR, *,
                      sources: dict | None = None) -> dict:
    """Export to a scratch directory, run the identity checks, then replace ``target``."""
    target = Path(target)
    with tempfile.TemporaryDirectory(prefix="learnpilot-canonical-") as scratch:
        staged = Path(scratch) / "canonical"
        previous = load_manifest(target) if (target / ct.MANIFEST_NAME).is_file() else None
        manifest = export_database(conn, staged, sources=sources if sources is not None
                                   else (previous or {}).get("sources"))
        problems = identity_problems(target, staged) + check_directory(staged)
        if problems:
            raise ct.CanonicalError("refusing to replace canonical data:\n  - " + "\n  - ".join(problems))
        target.mkdir(parents=True, exist_ok=True)
        for old in target.iterdir():
            if old.suffix == ".jsonl" or old.name == ct.MANIFEST_NAME:
                old.unlink()
        for new in staged.iterdir():
            shutil.copyfile(new, target / new.name)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", type=Path, help="database to export (opened read-only and immutable)")
    parser.add_argument("--out", type=Path, help="fresh directory to write; never the tracked data/canonical")
    parser.add_argument("--replace-canonical", action="store_true",
                        help="replace tracked data/canonical after the stable-identity checks")
    parser.add_argument("--check", action="store_true", help="verify the tracked data/canonical and exit")
    args = parser.parse_args(argv)
    if args.check:
        problems = check_directory(ct.CANONICAL_DIR)
        for problem in problems:
            print(f"FAIL {problem}", file=sys.stderr)
        print("canonical data OK" if not problems else f"{len(problems)} problems")
        return 1 if problems else 0
    if args.db is None or (args.out is None) == (not args.replace_canonical):
        parser.error("give --db and exactly one of --out DIR or --replace-canonical")
    try:
        with closing(open_snapshot(args.db)) as conn:
            if args.replace_canonical:
                manifest = replace_canonical(conn)
            else:
                out = args.out.resolve()
                if out == ct.CANONICAL_DIR.resolve():
                    parser.error("--out must not be the tracked data/canonical; use --replace-canonical")
                if out.exists() and any(out.iterdir()):
                    parser.error(f"--out {out} is not empty")
                manifest = export_database(conn, out)
    except (ct.CanonicalError, sqlite3.Error) as exc:
        print(f"Export refused: {exc}", file=sys.stderr)
        return 1
    print(f"Exported {len(manifest['tables'])} canonical tables")
    return 0


if __name__ == "__main__":
    sys.exit(main())
