"""
LearnPilot — Web app, Phase 1.

Браузър за секции и въпроси от curriculum-а. Read-only.

Структура:
  /                   → начална страница, списък с класове 8-12
  /grade/<n>          → секции за избран клас, групирани по модул
  /section/<slug>     → въпроси в избрана секция

Зависимости:
  pip3 install flask

Употреба:
  cd ~/dzi-generator
  python3 web/app.py
  → отвори http://127.0.0.1:5000
"""

from __future__ import annotations

import os
import hmac
import json
import secrets
import warnings

import sqlite3
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, abort, g, render_template, url_for, request, redirect, session


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "questions.db"


def build_secret_key() -> str:
    configured = os.environ.get("DZI_SECRET_KEY")
    if configured:
        return configured

    warnings.warn(
        "DZI_SECRET_KEY is not set; using a per-process development key. "
        "Set DZI_SECRET_KEY for stable/deployed use.",
        RuntimeWarning,
        stacklevel=2,
    )
    return secrets.token_urlsafe(32)


app = Flask(__name__)
app.config["DB_PATH"] = str(DB_PATH)
app.config["SECRET_KEY"] = build_secret_key()
app.config["SESSION_COOKIE_SAMESITE"] = "Strict"
app.config["SESSION_COOKIE_HTTPONLY"] = True



# ---------------------------------------------------------------------------
# Local UI profiles
# ---------------------------------------------------------------------------

VALID_UI_PROFILES = {"admin", "tester"}
TESTER_TEACHER_ENDPOINTS = {"teacher_new", "teacher_assignment"}


def safe_redirect_target(candidate: str | None, fallback: str) -> str:
    fallback_url = fallback if fallback.startswith("/") else url_for(fallback)
    if not isinstance(candidate, str) or not candidate:
        return fallback_url
    if not candidate.startswith("/") or candidate.startswith("//"):
        return fallback_url

    parsed = urlparse(candidate)
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/"):
        return fallback_url
    return candidate


UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _same_origin_url(candidate_url: str | None) -> bool:
    if not isinstance(candidate_url, str) or not candidate_url.strip():
        return False

    candidate = urlparse(candidate_url)
    current = urlparse(request.host_url)
    return (
        candidate.scheme.lower() == current.scheme.lower()
        and candidate.netloc.lower() == current.netloc.lower()
    )


def _request_has_same_origin() -> bool:
    origin = request.headers.get("Origin")
    if origin:
        return _same_origin_url(origin)

    referer = request.headers.get("Referer")
    if referer:
        return _same_origin_url(referer)

    return bool(app.config.get("TESTING"))


@app.before_request
def protect_same_origin_unsafe_methods():
    if request.method not in UNSAFE_METHODS:
        return
    if not _request_has_same_origin():
        abort(403)


@app.before_request
def load_ui_profile():
    profile = session.get("ui_profile", "tester")
    if profile not in VALID_UI_PROFILES:
        profile = "tester"

    if profile == "admin" and not session.get("admin_authenticated"):
        profile = "tester"

    session["ui_profile"] = profile
    g.ui_profile = profile



@app.context_processor
def inject_app_meta():
    sync_label = None
    try:
        db_mtime = DB_PATH.stat().st_mtime
        vault_path = BASE_DIR / "vault"
        vault_mtime = vault_path.stat().st_mtime if vault_path.exists() else db_mtime
        latest = max(db_mtime, vault_mtime)
        sync_label = datetime.fromtimestamp(latest).strftime("%Y-%m-%d %H:%M")
    except Exception:
        sync_label = None

    return {
        "app_version": "UI pass 9",
        "last_sync_label": sync_label,
        "source_url": os.environ.get("DZI_SOURCE_URL"),
    }


@app.context_processor
def inject_ui_profile():
    profile = getattr(g, "ui_profile", "admin")
    return {
        "ui_profile": profile,
        "ui_profile_label": "Админ" if profile == "admin" else "Тестер",
    }


@app.route("/profile/<profile>")
def switch_profile(profile: str):
    if profile not in VALID_UI_PROFILES:
        abort(404)

    session["ui_profile"] = profile
    fallback = "tester_home" if profile == "tester" else "index"
    next_url = safe_redirect_target(request.args.get("next"), fallback)
    return redirect(next_url)




# ---------------------------------------------------------------------------
# Tester password protection
# ---------------------------------------------------------------------------

def tester_password_configured() -> bool:
    return bool(os.environ.get("DZI_TESTER_PASSWORD"))


def is_tester_authenticated() -> bool:
    return bool(session.get("tester_authenticated")) or is_admin_authenticated()


def can_generate_tests() -> bool:
    return is_tester_authenticated() or is_admin_authenticated()


@app.context_processor
def inject_tester_auth():
    return {
        "tester_authenticated": is_tester_authenticated(),
        "tester_password_configured": tester_password_configured(),
        "can_generate_tests": can_generate_tests(),
    }


@app.route("/tester/login", methods=["GET", "POST"])
def tester_login():
    error = None
    next_url = safe_redirect_target(
        request.args.get("next") or request.form.get("next"),
        "tester_home",
    )

    if request.method == "POST":
        configured_password = os.environ.get("DZI_TESTER_PASSWORD", "")
        submitted_password = request.form.get("password", "")

        if not configured_password:
            error = "Тестерската парола не е зададена. Стартирай сървъра с DZI_TESTER_PASSWORD."
        elif hmac.compare_digest(submitted_password, configured_password):
            session["tester_authenticated"] = True
            session["ui_profile"] = "tester"
            return redirect(next_url)
        else:
            error = "Грешна парола."

    return render_template("tester_login.html", error=error, next_url=next_url)


@app.route("/tester/logout")
def tester_logout():
    session.pop("tester_authenticated", None)
    if not session.get("admin_authenticated"):
        session["ui_profile"] = "tester"
    return redirect(url_for("tester_home"))


# ---------------------------------------------------------------------------
# Admin password protection
# ---------------------------------------------------------------------------

def admin_password_configured() -> bool:
    return bool(os.environ.get("DZI_ADMIN_PASSWORD"))


def is_admin_authenticated() -> bool:
    return bool(session.get("admin_authenticated"))


@app.context_processor
def inject_admin_auth():
    return {
        "admin_authenticated": is_admin_authenticated(),
        "admin_password_configured": admin_password_configured(),
    }


PUBLIC_DZI_ENDPOINTS: set[str] = set()
# DZI endpoints default to admin-only. Add future intentionally public
# endpoint names here instead of weakening the default-deny guard below.


@app.before_request
def protect_admin_routes():
    endpoint = request.endpoint or ""

    if endpoint in {"static", "admin_login", "tester_login", "switch_profile"}:
        # /profile/admin is handled below inside switch_profile route protection.
        if endpoint != "switch_profile":
            return

    if endpoint == "switch_profile":
        profile = (request.view_args or {}).get("profile")
        if profile == "admin" and not is_admin_authenticated():
            next_url = safe_redirect_target(
                request.args.get("next") or request.referrer,
                "index",
            )
            return redirect(url_for("admin_login", next=next_url))
        return

    if endpoint.startswith("admin") and endpoint != "admin_login" and not is_admin_authenticated():
        return redirect(url_for("admin_login", next=request.path))

    if endpoint.startswith("dzi") and endpoint not in PUBLIC_DZI_ENDPOINTS and not is_admin_authenticated():
        session["ui_profile"] = "tester"
        return redirect(url_for("admin_login", next=request.path))

    if endpoint in TESTER_TEACHER_ENDPOINTS and not can_generate_tests():
        session["ui_profile"] = "tester"
        return redirect(url_for("tester_login", next=request.path))

    if endpoint.startswith("teacher") and endpoint not in TESTER_TEACHER_ENDPOINTS and not is_admin_authenticated():
        session["ui_profile"] = "tester"
        return redirect(url_for("admin_login", next=request.path))


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None
    next_url = safe_redirect_target(
        request.args.get("next") or request.form.get("next"),
        "teacher_dashboard",
    )

    if request.method == "POST":
        configured_password = os.environ.get("DZI_ADMIN_PASSWORD", "")
        submitted_password = request.form.get("password", "")

        if not configured_password:
            error = "Админ паролата не е зададена. Стартирай сървъра с DZI_ADMIN_PASSWORD."
        elif hmac.compare_digest(submitted_password, configured_password):
            session["admin_authenticated"] = True
            session["ui_profile"] = "admin"
            return redirect(next_url)
        else:
            error = "Грешна парола."

    return render_template("admin_login.html", error=error, next_url=next_url)


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin_authenticated", None)
    session["ui_profile"] = "tester"
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    """Per-request SQLite connection. Closed in teardown."""
    if "db" not in g:
        conn = sqlite3.connect(app.config["DB_PATH"])
        conn.row_factory = sqlite3.Row
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def fetch_grades_with_counts() -> list[dict]:
    """List of grades 8-12 with question counts (production-ready only)."""
    db = get_db()
    rows = db.execute("""
        SELECT
            tc.class AS grade,
            COUNT(DISTINCT q.id) AS question_count,
            COUNT(DISTINCT t.id) AS topic_count
        FROM topic_classes tc
        JOIN curriculum_topics t ON t.id = tc.topic_id
        LEFT JOIN questions q
            ON q.topic_id = t.id
            AND (q.is_ai_generated = 0 OR q.quality_score >= 1.0)
        WHERE tc.class BETWEEN 8 AND 12
        GROUP BY tc.class
        ORDER BY tc.class
    """).fetchall()
    return [dict(r) for r in rows]


def fetch_sections_for_grade(grade: int) -> list[dict]:
    """Sections for a grade, grouped by module."""
    db = get_db()
    rows = db.execute("""
        SELECT
            cs.id              AS section_id,
            cs.section_slug    AS section_slug,
            cs.title_bg        AS section_title,
            cs.section_type    AS section_type,
            cs.has_section_test AS has_test,
            cm.module_slug     AS module_slug,
            cm.title_bg        AS module_title,
            cm.module_number   AS module_number,
            (
                SELECT COUNT(DISTINCT q.id)
                FROM topic_section_assignments tsa
                JOIN questions q ON q.topic_id = tsa.topic_id
                WHERE tsa.section_id = cs.id
                  AND (q.is_ai_generated = 0 OR q.quality_score >= 1.0)
            ) AS question_count
        FROM curriculum_sections cs
        LEFT JOIN curriculum_modules cm ON cm.id = cs.module_id
        WHERE cs.class = ?
        ORDER BY COALESCE(cm.module_number, 99), cs.display_order, cs.title_bg
    """, (grade,)).fetchall()
    return [dict(r) for r in rows]


def fetch_section(slug: str) -> dict | None:
    db = get_db()
    row = db.execute("""
        SELECT
            cs.id              AS section_id,
            cs.section_slug    AS section_slug,
            cs.title_bg        AS section_title,
            cs.class           AS grade,
            cm.title_bg        AS module_title,
            cm.module_number   AS module_number
        FROM curriculum_sections cs
        LEFT JOIN curriculum_modules cm ON cm.id = cs.module_id
        WHERE cs.section_slug = ?
    """, (slug,)).fetchone()
    return dict(row) if row else None


def fetch_questions_in_section(section_id: int) -> list[dict]:
    """Approved questions linked to this section via topic_section_assignments."""
    db = get_db()
    rows = db.execute("""
        SELECT DISTINCT
            q.id            AS question_id,
            q.prompt        AS prompt,
            q.question_type AS question_type,
            q.difficulty    AS difficulty,
            t.topic_slug    AS topic_slug,
            t.title_bg      AS topic_title
        FROM topic_section_assignments tsa
        JOIN questions q ON q.topic_id = tsa.topic_id
        JOIN curriculum_topics t ON t.id = q.topic_id
        WHERE tsa.section_id = ?
          AND (q.is_ai_generated = 0 OR q.quality_score >= 1.0)
        ORDER BY t.topic_slug, q.id
    """, (section_id,)).fetchall()

    questions = []
    for r in rows:
        q = dict(r)
        if q["question_type"] in ("multiple_choice", "true_false"):
            opts = db.execute("""
                SELECT option_letter, option_text, is_correct
                FROM multiple_choice_options
                WHERE question_id = ?
                ORDER BY option_letter
            """, (q["question_id"],)).fetchall()
            q["options"] = [dict(o) for o in opts]
        else:
            q["options"] = []
        questions.append(q)
    return questions


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


def dzi_find_exam(source_slug: str) -> dict | None:
    parsed = dzi_parse_source_slug(source_slug)
    if parsed is None:
        return None
    year, session, variant = parsed
    db = get_db()
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


def fetch_dzi_sources() -> list[dict]:
    db = get_db()
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


def fetch_dzi_task_asset_counts(exam_id: int) -> dict[int, int]:
    db = get_db()
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


def fetch_dzi_question_options(question_id: int) -> list[dict]:
    db = get_db()
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


def fetch_dzi_fill_subquestions(question_id: int) -> list[dict]:
    db = get_db()
    rows = db.execute("""
        SELECT subquestion_number, subquestion_text, correct_answer, answer_alternatives, points
        FROM fill_in_subquestions
        WHERE question_id = ?
        ORDER BY subquestion_number
    """, (question_id,)).fetchall()
    subquestions = []
    for row in rows:
        subquestion = dict(row)
        subquestion["correct_answers"] = dzi_json_text_list(row["correct_answer"])
        subquestion["answer_alternatives_list"] = dzi_json_text_list(row["answer_alternatives"])
        subquestions.append(subquestion)
    return subquestions


def fetch_dzi_tasks(exam_id: int) -> dict[str, list[dict]]:
    db = get_db()
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

    asset_counts = fetch_dzi_task_asset_counts(exam_id)
    grouped = {"part1": [], "part2": []}
    for row in rows:
        task = dict(row)
        task["asset_count"] = asset_counts.get(int(task["task_number"]), 0)
        task["linked"] = task["question_id"] is not None
        task["options"] = []
        task["subquestions"] = []
        if task["question_id"] and task["question_type"] == "multiple_choice":
            task["options"] = fetch_dzi_question_options(int(task["question_id"]))
        elif task["question_id"] and task["question_type"] in {"fill_in", "short_answer"}:
            task["subquestions"] = fetch_dzi_fill_subquestions(int(task["question_id"]))

        key = "part1" if int(task["task_number"]) <= 25 else "part2"
        grouped[key].append(task)
    return grouped


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/tester")
def tester_home():
    return render_template("tester_home.html")


@app.route("/")
def index():
    grades = fetch_grades_with_counts()
    return render_template("index.html", grades=grades)


@app.route("/grade/<int:grade>")
def grade_view(grade: int):
    if not 8 <= grade <= 12:
        abort(404)
    sections = fetch_sections_for_grade(grade)

    # Group by module for display
    modules: dict[str, dict] = {}
    for s in sections:
        key = s["module_slug"]
        if key not in modules:
            modules[key] = {
                "module_slug": key,
                "module_title": s["module_title"],
                "module_number": s["module_number"],
                "sections": [],
            }
        modules[key]["sections"].append(s)

    modules_list = sorted(
        modules.values(),
        key=lambda m: (m["module_number"] or 99, m["module_title"]),
    )

    return render_template(
        "grade.html",
        grade=grade,
        modules=modules_list,
    )


@app.route("/section/<slug>")
def section_view(slug: str):
    section = fetch_section(slug)
    if section is None:
        abort(404)
    questions = fetch_questions_in_section(section["section_id"])
    return render_template(
        "section.html",
        section=section,
        questions=questions,
    )


@app.route("/dzi")
def dzi_index():
    sources = fetch_dzi_sources()
    return render_template("dzi_index.html", sources=sources)


@app.route("/dzi/source/<source_slug>")
def dzi_source_view(source_slug: str):
    exam = dzi_find_exam(source_slug)
    if exam is None:
        abort(404)
    tasks = fetch_dzi_tasks(int(exam["id"]))
    return render_template("dzi_source.html", exam=exam, tasks=tasks)


@app.route("/admin/open-question-candidates")
def admin_open_question_candidates():
    conn = quiz_db()
    try:
        candidates = fetch_open_question_candidates(conn)
    finally:
        conn.close()

    grouped: dict[str, int] = {}
    for candidate in candidates:
        source_slug = candidate.get("source_slug") or "unknown"
        grouped[source_slug] = grouped.get(source_slug, 0) + 1

    grouped_sources = [
        {"source_slug": source_slug, "count": count}
        for source_slug, count in sorted(grouped.items())
    ]

    return _quiz_render_template(
        "open_question_candidates.html",
        candidates=candidates,
        grouped_sources=grouped_sources,
        total_candidates=len(candidates),
    )


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------

@app.errorhandler(404)
def not_found(e):
    return render_template("404.html"), 404


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------



# === QUIZ PHASE 2 START ===

import csv as _quiz_csv
import hashlib as _quiz_hashlib
import io as _quiz_io
import json as _quiz_json
import os as _quiz_os
import random as _quiz_random
import re as _quiz_re
import sqlite3 as _quiz_sqlite3
import sys as _quiz_sys
import unicodedata as _quiz_unicodedata
from datetime import datetime as _quiz_datetime
from pathlib import Path as _QuizPath

from flask import Response as _quiz_response
from flask import abort as _quiz_abort
from flask import redirect as _quiz_redirect
from flask import request as _quiz_request
from flask import url_for as _quiz_url_for
from flask import render_template as _quiz_render_template


QUIZ_APPROVED_FILTER = "(q.is_ai_generated = 0 OR q.quality_score >= 1.0)"
QUIZ_DB_PATH = _QuizPath(_quiz_os.environ.get("DZI_DB", "data/questions.db"))
QUIZ_VAULT_PATH = _QuizPath(
    _quiz_os.environ.get(
        "DZI_VAULT",
        str(_QuizPath.home() / "dzi-generator" / "vault"),
    )
)


def quiz_db():
    conn = _quiz_sqlite3.connect(str(QUIZ_DB_PATH))
    conn.row_factory = _quiz_sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def quiz_apply_migration() -> None:
    migration = _QuizPath("web/migrations/001_quiz_tables.sql")
    if not migration.exists():
        return
    conn = quiz_db()
    conn.executescript(migration.read_text(encoding="utf-8"))
    conn.commit()
    conn.close()


def quiz_slugify(value: str) -> str:
    bg = {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh",
        "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n",
        "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
        "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sht", "ъ": "a",
        "ь": "", "ю": "yu", "я": "ya",
    }
    out = []
    for ch in (value or "").lower():
        out.append(bg.get(ch, ch))
    value = "".join(out)
    value = _quiz_re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value or "quiz"


def quiz_markdown_escape(value: str | None) -> str:
    return (value or "").replace("\r\n", "\n").replace("\r", "\n")


def quiz_frontmatter(data: dict) -> str:
    lines = ["---"]
    for key, value in data.items():
        if isinstance(value, list):
            inner = ", ".join(str(v) for v in value)
            lines.append(f"{key}: [{inner}]")
        elif value is None:
            lines.append(f"{key}: null")
        elif isinstance(value, str):
            safe = value.replace('"', '\\"')
            lines.append(f'{key}: "{safe}"')
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines)


def quiz_assignment_note_path(assignment_id: int, section_slug: str, created_at: str | None = None) -> _QuizPath:
    date = (created_at or _quiz_datetime.now().isoformat())[:10]
    filename = f"{date}-assignment-{assignment_id}-{quiz_slugify(section_slug)}.md"
    return QUIZ_VAULT_PATH / "Generated" / "Quizzes" / filename


def quiz_attempt_note_path(assignment_id: int, student_name: str, submitted_at: str | None = None) -> _QuizPath:
    date = (submitted_at or _quiz_datetime.now().isoformat())[:10]
    filename = f"{date}-assignment-{assignment_id}-{quiz_slugify(student_name)}.md"
    return QUIZ_VAULT_PATH / "Generated" / "Quizzes" / "attempts" / filename


def quiz_fetch_assignment(conn, assignment_id: int):
    return conn.execute("""
        SELECT
            qa.*,
            cs.section_slug,
            cs.title_bg AS section_title,
            cs.class AS section_class
        FROM quiz_assignments qa
        JOIN curriculum_sections cs ON cs.id = qa.section_id
        WHERE qa.id = ?
    """, (assignment_id,)).fetchone()


QUIZ_VISUAL_DEPENDENT_PATTERNS = (
    "изображението",
    "на изображението",
    "даденото изображение",
    "следното изображение",
    "показаното изображение",
    "диаграмата",
    "диаграма е представена",
    "графиката",
    "разгледайте графиката",
    "таблицата по-долу",
    "фигурата",
    "в диаграмата",
    "показана диаграма",
    "показаната таблица",
    "показаната диаграма",
    "дадената таблица",
    "даден е фрагмент от електронна таблица",
)


def quiz_clean_answer_text(value: object) -> str:
    return str(value or "").strip()


_QUIZ_SMART_QUOTE_TRANSLATION = str.maketrans({
    "‘": "'",
    "’": "'",
    "“": '"',
    "”": '"',
})


def quiz_normalize_text_answer(value: object) -> str:
    if value is None:
        return ""

    text = _quiz_unicodedata.normalize("NFKC", str(value))
    text = text.translate(_QUIZ_SMART_QUOTE_TRANSLATION)
    text = " ".join(text.strip().split())
    return text.casefold()


def _quiz_mapping_get(value: object, key: str, default: object = None) -> object:
    if value is None:
        return default
    if hasattr(value, "keys"):
        try:
            return value[key]  # sqlite3.Row supports mapping-style access without get().
        except (IndexError, KeyError):
            return default
    return getattr(value, key, default)


def _quiz_int_value(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _quiz_answer_values(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if quiz_answer_text_is_real(item)]

    text = str(value).strip()
    if not text:
        return []

    try:
        parsed = _quiz_json.loads(text)
    except (TypeError, ValueError):
        return [text] if quiz_answer_text_is_real(text) else []

    if isinstance(parsed, (list, tuple, set)):
        return [str(item) for item in parsed if quiz_answer_text_is_real(item)]
    if quiz_answer_text_is_real(parsed):
        return [str(parsed)]
    return []


def is_fill_in_question_auto_gradable(question, subquestions) -> bool:
    question_type = _quiz_mapping_get(question, "question_type")
    if question_type not in {"fill_in", "short_answer"}:
        return False

    source_number = _quiz_int_value(_quiz_mapping_get(question, "source_number"))
    task_number = _quiz_int_value(_quiz_mapping_get(question, "task_number"))
    if question_type == "practical" or source_number in {26, 27, 28} or task_number in {26, 27, 28}:
        return False

    if quiz_prompt_needs_visual(_quiz_mapping_get(question, "prompt", "")):
        return False

    rows = list(subquestions or [])
    if not rows:
        return False

    for row in rows:
        accepted = []
        accepted.extend(_quiz_answer_values(_quiz_mapping_get(row, "accepted_answers")))
        accepted.extend(_quiz_answer_values(_quiz_mapping_get(row, "correct_answers")))
        accepted.extend(_quiz_answer_values(_quiz_mapping_get(row, "correct_answer")))
        accepted.extend(_quiz_answer_values(_quiz_mapping_get(row, "answer_alternatives")))
        if not accepted:
            return False

    return True


def _quiz_subquestion_accepted_answers(subquestion) -> list[str]:
    accepted = []
    accepted.extend(_quiz_answer_values(_quiz_mapping_get(subquestion, "accepted_answers")))
    accepted.extend(_quiz_answer_values(_quiz_mapping_get(subquestion, "correct_answers")))
    accepted.extend(_quiz_answer_values(_quiz_mapping_get(subquestion, "correct_answer")))
    accepted.extend(_quiz_answer_values(_quiz_mapping_get(subquestion, "answer_alternatives")))
    return accepted


def _quiz_candidate_grading_mode(subquestions: list[dict]) -> str:
    accepted_sets = [
        {quiz_normalize_text_answer(value) for value in _quiz_subquestion_accepted_answers(row)}
        for row in subquestions
    ]
    if len(accepted_sets) > 1 and all(values and values == accepted_sets[0] for values in accepted_sets):
        return "order_independent"
    return "ordered"


def fetch_open_question_candidates(
    conn,
    *,
    source_slug: str | None = None,
    section_id: int | None = None,
    limit: int | None = None,
) -> list[dict]:
    params = []
    source_filter = ""
    if source_slug is not None:
        source_filter = "AND q.source_exam = ?"
        params.append(source_slug)
    section_join = ""
    section_filter = ""
    if section_id is not None:
        section_join = "JOIN topic_section_assignments tsa ON tsa.topic_id = q.topic_id"
        section_filter = "AND tsa.section_id = ?"
        params.append(section_id)

    question_rows = conn.execute(f"""
        SELECT DISTINCT
            q.id,
            q.source_exam,
            q.source_number,
            q.question_type,
            q.prompt,
            q.has_image,
            q.image_path
        FROM questions q
        {section_join}
        WHERE q.question_type IN ('fill_in', 'short_answer')
          {source_filter}
          {section_filter}
        ORDER BY q.source_exam, q.source_number, q.id
    """, params).fetchall()

    candidates = []
    for question in question_rows:
        subquestions = [
            dict(row)
            for row in conn.execute("""
                SELECT id, subquestion_number, correct_answer, answer_alternatives
                FROM fill_in_subquestions
                WHERE question_id = ?
                ORDER BY subquestion_number
            """, (question["id"],)).fetchall()
        ]
        question_dict = dict(question)
        if not is_fill_in_question_auto_gradable(question_dict, subquestions):
            continue
        candidates.append({
            "question_id": int(question["id"]),
            "source_slug": question["source_exam"],
            "task_number": question["source_number"],
            "grading_mode": _quiz_candidate_grading_mode(subquestions),
            "subquestion_count": len(subquestions),
        })
        if limit is not None and len(candidates) >= limit:
            break

    return candidates


def build_mixed_quiz_plan(
    conn,
    *,
    closed_count: int,
    open_count: int,
    source_slug: str | None = None,
    open_source_slug: str | None = None,
    section_id: int | None = None,
) -> dict:
    if closed_count < 0 or open_count < 0:
        raise ValueError("closed_count and open_count must be non-negative")

    params = []
    source_filter = ""
    if source_slug is not None:
        source_filter = "AND q.source_exam = ?"
        params.append(source_slug)
    section_join = ""
    section_filter = ""
    if section_id is not None:
        section_join = "JOIN topic_section_assignments tsa ON tsa.topic_id = q.topic_id"
        section_filter = "AND tsa.section_id = ?"
        params.append(section_id)

    closed_rows = conn.execute(f"""
        SELECT DISTINCT
            q.id,
            q.source_exam,
            q.source_number,
            q.prompt,
            q.question_type,
            q.has_image,
            q.image_path
        FROM questions q
        {section_join}
        WHERE q.question_type = 'multiple_choice'
          AND {QUIZ_APPROVED_FILTER}
          {source_filter}
          {section_filter}
        ORDER BY q.source_exam, q.source_number, q.id
    """, params).fetchall()

    closed_candidates = []
    for row in closed_rows:
        if not is_quiz_question_eligible(conn, row):
            continue
        closed_candidates.append({
            "question_id": int(row["id"]),
            "source_slug": row["source_exam"],
            "task_number": row["source_number"],
            "question_type": "multiple_choice",
        })

    open_candidates = fetch_open_question_candidates(
        conn,
        source_slug=open_source_slug if open_source_slug is not None else source_slug,
        section_id=section_id,
    )

    return {
        "closed_questions": closed_candidates[:closed_count],
        "open_questions": open_candidates[:open_count],
        "requested_closed_count": closed_count,
        "requested_open_count": open_count,
        "available_closed_count": len(closed_candidates),
        "available_open_count": len(open_candidates),
    }


def insert_quiz_text_answer(
    conn,
    *,
    attempt_id: int,
    question_id: int,
    subquestion_number: int,
    raw_answer: object,
    normalized_answer: str,
    subquestion_id: int | None = None,
    response_order: int | None = None,
    grading_mode: str = "ordered",
    accepted_answers_json: str = "[]",
    matched_answer: str | None = None,
    is_correct: bool = False,
    points_awarded: float = 0,
    points_possible: float = 1,
    grader_version: str | None = None,
) -> int:
    if grading_mode not in {"ordered", "order_independent"}:
        raise ValueError("grading_mode must be 'ordered' or 'order_independent'")
    if normalized_answer is None:
        raise ValueError("normalized_answer is required")

    cur = conn.execute("""
        INSERT INTO quiz_text_answers (
            attempt_id, question_id, subquestion_id, subquestion_number,
            response_order, raw_answer, normalized_answer, grading_mode,
            accepted_answers_json, matched_answer, is_correct,
            points_awarded, points_possible, grader_version
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        attempt_id,
        question_id,
        subquestion_id,
        subquestion_number,
        response_order,
        "" if raw_answer is None else str(raw_answer),
        normalized_answer,
        grading_mode,
        accepted_answers_json,
        matched_answer,
        1 if is_correct else 0,
        points_awarded,
        points_possible,
        grader_version,
    ))
    return int(cur.lastrowid)


def _quiz_slot_items(values) -> list[tuple[int, object]]:
    if hasattr(values, "items"):
        return sorted((int(slot), value) for slot, value in values.items())
    return [(index, value) for index, value in enumerate(values or [], start=1)]


def _quiz_accepted_by_slot(values) -> dict[int, list[str]]:
    accepted = {}
    for slot, slot_values in _quiz_slot_items(values):
        if slot_values is None:
            accepted[slot] = []
        elif isinstance(slot_values, (list, tuple, set)):
            accepted[slot] = [str(value) for value in slot_values if value is not None]
        else:
            accepted[slot] = [str(slot_values)]
    return accepted


def _quiz_text_grade_result(
    subquestion_number: int,
    raw_answer: object,
    accepted_answers: list[str],
    *,
    matched_answer: str | None,
    is_correct: bool,
) -> dict:
    return {
        "subquestion_number": subquestion_number,
        "raw_answer": "" if raw_answer is None else str(raw_answer),
        "normalized_answer": quiz_normalize_text_answer(raw_answer),
        "accepted_answers": accepted_answers,
        "accepted_answers_json": _quiz_json.dumps(accepted_answers, ensure_ascii=False),
        "matched_answer": matched_answer,
        "is_correct": is_correct,
        "points_awarded": 1 if is_correct else 0,
        "points_possible": 1,
    }


def grade_quiz_text_answers(submitted_answers, accepted_answers_by_slot, *, grading_mode: str = "ordered") -> list[dict]:
    if grading_mode not in {"ordered", "order_independent"}:
        raise ValueError("grading_mode must be 'ordered' or 'order_independent'")

    submitted = dict(_quiz_slot_items(submitted_answers))
    accepted_by_slot = _quiz_accepted_by_slot(accepted_answers_by_slot)
    slots = sorted(set(submitted) | set(accepted_by_slot))

    if grading_mode == "ordered":
        results = []
        for slot in slots:
            raw_answer = submitted.get(slot, "")
            accepted_answers = accepted_by_slot.get(slot, [])
            normalized_answer = quiz_normalize_text_answer(raw_answer)
            matched_answer = None
            for accepted_answer in accepted_answers:
                if normalized_answer and normalized_answer == quiz_normalize_text_answer(accepted_answer):
                    matched_answer = accepted_answer
                    break
            results.append(_quiz_text_grade_result(
                slot,
                raw_answer,
                accepted_answers,
                matched_answer=matched_answer,
                is_correct=matched_answer is not None,
            ))
        return results

    remaining = {}
    for accepted_answers in accepted_by_slot.values():
        for accepted_answer in accepted_answers:
            normalized = quiz_normalize_text_answer(accepted_answer)
            if not normalized:
                continue
            remaining.setdefault(normalized, []).append(accepted_answer)

    results = []
    for slot in slots:
        raw_answer = submitted.get(slot, "")
        accepted_answers = accepted_by_slot.get(slot, [])
        normalized_answer = quiz_normalize_text_answer(raw_answer)
        matched_answer = None
        if normalized_answer and remaining.get(normalized_answer):
            matched_answer = remaining[normalized_answer].pop(0)
        results.append(_quiz_text_grade_result(
            slot,
            raw_answer,
            accepted_answers,
            matched_answer=matched_answer,
            is_correct=matched_answer is not None,
        ))
    return results


def record_quiz_text_answers(
    conn,
    *,
    attempt_id: int,
    question_id: int,
    submitted_answers,
    accepted_answers_by_slot,
    grading_mode: str = "ordered",
    subquestion_ids_by_slot=None,
    grader_version: str | None = None,
) -> list[dict]:
    graded_results = grade_quiz_text_answers(
        submitted_answers,
        accepted_answers_by_slot,
        grading_mode=grading_mode,
    )
    subquestion_ids = dict(_quiz_slot_items(subquestion_ids_by_slot)) if subquestion_ids_by_slot else {}

    recorded = []
    for result in graded_results:
        subquestion_number = int(result["subquestion_number"])
        row_id = insert_quiz_text_answer(
            conn,
            attempt_id=attempt_id,
            question_id=question_id,
            subquestion_id=subquestion_ids.get(subquestion_number),
            subquestion_number=subquestion_number,
            raw_answer=result["raw_answer"],
            normalized_answer=result["normalized_answer"],
            grading_mode=grading_mode,
            accepted_answers_json=result["accepted_answers_json"],
            matched_answer=result["matched_answer"],
            is_correct=result["is_correct"],
            points_awarded=result["points_awarded"],
            points_possible=result["points_possible"],
            grader_version=grader_version,
        )
        recorded.append({**result, "id": row_id})
    return recorded


def fetch_quiz_text_answers_for_attempt(conn, attempt_id: int, *, question_id: int | None = None) -> list[dict]:
    params = [attempt_id]
    question_filter = ""
    if question_id is not None:
        question_filter = "AND question_id = ?"
        params.append(question_id)

    rows = conn.execute(f"""
        SELECT
            id,
            attempt_id,
            question_id,
            subquestion_id,
            subquestion_number,
            raw_answer,
            normalized_answer,
            grading_mode,
            accepted_answers_json,
            matched_answer,
            is_correct,
            points_awarded,
            points_possible,
            grader_version,
            teacher_override,
            teacher_note
        FROM quiz_text_answers
        WHERE attempt_id = ?
          {question_filter}
        ORDER BY question_id, subquestion_number
    """, params).fetchall()
    return [dict(row) for row in rows]


def quiz_text_answer_informational_subtotal(rows) -> dict:
    awarded = 0.0
    possible = 0.0
    count = 0
    for row in rows or []:
        count += 1
        try:
            row_possible = float(row.get("points_possible") or 0)
        except (TypeError, ValueError):
            row_possible = 0.0
        possible += row_possible

        override = row.get("teacher_override")
        if override == 1:
            awarded += row_possible
        elif override == 0 and row.get("teacher_note"):
            awarded += 0.0
        else:
            try:
                awarded += float(row.get("points_awarded") or 0)
            except (TypeError, ValueError):
                pass

    return {
        "awarded": awarded,
        "possible": possible,
        "count": count,
    }


def quiz_combined_score_summary(attempt, open_subtotal: dict | None, *, enabled: bool) -> dict | None:
    if not enabled or not open_subtotal:
        return None
    return {
        "mc_awarded": float(attempt["score_correct"] or 0),
        "mc_possible": float(attempt["score_total"] or 0),
        "open_awarded": float(open_subtotal.get("awarded") or 0),
        "open_possible": float(open_subtotal.get("possible") or 0),
        "combined_awarded": float(attempt["score_correct"] or 0) + float(open_subtotal.get("awarded") or 0),
        "combined_possible": float(attempt["score_total"] or 0) + float(open_subtotal.get("possible") or 0),
    }


def quiz_record_planned_open_text_answers(
    conn,
    *,
    attempt_id: int,
    question_ids: list[int],
    open_question_ids: list[int],
    form,
) -> None:
    planned_open_ids = set()
    renderable_ids = {int(qid) for qid in question_ids}
    for qid in open_question_ids or []:
        try:
            if isinstance(qid, bool):
                raise ValueError
            planned_qid = int(qid)
        except (TypeError, ValueError):
            continue
        if planned_qid in renderable_ids:
            planned_open_ids.add(planned_qid)

    for qid in question_ids:
        if int(qid) not in planned_open_ids:
            continue

        subquestions = conn.execute("""
            SELECT id, subquestion_number, correct_answer, answer_alternatives
            FROM fill_in_subquestions
            WHERE question_id = ?
            ORDER BY subquestion_number
        """, (qid,)).fetchall()
        submitted_answers = {}
        accepted_answers_by_slot = {}
        subquestion_ids_by_slot = {}

        for row in subquestions:
            slot = int(row["subquestion_number"])
            field_name = f"open_q_{qid}_{slot}"
            if field_name in form:
                submitted_answers[slot] = form.get(field_name, "")
            accepted_answers_by_slot[slot] = _quiz_subquestion_accepted_answers(row)
            subquestion_ids_by_slot[slot] = int(row["id"])

        if submitted_answers:
            record_quiz_text_answers(
                conn,
                attempt_id=attempt_id,
                question_id=int(qid),
                submitted_answers=submitted_answers,
                accepted_answers_by_slot=accepted_answers_by_slot,
                grading_mode=_quiz_candidate_grading_mode([dict(row) for row in subquestions]),
                subquestion_ids_by_slot=subquestion_ids_by_slot,
            )


def quiz_answer_text_is_real(value: object) -> bool:
    text = quiz_clean_answer_text(value)
    return bool(text) and text not in {"-", "—", "[]"}


def quiz_prompt_needs_visual(prompt: str | None) -> bool:
    text = (prompt or "").lower()
    return any(pattern in text for pattern in QUIZ_VISUAL_DEPENDENT_PATTERNS)


def quiz_path_exists(path_value: object) -> bool:
    raw_path = quiz_clean_answer_text(path_value)
    if not raw_path or raw_path in {"-", "—"}:
        return False

    path = _QuizPath(raw_path)
    if path.is_absolute():
        return path.exists()
    return (PROJECT_ROOT / path).exists()


def question_has_usable_visual(conn, question) -> bool:
    if quiz_path_exists(question["image_path"]):
        return True

    if int(question["has_image"] or 0) and quiz_path_exists(question["image_path"]):
        return True

    asset_rows = conn.execute("""
        SELECT a.local_path
        FROM asset_links al
        JOIN assets a ON a.id = al.asset_id
        WHERE (
            (al.owner_type IN ('question', 'questions') AND al.owner_id = ?)
            OR (
                al.owner_type = 'exam_task'
                AND al.owner_id IN (
                    SELECT task_id
                    FROM exam_task_questions
                    WHERE question_id = ?
                )
            )
          )
          AND a.asset_type IN ('image', 'pdf_crop')
    """, (question["id"], question["id"])).fetchall()

    return any(quiz_path_exists(row["local_path"]) for row in asset_rows)


def quiz_multiple_choice_is_eligible(conn, question_id: int) -> bool:
    options = conn.execute("""
        SELECT option_text, is_correct
        FROM multiple_choice_options
        WHERE question_id = ?
        ORDER BY option_letter
    """, (question_id,)).fetchall()

    if len(options) != 4:
        return False

    if any(not quiz_answer_text_is_real(row["option_text"]) for row in options):
        return False

    correct_options = [row for row in options if int(row["is_correct"] or 0) == 1]
    if len(correct_options) != 1:
        return False

    return quiz_answer_text_is_real(correct_options[0]["option_text"])


def quiz_fill_in_is_eligible(conn, question_id: int) -> bool:
    rows = conn.execute("""
        SELECT correct_answer
        FROM fill_in_subquestions
        WHERE question_id = ?
        ORDER BY subquestion_number
    """, (question_id,)).fetchall()

    if not rows:
        return False

    return all(quiz_answer_text_is_real(row["correct_answer"]) for row in rows)


def is_quiz_question_eligible(conn, question) -> bool:
    if quiz_prompt_needs_visual(question["prompt"]) and not question_has_usable_visual(conn, question):
        return False

    question_type = question["question_type"]
    if question_type == "multiple_choice":
        return quiz_multiple_choice_is_eligible(conn, int(question["id"]))

    if question_type in {"fill_in", "short_answer"}:
        return quiz_fill_in_is_eligible(conn, int(question["id"]))

    return False


def quiz_section_question_ids(conn, section_id: int) -> list[int]:
    rows = conn.execute(f"""
        SELECT DISTINCT q.id, q.prompt, q.question_type, q.has_image, q.image_path
        FROM questions q
        JOIN topic_section_assignments tsa ON tsa.topic_id = q.topic_id
        WHERE tsa.section_id = ?
          AND {QUIZ_APPROVED_FILTER}
          AND q.question_type = 'multiple_choice'
        ORDER BY q.id
    """, (section_id,)).fetchall()
    return [int(r["id"]) for r in rows if is_quiz_question_eligible(conn, r)]


def fetch_dzi_pool_health(source_slug: str = "may_2025_v2") -> dict | None:
    exam = dzi_find_exam(source_slug)
    if exam is None:
        return None

    conn = quiz_db()
    try:
        rows = conn.execute(f"""
            SELECT DISTINCT q.id, q.prompt, q.question_type, q.has_image, q.image_path
            FROM exam_tasks et
            JOIN exam_task_questions etq
              ON etq.task_id = et.id
             AND etq.role = 'primary'
            JOIN questions q
              ON q.id = etq.question_id
            WHERE et.exam_id = ?
              AND et.task_number BETWEEN 1 AND 25
              AND {QUIZ_APPROVED_FILTER}
            ORDER BY et.task_number, q.id
        """, (exam["id"],)).fetchall()

        usable_count = 0
        not_yet_supported_count = 0
        invalid_mc_count = 0

        for row in rows:
            if row["question_type"] != "multiple_choice":
                not_yet_supported_count += 1
            elif is_quiz_question_eligible(conn, row):
                usable_count += 1
            else:
                invalid_mc_count += 1

        imported_count = len(rows)
        return {
            "source_slug": source_slug,
            "imported_count": imported_count,
            "usable_count": usable_count,
            "filtered_count": imported_count - usable_count,
            "not_yet_supported_count": not_yet_supported_count,
            "invalid_mc_count": invalid_mc_count,
        }
    finally:
        conn.close()


def quiz_seed(assignment_id: int, student_name: str) -> str:
    raw = f"{assignment_id}|{student_name.strip().lower()}".encode("utf-8")
    return _quiz_hashlib.sha256(raw).hexdigest()


def quiz_pick_questions(conn, assignment, student_name: str) -> tuple[str, list[int]]:
    seed = quiz_seed(int(assignment["id"]), student_name)
    ids = quiz_section_question_ids(conn, int(assignment["section_id"]))
    rng = _quiz_random.Random(seed)
    rng.shuffle(ids)
    count = min(int(assignment["question_count"]), len(ids))
    return seed, ids[:count]


def quiz_shuffle_options(seed: str, question_id: int, options: list[dict]) -> list[dict]:
    shuffled = [dict(o) for o in options]
    rng = _quiz_random.Random(f"{seed}|options|{question_id}")
    rng.shuffle(shuffled)

    display_letters = ["А", "Б", "В", "Г", "Д", "Е"]
    for i, opt in enumerate(shuffled):
        opt["display_letter"] = display_letters[i] if i < len(display_letters) else str(i + 1)

    return shuffled


def quiz_load_questions(conn, question_ids: list[int], seed: str, include_correct: bool = False) -> list[dict]:
    if not question_ids:
        return []

    placeholders = ",".join("?" for _ in question_ids)
    q_rows = conn.execute(f"""
        SELECT id, prompt, question_type, difficulty
        FROM questions
        WHERE id IN ({placeholders})
    """, question_ids).fetchall()

    by_id = {int(r["id"]): dict(r) for r in q_rows}
    result = []

    for qid in question_ids:
        q = by_id.get(int(qid))
        if not q:
            continue

        if q["question_type"] in {"fill_in", "short_answer"}:
            sub_rows = conn.execute("""
                SELECT id, subquestion_number
                FROM fill_in_subquestions
                WHERE question_id = ?
                ORDER BY subquestion_number
            """, (qid,)).fetchall()
            q["options"] = []
            q["subquestions"] = [dict(row) for row in sub_rows]
            result.append(q)
            continue

        if include_correct:
            opt_rows = conn.execute("""
                SELECT id, option_letter, option_text, is_correct
                FROM multiple_choice_options
                WHERE question_id = ?
                ORDER BY option_letter
            """, (qid,)).fetchall()
        else:
            opt_rows = conn.execute("""
                SELECT id, option_letter, option_text
                FROM multiple_choice_options
                WHERE question_id = ?
                ORDER BY option_letter
            """, (qid,)).fetchall()

        q["options"] = quiz_shuffle_options(seed, int(qid), [dict(o) for o in opt_rows])
        result.append(q)

    return result


STALE_ATTEMPT_MESSAGE = (
    "Този тест съдържа стари или непълни въпроси и не може да бъде показан коректно. "
    "Моля, създайте нов тест."
)


def quiz_parse_attempt_question_plan(raw_value) -> dict:
    try:
        parsed = _quiz_json.loads(raw_value or "[]")
    except (_quiz_json.JSONDecodeError, TypeError):
        parsed = []

    if isinstance(parsed, dict):
        raw_question_ids = parsed.get("question_ids", [])
        raw_open_ids = parsed.get("open_question_ids", [])
        mixed_open_enabled = bool(parsed.get("mixed_open_enabled"))
        include_open_answers_in_final_score = bool(parsed.get("include_open_answers_in_final_score"))
    else:
        raw_question_ids = parsed
        raw_open_ids = []
        mixed_open_enabled = False
        include_open_answers_in_final_score = False

    return {
        "question_ids": raw_question_ids if isinstance(raw_question_ids, list) else [],
        "open_question_ids": raw_open_ids if isinstance(raw_open_ids, list) else [],
        "mixed_open_enabled": mixed_open_enabled,
        "include_open_answers_in_final_score": include_open_answers_in_final_score,
    }


def quiz_parse_assignment_question_plan(raw_value) -> dict | None:
    if raw_value is None or str(raw_value).strip() == "":
        return None

    plan = quiz_parse_attempt_question_plan(raw_value)
    if not plan["mixed_open_enabled"]:
        return None
    if not plan["question_ids"] or not plan["open_question_ids"]:
        return None

    planned_ids = set()
    question_ids = []
    for qid in plan["question_ids"]:
        try:
            if isinstance(qid, bool):
                raise ValueError
            parsed_qid = int(qid)
            planned_ids.add(parsed_qid)
            question_ids.append(parsed_qid)
        except (TypeError, ValueError):
            return None

    open_question_ids = []
    for qid in plan["open_question_ids"]:
        try:
            if isinstance(qid, bool):
                raise ValueError
            open_qid = int(qid)
        except (TypeError, ValueError):
            return None
        if open_qid not in planned_ids:
            return None
        open_question_ids.append(open_qid)

    return {
        "mixed_open_enabled": True,
        "question_ids": question_ids,
        "open_question_ids": open_question_ids,
        "include_open_answers_in_final_score": bool(plan["include_open_answers_in_final_score"]),
    }


def quiz_assignment_mixed_status(raw_value) -> dict:
    plan = quiz_parse_assignment_question_plan(raw_value)
    if plan is not None:
        return {
            "is_mixed": True,
            "open_count": len(plan["open_question_ids"]),
            "combined_score": bool(plan["include_open_answers_in_final_score"]),
            "plan_invalid": False,
        }
    if raw_value is None:
        raw_str = ""
    elif isinstance(raw_value, str):
        raw_str = raw_value.strip()
    else:
        raw_str = str(raw_value).strip()
    return {
        "is_mixed": False,
        "open_count": 0,
        "combined_score": False,
        "plan_invalid": bool(raw_str),
    }


def filter_renderable_attempt_question_ids(
    conn,
    question_ids,
    *,
    open_question_ids: list[int] | None = None,
) -> tuple[list[int], int]:
    allowed_open_ids = set()
    for qid in open_question_ids or []:
        try:
            if isinstance(qid, bool):
                raise ValueError
            allowed_open_ids.add(int(qid))
        except (TypeError, ValueError):
            pass

    parsed_ids = []
    skipped = 0
    for qid in question_ids or []:
        try:
            if isinstance(qid, bool):
                raise ValueError
            parsed_ids.append(int(qid))
        except (TypeError, ValueError):
            skipped += 1

    if not parsed_ids:
        return [], skipped

    unique_ids = list(dict.fromkeys(parsed_ids))
    placeholders = ",".join("?" for _ in unique_ids)
    rows = conn.execute(f"""
        SELECT id, prompt, question_type, has_image, image_path
        FROM questions
        WHERE id IN ({placeholders})
    """, unique_ids).fetchall()
    by_id = {int(row["id"]): row for row in rows}

    valid_ids = []
    for qid in parsed_ids:
        question = by_id.get(qid)
        if not question:
            skipped += 1
            continue

        if question["question_type"] == "multiple_choice" and is_quiz_question_eligible(conn, question):
            valid_ids.append(qid)
        elif (
            qid in allowed_open_ids
            and question["question_type"] in {"fill_in", "short_answer"}
            and is_fill_in_question_auto_gradable(
                question,
                conn.execute("""
                    SELECT id, subquestion_number, correct_answer, answer_alternatives
                    FROM fill_in_subquestions
                    WHERE question_id = ?
                    ORDER BY subquestion_number
                """, (qid,)).fetchall(),
            )
        ):
            valid_ids.append(qid)
        else:
            skipped += 1

    return valid_ids, skipped


def quiz_time_taken_seconds(attempt) -> int:
    try:
        started = _quiz_datetime.fromisoformat(attempt["started_at"])
        ended_raw = attempt["submitted_at"] or _quiz_datetime.now().isoformat(timespec="seconds")
        ended = _quiz_datetime.fromisoformat(ended_raw)
        return max(0, int((ended - started).total_seconds()))
    except Exception:
        return 0


def quiz_format_duration(seconds: int) -> str:
    minutes = seconds // 60
    rest = seconds % 60
    if minutes:
        return f"{minutes} мин. {rest} сек."
    return f"{rest} сек."


def quiz_remaining_seconds(assignment, attempt) -> int | None:
    limit = assignment["time_limit_minutes"]
    if not limit:
        return None

    try:
        started = _quiz_datetime.fromisoformat(attempt["started_at"])
        elapsed = int((_quiz_datetime.now() - started).total_seconds())
        return max(0, int(limit) * 60 - elapsed)
    except Exception:
        return int(limit) * 60


def quiz_write_assignment_note(assignment_id: int, base_url: str) -> None:
    try:
        conn = quiz_db()
        a = quiz_fetch_assignment(conn, assignment_id)
        if not a:
            conn.close()
            return

        attempts = conn.execute("""
            SELECT *
            FROM quiz_attempts
            WHERE assignment_id = ?
            ORDER BY started_at
        """, (assignment_id,)).fetchall()

        submitted = [x for x in attempts if x["submitted_at"]]
        avg = None
        if submitted:
            percentages = [
                (float(x["score_correct"] or 0) / max(1, int(x["score_total"] or 1))) * 100
                for x in submitted
            ]
            avg = round(sum(percentages) / len(percentages), 1)

        quiz_link = base_url.rstrip("/") + _quiz_url_for("quiz_start", assignment_id=assignment_id)
        path = quiz_assignment_note_path(assignment_id, a["section_slug"], a["created_at"])

        rows = []
        for x in attempts:
            attempt_path = quiz_attempt_note_path(assignment_id, x["student_name"], x["submitted_at"])
            # Use basename without .md so Obsidian wikilink resolution finds the file
            # regardless of where it sits in the vault. Filesystem-relative paths like
            # "attempts/foo.md" don't resolve in Obsidian.
            rel = attempt_path.stem
            score = f'{x["score_correct"]}/{x["score_total"]}' if x["submitted_at"] else "в процес"
            rows.append(
                f'| [[{rel}|{x["student_name"]}]] | {score} | {x["started_at"]} | {x["submitted_at"] or "—"} |'
            )

        fm = quiz_frontmatter({
            "title": a["title_bg"],
            "type": "quiz_assignment",
            "section_slug": a["section_slug"],
            "class": a["section_class"],
            "question_count": a["question_count"],
            "time_limit_minutes": a["time_limit_minutes"],
            "created_at": a["created_at"],
            "attempts_count": len(attempts),
            "average_score": avg,
            "status": "open",
            "tags": ["quiz", "assignment"],
        })

        body = f"""{fm}

# {a["title_bg"]}

**Секция:** {a["section_title"]}
**Брой въпроси:** {a["question_count"]}
**Време:** {str(a["time_limit_minutes"]) + " минути" if a["time_limit_minutes"] else "без ограничение"}
**Споделяем линк:** {quiz_link}

## Опити

| Ученик | Резултат | Начало | Предадено |
|--------|----------|--------|-----------|
{chr(10).join(rows) if rows else "| — | — | — | — |"}
"""

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        conn.close()
    except Exception as e:
        print(f"[quiz vault] assignment note failed: {e}", file=_quiz_sys.stderr)


def quiz_write_attempt_note(attempt_id: int) -> None:
    try:
        conn = quiz_db()
        attempt = conn.execute("SELECT * FROM quiz_attempts WHERE id = ?", (attempt_id,)).fetchone()
        if not attempt:
            conn.close()
            return

        assignment = quiz_fetch_assignment(conn, int(attempt["assignment_id"]))
        if not assignment:
            conn.close()
            return

        seed = attempt["seed"]
        qids = _quiz_json.loads(attempt["question_ids_json"])
        qids, _skipped_count = filter_renderable_attempt_question_ids(conn, qids)
        questions = quiz_load_result_questions(conn, attempt, qids, seed)

        path = quiz_attempt_note_path(int(assignment["id"]), attempt["student_name"], attempt["submitted_at"])
        seconds = quiz_time_taken_seconds(attempt)
        pct = round((float(attempt["score_correct"] or 0) / max(1, int(attempt["score_total"] or 1))) * 100, 1)

        fm = quiz_frontmatter({
            "title": f'{attempt["student_name"]} — {assignment["title_bg"]}',
            "type": "quiz_attempt",
            "assignment_id": assignment["id"],
            "student_name": attempt["student_name"],
            "score_correct": attempt["score_correct"],
            "score_total": attempt["score_total"],
            "time_taken_seconds": seconds,
            "submitted_at": attempt["submitted_at"],
            "tags": ["quiz", "attempt"],
        })

        parts = [fm, "", "# Резултат", ""]
        parts.append(f'**Точен резултат:** {attempt["score_correct"]}/{attempt["score_total"]} ({pct}%)')
        parts.append(f"**Време:** {quiz_format_duration(seconds)}")
        parts.append("")
        parts.append("## Въпроси")
        parts.append("")

        for i, q in enumerate(questions, 1):
            parts.append(f"### Q{i}: {quiz_markdown_escape(q['prompt'])}")
            parts.append("")
            parts.append(f"- Отговор на ученика: {quiz_markdown_escape(q['chosen_text'] or 'няма отговор')}")
            parts.append(f"- Правилен отговор: {quiz_markdown_escape(q['correct_text'])}")
            parts.append(f"- Резултат: {'✓ вярно' if q['is_correct'] else '✗ грешно'}")
            parts.append("")

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(parts), encoding="utf-8")
        conn.close()
    except Exception as e:
        print(f"[quiz vault] attempt note failed: {e}", file=_quiz_sys.stderr)


def quiz_load_result_questions(conn, attempt, question_ids: list[int], seed: str) -> list[dict]:
    questions = quiz_load_questions(conn, question_ids, seed, include_correct=True)
    answer_rows = conn.execute("""
        SELECT question_id, chosen_letter, is_correct
        FROM quiz_answers
        WHERE attempt_id = ?
    """, (attempt["id"],)).fetchall()
    answers = {int(r["question_id"]): r for r in answer_rows}

    for q in questions:
        ans = answers.get(int(q["id"]))
        chosen_letter = ans["chosen_letter"] if ans else None

        chosen_text = None
        correct_text = None

        for opt in q["options"]:
            if opt["option_letter"] == chosen_letter:
                chosen_text = opt["option_text"]
            if int(opt.get("is_correct") or 0) == 1:
                correct_text = opt["option_text"]

        q["chosen_letter"] = chosen_letter
        q["chosen_text"] = chosen_text
        q["correct_text"] = correct_text or "—"
        q["is_correct"] = bool(ans and ans["is_correct"])

    return questions


quiz_apply_migration()





@app.route("/teacher/dzi-training", methods=["GET", "POST"])
def teacher_dzi_training():
    import json
    import subprocess
    import sys
    from pathlib import Path

    project_root = Path(__file__).resolve().parents[1]
    config_path = project_root / "data" / "dzi_training" / "class_mix.json"
    generator_path = project_root / "src" / "generate_dzi_training_set.py"
    sets_dir = project_root / "vault" / "Generated" / "DZI-Training" / "sets"

    generated_output = None
    generated_ok = None
    dzi_generation_error = None
    dzi_pool_health = fetch_dzi_pool_health("may_2025_v2")

    def short_generator_output(*parts) -> str:
        text = "\n".join(str(part).strip() for part in parts if part).strip()
        if len(text) > 1200:
            return text[:1200].rstrip() + "\n..."
        return text

    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception as exc:
            config = {
                "status": "error",
                "classes": {},
                "error": str(exc),
            }
    else:
        config = {
            "status": "missing",
            "classes": {},
            "error": f"Missing config: {config_path}",
        }

    if _quiz_request.method == "POST":
        raw_count = (_quiz_request.form.get("count") or "28").strip()
        raw_seed = (_quiz_request.form.get("seed") or "").strip()
        raw_name = (_quiz_request.form.get("name") or "").strip()

        try:
            count = int(raw_count)
        except ValueError:
            count = 28

        count = max(1, min(count, 100))
        name = raw_name or f"ДЗИ тренировъчен комплект — {count} въпроса"

        if not generator_path.exists():
            generated_ok = False
            dzi_generation_error = (
                "Не успях да генерирам тренировъчен ДЗИ комплект. "
                "Опитай пак след малко или провери входните данни."
            )
            generated_output = "Скриптът за генериране не е намерен."
        else:
            cmd = [
                sys.executable,
                str(generator_path),
                "--count",
                str(count),
                "--name",
                name,
            ]

            if raw_seed:
                cmd.extend(["--seed", raw_seed])

            try:
                proc = subprocess.run(
                    cmd,
                    cwd=str(project_root),
                    text=True,
                    capture_output=True,
                    timeout=60,
                )
            except subprocess.TimeoutExpired as exc:
                generated_ok = False
                dzi_generation_error = (
                    "Не успях да генерирам тренировъчен ДЗИ комплект. "
                    "Опитай пак след малко или провери входните данни."
                )
                generated_output = short_generator_output(exc.stdout, exc.stderr)
            else:
                generated_ok = proc.returncode == 0
                generated_output = (proc.stdout or "") + (proc.stderr or "")
                if proc.returncode != 0:
                    dzi_generation_error = (
                        "Не успях да генерирам тренировъчен ДЗИ комплект. "
                        "Опитай пак след малко или провери входните данни."
                    )
                    generated_output = short_generator_output(proc.stdout, proc.stderr)

    recent_sets = []
    if sets_dir.exists():
        for path in sorted(sets_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[:20]:
            recent_sets.append({
                "name": path.name,
                "relative_path": str(path.relative_to(project_root / "vault")),
                "size": path.stat().st_size,
                "modified": path.stat().st_mtime,
            })

    return _quiz_render_template(
        "teacher_dzi_training.html",
        config=config,
        recent_sets=recent_sets,
        generated_output=generated_output,
        generated_ok=generated_ok,
        dzi_generation_error=dzi_generation_error,
        dzi_pool_health=dzi_pool_health,
    )


@app.route("/teacher")
def teacher_dashboard():
    conn = quiz_db()

    stats = conn.execute("""
        SELECT
            (SELECT COUNT(*) FROM quiz_assignments) AS assignments_count,
            (SELECT COUNT(*) FROM quiz_attempts) AS attempts_count,
            (SELECT COUNT(*) FROM quiz_attempts WHERE submitted_at IS NOT NULL) AS submitted_count,
            (SELECT COUNT(*) FROM quiz_attempts WHERE submitted_at IS NULL) AS unfinished_count,
            (
              SELECT AVG(100.0 * score_correct / score_total)
              FROM quiz_attempts
              WHERE submitted_at IS NOT NULL
                AND score_total IS NOT NULL
                AND score_total > 0
            ) AS avg_percent
    """).fetchone()

    recent_assignment_rows = conn.execute("""
        SELECT
            qa.id,
            qa.title_bg,
            qa.question_count,
            qa.time_limit_minutes,
            qa.question_plan_json,
            qa.created_at,
            cs.class,
            cs.section_slug,
            cs.title_bg AS section_title,
            COUNT(DISTINCT qt.id) AS attempts_count,
            SUM(CASE WHEN qt.submitted_at IS NOT NULL THEN 1 ELSE 0 END) AS submitted_count
        FROM quiz_assignments qa
        JOIN curriculum_sections cs ON cs.id = qa.section_id
        LEFT JOIN quiz_attempts qt ON qt.assignment_id = qa.id
        GROUP BY qa.id
        ORDER BY qa.created_at DESC, qa.id DESC
        LIMIT 5
    """).fetchall()

    recent_assignments = []
    for row in recent_assignment_rows:
        row_dict = dict(row)
        row_dict["mixed_status"] = quiz_assignment_mixed_status(row_dict.pop("question_plan_json", None))
        recent_assignments.append(row_dict)

    recent_attempts = conn.execute("""
        SELECT
            qt.id,
            qt.assignment_id,
            qt.student_name,
            qt.started_at,
            qt.submitted_at,
            qt.score_correct,
            qt.score_total,
            qa.title_bg AS assignment_title,
            CASE
              WHEN qt.score_total IS NOT NULL AND qt.score_total > 0
              THEN ROUND(100.0 * qt.score_correct / qt.score_total, 1)
              ELSE NULL
            END AS percent
        FROM quiz_attempts qt
        JOIN quiz_assignments qa ON qa.id = qt.assignment_id
        ORDER BY qt.started_at DESC, qt.id DESC
        LIMIT 8
    """).fetchall()

    conn.close()

    return _quiz_render_template(
        "teacher_dashboard.html",
        stats=stats,
        recent_assignments=recent_assignments,
        recent_attempts=recent_attempts,
    )


@app.route("/teacher/assignments")
def teacher_assignments():
    conn = quiz_db()

    type_filter = (_quiz_request.args.get("type") or "all").strip().lower()
    if type_filter not in {"all", "mc", "mixed"}:
        type_filter = "all"

    where_clause = ""
    if type_filter == "mc":
        where_clause = "WHERE (qa.question_plan_json IS NULL OR TRIM(qa.question_plan_json) = '')"
    elif type_filter == "mixed":
        where_clause = "WHERE qa.question_plan_json IS NOT NULL AND TRIM(qa.question_plan_json) <> ''"

    assignment_rows = conn.execute(f"""
        SELECT
            qa.id,
            qa.section_id,
            qa.title_bg,
            qa.question_count,
            qa.time_limit_minutes,
            qa.question_plan_json,
            qa.created_at,
            cs.class,
            cs.title_bg AS section_title,
            cs.section_slug,
            COUNT(DISTINCT qt.id) AS attempts_count,
            SUM(CASE WHEN qt.submitted_at IS NOT NULL THEN 1 ELSE 0 END) AS submitted_count
        FROM quiz_assignments qa
        JOIN curriculum_sections cs ON cs.id = qa.section_id
        LEFT JOIN quiz_attempts qt ON qt.assignment_id = qa.id
        {where_clause}
        GROUP BY qa.id
        ORDER BY qa.created_at DESC, qa.id DESC
    """).fetchall()

    assignments = []
    for row in assignment_rows:
        row_dict = dict(row)
        row_dict["mixed_status"] = quiz_assignment_mixed_status(row_dict.pop("question_plan_json", None))
        assignments.append(row_dict)

    conn.close()

    return _quiz_render_template(
        "teacher_assignments.html",
        assignments=assignments,
        type_filter=type_filter,
    )


@app.route("/teacher/new", methods=["GET", "POST"])
def teacher_new():
    conn = quiz_db()
    preselected_section_id = None
    mixed_planning_result = None
    teacher_new_error = None
    form_values = {
        "question_count": 10,
        "time_limit_minutes": "",
        "include_open_questions": False,
        "open_count": 0,
        "source_slug": "",
    }

    if _quiz_request.method == "POST":
        raw_section_id = (_quiz_request.form.get("section_id") or "").strip()
        raw_question_count = (_quiz_request.form.get("question_count") or "").strip()
        raw_limit = (_quiz_request.form.get("time_limit_minutes") or "").strip()
        include_open_questions = bool(_quiz_request.form.get("include_open_questions"))
        include_open_answers_in_final_score = (
            bool(_quiz_request.form.get("include_open_answers_in_final_score"))
            and is_admin_authenticated()
        )
        raw_open_count = (_quiz_request.form.get("open_count") or "").strip()
        source_slug = (_quiz_request.form.get("source_slug") or "").strip()
        section_id = None
        requested_count = None
        time_limit = None
        open_count = None
        form_values = {
            "question_count": raw_question_count or 10,
            "time_limit_minutes": raw_limit,
            "include_open_questions": include_open_questions,
            "open_count": raw_open_count or 0,
            "source_slug": source_slug,
        }

        try:
            section_id = int(raw_section_id)
            requested_count = int(raw_question_count or 10)
            open_count = int(raw_open_count or 0)
            if raw_limit:
                time_limit = int(raw_limit)
        except ValueError:
            teacher_new_error = "invalid_number"
        else:
            if section_id <= 0 or requested_count < 1 or open_count < 0:
                teacher_new_error = "invalid_number"
            elif time_limit is not None and (
                time_limit < 1 or time_limit > QUIZ_TIME_LIMIT_MAX_MINUTES
            ):
                teacher_new_error = "time_out_of_range"
            elif open_count > requested_count:
                teacher_new_error = "open_count_exceeds_total"

        if teacher_new_error is None:
            form_values = {
                "question_count": requested_count,
                "time_limit_minutes": raw_limit,
                "include_open_questions": include_open_questions,
                "open_count": open_count,
                "source_slug": source_slug,
            }

            section = conn.execute("""
                SELECT id, title_bg
                FROM curriculum_sections
                WHERE id = ?
            """, (section_id,)).fetchone()

            if not section:
                conn.close()
                _quiz_abort(404)

            available = len(quiz_section_question_ids(conn, section_id))
            if available <= 0:
                conn.close()
                return _quiz_redirect(_quiz_url_for("teacher_new"))

            question_count = max(1, min(requested_count, available))

            if open_count > 0:
                closed_count = requested_count - open_count
                plan = build_mixed_quiz_plan(
                    conn,
                    closed_count=closed_count,
                    open_count=open_count,
                    open_source_slug=source_slug or None,
                    section_id=section_id,
                )
                closed_question_ids = [int(question["question_id"]) for question in plan["closed_questions"]]
                open_question_ids = [int(question["question_id"]) for question in plan["open_questions"]]
                mixed_planning_result = {
                    "planning_only": True,
                    "source_slug": source_slug or "всички",
                    "closed_count": len(plan["closed_questions"]),
                    "open_count": len(plan["open_questions"]),
                    "requested_closed_count": plan["requested_closed_count"],
                    "requested_open_count": plan["requested_open_count"],
                    "available_closed_count": plan["available_closed_count"],
                    "available_open_count": plan["available_open_count"],
                    "closed_shortfall": max(0, plan["requested_closed_count"] - plan["available_closed_count"]),
                    "open_shortfall": max(0, plan["requested_open_count"] - plan["available_open_count"]),
                }
                if mixed_planning_result["open_shortfall"]:
                    teacher_new_error = "insufficient_open_questions"
                    preselected_section_id = section_id
                elif mixed_planning_result["closed_shortfall"]:
                    teacher_new_error = "insufficient_closed_questions"
                    preselected_section_id = section_id
                else:
                    question_plan = {
                        "mixed_open_enabled": True,
                        "question_ids": closed_question_ids + open_question_ids,
                        "open_question_ids": open_question_ids,
                        "include_open_answers_in_final_score": include_open_answers_in_final_score,
                    }
                    cur = conn.execute("""
                        INSERT INTO quiz_assignments (
                            section_id, title_bg, question_count, time_limit_minutes, question_plan_json
                        )
                        VALUES (?, ?, ?, ?, ?)
                    """, (
                        section_id,
                        section["title_bg"],
                        len(question_plan["question_ids"]),
                        time_limit,
                        _quiz_json.dumps(question_plan, ensure_ascii=False),
                    ))
                    assignment_id = cur.lastrowid
                    conn.commit()
                    conn.close()

                    quiz_write_assignment_note(assignment_id, _quiz_request.host_url)
                    return _quiz_redirect(_quiz_url_for("teacher_assignment", assignment_id=assignment_id))
            else:
                cur = conn.execute("""
                    INSERT INTO quiz_assignments (section_id, title_bg, question_count, time_limit_minutes)
                    VALUES (?, ?, ?, ?)
                """, (section_id, section["title_bg"], question_count, time_limit))
                assignment_id = cur.lastrowid
                conn.commit()
                conn.close()

                quiz_write_assignment_note(assignment_id, _quiz_request.host_url)
                return _quiz_redirect(_quiz_url_for("teacher_assignment", assignment_id=assignment_id))

    section_rows = conn.execute(f"""
        SELECT
            cs.id,
            cs.class,
            cs.title_bg,
            COUNT(DISTINCT q.id) AS question_count
        FROM curriculum_sections cs
        LEFT JOIN topic_section_assignments tsa ON tsa.section_id = cs.id
        LEFT JOIN questions q
          ON q.topic_id = tsa.topic_id
         AND {QUIZ_APPROVED_FILTER}
         AND q.question_type = 'multiple_choice'
        GROUP BY cs.id
        HAVING question_count > 0
        ORDER BY cs.class, cs.display_order, cs.title_bg
    """).fetchall()

    sections = []
    for row in section_rows:
        section = dict(row)
        eligible_count = len(quiz_section_question_ids(conn, int(section["id"])))
        if eligible_count <= 0:
            continue
        section["question_count"] = eligible_count
        sections.append(section)

    section_slug = (_quiz_request.args.get("section") or "").strip()
    if section_slug:
        preselected = conn.execute("""
            SELECT id
            FROM curriculum_sections
            WHERE section_slug = ?
        """, (section_slug,)).fetchone()
        if preselected:
            preselected_section_id = int(preselected["id"])

    open_candidates = fetch_open_question_candidates(conn)
    open_source_counts: dict[str, int] = {}
    for candidate in open_candidates:
        source_slug = candidate.get("source_slug") or "unknown"
        open_source_counts[source_slug] = open_source_counts.get(source_slug, 0) + 1
    open_source_options = [
        {"source_slug": source_slug, "count": count}
        for source_slug, count in sorted(open_source_counts.items())
    ]

    conn.close()

    return _quiz_render_template(
        "teacher_new.html",
        sections=sections,
        preselected_section_id=preselected_section_id,
        form_values=form_values,
        teacher_new_error=teacher_new_error,
        quiz_time_limit_max_minutes=QUIZ_TIME_LIMIT_MAX_MINUTES,
        mixed_planning_result=mixed_planning_result,
        open_source_options=open_source_options,
        total_open_candidates=len(open_candidates),
    ), 400 if teacher_new_error else 200


@app.route("/teacher/assignment/<int:assignment_id>")
def teacher_assignment(assignment_id):
    conn = quiz_db()
    assignment = quiz_fetch_assignment(conn, assignment_id)
    if not assignment:
        conn.close()
        _quiz_abort(404)

    attempts = conn.execute("""
        SELECT *
        FROM quiz_attempts
        WHERE assignment_id = ?
        ORDER BY started_at
    """, (assignment_id,)).fetchall()

    quiz_url = _quiz_request.host_url.rstrip("/") + _quiz_url_for("quiz_start", assignment_id=assignment_id)
    mixed_status = quiz_assignment_mixed_status(assignment["question_plan_json"])
    conn.close()

    return _quiz_render_template(
        "teacher_assignment.html",
        assignment=assignment,
        attempts=attempts,
        quiz_url=quiz_url,
        mixed_status=mixed_status,
        edit_error=None,
        edit_form_values=None,
    )


QUIZ_DUPLICATE_TITLE_SUFFIX = " (копие)"
QUIZ_TITLE_MAX_LENGTH = 200
QUIZ_TIME_LIMIT_MAX_MINUTES = 600


def quiz_duplicate_title(title: str | None) -> str:
    base_title = title or ""
    max_base_length = max(0, QUIZ_TITLE_MAX_LENGTH - len(QUIZ_DUPLICATE_TITLE_SUFFIX))
    return f"{base_title[:max_base_length]}{QUIZ_DUPLICATE_TITLE_SUFFIX}"[:QUIZ_TITLE_MAX_LENGTH]


@app.route("/teacher/assignment/<int:assignment_id>/edit", methods=["POST"])
def teacher_assignment_edit(assignment_id):
    conn = quiz_db()
    assignment = quiz_fetch_assignment(conn, assignment_id)
    if not assignment:
        conn.close()
        _quiz_abort(404)

    raw_title = (_quiz_request.form.get("title_bg") or "").replace("\x00", "").strip()
    raw_time = (_quiz_request.form.get("time_limit_minutes") or "").strip()

    error = None
    new_time_limit = None
    if not raw_title:
        error = "title_required"
    elif len(raw_title) > QUIZ_TITLE_MAX_LENGTH:
        error = "title_too_long"
    elif raw_time:
        try:
            new_time_limit = int(raw_time)
        except ValueError:
            error = "time_invalid"
        else:
            if new_time_limit < 1 or new_time_limit > QUIZ_TIME_LIMIT_MAX_MINUTES:
                error = "time_out_of_range"

    if error is not None:
        attempts = conn.execute("""
            SELECT *
            FROM quiz_attempts
            WHERE assignment_id = ?
            ORDER BY started_at
        """, (assignment_id,)).fetchall()
        quiz_url = _quiz_request.host_url.rstrip("/") + _quiz_url_for("quiz_start", assignment_id=assignment_id)
        mixed_status = quiz_assignment_mixed_status(assignment["question_plan_json"])
        conn.close()
        rendered = _quiz_render_template(
            "teacher_assignment.html",
            assignment=assignment,
            attempts=attempts,
            quiz_url=quiz_url,
            mixed_status=mixed_status,
            edit_error=error,
            edit_form_values={"title_bg": raw_title, "time_limit_minutes": raw_time},
        )
        return rendered, 400

    conn.execute("""
        UPDATE quiz_assignments
        SET title_bg = ?, time_limit_minutes = ?
        WHERE id = ?
    """, (raw_title, new_time_limit, assignment_id))
    conn.commit()
    conn.close()

    quiz_write_assignment_note(assignment_id, _quiz_request.host_url)
    return _quiz_redirect(_quiz_url_for("teacher_assignment", assignment_id=assignment_id))


@app.route("/teacher/assignment/<int:assignment_id>/duplicate", methods=["POST"])
def teacher_assignment_duplicate(assignment_id):
    conn = quiz_db()
    source = quiz_fetch_assignment(conn, assignment_id)
    if not source:
        conn.close()
        _quiz_abort(404)

    cur = conn.execute("""
        INSERT INTO quiz_assignments (
            section_id, title_bg, question_count, time_limit_minutes, question_plan_json
        )
        VALUES (?, ?, ?, ?, ?)
    """, (
        source["section_id"],
        quiz_duplicate_title(source["title_bg"]),
        source["question_count"],
        source["time_limit_minutes"],
        source["question_plan_json"],
    ))
    new_assignment_id = int(cur.lastrowid)
    conn.commit()
    conn.close()

    quiz_write_assignment_note(new_assignment_id, _quiz_request.host_url)
    return _quiz_redirect(_quiz_url_for("teacher_assignment", assignment_id=new_assignment_id))


TEACHER_NOTE_MAX_LENGTH = 1000


QUIZ_RESULTS_ALLOWED_STATUS = {"all", "submitted", "unsubmitted"}
QUIZ_RESULTS_ALLOWED_OPEN = {"all", "has_open", "no_open"}
QUIZ_RESULTS_ALLOWED_SORTS = {
    "default",
    "name_asc",
    "name_desc",
    "submitted_desc",
    "submitted_asc",
    "mc_desc",
    "mc_asc",
    "open_desc",
    "open_asc",
}
QUIZ_RESULTS_MIXED_ONLY_SORTS = {"open_desc", "open_asc"}


def quiz_parse_results_filter_args(args, *, is_mixed: bool) -> dict:
    q = (args.get("q") or "").strip()
    status = (args.get("status") or "all").strip().lower()
    if status not in QUIZ_RESULTS_ALLOWED_STATUS:
        status = "all"
    open_filter = (args.get("open") or "all").strip().lower()
    if open_filter not in QUIZ_RESULTS_ALLOWED_OPEN:
        open_filter = "all"
    sort = (args.get("sort") or "default").strip().lower()
    if sort not in QUIZ_RESULTS_ALLOWED_SORTS:
        sort = "default"
    if sort in QUIZ_RESULTS_MIXED_ONLY_SORTS and not is_mixed:
        sort = "default"
    return {"q": q, "status": status, "open": open_filter, "sort": sort}


def quiz_filter_results_attempts(attempts, *, filters, attempt_answers_by_id):
    q_lower = (filters["q"] or "").lower()
    status = filters["status"]
    open_filter = filters["open"]

    def keep(attempt):
        if q_lower and q_lower not in (attempt["student_name"] or "").lower():
            return False
        if status == "submitted" and not attempt["submitted_at"]:
            return False
        if status == "unsubmitted" and attempt["submitted_at"]:
            return False
        if open_filter != "all":
            has_open = bool(attempt_answers_by_id.get(int(attempt["id"])))
            if open_filter == "has_open" and not has_open:
                return False
            if open_filter == "no_open" and has_open:
                return False
        return True

    return [a for a in attempts if keep(a)]


def quiz_sort_results_attempts(attempts, *, sort, open_subtotal_by_id):
    if sort == "default":
        return list(attempts)
    submitted = [a for a in attempts if a["submitted_at"]]
    unsubmitted = [a for a in attempts if not a["submitted_at"]]
    if sort == "name_asc":
        return sorted(attempts, key=lambda a: (a["student_name"] or "").casefold())
    if sort == "name_desc":
        return sorted(
            attempts,
            key=lambda a: (a["student_name"] or "").casefold(),
            reverse=True,
        )
    if sort == "submitted_desc":
        submitted.sort(key=lambda a: a["submitted_at"] or "", reverse=True)
        unsubmitted.sort(key=lambda a: a["started_at"] or "", reverse=True)
        return submitted + unsubmitted
    if sort == "submitted_asc":
        submitted.sort(key=lambda a: a["submitted_at"] or "")
        unsubmitted.sort(key=lambda a: a["started_at"] or "")
        return submitted + unsubmitted
    if sort == "mc_desc":
        submitted.sort(
            key=lambda a: float(a["percent"]) if a["percent"] is not None else float("-inf"),
            reverse=True,
        )
        return submitted + unsubmitted
    if sort == "mc_asc":
        submitted.sort(
            key=lambda a: float(a["percent"]) if a["percent"] is not None else float("inf"),
        )
        return submitted + unsubmitted
    if sort == "open_desc":
        submitted.sort(
            key=lambda a: float(
                (open_subtotal_by_id.get(int(a["id"])) or {}).get("awarded") or 0
            ),
            reverse=True,
        )
        return submitted + unsubmitted
    if sort == "open_asc":
        submitted.sort(
            key=lambda a: float(
                (open_subtotal_by_id.get(int(a["id"])) or {}).get("awarded") or 0
            ),
        )
        return submitted + unsubmitted
    return list(attempts)


@app.route("/teacher/assignment/<int:assignment_id>/results", methods=["GET", "POST"])
def teacher_assignment_results(assignment_id):
    conn = quiz_db()
    assignment = quiz_fetch_assignment(conn, assignment_id)
    if not assignment:
        conn.close()
        _quiz_abort(404)

    if _quiz_request.method == "POST":
        try:
            answer_id = int(_quiz_request.form.get("text_answer_id", ""))
        except (TypeError, ValueError):
            conn.close()
            _quiz_abort(400)

        override_raw = (_quiz_request.form.get("teacher_override") or "").strip()
        if override_raw == "":
            override_value = 0
        elif override_raw in {"0", "1"}:
            override_value = int(override_raw)
        else:
            conn.close()
            _quiz_abort(400)

        teacher_note = (_quiz_request.form.get("teacher_note") or "").replace("\x00", "").strip()
        if len(teacher_note) > TEACHER_NOTE_MAX_LENGTH:
            conn.close()
            _quiz_abort(400)
        teacher_note_value = teacher_note or None

        row = conn.execute("""
            SELECT qta.id
            FROM quiz_text_answers qta
            JOIN quiz_attempts qa ON qa.id = qta.attempt_id
            WHERE qta.id = ?
              AND qa.assignment_id = ?
        """, (answer_id, assignment_id)).fetchone()
        if not row:
            conn.close()
            _quiz_abort(404)

        conn.execute("""
            UPDATE quiz_text_answers
            SET teacher_override = ?,
                teacher_note = ?
            WHERE id = ?
        """, (override_value, teacher_note_value, answer_id))
        conn.commit()
        conn.close()
        return _quiz_redirect(_quiz_url_for("teacher_assignment_results", assignment_id=assignment_id))

    mixed_status = quiz_assignment_mixed_status(assignment["question_plan_json"])
    filters = quiz_parse_results_filter_args(
        _quiz_request.args, is_mixed=bool(mixed_status["is_mixed"])
    )

    all_attempts = conn.execute("""
        SELECT
            id,
            student_name,
            question_ids_json,
            started_at,
            submitted_at,
            score_correct,
            score_total,
            CASE
              WHEN score_total IS NOT NULL AND score_total > 0
              THEN ROUND(100.0 * score_correct / score_total, 1)
              ELSE NULL
            END AS percent
        FROM quiz_attempts
        WHERE assignment_id = ?
        ORDER BY
            submitted_at IS NULL,
            submitted_at DESC,
            started_at DESC,
            student_name
    """, (assignment_id,)).fetchall()

    attempt_answers_by_id: dict[int, list[dict]] = {}
    open_subtotal_by_id: dict[int, dict] = {}
    combined_score_by_id: dict[int, dict] = {}
    for attempt in all_attempts:
        if not attempt["submitted_at"]:
            continue
        attempt_id_int = int(attempt["id"])
        ans = fetch_quiz_text_answers_for_attempt(conn, attempt_id_int)
        attempt_answers_by_id[attempt_id_int] = ans
        if ans:
            sub = quiz_text_answer_informational_subtotal(ans)
            open_subtotal_by_id[attempt_id_int] = sub
            attempt_plan = quiz_parse_attempt_question_plan(attempt["question_ids_json"])
            combined = quiz_combined_score_summary(
                attempt,
                sub,
                enabled=bool(attempt_plan["include_open_answers_in_final_score"]),
            )
            if combined:
                combined_score_by_id[attempt_id_int] = combined

    filtered_attempts = quiz_filter_results_attempts(
        all_attempts, filters=filters, attempt_answers_by_id=attempt_answers_by_id
    )
    attempts = quiz_sort_results_attempts(
        filtered_attempts,
        sort=filters["sort"],
        open_subtotal_by_id=open_subtotal_by_id,
    )

    open_text_answers = []
    open_subtotals_by_attempt: dict[int, dict] = {}
    combined_scores_by_attempt: dict[int, dict] = {}
    mc_percentages: list[float] = []
    submitted_attempt_count = 0
    open_answer_attempt_count = 0
    open_answer_total = 0
    open_answer_auto_matched_count = 0
    open_answer_teacher_override_count = 0
    open_subtotal_awarded_total = 0.0
    open_subtotal_possible_total = 0.0
    for attempt in attempts:
        if not attempt["submitted_at"]:
            continue
        attempt_id_int = int(attempt["id"])
        submitted_attempt_count += 1
        if attempt["score_total"] is not None and int(attempt["score_total"]) > 0:
            mc_percentages.append(
                100.0 * float(attempt["score_correct"] or 0) / float(attempt["score_total"])
            )
        attempt_answers = attempt_answers_by_id.get(attempt_id_int, [])
        if attempt_answers:
            open_subtotal = open_subtotal_by_id[attempt_id_int]
            open_subtotals_by_attempt[attempt_id_int] = open_subtotal
            if attempt_id_int in combined_score_by_id:
                combined_scores_by_attempt[attempt_id_int] = combined_score_by_id[attempt_id_int]
            open_answer_attempt_count += 1
            open_subtotal_awarded_total += float(open_subtotal.get("awarded") or 0)
            open_subtotal_possible_total += float(open_subtotal.get("possible") or 0)
        for answer in attempt_answers:
            open_answer_total += 1
            if answer.get("is_correct") == 1:
                open_answer_auto_matched_count += 1
            if answer.get("teacher_override") == 1:
                open_answer_teacher_override_count += 1
            open_text_answers.append({
                **answer,
                "student_name": attempt["student_name"],
                "attempt_id": attempt_id_int,
                "attempt_score_correct": attempt["score_correct"],
                "attempt_score_total": attempt["score_total"],
                "submitted_at": attempt["submitted_at"],
            })

    totals = conn.execute("""
        SELECT
            COUNT(*) AS attempts_count,
            SUM(CASE WHEN submitted_at IS NOT NULL THEN 1 ELSE 0 END) AS submitted_count,
            SUM(CASE WHEN submitted_at IS NULL THEN 1 ELSE 0 END) AS unfinished_count,
            AVG(CASE
              WHEN submitted_at IS NOT NULL AND score_total > 0
              THEN 100.0 * score_correct / score_total
              ELSE NULL
            END) AS avg_percent
        FROM quiz_attempts
        WHERE assignment_id = ?
    """, (assignment_id,)).fetchone()

    conn.close()

    summary = {
        "submitted_attempt_count": submitted_attempt_count,
        "highest_mc_percent": round(max(mc_percentages), 1) if mc_percentages else None,
        "lowest_mc_percent": round(min(mc_percentages), 1) if mc_percentages else None,
        "mixed_open_enabled": bool(mixed_status["is_mixed"]),
        "combined_score_display_enabled": bool(mixed_status["combined_score"]),
        "open_answer_attempt_count": open_answer_attempt_count,
        "open_answer_total": open_answer_total,
        "open_answer_auto_matched_count": open_answer_auto_matched_count,
        "open_answer_teacher_override_count": open_answer_teacher_override_count,
        "open_subtotal_awarded_total": round(open_subtotal_awarded_total, 2),
        "open_subtotal_possible_total": round(open_subtotal_possible_total, 2),
    }

    filter_active = (
        bool(filters["q"]) or filters["status"] != "all" or filters["open"] != "all"
    )
    sort_active = filters["sort"] != "default"

    return _quiz_render_template(
        "teacher_results.html",
        assignment=assignment,
        attempts=attempts,
        open_text_answers=open_text_answers,
        open_subtotals_by_attempt=open_subtotals_by_attempt,
        combined_scores_by_attempt=combined_scores_by_attempt,
        totals=totals,
        mixed_status=mixed_status,
        summary=summary,
        filters=filters,
        filter_active=filter_active,
        sort_active=sort_active,
    )


QUIZ_RESULTS_EXPORT_COLUMNS = [
    "row_type",
    "assignment_id",
    "assignment_title",
    "attempt_id",
    "student_name",
    "submitted_at",
    "mc_score_correct",
    "mc_score_total",
    "mc_percent",
    "mixed_open_enabled",
    "include_open_answers_in_final_score",
    "open_answer_count",
    "open_subtotal_awarded",
    "open_subtotal_possible",
    "combined_awarded",
    "combined_possible",
    "question_id",
    "subquestion_number",
    "raw_answer",
    "normalized_answer",
    "matched_answer",
    "points_awarded",
    "points_possible",
    "grading_mode",
    "teacher_override",
    "teacher_note",
    "is_correct",
]


def _quiz_csv_value(value):
    if value is None:
        return ""
    return value


def _quiz_format_points(value) -> str:
    try:
        return f"{float(value):.4f}"
    except (TypeError, ValueError):
        return ""


@app.route("/teacher/assignment/<int:assignment_id>/results.csv")
def teacher_assignment_results_export(assignment_id):
    conn = quiz_db()
    assignment = quiz_fetch_assignment(conn, assignment_id)
    if not assignment:
        conn.close()
        _quiz_abort(404)

    apply_filters = (_quiz_request.args.get("filtered") or "").strip().lower() in {
        "1",
        "true",
        "yes",
    }

    attempt_answers_by_id: dict[int, list[dict]] = {}

    if apply_filters:
        all_attempts = conn.execute("""
            SELECT
                id,
                student_name,
                question_ids_json,
                started_at,
                submitted_at,
                score_correct,
                score_total,
                CASE
                  WHEN score_total IS NOT NULL AND score_total > 0
                  THEN ROUND(100.0 * score_correct / score_total, 1)
                  ELSE NULL
                END AS percent
            FROM quiz_attempts
            WHERE assignment_id = ?
            ORDER BY
                submitted_at IS NULL,
                submitted_at DESC,
                started_at DESC,
                student_name
        """, (assignment_id,)).fetchall()

        open_subtotal_by_id: dict[int, dict] = {}
        for attempt in all_attempts:
            if not attempt["submitted_at"]:
                continue
            ans = fetch_quiz_text_answers_for_attempt(conn, int(attempt["id"]))
            attempt_answers_by_id[int(attempt["id"])] = ans
            if ans:
                open_subtotal_by_id[int(attempt["id"])] = (
                    quiz_text_answer_informational_subtotal(ans)
                )

        mixed_status = quiz_assignment_mixed_status(assignment["question_plan_json"])
        filters = quiz_parse_results_filter_args(
            _quiz_request.args, is_mixed=bool(mixed_status["is_mixed"])
        )
        filtered = quiz_filter_results_attempts(
            all_attempts, filters=filters, attempt_answers_by_id=attempt_answers_by_id
        )
        attempts = quiz_sort_results_attempts(
            filtered,
            sort=filters["sort"],
            open_subtotal_by_id=open_subtotal_by_id,
        )
        filename = f"assignment_{assignment_id}_results_filtered.csv"
    else:
        attempts = conn.execute("""
            SELECT
                id,
                student_name,
                question_ids_json,
                started_at,
                submitted_at,
                score_correct,
                score_total
            FROM quiz_attempts
            WHERE assignment_id = ?
              AND submitted_at IS NOT NULL
            ORDER BY submitted_at DESC, started_at DESC, student_name
        """, (assignment_id,)).fetchall()
        filename = f"assignment_{assignment_id}_results.csv"

    buf = _quiz_io.StringIO()
    writer = _quiz_csv.writer(buf)
    writer.writerow(QUIZ_RESULTS_EXPORT_COLUMNS)

    for attempt in attempts:
        attempt_id_int = int(attempt["id"])

        if not attempt["submitted_at"]:
            writer.writerow([
                "attempt",
                assignment_id,
                assignment["title_bg"],
                attempt_id_int,
                attempt["student_name"],
                "",
                "",
                "",
                "",
                "",
                "",
                0,
                "",
                "",
                "",
                "",
                "", "", "", "", "", "", "", "", "", "", "",
            ])
            continue

        if attempt_id_int in attempt_answers_by_id:
            attempt_answers = attempt_answers_by_id[attempt_id_int]
        else:
            attempt_answers = fetch_quiz_text_answers_for_attempt(conn, attempt_id_int)
        attempt_plan = quiz_parse_attempt_question_plan(attempt["question_ids_json"])
        mixed_open_enabled = bool(attempt_plan["mixed_open_enabled"])
        combined_enabled = bool(attempt_plan["include_open_answers_in_final_score"])

        open_subtotal = (
            quiz_text_answer_informational_subtotal(attempt_answers)
            if attempt_answers
            else None
        )
        combined_score = quiz_combined_score_summary(
            attempt, open_subtotal, enabled=combined_enabled
        )

        mc_correct = attempt["score_correct"]
        mc_total = attempt["score_total"]
        mc_percent = ""
        if mc_total is not None and int(mc_total) > 0 and mc_correct is not None:
            mc_percent = f"{round(100.0 * float(mc_correct) / float(mc_total), 1)}"

        writer.writerow([
            "attempt",
            assignment_id,
            assignment["title_bg"],
            attempt_id_int,
            attempt["student_name"],
            attempt["submitted_at"],
            _quiz_csv_value(mc_correct),
            _quiz_csv_value(mc_total),
            mc_percent,
            "1" if mixed_open_enabled else "0",
            "1" if combined_enabled else "0",
            len(attempt_answers),
            _quiz_format_points(open_subtotal["awarded"]) if open_subtotal else "",
            _quiz_format_points(open_subtotal["possible"]) if open_subtotal else "",
            _quiz_format_points(combined_score["combined_awarded"]) if combined_score else "",
            _quiz_format_points(combined_score["combined_possible"]) if combined_score else "",
            "", "", "", "", "", "", "", "", "", "", "",
        ])

        for ans in attempt_answers:
            writer.writerow([
                "open_answer",
                assignment_id,
                assignment["title_bg"],
                attempt_id_int,
                attempt["student_name"],
                attempt["submitted_at"],
                "",
                "",
                "",
                "1" if mixed_open_enabled else "0",
                "1" if combined_enabled else "0",
                "",
                "",
                "",
                "",
                "",
                _quiz_csv_value(ans.get("question_id")),
                _quiz_csv_value(ans.get("subquestion_number")),
                _quiz_csv_value(ans.get("raw_answer")),
                _quiz_csv_value(ans.get("normalized_answer")),
                _quiz_csv_value(ans.get("matched_answer")),
                _quiz_csv_value(ans.get("points_awarded")),
                _quiz_csv_value(ans.get("points_possible")),
                _quiz_csv_value(ans.get("grading_mode")),
                _quiz_csv_value(ans.get("teacher_override")),
                _quiz_csv_value(ans.get("teacher_note")),
                _quiz_csv_value(ans.get("is_correct")),
            ])

    conn.close()

    return _quiz_response(
        buf.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/quiz/<int:assignment_id>", methods=["GET", "POST"])
def quiz_start(assignment_id):
    conn = quiz_db()
    assignment = quiz_fetch_assignment(conn, assignment_id)
    if not assignment:
        conn.close()
        _quiz_abort(404)

    if _quiz_request.method == "POST":
        student_name = " ".join((_quiz_request.form.get("student_name") or "").strip().split())
        if not student_name:
            conn.close()
            return _quiz_redirect(_quiz_url_for("quiz_start", assignment_id=assignment_id))

        existing = conn.execute("""
            SELECT *
            FROM quiz_attempts
            WHERE assignment_id = ?
              AND student_name = ?
        """, (assignment_id, student_name)).fetchone()

        if existing:
            conn.close()
            if existing["submitted_at"]:
                return _quiz_redirect(_quiz_url_for("quiz_result", attempt_id=existing["id"]))
            return _quiz_redirect(_quiz_url_for("quiz_attempt", attempt_id=existing["id"]))

        assignment_question_plan = quiz_parse_assignment_question_plan(assignment["question_plan_json"])
        if (assignment["question_plan_json"] or "").strip() and assignment_question_plan is None:
            conn.close()
            _quiz_abort(400)

        if assignment_question_plan:
            seed = quiz_seed(assignment_id, student_name)
            question_ids_json = _quiz_json.dumps(assignment_question_plan, ensure_ascii=False)
            score_total = len([
                qid
                for qid in assignment_question_plan["question_ids"]
                if qid not in set(assignment_question_plan["open_question_ids"])
            ])
        else:
            seed, question_ids = quiz_pick_questions(conn, assignment, student_name)
            question_ids_json = _quiz_json.dumps(question_ids)
            score_total = len(question_ids)

        try:
            cur = conn.execute("""
                INSERT INTO quiz_attempts (assignment_id, student_name, seed, question_ids_json, score_total)
                VALUES (?, ?, ?, ?, ?)
            """, (
                assignment_id,
                student_name,
                seed,
                question_ids_json,
                score_total,
            ))
        except sqlite3.IntegrityError:
            existing = conn.execute("""
                SELECT *
                FROM quiz_attempts
                WHERE assignment_id = ?
                  AND student_name = ?
            """, (assignment_id, student_name)).fetchone()
            conn.close()
            if existing:
                if existing["submitted_at"]:
                    return _quiz_redirect(_quiz_url_for("quiz_result", attempt_id=existing["id"]))
                return _quiz_redirect(_quiz_url_for("quiz_attempt", attempt_id=existing["id"]))
            raise
        attempt_id = cur.lastrowid
        conn.commit()
        conn.close()

        return _quiz_redirect(_quiz_url_for("quiz_attempt", attempt_id=attempt_id))

    mixed_status = quiz_assignment_mixed_status(assignment["question_plan_json"])
    conn.close()
    return _quiz_render_template("quiz_start.html", assignment=assignment, mixed_status=mixed_status)


@app.route("/quiz/attempt/<int:attempt_id>", methods=["GET", "POST"])
def quiz_attempt(attempt_id):
    conn = quiz_db()
    attempt = conn.execute("SELECT * FROM quiz_attempts WHERE id = ?", (attempt_id,)).fetchone()
    if not attempt:
        conn.close()
        _quiz_abort(404)

    assignment = quiz_fetch_assignment(conn, int(attempt["assignment_id"]))
    if not assignment:
        conn.close()
        _quiz_abort(404)

    if attempt["submitted_at"]:
        conn.close()
        return _quiz_redirect(_quiz_url_for("quiz_result", attempt_id=attempt_id))

    attempt_question_plan = quiz_parse_attempt_question_plan(attempt["question_ids_json"])
    open_question_ids = (
        attempt_question_plan["open_question_ids"]
        if attempt_question_plan["mixed_open_enabled"]
        else []
    )
    question_ids, skipped_count = filter_renderable_attempt_question_ids(
        conn,
        attempt_question_plan["question_ids"],
        open_question_ids=open_question_ids,
    )
    seed = attempt["seed"]

    attempt_mixed_status = {
        "is_mixed": bool(attempt_question_plan["mixed_open_enabled"]),
        "open_count": (
            len(attempt_question_plan["open_question_ids"])
            if attempt_question_plan["mixed_open_enabled"]
            else 0
        ),
        "combined_score": bool(
            attempt_question_plan["include_open_answers_in_final_score"]
        ),
    }

    if _quiz_request.method == "POST":
        if not question_ids:
            conn.close()
            return _quiz_render_template(
                "quiz_attempt.html",
                assignment=assignment,
                attempt=attempt,
                questions=[],
                remaining_seconds=None,
                stale_attempt_message=STALE_ATTEMPT_MESSAGE,
                skipped_question_count=skipped_count,
                mixed_status=attempt_mixed_status,
            )

        correct_by_qid = {}
        mc_question_ids = []
        for qid in question_ids:
            row = conn.execute("""
                SELECT question_type
                FROM questions
                WHERE id = ?
            """, (qid,)).fetchone()
            if row and row["question_type"] == "multiple_choice":
                mc_question_ids.append(qid)

        for qid in mc_question_ids:
            row = conn.execute("""
                SELECT option_letter
                FROM multiple_choice_options
                WHERE question_id = ?
                  AND is_correct = 1
                LIMIT 1
            """, (qid,)).fetchone()
            correct_by_qid[int(qid)] = row["option_letter"] if row else None

        score = 0
        # Stale invalid IDs and planned open fields are excluded from MC scoring.
        total = len(mc_question_ids)

        for qid in mc_question_ids:
            chosen = _quiz_request.form.get(f"q_{qid}")
            correct_letter = correct_by_qid.get(int(qid))
            is_correct = 1 if chosen and correct_letter and chosen == correct_letter else 0
            score += is_correct

            conn.execute("""
                INSERT INTO quiz_answers (attempt_id, question_id, chosen_letter, is_correct)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(attempt_id, question_id)
                DO UPDATE SET chosen_letter = excluded.chosen_letter,
                              is_correct = excluded.is_correct
            """, (attempt_id, qid, chosen, is_correct))

        quiz_record_planned_open_text_answers(
            conn,
            attempt_id=attempt_id,
            question_ids=question_ids,
            open_question_ids=open_question_ids,
            form=_quiz_request.form,
        )

        conn.execute("""
            UPDATE quiz_attempts
            SET submitted_at = CURRENT_TIMESTAMP,
                score_correct = ?,
                score_total = ?
            WHERE id = ?
        """, (score, total, attempt_id))

        conn.commit()
        conn.close()

        quiz_write_attempt_note(attempt_id)
        quiz_write_assignment_note(int(assignment["id"]), _quiz_request.host_url)

        return _quiz_redirect(_quiz_url_for("quiz_result", attempt_id=attempt_id))

    questions = quiz_load_questions(conn, question_ids, seed, include_correct=False)
    remaining = quiz_remaining_seconds(assignment, attempt)
    if not questions:
        remaining = None
    conn.close()

    return _quiz_render_template(
        "quiz_attempt.html",
        assignment=assignment,
        attempt=attempt,
        questions=questions,
        remaining_seconds=remaining,
        stale_attempt_message=STALE_ATTEMPT_MESSAGE if not questions else None,
        skipped_question_count=skipped_count,
        mixed_status=attempt_mixed_status,
    )


@app.route("/quiz/attempt/<int:attempt_id>/result")
def quiz_result(attempt_id):
    conn = quiz_db()
    attempt = conn.execute("SELECT * FROM quiz_attempts WHERE id = ?", (attempt_id,)).fetchone()
    if not attempt:
        conn.close()
        _quiz_abort(404)

    assignment = quiz_fetch_assignment(conn, int(attempt["assignment_id"]))
    if not assignment:
        conn.close()
        _quiz_abort(404)

    if not attempt["submitted_at"]:
        conn.close()
        return _quiz_redirect(_quiz_url_for("quiz_attempt", attempt_id=attempt_id))

    attempt_question_plan = quiz_parse_attempt_question_plan(attempt["question_ids_json"])
    stored_qids = attempt_question_plan["question_ids"]
    # MC questions remain the only scored result items; open answers render as read-only review.
    qids, skipped_count = filter_renderable_attempt_question_ids(conn, stored_qids)
    original_question_count = len(stored_qids or [])
    renderable_question_count = len(qids)
    skipped_question_count = max(0, original_question_count - renderable_question_count)
    questions = quiz_load_result_questions(conn, attempt, qids, attempt["seed"])
    open_text_answers = fetch_quiz_text_answers_for_attempt(conn, attempt_id)
    open_text_subtotal = quiz_text_answer_informational_subtotal(open_text_answers) if open_text_answers else None
    combined_score = quiz_combined_score_summary(
        attempt,
        open_text_subtotal,
        enabled=bool(attempt_question_plan["include_open_answers_in_final_score"]),
    )
    seconds = quiz_time_taken_seconds(attempt)
    conn.close()

    return _quiz_render_template(
        "quiz_result.html",
        assignment=assignment,
        attempt=attempt,
        questions=questions,
        time_taken=quiz_format_duration(seconds),
        open_text_answers=open_text_answers,
        open_text_subtotal=open_text_subtotal,
        combined_score=combined_score,
        stale_attempt_message=STALE_ATTEMPT_MESSAGE if not questions else None,
        original_question_count=original_question_count,
        renderable_question_count=renderable_question_count,
        skipped_question_count=skipped_question_count,
    )

# === QUIZ PHASE 2 END ===


if __name__ == "__main__":
    if not DB_PATH.exists():
        print(f"❌ DB не намерен: {DB_PATH}")
        raise SystemExit(1)
    print(f"📚 LearnPilot — Web app")
    print(f"   DB:      {DB_PATH}")
    print(f"   URL:     http://127.0.0.1:5000")
    print(f"   Stop:    Ctrl+C")
    app.run(host="127.0.0.1", port=5000, debug=True)
