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
import math
import secrets
import warnings
from io import BytesIO

import sqlite3
from datetime import datetime
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

from flask import Flask, abort, g, render_template, url_for, request, redirect, send_file, session
from werkzeug.exceptions import RequestEntityTooLarge

from src.subject_browsing import subject_content_grades
from src.subject_browse_cache import CachedSubjectBrowser
from src.subject_assessment import AssessmentScope, question_belongs_to_subject
from src.subject_content import IT, PHYSICS, canonical_subject, section_ownership
from src.subject_registry import all_subjects, get_subject, subject_for_route_slug
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


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_ENV = "DZI_DB"
UPLOAD_ROOT_ENV = "DZI_UPLOAD_ROOT"
REFERENCE_ROOT_ENV = "DZI_REFERENCE_ROOT"
SKIP_STARTUP_MIGRATION_ENV = "DZI_SKIP_STARTUP_MIGRATION"
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
# Development/tests keep legacy self-healing schema helpers (import-time quiz
# tables, request-time column guard). web.wsgi sets the flag so production
# changes schema only through `python3 -m src.manage_migrations`.
SCHEMA_CHANGES_ON_DEMAND = (
    (os.environ.get(SKIP_STARTUP_MIGRATION_ENV) or "").strip().lower()
    not in TRUE_ENV_VALUES
)


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
QUESTION_DIFFICULTY_BUCKETS = {
    "1": ("very_easy", "Много лесни", "diff-easy"),
    "2": ("easy", "Лесни", "diff-easy"),
    "3": ("medium", "Средни", "diff-medium"),
    "4": ("hard", "Трудни", "diff-hard"),
    "5": ("very_hard", "Много трудни", "diff-hard"),
    "easy": ("easy", "Лесни", "diff-easy"),
    "medium": ("medium", "Средни", "diff-medium"),
    "hard": ("hard", "Трудни", "diff-hard"),
}
QUESTION_DIFFICULTY_BUCKET_ORDER = [
    "very_easy",
    "easy",
    "medium",
    "hard",
    "very_hard",
    "unknown",
]

SUBJECT_STATUS_LABELS = {
    "active": "Активен",
    "in_preparation": "В подготовка",
}
PHYSICS_TOPIC_GROUPS_BY_GRADE = get_subject(PHYSICS).placeholder_topic_groups


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
            "has_real_content": bool(grades),
            "content_grades": grades,
            "search_terms": definition.search_terms,
            "topic_groups_by_grade": definition.placeholder_topic_groups,
            "dzi": definition.dzi,
            "legacy_grade_routes": definition.legacy_grade_routes,
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


def question_difficulty_bucket(value) -> dict:
    key = str(value or "").strip().lower()
    bucket_key, label, css_class = QUESTION_DIFFICULTY_BUCKETS.get(
        key,
        ("unknown", "Без трудност", "diff-unknown"),
    )
    return {"key": bucket_key, "label": label, "class": css_class}


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
        if subject["legacy_grade_routes"]:
            return url_for("grade_view", grade=grade)
        return url_for("subject_grade_view", subject_slug=subject["slug"], grade=grade)

    def subject_topic_group_url(subject: dict, grade: int, topic_group: dict) -> str:
        return url_for(
            "subject_topic_group_view",
            subject_slug=subject["slug"],
            grade=grade,
            topic_group_slug=topic_group["slug"],
        )

    return {
        "learnpilot_subjects": subject_catalog(),
        "subject_url": subject_url,
        "subject_class_url": subject_class_url,
        "subject_topic_group_url": subject_topic_group_url,
        "subject_topic_groups": subject_topic_groups,
        "subject_status_label": subject_status_label,
        "subject_class_range_label": subject_class_range_label,
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
def admin_logout():
    session.pop("admin_authenticated", None)
    session["ui_profile"] = "tester"
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

ALLOWED_INTERNAL_TABLES = {
    "multiple_choice_options",
    "practical_submission_grades",
    "practical_submissions",
    "questions",
    "quiz_text_answers",
}


def get_db() -> sqlite3.Connection:
    """Per-request SQLite connection. Closed in teardown."""
    if "db" not in g:
        conn = sqlite3.connect(app.config["DB_PATH"])
        conn.execute("PRAGMA journal_mode=WAL")
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


def fetch_sections_for_grade(grade: int) -> list[dict]:
    """Legacy IT sections, grouped by module."""
    return subject_browser().sections_for_grade(grade)


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
QUESTION_VISUAL_ASSET_TYPES = ("image", "pdf_crop")
QUESTION_VISUAL_STATIC_PREFIX = "web/static/"
QUESTION_VISUAL_ALLOWED_PREFIX = "web/static/img/questions/dzi/"
QUESTION_VISUAL_ALLOWED_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


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


def question_visual_static_filename(local_path: object) -> str | None:
    raw_path = str(local_path or "").strip().replace("\\", "/")
    if not raw_path or raw_path.startswith(("/", "~")) or "://" in raw_path:
        return None
    if not raw_path.startswith(QUESTION_VISUAL_ALLOWED_PREFIX):
        return None

    path = PurePosixPath(raw_path)
    if path.is_absolute() or any(part in {".", ".."} for part in path.parts):
        return None
    if path.suffix.lower() not in QUESTION_VISUAL_ALLOWED_SUFFIXES:
        return None

    return raw_path[len(QUESTION_VISUAL_STATIC_PREFIX):]


def fetch_question_visual_assets(conn: sqlite3.Connection, question_ids: list[int]) -> dict[int, list[dict]]:
    clean_ids = []
    for question_id in question_ids:
        try:
            if isinstance(question_id, bool):
                raise ValueError
            parsed_id = int(question_id)
        except (TypeError, ValueError):
            continue
        if parsed_id not in clean_ids:
            clean_ids.append(parsed_id)

    if not clean_ids:
        return {}
    if not sqlite_table_exists(conn, "assets") or not sqlite_table_exists(conn, "asset_links"):
        return {question_id: [] for question_id in clean_ids}

    # SAFE: placeholders are generated from parsed integer question IDs.
    placeholders = ",".join("?" for _ in clean_ids)
    # SAFE: SQL fragment is static and values still use ? params.
    owner_clause = "(al.owner_type IN ('question', 'questions') AND al.owner_id = q.id)"
    if sqlite_table_exists(conn, "exam_task_questions"):
        owner_clause = f"""
            (
                {owner_clause}
                OR (
                    al.owner_type = 'exam_task'
                    AND al.owner_id IN (
                        SELECT etq.task_id
                        FROM exam_task_questions etq
                        WHERE etq.question_id = q.id
                    )
                )
            )
        """

    rows = conn.execute(f"""
        SELECT
            q.id AS question_id,
            q.source_exam,
            q.source_number,
            a.id AS asset_id,
            a.local_path,
            al.caption_bg,
            al.display_order,
            al.owner_type
        FROM questions q
        JOIN asset_links al
          ON {owner_clause}
        JOIN assets a
          ON a.id = al.asset_id
        WHERE q.id IN ({placeholders})
          AND a.asset_type IN (?, ?)
        ORDER BY
            q.id,
            COALESCE(al.display_order, 0),
            CASE WHEN al.owner_type IN ('question', 'questions') THEN 0 ELSE 1 END,
            a.id
    """, (*clean_ids, *QUESTION_VISUAL_ASSET_TYPES)).fetchall()

    assets_by_question: dict[int, list[dict]] = {question_id: [] for question_id in clean_ids}
    seen_assets: dict[int, set[int]] = {question_id: set() for question_id in clean_ids}
    for row in rows:
        question_id = int(row["question_id"])
        asset_id = int(row["asset_id"])
        if asset_id in seen_assets[question_id]:
            continue

        static_filename = question_visual_static_filename(row["local_path"])
        if static_filename is None:
            continue

        source_exam = str(row["source_exam"] or "").strip()
        source_number = row["source_number"]
        if source_exam and source_number is not None:
            alt_text = f"Изображение към {source_exam} задача {source_number}"
        else:
            alt_text = f"Изображение към въпрос {question_id}"

        assets_by_question[question_id].append({
            "asset_id": asset_id,
            "static_filename": static_filename,
            "alt_text": alt_text,
            "caption": str(row["caption_bg"] or "").strip(),
        })
        seen_assets[question_id].add(asset_id)

    return assets_by_question


def attach_question_visual_assets(
    conn: sqlite3.Connection, questions: list[dict], *, id_key: str = "id"
) -> list[dict]:
    assets_by_question = fetch_question_visual_assets(
        conn,
        [int(question[id_key]) for question in questions if question.get(id_key) is not None],
    )
    for question in questions:
        question["visual_assets"] = assets_by_question.get(int(question[id_key]), [])
    return questions


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


def fetch_practical_assignment_options(conn: sqlite3.Connection) -> list[dict]:
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
        WHERE e.format_version = ?
        GROUP BY e.id
        HAVING resource_count > 0
        ORDER BY e.year DESC, e.session, e.variant
    """, (DZI_FORMAT_VERSION,)).fetchall()

    options = []
    for row in rows:
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


def unique_text_values(values: list[str]) -> list[str]:
    unique = []
    seen = set()
    for value in values:
        text = str(value).strip()
        if not text or text in seen:
            continue
        unique.append(text)
        seen.add(text)
    return unique


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


def sqlite_table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def sqlite_table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    if table_name not in ALLOWED_INTERNAL_TABLES:
        raise ValueError(f"Unsupported internal table name: {table_name}")
    # SAFE: table name is selected from ALLOWED_INTERNAL_TABLES.
    return {
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    }


def first_existing_column(columns: set[str], candidates: tuple[str, ...]) -> str | None:
    for candidate in candidates:
        if candidate in columns:
            return candidate
    return None


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
def tester_home():
    return render_template("tester_home.html")


@app.route("/")
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
        topic_groups=subject_topic_groups(subject, grade) if not sections else (),
    )


def render_subject_topic_group_page(subject_slug: str, grade: int, topic_group_slug: str):
    subject = catalog_subject(subject_slug)
    if subject is None or grade not in subject["available_classes"]:
        abort(404)

    browser = _subject_browser(subject)
    if browser is not None:
        section = browser.section(topic_group_slug, grade=grade)
        if section is not None:
            return render_template(
                "subject_topic_group.html",
                subject=subject,
                grade=grade,
                topic_group={"slug": section["section_slug"], "title": section["section_title"]},
                section=section,
                topics=browser.topics_in_section(section["section_id"]),
                questions=fetch_questions_in_section(section["section_id"], browser=browser),
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
        "subject_topic_group.html",
        subject=subject,
        grade=grade,
        topic_group=topic_group,
    )


@app.route("/subjects/<subject_slug>")
def subject_view(subject_slug: str):
    return render_subject_page(subject_slug)


@app.route("/subjects/<subject_slug>/<int:grade>")
def subject_grade_view(subject_slug: str, grade: int):
    return render_subject_grade_page(subject_slug, grade)


@app.route("/subjects/<subject_slug>/<int:grade>/<topic_group_slug>")
def subject_topic_group_view(subject_slug: str, grade: int, topic_group_slug: str):
    return render_subject_topic_group_page(subject_slug, grade, topic_group_slug)


@app.route("/physics")
def physics():
    return render_subject_page("physics")


@app.route("/physics/grade/<int:grade>")
def physics_grade(grade: int):
    return render_subject_grade_page("physics", grade)


@app.route("/favicon.ico")
def favicon():
    return send_file(PROJECT_ROOT / "web" / "static" / "img" / "brand" / "favicon.ico", mimetype="image/x-icon")


HEALTH_DB_TIMEOUT_SECONDS = 2.0


def open_existing_database_read_only(path, *, timeout: float = 5.0) -> sqlite3.Connection:
    """Never creates a missing database file; this connection refuses SQL writes.

    mode=rw (not mode=ro) lets SQLite initialize absent WAL/SHM sidecars, as in
    src.subject_browse_cache; query_only keeps the connection read-only.
    """
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=rw", uri=True, timeout=timeout)
    try:
        conn.execute("PRAGMA query_only=ON")
    except BaseException:
        conn.close()
        raise
    return conn


@app.route("/healthz")
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
        key=lambda m: (m["module_number"] or 99, m["module_title"] or ""),
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
    practical_resource_count = fetch_practical_resource_count(int(exam["id"]))
    return render_template(
        "dzi_source.html",
        exam=exam,
        tasks=tasks,
        practical_resource_count=practical_resource_count,
    )


@app.route("/dzi/source/<source_slug>/practical")
def dzi_practical_source(source_slug: str):
    resolved = fetch_practical_tasks(source_slug)
    if resolved is None:
        abort(404)
    exam, tasks = resolved
    return render_template("practical_tasks.html", exam=exam, tasks=tasks)


@app.route("/dzi/practical/resource/<int:resource_id>/download")
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
import hashlib as _quiz_hashlib
import io as _quiz_io
import json as _quiz_json
import os as _quiz_os
import random as _quiz_random
import re as _quiz_re
import sqlite3 as _quiz_sqlite3
import sys as _quiz_sys
import unicodedata as _quiz_unicodedata
from datetime import datetime as _quiz_datetime, timezone as _quiz_timezone
from pathlib import Path as _QuizPath

from flask import Response as _quiz_response
from flask import abort as _quiz_abort
from flask import redirect as _quiz_redirect
from flask import request as _quiz_request
from flask import url_for as _quiz_url_for
from flask import render_template as _quiz_render_template


QUIZ_APPROVED_FILTER = "(q.is_ai_generated = 0 OR q.quality_score >= 1.0)"
QUIZ_DB_PATH = DB_PATH  # Same database as browsing; see configured_db_path().
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
    subject: str | None = None,
    scope: AssessmentScope | None = None,
) -> list[dict]:
    if scope is not None and section_id != scope.section_id:
        raise ValueError("Open-question scope must match its section")
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

    # SAFE: optional SQL fragments are static; values still use ? params.
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
        if scope is not None and not scope.accepts(int(question["id"])):
            continue
        if subject is not None and not question_belongs_to_subject(conn, int(question["id"]), subject):
            continue
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

    attach_question_image_indicators(conn, candidates)
    return candidates


def build_mixed_quiz_plan(
    conn,
    *,
    closed_count: int,
    open_count: int,
    source_slug: str | None = None,
    open_source_slug: str | None = None,
    section_id: int | None = None,
    rng: "_quiz_random.Random | None" = None,
    scope: AssessmentScope | None = None,
) -> dict:
    if closed_count < 0 or open_count < 0:
        raise ValueError("closed_count and open_count must be non-negative")
    if scope is not None and section_id != scope.section_id:
        raise ValueError("Mixed-question scope must match its section")
    # Randomization happens at plan-creation time so that two new assignments
    # for the same section/count pick different question sets when the pool
    # is large enough. Callers can pass a seeded ``rng`` for deterministic
    # tests; the persisted ``question_plan_json`` keeps a *single* fixed
    # selection per assignment, so already-started attempts never reshuffle.
    if rng is None:
        rng = _quiz_random.Random()

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

    # SAFE: optional SQL fragments are static; values still use ? params.
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
        if scope is not None and not scope.accepts(int(row["id"])):
            continue
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
        scope=scope,
    )

    return {
        "closed_questions": _quiz_random_sample(rng, closed_candidates, closed_count),
        "open_questions": _quiz_random_sample(rng, open_candidates, open_count),
        "requested_closed_count": closed_count,
        "requested_open_count": open_count,
        "available_closed_count": len(closed_candidates),
        "available_open_count": len(open_candidates),
    }


def _quiz_random_sample(rng, population, k):
    """Sample up to k items from population without replacement.

    Returns the full population (in random order) when k >= len(population).
    Returns [] for empty input or non-positive k. The order of the returned
    items is randomized so that the persisted question_plan_json varies
    across new assignments even when the pool is small.
    """
    if k <= 0 or not population:
        return []
    pool = list(population)
    if k >= len(pool):
        rng.shuffle(pool)
        return pool
    return rng.sample(pool, k)


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
    columns = sqlite_table_columns(conn, "quiz_text_answers")
    teacher_status_expr = (
        "teacher_override_status"
        if "teacher_override_status" in columns
        else "NULL AS teacher_override_status"
    )

    # SAFE: question_filter is a static fragment and teacher_status_expr is
    # selected from a known column name or a static NULL expression.
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
            {teacher_status_expr},
            teacher_note
        FROM quiz_text_answers
        WHERE attempt_id = ?
          {question_filter}
        ORDER BY question_id, subquestion_number
    """, params).fetchall()
    answers = []
    for row in rows:
        answer = dict(row)
        answer["accepted_answers"] = quiz_display_answer_list(answer.get("accepted_answers_json"))
        answer["feedback_explanation"] = QUIZ_NO_EXPLANATION_MESSAGE
        prepare_quiz_text_answer_review(answer)
        answers.append(answer)
    return answers


QUIZ_NO_EXPLANATION_MESSAGE = "Няма въведено обяснение за този въпрос."
QUIZ_NO_ACCEPTED_ANSWER_MESSAGE = "Няма въведен приет отговор."
QUESTION_EXPLANATION_COLUMNS = (
    "explanation",
    "explanation_bg",
    "answer_explanation",
    "answer_explanation_bg",
    "rationale",
    "rationale_bg",
    "feedback",
    "feedback_bg",
)
OPTION_EXPLANATION_COLUMNS = (
    "explanation",
    "explanation_bg",
    "rationale",
    "rationale_bg",
    "feedback",
    "feedback_bg",
)


def quiz_display_answer_list(raw_value: object) -> list[str]:
    return unique_text_values(_quiz_answer_values(raw_value))


def fetch_question_explanations(conn, question_ids: list[int]) -> dict[int, str]:
    if not question_ids:
        return {}
    column = first_existing_column(sqlite_table_columns(conn, "questions"), QUESTION_EXPLANATION_COLUMNS)
    if column is None:
        return {}

    # SAFE: column is selected from QUESTION_EXPLANATION_COLUMNS; placeholders
    # are generated from trusted question IDs.
    placeholders = ",".join("?" for _ in question_ids)
    rows = conn.execute(f"""
        SELECT id, {column} AS explanation
        FROM questions
        WHERE id IN ({placeholders})
    """, question_ids).fetchall()
    return {
        int(row["id"]): str(row["explanation"]).strip()
        for row in rows
        if row["explanation"] is not None and str(row["explanation"]).strip()
    }


def fetch_option_explanations(conn, question_ids: list[int]) -> dict[tuple[int, str], str]:
    if not question_ids:
        return {}
    column = first_existing_column(
        sqlite_table_columns(conn, "multiple_choice_options"),
        OPTION_EXPLANATION_COLUMNS,
    )
    if column is None:
        return {}

    # SAFE: column is selected from OPTION_EXPLANATION_COLUMNS; placeholders
    # are generated from trusted question IDs.
    placeholders = ",".join("?" for _ in question_ids)
    rows = conn.execute(f"""
        SELECT question_id, option_letter, {column} AS explanation
        FROM multiple_choice_options
        WHERE question_id IN ({placeholders})
    """, question_ids).fetchall()
    return {
        (int(row["question_id"]), row["option_letter"]): str(row["explanation"]).strip()
        for row in rows
        if row["explanation"] is not None and str(row["explanation"]).strip()
    }


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


def quiz_section_question_ids(
    conn, section_id: int, *, scope: AssessmentScope | None = None
) -> list[int]:
    if scope is not None and section_id != scope.section_id:
        raise ValueError("MC scope must match its section")
    # SAFE: QUIZ_APPROVED_FILTER is a static SQL fragment; section_id uses ?.
    rows = conn.execute(f"""
        SELECT DISTINCT q.id, q.prompt, q.question_type, q.has_image, q.image_path
        FROM questions q
        JOIN topic_section_assignments tsa ON tsa.topic_id = q.topic_id
        WHERE tsa.section_id = ?
          AND {QUIZ_APPROVED_FILTER}
          AND q.question_type = 'multiple_choice'
        ORDER BY q.id
    """, (section_id,)).fetchall()
    return [
        int(r["id"]) for r in rows
        if (scope is None or scope.accepts(int(r["id"])))
        and is_quiz_question_eligible(conn, r)
    ]


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


def quiz_seed(assignment_id: int, student_name: str) -> str:
    raw = f"{assignment_id}|{student_name.strip().lower()}".encode("utf-8")
    return _quiz_hashlib.sha256(raw).hexdigest()


def quiz_pick_questions(
    conn, assignment, student_name: str, *, scope: AssessmentScope | None = None
) -> tuple[str, list[int]]:
    seed = quiz_seed(int(assignment["id"]), student_name)
    if scope is None:
        try:
            owner = section_ownership(conn, int(assignment["section_id"]))
        except sqlite3.OperationalError as exc:
            if "no such table: curriculum_sections" not in str(exc):
                raise
            # Legacy in-memory picker unit fixtures have no curriculum tables.
            owner = None
        if owner is not None and owner.is_consistent:
            scope = AssessmentScope(conn, int(assignment["section_id"]))
    ids = quiz_section_question_ids(conn, int(assignment["section_id"]), scope=scope)
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

    # SAFE: placeholders are generated from trusted question IDs.
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

    return attach_question_visual_assets(conn, result)


def quiz_question_types_by_id(conn, question_ids: list[int]) -> dict[int, str]:
    if not question_ids:
        return {}

    unique_ids = list(dict.fromkeys(int(qid) for qid in question_ids))
    # SAFE: placeholders are generated from trusted question IDs.
    placeholders = ",".join("?" for _ in unique_ids)
    rows = conn.execute(f"""
        SELECT id, question_type
        FROM questions
        WHERE id IN ({placeholders})
    """, unique_ids).fetchall()
    return {int(row["id"]): row["question_type"] for row in rows}


def quiz_correct_mc_letters_by_id(conn, question_ids: list[int]) -> dict[int, str | None]:
    if not question_ids:
        return {}

    unique_ids = list(dict.fromkeys(int(qid) for qid in question_ids))
    # SAFE: placeholders are generated from trusted question IDs.
    placeholders = ",".join("?" for _ in unique_ids)
    rows = conn.execute(f"""
        SELECT question_id, option_letter
        FROM multiple_choice_options
        WHERE question_id IN ({placeholders})
          AND is_correct = 1
    """, unique_ids).fetchall()

    correct_by_qid: dict[int, str | None] = {}
    for row in rows:
        qid = int(row["question_id"])
        if qid not in correct_by_qid:
            correct_by_qid[qid] = row["option_letter"]
    return correct_by_qid


STALE_ATTEMPT_MESSAGE = (
    "Този тест съдържа стари или непълни въпроси и не може да бъде показан коректно. "
    "Моля, създайте нов тест."
)
QUIZ_STUDENT_NAME_TOO_SHORT_MESSAGE = "Името трябва да съдържа поне 2 символа."
QUIZ_STUDENT_NAME_TOO_LONG_MESSAGE = "Името трябва да е най-много 80 символа."
QUIZ_EMPTY_SUBMISSION_MESSAGE = "Попълни поне един отговор, преди да предадеш теста."


def quiz_student_name_error(student_name: str) -> str | None:
    if len(student_name) < 2:
        return QUIZ_STUDENT_NAME_TOO_SHORT_MESSAGE
    if len(student_name) > 80:
        return QUIZ_STUDENT_NAME_TOO_LONG_MESSAGE
    return None


def quiz_has_any_submitted_answer(form, *, mc_question_ids: list[int], question_ids: list[int], open_question_ids: list[int]) -> bool:
    for qid in mc_question_ids:
        if form.get(f"q_{qid}"):
            return True

    renderable_ids = set()
    for qid in question_ids:
        try:
            renderable_ids.add(int(qid))
        except (TypeError, ValueError):
            continue
    planned_open_ids = set()
    for qid in open_question_ids or []:
        try:
            parsed = int(qid)
        except (TypeError, ValueError):
            continue
        if parsed in renderable_ids:
            planned_open_ids.add(parsed)
    for key in form.keys():
        if not key.startswith("open_q_"):
            continue
        parts = key.split("_")
        if len(parts) < 4:
            continue
        try:
            qid = int(parts[2])
        except (TypeError, ValueError):
            continue
        if qid in planned_open_ids and str(form.get(key) or "").strip():
            return True
    return False


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


def _quiz_clean_question_ids(question_ids) -> list[int]:
    clean_ids = []
    for qid in question_ids or []:
        try:
            if isinstance(qid, bool):
                raise ValueError
            parsed_id = int(qid)
        except (TypeError, ValueError):
            continue
        if parsed_id not in clean_ids:
            clean_ids.append(parsed_id)
    return clean_ids


def question_linked_image_counts(conn, question_ids) -> dict[int, int]:
    clean_ids = _quiz_clean_question_ids(question_ids)
    if not clean_ids:
        return {}
    visual_assets_by_question = fetch_question_visual_assets(conn, clean_ids)
    return {
        question_id: len(visual_assets_by_question.get(question_id, []))
        for question_id in clean_ids
    }


def attach_question_image_indicators(
    conn,
    items: list[dict],
    *,
    question_id_key: str = "question_id",
) -> list[dict]:
    question_ids = _quiz_clean_question_ids(
        item.get(question_id_key)
        for item in items
        if item.get(question_id_key) is not None
    )
    image_counts = question_linked_image_counts(conn, question_ids)
    for item in items:
        try:
            question_id = int(item.get(question_id_key))
        except (TypeError, ValueError):
            image_count = 0
        else:
            image_count = image_counts.get(question_id, 0)
        item["linked_image_count"] = image_count
        item["has_linked_image"] = image_count > 0
    return items


def attach_attempt_question_image_counts(conn, attempts) -> list[dict]:
    annotated_attempts = [dict(attempt) for attempt in attempts]
    question_ids_by_attempt: dict[int, list[int]] = {}
    all_question_ids: list[int] = []

    for attempt in annotated_attempts:
        attempt_id = int(attempt["id"])
        plan = quiz_parse_attempt_question_plan(attempt.get("question_ids_json"))
        question_ids = _quiz_clean_question_ids(plan["question_ids"])
        question_ids_by_attempt[attempt_id] = question_ids
        all_question_ids.extend(question_ids)

    image_counts = question_linked_image_counts(conn, all_question_ids)
    for attempt in annotated_attempts:
        attempt_id = int(attempt["id"])
        image_question_count = sum(
            1
            for question_id in question_ids_by_attempt.get(attempt_id, [])
            if image_counts.get(question_id, 0) > 0
        )
        attempt["image_question_count"] = image_question_count
        attempt["has_linked_image_question"] = image_question_count > 0
    return annotated_attempts


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
    # SAFE: placeholders are generated from parsed integer question IDs.
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


QUIZ_DURATION_FALLBACK_BG = "Няма данни за време"
QUIZ_SUCCESS_RATE_FALLBACK_BG = "Няма данни за успеваемост"


def quiz_parse_timestamp_utc(raw_value) -> _quiz_datetime | None:
    if not raw_value:
        return None
    try:
        parsed = _quiz_datetime.fromisoformat(str(raw_value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(_quiz_timezone.utc).replace(tzinfo=None)
    return parsed


def quiz_time_taken_seconds(attempt) -> int | None:
    started = quiz_parse_timestamp_utc(attempt["started_at"])
    ended = quiz_parse_timestamp_utc(attempt["submitted_at"])
    if started is None or ended is None:
        return None

    seconds = int((ended - started).total_seconds())
    if seconds < 0:
        return None
    return seconds


def quiz_format_duration(seconds: int) -> str:
    if seconds is None:
        return QUIZ_DURATION_FALLBACK_BG
    try:
        total_seconds = max(0, int(seconds))
    except (TypeError, ValueError):
        return QUIZ_DURATION_FALLBACK_BG

    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    rest = total_seconds % 60

    if hours:
        return f"{hours} ч. {minutes} мин. {rest} сек."
    if minutes:
        return f"{minutes} мин. {rest} сек."
    return f"{rest} сек."


def quiz_attempt_duration_display(attempt) -> str:
    return quiz_format_duration(quiz_time_taken_seconds(attempt))


def quiz_success_rate_percent(score_correct, score_total) -> float | None:
    try:
        total = float(score_total)
        correct = float(score_correct or 0)
    except (TypeError, ValueError):
        return None
    if total <= 0:
        return None
    percent = (correct / total) * 100.0
    return max(0.0, min(100.0, percent))


def quiz_success_rate_display(score_correct, score_total) -> str:
    percent = quiz_success_rate_percent(score_correct, score_total)
    if percent is None:
        return QUIZ_SUCCESS_RATE_FALLBACK_BG
    rounded = round(percent, 1)
    if rounded.is_integer():
        return f"{int(rounded)}%"
    return f"{rounded:.1f}%"


def quiz_attempt_success_rate_display(attempt) -> str:
    return quiz_success_rate_display(attempt["score_correct"], attempt["score_total"])


def quiz_difficulty_breakdown_from_rows(rows) -> list[dict]:
    buckets: dict[str, dict] = {}
    for row in rows:
        bucket = question_difficulty_bucket(row.get("difficulty"))
        key = bucket["key"]
        if key not in buckets:
            buckets[key] = {
                "key": key,
                "label": bucket["label"],
                "class": bucket["class"],
                "correct": 0,
                "total": 0,
                "percent": None,
                "percent_display": QUIZ_SUCCESS_RATE_FALLBACK_BG,
            }
        buckets[key]["total"] += 1
        if row.get("is_correct"):
            buckets[key]["correct"] += 1

    for bucket in buckets.values():
        bucket["percent"] = quiz_success_rate_percent(bucket["correct"], bucket["total"])
        bucket["percent_display"] = quiz_success_rate_display(bucket["correct"], bucket["total"])

    return sorted(
        buckets.values(),
        key=lambda item: QUESTION_DIFFICULTY_BUCKET_ORDER.index(item["key"])
        if item["key"] in QUESTION_DIFFICULTY_BUCKET_ORDER
        else len(QUESTION_DIFFICULTY_BUCKET_ORDER),
    )


def quiz_difficulty_breakdown_from_questions(questions: list[dict]) -> list[dict]:
    return quiz_difficulty_breakdown_from_rows([
        {
            "difficulty": q.get("difficulty"),
            "is_correct": bool(q.get("is_correct")),
        }
        for q in questions
    ])


def quiz_teacher_difficulty_breakdown(conn, attempt_ids: list[int]) -> list[dict]:
    if not attempt_ids:
        return []

    # SAFE: placeholders are generated from trusted attempt IDs.
    placeholders = ",".join("?" for _ in attempt_ids)
    rows = conn.execute(f"""
        SELECT q.difficulty, qa.is_correct
        FROM quiz_answers qa
        JOIN questions q ON q.id = qa.question_id
        WHERE qa.attempt_id IN ({placeholders})
        ORDER BY q.difficulty
    """, attempt_ids).fetchall()
    return quiz_difficulty_breakdown_from_rows([
        {
            "difficulty": row["difficulty"],
            "is_correct": bool(row["is_correct"]),
        }
        for row in rows
    ])


def quiz_current_timestamp() -> _quiz_datetime:
    return _quiz_datetime.utcnow()


def quiz_remaining_seconds(assignment, attempt) -> int | None:
    limit = assignment["time_limit_minutes"]
    if not limit:
        return None

    try:
        started = _quiz_datetime.fromisoformat(attempt["started_at"])
        elapsed = int((quiz_current_timestamp() - started).total_seconds())
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
    result_question_ids = [int(q["id"]) for q in questions]
    question_explanations = fetch_question_explanations(conn, result_question_ids)
    option_explanations = fetch_option_explanations(conn, result_question_ids)
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
        q["feedback_explanation"] = (
            option_explanations.get((int(q["id"]), chosen_letter))
            or question_explanations.get(int(q["id"]))
            or QUIZ_NO_EXPLANATION_MESSAGE
        )

    return questions


# Development/test imports keep the legacy idempotent quiz-table guard. The
# production entry point (web.wsgi) disables it: production schema changes run
# only through the explicit `python3 -m src.manage_migrations` deploy step.
if SCHEMA_CHANGES_ON_DEMAND:
    quiz_apply_migration()





@app.route("/teacher/dzi-training", methods=["GET", "POST"])
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
def teacher_practical_submissions():
    submissions = fetch_practical_submission_review_rows()
    return render_template(
        "teacher_practical_submissions.html",
        submissions=submissions,
    )


@app.route("/teacher/practical/submission-file/<int:file_id>/download")
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

    open_candidates = fetch_open_question_candidates(conn, subject=selected_subject)
    open_source_counts: dict[str, int] = {}
    for candidate in open_candidates:
        source_slug = candidate.get("source_slug") or "unknown"
        open_source_counts[source_slug] = open_source_counts.get(source_slug, 0) + 1
    open_source_options = [
        {"source_slug": source_slug, "count": count}
        for source_slug, count in sorted(open_source_counts.items())
    ]
    practical_source_options = (
        fetch_practical_assignment_options(conn) if selected_definition.dzi else []
    )

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
        open_source_options=open_source_options,
        total_open_candidates=len(open_candidates),
        practical_source_options=practical_source_options,
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
TEACHER_OPEN_ANSWER_STATUSES = {"correct", "partially_correct", "incorrect"}
TEACHER_OPEN_ANSWER_STATUS_LABELS = {
    "correct": "Вярно",
    "partially_correct": "Частично вярно",
    "incorrect": "Грешно",
}
QUIZ_TEXT_AUTO_STATUS_LABELS = {
    "correct": "Автоматично съвпадение",
    "incorrect": "Няма автоматично съвпадение",
    "pending": "Предстои проверка",
}


def quiz_text_auto_points(row) -> float:
    try:
        possible = float(_quiz_mapping_get(row, "points_possible") or 0)
    except (TypeError, ValueError):
        possible = 0.0
    return possible if _quiz_mapping_get(row, "is_correct") == 1 else 0.0


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


def quiz_ensure_text_answer_teacher_status_column(conn) -> None:
    # Production never runs schema code from requests: migration 009 adds this
    # column and web.wsgi refuses to start while any migration is pending.
    if not SCHEMA_CHANGES_ON_DEMAND:
        return
    if "teacher_override_status" in sqlite_table_columns(conn, "quiz_text_answers"):
        return
    conn.execute("""
        ALTER TABLE quiz_text_answers
        ADD COLUMN teacher_override_status TEXT
            CHECK (
                teacher_override_status IS NULL
                OR teacher_override_status IN ('correct', 'partially_correct', 'incorrect')
            )
    """)


def prepare_quiz_text_answer_review(answer: dict) -> dict:
    auto_status = "pending"
    if answer.get("is_correct") == 1:
        auto_status = "correct"
    elif answer.get("is_correct") is not None:
        auto_status = "incorrect"

    teacher_status = answer.get("teacher_override_status")
    if teacher_status not in TEACHER_OPEN_ANSWER_STATUSES:
        teacher_status = None
    if answer.get("teacher_override") == 1 and teacher_status is None:
        teacher_status = "correct"
        try:
            answer["points_awarded"] = float(answer.get("points_possible") or 0)
        except (TypeError, ValueError):
            answer["points_awarded"] = 0.0

    answer["auto_status"] = auto_status
    answer["auto_status_label"] = QUIZ_TEXT_AUTO_STATUS_LABELS[auto_status]
    answer["auto_points_awarded"] = quiz_text_auto_points(answer)
    answer["teacher_override_status"] = teacher_status
    answer["teacher_override_status_label"] = (
        TEACHER_OPEN_ANSWER_STATUS_LABELS.get(teacher_status)
        if teacher_status
        else "Няма учителски override"
    )
    answer["teacher_reviewed"] = bool(answer.get("teacher_override") and teacher_status)
    return answer


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

        quiz_ensure_text_answer_teacher_status_column(conn)
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
