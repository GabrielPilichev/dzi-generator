"""Parity comparator and identity-reference validators for canonical content (G1).

``compare_to_canonical`` answers: does this database hold exactly the canonical
content of a ``data/canonical`` directory? It compares logical content, never
SQLite file bytes:

  A schema (columns, foreign keys, indexes, constraints) against a database
    freshly built from 000 + the numbered migrations;
  B row counts, C normalized per-table hashes, D explicit ids (reported per row
    when a hash differs), E the foreign-key graph, F sqlite_sequence,
  G non-FK / JSON id references, H runtime and operational tables empty.

Hashing and serialization are ``canonical_tables``'; nothing is reimplemented.
"""

from __future__ import annotations

import json
import re
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

try:
    from src import canonical_export as export
    from src import canonical_tables as ct
    from src.schema_introspection import table_shape, user_table_names
except ModuleNotFoundError:  # pragma: no cover - script-style import
    import canonical_export as export
    import canonical_tables as ct
    from schema_introspection import table_shape, user_table_names


def _reference_shapes() -> dict[str, dict]:
    import io
    from src import manage_migrations
    with tempfile.TemporaryDirectory(prefix="learnpilot-shapes-") as directory:
        path = Path(directory) / "reference.db"
        sqlite3.connect(path).close()
        manage_migrations.apply_migrations(path, ct.MIGRATIONS_DIR, out=io.StringIO())
        with closing(sqlite3.connect(path)) as conn:
            return {name: table_shape(conn, name) for name in user_table_names(conn)}


def schema_parity_problems(conn: sqlite3.Connection) -> list[str]:
    expected, actual = _reference_shapes(), {name: table_shape(conn, name) for name in user_table_names(conn)}
    problems = [f"table {name} is missing" for name in sorted(set(expected) - set(actual))]
    problems += [f"table {name} is not in the migrated schema" for name in sorted(set(actual) - set(expected))]
    problems += [f"table {name}: shape differs from the migrated schema"
                 for name in sorted(set(expected) & set(actual)) if expected[name] != actual[name]]
    return problems


def _diff_rows(conn: sqlite3.Connection, table: str, directory: Path, entry: dict) -> str:
    keys = entry["primary_key"]
    columns = entry["columns"]
    canonical = {tuple(row[k] for k in keys): row for row in ct.read_table_rows(directory / entry["file"])}
    select = ", ".join(f'"{c}"' for c in columns)
    actual = {}
    for values in conn.execute(f'SELECT {select} FROM "{table}"'):
        row = dict(zip(columns, values))
        actual[tuple(row[k] for k in keys)] = row
    missing = sorted(set(canonical) - set(actual))
    extra = sorted(set(actual) - set(canonical))
    changed = sorted(key for key in set(canonical) & set(actual) if canonical[key] != actual[key])
    return (f"{len(missing)} ids missing{missing[:3]}, {len(extra)} unexpected{extra[:3]}, "
            f"{len(changed)} changed{changed[:3]}")


def compare_to_canonical(conn: sqlite3.Connection, canonical_dir: Path = ct.CANONICAL_DIR) -> list[str]:
    """All parity problems between ``conn`` and ``canonical_dir`` (empty means equal)."""
    canonical_dir = Path(canonical_dir)
    problems = [f"canonical data: {p}" for p in export.check_directory(canonical_dir)]
    if problems:
        return problems
    manifest = export.load_manifest(canonical_dir)
    problems += [f"schema: {p}" for p in schema_parity_problems(conn)]
    problems += [f"classification: {p}" for p in ct.classification_problems(conn)]
    for table in manifest["canonical_tables"]:
        entry = manifest["tables"][table]
        count, digest = ct.table_digest(conn, table)
        if count != entry["rows"]:
            problems.append(f"{table}: {count} rows, canonical has {entry['rows']}")
        if digest != entry["sha256"]:
            problems.append(f"{table}: normalized hash differs ({_diff_rows(conn, table, canonical_dir, entry)})")
        if entry["autoincrement"]:
            sequence = ct.stored_sequence(conn, table)
            if sequence != entry["sqlite_sequence"]:
                problems.append(f"{table}: sqlite_sequence {sequence}, expected {entry['sqlite_sequence']}")
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        problems.append(f"foreign key check reports {len(violations)} violations, first {tuple(violations[0])}")
    problems += [f"references: {p}" for p in embedded_reference_problems(conn)]
    for table in ct.tables_of(ct.RUNTIME) + [t for t in ct.tables_of(ct.OPERATIONAL)
                                             if t not in ("schema_migrations", "sqlite_sequence")]:
        count = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        seeded = ct.MIGRATION_SEEDED_ROWS.get(table, 0)
        if seeded and count != seeded:
            problems.append(f"{table}: {count} rows; a canonical rebuild must hold exactly the "
                            f"{seeded} row(s) its migration seeds")
        elif not seeded and count:
            problems.append(f"{table}: {count} rows; a canonical rebuild must leave non-canonical tables empty")
    return problems


# ----------------------------------------------------------------------------- id references

# Canonical ids that are stored without a foreign key.
OWNER_TABLES = {"exam": "exams", "exam_task": "exam_tasks", "question": "questions"}


def embedded_reference_problems(conn: sqlite3.Connection) -> list[str]:
    """Non-FK id references inside canonical rows, and any runtime JSON/id columns present."""
    problems = []
    for count, label in (
        (conn.execute("SELECT COUNT(*) FROM questions WHERE topic_id IS NOT NULL AND topic_id "
                      "NOT IN (SELECT id FROM curriculum_topics)").fetchone()[0], "questions.topic_id"),
    ):
        if count:
            problems.append(f"{label}: {count} ids do not resolve")
    for owner_type, owner_id in conn.execute("SELECT owner_type, owner_id FROM asset_links"):
        table = OWNER_TABLES.get(owner_type)
        if table is None:
            problems.append(f"asset_links: unknown owner_type {owner_type!r}")
        elif conn.execute(f'SELECT 1 FROM "{table}" WHERE id = ?', (owner_id,)).fetchone() is None:
            problems.append(f"asset_links: {owner_type} {owner_id} does not exist")
    question_ids = {row[0] for row in conn.execute("SELECT id FROM questions")}
    problems += runtime_reference_problems(conn, question_ids)
    return problems


def _ids_in(text: str | None) -> list[int]:
    if text is None:
        return []
    value = json.loads(text)
    found: list[int] = []

    def walk(node):
        if isinstance(node, bool):
            return
        if isinstance(node, int):
            found.append(node)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            for key, item in node.items():
                if key in ("question_id", "question_ids", "id") or isinstance(item, (list, dict)):
                    walk(item)
    walk(value)
    return found


def runtime_reference_problems(conn: sqlite3.Connection, question_ids: set[int]) -> list[str]:
    """Do question ids stored in runtime/operational rows resolve against ``question_ids``?

    Reads whatever rows the connection has; on a canonical rebuild they are empty,
    on a legacy database they are the historical references that must stay valid.
    """
    problems: list[str] = []

    def check(label: str, ids) -> None:
        missing = sorted({i for i in ids if i not in question_ids})
        if missing:
            problems.append(f"{label}: question ids {missing[:5]} do not resolve")

    tables = set(ct.application_tables(conn))
    if "quiz_answers" in tables:
        check("quiz_answers.question_id", (r[0] for r in conn.execute("SELECT question_id FROM quiz_answers")))
    if "quiz_attempts" in tables:
        check("quiz_attempts.question_ids_json",
              (i for (text,) in conn.execute("SELECT question_ids_json FROM quiz_attempts") for i in _ids_in(text)))
    if "practice_sessions" in tables:
        check("practice_sessions.question_ids_json",
              (i for (text,) in conn.execute("SELECT question_ids_json FROM practice_sessions") for i in _ids_in(text)))
    if "practice_answers" in tables:
        check("practice_answers.question_id", (r[0] for r in conn.execute("SELECT question_id FROM practice_answers")))
    # dzi_part1_attempts.slot_format_json carries no question references and is deliberately excluded.
    # This walker (_ids_in) skips a scalar such as "points" under a dict key that is not id-like, but the
    # updater's reference gate (update_canonical_content._json_ids) reads every integer in a registered
    # JSON column, so the column is registered with neither.
    if "dzi_part1_attempts" in tables:
        check("dzi_part1_attempts.question_ids_json",
              (i for (text,) in conn.execute("SELECT question_ids_json FROM dzi_part1_attempts") for i in _ids_in(text)))
    if "dzi_part1_answers" in tables:
        check("dzi_part1_answers.question_id", (r[0] for r in conn.execute("SELECT question_id FROM dzi_part1_answers")))
    if "quiz_assignments" in tables:
        check("quiz_assignments.question_plan_json",
              (i for (text,) in conn.execute("SELECT question_plan_json FROM quiz_assignments") for i in _ids_in(text)))
    if "generated_exams" in tables:
        check("generated_exams.question_ids",
              (i for (text,) in conn.execute("SELECT question_ids FROM generated_exams") for i in _ids_in(text)))
    return problems


def image_mapping_problems(mapping_path: Path, question_ids: set[int]) -> list[str]:
    """``db_question_id`` values of the confirmed DZI image mapping file must be canonical ids."""
    data = json.loads(Path(mapping_path).read_text(encoding="utf-8"))
    found: list[int] = []

    def walk(node):
        if isinstance(node, dict):
            for key, item in node.items():
                if key == "db_question_id":
                    found.append(item)
                else:
                    walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)
    walk(data)
    missing = sorted({i for i in found if i not in question_ids})
    return [f"image mappings: db_question_id {missing[:5]} do not resolve"] if missing else []


FRONTMATTER_IDS = re.compile(r"^question_ids:\s*\[([^\]]*)\]\s*$", re.MULTILINE)


def vault_reference_problems(vault_dir: Path, question_ids: set[int]) -> list[str]:
    """``question_ids: [..]`` frontmatter in tracked vault notes must name canonical questions."""
    problems = []
    for path in sorted(Path(vault_dir).rglob("*.md")):
        match = FRONTMATTER_IDS.search(path.read_text(encoding="utf-8"))
        if not match:
            continue
        ids = [int(token) for token in match.group(1).split(",") if token.strip()]
        missing = sorted({i for i in ids if i not in question_ids})
        if missing:
            problems.append(f"{path.name}: question ids {missing[:5]} do not resolve")
    return problems


def canonical_question_ids(canonical_dir: Path = ct.CANONICAL_DIR) -> set[int]:
    return {row["id"] for row in ct.read_table_rows(Path(canonical_dir) / "questions.jsonl")}
