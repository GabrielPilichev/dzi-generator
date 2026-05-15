#!/usr/bin/env python3
"""Import official resource metadata for reviewed DZI practical-task batches."""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.validate_practical_task_batch import (
    ALLOWED_RESOURCE_ROOTS,
    DEFAULT_DB_PATH,
    ZIP_RESOURCE_SEPARATOR,
    load_json,
    resolve_exam,
    resolve_exam_task,
    validate_zip_resource_path,
    validate_batch,
)


@dataclass
class ImportSummary:
    source_slug: str
    exam_id: int
    tasks_read: int
    resources_seen: int
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_target_schema(conn: sqlite3.Connection) -> None:
    row = conn.execute("""
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table'
          AND name = 'practical_task_resources'
    """).fetchone()
    if row is None:
        raise RuntimeError(
            "practical_task_resources table is missing; apply the schema migration first"
        )


def resource_metadata(
    raw_path: str,
    *,
    allowed_roots: tuple[Path, ...] = ALLOWED_RESOURCE_ROOTS,
) -> dict[str, object]:
    if ZIP_RESOURCE_SEPARATOR in raw_path:
        zip_path, member_path = validate_zip_resource_path(raw_path, allowed_roots)
        with zipfile.ZipFile(zip_path) as archive:
            info = archive.getinfo(member_path)
        return {
            "resource_path": raw_path,
            "original_filename": PurePosixPath(member_path).name,
            "label_bg": None,
            "file_size_bytes": int(info.file_size),
            "sha256": None,
        }

    path = Path(raw_path)
    stat = path.stat()
    return {
        "resource_path": raw_path,
        "original_filename": path.name,
        "label_bg": None,
        "file_size_bytes": int(stat.st_size),
        "sha256": sha256_file(path),
    }


def upsert_resource(
    conn: sqlite3.Connection,
    *,
    exam_task_id: int,
    metadata: dict[str, object],
) -> str:
    existing = conn.execute(
        """
        SELECT original_filename, label_bg, file_size_bytes, sha256
        FROM practical_task_resources
        WHERE exam_task_id = ? AND resource_path = ?
        """,
        (exam_task_id, metadata["resource_path"]),
    ).fetchone()

    values = (
        exam_task_id,
        metadata["resource_path"],
        metadata["original_filename"],
        metadata["label_bg"],
        metadata["file_size_bytes"],
        metadata["sha256"],
    )

    if existing is None:
        conn.execute(
            """
            INSERT INTO practical_task_resources (
                exam_task_id, resource_path, original_filename, label_bg,
                file_size_bytes, sha256
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            values,
        )
        return "inserted"

    current = (
        existing["original_filename"],
        existing["label_bg"],
        existing["file_size_bytes"],
        existing["sha256"],
    )
    desired = (
        metadata["original_filename"],
        metadata["label_bg"],
        metadata["file_size_bytes"],
        metadata["sha256"],
    )
    if current == desired:
        return "unchanged"

    conn.execute(
        """
        UPDATE practical_task_resources
        SET original_filename = ?,
            label_bg = ?,
            file_size_bytes = ?,
            sha256 = ?
        WHERE exam_task_id = ? AND resource_path = ?
        """,
        (
            metadata["original_filename"],
            metadata["label_bg"],
            metadata["file_size_bytes"],
            metadata["sha256"],
            exam_task_id,
            metadata["resource_path"],
        ),
    )
    return "updated"


def import_practical_task_resources(
    conn: sqlite3.Connection,
    payload: dict,
    *,
    allowed_roots: tuple[Path, ...] = ALLOWED_RESOURCE_ROOTS,
) -> ImportSummary:
    ensure_target_schema(conn)
    validation = validate_batch(conn, payload, allowed_roots=allowed_roots)
    exam = resolve_exam(conn, validation.source_slug)
    exam_id = int(exam["id"])
    summary = ImportSummary(
        source_slug=validation.source_slug,
        exam_id=exam_id,
        tasks_read=validation.tasks_read,
        resources_seen=validation.resource_files_checked,
    )

    for task in payload["tasks"]:
        exam_task = resolve_exam_task(conn, exam_id, int(task["task_number"]))
        exam_task_id = int(exam_task["id"])
        for raw_path in task.get("resource_files") or []:
            result = upsert_resource(
                conn,
                exam_task_id=exam_task_id,
                metadata=resource_metadata(raw_path, allowed_roots=allowed_roots),
            )
            if result == "inserted":
                summary.inserted += 1
            elif result == "updated":
                summary.updated += 1
            else:
                summary.unchanged += 1

    return summary


def open_writable_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import practical-task resource metadata from a reviewed DZI batch JSON."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--json", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        payload = load_json(args.json)
        conn = open_writable_db(args.db)
        try:
            summary = import_practical_task_resources(conn, payload)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    except (OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print("practical-task resource import summary:")
    print(f"source_slug: {summary.source_slug}")
    print(f"exam_id: {summary.exam_id}")
    print(f"tasks read: {summary.tasks_read}")
    print(f"resources seen: {summary.resources_seen}")
    print(f"inserted: {summary.inserted}")
    print(f"updated: {summary.updated}")
    print(f"unchanged: {summary.unchanged}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
