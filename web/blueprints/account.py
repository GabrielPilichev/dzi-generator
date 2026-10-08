"""Account sign-in, sign-out and password change (Accounts PR 4, admin TOTP PR T).

``/login``, ``POST /login/totp``, ``POST /logout`` and ``/account/password``. This
module authenticates and issues the account session (``uid``, ``sk``, ``iat``); role,
name and active state stay in the ``users`` row and are loaded per request by
``web.app``.

An admin's correct password does not issue that session. It issues only a pending
second-factor state (``mfa_uid``, ``mfa_sk``, ``mfa_iat``) that no principal loader
reads, so it authorizes nothing; ``POST /login/totp`` turns it into the account
session after a TOTP or recovery code. An admin without a confirmed TOTP credential
cannot sign in at all.
Authorization stays in :mod:`web.access` and same-origin request integrity in
:mod:`web.request_integrity`: this module only declares each view's policy.
This is the only sign-in the application has.
The module imports neither ``web.app`` nor another controller.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from flask import Blueprint, current_app, redirect, render_template, request, session, url_for

from web.access import PUBLIC, SIGNED_IN, access_policy, current_principal
from web.redirects import safe_redirect_target
from web.request_db import get_request_db
from web.request_integrity import SAME_ORIGIN, integrity_policy
from web.services import accounts as accounts_service
from web.services import login_throttle
from web.services import totp as totp_service

account_bp = Blueprint("account", __name__)

ATTEMPT_OWNER_KEY_PREFIX = "quiz_attempt_owner:"
# Pending second factor: a password-verified admin who has not yet given a code.
PENDING_SECOND_FACTOR_KEYS = ("mfa_uid", "mfa_sk", "mfa_iat")
# app.config key holding DZI_TOTP_KEY (``None`` when unset: TOTP sign-in fails closed).
TOTP_KEY_CONFIG = "TOTP_ENCRYPTION_KEY"

logger = logging.getLogger(__name__)

INVALID_CREDENTIALS_MESSAGE = "Грешен имейл или парола."
THROTTLED_MESSAGE = "Твърде много опити. Опитай отново след няколко минути."
WRONG_CURRENT_PASSWORD_MESSAGE = "Грешна текуща парола."
PASSWORD_MISMATCH_MESSAGE = "Новите пароли не съвпадат."
PASSWORD_CHANGED_MESSAGE = "Паролата е сменена. Останалите устройства са отписани."
INVALID_SECOND_FACTOR_MESSAGE = "Грешен или вече използван код."
SECOND_FACTOR_EXPIRED_MESSAGE = "Времето за въвеждане на кода изтече. Влез отново."
SECOND_FACTOR_UNAVAILABLE_MESSAGE = (
    "Двуфакторното удостоверяване временно не е достъпно. Опитай по-късно."
)
SECOND_FACTOR_NOT_ENROLLED_MESSAGE = (
    "Профилът изисква двуфакторно удостоверяване, което още не е настроено. "
    "Обърни се към администратора на сървъра."
)

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


def _account_throttle_key(user: dict) -> str:
    """The counter of an account: the one its email is throttled under on the login form."""
    return _throttle_key(user["email"] or f"\0uid\0{user['id']}")


def _submitted_next() -> str:
    """The requested return target if it is a safe same-site path, else empty."""
    candidate = request.args.get("next") or request.form.get("next") or ""
    return candidate if candidate and safe_redirect_target(candidate, "/") == candidate else ""


def issue_account_session(user_id: int, session_key: str, issued_at: int | None = None) -> None:
    """Replace the session with the account identity.

    Everything set before sign-in is dropped; only the attempt-ownership markers
    carry over. ``issued_at`` is kept
    when a signed-in session is re-keyed, so re-keying never extends its lifetime.
    """
    carried = {key: value for key, value in session.items() if key.startswith(ATTEMPT_OWNER_KEY_PREFIX)}
    session.clear()
    session.update(carried)
    session["uid"] = user_id
    session["sk"] = session_key
    session["iat"] = int(time.time()) if issued_at is None else issued_at


def issue_pending_second_factor(user_id: int, session_key: str, issued_at: int) -> None:
    """Replace the session with a pending second factor for a password-verified admin.

    Like :func:`issue_account_session` it drops everything but the attempt-ownership
    markers, so no earlier identity survives next to the pending state. ``mfa_*`` keys
    are not account keys: ``web.app`` never loads a principal from them.
    """
    carried = {key: value for key, value in session.items() if key.startswith(ATTEMPT_OWNER_KEY_PREFIX)}
    session.clear()
    session.update(carried)
    session["mfa_uid"] = user_id
    session["mfa_sk"] = session_key
    session["mfa_iat"] = issued_at


def _clear_pending_second_factor() -> None:
    for key in PENDING_SECOND_FACTOR_KEYS:
        if key in session:
            session.pop(key)


def _pending_second_factor(conn, now: datetime):
    """``(user, pending_since)`` of a live pending sign-in, or ``None``.

    Fails closed: malformed, future or expired (5 minutes) state; an unknown, disabled
    or re-keyed account (password change, disable, role change, sign-out, TOTP reset);
    a non-admin; a missing or unconfirmed credential; and a pending sign-in already
    spent by a completed second factor.
    """
    issued_at = session.get("mfa_iat")
    if not isinstance(issued_at, int) or isinstance(issued_at, bool):
        return None
    now_seconds = int(now.timestamp())
    if issued_at <= 0 or issued_at > now_seconds or now_seconds - issued_at >= totp_service.PENDING_LIFETIME_SECONDS:
        return None
    user = accounts_service.load_session_user(conn, session.get("mfa_uid"), session.get("mfa_sk"))
    if user is None or user["role"] != "admin":
        return None
    credential = totp_service.credential(conn, user["id"])
    pending_since = totp_service.timestamp(datetime.fromtimestamp(issued_at, timezone.utc))
    if (
        credential is None
        or credential["confirmed_at"] is None
        or totp_service.pending_is_spent(credential, pending_since)
    ):
        return None
    return user, pending_since


def _start_second_factor(conn, user: dict, next_url: str, now: datetime):
    """The password step of an admin: a pending second factor, never an account session.

    The throttle is not cleared and ``last_login_at`` is not recorded here: a correct
    password alone must not reset the counter that also limits code guessing.
    """
    if not totp_service.has_confirmed_credential(conn, user["id"]):
        conn.commit()  # keeps a password-hash upgrade, nothing else changed
        _clear_pending_second_factor()
        return render_template(
            "login.html", error=SECOND_FACTOR_NOT_ENROLLED_MESSAGE, next_url=next_url, email=""
        ), 403
    conn.commit()
    issue_pending_second_factor(user["id"], user["session_key"], int(now.timestamp()))
    return render_template("login_totp.html", error=None, next_url=next_url)


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
        # Already signed in (for example after following an external link, whose
        # first cross-site request carries no SameSite=Strict cookie): go on instead
        # of asking again. Only a fully validated account counts; a pending second
        # factor is not a principal and still gets the form.
        principal = current_principal()
        if principal.user_id is not None:
            return redirect(safe_redirect_target(
                next_url, ROLE_HOME_ENDPOINTS.get(principal.role, "browsing.index")
            ))
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

    if user["role"] == "admin":
        return _start_second_factor(conn, user, next_url, now)

    login_throttle.clear(conn, key)
    accounts_service.record_login(conn, user["id"])
    conn.commit()
    issue_account_session(user["id"], user["session_key"])
    return redirect(safe_redirect_target(next_url, ROLE_HOME_ENDPOINTS.get(user["role"], "browsing.index")))


@account_bp.route("/login/totp", methods=["POST"])
@access_policy(PUBLIC)
@integrity_policy(SAME_ORIGIN)
def login_totp():
    next_url = _submitted_next()
    conn = get_request_db()
    now = _utcnow()
    pending = _pending_second_factor(conn, now)
    if pending is None:
        _clear_pending_second_factor()
        return render_template("login.html", error=SECOND_FACTOR_EXPIRED_MESSAGE, next_url=next_url, email="")
    user, pending_since = pending

    key = _account_throttle_key(user)
    # The login form's counter: password and code guesses share one limit.
    if login_throttle.is_blocked(conn, key, now):
        return render_template("login_totp.html", error=THROTTLED_MESSAGE, next_url=next_url), 429

    submitted = request.form.get("code", "")
    totp_code = totp_service.normalize_totp_code(submitted)
    recovery_code = None if totp_code else totp_service.normalize_recovery_code(submitted)
    try:
        if totp_code:
            verified = totp_service.verify_totp(
                conn, user["id"], current_app.config.get(TOTP_KEY_CONFIG), totp_code, now, pending_since
            )
        elif recovery_code:
            verified = totp_service.verify_recovery_code(conn, user["id"], recovery_code, now, pending_since)
        else:
            verified = False
    except totp_service.TotpKeyError as exc:
        conn.rollback()
        logger.error("Admin TOTP sign-in unavailable: %s", exc)
        return render_template("login_totp.html", error=SECOND_FACTOR_UNAVAILABLE_MESSAGE, next_url=next_url), 503
    except totp_service.TotpSecretError:
        conn.rollback()
        logger.error("Admin TOTP sign-in unavailable: the secret of account %s cannot be decrypted", user["id"])
        return render_template("login_totp.html", error=SECOND_FACTOR_UNAVAILABLE_MESSAGE, next_url=next_url), 503
    except BaseException:
        conn.rollback()
        raise

    if not verified:
        conn.rollback()  # undo a recovery code consumed for an already-spent sign-in
        login_throttle.record_failure(conn, key, now)
        conn.commit()
        return render_template("login_totp.html", error=INVALID_SECOND_FACTOR_MESSAGE, next_url=next_url)

    try:
        login_throttle.clear(conn, key)
        accounts_service.record_login(conn, user["id"])
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    if recovery_code:
        # Recovery codes are an independent factor and work without DZI_TOTP_KEY, so a
        # key outage cannot lock every admin out; each use is still made visible.
        logger.warning(
            "Admin account %s signed in with a recovery code; %d unused recovery codes remain%s",
            user["id"],
            totp_service.unused_recovery_code_count(conn, user["id"]),
            "; DZI_TOTP_KEY is unavailable" if totp_service.key_problem(current_app.config.get(TOTP_KEY_CONFIG)) else "",
        )
    # Clears the pending keys; the session is exactly uid/sk/iat (+ attempt markers).
    issue_account_session(user["id"], user["session_key"])
    return redirect(safe_redirect_target(next_url, ROLE_HOME_ENDPOINTS["admin"]))


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
    key = _account_throttle_key(user)
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
