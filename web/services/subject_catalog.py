"""The subject catalog: registered subjects with content-derived availability.

Shared by the application's browsing pages and the teacher controllers, so it
lives here rather than in ``web.app``.
"""

from __future__ import annotations

import sqlite3

from flask import current_app, g

from src.subject_browsing import subject_content_grades
from src.subject_registry import all_subjects, subject_for_route_slug
from web.request_db import get_request_db


def subject_catalog() -> tuple[dict, ...]:
    """Registered subjects with content-derived availability, cached per request.

    Static metadata comes from the canonical registry; status and content grades
    are derived from stored curriculum sections. A database error degrades to
    every subject being in preparation instead of failing the page.
    """
    if "subject_catalog" in g:
        return g.subject_catalog
    try:
        content_grades = subject_content_grades(get_request_db())
    except sqlite3.Error as exc:
        current_app.logger.warning("Subject availability lookup failed: %s", exc)
        content_grades = {}
    catalog = []
    for definition in all_subjects():
        grades = sorted(content_grades.get(definition.id, ()))
        catalog.append({
            "id": definition.id,
            "slug": definition.route_slug,
            "title": definition.title,
            "description": definition.description,
            "active_description": definition.active_description
            or f"Съдържание по класове, раздели и въпроси по {definition.title.lower()}.",
            "status": "active" if grades else "in_preparation",
            "available_classes": definition.grades,
            "grade_paths": dict(definition.grade_paths),
            "supported_grade_paths": {
                grade: definition.supported_path(grade) for grade in definition.grades
            },
            "framework_plan_dependent_grades": definition.framework_plan_dependent_grades,
            "profiled_grades_outside_scope": definition.profiled_grades_outside_scope,
            "has_real_content": bool(grades),
            "content_grades": grades,
            "search_terms": definition.search_terms,
            "topic_groups_by_grade": definition.placeholder_topic_groups,
            "dzi": definition.dzi,
        })
    g.subject_catalog = tuple(catalog)
    return g.subject_catalog


def catalog_subject(route_slug: str) -> dict | None:
    """Catalog entry for a URL route slug; ids and legacy aliases are not URLs."""
    definition = subject_for_route_slug(route_slug)
    if definition is None:
        return None
    return next((subject for subject in subject_catalog() if subject["id"] == definition.id), None)


def subject_topic_groups(subject: dict, grade: int) -> tuple[dict, ...]:
    topic_groups_by_grade = subject.get("topic_groups_by_grade") or {}
    return tuple(topic_groups_by_grade.get(int(grade), ()))


def get_subject_topic_group_config(subject_slug: str, grade: int, topic_group_slug: str) -> tuple[dict, dict] | None:
    subject = catalog_subject(subject_slug)
    if subject is None or grade not in subject["available_classes"]:
        return None

    for topic_group in subject_topic_groups(subject, grade):
        if topic_group.get("slug") == topic_group_slug:
            return subject, topic_group
    return None
