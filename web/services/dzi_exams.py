"""Read-only queries behind the DZI exam pages (roadmap PR E1).

Source slugs, exam lookup, the source list and the task listing for one exam.
Every function takes the SQLite connection it reads through, so the module owns
no connection policy and never imports ``web.app``, the DZI blueprint or the
quiz service.

This is not the content-identity predicate: *which questions count as official
DZI content* stays in :mod:`web.services.dzi`. Authorization is the access
policy declared on the views, not this module.
"""

from __future__ import annotations

import json
import sqlite3

from web.services.question_visuals import fetch_question_visual_assets
from web.text_values import unique_text_values

DZI_FORMAT_VERSION = "dzi_it_pp_2025_format"
DZI_SESSION_SLUGS = {
    "may": "may",
    "august": "aug",
}
DZI_SLUG_SESSIONS = {
    "may": "may",
    "aug": "august",
}
DZI_SESSION_LABELS = {
    "may": "май",
    "august": "август",
}


def dzi_source_slug(row: sqlite3.Row | dict) -> str:
    prefix = DZI_SESSION_SLUGS.get(row["session"], row["session"])
    return f"{prefix}_{row['year']}_v{row['variant']}"


def dzi_source_title(row: sqlite3.Row | dict) -> str:
    session = DZI_SESSION_LABELS.get(row["session"], row["session"])
    return f"ДЗИ ИТ ПП — {session} {row['year']}, вариант {row['variant']}"


def dzi_parse_source_slug(source_slug: str) -> tuple[int, str, int] | None:
    parts = source_slug.split("_")
    if len(parts) != 3:
        return None
    session = DZI_SLUG_SESSIONS.get(parts[0])
    if session is None:
        return None
    try:
        year = int(parts[1])
        if not parts[2].startswith("v"):
            return None
        variant = int(parts[2][1:])
    except ValueError:
        return None
    return year, session, variant


def dzi_find_exam(db: sqlite3.Connection, source_slug: str) -> dict | None:
    parsed = dzi_parse_source_slug(source_slug)
    if parsed is None:
        return None
    year, session, variant = parsed
    row = db.execute("""
        SELECT *
        FROM exams
        WHERE format_version = ?
          AND year = ?
          AND session = ?
          AND variant = ?
        ORDER BY id
        LIMIT 1
    """, (DZI_FORMAT_VERSION, year, session, variant)).fetchone()
    if row is None:
        return None
    exam = dict(row)
    exam["source_slug"] = dzi_source_slug(row)
    exam["title"] = dzi_source_title(row)
    return exam


def fetch_dzi_sources(db: sqlite3.Connection) -> list[dict]:
    rows = db.execute("""
        SELECT
            e.*,
            (
                SELECT COUNT(*)
                FROM exam_tasks et
                WHERE et.exam_id = e.id
            ) AS task_count,
            (
                SELECT COALESCE(SUM(et.points), 0)
                FROM exam_tasks et
                WHERE et.exam_id = e.id
            ) AS total_points,
            (
                SELECT COUNT(DISTINCT etq.question_id)
                FROM exam_tasks et
                JOIN exam_task_questions etq
                    ON etq.task_id = et.id
                   AND etq.role = 'primary'
                WHERE et.exam_id = e.id
                  AND et.task_number BETWEEN 1 AND 25
            ) AS part1_linked_questions,
            (
                SELECT COUNT(*)
                FROM official_exam_sources oes
                WHERE oes.exam_id = e.id
                  AND oes.source_kind = 'exam_pdf'
            ) AS exam_pdf_sources,
            (
                SELECT COUNT(DISTINCT source_links.asset_id)
                FROM asset_links source_links
                WHERE source_links.owner_type = 'exam'
                  AND source_links.owner_id = e.id
                  AND source_links.role = 'source_pdf'
            ) AS source_pdf_assets
        FROM exams e
        WHERE e.format_version = ?
        ORDER BY e.year DESC, e.session, e.variant
    """, (DZI_FORMAT_VERSION,)).fetchall()

    sources = []
    for row in rows:
        source = dict(row)
        source["source_slug"] = dzi_source_slug(row)
        source["title"] = dzi_source_title(row)
        source["source_pdf_status"] = (
            "наличен"
            if source["source_file"] and source["exam_pdf_sources"] and source["source_pdf_assets"]
            else "непълен"
        )
        sources.append(source)
    return sources


def fetch_dzi_task_asset_counts(db: sqlite3.Connection, exam_id: int) -> dict[int, int]:
    rows = db.execute("""
        SELECT et.task_number, COUNT(DISTINCT al.asset_id) AS asset_count
        FROM exam_tasks et
        LEFT JOIN asset_links al
            ON al.owner_type = 'exam_task'
           AND al.owner_id = et.id
        WHERE et.exam_id = ?
        GROUP BY et.id
    """, (exam_id,)).fetchall()
    return {int(row["task_number"]): int(row["asset_count"] or 0) for row in rows}


def fetch_practical_resource_count(db: sqlite3.Connection, exam_id: int) -> int:
    row = db.execute("""
        SELECT COUNT(*)
        FROM practical_task_resources ptr
        JOIN exam_tasks et ON et.id = ptr.exam_task_id
        WHERE et.exam_id = ?
          AND et.task_number BETWEEN 26 AND 28
    """, (exam_id,)).fetchone()
    return int(row[0] or 0)


def fetch_dzi_question_options(db: sqlite3.Connection, question_id: int) -> list[dict]:
    rows = db.execute("""
        SELECT option_letter, option_text, is_correct
        FROM multiple_choice_options
        WHERE question_id = ?
        ORDER BY option_letter
    """, (question_id,)).fetchall()
    return [dict(row) for row in rows]


def dzi_json_text_list(value: str | None) -> list[str]:
    if value is None:
        return []
    value = str(value).strip()
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return [value]
    if parsed is None:
        return []
    if isinstance(parsed, list):
        return [str(item) for item in parsed if item is not None and str(item).strip()]
    if isinstance(parsed, str):
        return [parsed] if parsed.strip() else []
    return [str(parsed)]


def fetch_fill_in_answer_rows(db: sqlite3.Connection, question_id: int) -> list[dict]:
    rows = db.execute("""
        SELECT subquestion_number, subquestion_text, correct_answer, answer_alternatives, points
        FROM fill_in_subquestions
        WHERE question_id = ?
        ORDER BY subquestion_number
    """, (question_id,)).fetchall()
    subquestions = []
    for row in rows:
        correct_answers = dzi_json_text_list(row["correct_answer"])
        alternatives = dzi_json_text_list(row["answer_alternatives"])
        accepted_answers = unique_text_values(correct_answers + alternatives)
        subquestion = dict(row)
        subquestion["correct_answers"] = correct_answers
        subquestion["answer_alternatives_list"] = alternatives
        subquestion["accepted_answers"] = accepted_answers
        subquestions.append(subquestion)
    return subquestions


def fetch_dzi_tasks(db: sqlite3.Connection, exam_id: int) -> dict[str, list[dict]]:
    rows = db.execute("""
        SELECT
            et.id AS task_id,
            et.task_number,
            et.task_kind,
            et.points,
            et.has_assets,
            q.id AS question_id,
            q.prompt AS question_prompt,
            q.question_type,
            COALESCE(t.title_bg, tq.title_bg) AS topic_title,
            pt.work_environment
        FROM exam_tasks et
        LEFT JOIN exam_task_questions etq
            ON etq.task_id = et.id
           AND etq.role = 'primary'
        LEFT JOIN questions q
            ON q.id = etq.question_id
           AND (q.is_ai_generated = 0 OR q.quality_score >= 1.0)
        LEFT JOIN curriculum_topics t ON t.id = q.topic_id
        LEFT JOIN curriculum_topics tq ON tq.id = et.topic_id
        LEFT JOIN practical_tasks pt ON pt.task_id = et.id
        WHERE et.exam_id = ?
        ORDER BY et.task_number
    """, (exam_id,)).fetchall()

    asset_counts = fetch_dzi_task_asset_counts(db, exam_id)
    grouped = {"part1": [], "part2": []}
    question_ids = [
        int(row["question_id"])
        for row in rows
        if row["question_id"] is not None
    ]
    visual_assets_by_question = fetch_question_visual_assets(db, question_ids)
    for row in rows:
        task = dict(row)
        task["asset_count"] = asset_counts.get(int(task["task_number"]), 0)
        task["linked"] = task["question_id"] is not None
        task["options"] = []
        task["subquestions"] = []
        task["visual_assets"] = (
            visual_assets_by_question.get(int(task["question_id"]), [])
            if task["question_id"] is not None
            else []
        )
        task["linked_image_count"] = len(task["visual_assets"])
        task["has_linked_image"] = bool(task["linked_image_count"])
        if task["question_id"] and task["question_type"] == "multiple_choice":
            task["options"] = fetch_dzi_question_options(db, int(task["question_id"]))
        elif task["question_id"] and task["question_type"] in {"fill_in", "short_answer"}:
            task["subquestions"] = fetch_fill_in_answer_rows(db, int(task["question_id"]))

        key = "part1" if int(task["task_number"]) <= 25 else "part2"
        grouped[key].append(task)
    return grouped
