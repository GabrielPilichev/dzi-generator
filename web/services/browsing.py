"""Public subject browsing reads shared by the browsing controllers.

The per-request browsing facade, a section's cached topics and questions, the
grade-page module grouping and the per-grade subject listing live here, not in
``web.app`` or the controller. Subject metadata comes from
:mod:`web.services.subject_catalog`; the ownership and slug rules stay in
``src.subject_browsing`` and ``src.subject_browse_cache``.
"""

from __future__ import annotations

import sqlite3

from flask import g

from src.subject_browse_cache import CachedSubjectBrowser
from src.subject_content import IT, canonical_subject, sql_chunks
from web.request_db import get_request_db
from web.services.dzi_exams import fill_in_answer_row
from web.services.question_visuals import attach_question_visual_assets
from web.services.subject_catalog import subject_catalog


def subject_browser(subject_id: str = IT) -> CachedSubjectBrowser:
    """One public browsing facade per subject/request, shared by legacy routes."""
    subject_id = canonical_subject(subject_id)
    browsers = g.setdefault("subject_browsers", {})
    if subject_id not in browsers:
        browsers[subject_id] = CachedSubjectBrowser(
            get_request_db(), subject_id, legacy_it=subject_id == IT
        )
    return browsers[subject_id]


def fetch_grades_with_counts() -> list[dict]:
    """Legacy IT grade counts; other subjects use their own browsing scope."""
    return subject_browser().grade_counts()


def fetch_section(slug: str) -> dict | None:
    return subject_browser().section(slug, legacy_unclassified=True)


def fetch_questions_in_section(
    section_id: int, *, browser: CachedSubjectBrowser | None = None
) -> list[dict]:
    """Approved questions linked to this section via topic_section_assignments."""
    browser = browser or subject_browser()
    return browser.question_details(section_id, _decorate_browse_questions)


def _decorate_browse_questions(db: sqlite3.Connection, rows: list[dict]) -> list[dict]:
    """DB-only public options/answers/asset metadata; no URLs or request state."""

    mc_ids = [r["question_id"] for r in rows if r["question_type"] in ("multiple_choice", "true_false")]
    fill_ids = [r["question_id"] for r in rows if r["question_type"] in {"fill_in", "short_answer"}]
    options = _options_by_question(db, mc_ids)
    subquestions = _subquestions_by_question(db, fill_ids)
    questions = []
    for r in rows:
        q = dict(r)
        is_mc = q["question_type"] in ("multiple_choice", "true_false")
        q["options"] = options.get(q["question_id"], []) if is_mc else []
        is_fill = q["question_type"] in {"fill_in", "short_answer"}
        q["subquestions"] = [dict(s) for s in subquestions.get(q["question_id"], [])] if is_fill else []
        questions.append(q)
    return attach_question_visual_assets(db, questions, id_key="question_id")


def _options_by_question(db: sqlite3.Connection, question_ids: list) -> dict[int, list[dict]]:
    """Options per question in one query per ``SQL_CHUNK_SIZE`` ids, letter-ordered."""
    grouped: dict[int, list[dict]] = {}
    for chunk in sql_chunks(set(question_ids)):
        marks = ",".join("?" for _ in chunk)
        for row in db.execute(f"""
            SELECT question_id, option_letter, option_text, is_correct
            FROM multiple_choice_options
            WHERE question_id IN ({marks})
            ORDER BY question_id, option_letter
        """, chunk):
            option = dict(row)
            grouped.setdefault(option.pop("question_id"), []).append(option)
    return grouped


def _subquestions_by_question(db: sqlite3.Connection, question_ids: list) -> dict[int, list[dict]]:
    """Fill-in subquestions per question in one query per ``SQL_CHUNK_SIZE`` ids."""
    grouped: dict[int, list[dict]] = {}
    for chunk in sql_chunks(set(question_ids)):
        marks = ",".join("?" for _ in chunk)
        for row in db.execute(f"""
            SELECT question_id, subquestion_number, subquestion_text, correct_answer,
                   answer_alternatives, points
            FROM fill_in_subquestions
            WHERE question_id IN ({marks})
            ORDER BY question_id, subquestion_number
        """, chunk):
            subquestion = fill_in_answer_row(row)
            del subquestion["question_id"]
            grouped.setdefault(row["question_id"], []).append(subquestion)
    return grouped


def subject_content_browser(subject: dict) -> CachedSubjectBrowser | None:
    """Browser for subjects that own stored sections; None while in preparation."""
    if not subject["content_grades"]:
        return None
    return subject_browser(subject["id"])


def subject_section_groups(sections: list[dict]) -> list[dict]:
    """Sections grouped by curriculum module, only where that adds structure.

    Modules are optional: a grade with no module, or with a single one, stays a
    flat list of sections. Sections outside any module form the last group.
    """
    groups: dict[object, dict] = {}
    for section in sections:
        key = section.get("module_slug")
        group = groups.setdefault(
            key, {"module_slug": key, "title": section.get("module_title"), "sections": []}
        )
        group["sections"].append(section)
    if len(groups) < 2:
        return []
    ungrouped = groups.pop(None, None)
    return [*groups.values(), *([ungrouped] if ungrouped else [])]


def subject_section_content(browser: CachedSubjectBrowser, section: dict) -> tuple[list[dict], list[dict]]:
    """A section's topics (with question counts) and questions, from cached reads."""
    questions = fetch_questions_in_section(section["section_id"], browser=browser)
    counts: dict[int, int] = {}
    for question in questions:
        counts[int(question["topic_id"])] = counts.get(int(question["topic_id"]), 0) + 1
    topics = browser.topics_in_section(section["section_id"])
    for topic in topics:
        topic["question_count"] = counts.get(int(topic["topic_id"]), 0)
    return topics, questions


def grade_subject_entries(grade: int) -> list[dict]:
    """Subjects LearnPilot supports in a grade, in registry order, with content state.

    Membership comes from the registry (``grades``); counts come from the
    database. A supported subject without content is listed as in preparation.
    """
    entries = []
    for subject in subject_catalog():
        if grade not in subject["available_classes"]:
            continue
        row = None
        browser = subject_content_browser(subject)
        if browser is not None:
            row = next(
                (row for row in browser.grade_counts() if int(row["grade"]) == grade), None
            )
        entries.append({
            "subject": subject,
            "path": subject["supported_grade_paths"].get(grade),
            "framework_plan_dependent": grade in subject["framework_plan_dependent_grades"],
            "has_content": grade in subject["content_grades"],
            "question_count": int(row["question_count"] or 0) if row else 0,
        })
    return entries
