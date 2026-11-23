"""Canonical content model shared by the exporter, loader and parity comparator (G1).

One module owns: the table classification, the load order, the stable-identity
natural keys, the single row serialization and the single hashing algorithm.
Everything that writes, reads, hashes or compares canonical rows goes through
``iter_table_lines`` / ``hash_lines`` so there is exactly one normalization.

Canonical files are ``data/canonical/<table>.jsonl``: UTF-8, LF, final newline,
one compact JSON object per row, every column in schema order, explicit null,
``ensure_ascii=False``, rows in primary-key order. Nothing here writes to a
database.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Iterable, Iterator

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CANONICAL_DIR = PROJECT_ROOT / "data" / "canonical"
MIGRATIONS_DIR = PROJECT_ROOT / "web" / "migrations"
MANIFEST_NAME = "manifest.json"
FORMAT_NAME = "learnpilot-canonical-content"
FORMAT_VERSION = 1

CANONICAL = "canonical"      # durable curriculum/content rows; rebuilt from tracked inputs
RUNTIME = "runtime"          # user/session state; never replaced by a content rebuild
OPERATIONAL = "operational"  # caches, sync mirrors, logs and bookkeeping with their own lifecycle

# Every application table is classified exactly once, with the reason. A table
# added by a migration without an entry here fails tests.
TABLE_CLASSIFICATION: dict[str, tuple[str, str]] = {
    "assessment_event_sections": (CANONICAL, "dormant curated assessment structure"),
    "assessment_events": (CANONICAL, "dormant curated assessment structure"),
    "asset_links": (CANONICAL, "DZI asset ownership metadata (no binary content)"),
    "assets": (CANONICAL, "DZI asset metadata: relative path and hash only, no blobs"),
    "curricula": (CANONICAL, "curriculum identity"),
    "curriculum_areas": (CANONICAL, "curriculum taxonomy"),
    "curriculum_modules": (CANONICAL, "curriculum structure"),
    "curriculum_sections": (CANONICAL, "curriculum structure"),
    "curriculum_topics": (CANONICAL, "curriculum structure"),
    "dzi_blueprint_slots": (CANONICAL, "DZI format definition"),
    "dzi_blueprints": (CANONICAL, "DZI format definition"),
    "exam_task_questions": (CANONICAL, "links DZI tasks to questions"),
    "exam_tasks": (CANONICAL, "DZI task skeleton"),
    "exams": (CANONICAL, "official exam identity"),
    "fill_in_subquestions": (CANONICAL, "question content"),
    "lessons": (CANONICAL, "curriculum structure (currently empty)"),
    "multiple_choice_options": (CANONICAL, "question content"),
    "official_exam_sources": (CANONICAL, "official source/archive metadata"),
    "practical_task_resources": (CANONICAL, "DZI practical task resource metadata"),
    "practical_tasks": (CANONICAL, "DZI practical task metadata"),
    "question_topic_assignments": (CANONICAL, "question to curriculum placement"),
    "questions": (CANONICAL, "question content"),
    "topic_aliases": (CANONICAL, "curriculum taxonomy"),
    "topic_classes": (CANONICAL, "curriculum structure"),
    "topic_concepts": (CANONICAL, "curriculum taxonomy (currently empty)"),
    "topic_prerequisites": (CANONICAL, "curriculum taxonomy (currently empty)"),
    "topic_section_assignments": (CANONICAL, "topic to section placement"),
    "practical_submission_files": (RUNTIME, "student upload metadata"),
    "practical_submissions": (RUNTIME, "student practical submissions"),
    "quiz_answers": (RUNTIME, "student answers"),
    "quiz_assignments": (RUNTIME, "teacher assignments, including seed smoke-test rows"),
    "quiz_attempts": (RUNTIME, "student attempts"),
    "quiz_text_answers": (RUNTIME, "student text answers"),
    "lifecycle_clock": (RUNTIME, "assignment lifecycle clock-health state: one row, shared by every process"),
    "users": (RUNTIME, "local accounts: credentials and session keys, never canonical content"),
    "totp_credentials": (RUNTIME, "admin TOTP credentials: encrypted secrets, never canonical content"),
    "totp_recovery_codes": (RUNTIME, "admin TOTP recovery codes: scrypt hashes, never canonical content"),
    "user_identities": (RUNTIME, "external sign-in identities (issuer + subject) of accounts, never canonical content"),
    "user_profiles": (RUNTIME, "declared names, grade and school of accounts: personal data, never canonical content"),
    "teacher_subjects": (RUNTIME, "subjects a teacher declared at registration, never canonical content"),
    "pending_registrations": (RUNTIME, "unverified registrations: personal data and a credential hash, never canonical content"),
    "password_recoveries": (RUNTIME, "password recovery in progress: an address, an account link and a credential hash; never canonical content"),
    "registration_sends": (RUNTIME, "send ledger of registration mail: keyed address hashes, never canonical content"),
    "registration_control": (RUNTIME, "registration clock mark and restore hold: one row at most, never canonical content"),
    "assignment_creations": (RUNTIME, "ledger of assignment creations per account, kept after closure or deletion: ids and times only, never canonical content"),
    "practice_sessions": (RUNTIME, "student practice sessions: account-owned plans, seeds and scores; never canonical content"),
    "practice_answers": (RUNTIME, "student practice answer verdicts per session and question; never canonical content"),
    "dzi_part1_attempts": (RUNTIME, "timed DZI Part 1 simulation attempts: account-owned plans, slot format, deadlines and scores; never canonical content"),
    "dzi_part1_answers": (RUNTIME, "DZI Part 1 simulation answer verdicts per attempt, slot and part; never canonical content"),
    "generated_exams": (OPERATIONAL, "generated exam log"),
    "login_throttle": (OPERATIONAL, "per-identifier login throttle counters (keyed hashes, no emails)"),
    "oidc_consumed_transactions": (OPERATIONAL, "single-use records of OIDC transactions (keyed hashes only)"),
    "note_question_links": (OPERATIONAL, "vault note mirror"),
    "obsidian_notes": (OPERATIONAL, "vault sync mirror"),
    "schema_migrations": (OPERATIONAL, "migration bookkeeping"),
    "scrape_log": (OPERATIONAL, "scrape log"),
    "sqlite_sequence": (OPERATIONAL, "SQLite internal; canonical sequences are recorded in the manifest"),
}

# Non-canonical tables a migration seeds, with the number of rows a database built
# from migrations alone holds. Every other runtime/operational table starts empty.
# lifecycle_clock: the single clock-health row of migration 018, which the
# application requires to exist (docs/ASSIGNMENT_LIFECYCLE_DESIGN.md, section 3.6).
MIGRATION_SEEDED_ROWS: dict[str, int] = {"lifecycle_clock": 1}

# Natural key per surrogate-id table. The stable-id guard fails if an update changes
# the id a natural key maps to. ``None`` means the id itself is the only identity.
# Questions are keyed only where source_number is set (579 legacy rows have none).
NATURAL_KEYS: dict[str, tuple[str, ...] | None] = {
    "assessment_events": ("event_slug",),
    "asset_links": ("asset_id", "owner_type", "owner_id", "role", "display_order"),
    "assets": ("local_path",),
    "curricula": ("subject", "grade", "education_type"),
    "curriculum_areas": ("area_id",),
    "curriculum_modules": None,
    "curriculum_sections": ("section_slug",),
    "curriculum_topics": ("topic_slug",),
    "dzi_blueprint_slots": ("blueprint_id", "slot_number"),
    "dzi_blueprints": ("blueprint_slug",),
    "exam_tasks": ("exam_id", "task_number"),
    "exams": None,
    "fill_in_subquestions": ("question_id", "subquestion_number"),
    "lessons": ("section_id", "lesson_number"),
    "multiple_choice_options": ("question_id", "option_letter"),
    "official_exam_sources": None,
    "practical_task_resources": ("exam_task_id", "resource_path"),
    "question_topic_assignments": None,
    "questions": ("source_exam", "source_number"),
    "topic_aliases": ("alias_slug",),
    "topic_concepts": ("topic_id", "concept"),
    "topic_section_assignments": ("topic_id", "section_id"),
}


class CanonicalError(RuntimeError):
    """Canonical data or its classification is inconsistent."""


def tables_of(kind: str) -> list[str]:
    return sorted(name for name, (table_kind, _) in TABLE_CLASSIFICATION.items() if table_kind == kind)


def application_tables(conn: sqlite3.Connection) -> list[str]:
    """Every table in the database, including SQLite's own sqlite_sequence."""
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name").fetchall()
    return [row[0] for row in rows]


def classification_problems(conn: sqlite3.Connection) -> list[str]:
    """Tables without a classification, and classified tables the schema lacks."""
    present = set(application_tables(conn))
    problems = [f"table {name} has no classification" for name in sorted(present - set(TABLE_CLASSIFICATION))]
    # sqlite_sequence only exists once an AUTOINCREMENT row was inserted.
    problems += [f"classified table {name} does not exist"
                 for name in sorted(set(TABLE_CLASSIFICATION) - present - {"sqlite_sequence"})]
    return problems


def table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]


def primary_key_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = sorted((row for row in conn.execute(f'PRAGMA table_info("{table}")') if row[5]), key=lambda r: r[5])
    return [row[1] for row in rows]


def is_autoincrement(conn: sqlite3.Connection, table: str) -> bool:
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()[0]
    return "AUTOINCREMENT" in (sql or "").upper()


def canonical_load_order(conn: sqlite3.Connection) -> list[str]:
    """Canonical tables in foreign-key-topological order (parents first, ties by name)."""
    canonical = set(tables_of(CANONICAL))
    parents: dict[str, set[str]] = {}
    for table in canonical:
        parents[table] = set()
        for row in conn.execute(f'PRAGMA foreign_key_list("{table}")'):
            parent = row[2]
            if parent == table:
                continue
            if parent not in canonical:
                raise CanonicalError(f"canonical table {table} references non-canonical table {parent}")
            parents[table].add(parent)
    order: list[str] = []
    remaining = dict(parents)
    while remaining:
        ready = sorted(name for name, needs in remaining.items() if needs <= set(order))
        if not ready:
            raise CanonicalError(f"foreign-key cycle among canonical tables: {sorted(remaining)}")
        order.extend(ready)
        for name in ready:
            del remaining[name]
    return order


# ----------------------------------------------------------------------------- serialization

def serialize_row(columns: list[str], values: Iterable) -> str:
    """The one canonical JSON line for a row (no trailing newline)."""
    row = {}
    for column, value in zip(columns, values):
        if isinstance(value, (bytes, bytearray, memoryview)):
            raise CanonicalError(f"column {column} holds a binary value; canonical data stores no blobs")
        row[column] = value
    return json.dumps(row, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def iter_table_lines(conn: sqlite3.Connection, table: str) -> Iterator[str]:
    """Canonical lines of one table, in primary-key order."""
    columns = table_columns(conn, table)
    keys = primary_key_columns(conn, table)
    if not keys:
        raise CanonicalError(f"canonical table {table} has no primary key to order by")
    select = ", ".join(f'"{column}"' for column in columns)
    order = ", ".join(f'"{key}"' for key in keys)
    for values in conn.execute(f'SELECT {select} FROM "{table}" ORDER BY {order}'):
        yield serialize_row(columns, values)


def hash_lines(lines: Iterable[str]) -> tuple[int, str]:
    """(row count, sha256 hex) over the UTF-8 bytes of ``line + LF`` for every line."""
    digest, count = hashlib.sha256(), 0
    for line in lines:
        digest.update(line.encode("utf-8"))
        digest.update(b"\n")
        count += 1
    return count, digest.hexdigest()


def table_digest(conn: sqlite3.Connection, table: str) -> tuple[int, str]:
    return hash_lines(iter_table_lines(conn, table))


def _reject_duplicate_keys(pairs):
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise CanonicalError("duplicate key in canonical row")
    return dict(pairs)


def read_table_rows(path: Path) -> Iterator[dict]:
    """Parse a canonical file strictly: LF only, final newline, normalized lines."""
    data = Path(path).read_bytes()
    if data.startswith(b"\xef\xbb\xbf"):
        raise CanonicalError(f"{path.name}: byte-order mark")
    if b"\r" in data:
        raise CanonicalError(f"{path.name}: carriage return; canonical files use LF")
    if data and not data.endswith(b"\n"):
        raise CanonicalError(f"{path.name}: missing final newline")
    for number, line in enumerate(data.decode("utf-8").split("\n")[:-1], 1):
        row = json.loads(line, object_pairs_hook=_reject_duplicate_keys)
        if serialize_row(list(row), row.values()) != line:
            raise CanonicalError(f"{path.name}:{number}: line is not in normalized canonical form")
        yield row


def file_digest(path: Path) -> tuple[int, str]:
    """(row count, sha256) of a canonical file using the same algorithm as ``table_digest``."""
    lines = (serialize_row(list(row), row.values()) for row in read_table_rows(path))
    return hash_lines(lines)


def max_id(conn: sqlite3.Connection, table: str) -> int | None:
    keys = primary_key_columns(conn, table)
    if len(keys) != 1:
        return None
    info = {row[1]: row[2] for row in conn.execute(f'PRAGMA table_info("{table}")')}
    if info[keys[0]].upper() != "INTEGER":
        return None
    return conn.execute(f'SELECT MAX("{keys[0]}") FROM "{table}"').fetchone()[0]


def stored_sequence(conn: sqlite3.Connection, table: str) -> int | None:
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'sqlite_sequence'").fetchone()
    if not exists:
        return None
    row = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = ?", (table,)).fetchone()
    return None if row is None else row[0]


def next_free_id(canonical_entry: dict, target_conn: sqlite3.Connection, table: str) -> int:
    """First id a NEW canonical row may take: above everything canonical or the target has used.

    "Used" includes sequence high-water marks, so ids of deleted rows are never recycled.
    """
    marks = [canonical_entry.get("max_id") or 0, canonical_entry.get("sqlite_sequence") or 0]
    marks.append(max_id(target_conn, table) or 0)
    marks.append(stored_sequence(target_conn, table) or 0)
    return max(marks) + 1
