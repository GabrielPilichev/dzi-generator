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

import sqlite3
from pathlib import Path

from flask import Flask, abort, g, render_template, url_for, request, redirect, session


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "questions.db"

app = Flask(__name__)
app.config["DB_PATH"] = str(DB_PATH)
app.config["SECRET_KEY"] = os.environ.get("DZI_SECRET_KEY", "local-learnpilot-dev-key")



# ---------------------------------------------------------------------------
# Local UI profiles
# ---------------------------------------------------------------------------

VALID_UI_PROFILES = {"admin", "tester"}
TESTER_TEACHER_ENDPOINTS = {"teacher_new", "teacher_assignment"}

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
    next_url = request.args.get("next") or (url_for("tester_home") if profile == "tester" else url_for("index"))
    if not next_url.startswith("/"):
        next_url = url_for("index")
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
    next_url = request.args.get("next") or request.form.get("next") or url_for("tester_home")

    if not next_url.startswith("/"):
        next_url = url_for("tester_home")

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
            return redirect(url_for("admin_login", next=request.args.get("next") or request.referrer or url_for("index")))
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
    next_url = request.args.get("next") or request.form.get("next") or url_for("teacher_dashboard")

    if not next_url.startswith("/"):
        next_url = url_for("teacher_dashboard")

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

import hashlib as _quiz_hashlib
import json as _quiz_json
import os as _quiz_os
import random as _quiz_random
import re as _quiz_re
import sqlite3 as _quiz_sqlite3
import sys as _quiz_sys
import unicodedata as _quiz_unicodedata
from datetime import datetime as _quiz_datetime
from pathlib import Path as _QuizPath

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


def fetch_open_question_candidates(conn, *, source_slug: str | None = None, limit: int | None = None) -> list[dict]:
    params = []
    source_filter = ""
    if source_slug is not None:
        source_filter = "AND q.source_exam = ?"
        params.append(source_slug)

    question_rows = conn.execute(f"""
        SELECT
            q.id,
            q.source_exam,
            q.source_number,
            q.question_type,
            q.prompt,
            q.has_image,
            q.image_path
        FROM questions q
        WHERE q.question_type IN ('fill_in', 'short_answer')
          {source_filter}
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
) -> dict:
    if closed_count < 0 or open_count < 0:
        raise ValueError("closed_count and open_count must be non-negative")

    params = []
    source_filter = ""
    if source_slug is not None:
        source_filter = "AND q.source_exam = ?"
        params.append(source_slug)

    closed_rows = conn.execute(f"""
        SELECT
            q.id,
            q.source_exam,
            q.source_number,
            q.prompt,
            q.question_type,
            q.has_image,
            q.image_path
        FROM questions q
        WHERE q.question_type = 'multiple_choice'
          AND {QUIZ_APPROVED_FILTER}
          {source_filter}
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

    open_candidates = fetch_open_question_candidates(conn, source_slug=source_slug)

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
        SELECT id, prompt, question_type
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
    else:
        raw_question_ids = parsed
        raw_open_ids = []
        mixed_open_enabled = False

    return {
        "question_ids": raw_question_ids if isinstance(raw_question_ids, list) else [],
        "open_question_ids": raw_open_ids if isinstance(raw_open_ids, list) else [],
        "mixed_open_enabled": mixed_open_enabled,
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

    recent_assignments = conn.execute("""
        SELECT
            qa.id,
            qa.title_bg,
            qa.question_count,
            qa.time_limit_minutes,
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

    assignments = conn.execute("""
        SELECT
            qa.id,
            qa.section_id,
            qa.title_bg,
            qa.question_count,
            qa.time_limit_minutes,
            qa.created_at,
            cs.class,
            cs.title_bg AS section_title,
            cs.section_slug,
            COUNT(DISTINCT qt.id) AS attempts_count,
            SUM(CASE WHEN qt.submitted_at IS NOT NULL THEN 1 ELSE 0 END) AS submitted_count
        FROM quiz_assignments qa
        JOIN curriculum_sections cs ON cs.id = qa.section_id
        LEFT JOIN quiz_attempts qt ON qt.assignment_id = qa.id
        GROUP BY qa.id
        ORDER BY qa.created_at DESC, qa.id DESC
    """).fetchall()

    conn.close()

    return _quiz_render_template(
        "teacher_assignments.html",
        assignments=assignments,
    )


@app.route("/teacher/new", methods=["GET", "POST"])
def teacher_new():
    conn = quiz_db()
    preselected_section_id = None
    mixed_planning_result = None
    form_values = {
        "question_count": 10,
        "time_limit_minutes": "",
        "include_open_questions": False,
        "open_count": 0,
        "source_slug": "",
    }

    if _quiz_request.method == "POST":
        section_id = int(_quiz_request.form.get("section_id") or 0)
        requested_count = int(_quiz_request.form.get("question_count") or 10)
        raw_limit = (_quiz_request.form.get("time_limit_minutes") or "").strip()
        time_limit = int(raw_limit) if raw_limit else None
        include_open_questions = bool(_quiz_request.form.get("include_open_questions"))
        open_count = max(0, int(_quiz_request.form.get("open_count") or 0))
        source_slug = (_quiz_request.form.get("source_slug") or "").strip()
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

        if include_open_questions and open_count > 0:
            plan = build_mixed_quiz_plan(
                conn,
                closed_count=question_count,
                open_count=open_count,
                source_slug=source_slug or None,
            )
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
            preselected_section_id = section_id
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
        mixed_planning_result=mixed_planning_result,
        open_source_options=open_source_options,
        total_open_candidates=len(open_candidates),
    )


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
    conn.close()

    return _quiz_render_template(
        "teacher_assignment.html",
        assignment=assignment,
        attempts=attempts,
        quiz_url=quiz_url,
    )



@app.route("/teacher/assignment/<int:assignment_id>/results")
def teacher_assignment_results(assignment_id):
    conn = quiz_db()
    assignment = quiz_fetch_assignment(conn, assignment_id)
    if not assignment:
        conn.close()
        _quiz_abort(404)

    attempts = conn.execute("""
        SELECT
            id,
            student_name,
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

    open_text_answers = []
    for attempt in attempts:
        if not attempt["submitted_at"]:
            continue
        for answer in fetch_quiz_text_answers_for_attempt(conn, int(attempt["id"])):
            open_text_answers.append({
                **answer,
                "student_name": attempt["student_name"],
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

    return _quiz_render_template(
        "teacher_results.html",
        assignment=assignment,
        attempts=attempts,
        open_text_answers=open_text_answers,
        totals=totals,
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

        seed, question_ids = quiz_pick_questions(conn, assignment, student_name)

        cur = conn.execute("""
            INSERT INTO quiz_attempts (assignment_id, student_name, seed, question_ids_json, score_total)
            VALUES (?, ?, ?, ?, ?)
        """, (
            assignment_id,
            student_name,
            seed,
            _quiz_json.dumps(question_ids),
            len(question_ids),
        ))
        attempt_id = cur.lastrowid
        conn.commit()
        conn.close()

        return _quiz_redirect(_quiz_url_for("quiz_attempt", attempt_id=attempt_id))

    conn.close()
    return _quiz_render_template("quiz_start.html", assignment=assignment)


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
    seconds = quiz_time_taken_seconds(attempt)
    conn.close()

    return _quiz_render_template(
        "quiz_result.html",
        assignment=assignment,
        attempt=attempt,
        questions=questions,
        time_taken=quiz_format_duration(seconds),
        open_text_answers=open_text_answers,
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
