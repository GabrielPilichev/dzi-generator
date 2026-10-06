#!/usr/bin/env python3
"""One-time construction of the initial canonical dataset (G1).

    python3 -m src.canonical_reconcile --identity-manifest PATH --out DIR

Sources and their authority (never mixed):

* legacy content (832 questions, IT curriculum, DZI): the tracked seed
  ``data/questions.db`` provides values AND ids, copied unchanged;
* Physics/Biology content: the tracked reviewed structures and batches provide
  the VALUES (run through the real importers on a scratch copy of the seed);
  a read-only local identity export provides the IDS. Rows are matched by
  natural key, never by position, and any missing, duplicate, unexpected or
  conflicting match stops the build - no id is ever guessed;
* link-table ids the identity export does not carry (topic_section_assignments,
  question_topic_assignments) have no external identity (no foreign key or stored
  reference points at them), so they are assigned deterministically above the
  seed's sequence in parent-id order, which is what a sequential import produced.

The seed is read through an immutable read-only snapshot and byte copies only.
Runtime and operational tables are never read into the dataset. The output
directory must be empty; replacing tracked data goes through
``canonical_export --replace-canonical`` instead.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

try:
    from src import canonical_export as export
    from src import canonical_load as load
    from src import canonical_tables as ct
    from src import import_reviewed_subject_questions as reviewed_import
    from src import import_subject_structure as structure_import
except ModuleNotFoundError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from src import canonical_export as export
    from src import canonical_load as load
    from src import canonical_tables as ct
    from src import import_reviewed_subject_questions as reviewed_import
    from src import import_subject_structure as structure_import

SEED_SHA256 = "47dc58348b08b59ce2c4e1081fcd6d99cdda1b6003eddfbd81fd4f65d20cb91e"
LOCAL_DB_SHA256 = "fb7748a29e4608fb029d0c9d955ff697c9b8f2b3a1918668878825e982e40ad5"
STRUCTURE_GLOB = "data/subject_structures/*/*.json"
BATCH_GLOB = "data/import_batches/subjects/*/*/*.json"

# Tables whose ids the identity export carries, and how a row is matched.
IDENTITY_KEYS = {
    "curricula": ("subject", "grade", "education_type"),
    "curriculum_sections": ("section_slug",),
    "curriculum_topics": ("topic_slug",),
    "questions": ("source_exam", "source_number"),
}
# Content fields the identity export repeats; they must agree with the tracked values.
IDENTITY_VALUE_CHECKS = {"questions": ("question_type", "prompt")}
# Surrogate ids with no external identity, assigned in parent-id order above the seed sequence.
DERIVED_ID_TABLES = {
    "topic_section_assignments": "topic_id",
    "question_topic_assignments": "question_id",
}
# Tables the Physics/Biology importers create rows in; anything else is a surprise.
EXPECTED_NEW_TABLES = {
    "curricula", "curriculum_sections", "curriculum_topics", "questions", "multiple_choice_options",
    "topic_classes", *DERIVED_ID_TABLES,
}
# Non-foreign-key id columns that must be remapped with their parent.
EXTRA_REFERENCES = {"questions": {"topic_id": "curriculum_topics"}}


class ReconcileError(RuntimeError):
    """Identity reconciliation failed; nothing was written."""


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _key(row: dict, fields: tuple[str, ...]) -> tuple:
    return tuple(row[field] for field in fields)


def _unique_index(rows: list[dict], fields: tuple[str, ...], label: str) -> dict[tuple, dict]:
    index: dict[tuple, dict] = {}
    for row in rows:
        key = _key(row, fields)
        if key in index:
            raise ReconcileError(f"{label}: duplicate natural key {key}")
        index[key] = row
    return index


def _rows(conn: sqlite3.Connection, table: str) -> list[dict]:
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(f'SELECT * FROM "{table}"')]
    finally:
        conn.row_factory = None


# ----------------------------------------------------------------------------- scratch import

def scratch_import(seed_path: Path, scratch_path: Path, root: Path) -> None:
    """Apply the tracked structures then reviewed batches to a byte copy of the seed."""
    shutil.copyfile(seed_path, scratch_path)
    for path in sorted(root.glob(STRUCTURE_GLOB)):
        structure_import.import_structure(scratch_path, json.loads(path.read_text(encoding="utf-8")), apply=True)
    for path in sorted(root.glob(BATCH_GLOB)):
        if path.name.endswith(".provenance.json"):
            continue
        reviewed_import.import_batch(scratch_path, json.loads(path.read_text(encoding="utf-8")), apply=True)


# ----------------------------------------------------------------------------- id reconciliation

def build_id_maps(identities: dict, seed: dict[str, list[dict]], scratch: dict[str, list[dict]],
                  seed_sequences: dict[str, int]) -> dict[str, dict[int, int]]:
    """scratch id -> authoritative id, for the identity tables (options keyed after questions).

    ``identities`` has the shape of the local identity export; ``seed``/``scratch`` map table
    to rows. Raises ReconcileError on any missing, duplicate, conflicting or unexpected match.
    """
    maps: dict[str, dict[int, int]] = {}
    seed_ids = {table: {row["id"] for row in rows} for table, rows in seed.items()
                if rows and "id" in rows[0]}

    def remap(table: str, scratch_id: int) -> int:
        if scratch_id in seed_ids[table]:
            return scratch_id
        return maps[table][scratch_id]

    for table in ("curricula", "curriculum_sections", "curriculum_topics", "questions"):
        fields = IDENTITY_KEYS[table]
        manifest_rows = identities[table]
        if len({row["id"] for row in manifest_rows}) != len(manifest_rows):
            raise ReconcileError(f"{table}: duplicate id in the identity export")
        legacy = {row["id"]: row for row in seed[table]}
        for row in manifest_rows:
            if row["id"] in legacy:
                stored = legacy[row["id"]]
                if any(stored[f] != row[f] for f in (*fields, *IDENTITY_VALUE_CHECKS.get(table, ()))
                       if f in row):
                    raise ReconcileError(f"{table}: id {row['id']} conflicts with the seed row")
        manifest_new = [row for row in manifest_rows if row["id"] not in legacy]
        scratch_new = [row for row in scratch[table] if row["id"] not in legacy]
        # Natural key: questions without a source number cannot be new reviewed rows.
        by_manifest = _unique_index(manifest_new, fields, f"{table} (identity export)")
        by_scratch = _unique_index(scratch_new, fields, f"{table} (tracked inputs)")
        missing = sorted(set(by_scratch) - set(by_manifest))
        unexpected = sorted(set(by_manifest) - set(by_scratch))
        if missing or unexpected:
            raise ReconcileError(
                f"{table}: {len(missing)} tracked rows have no identity match (first {missing[:2]}); "
                f"{len(unexpected)} identity rows have no tracked source (first {unexpected[:2]})")
        for new_id in (row["id"] for row in manifest_new):
            if new_id <= seed_sequences.get(table, 0):
                raise ReconcileError(f"{table}: id {new_id} would recycle a historical id (seed sequence "
                                     f"{seed_sequences.get(table)})")
        maps[table] = {by_scratch[key]["id"]: by_manifest[key]["id"] for key in by_scratch}
        for key, scratch_row in by_scratch.items():
            for field in IDENTITY_VALUE_CHECKS.get(table, ()):
                if scratch_row[field] != by_manifest[key][field]:
                    raise ReconcileError(f"{table}: {key} {field} conflicts with the identity export")
        if table == "curriculum_sections":
            for key, scratch_row in by_scratch.items():
                if by_manifest[key]["curriculum_id"] != remap("curricula", scratch_row["curriculum_id"]):
                    raise ReconcileError(f"section {key}: curriculum conflicts with the identity export")
        if table == "curriculum_topics":
            for key, scratch_row in by_scratch.items():
                if by_manifest[key]["section_id"] != remap("curriculum_sections", scratch_row["section_id"]):
                    raise ReconcileError(f"topic {key}: section conflicts with the identity export")

    # Options: matched by (question, letter); the identity export carries their ids.
    manifest_options = identities["multiple_choice_options"]
    legacy_options = {row["id"]: row for row in seed["multiple_choice_options"]}
    manifest_new = {}
    for row in manifest_options:
        if row["id"] in legacy_options:
            stored = legacy_options[row["id"]]
            if (stored["question_id"], stored["option_letter"]) != (row["question_id"], row["option_letter"]):
                raise ReconcileError(f"multiple_choice_options: id {row['id']} conflicts with the seed row")
            continue
        key = (row["question_id"], row["option_letter"])
        if key in manifest_new:
            raise ReconcileError(f"multiple_choice_options: duplicate identity {key}")
        if row["id"] <= seed_sequences.get("multiple_choice_options", 0):
            raise ReconcileError(f"multiple_choice_options: id {row['id']} would recycle a historical id")
        manifest_new[key] = row["id"]
    scratch_new = {}
    for row in scratch["multiple_choice_options"]:
        if row["id"] in legacy_options:
            continue
        key = (remap("questions", row["question_id"]), row["option_letter"])
        if key in scratch_new:
            raise ReconcileError(f"multiple_choice_options: duplicate tracked option {key}")
        scratch_new[key] = row["id"]
    if set(manifest_new) != set(scratch_new):
        raise ReconcileError(
            f"multiple_choice_options: {len(set(scratch_new) - set(manifest_new))} tracked options lack an identity; "
            f"{len(set(manifest_new) - set(scratch_new))} identity options lack a tracked source")
    maps["multiple_choice_options"] = {scratch_new[key]: manifest_new[key] for key in scratch_new}
    return maps


def assign_derived_ids(table: str, parent_column: str, new_rows: list[dict], seed_sequence: int) -> None:
    """Give new link rows consecutive ids above the seed sequence, in (parent id, row) order."""
    ordered = sorted(new_rows, key=lambda row: (row[parent_column], row["topic_id"] if row["topic_id"] is not None else 0,
                                               row["section_id"] if row["section_id"] is not None else 0))
    for offset, row in enumerate(ordered, 1):
        row["id"] = seed_sequence + offset


def _fk_columns(conn: sqlite3.Connection, table: str) -> dict[str, str]:
    columns = {row[3]: row[2] for row in conn.execute(f'PRAGMA foreign_key_list("{table}")')}
    columns.update(EXTRA_REFERENCES.get(table, {}))
    return columns


def _null_database_timestamps(conn: sqlite3.Connection, table: str, row: dict) -> None:
    for _cid, name, _type, _notnull, default, _pk in conn.execute(f'PRAGMA table_info("{table}")'):
        if default and default.upper() == "CURRENT_TIMESTAMP":
            row[name] = None


def reconcile(seed_path: Path, identity_path: Path, out_dir: Path, root: Path = ct.PROJECT_ROOT) -> dict:
    seed_path, out_dir = Path(seed_path), Path(out_dir)
    if sha256_file(seed_path) != SEED_SHA256:
        raise ReconcileError("the tracked seed differs from the audited seed; stop and re-audit")
    identity = json.loads(Path(identity_path).read_text(encoding="utf-8"))
    if identity.get("database_sha256") != LOCAL_DB_SHA256:
        raise ReconcileError("the identity export does not come from the audited local database")
    identities = identity["identities"]
    if out_dir.exists() and any(out_dir.iterdir()):
        raise ReconcileError(f"{out_dir} is not empty")

    with tempfile.TemporaryDirectory(prefix="learnpilot-reconcile-") as directory:
        work = Path(directory)
        scratch_path, staging_path = work / "scratch.db", work / "staging.db"
        scratch_import(seed_path, scratch_path, root)
        with closing(export.open_snapshot(seed_path)) as seed_conn, \
                closing(sqlite3.connect(scratch_path)) as scratch_conn:
            canonical = ct.tables_of(ct.CANONICAL)
            seed = {table: _rows(seed_conn, table) for table in canonical}
            scratch = {table: _rows(scratch_conn, table) for table in canonical}
            seed_sequences = {t: ct.stored_sequence(seed_conn, t) or 0 for t in canonical}
            runtime_in_seed = {t: seed_conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                               for t in ct.tables_of(ct.RUNTIME) + ct.tables_of(ct.OPERATIONAL)
                               if t != "sqlite_sequence"}
            keys = {t: ct.primary_key_columns(seed_conn, t) for t in canonical}
            seed_keys = {t: {tuple(r[k] for k in keys[t]) for r in seed[t]} for t in canonical}
            by_key = {t: {tuple(r[k] for k in keys[t]): r for r in seed[t]} for t in canonical}

            # The importers must have added rows only; legacy rows are untouched.
            new_rows = {t: [r for r in scratch[t] if tuple(r[k] for k in keys[t]) not in seed_keys[t]]
                        for t in canonical}
            for table in canonical:
                for row in scratch[table]:
                    key = tuple(row[k] for k in keys[table])
                    if key in seed_keys[table] and row != by_key[table][key]:
                        raise ReconcileError(f"{table}: the importers changed legacy row {key}")
                if len(scratch[table]) - len(new_rows[table]) != len(seed[table]):
                    raise ReconcileError(f"{table}: the importers removed legacy rows")
            surprise = {t for t in canonical if new_rows[t]} - EXPECTED_NEW_TABLES
            if surprise:
                raise ReconcileError(f"unexpected new rows in {sorted(surprise)}")

            maps = build_id_maps(identities, seed, scratch, seed_sequences)

            for table, parent in DERIVED_ID_TABLES.items():
                referenced = [t for t in ct.application_tables(scratch_conn) if t != "sqlite_sequence"
                              for r in scratch_conn.execute(f'PRAGMA foreign_key_list("{t}")') if r[2] == table]
                if referenced:
                    raise ReconcileError(f"{table} ids are referenced by foreign keys; they are not derivable")

            seed_id_sets = {t: {r["id"] for r in seed[t]} for t in canonical if keys[t] == ["id"]}
            final: dict[str, list[dict]] = {}
            for table in canonical:
                fks = _fk_columns(scratch_conn, table)
                converted = []
                for row in new_rows[table]:
                    row = dict(row)
                    for column, parent in fks.items():
                        if row[column] is not None:
                            row[column] = (row[column] if row[column] in seed_id_sets[parent]
                                           else maps[parent][row[column]])
                    if table in maps:
                        row["id"] = maps[table][row["id"]]
                    _null_database_timestamps(scratch_conn, table, row)
                    converted.append(row)
                if table in DERIVED_ID_TABLES:
                    assign_derived_ids(table, DERIVED_ID_TABLES[table], converted, seed_sequences[table])
                final[table] = seed[table] + converted
            # Identity tables must now equal the identity export exactly.
            for table in ("curricula", "curriculum_sections", "curriculum_topics", "questions",
                          "multiple_choice_options"):
                if {row["id"] for row in final[table]} != {row["id"] for row in identities[table]}:
                    raise ReconcileError(f"{table}: final ids differ from the identity export")

            load.migrated_empty_database(staging_path)
            with closing(sqlite3.connect(staging_path, isolation_level=None)) as staging:
                staging.execute("PRAGMA foreign_keys = ON")
                staging.execute("BEGIN")
                for table in ct.canonical_load_order(staging):
                    columns = ct.table_columns(staging, table)
                    rows = sorted(final[table], key=lambda r: tuple(r[k] for k in ct.primary_key_columns(staging, table)))
                    load.insert_rows(staging, table, columns, [tuple(r[c] for c in columns) for r in rows])
                    if ct.is_autoincrement(staging, table):
                        highest = max([r["id"] for r in rows], default=0)
                        load.apply_sequence(staging, table, max(seed_sequences[table], highest) or None)
                if staging.execute("PRAGMA foreign_key_check").fetchall():
                    raise ReconcileError("foreign key violations in the reconciled dataset")
                staging.execute("COMMIT")
            sources = _sources(root, seed, new_rows, final, identity, runtime_in_seed, seed_id_sets)
            with closing(sqlite3.connect(staging_path)) as staging:
                return export.export_database(staging, out_dir, sources=sources)


def _sources(root: Path, seed, new_rows, final, identity, runtime_in_seed, seed_id_sets) -> dict:
    def listing(pattern):
        return [{"path": str(path.relative_to(root)), "sha256": sha256_file(path)}
                for path in sorted(root.glob(pattern)) if not path.name.endswith(".provenance.json")]
    new_ids = {}
    for table in sorted(EXPECTED_NEW_TABLES - {"topic_classes"}):
        ids = [r["id"] for r in final[table] if r["id"] not in seed_id_sets[table]]
        if ids:
            new_ids[table] = [min(ids), max(ids)]
    return {
        "note": "G1 one-time reconciliation; see docs/CANONICAL_DATA.md",
        "legacy_seed": {
            "authority": "values and ids of all legacy rows",
            "path": "data/questions.db",
            "sha256": SEED_SHA256,
            "rows": {table: len(rows) for table, rows in sorted(seed.items())},
        },
        "subject_content": {
            "authority": "values of Physics/Biology rows",
            "importers": ["src/import_subject_structure.py", "src/import_reviewed_subject_questions.py"],
            "structures": listing(STRUCTURE_GLOB),
            "batches": listing(BATCH_GLOB),
            "rows_added": {table: len(rows) for table, rows in sorted(new_rows.items()) if rows},
        },
        "local_identity": {
            "authority": "ids of Physics/Biology curricula, sections, topics, questions and options",
            "source_database_sha256": identity["database_sha256"],
            "identity_export_sha256_note": "the raw export is not tracked; these canonical files are its durable form",
            "id_ranges": new_ids,
            "counts": identity["table_counts"],
        },
        "derived_ids": {
            "tables": sorted(DERIVED_ID_TABLES),
            "rule": "ids without external identity, assigned above the seed sequence in ascending "
                    "(parent question/topic id, topic id, section id) order",
        },
        "intentional_differences_from_local_database": [
            "created_at is NULL on rows added from tracked inputs (curriculum_topics, questions, "
            "topic_section_assignments, question_topic_assignments); the local timestamps are not tracked",
            "topic_section_assignments and question_topic_assignments ids for those rows are derived, not copied",
            "runtime and operational tables are excluded by classification",
        ],
        "excluded_seed_rows": runtime_in_seed,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--identity-manifest", type=Path, required=True)
    parser.add_argument("--seed", type=Path, default=ct.PROJECT_ROOT / "data" / "questions.db")
    parser.add_argument("--out", type=Path, required=True, help="empty directory for the canonical files")
    args = parser.parse_args(argv)
    try:
        manifest = reconcile(args.seed, args.identity_manifest, args.out)
    except (ReconcileError, ct.CanonicalError, structure_import.StructureImportError,
            ValueError, sqlite3.Error, OSError) as exc:
        print(f"Reconciliation stopped: {exc}", file=sys.stderr)
        return 1
    print("Canonical dataset written: " + ", ".join(
        f"{t}={manifest['tables'][t]['rows']}" for t in ("curricula", "curriculum_sections", "curriculum_topics",
                                                         "questions", "multiple_choice_options")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
