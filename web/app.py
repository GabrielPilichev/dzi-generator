"""
LearnPilot — Web app, Phase 1.

Браузър за секции и въпроси от curriculum-а. Read-only.

Структура:
  /                   → начална страница, списък с класове 8-12
  /grade/<n>          → предметите за избран клас
  /section/<slug>     → въпроси в избрана секция

Зависимости:
  pip3 install flask

Употреба:
  cd ~/dzi-generator
  python3 web/app.py
  → отвори http://127.0.0.1:5000
"""

from __future__ import annotations

import copy
import os
import hmac
import json
import math
import secrets
import warnings

import sqlite3
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, abort, g, render_template, url_for, request, redirect, send_file, session

from src.subject_browsing import public_slug, stored_slug_candidates
from src.subject_browse_cache import CachedSubjectBrowser
from src.subject_assessment import AssessmentScope, question_belongs_to_subject
from src.subject_content import IT, canonical_subject
from src.subject_registry import (
    PLATFORM_GRADES, all_subjects, get_subject, subject_for_route_slug, subject_supports_grade,
)
from src.practical_uploads import MAX_FILES_PER_REQUEST, MAX_REQUEST_BYTES, MAX_UPLOAD_BYTES
from web.blueprints.dzi import dzi_bp
from web.blueprints.practical import practical_bp
from web.attempt_access import quiz_attempt_owner_session_key
from web.blueprints.quiz import quiz_bp, register_legacy_quiz_endpoint_aliases
from web.blueprints.teacher import teacher_bp
from web import db as web_db
from web.access import (
    ADMIN,
    PROFILE_SWITCH,
    PUBLIC,
    TEACHER,
    access_policy,
    current_principal,
    denial_login_endpoint,
    is_route_allowed,
    policy_for_endpoint,
)
from web.request_integrity import SAME_ORIGIN, integrity_policy
from web.request_db import get_request_db
from web.production import warn_if_schema_migrations_pending
from web.services import dzi_exams
from web.services.subject_catalog import subject_catalog
from web.services import quiz as quiz_service
from web.services.dzi_exams import (
    DZI_FORMAT_VERSION,
    dzi_json_text_list,
    dzi_parse_source_slug,
    dzi_source_slug,
    dzi_source_title,
    fetch_fill_in_answer_rows,
)
from web.services.question_visuals import (
    attach_question_visual_assets,
    fetch_question_visual_assets,
)
from web.services.quiz import (
    QUIZ_APPROVED_FILTER,
    QUIZ_DURATION_FALLBACK_BG,
    QUIZ_SUCCESS_RATE_FALLBACK_BG,
    TEACHER_OPEN_ANSWER_STATUSES,
    attach_attempt_question_image_counts,
    attach_question_image_indicators,
    build_mixed_quiz_plan,
    fetch_open_question_candidates,
    fetch_quiz_text_answers_for_attempt,
    is_quiz_question_eligible,
    open_question_scope_summary,
    quiz_assignment_mixed_status,
    quiz_attempt_duration_display,
    quiz_attempt_success_rate_display,
    quiz_combined_score_summary,
    quiz_db,
    quiz_fetch_assignment,
    quiz_parse_assignment_question_plan,
    quiz_parse_attempt_question_plan,
    quiz_section_question_ids,
    quiz_section_question_ids_by_section,
    quiz_teacher_difficulty_breakdown,
    quiz_text_answer_informational_subtotal,
    quiz_text_auto_points,
    quiz_write_assignment_note,
)
from web.db_introspect import sqlite_table_columns, sqlite_table_exists
from web.text_values import unique_text_values


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_ENV = "DZI_DB"
UPLOAD_ROOT_ENV = "DZI_UPLOAD_ROOT"
REFERENCE_ROOT_ENV = "DZI_REFERENCE_ROOT"
SESSION_COOKIE_SECURE_ENV = "DZI_SESSION_COOKIE_SECURE"


def configured_db_path() -> Path:
    """The one database for browsing and quiz code: DZI_DB, else the repository seed."""
    configured = os.environ.get(DB_ENV)
    return Path(configured) if configured else PROJECT_ROOT / "data" / "questions.db"


def configured_reference_root() -> Path | None:
    """Where stored ``data/reference/...`` resources live, if not in the checkout."""
    configured = os.environ.get(REFERENCE_ROOT_ENV)
    return Path(configured) if configured else None


DB_PATH = configured_db_path()
TRUE_ENV_VALUES = {"1", "true", "yes", "on"}
FALSE_ENV_VALUES = {"0", "false", "no", "off"}


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


def build_session_cookie_secure() -> bool:
    configured = os.environ.get(SESSION_COOKIE_SECURE_ENV)
    if configured is None:
        return True

    normalized = configured.strip().lower()
    if normalized in TRUE_ENV_VALUES:
        return True
    if normalized in FALSE_ENV_VALUES:
        return False

    warnings.warn(
        f"{SESSION_COOKIE_SECURE_ENV} has unsupported value {configured!r}; "
        "using secure cookies. Expected one of: 1/true/yes/on or 0/false/no/off.",
        RuntimeWarning,
        stacklevel=2,
    )
    return True


app = Flask(__name__)
app.config["DB_PATH"] = str(DB_PATH)
app.config["SECRET_KEY"] = build_secret_key()
app.config["SESSION_COOKIE_SAMESITE"] = "Strict"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SECURE"] = build_session_cookie_secure()
app.config["PRACTICAL_TASK_BATCH_DIR"] = str(PROJECT_ROOT / "data" / "import_batches")
app.config["PRACTICAL_UPLOAD_ROOT"] = (
    os.environ.get(UPLOAD_ROOT_ENV) or str(PROJECT_ROOT / "data" / "uploads" / "practical")
)
app.config["PRACTICAL_MAX_UPLOAD_BYTES"] = MAX_UPLOAD_BYTES
app.config["PRACTICAL_MAX_FILES_PER_REQUEST"] = MAX_FILES_PER_REQUEST
# Request-wide body limit for every route; uploads are the only large requests.
app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BYTES



# ---------------------------------------------------------------------------
# Local UI profiles
# ---------------------------------------------------------------------------

VALID_UI_PROFILES = {"admin", "tester"}
QUESTION_DIFFICULTY_LABELS = {
    "1": "много лесен",
    "2": "лесен",
    "3": "среден",
    "4": "труден",
    "5": "много труден",
    "easy": "лесен",
    "medium": "среден",
    "hard": "труден",
    "unknown": "не е посочена",
}
QUESTION_KIND_LABELS = {
    "multiple_choice": "затворен въпрос",
    "true_false": "вярно/невярно",
    "short_answer": "отворен въпрос",
    "fill_in": "отворен въпрос",
    "open": "отворен въпрос",
    "free_response": "отворен въпрос",
    "practical_spreadsheet": "практическа задача: електронна таблица",
    "practical_graphics": "практическа задача: графика",
    "practical_web": "практическа задача: уеб",
    "practical": "практическа задача",
}
SUBJECT_STATUS_LABELS = {
    "active": "Активен",
    "in_preparation": "В подготовка",
}


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


def subject_status_label(subject: dict) -> str:
    return SUBJECT_STATUS_LABELS.get(str(subject.get("status", "")), "В подготовка")


def subject_class_range_label(subject: dict) -> str:
    classes = tuple(subject.get("available_classes") or ())
    if not classes:
        return "Класове в подготовка"
    if len(classes) == 1:
        return f"{classes[0]}. клас"
    return f"{min(classes)}.–{max(classes)}. клас"


def grade_list_label(grades) -> str:
    """Bulgarian label for a set of grades: consecutive runs become ranges ("11.–12. клас")."""
    runs: list[list[int]] = []
    for grade in sorted(set(grades)):
        if runs and grade == runs[-1][-1] + 1:
            runs[-1].append(grade)
        else:
            runs.append([grade])
    if not runs:
        return ""
    parts = [f"{run[0]}." if len(run) == 1 else f"{run[0]}.–{run[-1]}." for run in runs]
    return f"{', '.join(parts)} клас"


def subject_search_text(subject: dict) -> str:
    class_terms = " ".join(f"{grade}. клас" for grade in subject.get("available_classes") or ())
    return " ".join(
        part
        for part in (
            str(subject.get("title", "")),
            str(subject.get("search_terms", "")),
            class_terms,
            subject_status_label(subject).lower(),
            str(subject.get("search_content", "")),
        )
        if part
    )


def question_kind_label(value) -> str:
    key = str(value or "").strip().lower()
    return QUESTION_KIND_LABELS.get(key, key or "неизвестен тип")


def question_difficulty_label(value) -> str | None:
    if value is None:
        return None
    key = str(value).strip().lower()
    if not key:
        return None
    return QUESTION_DIFFICULTY_LABELS.get(key)


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
    if request.endpoint == "healthz":
        return  # Monitoring probes get no session cookie.
    profile = session.get("ui_profile", "tester")
    if profile not in VALID_UI_PROFILES:
        profile = "tester"

    if profile == "admin" and not session.get("admin_authenticated"):
        profile = "tester"

    if session.get("ui_profile") not in VALID_UI_PROFILES or (
        session.get("ui_profile") == "admin" and not session.get("admin_authenticated")
    ):
        session.pop("ui_profile", None)
    g.ui_profile = profile



@app.context_processor
def inject_app_meta():
    sync_label = None
    try:
        db_path = Path(app.config.get("DB_PATH", DB_PATH))
        db_mtime = db_path.stat().st_mtime
        vault_path = PROJECT_ROOT / "vault"
        try:
            vault_mtime = vault_path.stat().st_mtime
        except FileNotFoundError:
            vault_mtime = db_mtime
        latest = max(db_mtime, vault_mtime)
        sync_label = datetime.fromtimestamp(latest).strftime("%Y-%m-%d %H:%M")
    except (OSError, TypeError, ValueError, OverflowError):
        sync_label = None

    return {
        "app_version": "UI pass 9",
        "last_sync_label": sync_label,
        "source_url": os.environ.get("DZI_SOURCE_URL"),
    }


@app.context_processor
def inject_subject_navigation():
    def subject_url(subject: dict) -> str:
        return url_for("subject_view", subject_slug=subject["slug"])

    def subject_class_url(subject: dict, grade: int) -> str:
        return url_for("subject_grade_view", subject_slug=subject["slug"], grade=grade)

    def subject_topic_group_url(subject: dict, grade: int, topic_group: dict) -> str:
        return url_for(
            "subject_topic_group_view",
            subject_slug=subject["slug"],
            grade=grade,
            topic_group_slug=topic_group["slug"],
        )

    def subject_section_url(subject: dict, grade: int, section: dict) -> str:
        return url_for(
            "subject_topic_group_view",
            subject_slug=subject["slug"],
            grade=grade,
            topic_group_slug=public_slug(subject["id"], grade, section["section_slug"]),
        )

    def subject_topic_url(subject: dict, grade: int, section: dict, topic: dict) -> str:
        return url_for(
            "subject_topic_view",
            subject_slug=subject["slug"],
            grade=grade,
            topic_group_slug=public_slug(subject["id"], grade, section["section_slug"]),
            topic_slug=public_slug(subject["id"], grade, topic["topic_slug"]),
        )

    def subject_create_test_url(subject: dict, section: dict) -> str:
        return url_for("teacher.teacher_new", subject=subject["slug"], section=section["section_slug"])

    return {
        "learnpilot_subjects": subject_catalog(),
        "subject_url": subject_url,
        "subject_class_url": subject_class_url,
        "subject_topic_group_url": subject_topic_group_url,
        "subject_section_url": subject_section_url,
        "subject_topic_url": subject_topic_url,
        "subject_create_test_url": subject_create_test_url,
        "subject_topic_groups": subject_topic_groups,
        "subject_status_label": subject_status_label,
        "subject_class_range_label": subject_class_range_label,
        "grade_list_label": grade_list_label,
        "subject_search_text": subject_search_text,
    }


@app.context_processor
def inject_ui_profile():
    profile = getattr(g, "ui_profile", "admin")
    return {
        "ui_profile": profile,
        "ui_profile_label": "Админ" if profile == "admin" else "Тестер",
        "question_difficulty_label": question_difficulty_label,
        "question_kind_label": question_kind_label,
    }


@app.route("/profile/<profile>")
@access_policy(PROFILE_SWITCH)
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


def password_matches(submitted_password: str | None, configured_password: str | None) -> bool:
    if not isinstance(submitted_password, str) or not isinstance(configured_password, str):
        return False
    return hmac.compare_digest(
        submitted_password.encode("utf-8", errors="surrogatepass"),
        configured_password.encode("utf-8", errors="surrogatepass"),
    )


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
@access_policy(PUBLIC)
@integrity_policy(SAME_ORIGIN)
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
        elif password_matches(submitted_password, configured_password):
            session["tester_authenticated"] = True
            session["ui_profile"] = "tester"
            return redirect(next_url)
        else:
            error = "Грешна парола."

    return render_template("tester_login.html", error=error, next_url=next_url)


@app.route("/tester/logout")
@access_policy(PUBLIC)
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


@app.before_request
def enforce_access_policy():
    # Route-level authorization follows the policy declared on the resolved view
    # function (web.access), never the endpoint name, so aliases and blueprint
    # endpoint names share one decision. A view without a declared policy is
    # denied. Same-origin request integrity is a separate check that runs first
    # (protect_same_origin_unsafe_methods); passing one never implies the other.
    # attempt_owner routes pass here and keep their own owner check (404) inside
    # the view. The contract is documented in docs/AUTH_ROUTE_CONTRACT.md.
    if request.routing_exception is not None:
        return  # No view matched; Flask answers with its own 404/405/redirect.

    view_args = request.view_args or {}
    level = policy_for_endpoint(app, request.endpoint)
    if is_route_allowed(level, current_principal(), view_args):
        return

    if level == PROFILE_SWITCH:
        next_url = safe_redirect_target(
            request.args.get("next") or request.referrer,
            "index",
        )
    else:
        next_url = request.path
    return redirect(url_for(denial_login_endpoint(level, view_args), next=next_url))


@app.route("/admin/login", methods=["GET", "POST"])
@access_policy(PUBLIC)
@integrity_policy(SAME_ORIGIN)
def admin_login():
    error = None
    next_url = safe_redirect_target(
        request.args.get("next") or request.form.get("next"),
        "teacher.teacher_dashboard",
    )

    if request.method == "POST":
        configured_password = os.environ.get("DZI_ADMIN_PASSWORD", "")
        submitted_password = request.form.get("password", "")

        if not configured_password:
            error = "Админ паролата не е зададена. Стартирай сървъра с DZI_ADMIN_PASSWORD."
        elif password_matches(submitted_password, configured_password):
            session["admin_authenticated"] = True
            session["ui_profile"] = "admin"
            return redirect(next_url)
        else:
            error = "Грешна парола."

    return render_template("admin_login.html", error=error, next_url=next_url)


@app.route("/admin/logout")
@access_policy(ADMIN)
def admin_logout():
    session.pop("admin_authenticated", None)
    session["ui_profile"] = "tester"
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    """Per-request SQLite connection (web.db policy). Closed in teardown."""
    return get_request_db()


@app.teardown_appcontext
def close_db(exception):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def subject_browser(subject_id: str = IT) -> CachedSubjectBrowser:
    """One public browsing facade per subject/request, shared by legacy routes."""
    subject_id = canonical_subject(subject_id)
    browsers = g.setdefault("subject_browsers", {})
    if subject_id not in browsers:
        browsers[subject_id] = CachedSubjectBrowser(
            get_db(), subject_id, legacy_it=subject_id == IT
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
        if q["question_type"] in {"fill_in", "short_answer"}:
            q["subquestions"] = fetch_fill_in_answer_rows(db, int(q["question_id"]))
        else:
            q["subquestions"] = []
        questions.append(q)
    return attach_question_visual_assets(db, questions, id_key="question_id")


# DZI_REFERENCE_ROOT relocates only data/reference (official DZI resources);
# data/assets always stays in the checkout.
PRACTICAL_REFERENCE_ROOT = configured_reference_root()
PRACTICAL_RESOURCE_ROOTS = (
    PRACTICAL_REFERENCE_ROOT or PROJECT_ROOT / "data" / "reference",
    PROJECT_ROOT / "data" / "assets",
)
# The practical blueprint and service read these from configuration per request.
app.config["PROJECT_ROOT"] = PROJECT_ROOT
app.config["PRACTICAL_REFERENCE_ROOT"] = PRACTICAL_REFERENCE_ROOT
app.config["PRACTICAL_RESOURCE_ROOTS"] = PRACTICAL_RESOURCE_ROOTS


def dzi_find_exam(source_slug: str) -> dict | None:
    return dzi_exams.dzi_find_exam(get_db(), source_slug)


def fetch_dzi_tasks(exam_id: int) -> dict[str, list[dict]]:
    return dzi_exams.fetch_dzi_tasks(get_db(), exam_id)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/tester")
@access_policy(PUBLIC)
def tester_home():
    return render_template("tester_home.html")


@app.route("/")
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


def _subject_browser(subject: dict) -> CachedSubjectBrowser | None:
    """Browser for subjects that own stored sections; None while in preparation."""
    if not subject["content_grades"]:
        return None
    return subject_browser(subject["id"])


def render_subject_page(subject_slug: str):
    subject = catalog_subject(subject_slug)
    if subject is None:
        abort(404)

    grade_rows = []
    browser = _subject_browser(subject)
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


def render_subject_grade_page(subject_slug: str, grade: int):
    subject = catalog_subject(subject_slug)
    if subject is None or grade not in subject["available_classes"]:
        abort(404)

    sections = []
    browser = _subject_browser(subject)
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


def _subject_section_content(browser: CachedSubjectBrowser, section: dict) -> tuple[list[dict], list[dict]]:
    """A section's topics (with question counts) and questions, from cached reads."""
    questions = fetch_questions_in_section(section["section_id"], browser=browser)
    counts: dict[int, int] = {}
    for question in questions:
        counts[int(question["topic_id"])] = counts.get(int(question["topic_id"]), 0) + 1
    topics = browser.topics_in_section(section["section_id"])
    for topic in topics:
        topic["question_count"] = counts.get(int(topic["topic_id"]), 0)
    return topics, questions


def render_subject_topic_group_page(subject_slug: str, grade: int, topic_group_slug: str):
    subject = catalog_subject(subject_slug)
    if subject is None or grade not in subject["available_classes"]:
        abort(404)

    browser = _subject_browser(subject)
    if browser is not None:
        section = browser.section_for_url(topic_group_slug, grade)
        if section is not None:
            topics, questions = _subject_section_content(browser, section)
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
    browser = _subject_browser(subject)
    if browser is None:
        abort(404)
    section = browser.section_for_url(section_slug, grade)
    if section is None:
        abort(404)

    # The section's cached, ownership-checked topics and questions are filtered
    # here; a topic page adds no query path of its own.
    topics, section_questions = _subject_section_content(browser, section)
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


@app.route("/subjects/<subject_slug>")
@access_policy(PUBLIC)
def subject_view(subject_slug: str):
    return render_subject_page(subject_slug)


@app.route("/subjects/<subject_slug>/<int:grade>")
@access_policy(PUBLIC)
def subject_grade_view(subject_slug: str, grade: int):
    return render_subject_grade_page(subject_slug, grade)


@app.route("/subjects/<subject_slug>/<int:grade>/<topic_group_slug>")
@access_policy(PUBLIC)
def subject_topic_group_view(subject_slug: str, grade: int, topic_group_slug: str):
    return render_subject_topic_group_page(subject_slug, grade, topic_group_slug)


@app.route("/subjects/<subject_slug>/<int:grade>/<topic_group_slug>/<topic_slug>")
@access_policy(PUBLIC)
def subject_topic_view(subject_slug: str, grade: int, topic_group_slug: str, topic_slug: str):
    return render_subject_topic_page(subject_slug, grade, topic_group_slug, topic_slug)


# Pre-registry Physics URLs: permanent redirects to the generic subject pages.
@app.route("/physics")
@access_policy(PUBLIC)
def physics():
    return redirect(url_for("subject_view", subject_slug="physics"), code=301)


@app.route("/physics/grade/<int:grade>")
@access_policy(PUBLIC)
def physics_grade(grade: int):
    if not subject_supports_grade("physics", grade):
        abort(404)
    return redirect(url_for("subject_grade_view", subject_slug="physics", grade=grade), code=301)


@app.route("/favicon.ico")
@access_policy(PUBLIC)
def favicon():
    return send_file(PROJECT_ROOT / "web" / "static" / "img" / "brand" / "favicon.ico", mimetype="image/x-icon")


HEALTH_DB_TIMEOUT_SECONDS = 2.0


def open_existing_database_read_only(path, *, timeout: float = 5.0) -> sqlite3.Connection:
    """Never creates a missing database file; this connection refuses SQL writes."""
    return web_db.connect_db(path, query_only=True, timeout=timeout)


@app.route("/healthz")
@access_policy(PUBLIC)
def healthz():
    """Liveness plus one cheap database header read; no templates or content queries."""
    try:
        conn = open_existing_database_read_only(
            app.config["DB_PATH"], timeout=HEALTH_DB_TIMEOUT_SECONDS
        )
        try:
            conn.execute("PRAGMA schema_version").fetchone()
        finally:
            conn.close()
    except (sqlite3.Error, OSError, ValueError) as exc:
        app.logger.warning("Health check failed: %s", type(exc).__name__)
        status, body = 503, "unavailable\n"
    else:
        status, body = 200, "ok\n"
    return app.response_class(
        body, status=status, mimetype="text/plain",
        headers={"Cache-Control": "no-store"},
    )


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
        browser = _subject_browser(subject)
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


@app.route("/grade/<int:grade>")
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


@app.route("/section/<slug>")
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


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------

@app.errorhandler(404)
def not_found(e):
    return render_template("404.html"), 404


@app.errorhandler(500)
def internal_server_error(e):
    return render_template("500.html"), 500


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------



# === QUIZ PHASE 2 START ===

import csv as _quiz_csv
import io as _quiz_io
import json as _quiz_json
import os as _quiz_os
import unicodedata as _quiz_unicodedata
from pathlib import Path as _QuizPath

from flask import Response as _quiz_response
from flask import abort as _quiz_abort
from flask import redirect as _quiz_redirect
from flask import request as _quiz_request
from flask import url_for as _quiz_url_for
from flask import render_template as _quiz_render_template


QUIZ_DB_PATH = DB_PATH  # Same database as browsing; see configured_db_path().
QUIZ_VAULT_PATH = _QuizPath(
    _quiz_os.environ.get(
        "DZI_VAULT",
        str(_QuizPath.home() / "dzi-generator" / "vault"),
    )
)

# Quiz services never import this module; register where they read and write.
# The providers read these module globals on every use, so reconfiguring
# QUIZ_DB_PATH / QUIZ_VAULT_PATH / PROJECT_ROOT (tests, deployments) still applies.
quiz_service.configure_runtime(
    db_path=lambda: QUIZ_DB_PATH,
    vault_path=lambda: QUIZ_VAULT_PATH,
    project_root=lambda: PROJECT_ROOT,
)


# The web application never changes schema: that is the explicit
# `python3 -m src.manage_migrations --db ... --apply` step. Local runs only get
# a read-only warning; web.wsgi refuses to start while migrations are pending.
warn_if_schema_migrations_pending(DB_PATH, open_existing_database_read_only)





# Keep legacy quiz endpoint names available for existing url_for(...) calls
# while the canonical route declarations live on the quiz blueprint.
register_legacy_quiz_endpoint_aliases(app)
app.register_blueprint(dzi_bp)
app.register_blueprint(practical_bp)
app.register_blueprint(quiz_bp)
app.register_blueprint(teacher_bp)

# === QUIZ PHASE 2 END ===


if __name__ == "__main__":
    if not DB_PATH.exists():
        print(f"❌ DB не намерен: {DB_PATH}")
        raise SystemExit(1)
    print(f"📚 LearnPilot — Web app")
    print(f"   DB:      {DB_PATH}")
    print(f"   URL:     http://127.0.0.1:5000")
    print(f"   Stop:    Ctrl+C")
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host="127.0.0.1", port=5000, debug=debug)
