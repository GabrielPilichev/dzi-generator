"""Account sign-in, sign-out and password change (Accounts PR 4).

``/login``, ``POST /logout`` and ``/account/password``. This module authenticates
and issues the account session (``uid``, ``sk``, ``iat``); role, name and active
state stay in the ``users`` row and are loaded per request by ``web.app``.
Authorization stays in :mod:`web.access` and same-origin request integrity in
:mod:`web.request_integrity`: this module only declares each view's policy. The
legacy shared-password logins remain in :mod:`web.blueprints.auth`.
The module imports neither ``web.app`` nor another controller.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from flask import Blueprint, current_app, redirect, render_template, request, session, url_for

from web.access import PUBLIC, SIGNED_IN, access_policy, current_principal
from web.redirects import safe_redirect_target
from web.request_db import get_request_db
from web.request_integrity import SAME_ORIGIN, integrity_policy
from web.services import accounts as accounts_service
from web.services import login_throttle

account_bp = Blueprint("account", __name__)

ATTEMPT_OWNER_KEY_PREFIX = "quiz_attempt_owner:"

INVALID_CREDENTIALS_MESSAGE = "Грешен имейл или парола."
THROTTLED_MESSAGE = "Твърде много опити. Опитай отново след няколко минути."
WRONG_CURRENT_PASSWORD_MESSAGE = "Грешна текуща парола."
PASSWORD_MISMATCH_MESSAGE = "Новите пароли не съвпадат."
PASSWORD_CHANGED_MESSAGE = "Паролата е сменена. Останалите устройства са отписани."

# Where a sign-in lands without a usable ``next`` (design §14).
ROLE_HOME_ENDPOINTS = {
    "admin": "teacher.teacher_dashboard",
    "teacher": "teacher.teacher_new",
    "student": "browsing.index",
}
ROLE_LABELS = {"admin": "Админ", "teacher": "Учител", "student": "Ученик"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _throttle_key(identifier: str) -> str:
    return login_throttle.identifier_hash(current_app.config["SECRET_KEY"], identifier)


def _submitted_next() -> str:
    """The requested return target if it is a safe same-site path, else empty."""
    candidate = request.args.get("next") or request.form.get("next") or ""
    return candidate if candidate and safe_redirect_target(candidate, "/") == candidate else ""


def issue_account_session(user_id: int, session_key: str, issued_at: int | None = None) -> None:
    """Replace the session with the account identity.

    Everything set before sign-in is dropped, legacy auth keys and ``ui_profile``
    included; only the attempt-ownership markers carry over. ``issued_at`` is kept
    when a signed-in session is re-keyed, so re-keying never extends its lifetime.
    """
    carried = {key: value for key, value in session.items() if key.startswith(ATTEMPT_OWNER_KEY_PREFIX)}
    session.clear()
    session.update(carried)
    session["uid"] = user_id
    session["sk"] = session_key
    session["iat"] = int(time.time()) if issued_at is None else issued_at


@account_bp.app_context_processor
def inject_account():
    """``current_account`` for templates: the account principal, or ``None``."""
    principal = current_principal()
    if principal.user_id is None:
        return {"current_account": None, "current_account_role_label": None}
    return {
        "current_account": principal,
        "current_account_role_label": ROLE_LABELS.get(principal.role, ""),
    }


@account_bp.route("/login", methods=["GET", "POST"])
@access_policy(PUBLIC)
@integrity_policy(SAME_ORIGIN)
def login():
    next_url = _submitted_next()
    if request.method == "GET":
        return render_template("login.html", error=None, next_url=next_url, email="")

    submitted_email = request.form.get("email", "")
    # Echoed back into the form on failure; never more than an email's length.
    shown_email = submitted_email[: accounts_service.MAX_EMAIL_LENGTH]
    conn = get_request_db()
    now = _utcnow()
    key = _throttle_key(accounts_service.throttle_identifier(submitted_email))
    # Checked before any password hashing: a blocked identifier costs one read.
    if login_throttle.is_blocked(conn, key, now):
        return render_template(
            "login.html", error=THROTTLED_MESSAGE, next_url=next_url, email=shown_email
        ), 429

    user = accounts_service.authenticate(conn, submitted_email, request.form.get("password", ""))
    if user is None:
        login_throttle.record_failure(conn, key, now)
        conn.commit()
        return render_template(
            "login.html", error=INVALID_CREDENTIALS_MESSAGE, next_url=next_url, email=shown_email
        )

    login_throttle.clear(conn, key)
    accounts_service.record_login(conn, user["id"])
    conn.commit()
    issue_account_session(user["id"], user["session_key"])
    return redirect(safe_redirect_target(next_url, ROLE_HOME_ENDPOINTS.get(user["role"], "browsing.index")))


@account_bp.route("/logout", methods=["POST"])
@access_policy(PUBLIC)
@integrity_policy(SAME_ORIGIN)
def logout():
    # This browser only: session_key is not rotated, so other devices stay signed in.
    session.clear()
    return redirect(url_for("browsing.index"))


@account_bp.route("/account/password", methods=["GET", "POST"])
@access_policy(SIGNED_IN)
@integrity_policy(SAME_ORIGIN)
def password():
    if request.method == "GET":
        return render_template("account_password.html", error=None, success=None)

    principal = current_principal()
    conn = get_request_db()
    user = accounts_service.get_user_by_id(conn, principal.user_id)
    now = _utcnow()
    # The same counter the login form uses for this account's email.
    key = _throttle_key(user["email"] or f"\0uid\0{user['id']}")
    if login_throttle.is_blocked(conn, key, now):
        return render_template("account_password.html", error=THROTTLED_MESSAGE, success=None), 429

    new_password = request.form.get("new_password", "")
    error = None
    if not accounts_service.verify_password(conn, user["id"], request.form.get("current_password", "")):
        login_throttle.record_failure(conn, key, now)
        conn.commit()
        error = WRONG_CURRENT_PASSWORD_MESSAGE
    elif new_password != request.form.get("confirm_password", ""):
        error = PASSWORD_MISMATCH_MESSAGE
    else:
        error = accounts_service.password_problem(new_password, user["email"], user["role"])
    if error:
        return render_template("account_password.html", error=error, success=None)

    try:
        new_session_key = accounts_service.set_password(conn, user["id"], new_password)
        login_throttle.clear(conn, key)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    # Every other session of this account now carries a stale key.
    issue_account_session(user["id"], new_session_key, session.get("iat"))
    return render_template("account_password.html", error=None, success=PASSWORD_CHANGED_MESSAGE)
