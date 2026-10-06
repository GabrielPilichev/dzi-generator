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

import os
import hmac
import json
import math
import secrets
import warnings
from io import BytesIO

import sqlite3
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, abort, g, render_template, url_for, request, redirect, send_file, session
from werkzeug.exceptions import RequestEntityTooLarge

from src.subject_browsing import public_slug, stored_slug_candidates, subject_content_grades
from src.subject_browse_cache import CachedSubjectBrowser
from src.subject_assessment import AssessmentScope, question_belongs_to_subject
from src.subject_content import IT, canonical_subject
from src.subject_registry import (
    PLATFORM_GRADES, all_subjects, get_subject, subject_for_route_slug, subject_supports_grade,
)
from src.practical_resource_resolution import (
    ResourceResolutionError,
    read_zip_member,
    resolve_resource_reference,
)
from src.practical_uploads import (
    MAX_FILES_PER_REQUEST,
    MAX_REQUEST_BYTES,
    MAX_UPLOAD_BYTES,
    PracticalUploadError,
    build_practical_upload_path,
    sanitize_original_filename,
    validate_upload_capacity,
    validate_upload_extension,
    validate_upload_size,
)
from web.blueprints.quiz import (
    has_quiz_attempt_access,
    mark_quiz_attempt_owner,
    quiz_attempt_owner_session_key,
    quiz_bp,
    register_legacy_quiz_endpoint_aliases,
    require_quiz_attempt_access,
)
from web import db as web_db
from web.access import (
    ADMIN,
    ATTEMPT_OWNER,
    PROFILE_SWITCH,
    PUBLIC,
    TEACHER,
    access_policy,
)
from web.request_integrity import SAME_ORIGIN, integrity_policy
from web.production import warn_if_schema_migrations_pending
from web.services import quiz as quiz_service
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
    quiz_teacher_difficulty_breakdown,
    quiz_text_answer_informational_subtotal,
    quiz_text_auto_points,
    quiz_write_assignment_note,
)
from web.db_introspect import first_existing_column, sqlite_table_columns, sqlite_table_exists
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
TESTER_TEACHER_ENDPOINTS = {"teacher_new", "teacher_assignment"}
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


def subject_catalog() -> tuple[dict, ...]:
    """Registered subjects with content-derived availability, cached per request.

    Static metadata comes from the canonical registry; status and content grades
    are derived from stored curriculum sections. A database error degrades to
    every subject being in preparation instead of failing the page.
    """
    if "subject_catalog" in g:
        return g.subject_catalog
    try:
        content_grades = subject_content_grades(get_db())
    except sqlite3.Error as exc:
        app.logger.warning("Subject availability lookup failed: %s", exc)
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
        return url_for("teacher_new", subject=subject["slug"], section=section["section_slug"])

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


PUBLIC_DZI_ENDPOINTS: set[str] = set()
# DZI endpoints default to admin-only. Add future intentionally public
# endpoint names here instead of weakening the default-deny guard below.


@app.before_request
def protect_admin_routes():
    # Route access contract:
    # - endpoint names starting with "admin" are admin-only, except admin_login.
    # - endpoint names starting with "dzi" are admin-only unless explicitly listed
    #   in PUBLIC_DZI_ENDPOINTS.
    # - endpoint names starting with "teacher" are admin-only, except the
    #   TESTER_TEACHER_ENDPOINTS allowlist used for tester assignment creation/detail.
    # - switch_profile is public only for switching to tester; switching to admin
    #   redirects to admin login unless the session is already admin-authenticated.
    # - quiz attempt routes do not use endpoint prefixes for ownership; they call
    #   require_quiz_attempt_access(attempt_id) inside each attempt route.
    # Future blueprint extraction must preserve these endpoint names or update this
    # guard and docs/AUTH_ROUTE_CONTRACT.md together.
    # Each view also declares an explicit policy (web.access). This guard still
    # enforces access; tests/test_access_policy.py asserts both agree, and
    # roadmap PR D2 replaces this guard with the declared policies.
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
        return redirect(url_for("admin_login", next=request.path))

    if endpoint in TESTER_TEACHER_ENDPOINTS and not can_generate_tests():
        return redirect(url_for("tester_login", next=request.path))

    if endpoint.startswith("teacher") and endpoint not in TESTER_TEACHER_ENDPOINTS and not is_admin_authenticated():
        return redirect(url_for("admin_login", next=request.path))


@app.route("/admin/login", methods=["GET", "POST"])
@access_policy(PUBLIC)
@integrity_policy(SAME_ORIGIN)
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
    if "db" not in g:
        g.db = web_db.connect_db(app.config["DB_PATH"])
    return g.db


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
# DZI_REFERENCE_ROOT relocates only data/reference (official DZI resources);
# data/assets always stays in the checkout.
PRACTICAL_REFERENCE_ROOT = configured_reference_root()
PRACTICAL_RESOURCE_ROOTS = (
    PRACTICAL_REFERENCE_ROOT or PROJECT_ROOT / "data" / "reference",
    PROJECT_ROOT / "data" / "assets",
)


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


def fetch_practical_resource_count(exam_id: int) -> int:
    db = get_db()
    row = db.execute("""
        SELECT COUNT(*)
        FROM practical_task_resources ptr
        JOIN exam_tasks et ON et.id = ptr.exam_task_id
        WHERE et.exam_id = ?
          AND et.task_number BETWEEN 26 AND 28
    """, (exam_id,)).fetchone()
    return int(row[0] or 0)


def fetch_practical_assignment_options(
    conn: sqlite3.Connection, *, scope: AssessmentScope
) -> list[dict]:
    if not scope.allows_dzi_content:
        return []
    required_tables = ("exams", "exam_tasks", "practical_task_resources")
    if not all(sqlite_table_exists(conn, table) for table in required_tables):
        return []

    rows = conn.execute("""
        SELECT
            e.*,
            COUNT(DISTINCT et.id) AS practical_task_count,
            COUNT(ptr.id) AS resource_count
        FROM exams e
        JOIN exam_tasks et
          ON et.exam_id = e.id
         AND et.task_number BETWEEN 26 AND 28
         AND et.task_kind LIKE 'practical_%'
        JOIN practical_task_resources ptr
          ON ptr.exam_task_id = et.id
        WHERE e.format_version = ? AND e.level = 'DZI'
        GROUP BY e.id
        HAVING resource_count > 0
        ORDER BY e.year DESC, e.session, e.variant
    """, (DZI_FORMAT_VERSION,)).fetchall()

    options = []
    for row in rows:
        if canonical_subject(row["subject"]) != IT:
            continue
        option = dict(row)
        option["source_slug"] = dzi_source_slug(row)
        option["title"] = dzi_source_title(row)
        options.append(option)
    return options


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


def fetch_dzi_fill_subquestions(question_id: int) -> list[dict]:
    db = get_db()
    return fetch_fill_in_answer_rows(db, question_id)


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
            task["options"] = fetch_dzi_question_options(int(task["question_id"]))
        elif task["question_id"] and task["question_type"] in {"fill_in", "short_answer"}:
            task["subquestions"] = fetch_dzi_fill_subquestions(int(task["question_id"]))

        key = "part1" if int(task["task_number"]) <= 25 else "part2"
        grouped[key].append(task)
    return grouped


def practical_task_batch_path(source_slug: str) -> Path:
    return Path(app.config["PRACTICAL_TASK_BATCH_DIR"]) / f"{source_slug}_practical_tasks_26_28.json"


def load_practical_task_batch(source_slug: str) -> dict[int, dict]:
    path = practical_task_batch_path(source_slug)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if payload.get("source_slug") != source_slug:
        return {}
    tasks = payload.get("tasks")
    if not isinstance(tasks, list):
        return {}
    by_number = {}
    for task in tasks:
        if not isinstance(task, dict):
            continue
        number = task.get("task_number")
        if isinstance(number, int):
            by_number[number] = task
    return by_number


def practical_resource_status(resource: dict) -> dict:
    resource = dict(resource)
    try:
        resolve_resource_reference(
            resource.get("resource_path"),
            project_root=PROJECT_ROOT,
            allowed_roots=PRACTICAL_RESOURCE_ROOTS,
            reference_root=PRACTICAL_REFERENCE_ROOT,
        )
    except ResourceResolutionError as exc:
        resource["is_path_safe"] = exc.reason not in {"unsafe_path", "invalid_archive"}
        resource["is_available"] = False
    else:
        resource["is_path_safe"] = True
        resource["is_available"] = True
    return resource


def safe_download_name(candidate: str | None, fallback: str) -> str:
    name = candidate if isinstance(candidate, str) else ""
    name = name.replace("\x00", "").replace("\r", "").replace("\n", "").strip()
    name = name.replace("/", "_").replace("\\", "_")
    return name or fallback


def fetch_practical_resource(resource_id: int) -> dict | None:
    db = get_db()
    row = db.execute("""
        SELECT
            ptr.id,
            ptr.exam_task_id,
            ptr.resource_path,
            ptr.original_filename,
            ptr.label_bg,
            ptr.file_size_bytes,
            ptr.sha256,
            et.task_number,
            et.exam_id,
            e.year,
            e.session,
            e.variant
        FROM practical_task_resources ptr
        JOIN exam_tasks et ON et.id = ptr.exam_task_id
        JOIN exams e ON e.id = et.exam_id
        WHERE ptr.id = ?
    """, (resource_id,)).fetchone()
    return dict(row) if row else None


def fetch_practical_tasks(source_slug: str) -> tuple[dict, list[dict]] | None:
    exam = dzi_find_exam(source_slug)
    if exam is None:
        return None

    batch_tasks = load_practical_task_batch(source_slug)
    db = get_db()
    task_rows = db.execute("""
        SELECT
            et.id AS task_id,
            et.task_number,
            et.task_kind,
            et.points,
            et.prompt,
            et.rubric,
            pt.work_environment,
            pt.expected_outputs_json,
            pt.notes
        FROM exam_tasks et
        LEFT JOIN practical_tasks pt ON pt.task_id = et.id
        WHERE et.exam_id = ?
          AND et.task_number BETWEEN 26 AND 28
          AND et.task_kind LIKE 'practical_%'
        ORDER BY et.task_number
    """, (exam["id"],)).fetchall()

    tasks = []
    for row in task_rows:
        task = dict(row)
        batch_task = batch_tasks.get(int(task["task_number"]), {})
        task["prompt_bg"] = batch_task.get("prompt_bg") or task.get("prompt")
        task["instructions_bg"] = batch_task.get("instructions_bg") or task.get("rubric")
        task["expected_outputs"] = batch_task.get("expected_outputs") or dzi_json_text_list(task.get("expected_outputs_json"))
        resource_rows = db.execute("""
            SELECT id, resource_path, original_filename, label_bg, file_size_bytes, sha256
            FROM practical_task_resources
            WHERE exam_task_id = ?
            ORDER BY id
        """, (task["task_id"],)).fetchall()
        task["resources"] = [
            practical_resource_status(dict(resource))
            for resource in resource_rows
        ]
        tasks.append(task)
    return exam, tasks


def fetch_practical_submission_review(conn: sqlite3.Connection, submission_id: int) -> dict | None:
    if sqlite_table_exists(conn, "practical_submission_grades"):
        columns = sqlite_table_columns(conn, "practical_submission_grades")
        if "practical_submission_id" in columns:
            score_col = first_existing_column(columns, ("score", "manual_score"))
            max_score_col = first_existing_column(columns, ("max_score", "manual_score_max"))
            note_col = first_existing_column(columns, ("teacher_note", "teacher_note_bg", "note"))
            reviewed_at_col = first_existing_column(columns, ("reviewed_at", "created_at", "updated_at"))
            if score_col or note_col:
                selected = []
                aliases = {
                    "score": score_col,
                    "max_score": max_score_col,
                    "teacher_note": note_col,
                    "reviewed_at": reviewed_at_col,
                }
                # SAFE: selected columns and ORDER BY column come from schema columns
                # intersected with fixed candidate whitelists above.
                for alias, column in aliases.items():
                    if column:
                        selected.append(f"{column} AS {alias}")
                    else:
                        selected.append(f"NULL AS {alias}")
                order_by = first_existing_column(columns, ("id", "reviewed_at", "updated_at", "created_at"))
                order_clause = f"ORDER BY {order_by} DESC" if order_by else ""
                row = conn.execute(f"""
                    SELECT {", ".join(selected)}
                    FROM practical_submission_grades
                    WHERE practical_submission_id = ?
                    {order_clause}
                    LIMIT 1
                """, (submission_id,)).fetchone()
                if row is not None:
                    review = dict(row)
                    if any(review.get(key) is not None for key in ("score", "max_score", "teacher_note", "reviewed_at")):
                        return review

    columns = sqlite_table_columns(conn, "practical_submissions")
    score_col = first_existing_column(columns, ("manual_score", "score"))
    max_score_col = first_existing_column(columns, ("manual_score_max", "max_score"))
    note_col = first_existing_column(columns, ("teacher_note", "teacher_note_bg", "note"))
    reviewed_at_col = first_existing_column(columns, ("reviewed_at",))
    if not (score_col or note_col or reviewed_at_col):
        return None

    aliases = {
        "score": score_col,
        "max_score": max_score_col,
        "teacher_note": note_col,
        "reviewed_at": reviewed_at_col,
    }
    selected = []
    # SAFE: selected columns come from schema columns intersected with fixed
    # candidate whitelists above.
    for alias, column in aliases.items():
        if column:
            selected.append(f"{column} AS {alias}")
        else:
            selected.append(f"NULL AS {alias}")
    row = conn.execute(f"""
        SELECT {", ".join(selected)}
        FROM practical_submissions
        WHERE id = ?
    """, (submission_id,)).fetchone()
    if row is None:
        return None
    review = dict(row)
    if any(review.get(key) is not None for key in ("score", "max_score", "teacher_note", "reviewed_at")):
        return review
    return None


def fetch_practical_attempt_page(attempt_id: int, source_slug: str) -> tuple[dict, dict, list[dict]] | None:
    db = get_db()
    attempt_row = db.execute("""
        SELECT *
        FROM quiz_attempts
        WHERE id = ?
    """, (attempt_id,)).fetchone()
    if attempt_row is None:
        return None

    resolved = fetch_practical_tasks(source_slug)
    if resolved is None:
        return None
    exam, tasks = resolved

    for task in tasks:
        submission = db.execute("""
            SELECT id, status, submitted_at, created_at, updated_at
            FROM practical_submissions
            WHERE quiz_attempt_id = ? AND exam_task_id = ?
        """, (attempt_id, task["task_id"])).fetchone()
        task["submission"] = dict(submission) if submission else None
        task["review"] = fetch_practical_submission_review(db, int(submission["id"])) if submission else None
        if submission:
            file_rows = db.execute("""
                SELECT id, original_filename, size_bytes, mime_type, uploaded_at, is_deleted
                FROM practical_submission_files
                WHERE practical_submission_id = ?
                  AND is_deleted = 0
                ORDER BY uploaded_at, id
            """, (submission["id"],)).fetchall()
            task["uploaded_files"] = [dict(row) for row in file_rows]
        else:
            task["uploaded_files"] = []
    return dict(attempt_row), exam, tasks


def fetch_practical_upload_task(conn: sqlite3.Connection, *, attempt_id: int, source_slug: str, exam_task_id: int):
    attempt = conn.execute("""
        SELECT *
        FROM quiz_attempts
        WHERE id = ?
    """, (attempt_id,)).fetchone()
    if attempt is None:
        return None, None, None

    exam = dzi_find_exam(source_slug)
    if exam is None:
        return attempt, None, None

    task = conn.execute("""
        SELECT id, task_number, task_kind
        FROM exam_tasks
        WHERE id = ?
          AND exam_id = ?
          AND task_number BETWEEN 26 AND 28
          AND task_kind LIKE 'practical_%'
    """, (exam_task_id, exam["id"])).fetchone()
    if task is None:
        return attempt, exam, None
    return attempt, exam, task


def uploaded_file_size(file_storage) -> int:
    stream = file_storage.stream
    current_position = stream.tell()
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    stream.seek(current_position)
    return int(size)


def upload_error_redirect(attempt_id: int, source_slug: str, error_code: str):
    return redirect(url_for(
        "quiz_practical_attempt",
        attempt_id=attempt_id,
        source_slug=source_slug,
        upload_error=error_code,
    ))


def practical_uploaded_file_safe_path(stored_path: str | None) -> Path | None:
    if not isinstance(stored_path, str) or not stored_path.strip():
        return None
    raw_path = Path(stored_path)
    if raw_path.is_absolute() or ".." in raw_path.parts:
        return None
    try:
        relative_path = raw_path.relative_to(Path("data/uploads/practical"))
    except ValueError:
        return None
    upload_root = Path(app.config["PRACTICAL_UPLOAD_ROOT"])
    candidate = (upload_root / relative_path).resolve(strict=False)
    root = upload_root.resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def fetch_practical_submission_file(file_id: int) -> dict | None:
    db = get_db()
    row = db.execute("""
        SELECT
            psf.id,
            psf.practical_submission_id,
            psf.stored_path,
            psf.original_filename,
            psf.size_bytes,
            psf.mime_type,
            psf.uploaded_at,
            psf.is_deleted,
            ps.quiz_attempt_id,
            ps.exam_task_id,
            qt.student_name,
            et.task_number,
            et.task_kind
        FROM practical_submission_files psf
        JOIN practical_submissions ps ON ps.id = psf.practical_submission_id
        JOIN quiz_attempts qt ON qt.id = ps.quiz_attempt_id
        JOIN exam_tasks et ON et.id = ps.exam_task_id
        WHERE psf.id = ?
    """, (file_id,)).fetchone()
    return dict(row) if row else None


def fetch_practical_submission_review_rows() -> list[dict]:
    db = get_db()
    rows = db.execute("""
        SELECT
            ps.id AS submission_id,
            ps.status,
            ps.submitted_at,
            ps.created_at,
            ps.updated_at,
            qt.id AS attempt_id,
            qt.student_name,
            qt.assignment_id,
            qa.title_bg AS assignment_title,
            et.id AS exam_task_id,
            et.task_number,
            et.task_kind,
            e.year,
            e.session,
            e.variant,
            psf.id AS file_id,
            psf.original_filename,
            psf.size_bytes,
            psf.mime_type,
            psf.uploaded_at,
            psf.is_deleted,
            psf.stored_path
        FROM practical_submissions ps
        JOIN quiz_attempts qt ON qt.id = ps.quiz_attempt_id
        LEFT JOIN quiz_assignments qa ON qa.id = qt.assignment_id
        JOIN exam_tasks et ON et.id = ps.exam_task_id
        JOIN exams e ON e.id = et.exam_id
        LEFT JOIN practical_submission_files psf
            ON psf.practical_submission_id = ps.id
        ORDER BY ps.updated_at DESC, ps.id DESC, psf.uploaded_at DESC, psf.id DESC
    """).fetchall()

    submissions_by_id: dict[int, dict] = {}
    for row in rows:
        submission_id = int(row["submission_id"])
        if submission_id not in submissions_by_id:
            exam = {
                "year": row["year"],
                "session": row["session"],
                "variant": row["variant"],
            }
            submissions_by_id[submission_id] = {
                "id": submission_id,
                "status": row["status"],
                "submitted_at": row["submitted_at"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "attempt_id": row["attempt_id"],
                "student_name": row["student_name"],
                "assignment_id": row["assignment_id"],
                "assignment_title": row["assignment_title"],
                "exam_task_id": row["exam_task_id"],
                "task_number": row["task_number"],
                "task_kind": row["task_kind"],
                "source_slug": dzi_source_slug(exam),
                "files": [],
            }
        if row["file_id"] is not None:
            safe_path = practical_uploaded_file_safe_path(row["stored_path"])
            submissions_by_id[submission_id]["files"].append({
                "id": row["file_id"],
                "original_filename": row["original_filename"],
                "size_bytes": row["size_bytes"],
                "mime_type": row["mime_type"],
                "uploaded_at": row["uploaded_at"],
                "is_deleted": bool(row["is_deleted"]),
                "is_available": bool(safe_path and safe_path.is_file() and not row["is_deleted"]),
            })
    return list(submissions_by_id.values())


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


@app.route("/dzi")
@access_policy(ADMIN)
def dzi_index():
    sources = fetch_dzi_sources()
    return render_template("dzi_index.html", sources=sources)


@app.route("/dzi/source/<source_slug>")
@access_policy(ADMIN)
def dzi_source_view(source_slug: str):
    exam = dzi_find_exam(source_slug)
    if exam is None:
        abort(404)
    tasks = fetch_dzi_tasks(int(exam["id"]))
    practical_resource_count = fetch_practical_resource_count(int(exam["id"]))
    return render_template(
        "dzi_source.html",
        exam=exam,
        tasks=tasks,
        practical_resource_count=practical_resource_count,
    )


@app.route("/dzi/source/<source_slug>/practical")
@access_policy(ADMIN)
def dzi_practical_source(source_slug: str):
    resolved = fetch_practical_tasks(source_slug)
    if resolved is None:
        abort(404)
    exam, tasks = resolved
    return render_template("practical_tasks.html", exam=exam, tasks=tasks)


@app.route("/dzi/practical/resource/<int:resource_id>/download")
@access_policy(ADMIN)
def dzi_practical_resource_download(resource_id: int):
    resource = fetch_practical_resource(resource_id)
    if resource is None:
        app.logger.warning(
            "Practical resource download rejected: reason=not_found resource_id=%s",
            resource_id,
        )
        abort(404)

    try:
        resolved = resolve_resource_reference(
            resource.get("resource_path"),
            project_root=PROJECT_ROOT,
            allowed_roots=PRACTICAL_RESOURCE_ROOTS,
            reference_root=PRACTICAL_REFERENCE_ROOT,
        )
        download_name = safe_download_name(resource.get("original_filename"), resolved.filename)
        if resolved.kind == "zip_member":
            response = send_file(
                BytesIO(read_zip_member(resolved)),
                as_attachment=True,
                download_name=download_name,
                mimetype="application/octet-stream",
                max_age=0,
            )
            response.headers["X-Content-Type-Options"] = "nosniff"
            return response
        return send_file(
            resolved.path,
            as_attachment=True,
            download_name=download_name,
            max_age=0,
        )
    except ResourceResolutionError as exc:
        app.logger.warning(
            "Practical resource download rejected: reason=%s resource_id=%s exam_task_id=%s task_number=%s",
            exc.reason,
            resource_id,
            resource.get("exam_task_id"),
            resource.get("task_number"),
        )
        abort(404)
    except (OSError, ValueError) as exc:
        app.logger.warning(
            "Practical resource download rejected: reason=send_failed resource_id=%s exam_task_id=%s task_number=%s error_type=%s",
            resource_id,
            resource.get("exam_task_id"),
            resource.get("task_number"),
            type(exc).__name__,
        )
        abort(404)


@app.route("/quiz/attempt/<int:attempt_id>/practical/<source_slug>")
@access_policy(ATTEMPT_OWNER)
def quiz_practical_attempt(attempt_id: int, source_slug: str):
    require_quiz_attempt_access(attempt_id)
    resolved = fetch_practical_attempt_page(attempt_id, source_slug)
    if resolved is None:
        abort(404)
    attempt, exam, tasks = resolved
    return render_template(
        "practical_tasks.html",
        exam=exam,
        tasks=tasks,
        attempt=attempt,
        upload_success=request.args.get("uploaded") == "1",
        upload_error=request.args.get("upload_error"),
    )


@app.route("/quiz/attempt/<int:attempt_id>/practical/<source_slug>/task/<int:exam_task_id>/upload", methods=["POST"])
@access_policy(ATTEMPT_OWNER)
@integrity_policy(SAME_ORIGIN)
def quiz_practical_upload(attempt_id: int, source_slug: str, exam_task_id: int):
    require_quiz_attempt_access(attempt_id)
    conn = get_db()
    attempt, exam, task = fetch_practical_upload_task(
        conn,
        attempt_id=attempt_id,
        source_slug=source_slug,
        exam_task_id=exam_task_id,
    )
    if attempt is None or exam is None or task is None:
        app.logger.warning(
            "Practical upload rejected: reason=context_not_found attempt_id=%s source_slug=%s exam_task_id=%s",
            attempt_id,
            source_slug,
            exam_task_id,
        )
        abort(404)

    try:
        submitted_files = request.files.getlist("files")
    except RequestEntityTooLarge:
        app.logger.warning(
            "Practical upload rejected: reason=request_too_large attempt_id=%s source_slug=%s exam_task_id=%s",
            attempt_id,
            source_slug,
            exam_task_id,
        )
        return upload_error_redirect(attempt_id, source_slug, "too_large")
    uploaded_files = [
        file_storage
        for file_storage in submitted_files
        if file_storage and (file_storage.filename or "").strip()
    ]
    if not uploaded_files:
        app.logger.info(
            "Practical upload rejected: reason=empty attempt_id=%s source_slug=%s exam_task_id=%s",
            attempt_id,
            source_slug,
            exam_task_id,
        )
        return upload_error_redirect(attempt_id, source_slug, "empty")

    if len(uploaded_files) > int(app.config["PRACTICAL_MAX_FILES_PER_REQUEST"]):
        app.logger.warning(
            "Practical upload rejected: reason=too_many_files attempt_id=%s source_slug=%s exam_task_id=%s file_count=%s",
            attempt_id,
            source_slug,
            exam_task_id,
            len(uploaded_files),
        )
        return upload_error_redirect(attempt_id, source_slug, "invalid")

    prepared_files = []
    try:
        for file_storage in uploaded_files:
            original_filename = sanitize_original_filename(file_storage.filename or "")
            validate_upload_extension(original_filename)
            size_bytes = validate_upload_size(
                uploaded_file_size(file_storage),
                max_bytes=int(app.config["PRACTICAL_MAX_UPLOAD_BYTES"]),
            )
            file_storage.stream.seek(0)
            prepared_files.append((file_storage, original_filename, size_bytes))
    except PracticalUploadError as exc:
        app.logger.warning(
            "Practical upload rejected: reason=validation attempt_id=%s source_slug=%s exam_task_id=%s file_count=%s error_type=%s",
            attempt_id,
            source_slug,
            exam_task_id,
            len(uploaded_files),
            type(exc).__name__,
        )
        return upload_error_redirect(attempt_id, source_slug, "invalid")

    saved_paths = []
    try:
        # Serialize quota checks and inserts across workers. The existing route
        # already held a write transaction while saving files.
        conn.execute("BEGIN IMMEDIATE")
        current_attempt = conn.execute(
            "SELECT submitted_at FROM quiz_attempts WHERE id = ?", (attempt_id,)
        ).fetchone()
        if current_attempt is None or current_attempt["submitted_at"] is not None:
            raise PracticalUploadError("quiz attempt is closed")
        submission = conn.execute("""
            SELECT id, status
            FROM practical_submissions
            WHERE quiz_attempt_id = ? AND exam_task_id = ?
        """, (attempt_id, exam_task_id)).fetchone()
        if submission is not None and submission["status"] != "draft":
            raise PracticalUploadError("practical submission is closed")
        totals = conn.execute("""
            SELECT COUNT(*), COALESCE(SUM(psf.size_bytes), 0)
            FROM practical_submission_files psf
            JOIN practical_submissions ps ON ps.id = psf.practical_submission_id
            WHERE ps.quiz_attempt_id = ? AND ps.exam_task_id = ?
        """, (attempt_id, exam_task_id)).fetchone()
        validate_upload_capacity(
            Path(app.config["PRACTICAL_UPLOAD_ROOT"]),
            sum(size for _, _, size in prepared_files),
            existing_bytes=int(totals[1]), existing_files=int(totals[0]),
            incoming_files=len(prepared_files),
        )
        if submission is None:
            cur = conn.execute("""
                INSERT INTO practical_submissions (
                    quiz_attempt_id, exam_task_id, status, updated_at
                )
                VALUES (?, ?, 'draft', CURRENT_TIMESTAMP)
            """, (attempt_id, exam_task_id))
            submission_id = int(cur.lastrowid)
        else:
            submission_id = int(submission["id"])
            conn.execute("""
                UPDATE practical_submissions
                SET updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (submission_id,))

        for file_storage, original_filename, size_bytes in prepared_files:
            stored = build_practical_upload_path(
                upload_root=Path(app.config["PRACTICAL_UPLOAD_ROOT"]),
                attempt_id=attempt_id,
                exam_task_id=exam_task_id,
                submission_id=submission_id,
                original_filename=original_filename,
                size_bytes=size_bytes,
            )
            stored.absolute_path.parent.mkdir(parents=True, exist_ok=True)
            saved_paths.append(stored.absolute_path)
            file_storage.save(stored.absolute_path)
            if stored.absolute_path.stat().st_size != size_bytes:
                raise PracticalUploadError("incomplete practical upload")
            conn.execute("""
                INSERT INTO practical_submission_files (
                    practical_submission_id, stored_path, original_filename,
                    size_bytes, mime_type
                )
                VALUES (?, ?, ?, ?, ?)
            """, (
                submission_id,
                stored.stored_path,
                stored.original_filename,
                size_bytes,
                file_storage.mimetype,
            ))

        conn.commit()
    except (OSError, sqlite3.Error, PracticalUploadError) as exc:
        conn.rollback()
        for path in saved_paths:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        app.logger.warning(
            "Practical upload rejected: reason=save_failed attempt_id=%s source_slug=%s exam_task_id=%s file_count=%s error_type=%s",
            attempt_id,
            source_slug,
            exam_task_id,
            len(prepared_files),
            type(exc).__name__,
        )
        return upload_error_redirect(attempt_id, source_slug, "invalid")

    return redirect(url_for(
        "quiz_practical_attempt",
        attempt_id=attempt_id,
        source_slug=source_slug,
        uploaded="1",
    ))


@app.route("/admin/open-question-candidates")
@access_policy(ADMIN)
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


def fetch_dzi_pool_health(source_slug: str = "may_2025_v2") -> dict | None:
    exam = dzi_find_exam(source_slug)
    if exam is None:
        return None

    conn = quiz_db()
    try:
        # SAFE: QUIZ_APPROVED_FILTER is a static SQL fragment; exam id uses ?.
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


# The web application never changes schema: that is the explicit
# `python3 -m src.manage_migrations --db ... --apply` step. Local runs only get
# a read-only warning; web.wsgi refuses to start while migrations are pending.
warn_if_schema_migrations_pending(DB_PATH, open_existing_database_read_only)





@app.route("/teacher/dzi-training", methods=["GET", "POST"])
@access_policy(ADMIN)
@integrity_policy(SAME_ORIGIN)
def teacher_dzi_training():
    import json
    import subprocess
    import sys
    from pathlib import Path

    project_root = Path(__file__).resolve().parents[1]
    config_path = project_root / "deploy" / "dzi-training-class-mix.json"
    generator_path = project_root / "src" / "generate_dzi_training_set.py"
    sets_dir = QUIZ_VAULT_PATH / "Generated" / "DZI-Training" / "sets"

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
                "--db", str(Path(app.config["DB_PATH"]).resolve()),
                "--config", str(config_path.resolve()),
                "--data-output-dir", str(sets_dir.resolve()),
                "--vault-output-dir", str(sets_dir.resolve()),
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
                "relative_path": str(path.relative_to(QUIZ_VAULT_PATH)),
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
@access_policy(ADMIN)
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
        row_dict["teacher_label"] = "не е посочен"
        recent_assignments.append(row_dict)

    recent_assignments_by_class = []
    for class_value in sorted({int(row["class"]) for row in recent_assignments}):
        recent_assignments_by_class.append({
            "class": class_value,
            "assignments": [row for row in recent_assignments if int(row["class"]) == class_value],
        })

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
        recent_assignments_by_class=recent_assignments_by_class,
        recent_attempts=recent_attempts,
    )


@app.route("/teacher/practical-submissions")
@access_policy(ADMIN)
def teacher_practical_submissions():
    submissions = fetch_practical_submission_review_rows()
    return render_template(
        "teacher_practical_submissions.html",
        submissions=submissions,
    )


@app.route("/teacher/practical/submission-file/<int:file_id>/download")
@access_policy(ADMIN)
def teacher_practical_submission_file_download(file_id: int):
    file_row = fetch_practical_submission_file(file_id)
    if file_row is None:
        app.logger.warning(
            "Practical submission download rejected: reason=not_found file_id=%s",
            file_id,
        )
        abort(404)
    if file_row["is_deleted"]:
        app.logger.warning(
            "Practical submission download rejected: reason=deleted file_id=%s attempt_id=%s exam_task_id=%s",
            file_id,
            file_row.get("quiz_attempt_id"),
            file_row.get("exam_task_id"),
        )
        abort(404)

    safe_path = practical_uploaded_file_safe_path(file_row.get("stored_path"))
    if safe_path is None:
        app.logger.warning(
            "Practical submission download rejected: reason=unsafe_path file_id=%s attempt_id=%s exam_task_id=%s",
            file_id,
            file_row.get("quiz_attempt_id"),
            file_row.get("exam_task_id"),
        )
        abort(404)
    if not safe_path.is_file():
        app.logger.warning(
            "Practical submission download rejected: reason=missing_file file_id=%s attempt_id=%s exam_task_id=%s",
            file_id,
            file_row.get("quiz_attempt_id"),
            file_row.get("exam_task_id"),
        )
        abort(404)

    return send_file(
        safe_path,
        as_attachment=True,
        download_name=file_row["original_filename"] or safe_path.name,
        max_age=0,
    )


@app.route("/teacher/assignments")
@access_policy(ADMIN)
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

    # SAFE: where_clause is selected from fixed branches after type_filter validation.
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
        row_dict["teacher_label"] = "не е посочен"
        assignments.append(row_dict)

    conn.close()

    return _quiz_render_template(
        "teacher_assignments.html",
        assignments=assignments,
        type_filter=type_filter,
    )


@app.route("/teacher/new", methods=["GET", "POST"])
@access_policy(TEACHER)
@integrity_policy(SAME_ORIGIN)
def teacher_new():
    conn = quiz_db()
    raw_subject = (
        _quiz_request.form.get("subject") if _quiz_request.method == "POST"
        else _quiz_request.args.get("subject")
    )
    selected_subject = canonical_subject(raw_subject) if raw_subject else IT
    if selected_subject is None:
        conn.close()
        _quiz_abort(400 if _quiz_request.method == "POST" else 404)
    selected_definition = get_subject(selected_subject)
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
        if any(key.startswith(("question_ids", "open_question_ids", "question_plan"))
               for key in _quiz_request.form):
            teacher_new_error = "invalid_question_plan"
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
            section = conn.execute("""
                SELECT id, title_bg
                FROM curriculum_sections
                WHERE id = ?
            """, (section_id,)).fetchone()
            if not section:
                conn.close()
                _quiz_abort(404)
            try:
                scope = AssessmentScope(conn, section_id)
            except ValueError:
                teacher_new_error = "invalid_subject"
            else:
                if scope.subject != selected_subject:
                    teacher_new_error = "subject_mismatch"
                elif open_count > 0 and not scope.allows_dzi_open_questions:
                    teacher_new_error = "insufficient_open_questions"
                    preselected_section_id = section_id

        if teacher_new_error is None:
            form_values = {
                "question_count": requested_count,
                "time_limit_minutes": raw_limit,
                "include_open_questions": include_open_questions,
                "open_count": open_count,
                "source_slug": source_slug,
            }

            available = len(quiz_section_question_ids(conn, section_id, scope=scope))
            if available <= 0:
                conn.close()
                return _quiz_redirect(_quiz_url_for("teacher_new", subject=selected_subject))

            question_count = max(1, min(requested_count, available))

            if open_count > 0:
                closed_count = requested_count - open_count
                plan = build_mixed_quiz_plan(
                    conn,
                    closed_count=closed_count,
                    open_count=open_count,
                    open_source_slug=source_slug or None,
                    section_id=section_id,
                    scope=scope,
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
                    try:
                        scope.validate_plan(
                            question_plan["question_ids"], question_plan["open_question_ids"],
                            open_source_slug=source_slug or None,
                        )
                    except ValueError:
                        teacher_new_error = "invalid_question_plan"
                        preselected_section_id = section_id
                    else:
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

    section_rows = conn.execute("""
        SELECT id, class, title_bg
        FROM curriculum_sections
        ORDER BY class, display_order, title_bg
    """).fetchall()

    sections = []
    practical_source_options = None
    for row in section_rows:
        section = dict(row)
        try:
            section_scope = AssessmentScope(conn, int(section["id"]))
        except ValueError:
            continue
        if section_scope.subject != selected_subject:
            continue
        eligible_count = len(quiz_section_question_ids(
            conn, int(section["id"]), scope=section_scope
        ))
        if eligible_count <= 0:
            continue
        section["question_count"] = eligible_count
        section["open"] = open_question_scope_summary(conn, int(section["id"]), section_scope)
        section["allows_dzi_open_questions"] = section_scope.allows_dzi_open_questions
        section["allows_dzi_content"] = section_scope.allows_dzi_content
        if section_scope.allows_dzi_content and practical_source_options is None:
            practical_source_options = fetch_practical_assignment_options(conn, scope=section_scope)
        sections.append(section)

    section_slug = (_quiz_request.args.get("section") or "").strip()
    if section_slug:
        preselected = conn.execute("""
            SELECT id
            FROM curriculum_sections
            WHERE section_slug = ?
        """, (section_slug,)).fetchone()
        if preselected and any(int(row["id"]) == int(preselected["id"]) for row in sections):
            preselected_section_id = int(preselected["id"])

    # Only a server-loaded DZI-training section authorizes the DZI open pool.
    selected_section = next(
        (row for row in sections if int(row["id"]) == preselected_section_id), None
    )
    practical_source_options = practical_source_options or []

    conn.close()

    teacher_subject_options = [
        subject for subject in subject_catalog()
        if subject["has_real_content"] or subject["id"] == selected_subject
    ]

    return _quiz_render_template(
        "teacher_new.html",
        sections=sections,
        selected_subject=selected_subject,
        selected_subject_title=selected_definition.title,
        selected_subject_dzi=selected_definition.dzi,
        teacher_subject_options=teacher_subject_options,
        preselected_section_id=preselected_section_id,
        form_values=form_values,
        teacher_new_error=teacher_new_error,
        quiz_time_limit_max_minutes=QUIZ_TIME_LIMIT_MAX_MINUTES,
        mixed_planning_result=mixed_planning_result,
        selected_section_open=selected_section["open"] if selected_section else None,
        any_dzi_sections=any(row["allows_dzi_open_questions"] for row in sections),
        selected_open_applicable=bool(selected_section and selected_section["allows_dzi_open_questions"]),
        selected_dzi_context=bool(selected_section and selected_section["allows_dzi_content"]),
        selected_open_available=next((source["count"] for source in selected_section["open"]["sources"]
                                     if source["source_slug"] == form_values["source_slug"]), 0)
            if selected_section and form_values["source_slug"]
            else selected_section["open"]["count"] if selected_section else 0,
        practical_source_options=practical_source_options,
    ), 400 if teacher_new_error else 200


@app.route("/teacher/assignment/<int:assignment_id>")
@access_policy(TEACHER)
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
    attempts = attach_attempt_question_image_counts(conn, attempts)

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
@access_policy(ADMIN)
@integrity_policy(SAME_ORIGIN)
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
@access_policy(ADMIN)
@integrity_policy(SAME_ORIGIN)
def teacher_assignment_duplicate(assignment_id):
    conn = quiz_db()
    source = quiz_fetch_assignment(conn, assignment_id)
    if not source:
        conn.close()
        _quiz_abort(404)

    if source["question_plan_json"]:
        plan = quiz_parse_assignment_question_plan(source["question_plan_json"])
        try:
            if plan is None:
                raise ValueError("Invalid mixed plan")
            AssessmentScope(conn, source["section_id"]).validate_plan(
                plan["question_ids"], plan["open_question_ids"]
            )
        except ValueError:
            conn.close()
            _quiz_abort(400)

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


def quiz_parse_review_points(value: object, *, points_possible: float) -> float:
    raw = "" if value is None else str(value).strip().replace(",", ".")
    if raw == "":
        raise ValueError("points value is required")
    points = float(raw)
    if not math.isfinite(points):
        raise ValueError("points value must be finite")
    if points < 0 or points > points_possible:
        raise ValueError("points value out of range")
    return points


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
@access_policy(ADMIN)
@integrity_policy(SAME_ORIGIN)
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
        if override_raw in {"", "0", "none"}:
            teacher_status = None
        elif override_raw == "1":
            teacher_status = "correct"
        elif override_raw in TEACHER_OPEN_ANSWER_STATUSES:
            teacher_status = override_raw
        else:
            conn.close()
            _quiz_abort(400)
        override_value = 1 if teacher_status else 0

        teacher_note = (_quiz_request.form.get("teacher_note") or "").replace("\x00", "").strip()
        if len(teacher_note) > TEACHER_NOTE_MAX_LENGTH:
            conn.close()
            _quiz_abort(400)
        teacher_note_value = teacher_note or None

        row = conn.execute("""
            SELECT qta.id, qta.is_correct, qta.points_possible
            FROM quiz_text_answers qta
            JOIN quiz_attempts qa ON qa.id = qta.attempt_id
            WHERE qta.id = ?
              AND qa.assignment_id = ?
        """, (answer_id, assignment_id)).fetchone()
        if not row:
            conn.close()
            _quiz_abort(404)

        try:
            points_possible = float(row["points_possible"] or 0)
        except (TypeError, ValueError):
            points_possible = 0.0
        if teacher_status:
            points_raw = _quiz_request.form.get("points_awarded")
            if points_raw is None or str(points_raw).strip() == "":
                points_awarded = points_possible if teacher_status == "correct" else 0.0
            else:
                try:
                    points_awarded = quiz_parse_review_points(points_raw, points_possible=points_possible)
                except (TypeError, ValueError):
                    conn.close()
                    _quiz_abort(400)
        else:
            points_awarded = quiz_text_auto_points(row)

        conn.execute("""
            UPDATE quiz_text_answers
            SET teacher_override = ?,
                teacher_override_status = ?,
                points_awarded = ?,
                teacher_note = ?
            WHERE id = ?
        """, (override_value, teacher_status, points_awarded, teacher_note_value, answer_id))
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
    attempts = attach_attempt_question_image_counts(conn, attempts)

    open_text_answers = []
    open_subtotals_by_attempt: dict[int, dict] = {}
    combined_scores_by_attempt: dict[int, dict] = {}
    mc_percentages: list[float] = []
    submitted_attempt_ids: list[int] = []
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
        submitted_attempt_ids.append(attempt_id_int)
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

    attach_question_image_indicators(conn, open_text_answers)

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
    difficulty_breakdown = quiz_teacher_difficulty_breakdown(conn, submitted_attempt_ids)

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
        "difficulty_breakdown": difficulty_breakdown,
    }

    attempts = [
        {
            **dict(attempt),
            "duration_display": quiz_attempt_duration_display(attempt) if attempt["submitted_at"] else QUIZ_DURATION_FALLBACK_BG,
            "success_rate_display": quiz_attempt_success_rate_display(attempt) if attempt["submitted_at"] else QUIZ_SUCCESS_RATE_FALLBACK_BG,
        }
        for attempt in attempts
    ]

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


def _quiz_csv_text(value: str | None) -> str:
    """Prefix formula-like text with an apostrophe before CSV serialization."""
    if value is None:
        return ""
    for char in value:
        if char.isspace() or _quiz_unicodedata.category(char) in {"Cc", "Cf"}:
            continue
        if char in "=+-@":
            return "'" + value
        break
    return value


def _quiz_format_points(value) -> str:
    try:
        return f"{float(value):.4f}"
    except (TypeError, ValueError):
        return ""


@app.route("/teacher/assignment/<int:assignment_id>/results.csv")
@access_policy(ADMIN)
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
                _quiz_csv_text(assignment["title_bg"]),
                attempt_id_int,
                _quiz_csv_text(attempt["student_name"]),
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
            _quiz_csv_text(assignment["title_bg"]),
            attempt_id_int,
            _quiz_csv_text(attempt["student_name"]),
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
                _quiz_csv_text(assignment["title_bg"]),
                attempt_id_int,
                _quiz_csv_text(attempt["student_name"]),
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
                _quiz_csv_text(ans.get("raw_answer")),
                _quiz_csv_text(ans.get("normalized_answer")),
                _quiz_csv_text(ans.get("matched_answer")),
                _quiz_csv_value(ans.get("points_awarded")),
                _quiz_csv_value(ans.get("points_possible")),
                _quiz_csv_value(ans.get("grading_mode")),
                _quiz_csv_value(ans.get("teacher_override")),
                _quiz_csv_text(ans.get("teacher_note")),
                _quiz_csv_value(ans.get("is_correct")),
            ])

    conn.close()

    return _quiz_response(
        buf.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# Keep legacy quiz endpoint names available for existing url_for(...) calls
# while the canonical route declarations live on the quiz blueprint.
register_legacy_quiz_endpoint_aliases(app)
app.register_blueprint(quiz_bp)

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
