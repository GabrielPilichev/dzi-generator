#!/usr/bin/env python3
"""Insert-only update of canonical content into an existing populated SQLite database (B4).

    python3 -m src.update_canonical_content --db PATH --source DIR [--preimage DIR]
        [--apply --expect-plan-sha256 HEX64] [--busy-timeout-ms N] [--format text|json]

A dry run is the default and writes no row. ``--apply`` needs both ``--preimage`` and the
plan hash of a dry run. The tool only ever inserts canonical rows that the target lacks
and raises canonical ``sqlite_sequence`` entries; it never updates, deletes, remaps or
migrates, and it never creates the target. Every non-canonical row of the target is
digested before the first canonical write and again before COMMIT.

The owner release record that ties the preimage to the target, and a complete snapshot
of the target, are external and unverified; the report says so in every mode.
Specification: docs/reviews/canonical-content-updater-*.md.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import stat
import struct
import sys
import tempfile
import unicodedata
from contextlib import closing
from pathlib import Path, PurePosixPath

from src import canonical_export as export
from src import canonical_load as load
from src import canonical_parity as parity
from src import canonical_tables as ct
from src import manage_migrations
from src.schema_introspection import normalize_sql, table_shape, user_table_names
from web.services import lifecycle

TOOL_VERSION = 1
PLAN_FORMAT_VERSION = 1
REPORT_FORMAT = "learnpilot-canonical-update-report/1"

DEFAULT_BUSY_MS = 5000
MIN_BUSY_MS = 100
MAX_BUSY_MS = 60000
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
LIST_LIMIT = 20
IDENTIFIER_LIMIT = 80
PATH_LIMIT = 512
INT64_MIN, INT64_MAX = -(2 ** 63), 2 ** 63 - 1

# ----------------------------------------------------------------------------- vocabulary

STATUS_NO_CHANGE = "no_change"
STATUS_PLANNED = "planned"
STATUS_INCOMPLETE = "incomplete"
STATUS_REFUSED = "refused"
STATUS_NOT_COMMITTED = "not_committed"
STATUS_APPLIED = "applied"
STATUS_UNKNOWN = "outcome_unknown"

ROW_CODES = (
    "value_differs", "deleted_in_target", "removed_in_source", "id_not_above_high_water",
    "sequence_below_preimage", "unique_key_exists", "unique_key_in_source_twice", "duplicate_content",
    "unsupported_unique_index", "history_child_of_existing", "asset_link_reaches_existing",
    "asset_link_legacy_owner_type", "asset_link_unknown_owner_type", "null_primary_key",
    "invalid_key_type", "binary_or_unserializable_value", "unsafe_file_reference",
    "preexisting_fk_violation", "preexisting_unresolved_reference", "malformed_reference_json",
    "noninteger_reference_value",
)
RUN_CODES = (
    "usage_error", "invalid_source", "invalid_preimage", "preimage_required", "pinned_copy_changed",
    "target_not_regular_file", "restore_lock_present", "journal_present", "incompatible_target",
    "plan_conflicts", "busy", "stale_plan", "write_failed", "commit_error_not_committed",
    "commit_error_committed", "receipt_not_written", "commit_outcome_unknown", "internal_error",
)

# Fixed notices, printed in every mode. Nothing here is verified by the tool.
UNVERIFIED_PREREQUISITES = (
    "the owner release record that ties the preimage to the target: external, unverified",
    "a complete snapshot of the target taken before an apply: external, unverified",
    "the deployed build, the asset files and the schema level of the real database: not inferred",
    "execution while the service is up, stopped or in a maintenance window: an operator decision; "
    "the tool cannot detect a running service",
    "D6/D7 content gating and the DZI gate: not satisfied by a valid plan or apply",
)
NOTICES = (
    "this report is not a history witness (PR #327 is open)",
    "no backup, file availability or approval is claimed",
)
FILE_AVAILABILITY = "file availability: not checked"

# ----------------------------------------------------------------------------- SQLite constants
# Numeric values of the authorizer action codes; the ``sqlite3`` constants exist only from 3.11.
_OK, _DENY = 0, 1
_A_INSERT, _A_PRAGMA, _A_READ, _A_SELECT, _A_TRANSACTION, _A_UPDATE, _A_FUNCTION = 18, 19, 20, 21, 22, 23, 31
_READ_PRAGMAS = frozenset({"table_info", "index_list", "index_xinfo", "foreign_key_list",
                           "foreign_key_check", "quick_check", "journal_mode"})
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PREDICATE = re.compile(r"^[A-Za-z_]\w* IS NOT NULL(?: AND [A-Za-z_]\w* IS NOT NULL)*$")
_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")

# Columns that hold a relative file reference: (table, column, sha256 column or None).
FILE_COLUMNS = (
    ("assets", "local_path", "sha256"),
    ("practical_task_resources", "resource_path", "sha256"),
    ("official_exam_sources", "local_path", "sha256"),
    ("exams", "source_file", "sha256"),
    ("questions", "image_path", None),
    ("curriculum_topics", "note_path", None),
    ("curriculum_areas", "moc_filename", None),
    ("curriculum_modules", "moc_filename", None),
)
KNOWN_ROOTS = ("data/reference/", "web/static/img/", "Topics/")

# History rules (implementation map section 7): one rule per canonical table.
CHILD_OF_QUESTION = {"multiple_choice_options", "fill_in_subquestions", "question_topic_assignments"}
PARENT_COUNTED = frozenset({
    "curriculum_topics", "curriculum_sections", "curriculum_modules", "lessons", "topic_aliases",
    "topic_classes", "topic_concepts", "topic_prerequisites", "assessment_events",
    "assessment_event_sections", "official_exam_sources", "dzi_blueprint_slots",
})
JSON_ID_COLUMNS = (("quiz_attempts", "question_ids_json"), ("quiz_assignments", "question_plan_json"),
                   ("generated_exams", "question_ids"))


class Stop(Exception):
    """Ends the run with a fixed status, run code and exit code."""

    def __init__(self, status: str, code: str | None, exit_code: int):
        super().__init__(code or status)
        self.status, self.code, self.exit_code = status, code, exit_code


class UsageError(Exception):
    pass


class PinError(Exception):
    pass


# ----------------------------------------------------------------------------- small helpers

def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _compact(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=repr)


def _combined(files: dict[str, str]) -> str:
    return _sha256("".join(f"{name}\0{digest}\n" for name, digest in sorted(files.items())).encode("utf-8"))


def _q(name: str) -> str:
    if not isinstance(name, str) or not _IDENTIFIER.match(name):
        raise ct.CanonicalError("unsafe identifier")
    return f'"{name}"'


def _escape(text, limit: int = IDENTIFIER_LIMIT) -> str:
    """Printable, bounded text: control and format characters are shown as escapes."""
    text = text if isinstance(text, str) else str(text)
    pieces = []
    for char in text:
        if unicodedata.category(char)[0] == "C":
            code = ord(char)
            pieces.append(f"\\x{code:02x}" if code < 256 else f"\\u{code:04x}" if code < 65536 else f"\\U{code:08x}")
        else:
            pieces.append(char)
    shown = "".join(pieces)
    return shown if len(shown) <= limit else shown[:limit - 3] + "..."


def _key_text(key) -> str:
    return _escape(_compact(list(key)) if isinstance(key, (tuple, list)) else _compact(key))


def _key_order(key) -> str:
    return _compact(list(key)) if isinstance(key, (tuple, list)) else _compact(key)


def _is_busy(exc: BaseException) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(code, int):
        return (code & 0xFF) in (5, 6)
    return str(exc) in ("database is locked", "database table is locked", "database schema is locked")


def _error_names(exc: BaseException) -> dict:
    name = getattr(exc, "sqlite_errorname", None)
    return {"class": type(exc).__name__, "sqlite_name": name if isinstance(name, str) else None}


# ----------------------------------------------------------------------------- pinning

def pin_directory(source: str, destination: Path, accept, *, reading_label: str = "directory") -> dict[str, str]:
    """Copy the regular files of ``source`` that ``accept`` names, reading each once; return sha256 per file."""
    try:
        info = os.lstat(source)
    except OSError as exc:
        raise PinError(f"{reading_label} is unreadable") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise PinError(f"{reading_label} is not a plain directory")
    hashes: dict[str, str] = {}
    total = 0
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        entries = sorted(os.scandir(source), key=lambda entry: entry.name)
    except OSError as exc:
        raise PinError(f"{reading_label} is unreadable") from exc
    for entry in entries:
        if accept(entry.name) is None:
            if accept.strict:
                raise PinError(f"{reading_label} holds an unexpected entry")
            continue
        if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
            raise PinError(f"{reading_label} holds a link or a non-regular entry")
        try:
            descriptor = os.open(entry.path, flags)
        except OSError as exc:
            raise PinError(f"{reading_label} holds an unreadable file") from exc
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or opened.st_size > MAX_FILE_BYTES:
                raise PinError(f"{reading_label} holds an unsuitable file")
            chunks, size = [], 0
            while True:
                block = os.read(descriptor, 1 << 20)
                if not block:
                    break
                size += len(block)
                if size > MAX_FILE_BYTES:
                    raise PinError(f"{reading_label} holds an oversized file")
                chunks.append(block)
        except OSError as exc:
            raise PinError(f"{reading_label} holds an unreadable file") from exc
        finally:
            os.close(descriptor)
        total += size
        if total > MAX_TOTAL_BYTES:
            raise PinError(f"{reading_label} is too large")
        data = b"".join(chunks)
        hashes[entry.name] = _sha256(data)
        with open(Path(destination) / entry.name, "xb") as handle:
            handle.write(data)
        os.chmod(Path(destination) / entry.name, 0o600)
    return hashes


class _Accept:
    """Name filter of a pinned directory; ``strict`` makes an unexpected entry an error."""

    def __init__(self, names=None, suffix: str | None = None, strict: bool = True):
        self.names, self.suffix, self.strict = names, suffix, strict

    def __call__(self, name: str):
        if self.names is not None:
            return name if name in self.names else None
        return name if self.suffix and name.endswith(self.suffix) else None


def _canonical_names() -> frozenset[str]:
    return frozenset({ct.MANIFEST_NAME} | {f"{table}.jsonl" for table in ct.tables_of(ct.CANONICAL)})


def _file_sha(path: Path) -> str:
    return _sha256(Path(path).read_bytes())


class Pinned:
    """A private copy of one input directory and the sha256 of every file read."""

    def __init__(self, directory: Path, files: dict[str, str]):
        self.directory, self.files = Path(directory), dict(files)

    def unchanged(self) -> bool:
        try:
            return all(_file_sha(self.directory / name) == digest for name, digest in self.files.items())
        except OSError:
            return False


# ----------------------------------------------------------------------------- reference schema

class TableRef:
    def __init__(self, name: str):
        self.name = name
        self.columns: list[str] = []
        self.types: dict[str, str] = {}
        self.pk: list[str] = []
        self.autoincrement = False
        self.unique: list[tuple[str, tuple[str, ...]]] = []
        self.unsupported: list[str] = []
        self.fks: list[tuple[str, str]] = []


class Reference:
    """Columns, keys, unique indexes and shapes of the schema the pinned migrations produce."""

    def __init__(self):
        self.tables: dict[str, TableRef] = {}
        self.load_order: list[str] = []
        self.shapes: dict[str, dict] = {}
        self.inventory: list[list[str]] = []
        self.migrations: list[str] = []
        self.schema_sha256 = ""


def build_reference(migrations_pinned: Path, scratch: Path) -> Reference:
    directory = Path(scratch) / "reference"
    directory.mkdir(mode=0o700)
    reference_path = directory / "reference.db"
    sqlite3.connect(reference_path).close()
    if manage_migrations.apply_migrations(reference_path, Path(migrations_pinned), out=io.StringIO()) != 0:
        raise ct.CanonicalError("the migrations did not build a reference schema")
    reference = Reference()
    reference.migrations = manage_migrations.migration_filenames(Path(migrations_pinned))
    with closing(sqlite3.connect(reference_path)) as conn:
        reference.load_order = ct.canonical_load_order(conn)
        for table in reference.load_order:
            ref = TableRef(table)
            info = list(conn.execute(f'PRAGMA table_info({_q(table)})'))
            ref.columns = [row[1] for row in info]
            ref.types = {row[1]: (row[2] or "").upper() for row in info}
            ref.pk = ct.primary_key_columns(conn, table)
            ref.autoincrement = ct.is_autoincrement(conn, table)
            ref.fks = [(row[3], row[2]) for row in conn.execute(f'PRAGMA foreign_key_list({_q(table)})')
                       if row[4] == "id" or row[4] is None]
            seen = set()
            for _seq, index_name, unique, _origin, partial in conn.execute(f'PRAGMA index_list({_q(table)})').fetchall():
                if not unique:
                    continue
                key_rows = [row for row in conn.execute(f'PRAGMA index_xinfo("{index_name}")') if row[5]]
                columns = tuple(row[2] for row in key_rows)
                supported = all(row[2] is not None and row[1] >= 0 and row[4] == "BINARY" for row in key_rows)
                if partial:
                    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (index_name,)).fetchone()[0]
                    predicate = normalize_sql(re.split(r"\bWHERE\b", sql or "", maxsplit=1, flags=re.IGNORECASE)[-1])
                    terms = set(re.findall(r"([A-Za-z_]\w*) IS NOT NULL", predicate))
                    supported = supported and bool(_PREDICATE.match(predicate)) and terms <= set(columns)
                if not supported:
                    ref.unsupported.append(index_name)
                elif columns not in seen:
                    seen.add(columns)
                    ref.unique.append((index_name, columns))
            natural = ct.NATURAL_KEYS.get(table)
            if natural and tuple(natural) not in seen:
                ref.unique.append(("natural_key", tuple(natural)))
            reference.tables[table] = ref
        reference.shapes = {name: table_shape(conn, name) for name in user_table_names(conn)}
        reference.inventory = [list(row) for row in conn.execute(
            "SELECT type, name, tbl_name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name")]
    reference.schema_sha256 = _sha256(_compact({"shapes": reference.shapes, "inventory": reference.inventory}).encode())
    return reference


# ----------------------------------------------------------------------------- source and preimage

def _key_problem(ref: TableRef, key: tuple) -> str | None:
    if any(value is None for value in key):
        return "null_primary_key"
    for column, value in zip(ref.pk, key):
        declared = ref.types.get(column, "")
        if declared == "INTEGER":
            if type(value) is not int or not INT64_MIN <= value <= INT64_MAX:
                return "invalid_key_type"
        elif declared == "TEXT":
            if type(value) is not str:
                return "invalid_key_type"
        else:
            return "invalid_key_type"
    return None


class Dataset:
    """A validated canonical directory: manifest, rows per table and its identity facts."""

    def __init__(self, pinned: Pinned, manifest: dict):
        self.pinned, self.manifest = pinned, manifest
        self.rows: dict[str, list[dict]] = {}
        self.manifest_sha256 = pinned.files[ct.MANIFEST_NAME]
        self.files_sha256 = _combined(pinned.files)
        self.migration_level = (manifest.get("schema") or {}).get("migration_level")
        self.migrations = list((manifest.get("schema") or {}).get("migrations") or [])
        self.key_problems: list[tuple[str, tuple, str]] = []

    def identity(self) -> dict:
        return {"manifest_sha256": self.manifest_sha256, "files_sha256": self.files_sha256,
                "migration_level": self.migration_level}

    def sequence(self, table: str):
        return self.manifest["tables"][table].get("sqlite_sequence")


def _load_rows(dataset: Dataset, ref: Reference, problem: str) -> None:
    for table in ref.load_order:
        tref = ref.tables[table]
        entry = dataset.manifest["tables"][table]
        if entry.get("columns") != tref.columns or entry.get("primary_key") != tref.pk \
                or bool(entry.get("autoincrement")) != tref.autoincrement:
            raise ct.CanonicalError(problem)
        rows = list(ct.read_table_rows(dataset.pinned.directory / entry["file"]))
        for row in rows:
            for column, value in row.items():
                if type(value) is int and not INT64_MIN <= value <= INT64_MAX and column not in tref.pk:
                    raise ct.CanonicalError(problem)
        dataset.rows[table] = rows


def validate_source(pinned: Pinned, migrations_pinned: Path, ref: Reference) -> Dataset:
    try:
        if export.check_directory(pinned.directory, migrations_pinned):
            raise ct.CanonicalError("source validation failed")
        manifest = export.load_manifest(pinned.directory)
        dataset = Dataset(pinned, manifest)
        if manifest.get("canonical_tables") != ref.load_order:
            raise ct.CanonicalError("source load order differs")
        _load_rows(dataset, ref, "source keys differ from the reference schema")
        for table in ref.load_order:
            tref = ref.tables[table]
            for row in dataset.rows[table]:
                key = tuple(row[column] for column in tref.pk)
                code = _key_problem(tref, key)
                if code:
                    dataset.key_problems.append((table, key, code))
        return dataset
    except Exception as exc:
        raise Stop(STATUS_REFUSED, "invalid_source", 1) from exc


def validate_preimage(pinned: Pinned, migrations_pinned: Path, ref: Reference, source: Dataset) -> Dataset:
    try:
        manifest = export.load_manifest(pinned.directory)
        if manifest.get("format") != ct.FORMAT_NAME or manifest.get("format_version") != ct.FORMAT_VERSION:
            raise ct.CanonicalError("unsupported preimage format")
        if manifest.get("canonical_tables") != ref.load_order or sorted(manifest.get("tables", {})) != sorted(ref.load_order):
            raise ct.CanonicalError("preimage table set differs")
        dataset = Dataset(pinned, manifest)
        previous = dataset.migrations
        repository = manage_migrations.migration_filenames(Path(migrations_pinned))
        if not previous or repository[:len(previous)] != previous or dataset.migration_level != previous[-1]:
            raise ct.CanonicalError("preimage migration list is not a prefix")
        for table in ref.load_order:
            entry = manifest["tables"][table]
            if entry.get("columns") != source.manifest["tables"][table].get("columns"):
                raise ct.CanonicalError("preimage columns differ")
            count, digest = ct.file_digest(pinned.directory / entry["file"])
            if count != entry.get("rows") or digest != entry.get("sha256"):
                raise ct.CanonicalError("preimage hash differs")
        _load_rows(dataset, ref, "preimage keys differ from the reference schema")
        for table in ref.load_order:
            tref, entry = ref.tables[table], manifest["tables"][table]
            keys = set()
            for row in dataset.rows[table]:
                key = tuple(row[column] for column in tref.pk)
                if _key_problem(tref, key):
                    raise ct.CanonicalError("preimage key problem")
                keys.add(key)
            if len(keys) != len(dataset.rows[table]):
                raise ct.CanonicalError("preimage duplicate key")
            ids = [row[tref.pk[0]] for row in dataset.rows[table]] \
                if len(tref.pk) == 1 and tref.types.get(tref.pk[0]) == "INTEGER" else []
            if (max(ids) if ids else None) != entry.get("max_id"):
                raise ct.CanonicalError("preimage max_id differs")
            sequence = entry.get("sqlite_sequence")
            if tref.autoincrement and ids and (sequence is None or sequence < max(ids)):
                raise ct.CanonicalError("preimage sequence below max id")
            if not tref.autoincrement and sequence is not None:
                raise ct.CanonicalError("preimage sequence without AUTOINCREMENT")
        return dataset
    except Exception as exc:
        raise Stop(STATUS_REFUSED, "invalid_preimage", 1) from exc


# ----------------------------------------------------------------------------- file references

def _relative_ok(raw: object) -> bool:
    """The rules of ``practical_resource_resolution._relative_parts``, as a predicate."""
    if not isinstance(raw, str) or not raw or raw != raw.strip():
        return False
    if (raw.startswith(("/", "~")) or "\\" in raw or ":" in raw
            or any(ord(char) < 32 or ord(char) == 127 for char in raw)
            or unicodedata.normalize("NFC", raw) != raw):
        return False
    if any(part in {"", ".", ".."} for part in raw.split("/")):
        return False
    return PurePosixPath(raw).as_posix() == raw


def file_reference_problem(table: str, column: str, raw: object) -> bool:
    """True when ``raw`` is not an acceptable inert relative file reference (never touches a file)."""
    if not isinstance(raw, str) or not raw or len(raw) > PATH_LIMIT:
        return True
    if column == "moc_filename":
        return "/" in raw or not _relative_ok(raw)
    if column == "resource_path" and "::" in raw:
        if raw.count("::") != 1:
            return True
        archive, member = raw.split("::")
        return not (archive.lower().endswith(".zip") and member and not member.endswith("/")
                    and _relative_ok(archive) and _relative_ok(member))
    if (table, column) == ("exams", "source_file") and raw.endswith("/"):
        raw = raw[:-1]
    return not _relative_ok(raw)


# ----------------------------------------------------------------------------- reading the target

def _read_authorizer(action, arg1, arg2, _database, _source):
    if action in (_A_SELECT, _A_READ, _A_FUNCTION, _A_TRANSACTION):
        return _OK
    if action == _A_PRAGMA and arg1 in _READ_PRAGMAS and (arg1 != "journal_mode" or arg2 is None):
        return _OK
    return _DENY


_CANONICAL_NAMES = frozenset(ct.tables_of(ct.CANONICAL))


def _write_authorizer(action, arg1, arg2, database, source):
    if _read_authorizer(action, arg1, arg2, database, source) == _OK:
        return _OK
    if action == _A_INSERT and (arg1 in _CANONICAL_NAMES or arg1 == "sqlite_sequence"):
        return _OK
    if action == _A_UPDATE and arg1 == "sqlite_sequence":
        return _OK
    return _DENY


class TargetTable:
    def __init__(self):
        self.lines: dict[tuple, str | None] = {}
        self.rows: dict[tuple, tuple] = {}
        self.bad: list[tuple] = []
        self.unreadable = False


def read_canonical(conn, ref: Reference, hooks) -> dict[str, TargetTable]:
    result = {}
    for table in ref.load_order:
        tref = ref.tables[table]
        target = TargetTable()
        columns = tref.columns
        positions = [columns.index(column) for column in tref.pk]
        select = ", ".join(_q(column) for column in columns)
        order = ", ".join(_q(column) for column in tref.pk)
        try:
            for values in conn.execute(f"SELECT {select} FROM {_q(table)} ORDER BY {order}"):
                key = tuple(values[i] for i in positions)
                try:
                    target.lines[key] = ct.serialize_row(columns, values)
                except (ct.CanonicalError, ValueError, TypeError):
                    target.lines[key] = None
                    target.bad.append(key)
                target.rows[key] = tuple(values)
        except sqlite3.DatabaseError as exc:
            if _is_busy(exc) or "decode" not in str(exc).lower():
                raise
            target.unreadable = True
        result[table] = target
        hooks("after_table_read", table)
    return result


def read_sequences(conn) -> dict[str, int]:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'sqlite_sequence'").fetchone():
        return {}
    return {name: seq for name, seq in conn.execute("SELECT name, seq FROM sqlite_sequence")}


def _value_digest(kind: str, value) -> bytes:
    if kind == "null":
        return b"N"
    if kind == "integer":
        return b"I" + struct.pack(">q", value)
    if kind == "real":
        return b"R" + struct.pack(">d", value)
    tag = b"T" if kind == "text" else b"B"
    return tag + struct.pack(">Q", len(value)) + bytes(value)


def table_multiset(conn, table: str) -> tuple[int, str]:
    """(row count, digest) of one table: storage-class exact and independent of row order."""
    columns = ct.table_columns(conn, table)
    parts = []
    for column in columns:
        parts.append(f"typeof({_q(column)})")
        parts.append(f"CASE WHEN typeof({_q(column)}) IN ('text', 'blob') THEN CAST({_q(column)} AS BLOB) "
                     f"ELSE {_q(column)} END")
    row_digests = []
    for values in conn.execute(f"SELECT {', '.join(parts)} FROM {_q(table)}"):
        digest = hashlib.sha256(struct.pack(">I", len(columns)))
        for index in range(len(columns)):
            digest.update(_value_digest(values[2 * index], values[2 * index + 1]))
        row_digests.append(digest.digest())
    row_digests.sort()
    total = hashlib.sha256(struct.pack(">Q", len(row_digests)))
    for item in row_digests:
        total.update(item)
    return len(row_digests), total.hexdigest()


NONCANONICAL_TABLES = tuple(sorted(name for name, (kind, _) in ct.TABLE_CLASSIFICATION.items()
                                   if kind != ct.CANONICAL and name != "sqlite_sequence"))


def preservation_snapshot(conn) -> dict:
    tables = {name: table_multiset(conn, name) for name in NONCANONICAL_TABLES}
    master = hashlib.sha256()
    for row in conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"):
        master.update(_compact(list(row)).encode("utf-8"))
        master.update(b"\n")
    sequences = {name: seq for name, seq in read_sequences(conn).items() if name not in _CANONICAL_NAMES}
    return {"tables": tables, "master": master.hexdigest(), "sequences": sequences}


def preservation_counts(conn) -> dict[str, int]:
    return {name: conn.execute(f"SELECT COUNT(*) FROM {_q(name)}").fetchone()[0] for name in NONCANONICAL_TABLES}


# ----------------------------------------------------------------------------- runtime reference gate

def _json_ids(text: str):
    """(ids, noninteger values) found in one stored JSON text; raises ValueError when malformed."""
    try:
        value = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise ValueError("malformed") from exc
    ids, odd = [], 0
    stack = [value]
    while stack:
        node = stack.pop()
        if node is None or isinstance(node, bool):
            continue
        if isinstance(node, int):
            ids.append(node)
        elif isinstance(node, float):
            odd += 1
        elif isinstance(node, str):
            try:
                int(node)
            except ValueError:
                continue
            odd += 1
        elif isinstance(node, list):
            stack.extend(node)
        elif isinstance(node, dict):
            stack.extend(node.values())
    return ids, odd


def reference_gate(conn) -> dict[tuple[str, str], int]:
    """Counts of unresolved or malformed pre-state references, by (column, fixed code)."""
    counts: dict[tuple[str, str], int] = {}

    def add(column: str, code: str, amount: int = 1) -> None:
        if amount:
            counts[(column, code)] = counts.get((column, code), 0) + amount

    violations: dict[str, int] = {}
    for row in conn.execute("PRAGMA foreign_key_check"):
        violations[row[0]] = violations.get(row[0], 0) + 1
    for table, amount in violations.items():
        add(f"{table}.foreign_key", "preexisting_fk_violation", amount)
    question_ids = {row[0] for row in conn.execute("SELECT id FROM questions")}
    topic_ids = {row[0] for row in conn.execute("SELECT id FROM curriculum_topics")}
    add("questions.topic_id", "preexisting_unresolved_reference", sum(
        1 for (topic,) in conn.execute("SELECT topic_id FROM questions WHERE topic_id IS NOT NULL")
        if topic not in topic_ids))
    owners = {"exam": {r[0] for r in conn.execute("SELECT id FROM exams")},
              "exam_task": {r[0] for r in conn.execute("SELECT id FROM exam_tasks")},
              "question": question_ids}
    for owner_type, owner_id in conn.execute("SELECT owner_type, owner_id FROM asset_links"):
        known = owners.get(owner_type) if isinstance(owner_type, str) else None
        if known is None or owner_id not in known:
            add("asset_links.owner_id", "preexisting_unresolved_reference")
    for table, column in JSON_ID_COLUMNS:
        label = f"{table}.{column}"
        for kind, raw in conn.execute(f"SELECT typeof({_q(column)}), CAST({_q(column)} AS BLOB) FROM {_q(table)}"):
            if kind == "null":
                continue
            if kind != "text":
                add(label, "malformed_reference_json")
                continue
            try:
                text = bytes(raw).decode("utf-8")
                if not text.strip():
                    raise ValueError("empty")
                ids, odd = _json_ids(text)
            except ValueError:
                add(label, "malformed_reference_json")
                continue
            add(label, "noninteger_reference_value", odd)
            add(label, "preexisting_unresolved_reference", sum(1 for i in ids if i not in question_ids))
    for kind, value in conn.execute("SELECT typeof(question_id), CASE WHEN typeof(question_id) = 'integer' "
                                    "THEN question_id END FROM quiz_answers"):
        if kind != "integer":
            add("quiz_answers.question_id", "noninteger_reference_value")
        elif value not in question_ids:
            add("quiz_answers.question_id", "preexisting_unresolved_reference")
    return counts


# ----------------------------------------------------------------------------- plan

class Plan:
    def __init__(self, ref: Reference):
        self.tables = {table: {"identical": 0, "insert": 0, "target_only": 0, "conflict": 0} for table in ref.load_order}
        self.conflicts: list[tuple[str, object, str, list[str]]] = []
        self.candidates: dict[str, list[tuple[tuple, str, dict]]] = {table: [] for table in ref.load_order}
        self.sequence_items: list[dict] = []
        self.under_existing_parent: dict[str, int] = {}
        self.questions_added_to_existing_topics = 0
        self.target_only: dict[str, list[tuple]] = {}
        self.file_refs: dict[str, dict] = {}
        self.outside_roots = 0
        self.reference_counts: dict[tuple[str, str], int] = {}
        self.preservation_counts: dict[str, int] = {}
        self.facts: dict = {}
        self.digest = ""
        self.pre_lines: dict[str, dict] = {}
        self.pre_sequences: dict[str, int] = {}

    def conflict(self, table: str, key, code: str, columns=()) -> None:
        self.conflicts.append((table, key, code, sorted(columns)))
        if table in self.tables:
            self.tables[table]["conflict"] += 1

    def footprint_empty(self) -> bool:
        return not self.sequence_items and not any(self.candidates.values())

    def candidate_count(self) -> int:
        return sum(len(rows) for rows in self.candidates.values())

    def conflict_totals(self) -> dict[str, int]:
        totals: dict[str, int] = {}
        for _table, _key, code, _columns in self.conflicts:
            totals[code] = totals.get(code, 0) + 1
        for (_column, code), amount in self.reference_counts.items():
            totals[code] = totals.get(code, 0) + amount
        return dict(sorted(totals.items()))

    def has_conflicts(self) -> bool:
        return bool(self.conflicts or self.reference_counts)


def _check_target(conn, ref: Reference, source: Dataset, pinned_migrations: list[str]) -> dict:
    """Compatibility of the open target; raises ``Stop`` (incompatible_target) when it is not."""
    refuse = Stop(STATUS_REFUSED, "incompatible_target", 1)
    try:
        if ct.classification_problems(conn):
            raise refuse
        applied = [row[0] for row in conn.execute("SELECT filename FROM schema_migrations ORDER BY filename")]
        if applied != pinned_migrations:
            raise refuse
        shapes = {name: table_shape(conn, name) for name in user_table_names(conn)}
        if shapes != ref.shapes:
            raise refuse
        inventory = [list(row) for row in conn.execute(
            "SELECT type, name, tbl_name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name")]
        if inventory != ref.inventory:
            raise refuse
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type IN ('trigger', 'view') LIMIT 1").fetchone():
            raise refuse
        if source.manifest["canonical_tables"] != ct.canonical_load_order(conn):
            raise refuse
        if conn.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise refuse
    except Stop:
        raise
    except (ct.CanonicalError, KeyError, TypeError) as exc:
        raise refuse from exc
    except sqlite3.DatabaseError as exc:
        if _is_busy(exc):
            raise
        raise refuse from exc
    return {"applied_migrations": len(applied), "schema_sha256": _sha256(_compact(
        {"shapes": shapes, "inventory": inventory}).encode()),
        "applied": applied}


def build_plan(conn, ref: Reference, source: Dataset, preimage: Dataset | None, ctx) -> Plan:
    """One consistent plan from the connection's current transaction (read phase or under the lock)."""
    plan = Plan(ref)
    facts = _check_target(conn, ref, source, ref.migrations)
    row = conn.execute("PRAGMA journal_mode").fetchone()
    facts["journal_mode"] = row[0] if row else None
    plan.facts = facts
    canon = read_canonical(conn, ref, ctx.hook)
    sequences = read_sequences(conn)
    plan.pre_sequences = sequences
    plan.pre_lines = {table: dict(canon[table].lines) for table in ref.load_order}
    plan.reference_counts = reference_gate(conn)
    plan.preservation_counts = preservation_counts(conn)
    for table, key, code in source.key_problems:
        plan.conflict(table, key, code)
    bad_source_keys = {(table, key) for table, key, _code in source.key_problems}
    for table, tref in ref.tables.items():
        for name in tref.unsupported:
            plan.conflict(table, name, "unsupported_unique_index")
        if canon[table].unreadable:
            plan.conflict(table, None, "binary_or_unserializable_value")
        for key in canon[table].bad:
            plan.conflict(table, key, "binary_or_unserializable_value")
    keysets: dict[str, set] = {}
    cand_keys: dict[str, set] = {table: set() for table in ref.load_order}
    preimage_keys = {table: {tuple(row[c] for c in ref.tables[table].pk) for row in preimage.rows[table]}
                     for table in ref.load_order} if preimage else None

    # Pass 1: row classification and the id high-water rule.
    for table in ref.load_order:
        tref, tt = ref.tables[table], canon[table]
        columns = tref.columns
        pre_keys = preimage_keys[table] if preimage_keys else set()
        source_keys = set()
        high_water = 0
        if tref.autoincrement:
            high_water = max([0, *(k[0] for k in tt.rows if isinstance(k[0], int)),
                              sequences.get(table) or 0,
                              *(k[0] for k in pre_keys if isinstance(k[0], int)),
                              (preimage.sequence(table) or 0) if preimage else 0])
        for row in source.rows[table]:
            key = tuple(row[column] for column in tref.pk)
            if (table, key) in bad_source_keys:
                continue
            source_keys.add(key)
            line = ct.serialize_row(columns, [row[column] for column in columns])
            if key in tt.rows:
                if tt.lines[key] is None:
                    continue
                if tt.lines[key] == line:
                    plan.tables[table]["identical"] += 1
                else:
                    differing = [column for column, a, b in zip(columns, tt.rows[key], [row[c] for c in columns])
                                 if _compact(a) != _compact(b)]
                    plan.conflict(table, key, "value_differs", differing)
                continue
            if key in pre_keys:
                plan.conflict(table, key, "deleted_in_target")
                continue
            plan.candidates[table].append((key, line, row))
            plan.tables[table]["insert"] += 1
            cand_keys[table].add(key)
            if not all(value is None or type(value) in (int, float, str) for value in row.values()):
                plan.conflict(table, key, "binary_or_unserializable_value")
            if tref.autoincrement and not key[0] > high_water:
                plan.conflict(table, key, "id_not_above_high_water")
        for key in sorted(pre_keys - source_keys, key=_key_order):
            plan.conflict(table, key, "removed_in_source")
        extras = [key for key in tt.rows if key not in source_keys and key not in pre_keys]
        plan.tables[table]["target_only"] = len(extras)
        plan.target_only[table] = sorted(extras, key=_key_order)[:LIST_LIMIT]
        keysets[table] = set(tt.rows)
        if tref.autoincrement and preimage and preimage.sequence(table) is not None \
                and (sequences.get(table) or 0) < preimage.sequence(table):
            plan.conflict(table, None, "sequence_below_preimage")

    # Pass 2: unique keys, the non-id duplicate guard, history rules, references and file columns.
    for table in ref.load_order:
        tref, tt = ref.tables[table], canon[table]
        candidates = plan.candidates[table]
        if not candidates:
            continue
        columns = tref.columns
        for _name, key_columns in tref.unique:
            indexes = [columns.index(column) for column in key_columns]
            existing = {}
            for tkey, values in tt.rows.items():
                combined = tuple(values[i] for i in indexes)
                if None not in combined:
                    existing[combined] = tkey
            seen_candidates = set()
            for key, _line, row in candidates:
                combined = tuple(row[column] for column in key_columns)
                if None in combined:
                    continue
                if combined in existing:
                    plan.conflict(table, key, "unique_key_exists", key_columns)
                elif combined in seen_candidates:
                    plan.conflict(table, key, "unique_key_in_source_twice", key_columns)
                seen_candidates.add(combined)
        if tref.pk == ["id"]:
            others = [column for column in columns if column != "id"]
            signatures = {_compact([_compact(v) for v, c in zip(values, columns) if c != "id"]): tkey
                          for tkey, values in tt.rows.items()}
            seen_content = set()
            for key, _line, row in candidates:
                signature = _compact([_compact(row[c]) for c in others])
                if signature in signatures or signature in seen_content:
                    plan.conflict(table, key, "duplicate_content")
                seen_content.add(signature)
    _history_rules(plan, ref, canon, cand_keys, keysets)
    _file_reference_rules(plan, ref)

    # Sequences: expected post-state per AUTOINCREMENT table (the write footprint, part b).
    for table in ref.load_order:
        tref = ref.tables[table]
        manifest_sequence = source.sequence(table)
        if not tref.autoincrement or manifest_sequence is None:
            continue
        pre = sequences.get(table)
        expected = max(pre or 0, manifest_sequence)
        if expected != (pre or 0):
            plan.sequence_items.append({"table": table, "pre": pre, "expected": expected})
    plan.digest = _plan_digest(plan, ref, source, preimage, canon, sequences, facts)
    return plan


def _history_rules(plan: Plan, ref: Reference, canon, cand_keys, keysets) -> None:
    def is_new(table: str, value) -> bool:
        return (value,) in cand_keys[table]

    def is_existing(table: str, value) -> bool:
        return (value,) in keysets[table] and not is_new(table, value)

    questions_new, tasks_new = cand_keys["questions"], cand_keys["exam_tasks"]
    for table in ref.load_order:
        for key, _line, row in plan.candidates[table]:
            if table in CHILD_OF_QUESTION and (row["question_id"],) not in questions_new:
                plan.conflict(table, key, "history_child_of_existing", ["question_id"])
            elif table == "exam_tasks" and not is_new("exams", row["exam_id"]):
                plan.conflict(table, key, "history_child_of_existing", ["exam_id"])
            elif table == "practical_tasks" and (row["task_id"],) not in tasks_new:
                plan.conflict(table, key, "history_child_of_existing", ["task_id"])
            elif table == "practical_task_resources" and (row["exam_task_id"],) not in tasks_new:
                plan.conflict(table, key, "history_child_of_existing", ["exam_task_id"])
            elif table == "exam_task_questions" and ((row["task_id"],) not in tasks_new
                                                     or (row["question_id"],) not in questions_new):
                plan.conflict(table, key, "history_child_of_existing", ["task_id", "question_id"])
            elif table == "topic_section_assignments":
                if not is_new("curriculum_topics", row["topic_id"]):
                    plan.conflict(table, key, "history_child_of_existing", ["topic_id"])
                elif is_existing("curriculum_sections", row["section_id"]):
                    plan.under_existing_parent[table] = plan.under_existing_parent.get(table, 0) + 1
            elif table == "asset_links":
                owner_type, owner_id = row["owner_type"], row["owner_id"]
                if owner_type == "questions":
                    plan.conflict(table, key, "asset_link_legacy_owner_type", ["owner_type"])
                elif owner_type not in ("question", "exam_task", "exam"):
                    plan.conflict(table, key, "asset_link_unknown_owner_type", ["owner_type"])
                elif owner_type == "question":
                    if (owner_id,) not in questions_new:
                        plan.conflict(table, key, "asset_link_reaches_existing", ["owner_id"])
                elif owner_type == "exam_task":
                    linked = [r["question_id"] for _k, _l, r in plan.candidates["exam_task_questions"]
                              if r["task_id"] == owner_id]
                    if (owner_id,) not in tasks_new or any((q,) not in questions_new for q in linked):
                        plan.conflict(table, key, "asset_link_reaches_existing", ["owner_id"])
                elif is_existing("exams", owner_id):
                    plan.under_existing_parent[table] = plan.under_existing_parent.get(table, 0) + 1
            elif table == "questions":
                if row["topic_id"] is not None and is_existing("curriculum_topics", row["topic_id"]):
                    plan.questions_added_to_existing_topics += 1
            elif table in PARENT_COUNTED:
                for column, parent in ref.tables[table].fks:
                    value = row[column]
                    if value is not None and parent in keysets and is_existing(parent, value):
                        plan.under_existing_parent[table] = plan.under_existing_parent.get(table, 0) + 1
                        break


def _file_reference_rules(plan: Plan, ref: Reference) -> None:
    for table, column, sha_column in FILE_COLUMNS:
        label = f"{table}.{column}"
        entry = plan.file_refs.setdefault(label, {"total": 0, "listed": []})
        for key, _line, row in plan.candidates[table]:
            value = row[column]
            if value is None:
                continue
            if file_reference_problem(table, column, value):
                plan.conflict(table, key, "unsafe_file_reference", [column])
                continue
            entry["total"] += 1
            if column != "moc_filename" and not value.startswith(KNOWN_ROOTS) and table != "exams":
                plan.outside_roots += 1
            if len(entry["listed"]) < LIST_LIMIT:
                sha = row[sha_column] if sha_column else None
                entry["listed"].append({"path": _escape(value, PATH_LIMIT + 8),
                                        "sha256": None if sha is None else _escape(sha),
                                        "availability": "not checked"})


def _plan_digest(plan: Plan, ref: Reference, source: Dataset, preimage, canon, sequences, facts) -> str:
    actions = [[table, list(key), _sha256(line.encode("utf-8"))]
               for table, rows in plan.candidates.items() for key, line, _row in rows]
    actions += [[item["table"], item["pre"], item["expected"]] for item in plan.sequence_items]
    payload = {
        "plan_format": PLAN_FORMAT_VERSION,
        "source": source.identity(),
        "preimage": preimage.identity() if preimage else None,
        "migrations": _combined({name: "" for name in ref.migrations}),
        "target_migrations": facts["applied"],
        "target_schema": facts["schema_sha256"],
        "target_canonical": {table: list(ct.hash_lines(line for line in canon[table].lines.values() if line is not None))
                             for table in ref.load_order},
        "target_sequences": {table: sequences.get(table) for table in ref.load_order if ref.tables[table].autoincrement},
        "source_sequences": {table: source.sequence(table) for table in ref.load_order},
        "actions": sorted(actions, key=_compact),
    }
    return _sha256(_compact(payload).encode("utf-8"))


# ----------------------------------------------------------------------------- report

def _plan_view(plan: Plan | None) -> dict:
    if plan is None:
        return {}
    totals = plan.conflict_totals()
    ordered = sorted(plan.conflicts, key=lambda item: (item[0], _key_order(item[1]), item[2]))
    return {
        "tables": plan.tables,
        "sequence_items": plan.sequence_items,
        "conflicts": {
            "total": sum(totals.values()), "by_code": totals,
            "listed": [{"table": table, "key": _key_text(key), "code": code, "columns": columns}
                       for table, key, code, columns in ordered[:LIST_LIMIT]],
        },
        "target_only_keys": {table: [_key_text(key) for key in keys] for table, keys in plan.target_only.items() if keys},
        "under_existing_parent": dict(sorted(plan.under_existing_parent.items())),
        "questions_added_to_existing_topics": plan.questions_added_to_existing_topics,
        "unresolved_references": {column: {code: n for (c, code), n in sorted(plan.reference_counts.items()) if c == column}
                                  for column in sorted({c for c, _code in plan.reference_counts})},
        "file_references": {"availability": FILE_AVAILABILITY, "inserted": plan.file_refs,
                            "outside_known_roots": plan.outside_roots},
        "target": {"schema_sha256": plan.facts.get("schema_sha256"), "journal_mode": _escape(plan.facts.get("journal_mode")),
                   "applied_migrations": plan.facts.get("applied_migrations")} if plan.facts else None,
    }


def build_report(ctx, status: str, code: str | None, exit_code: int) -> dict:
    plan = ctx.plan
    view = _plan_view(plan)
    preservation = {"tables": len(NONCANONICAL_TABLES), "rows": plan.preservation_counts if plan else {},
                    "equal": ctx.preservation_equal}
    report = {
        "format": REPORT_FORMAT,
        "tool_version": TOOL_VERSION,
        "plan_format_version": PLAN_FORMAT_VERSION,
        "mode": ctx.mode,
        "status": status,
        "exit_code": exit_code,
        "run_code": code,
        "plan_sha256": ctx.plan_hash_for_report(status),
        "source": ctx.source.identity() if ctx.source else None,
        "preimage": ctx.preimage.identity() if ctx.preimage else None,
        "repository_migrations_sha256": ctx.migrations_identity,
    }
    report.update(view)
    report["preservation"] = preservation
    report["outcome"] = ctx.outcome_text(status, code, exit_code)
    report["commit_error"] = ctx.commit_error
    report["unverified_prerequisites"] = list(UNVERIFIED_PREREQUISITES)
    report["notices"] = list(NOTICES)
    return report


def render_text(report: dict) -> str:
    lines = [f"LearnPilot canonical content update report ({report['format']})",
             f"mode: {report['mode']}", f"status: {report['status']}", f"exit code: {report['exit_code']}",
             f"run code: {report['run_code'] or '-'}"]
    lines.append(f"outcome: {report['outcome']}")
    if report.get("commit_error"):
        error = report["commit_error"]
        lines.append(f"commit error: {error['class']} {error['sqlite_name'] or '-'}")
    lines.append(f"plan sha256: {report['plan_sha256'] or '-'}")
    lines.append(f"tool version: {report['tool_version']}; plan format version: {report['plan_format_version']}")
    for label in ("source", "preimage"):
        identity = report.get(label)
        if identity:
            lines.append(f"{label}: manifest sha256 {identity['manifest_sha256']}; files sha256 {identity['files_sha256']}; "
                         f"migration level {_escape(identity['migration_level'])}")
        else:
            lines.append(f"{label}: -")
    lines.append(f"repository migrations sha256: {report['repository_migrations_sha256'] or '-'}")
    target = report.get("target")
    if target:
        lines.append(f"target: schema sha256 {target['schema_sha256']}; journal mode {target['journal_mode']}; "
                     f"applied migrations {target['applied_migrations']}")
    for table, counters in (report.get("tables") or {}).items():
        lines.append(f"table {table}: identical {counters['identical']}, insert {counters['insert']}, "
                     f"target_only {counters['target_only']}, conflict {counters['conflict']}")
    for item in report.get("sequence_items") or []:
        lines.append(f"sequence item {item['table']}: {item['pre'] if item['pre'] is not None else 'absent'} -> {item['expected']}")
    conflicts = report.get("conflicts")
    if conflicts:
        lines.append(f"conflicts: {conflicts['total']}")
        for code, count in conflicts["by_code"].items():
            lines.append(f"  code {code}: {count}")
        for item in conflicts["listed"]:
            lines.append(f"  - {item['table']} {item['key']} {item['code']} {','.join(item['columns']) or '-'}")
    for table, keys in (report.get("target_only_keys") or {}).items():
        lines.append(f"target only {table}: {', '.join(keys)}")
    for table, count in (report.get("under_existing_parent") or {}).items():
        lines.append(f"under_existing_parent {table}: {count}")
    if "questions_added_to_existing_topics" in report:
        lines.append(f"questions_added_to_existing_topics: {report['questions_added_to_existing_topics']}")
    for column, codes in (report.get("unresolved_references") or {}).items():
        lines.append(f"unresolved reference {column}: " + ", ".join(f"{code} {n}" for code, n in codes.items()))
    refs = report.get("file_references")
    if refs:
        lines.append(refs["availability"])
        lines.append(f"outside known roots (advisory): {refs['outside_known_roots']}")
        for column, entry in refs["inserted"].items():
            if entry["total"]:
                lines.append(f"file references {column}: {entry['total']}")
            for item in entry["listed"]:
                lines.append(f"  - {item['path']} sha256 {item['sha256'] or '-'} ({FILE_AVAILABILITY})")
    preservation = report["preservation"]
    equal = {True: "yes", False: "no", None: "not compared"}[preservation["equal"]]
    lines.append(f"non-canonical preservation: {preservation['tables']} tables, equal: {equal}")
    for table, count in sorted(preservation["rows"].items()):
        lines.append(f"  {table}: {count} rows")
    lines.append("unverified external prerequisites:")
    lines += [f"  - {text}" for text in report["unverified_prerequisites"]]
    lines += [f"note: {text}" for text in report["notices"]]
    return "\n".join(lines) + "\n"


def _write_text(stream, text: str) -> None:
    try:
        stream.write(text)
    except UnicodeEncodeError:
        stream.write(text.encode("ascii", "backslashreplace").decode("ascii"))
    flush = getattr(stream, "flush", None)
    if flush:
        flush()


# ----------------------------------------------------------------------------- run context

class Context:
    def __init__(self, hooks, connect, migrations_dir):
        self.hooks, self.connect, self.migrations_dir = hooks, connect, migrations_dir
        self.mode = "dry-run"
        self.format = "text"
        self.args = None
        self.scratch: str | None = None
        self.plan: Plan | None = None
        self.source: Dataset | None = None
        self.preimage: Dataset | None = None
        self.migrations_identity: str | None = None
        self.wrote = False
        self.preservation_equal = None
        self.commit_error: dict | None = None
        self.pinned: list[Pinned] = []
        self.verified_hash: str | None = None
        self.reference_tables: dict = {}

    def hook(self, name: str, *args) -> None:
        callback = self.hooks.get(name) if isinstance(self.hooks, dict) else getattr(self.hooks, name, None)
        if callback is not None:
            callback(*args)

    def open_connection(self, uri: str):
        connect = self.connect if self.connect is not None else sqlite3.connect
        return connect(uri, uri=True, timeout=self.args.busy_timeout_ms / 1000.0, isolation_level=None)

    def plan_hash_for_report(self, status: str) -> str | None:
        if self.plan is None or status in (STATUS_INCOMPLETE,) or self.preimage is None:
            return None
        if self.plan.has_conflicts() or status == STATUS_REFUSED:
            return None
        return self.plan.digest

    def outcome_text(self, status: str, code: str | None, exit_code: int) -> str:
        if status == STATUS_APPLIED and code == "commit_error_committed":
            name = (self.commit_error or {}).get("sqlite_name") or (self.commit_error or {}).get("class")
            return ("COMMITTED: the planned write footprint is visible to a fresh connection after a COMMIT error "
                    f"{name}")
        if status == STATUS_APPLIED and code == "receipt_not_written":
            return "COMMITTED: the transaction was committed and the receipt could not be written"
        if status == STATUS_APPLIED:
            return "COMMITTED: the plan was applied in one transaction"
        if status == STATUS_UNKNOWN:
            return ("OUTCOME UNKNOWN: the COMMIT raised and neither state could be established. "
                    "Do not retry this plan or token; the next step is a fresh dry run under separate authority")
        if status == STATUS_NOT_COMMITTED:
            return "NOT COMMITTED: the write transaction did not commit; the database was left as it was"
        if status == STATUS_NO_CHANGE:
            return "no change: the write footprint is empty; nothing is written or committed"
        if status == STATUS_PLANNED:
            return "planned: a dry run writes nothing"
        if status == STATUS_INCOMPLETE:
            return "incomplete: no plan hash without --preimage; an apply is impossible"
        return "refused: the database was left untouched"


def _rollback(conn) -> None:
    try:
        conn.execute("ROLLBACK")
    except Exception:  # no transaction is active, or the connection is unusable
        pass


# ----------------------------------------------------------------------------- the run

def _build_parser() -> argparse.ArgumentParser:
    class Parser(argparse.ArgumentParser):
        def error(self, message):  # never echo the argument text
            raise UsageError()

    parser = Parser(prog="python3 -m src.update_canonical_content", add_help=False, allow_abbrev=False,
                    description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--preimage")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expect-plan-sha256")
    parser.add_argument("--busy-timeout-ms", type=int, default=DEFAULT_BUSY_MS)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("-h", "--help", action="store_true")
    return parser


def run(argv, *, out, err, hooks=None, connect=None, migrations_dir=ct.MIGRATIONS_DIR) -> int:
    """Run the tool; ``hooks``, ``connect`` and ``migrations_dir`` are test seams, not CLI options."""
    ctx = Context(hooks or {}, connect, migrations_dir)
    try:
        status, code, exit_code = _guarded(ctx, list(argv))
    except Stop as stop:
        status, code, exit_code = stop.status, stop.code, stop.exit_code
    finally:
        if ctx.scratch:
            shutil.rmtree(ctx.scratch, ignore_errors=True)
    if status == "help":
        _write_text(out, _build_parser().format_help())
        return 0
    report = build_report(ctx, status, code, exit_code)
    text = json.dumps(report, indent=2, ensure_ascii=True) + "\n" if ctx.format == "json" else render_text(report)
    try:
        _write_text(out, text)
    except Exception:
        if status == STATUS_APPLIED:
            exit_code, code = 4, "receipt_not_written"
            report = build_report(ctx, status, code, exit_code)
            try:
                _write_text(err, render_text(report))
            except Exception:
                pass
    return exit_code


def _guarded(ctx: Context, argv: list[str]):
    try:
        return _execute(ctx, argv)
    except Stop:
        raise
    except UsageError:
        raise Stop(STATUS_REFUSED, "usage_error", 1)
    except Exception as exc:
        if ctx.wrote:
            raise Stop(STATUS_NOT_COMMITTED, "internal_error", 3) from exc
        raise Stop(STATUS_REFUSED, "internal_error", 1) from exc


def _execute(ctx: Context, argv: list[str]):
    if any(item in ("-h", "--help") for item in argv):
        return "help", None, 0
    parser = _build_parser()
    for index, item in enumerate(argv):
        if item == "--format" and index + 1 < len(argv) and argv[index + 1] == "json":
            ctx.format = "json"
    args = parser.parse_args(argv)
    ctx.args, ctx.format = args, args.format
    ctx.mode = "apply" if args.apply else "dry-run"
    if not MIN_BUSY_MS <= args.busy_timeout_ms <= MAX_BUSY_MS:
        raise UsageError()
    if args.expect_plan_sha256 is not None and (not args.apply or not _HEX64.match(args.expect_plan_sha256)):
        raise UsageError()
    if args.apply and args.expect_plan_sha256 is None:
        raise UsageError()
    if args.apply and not args.preimage:
        raise Stop(STATUS_REFUSED, "preimage_required", 1)
    token = args.expect_plan_sha256.lower() if args.expect_plan_sha256 else None

    # Stage 1: pin and validate the inputs (each file is read once, into a private directory).
    ctx.scratch = tempfile.mkdtemp(prefix="learnpilot-updater-")
    os.chmod(ctx.scratch, 0o700)
    scratch = Path(ctx.scratch)
    for name in ("migrations", "source", "preimage"):
        (scratch / name).mkdir(mode=0o700)
    try:
        migration_hashes = pin_directory(str(ctx.migrations_dir), scratch / "migrations",
                                         _Accept(suffix=".sql", strict=False), reading_label="migrations")
    except PinError as exc:
        raise Stop(STATUS_REFUSED, "internal_error", 1) from exc
    migrations = Pinned(scratch / "migrations", migration_hashes)
    ctx.migrations_identity = _sha256(_compact(sorted(migration_hashes.items())).encode("utf-8"))
    try:
        source_pin = Pinned(scratch / "source", pin_directory(args.source, scratch / "source",
                                                              _Accept(_canonical_names()), reading_label="source"))
    except PinError as exc:
        raise Stop(STATUS_REFUSED, "invalid_source", 1) from exc
    preimage_pin = None
    if args.preimage:
        try:
            preimage_pin = Pinned(scratch / "preimage", pin_directory(args.preimage, scratch / "preimage",
                                                                      _Accept(_canonical_names()), reading_label="preimage"))
        except PinError as exc:
            raise Stop(STATUS_REFUSED, "invalid_preimage", 1) from exc
    ctx.pinned = [migrations, source_pin] + ([preimage_pin] if preimage_pin else [])
    ctx.hook("after_pin")
    try:
        reference = build_reference(migrations.directory, scratch)
    except Exception as exc:
        raise Stop(STATUS_REFUSED, "internal_error", 1) from exc
    ctx.source = validate_source(source_pin, migrations.directory, reference)
    if preimage_pin:
        ctx.preimage = validate_preimage(preimage_pin, migrations.directory, reference, ctx.source)

    # Stage 2: target preflight, before anything reads the target.
    given = os.path.abspath(args.db)
    resolved = os.path.realpath(args.db)
    if _restore_lock(given, resolved):
        raise Stop(STATUS_REFUSED, "restore_lock_present", 1)
    try:
        regular = stat.S_ISREG(os.lstat(args.db).st_mode)
    except OSError:
        regular = False
    if not regular:
        raise Stop(STATUS_REFUSED, "target_not_regular_file", 1)
    if os.path.lexists(resolved + "-journal"):
        raise Stop(STATUS_REFUSED, "journal_present", 1)

    uri = Path(resolved).as_uri() + "?mode=rw"
    conn = _open_target(ctx, uri)
    try:
        return _with_target(ctx, conn, reference, token, given, resolved, uri)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _restore_lock(given: str, resolved: str) -> bool:
    return lifecycle.restore_lock_present(given) or lifecycle.restore_lock_present(resolved)


def _open_target(ctx: Context, uri: str):
    conn = ctx.open_connection(uri)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA query_only=ON")
        conn.set_authorizer(_read_authorizer)
        return conn
    except sqlite3.DatabaseError as exc:
        conn.close()
        if _is_busy(exc):
            raise Stop(STATUS_REFUSED, "busy", 2) from exc
        raise Stop(STATUS_REFUSED, "incompatible_target", 1) from exc


def _read_phase(ctx: Context, conn, reference: Reference) -> Plan:
    try:
        conn.execute("BEGIN")
        try:
            plan = build_plan(conn, reference, ctx.source, ctx.preimage, ctx)
        finally:
            _rollback(conn)
    except sqlite3.DatabaseError as exc:
        if _is_busy(exc):
            raise Stop(STATUS_REFUSED, "busy", 2) from exc
        raise Stop(STATUS_REFUSED, "incompatible_target", 1) from exc
    return plan


def _phase_stop(ctx: Context, code: str) -> Stop:
    if ctx.wrote:
        return Stop(STATUS_NOT_COMMITTED, code, 3)
    return Stop(STATUS_REFUSED, code, 1)


def _with_target(ctx: Context, conn, reference: Reference, token, given: str, resolved: str, uri: str):
    ctx.reference_tables = reference.tables
    plan = _read_phase(ctx, conn, reference)
    ctx.plan = plan
    ctx.hook("after_read_phase", conn)
    if not all(pin.unchanged() for pin in ctx.pinned):
        raise Stop(STATUS_REFUSED, "pinned_copy_changed", 1)
    if ctx.preimage is None:
        return STATUS_INCOMPLETE, "preimage_required", 1
    if plan.has_conflicts():
        raise Stop(STATUS_REFUSED, "plan_conflicts", 1)
    if ctx.args.apply and token != plan.digest:
        raise Stop(STATUS_REFUSED, "stale_plan", 2)
    if not ctx.args.apply:
        return (STATUS_NO_CHANGE if plan.footprint_empty() else STATUS_PLANNED), None, 0
    if plan.footprint_empty():
        return STATUS_NO_CHANGE, None, 0
    return _apply(ctx, conn, reference, token, given, resolved, uri)


# ----------------------------------------------------------------------------- apply

def _apply(ctx: Context, conn, reference: Reference, token: str, given: str, resolved: str, uri: str):
    try:
        conn.set_authorizer(None)
        conn.execute("PRAGMA query_only=OFF")
        conn.set_authorizer(_write_authorizer)
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.DatabaseError as exc:
            if _is_busy(exc):
                raise Stop(STATUS_REFUSED, "busy", 2) from exc
            raise
        ctx.hook("after_lock", conn)
        if _restore_lock(given, resolved):
            raise _phase_stop(ctx, "restore_lock_present")
        plan = build_plan(conn, reference, ctx.source, ctx.preimage, ctx)
        ctx.plan = plan
        if plan.digest != token:
            raise Stop(STATUS_REFUSED, "stale_plan", 2)
        if plan.has_conflicts():
            raise Stop(STATUS_REFUSED, "plan_conflicts", 1)
        if not all(pin.unchanged() for pin in ctx.pinned):
            raise _phase_stop(ctx, "pinned_copy_changed")
        before = preservation_snapshot(conn)
        ctx.plan.preservation_counts = {name: n for name, (n, _digest) in before["tables"].items()}
        canonical_before = {table: dict(plan.pre_lines[table]) for table in reference.load_order}
        _write_footprint(ctx, conn, reference, plan)
        ctx.hook("before_commit", conn)
        _validate_before_commit(ctx, conn, reference, plan, before, canonical_before, given, resolved)
    except Stop:
        _rollback(conn)
        raise
    except Exception as exc:
        _rollback(conn)
        if ctx.wrote:
            raise Stop(STATUS_NOT_COMMITTED, "write_failed", 3) from exc
        raise Stop(STATUS_REFUSED, "internal_error", 1) from exc
    ctx.preservation_equal = True
    return _commit(ctx, conn, plan, resolved, uri)


def _write_footprint(ctx: Context, conn, reference: Reference, plan: Plan) -> None:
    items = {item["table"]: item for item in plan.sequence_items}
    for table in reference.load_order:
        rows = plan.candidates[table]
        columns = reference.tables[table].columns
        if rows:
            statement = f'INSERT INTO {_q(table)} ({", ".join(_q(c) for c in columns)}) ' \
                        f'VALUES ({", ".join("?" for _ in columns)})'
            for _key, _line, row in rows:
                ctx.wrote = True
                conn.execute(statement, [row[column] for column in columns])
            ctx.hook("after_insert", table)
        if table in items:
            ctx.wrote = True
            load.apply_sequence(conn, table, ctx.source.sequence(table))
            ctx.hook("after_sequence", table)


def _validate_before_commit(ctx, conn, reference, plan, before, canonical_before, given, resolved) -> None:
    def fail(code="write_failed"):
        return Stop(STATUS_NOT_COMMITTED, code, 3)

    if _restore_lock(given, resolved):
        raise fail("restore_lock_present")
    if not all(pin.unchanged() for pin in ctx.pinned):
        raise fail("pinned_copy_changed")
    after = read_canonical(conn, reference, lambda *_args: None)
    for table in reference.load_order:
        expected = dict(canonical_before[table])
        expected.update({key: line for key, line, _row in plan.candidates[table]})
        if after[table].lines != expected:
            raise fail()
    sequences = read_sequences(conn)
    expected_sequences = {name: seq for name, seq in plan.pre_sequences.items()}
    for item in plan.sequence_items:
        expected_sequences[item["table"]] = item["expected"]
    if sequences != expected_sequences:
        raise fail()
    snapshot = preservation_snapshot(conn)
    if snapshot != before:
        ctx.preservation_equal = False
        raise fail()
    if conn.execute("PRAGMA foreign_key_check").fetchall() or conn.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
        raise fail()
    try:
        if parity.embedded_reference_problems(conn):
            raise fail()
    except (TypeError, ValueError) as exc:
        raise fail() from exc


def _commit(ctx: Context, conn, plan: Plan, resolved: str, uri: str):
    try:
        conn.execute("COMMIT")
    except Exception as exc:
        return _commit_failed(ctx, conn, plan, exc, resolved, uri)
    return STATUS_APPLIED, None, 0


def _footprint_state(probe, reference_tables, plan: Plan) -> str:
    """'post', 'pre' or 'other' for the planned write footprint as seen by ``probe``."""
    post = pre = True
    for table, rows in plan.candidates.items():
        tref = reference_tables[table]
        columns = tref.columns
        select = ", ".join(_q(c) for c in columns)
        where = " AND ".join(f"{_q(c)} = ?" for c in tref.pk)
        for key, line, _row in rows:
            found = probe.execute(f"SELECT {select} FROM {_q(table)} WHERE {where}", list(key)).fetchone()
            if found is None:
                post = False
            else:
                pre = False
                if ct.serialize_row(columns, found) != line:
                    post = False
    if plan.sequence_items:
        sequences = read_sequences(probe)
        for item in plan.sequence_items:
            value = sequences.get(item["table"])
            if value != item["expected"]:
                post = False
            if value != item["pre"]:
                pre = False
    return "post" if post else "pre" if pre else "other"


def _commit_failed(ctx: Context, conn, plan: Plan, exc: Exception, resolved: str, uri: str):
    ctx.commit_error = _error_names(exc)
    busy = _is_busy(exc)
    _rollback(conn)
    still_open = bool(getattr(conn, "in_transaction", False))
    try:
        conn.close()
    except Exception:
        pass
    unknown = Stop(STATUS_UNKNOWN, "commit_outcome_unknown", 5)
    if still_open or os.path.lexists(resolved + "-journal"):
        raise unknown
    try:
        probe = ctx.open_connection(uri)
    except Exception:
        raise unknown
    try:
        probe.execute("PRAGMA query_only=ON")
        probe.set_authorizer(_read_authorizer)
        probe.execute("BEGIN")
        try:
            state = _footprint_state(probe, ctx.reference_tables, plan)
        finally:
            _rollback(probe)
    except Exception:
        raise unknown
    finally:
        try:
            probe.close()
        except Exception:
            pass
    if state == "post":
        return STATUS_APPLIED, "commit_error_committed", 4
    if state == "pre":
        if busy:
            raise Stop(STATUS_NOT_COMMITTED, "commit_error_not_committed", 2)
        raise Stop(STATUS_NOT_COMMITTED, "commit_error_not_committed", 3)
    raise unknown


def main(argv=None) -> int:
    return run(sys.argv[1:] if argv is None else argv, out=sys.stdout, err=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
