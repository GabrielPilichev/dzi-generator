#!/usr/bin/env python3
"""Apply confirmed DZI question image mappings to the existing asset tables."""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

try:
    from .validate_confirmed_dzi_image_files import (
        DEFAULT_IMAGE_DIR,
        ready_mapping_entries,
        validate_image_files,
    )
    from .validate_confirmed_dzi_image_mappings import (
        DEFAULT_DB_PATH,
        DEFAULT_MAPPING_PATH,
        collect_ready_question_ids,
        fetch_question_rows,
        load_mapping,
        readonly_uri,
        validate_entries,
    )
except ImportError:
    from validate_confirmed_dzi_image_files import (
        DEFAULT_IMAGE_DIR,
        ready_mapping_entries,
        validate_image_files,
    )
    from validate_confirmed_dzi_image_mappings import (
        DEFAULT_DB_PATH,
        DEFAULT_MAPPING_PATH,
        collect_ready_question_ids,
        fetch_question_rows,
        load_mapping,
        readonly_uri,
        validate_entries,
    )


DZI_FORMAT_VERSION = "dzi_it_pp_2025_format"
ASSET_TYPE = "pdf_crop"
LINK_ROLE = ASSET_TYPE
SUBJECT = "informatika_it"
LEVEL = "DZI"
SESSION_BY_SLUG_PREFIX = {
    "aug": "august",
    "jun": "june",
    "may": "may",
}

REQUIRED_SCHEMA = {
    "questions": {"id", "source_exam", "source_number", "has_image", "image_path"},
    "exams": {"id", "subject", "level", "year", "session", "variant", "format_version"},
    "exam_tasks": {"id", "exam_id", "task_number", "has_assets"},
    "exam_task_questions": {"task_id", "question_id", "role"},
    "assets": {
        "id",
        "asset_type",
        "original_filename",
        "local_path",
        "sha256",
        "mime_type",
        "file_size",
        "notes",
    },
    "asset_links": {
        "id",
        "asset_id",
        "owner_type",
        "owner_id",
        "role",
        "display_order",
        "caption_bg",
        "source_page",
        "source_bbox_json",
    },
}


@dataclass(frozen=True)
class MappingTarget:
    source_slug: str
    official_question_number: int
    db_question_id: int
    exam_task_id: int
    filename: str
    local_path: str
    file_path: Path
    question_has_image: int
    exam_task_has_assets: int

    @property
    def label(self) -> str:
        return f"{self.source_slug} Q{self.official_question_number} question_id={self.db_question_id}"


@dataclass(frozen=True)
class RowAction:
    table: str
    row_id: int
    action: str
    details: str = ""

    def format(self) -> str:
        suffix = f" {self.details}" if self.details else ""
        return f"{self.table} id={self.row_id} {self.action}{suffix}"


@dataclass(frozen=True)
class TargetCollection:
    targets: list[MappingTarget]
    skipped_missing: list[str]
    errors: list[str]


@dataclass(frozen=True)
class CheckResult:
    errors: list[str]
    linked: list[str]
    unlinked_available: list[str]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Link confirmed DZI question images to DB questions and exam tasks."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--mapping", type=Path, default=DEFAULT_MAPPING_PATH)
    parser.add_argument("--image-dir", type=Path, default=DEFAULT_IMAGE_DIR)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify that every ready mapping is already linked; do not write the DB.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Fail if any expected image file is missing instead of skipping missing mappings.",
    )
    return parser.parse_args(argv)


def writable_uri(db_path: Path) -> str:
    return f"file:{quote(str(db_path), safe='/')}?mode=rw"


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(readonly_uri(db_path), uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def connect_writable(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(writable_uri(db_path), uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row["name"]) for row in rows}


def validate_schema(conn: sqlite3.Connection) -> list[str]:
    errors = []
    for table, required_columns in sorted(REQUIRED_SCHEMA.items()):
        columns = table_columns(conn, table)
        if not columns:
            errors.append(f"missing table {table}; a migration is required before linking")
            continue
        missing_columns = sorted(required_columns - columns)
        if missing_columns:
            joined = ", ".join(missing_columns)
            errors.append(f"table {table} is missing columns: {joined}; a migration is required before linking")
    return errors


def parse_source_slug(source_slug: str) -> tuple[int, str, int] | None:
    parts = source_slug.split("_")
    if len(parts) != 3:
        return None
    session = SESSION_BY_SLUG_PREFIX.get(parts[0])
    if session is None or not parts[2].startswith("v"):
        return None
    try:
        year = int(parts[1])
        variant = int(parts[2][1:])
    except ValueError:
        return None
    return year, session, variant


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_metadata(path: Path) -> dict[str, object]:
    mime_type, _ = mimetypes.guess_type(path.as_posix())
    return {
        "sha256": sha256_file(path),
        "mime_type": mime_type or "image/png",
        "file_size": path.stat().st_size,
    }


def run_preflight(
    db_path: Path,
    mapping_path: Path,
    image_dir: Path,
    *,
    strict: bool,
) -> tuple[dict[str, object] | None, list[str]]:
    errors: list[str] = []
    payload: dict[str, object] | None = None

    try:
        payload = load_mapping(mapping_path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return None, [f"mapping load failed: {exc}"]

    if not db_path.exists():
        errors.append(f"DB not found: {db_path}")
    else:
        try:
            with closing(connect_readonly(db_path)) as conn:
                errors.extend(validate_schema(conn))
                question_rows = fetch_question_rows(conn, collect_ready_question_ids(payload))
                mapping_errors, _ = validate_entries(payload, question_rows)
                errors.extend(mapping_errors)
        except sqlite3.Error as exc:
            errors.append(f"DB validation failed: {exc}")

    try:
        asset_errors, _, _, _ = validate_image_files(payload, image_dir, require_files=strict)
        errors.extend(asset_errors)
    except (OSError, ValueError) as exc:
        errors.append(f"asset file validation failed: {exc}")

    return payload, errors


def collect_targets(
    conn: sqlite3.Connection,
    payload: dict[str, object],
    image_dir: Path,
) -> TargetCollection:
    targets: list[MappingTarget] = []
    skipped_missing: list[str] = []
    errors: list[str] = []

    for entry in ready_mapping_entries(payload):
        source_slug = str(entry["source_slug"])
        official_number = int(entry["official_question_number"])
        db_question_id = int(entry["db_question_id"])
        filename = str(entry["expected_asset_filename"])
        label = f"{source_slug} Q{official_number} question_id={db_question_id}"

        question = conn.execute(
            """
            SELECT id, source_exam, source_number, has_image, image_path
            FROM questions
            WHERE id = ?
            """,
            (db_question_id,),
        ).fetchone()
        if question is None:
            errors.append(f"{label}: question row is missing")
            continue
        if question["source_exam"] != source_slug or int(question["source_number"] or -1) != official_number:
            errors.append(
                f"{label}: question points to {question['source_exam']} Q{question['source_number']}"
            )
            continue

        parsed = parse_source_slug(source_slug)
        if parsed is None:
            errors.append(f"{label}: cannot parse source_slug")
            continue
        year, session, variant = parsed

        exam_rows = conn.execute(
            """
            SELECT id
            FROM exams
            WHERE subject = ?
              AND level = ?
              AND format_version = ?
              AND year = ?
              AND session = ?
              AND variant = ?
            ORDER BY id
            """,
            (SUBJECT, LEVEL, DZI_FORMAT_VERSION, year, session, variant),
        ).fetchall()
        if len(exam_rows) != 1:
            errors.append(f"{label}: expected 1 exam row, found {len(exam_rows)}")
            continue
        exam_id = int(exam_rows[0]["id"])

        task_rows = conn.execute(
            """
            SELECT id, has_assets
            FROM exam_tasks
            WHERE exam_id = ?
              AND task_number = ?
            ORDER BY id
            """,
            (exam_id, official_number),
        ).fetchall()
        if len(task_rows) != 1:
            errors.append(f"{label}: expected 1 exam_tasks row, found {len(task_rows)}")
            continue
        task = task_rows[0]
        exam_task_id = int(task["id"])

        task_question = conn.execute(
            """
            SELECT 1
            FROM exam_task_questions
            WHERE task_id = ?
              AND question_id = ?
              AND role = 'primary'
            """,
            (exam_task_id, db_question_id),
        ).fetchone()
        if task_question is None:
            errors.append(f"{label}: exam_task_questions primary link does not match mapped question")
            continue

        file_path = image_dir / filename
        if not file_path.exists() or not file_path.is_file():
            skipped_missing.append(f"{label}: expected image file is missing: {file_path}")
            continue

        targets.append(
            MappingTarget(
                source_slug=source_slug,
                official_question_number=official_number,
                db_question_id=db_question_id,
                exam_task_id=exam_task_id,
                filename=filename,
                local_path=file_path.as_posix(),
                file_path=file_path,
                question_has_image=int(question["has_image"] or 0),
                exam_task_has_assets=int(task["has_assets"] or 0),
            )
        )

    return TargetCollection(targets=targets, skipped_missing=skipped_missing, errors=errors)


def upsert_asset(conn: sqlite3.Connection, target: MappingTarget) -> tuple[int, RowAction]:
    metadata = file_metadata(target.file_path)
    desired = {
        "asset_type": ASSET_TYPE,
        "original_filename": target.filename,
        "sha256": metadata["sha256"],
        "mime_type": metadata["mime_type"],
        "file_size": metadata["file_size"],
        "notes": (
            "confirmed DZI official PDF crop "
            f"for {target.source_slug} Q{target.official_question_number}"
        ),
    }

    existing = conn.execute(
        """
        SELECT id, asset_type, original_filename, sha256, mime_type, file_size, notes
        FROM assets
        WHERE local_path = ?
        """,
        (target.local_path,),
    ).fetchone()
    if existing is None:
        cur = conn.execute(
            """
            INSERT INTO assets (
                asset_type, original_filename, local_path, sha256, mime_type, file_size, notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                desired["asset_type"],
                desired["original_filename"],
                target.local_path,
                desired["sha256"],
                desired["mime_type"],
                desired["file_size"],
                desired["notes"],
            ),
        )
        asset_id = int(cur.lastrowid)
        return asset_id, RowAction("assets", asset_id, "inserted", f"local_path={target.local_path}")

    changed_columns = [
        column
        for column, value in desired.items()
        if existing[column] != value
    ]
    asset_id = int(existing["id"])
    if changed_columns:
        conn.execute(
            """
            UPDATE assets
            SET asset_type = ?,
                original_filename = ?,
                sha256 = ?,
                mime_type = ?,
                file_size = ?,
                notes = ?
            WHERE id = ?
            """,
            (
                desired["asset_type"],
                desired["original_filename"],
                desired["sha256"],
                desired["mime_type"],
                desired["file_size"],
                desired["notes"],
                asset_id,
            ),
        )
        return asset_id, RowAction("assets", asset_id, "updated", f"columns={','.join(changed_columns)}")

    return asset_id, RowAction("assets", asset_id, "unchanged")


def upsert_link(
    conn: sqlite3.Connection,
    *,
    asset_id: int,
    owner_type: str,
    owner_id: int,
    target: MappingTarget,
) -> RowAction:
    desired = {
        "caption_bg": f"DZI {target.source_slug} Q{target.official_question_number}",
        "source_page": None,
        "source_bbox_json": None,
    }
    existing = conn.execute(
        """
        SELECT id, caption_bg, source_page, source_bbox_json
        FROM asset_links
        WHERE asset_id = ?
          AND owner_type = ?
          AND owner_id = ?
          AND role = ?
          AND display_order = 0
        """,
        (asset_id, owner_type, owner_id, LINK_ROLE),
    ).fetchone()
    if existing is None:
        cur = conn.execute(
            """
            INSERT INTO asset_links (
                asset_id, owner_type, owner_id, role, display_order,
                caption_bg, source_page, source_bbox_json
            )
            VALUES (?, ?, ?, ?, 0, ?, ?, ?)
            """,
            (
                asset_id,
                owner_type,
                owner_id,
                LINK_ROLE,
                desired["caption_bg"],
                desired["source_page"],
                desired["source_bbox_json"],
            ),
        )
        row_id = int(cur.lastrowid)
        return RowAction(
            "asset_links",
            row_id,
            "inserted",
            f"asset_id={asset_id} owner_type={owner_type} owner_id={owner_id} role={LINK_ROLE}",
        )

    changed_columns = [
        column
        for column, value in desired.items()
        if existing[column] != value
    ]
    row_id = int(existing["id"])
    if changed_columns:
        conn.execute(
            """
            UPDATE asset_links
            SET caption_bg = ?,
                source_page = ?,
                source_bbox_json = ?
            WHERE id = ?
            """,
            (
                desired["caption_bg"],
                desired["source_page"],
                desired["source_bbox_json"],
                row_id,
            ),
        )
        return RowAction("asset_links", row_id, "updated", f"columns={','.join(changed_columns)}")

    return RowAction("asset_links", row_id, "unchanged")


def ensure_flags(conn: sqlite3.Connection, target: MappingTarget) -> list[RowAction]:
    actions: list[RowAction] = []
    if target.question_has_image != 1:
        conn.execute(
            "UPDATE questions SET has_image = 1 WHERE id = ?",
            (target.db_question_id,),
        )
        actions.append(
            RowAction("questions", target.db_question_id, "updated", "has_image=1")
        )
    else:
        actions.append(RowAction("questions", target.db_question_id, "unchanged", "has_image=1"))

    if target.exam_task_has_assets != 1:
        conn.execute(
            "UPDATE exam_tasks SET has_assets = 1 WHERE id = ?",
            (target.exam_task_id,),
        )
        actions.append(
            RowAction("exam_tasks", target.exam_task_id, "updated", "has_assets=1")
        )
    else:
        actions.append(RowAction("exam_tasks", target.exam_task_id, "unchanged", "has_assets=1"))

    return actions


def apply_targets(conn: sqlite3.Connection, targets: list[MappingTarget]) -> list[tuple[MappingTarget, list[RowAction]]]:
    results: list[tuple[MappingTarget, list[RowAction]]] = []
    for target in targets:
        asset_id, asset_action = upsert_asset(conn, target)
        actions = [
            asset_action,
            upsert_link(
                conn,
                asset_id=asset_id,
                owner_type="question",
                owner_id=target.db_question_id,
                target=target,
            ),
            upsert_link(
                conn,
                asset_id=asset_id,
                owner_type="exam_task",
                owner_id=target.exam_task_id,
                target=target,
            ),
        ]
        actions.extend(ensure_flags(conn, target))
        results.append((target, actions))
    return results


def check_targets(conn: sqlite3.Connection, targets: list[MappingTarget]) -> CheckResult:
    errors: list[str] = []
    linked: list[str] = []
    unlinked_available: list[str] = []
    for target in targets:
        target_error_start = len(errors)
        metadata = file_metadata(target.file_path)
        asset = conn.execute(
            """
            SELECT id, asset_type, sha256, mime_type, file_size
            FROM assets
            WHERE local_path = ?
            """,
            (target.local_path,),
        ).fetchone()
        if asset is None:
            errors.append(f"{target.label}: missing assets row for {target.local_path}")
            unlinked_available.append(f"{target.label} local_path={target.local_path}")
            continue

        asset_id = int(asset["id"])
        if asset["asset_type"] != ASSET_TYPE:
            errors.append(f"{target.label}: asset {asset_id} has asset_type={asset['asset_type']}")
        if asset["sha256"] != metadata["sha256"]:
            errors.append(f"{target.label}: asset {asset_id} sha256 does not match file")
        if asset["mime_type"] != metadata["mime_type"]:
            errors.append(f"{target.label}: asset {asset_id} mime_type does not match file")
        if int(asset["file_size"] or -1) != int(metadata["file_size"]):
            errors.append(f"{target.label}: asset {asset_id} file_size does not match file")

        for owner_type, owner_id in (
            ("question", target.db_question_id),
            ("exam_task", target.exam_task_id),
        ):
            link = conn.execute(
                """
                SELECT id
                FROM asset_links
                WHERE asset_id = ?
                  AND owner_type = ?
                  AND owner_id = ?
                  AND role = ?
                  AND display_order = 0
                """,
                (asset_id, owner_type, owner_id, LINK_ROLE),
            ).fetchone()
            if link is None:
                errors.append(
                    f"{target.label}: missing {owner_type} asset_link for asset_id={asset_id}"
                )

        question = conn.execute(
            "SELECT has_image FROM questions WHERE id = ?",
            (target.db_question_id,),
        ).fetchone()
        if question is None or int(question["has_image"] or 0) != 1:
            errors.append(f"{target.label}: questions.has_image is not 1")

        task = conn.execute(
            "SELECT has_assets FROM exam_tasks WHERE id = ?",
            (target.exam_task_id,),
        ).fetchone()
        if task is None or int(task["has_assets"] or 0) != 1:
            errors.append(f"{target.label}: exam_tasks.has_assets is not 1")

        line = f"{target.label} task_id={target.exam_task_id} asset_id={asset_id} local_path={target.local_path}"
        if len(errors) == target_error_start:
            linked.append(line)
        else:
            unlinked_available.append(line)

    return CheckResult(errors=errors, linked=linked, unlinked_available=unlinked_available)


def print_preflight_errors(errors: list[str]) -> None:
    print("Preflight failed")
    print("================")
    print(f"validation errors: {len(errors)}")
    for error in errors:
        print(f"- {error}")


def print_apply_results(
    results: list[tuple[MappingTarget, list[RowAction]]],
    skipped_missing: list[str],
) -> None:
    print("Confirmed DZI image mappings applied")
    print("====================================")
    print(f"available mappings processed: {len(results)}")
    print(f"missing/skipped count: {len(skipped_missing)}")
    changed = 0
    for target, actions in results:
        print(f"- {target.label} task_id={target.exam_task_id}")
        for action in actions:
            if action.action != "unchanged":
                changed += 1
            print(f"  - {action.format()}")
    if skipped_missing:
        print()
        print("Skipped missing assets")
        print("----------------------")
        for line in skipped_missing:
            print(f"- {line}")
    print(f"rows inserted/updated: {changed}")
    print("apply_exit=0")


def print_check_results(
    result: CheckResult,
    skipped_missing: list[str],
) -> None:
    print("Confirmed DZI image mapping link check")
    print("======================================")
    print(f"ready mappings checked: {len(result.linked) + len(result.unlinked_available) + len(skipped_missing)}")
    print(f"linked count: {len(result.linked)}")
    print(f"missing/skipped count: {len(skipped_missing)}")
    print(f"unlinked available count: {len(result.unlinked_available)}")
    print(f"validation errors: {len(result.errors)}")
    if result.linked:
        print()
        print("Linked mappings")
        print("---------------")
        for line in result.linked:
            print(f"- {line}")
    if skipped_missing:
        print()
        print("Skipped missing assets")
        print("----------------------")
        for line in skipped_missing:
            print(f"- {line}")
    if result.unlinked_available:
        print()
        print("Unlinked available mappings")
        print("---------------------------")
        for line in result.unlinked_available:
            print(f"- {line}")
    if result.errors:
        print()
        print("Validation errors")
        print("-----------------")
        for error in result.errors:
            print(f"- {error}")
    print(f"check_exit={0 if not result.errors else 1}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    payload, preflight_errors = run_preflight(
        args.db,
        args.mapping,
        args.image_dir,
        strict=args.strict,
    )
    if payload is None or preflight_errors:
        print_preflight_errors(preflight_errors)
        return 1

    try:
        if args.check:
            with closing(connect_readonly(args.db)) as conn:
                collection = collect_targets(conn, payload, args.image_dir)
                if collection.errors:
                    print_preflight_errors(collection.errors)
                    return 1
                check_result = check_targets(conn, collection.targets)
                print_check_results(check_result, collection.skipped_missing)
                return 1 if check_result.errors else 0

        with closing(connect_writable(args.db)) as conn:
            collection = collect_targets(conn, payload, args.image_dir)
            if collection.errors:
                print_preflight_errors(collection.errors)
                return 1
            with conn:
                results = apply_targets(conn, collection.targets)
            print_apply_results(results, collection.skipped_missing)
            return 0
    except sqlite3.Error as exc:
        print(f"error: DB operation failed: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: file operation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
