"""The subject catalog: registered subjects with content-derived availability.

Shared by the application's browsing pages and the teacher controllers, so it
lives here rather than in ``web.app``.
"""

from __future__ import annotations

import os
import sqlite3

from flask import current_app, g

from src import subject_browse_cache
from src.subject_browsing import subject_content_grades
from src.subject_registry import all_subjects, subject_for_route_slug
from web.request_db import get_request_db, request_db_if_open

# Key of the availability read in the browse cache; browser keys are
# ``(subject id, legacy flag, ...)``, so the two cannot collide.
CONTENT_GRADES_KEY = ("subject_content_grades",)


def _database_file(path) -> str | None:
    """``path`` as a request connection would open it, if it is an existing file."""
    raw = str(path)
    return raw if raw not in ("", ":memory:") and os.path.isfile(raw) else None


def _content_grades() -> dict[str, frozenset[int]]:
    """Subject availability; shared across requests only without a request connection.

    A request that has opened no connection has no transaction-local data: it
    would open one only to read the committed content of the configured file.
    That read comes from the browse cache instead, which is keyed by the
    database's identity (file and WAL), computes on a fresh query-only connection,
    never caches a failure and gives every caller its own copy. A request that
    already has a connection keeps querying it, uncommitted changes included.
    """
    if request_db_if_open() is None:
        database_file = _database_file(current_app.config["DB_PATH"])
        if database_file is not None:
            return subject_browse_cache.browse_cache.get(
                database_file, CONTENT_GRADES_KEY, subject_content_grades)
    return subject_content_grades(get_request_db())


def subject_catalog() -> tuple[dict, ...]:
    """Registered subjects with content-derived availability, cached per request.

    Static metadata comes from the canonical registry; status and content grades
    are derived from stored curriculum sections. A database error degrades to
    every subject being in preparation instead of failing the page.
    """
    if "subject_catalog" in g:
        return g.subject_catalog
    try:
        content_grades = _content_grades()
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
