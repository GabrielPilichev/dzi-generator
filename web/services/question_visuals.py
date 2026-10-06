"""Question visual assets: DB lookup and static-path safety.

Shared by the quiz services and the DZI pages. Standard library plus
``web.db_introspect`` only; never ``web.app``.
"""

from __future__ import annotations

import sqlite3
from pathlib import PurePosixPath

from web.db_introspect import sqlite_table_exists

QUESTION_VISUAL_ASSET_TYPES = ("image", "pdf_crop")
QUESTION_VISUAL_STATIC_PREFIX = "web/static/"
QUESTION_VISUAL_ALLOWED_PREFIX = "web/static/img/questions/dzi/"
QUESTION_VISUAL_ALLOWED_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def question_visual_static_filename(local_path: object) -> str | None:
    raw_path = str(local_path or "").strip().replace("\\", "/")
    if not raw_path or raw_path.startswith(("/", "~")) or "://" in raw_path:
        return None
    if not raw_path.startswith(QUESTION_VISUAL_ALLOWED_PREFIX):
        return None

    path = PurePosixPath(raw_path)
    if path.is_absolute() or any(part in {".", ".."} for part in path.parts):
        return None
    if path.suffix.lower() not in QUESTION_VISUAL_ALLOWED_SUFFIXES:
        return None

    return raw_path[len(QUESTION_VISUAL_STATIC_PREFIX):]


def fetch_question_visual_assets(conn: sqlite3.Connection, question_ids: list[int]) -> dict[int, list[dict]]:
    clean_ids = []
    for question_id in question_ids:
        try:
            if isinstance(question_id, bool):
                raise ValueError
            parsed_id = int(question_id)
        except (TypeError, ValueError):
            continue
        if parsed_id not in clean_ids:
            clean_ids.append(parsed_id)

    if not clean_ids:
        return {}
    if not sqlite_table_exists(conn, "assets") or not sqlite_table_exists(conn, "asset_links"):
        return {question_id: [] for question_id in clean_ids}

    # SAFE: placeholders are generated from parsed integer question IDs.
    placeholders = ",".join("?" for _ in clean_ids)
    # SAFE: SQL fragment is static and values still use ? params.
    owner_clause = "(al.owner_type IN ('question', 'questions') AND al.owner_id = q.id)"
    if sqlite_table_exists(conn, "exam_task_questions"):
        owner_clause = f"""
            (
                {owner_clause}
                OR (
                    al.owner_type = 'exam_task'
                    AND al.owner_id IN (
                        SELECT etq.task_id
                        FROM exam_task_questions etq
                        WHERE etq.question_id = q.id
                    )
                )
            )
        """

    rows = conn.execute(f"""
        SELECT
            q.id AS question_id,
            q.source_exam,
            q.source_number,
            a.id AS asset_id,
            a.local_path,
            al.caption_bg,
            al.display_order,
            al.owner_type
        FROM questions q
        JOIN asset_links al
          ON {owner_clause}
        JOIN assets a
          ON a.id = al.asset_id
        WHERE q.id IN ({placeholders})
          AND a.asset_type IN (?, ?)
        ORDER BY
            q.id,
            COALESCE(al.display_order, 0),
            CASE WHEN al.owner_type IN ('question', 'questions') THEN 0 ELSE 1 END,
            a.id
    """, (*clean_ids, *QUESTION_VISUAL_ASSET_TYPES)).fetchall()

    assets_by_question: dict[int, list[dict]] = {question_id: [] for question_id in clean_ids}
    seen_assets: dict[int, set[int]] = {question_id: set() for question_id in clean_ids}
    for row in rows:
        question_id = int(row["question_id"])
        asset_id = int(row["asset_id"])
        if asset_id in seen_assets[question_id]:
            continue

        static_filename = question_visual_static_filename(row["local_path"])
        if static_filename is None:
            continue

        source_exam = str(row["source_exam"] or "").strip()
        source_number = row["source_number"]
        if source_exam and source_number is not None:
            alt_text = f"Изображение към {source_exam} задача {source_number}"
        else:
            alt_text = f"Изображение към въпрос {question_id}"

        assets_by_question[question_id].append({
            "asset_id": asset_id,
            "static_filename": static_filename,
            "alt_text": alt_text,
            "caption": str(row["caption_bg"] or "").strip(),
        })
        seen_assets[question_id].add(asset_id)

    return assets_by_question


def attach_question_visual_assets(
    conn: sqlite3.Connection, questions: list[dict], *, id_key: str = "id"
) -> list[dict]:
    assets_by_question = fetch_question_visual_assets(
        conn,
        [int(question[id_key]) for question in questions if question.get(id_key) is not None],
    )
    for question in questions:
        question["visual_assets"] = assets_by_question.get(int(question[id_key]), [])
    return questions
