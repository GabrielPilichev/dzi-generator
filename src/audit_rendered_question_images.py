#!/usr/bin/env python3
"""Read-only audit for linked and rendered DZI question images."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path, PurePosixPath

from jinja2 import Environment, FileSystemLoader, select_autoescape

try:
    from .apply_confirmed_dzi_image_mappings import (
        DZI_FORMAT_VERSION,
        LEVEL,
        SUBJECT,
        connect_readonly,
        parse_source_slug,
        validate_schema,
    )
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
        validate_entries,
    )
except ImportError:
    from apply_confirmed_dzi_image_mappings import (
        DZI_FORMAT_VERSION,
        LEVEL,
        SUBJECT,
        connect_readonly,
        parse_source_slug,
        validate_schema,
    )
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
        validate_entries,
    )


DEFAULT_TEMPLATES_DIR = Path("web/templates")
VISUAL_ASSET_TYPES = ("image", "pdf_crop")
STATIC_PREFIX = "web/static/"
QUESTION_IMAGE_PREFIX = "web/static/img/questions/dzi/"
SAFE_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit confirmed DZI question image links and renderable markup without DB writes."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--mapping", type=Path, default=DEFAULT_MAPPING_PATH)
    parser.add_argument("--image-dir", type=Path, default=DEFAULT_IMAGE_DIR)
    parser.add_argument("--templates-dir", type=Path, default=DEFAULT_TEMPLATES_DIR)
    return parser.parse_args()


def safe_static_filename(local_path: object) -> tuple[str | None, list[str]]:
    raw_path = str(local_path or "").strip().replace("\\", "/")
    errors: list[str] = []
    if not raw_path:
        return None, ["local_path is empty"]
    if raw_path.startswith(("/", "~")):
        errors.append("local_path must be relative")
    if "://" in raw_path:
        errors.append("local_path must not be a URL")
    if not raw_path.startswith(QUESTION_IMAGE_PREFIX):
        errors.append(f"local_path must start with {QUESTION_IMAGE_PREFIX}")

    path = PurePosixPath(raw_path)
    if path.is_absolute():
        errors.append("local_path is absolute")
    if any(part in {"", ".", ".."} for part in path.parts):
        errors.append("local_path contains an empty/current/parent path segment")
    if path.suffix.lower() not in SAFE_IMAGE_SUFFIXES:
        errors.append("local_path must use a safe image extension")

    if errors:
        return None, errors
    return raw_path[len(STATIC_PREFIX):], []


def fetch_exactly_one(
    conn: sqlite3.Connection,
    query: str,
    params: tuple[object, ...],
) -> tuple[sqlite3.Row | None, int]:
    rows = conn.execute(query, params).fetchall()
    return (rows[0], len(rows)) if len(rows) == 1 else (None, len(rows))


def resolve_mapping_context(
    conn: sqlite3.Connection,
    entry: dict[str, object],
) -> tuple[dict[str, int] | None, list[str]]:
    source_slug = str(entry["source_slug"])
    official_number = int(entry["official_question_number"])
    db_question_id = int(entry["db_question_id"])
    label = f"{source_slug} Q{official_number} question_id={db_question_id}"
    errors: list[str] = []

    question, question_count = fetch_exactly_one(
        conn,
        """
        SELECT id, source_exam, source_number
        FROM questions
        WHERE id = ?
        """,
        (db_question_id,),
    )
    if question is None:
        errors.append(f"{label}: expected 1 questions row, found {question_count}")
        return None, errors
    if question["source_exam"] != source_slug or int(question["source_number"] or -1) != official_number:
        errors.append(f"{label}: question points to {question['source_exam']} Q{question['source_number']}")

    parsed = parse_source_slug(source_slug)
    if parsed is None:
        errors.append(f"{label}: cannot parse source_slug")
        return None, errors
    year, session, variant = parsed

    exam, exam_count = fetch_exactly_one(
        conn,
        """
        SELECT id
        FROM exams
        WHERE subject = ?
          AND level = ?
          AND format_version = ?
          AND year = ?
          AND session = ?
          AND variant = ?
        """,
        (SUBJECT, LEVEL, DZI_FORMAT_VERSION, year, session, variant),
    )
    if exam is None:
        errors.append(f"{label}: expected 1 exams row, found {exam_count}")
        return None, errors

    task, task_count = fetch_exactly_one(
        conn,
        """
        SELECT id
        FROM exam_tasks
        WHERE exam_id = ?
          AND task_number = ?
        """,
        (int(exam["id"]), official_number),
    )
    if task is None:
        errors.append(f"{label}: expected 1 exam_tasks row, found {task_count}")
        return None, errors

    primary_link = conn.execute(
        """
        SELECT 1
        FROM exam_task_questions
        WHERE task_id = ?
          AND question_id = ?
          AND role = 'primary'
        """,
        (int(task["id"]), db_question_id),
    ).fetchone()
    if primary_link is None:
        errors.append(f"{label}: exam_task_questions primary link does not match mapped question")

    return {
        "question_id": db_question_id,
        "exam_task_id": int(task["id"]),
    }, errors


def audit_mapping_links(
    conn: sqlite3.Connection,
    payload: dict[str, object],
    image_dir: Path,
) -> tuple[list[str], list[dict[str, object]], list[str]]:
    errors: list[str] = []
    linked: list[dict[str, object]] = []
    skipped_missing: list[str] = []

    for entry in ready_mapping_entries(payload):
        source_slug = str(entry["source_slug"])
        official_number = int(entry["official_question_number"])
        db_question_id = int(entry["db_question_id"])
        filename = str(entry["expected_asset_filename"])
        label = f"{source_slug} Q{official_number} question_id={db_question_id}"
        expected_local_path = (image_dir / filename).as_posix()

        context, context_errors = resolve_mapping_context(conn, entry)
        errors.extend(context_errors)
        if context is None:
            continue

        static_filename, path_errors = safe_static_filename(expected_local_path)
        for path_error in path_errors:
            errors.append(f"{label}: expected local_path {path_error}: {expected_local_path}")

        file_path = Path(expected_local_path)
        file_exists = file_path.exists()
        if file_exists and not file_path.is_file():
            errors.append(f"{label}: linked image path is not a file: {expected_local_path}")

        asset_rows = conn.execute(
            """
            SELECT id, asset_type, local_path
            FROM assets
            WHERE local_path = ?
              AND asset_type IN (?, ?)
            ORDER BY id
            """,
            (expected_local_path, *VISUAL_ASSET_TYPES),
        ).fetchall()

        if not file_exists:
            if asset_rows:
                errors.append(
                    f"{label}: assets row exists for missing image file: {expected_local_path}"
                )
            stale_question = conn.execute(
                """
                SELECT has_image, image_path
                FROM questions
                WHERE id = ?
                """,
                (int(context["question_id"]),),
            ).fetchone()
            if stale_question is not None and int(stale_question["has_image"] or 0) == 1:
                errors.append(f"{label}: questions.has_image is 1 for missing image file")
            if stale_question is not None and stale_question["image_path"] == expected_local_path:
                errors.append(f"{label}: questions.image_path points to missing image file")
            skipped_missing.append(f"{label}: expected image file is missing: {expected_local_path}")
            continue

        if len(asset_rows) != 1:
            errors.append(f"{label}: expected 1 visual assets row for {expected_local_path}, found {len(asset_rows)}")
            continue

        asset = asset_rows[0]
        asset_id = int(asset["id"])
        for owner_type, owner_id in (
            ("question", int(context["question_id"])),
            ("exam_task", int(context["exam_task_id"])),
        ):
            link = conn.execute(
                """
                SELECT id
                FROM asset_links
                WHERE asset_id = ?
                  AND owner_type = ?
                  AND owner_id = ?
                  AND role IN (?, ?)
                """,
                (asset_id, owner_type, owner_id, *VISUAL_ASSET_TYPES),
            ).fetchone()
            if link is None:
                errors.append(f"{label}: missing {owner_type} asset_link for asset_id={asset_id}")

        if static_filename is not None:
            linked.append({
                "source_slug": source_slug,
                "official_question_number": official_number,
                "question_id": int(context["question_id"]),
                "asset_id": asset_id,
                "local_path": expected_local_path,
                "static_filename": static_filename,
                "alt_text": f"Изображение към {source_slug} задача {official_number}",
                "caption": f"DZI {source_slug} Q{official_number}",
            })

    return errors, linked, skipped_missing


def audit_all_db_static_links(conn: sqlite3.Connection) -> tuple[list[str], int]:
    rows = conn.execute(
        """
        SELECT
            a.id AS asset_id,
            a.asset_type,
            a.local_path,
            al.owner_type,
            al.owner_id
        FROM assets a
        JOIN asset_links al ON al.asset_id = a.id
        WHERE a.asset_type IN (?, ?)
          AND a.local_path LIKE 'web/static/img/questions/dzi/%'
        ORDER BY a.id, al.owner_type, al.owner_id
        """,
        VISUAL_ASSET_TYPES,
    ).fetchall()

    errors: list[str] = []
    for row in rows:
        label = f"asset_id={row['asset_id']} {row['owner_type']}:{row['owner_id']}"
        _static_filename, path_errors = safe_static_filename(row["local_path"])
        for path_error in path_errors:
            errors.append(f"{label}: {path_error}: {row['local_path']}")
        file_path = Path(row["local_path"])
        if not file_path.exists():
            errors.append(f"{label}: DB-linked image file is missing: {row['local_path']}")
        elif not file_path.is_file():
            errors.append(f"{label}: DB-linked image path is not a file: {row['local_path']}")

    return errors, len(rows)


def render_question_visual_markup(templates_dir: Path, visual_asset: dict[str, object]) -> str:
    env = Environment(
        loader=FileSystemLoader(str(templates_dir)),
        autoescape=select_autoescape(("html", "xml")),
    )

    def fake_url_for(endpoint: str, **values: object) -> str:
        if endpoint != "static":
            raise ValueError(f"unsupported endpoint in audit render: {endpoint}")
        filename = str(values.get("filename") or "")
        return f"/static/{filename}"

    env.globals["url_for"] = fake_url_for
    template = env.get_template("_question_visuals.html")
    return str(template.module.render_question_visuals([visual_asset]))


def audit_render_markup(templates_dir: Path, linked_assets: list[dict[str, object]]) -> list[str]:
    if not linked_assets:
        return ["no linked assets available to exercise rendered image markup"]
    try:
        markup = render_question_visual_markup(templates_dir, linked_assets[0])
    except Exception as exc:  # pragma: no cover - error detail matters more than exception type here
        return [f"rendering question visual markup failed: {exc}"]

    static_filename = str(linked_assets[0]["static_filename"])
    errors = []
    if "<img" not in markup:
        errors.append("rendered markup does not include an img tag")
    if f'/static/{static_filename}' not in markup:
        errors.append(f"rendered markup does not include static path /static/{static_filename}")
    if "alt=" not in markup:
        errors.append("rendered markup does not include alt text")
    return errors


def audit_rendered_question_images(
    *,
    db_path: Path = DEFAULT_DB_PATH,
    mapping_path: Path = DEFAULT_MAPPING_PATH,
    image_dir: Path = DEFAULT_IMAGE_DIR,
    templates_dir: Path = DEFAULT_TEMPLATES_DIR,
) -> tuple[list[str], dict[str, int]]:
    errors: list[str] = []
    payload = load_mapping(mapping_path)

    with closing(connect_readonly(db_path)) as conn:
        errors.extend(validate_schema(conn))
        question_rows = fetch_question_rows(conn, collect_ready_question_ids(payload))
        mapping_errors, mapping_summary = validate_entries(payload, question_rows)
        errors.extend(mapping_errors)

        image_errors, image_summary, _missing, _unexpected = validate_image_files(
            payload,
            image_dir,
            require_files=False,
        )
        errors.extend(image_errors)

        link_errors, linked_assets, skipped_missing = audit_mapping_links(conn, payload, image_dir)
        errors.extend(link_errors)

        db_static_errors, db_static_link_count = audit_all_db_static_links(conn)
        errors.extend(db_static_errors)

    render_errors = audit_render_markup(templates_dir, linked_assets) if linked_assets else []
    errors.extend(render_errors)

    summary = {
        "total_entries": mapping_summary["total_entries"],
        "ready_mappings": image_summary["ready_mappings"],
        "ready_mappings_linked": len(linked_assets),
        "assets_found": image_summary["files_present"],
        "assets_missing": image_summary["files_missing"],
        "missing_skipped": len(skipped_missing),
        "db_static_links": db_static_link_count,
        "render_markup_checks": 1 if linked_assets and not render_errors else 0,
        "validation_errors": len(errors),
    }
    return errors, summary


def print_summary(
    *,
    db_path: Path,
    mapping_path: Path,
    image_dir: Path,
    templates_dir: Path,
    summary: dict[str, int],
    errors: list[str],
) -> None:
    print("Rendered question image audit")
    print("=============================")
    print(f"mapping: {mapping_path}")
    print(f"DB: {db_path}")
    print(f"image_dir: {image_dir}")
    print(f"templates_dir: {templates_dir}")
    print(f"total entries: {summary['total_entries']}")
    print(f"ready mappings: {summary['ready_mappings']}")
    print(f"ready mappings linked: {summary['ready_mappings_linked']}")
    print(f"assets found: {summary['assets_found']}")
    print(f"assets missing: {summary['assets_missing']}")
    print(f"missing/skipped: {summary['missing_skipped']}")
    print(f"DB static links audited: {summary['db_static_links']}")
    print(f"render markup checks: {summary['render_markup_checks']}")
    print(f"validation errors: {summary['validation_errors']}")
    print(f"audit_exit={0 if not errors else 1}")
    if errors:
        print()
        print("Validation errors")
        print("-----------------")
        for error in errors:
            print(f"- {error}")


def main() -> int:
    args = parse_args()
    try:
        errors, summary = audit_rendered_question_images(
            db_path=args.db,
            mapping_path=args.mapping,
            image_dir=args.image_dir,
            templates_dir=args.templates_dir,
        )
    except (OSError, json.JSONDecodeError, sqlite3.Error, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print_summary(
        db_path=args.db,
        mapping_path=args.mapping,
        image_dir=args.image_dir,
        templates_dir=args.templates_dir,
        summary=summary,
        errors=errors,
    )
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
