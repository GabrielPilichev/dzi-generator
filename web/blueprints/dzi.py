"""DZI pages: the official-exam source list and source view, the practical-task
page for a source, and the open-question candidate overview (roadmap PR E1).

Every view is admin-only through its declared access policy
(:mod:`web.access`); this module never reads the session to decide access.
Queries live in :mod:`web.services.dzi_exams`, :mod:`web.services.practical`
and the quiz service. The module imports neither ``web.app`` nor any other
controller; the practical-task listing comes from the practical service, which
reads the application's resource roots from configuration.
"""

from __future__ import annotations

from contextlib import closing

from flask import Blueprint, abort, current_app, render_template

from web import db as web_db
from web.access import ADMIN, access_policy
from web.services import dzi_exams, practical
from web.services import quiz as quiz_service


dzi_bp = Blueprint("dzi", __name__)


def _request_db():
    return web_db.connect_db(current_app.config["DB_PATH"])


@dzi_bp.route("/dzi")
@access_policy(ADMIN)
def dzi_index():
    with closing(_request_db()) as db:
        sources = dzi_exams.fetch_dzi_sources(db)
    return render_template("dzi_index.html", sources=sources)


@dzi_bp.route("/dzi/source/<source_slug>")
@access_policy(ADMIN)
def dzi_source_view(source_slug: str):
    with closing(_request_db()) as db:
        exam = dzi_exams.dzi_find_exam(db, source_slug)
        if exam is None:
            abort(404)
        tasks = dzi_exams.fetch_dzi_tasks(db, int(exam["id"]))
        practical_resource_count = dzi_exams.fetch_practical_resource_count(db, int(exam["id"]))
    return render_template(
        "dzi_source.html",
        exam=exam,
        tasks=tasks,
        practical_resource_count=practical_resource_count,
    )


@dzi_bp.route("/dzi/source/<source_slug>/practical")
@access_policy(ADMIN)
def dzi_practical_source(source_slug: str):
    with closing(_request_db()) as db:
        resolved = practical.fetch_practical_tasks(
            db, source_slug,
            batch_dir=current_app.config["PRACTICAL_TASK_BATCH_DIR"],
            resources=practical.resources_from_config(current_app.config),
        )
    if resolved is None:
        abort(404)
    exam, tasks = resolved
    return render_template("practical_tasks.html", exam=exam, tasks=tasks)


@dzi_bp.route("/admin/open-question-candidates")
@access_policy(ADMIN)
def admin_open_question_candidates():
    with quiz_service.quiz_connection() as conn:
        candidates = quiz_service.fetch_open_question_candidates(conn)

    grouped: dict[str, int] = {}
    for candidate in candidates:
        source_slug = candidate.get("source_slug") or "unknown"
        grouped[source_slug] = grouped.get(source_slug, 0) + 1

    grouped_sources = [
        {"source_slug": source_slug, "count": count}
        for source_slug, count in sorted(grouped.items())
    ]

    return render_template(
        "open_question_candidates.html",
        candidates=candidates,
        grouped_sources=grouped_sources,
        total_candidates=len(candidates),
    )
