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
import json
import math
import secrets
import time
import warnings

import sqlite3
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, abort, g, render_template, url_for, request, redirect, session

from src.subject_browsing import public_slug
from src.subject_assessment import AssessmentScope, question_belongs_to_subject
from src.subject_registry import all_subjects, get_subject
from src.practical_uploads import MAX_FILES_PER_REQUEST, MAX_REQUEST_BYTES, MAX_UPLOAD_BYTES
from web.blueprints.dzi import dzi_bp
from web.blueprints.practical import practical_bp
from web.attempt_access import quiz_attempt_owner_session_key
from web.blueprints.account import ROLE_LABELS, account_bp
from web.blueprints.auth import VALID_UI_PROFILES, auth_bp
from web.redirects import safe_redirect_target
from web.blueprints.quiz import quiz_bp
from web.blueprints.teacher import teacher_bp
from web.blueprints.browsing import browsing_bp, open_existing_database_read_only
from web.access import (
    ANONYMOUS,
    PROFILE_SWITCH,
    account_principal,
    current_principal,
    denial_login_endpoint,
    is_route_allowed,
    policy_for_endpoint,
)
from web.request_db import get_request_db
from web.production import warn_if_schema_migrations_pending
from web.services import accounts as accounts_service
from web.services.subject_catalog import subject_catalog, subject_topic_groups
from web.services import quiz as quiz_service
from web.services.dzi_exams import (
    DZI_FORMAT_VERSION,
    dzi_json_text_list,
    dzi_parse_source_slug,
    dzi_source_slug,
    dzi_source_title,
)
from web.services.question_visuals import fetch_question_visual_assets
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


# Session identity of an account (Accounts design §6). Role, name and active state
# are never in the cookie; they are read from the users row on every request.
ACCOUNT_SESSION_KEYS = ("uid", "sk", "iat")
# Legacy shared-login keys, ignored while an account identity is present.
LEGACY_AUTH_SESSION_KEYS = ("tester_authenticated", "admin_authenticated")
ACCOUNT_LOADING_SKIPPED_ENDPOINTS = frozenset({"browsing.healthz", "static"})


@app.before_request
def load_current_user():
    """Resolve an account session to ``g.principal`` (authentication only).

    No account key in the session: nothing happens, no query, no cookie, and
    ``web.access.current_principal`` falls back to the legacy keys. Any account
    key present means an account session was attempted: it either resolves to the
    account's principal or the request is anonymous. It never falls back to
    legacy keys in the same cookie, and the stale identity is dropped together
    with those keys so the next request cannot fall through to them either.

    A session older than its role's absolute lifetime is stale too. The role
    is the one in the users row; a missing or malformed ``iat`` is expired.
    """
    if request.endpoint in ACCOUNT_LOADING_SKIPPED_ENDPOINTS:
        return
    if not any(key in session for key in ACCOUNT_SESSION_KEYS):
        return
    user = accounts_service.load_session_user(
        get_request_db(), session.get("uid"), session.get("sk")
    )
    principal = ANONYMOUS
    if user is not None and not accounts_service.session_expired(
        user["role"], session.get("iat"), time.time()
    ):
        principal = account_principal(user["id"], user["role"], user["display_name"])
    if principal.user_id is None:
        for key in (*ACCOUNT_SESSION_KEYS, *LEGACY_AUTH_SESSION_KEYS):
            session.pop(key, None)
    g.principal = principal


@app.after_request
def prevent_caching_for_accounts(response):
    # A signed-in account's pages must not be replayed from the browser cache or the
    # back button on a shared computer. Anonymous, legacy, static and health
    # responses are left alone.
    principal = g.get("principal")
    if principal is not None and principal.user_id is not None:
        response.headers["Cache-Control"] = "no-store"
    return response


@app.before_request
def load_ui_profile():
    if request.endpoint == "browsing.healthz":
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
        return url_for("browsing.subject_view", subject_slug=subject["slug"])

    def subject_class_url(subject: dict, grade: int) -> str:
        return url_for("browsing.subject_grade_view", subject_slug=subject["slug"], grade=grade)

    def subject_topic_group_url(subject: dict, grade: int, topic_group: dict) -> str:
        return url_for(
            "browsing.subject_topic_group_view",
            subject_slug=subject["slug"],
            grade=grade,
            topic_group_slug=topic_group["slug"],
        )

    def subject_section_url(subject: dict, grade: int, section: dict) -> str:
        return url_for(
            "browsing.subject_topic_group_view",
            subject_slug=subject["slug"],
            grade=grade,
            topic_group_slug=public_slug(subject["id"], grade, section["section_slug"]),
        )

    def subject_topic_url(subject: dict, grade: int, section: dict, topic: dict) -> str:
        return url_for(
            "browsing.subject_topic_view",
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
    label = "Админ" if profile == "admin" else "Тестер"
    principal = g.get("principal")
    if principal is not None and principal.user_id is not None:
        # An account has no cosmetic profile to switch: the label is its role.
        profile = "admin" if principal.is_admin else "tester"
        label = ROLE_LABELS.get(principal.role, label)
    return {
        "ui_profile": profile,
        "ui_profile_label": label,
        "question_difficulty_label": question_difficulty_label,
        "question_kind_label": question_kind_label,
    }


# ---------------------------------------------------------------------------
# Admin password protection
# ---------------------------------------------------------------------------

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
            "browsing.index",
        )
    else:
        next_url = request.path
    return redirect(url_for(denial_login_endpoint(level, view_args), next=next_url))


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

# DZI_REFERENCE_ROOT relocates only data/reference (official DZI resources);
# data/assets always stays in the checkout.
# The practical blueprint and service read these from app.config per request.
_practical_reference_root = configured_reference_root()
app.config["PROJECT_ROOT"] = PROJECT_ROOT
app.config["PRACTICAL_REFERENCE_ROOT"] = _practical_reference_root
app.config["PRACTICAL_RESOURCE_ROOTS"] = (
    _practical_reference_root or PROJECT_ROOT / "data" / "reference",
    PROJECT_ROOT / "data" / "assets",
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





app.register_blueprint(account_bp)
app.register_blueprint(auth_bp)
app.register_blueprint(browsing_bp)
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
