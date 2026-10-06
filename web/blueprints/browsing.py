"""Public browsing pages (roadmap PR E4): the home page, the grade and subject
hierarchy, the legacy IT section page, the permanent Physics redirects, the
health check and the favicon.

Every view is public and declares it (:mod:`web.access`); all routes are GET
(and the implicit HEAD/OPTIONS), so there is no unsafe-method integrity policy
to declare. Subject metadata comes from :mod:`web.services.subject_catalog` and
the cached browsing reads from :mod:`web.services.browsing`; the ownership and
short-slug rules stay in ``src.subject_browsing``. The module imports neither
``web.app`` nor another controller, and the URLs are unchanged. The tester and
auth routes stay on the application until E5.
"""

from __future__ import annotations

import sqlite3

from flask import Blueprint, abort, current_app, redirect, render_template, send_file, url_for

from src.subject_browsing import stored_slug_candidates
from src.subject_content import IT
from src.subject_registry import PLATFORM_GRADES, subject_supports_grade
from web.access import PUBLIC, access_policy
from web import db as web_db
from web.services.browsing import (
    fetch_questions_in_section,
    fetch_section,
    grade_subject_entries,
    subject_browser,
    subject_content_browser,
    subject_section_content,
    subject_section_groups,
)
from web.services.subject_catalog import (
    catalog_subject,
    get_subject_topic_group_config,
    subject_catalog,
    subject_topic_groups,
)

browsing_bp = Blueprint("browsing", __name__)

HEALTH_DB_TIMEOUT_SECONDS = 2.0


def open_existing_database_read_only(path, *, timeout: float = 5.0) -> sqlite3.Connection:
    """Never creates a missing database file; this connection refuses SQL writes."""
    return web_db.connect_db(path, query_only=True, timeout=timeout)


@browsing_bp.route("/")
@access_policy(PUBLIC)
def index():
    subjects = []
    dzi_section = None
    for catalog_entry in subject_catalog():
        subject = dict(catalog_entry)
        subject["question_count"] = 0
        subject["search_content"] = ""
        if subject["content_grades"]:
            browser = subject_browser(subject["id"])
            overview = browser.overview(subject["content_grades"])
            subject["question_count"] = overview["question_count"]
            subject["search_content"] = overview["search_content"]
            if subject["dzi"]:
                dzi_section = next(
                    (section for section in overview["sections"]
                     if section["section_slug"] == "grade12-dzi-preparation"),
                    None,
                )
        subjects.append(subject)

    return render_template(
        "index.html",
        subjects=subjects,
        home_total_questions=sum(subject["question_count"] for subject in subjects),
        home_class_count=len({grade for subject in subjects for grade in subject["content_grades"]}),
        dzi_section=dzi_section,
    )


def render_subject_page(subject_slug: str):
    subject = catalog_subject(subject_slug)
    if subject is None:
        abort(404)

    grade_rows = []
    browser = subject_content_browser(subject)
    if browser is not None:
        grade_rows = browser.grade_counts()
    grade_rows_by_number = {int(row["grade"]): row for row in grade_rows}
    total_questions = sum(int(row["question_count"] or 0) for row in grade_rows)
    return render_template(
        "subject.html",
        subject=subject,
        content_grades=set(subject["content_grades"]),
        grade_rows_by_number=grade_rows_by_number,
        total_questions=total_questions,
    )


def render_subject_grade_page(subject_slug: str, grade: int):
    subject = catalog_subject(subject_slug)
    if subject is None or grade not in subject["available_classes"]:
        abort(404)

    sections = []
    browser = subject_content_browser(subject)
    if browser is not None:
        sections = browser.sections_for_grade(grade)

    return render_template(
        "subject_grade.html",
        subject=subject,
        grade=grade,
        sections=sections,
        section_groups=subject_section_groups(sections),
        topic_groups=subject_topic_groups(subject, grade) if not sections else (),
    )


def render_subject_topic_group_page(subject_slug: str, grade: int, topic_group_slug: str):
    subject = catalog_subject(subject_slug)
    if subject is None or grade not in subject["available_classes"]:
        abort(404)

    browser = subject_content_browser(subject)
    if browser is not None:
        section = browser.section_for_url(topic_group_slug, grade)
        if section is not None:
            topics, questions = subject_section_content(browser, section)
            return render_template(
                "subject_section.html",
                subject=subject,
                grade=grade,
                topic_group={"slug": section["section_slug"], "title": section["section_title"]},
                section=section,
                topics=topics,
                questions=questions,
            )
        # Once this class has DB content, the provisional configuration is no
        # longer a parallel source of real group routes.
        if grade in subject["content_grades"]:
            abort(404)

    topic_group_result = get_subject_topic_group_config(subject_slug, grade, topic_group_slug)
    if topic_group_result is None:
        abort(404)

    subject, topic_group = topic_group_result
    return render_template(
        "subject_section.html",
        subject=subject,
        grade=grade,
        topic_group=topic_group,
    )


def render_subject_topic_page(subject_slug: str, grade: int, section_slug: str, topic_slug: str):
    """One topic of an owned section. Placeholder groups have no topics."""
    subject = catalog_subject(subject_slug)
    if subject is None or grade not in subject["available_classes"]:
        abort(404)
    browser = subject_content_browser(subject)
    if browser is None:
        abort(404)
    section = browser.section_for_url(section_slug, grade)
    if section is None:
        abort(404)

    # The section's cached, ownership-checked topics and questions are filtered
    # here; a topic page adds no query path of its own.
    topics, section_questions = subject_section_content(browser, section)
    by_slug = {topic["topic_slug"]: topic for topic in topics}
    topic = next(
        (by_slug[slug] for slug in stored_slug_candidates(subject["id"], grade, topic_slug)
         if slug in by_slug),
        None,
    )
    if topic is None:
        abort(404)

    return render_template(
        "subject_topic.html",
        subject=subject,
        grade=grade,
        section=section,
        topic=topic,
        questions=[q for q in section_questions if q["topic_id"] == topic["topic_id"]],
        section_question_count=len(section_questions),
    )


@browsing_bp.route("/subjects/<subject_slug>")
@access_policy(PUBLIC)
def subject_view(subject_slug: str):
    return render_subject_page(subject_slug)


@browsing_bp.route("/subjects/<subject_slug>/<int:grade>")
@access_policy(PUBLIC)
def subject_grade_view(subject_slug: str, grade: int):
    return render_subject_grade_page(subject_slug, grade)


@browsing_bp.route("/subjects/<subject_slug>/<int:grade>/<topic_group_slug>")
@access_policy(PUBLIC)
def subject_topic_group_view(subject_slug: str, grade: int, topic_group_slug: str):
    return render_subject_topic_group_page(subject_slug, grade, topic_group_slug)


@browsing_bp.route("/subjects/<subject_slug>/<int:grade>/<topic_group_slug>/<topic_slug>")
@access_policy(PUBLIC)
def subject_topic_view(subject_slug: str, grade: int, topic_group_slug: str, topic_slug: str):
    return render_subject_topic_page(subject_slug, grade, topic_group_slug, topic_slug)


# Pre-registry Physics URLs: permanent redirects to the generic subject pages.
@browsing_bp.route("/physics")
@access_policy(PUBLIC)
def physics():
    return redirect(url_for("browsing.subject_view", subject_slug="physics"), code=301)


@browsing_bp.route("/physics/grade/<int:grade>")
@access_policy(PUBLIC)
def physics_grade(grade: int):
    if not subject_supports_grade("physics", grade):
        abort(404)
    return redirect(url_for("browsing.subject_grade_view", subject_slug="physics", grade=grade), code=301)


@browsing_bp.route("/favicon.ico")
@access_policy(PUBLIC)
def favicon():
    project_root = current_app.config["PROJECT_ROOT"]
    return send_file(project_root / "web" / "static" / "img" / "brand" / "favicon.ico", mimetype="image/x-icon")


@browsing_bp.route("/healthz")
@access_policy(PUBLIC)
def healthz():
    """Liveness plus one cheap database header read; no templates or content queries."""
    try:
        conn = open_existing_database_read_only(
            current_app.config["DB_PATH"], timeout=HEALTH_DB_TIMEOUT_SECONDS
        )
        try:
            conn.execute("PRAGMA schema_version").fetchone()
        finally:
            conn.close()
    except (sqlite3.Error, OSError, ValueError) as exc:
        current_app.logger.warning("Health check failed: %s", type(exc).__name__)
        status, body = 503, "unavailable\n"
    else:
        status, body = 200, "ok\n"
    return current_app.response_class(
        body, status=status, mimetype="text/plain",
        headers={"Cache-Control": "no-store"},
    )


@browsing_bp.route("/grade/<int:grade>")
@access_policy(PUBLIC)
def grade_view(grade: int):
    if grade not in PLATFORM_GRADES:
        abort(404)
    return render_template(
        "grade.html",
        grade=grade,
        entries=grade_subject_entries(grade),
        profiled_outside_scope=[
            subject for subject in subject_catalog()
            if grade in subject["profiled_grades_outside_scope"]
        ],
    )


@browsing_bp.route("/section/<slug>")
@access_policy(PUBLIC)
def section_view(slug: str):
    section = fetch_section(slug)
    if section is None:
        abort(404)
    questions = fetch_questions_in_section(section["section_id"])
    return render_template(
        "section.html",
        section=section,
        questions=questions,
        section_subject=next(subject for subject in subject_catalog() if subject["id"] == IT),
    )
