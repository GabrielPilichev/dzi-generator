"""DZI pages: the official-exam source list and source view, the practical-task
page for a source, and the open-question candidate overview (roadmap PR E1).

Every view is admin-only through its declared access policy
(:mod:`web.access`); this module never reads the session to decide access.
Queries live in :mod:`web.services.dzi_exams` and the quiz service. The module
imports neither ``web.app`` nor any other controller. The one thing it cannot
own yet, the practical-task loader that is bound to the application's resource
roots, is registered by the composition root through :func:`configure_runtime`
and is read on every use; the practical blueprint step (E2) removes that seam.
"""

from __future__ import annotations

from contextlib import closing
from typing import Callable

from flask import Blueprint, abort, current_app, render_template

from web import db as web_db
from web.access import ADMIN, access_policy
from web.services import dzi_exams
from web.services import quiz as quiz_service


dzi_bp = Blueprint("dzi", __name__)

_runtime: dict[str, Callable] = {}


def configure_runtime(*, practical_tasks: Callable[[str], tuple[dict, list[dict]] | None]) -> None:
    """Register the loader for ``/dzi/source/<source_slug>/practical``."""
    _runtime["practical_tasks"] = practical_tasks


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
    try:
        load_practical_tasks = _runtime["practical_tasks"]
    except KeyError:
        raise RuntimeError(
            "web.blueprints.dzi is not configured; call configure_runtime() first"
        ) from None
    resolved = load_practical_tasks(source_slug)
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
