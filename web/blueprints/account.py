"""Account sign-in, sign-out, the account page and password change (Accounts PR 4,
admin TOTP PR T).

``/login``, ``POST /login/totp``, ``POST /logout``, ``/account``, ``/account/password`` and
``/account/profile``. This module authenticates with a password; the account session
(``uid``, ``sk``, ``iat``) is issued by ``web.account_session``, and role, name and
active state stay in the ``users`` row and are loaded per request by ``web.app``.

An admin's correct password does not issue that session. It issues only a pending
second-factor state (``mfa_uid``, ``mfa_sk``, ``mfa_iat``) that no principal loader
reads, so it authorizes nothing; ``POST /login/totp`` turns it into the account
session after a TOTP or recovery code. An admin without a confirmed TOTP credential
cannot sign in at all.
Authorization stays in :mod:`web.access` and same-origin request integrity in
:mod:`web.request_integrity`: this module only declares each view's policy.
Password sign-in is unchanged by Google sign-in (``web.blueprints.oidc``), which is
a second way in for students and teachers and never one for an admin. This module
shows that flow's outcomes on the sign-in page and on the account page and takes no
part in it otherwise.
``/account/profile`` (docs/reviews/EMAIL_REGISTRATION_DESIGN.md, 7.3 and 7.4) shows and
edits what a person declared (names and class, or school and subjects) and exists only
while the profile gate is on: until then it is the generic 404, and ``/account`` shows no
profile and no link to it. A profile is a declaration, never authorization: saving one
writes ``user_profiles`` and, with the names, ``users.display_name`` in one transaction,
and never the role, the email, the verification time, the active state or the session
key. The module imports neither ``web.app`` nor another controller.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone

from flask import Blueprint, abort, current_app, redirect, render_template, request, session, url_for

from src import subject_registry
from web import oauth_state
from web.access import PUBLIC, SIGNED_IN, access_policy, current_principal, is_route_allowed
from web.account_session import (
    account_throttle_key,
    end_session,
    issue_account_session,
    issue_pending_second_factor,
    loaded_account,
    pending_link_of_this_session,
    role_home,
    throttle_key,
)
from web.redirects import safe_redirect_target
from web.request_db import get_request_db
from web.request_integrity import SAME_ORIGIN, integrity_policy
from web.services import accounts as accounts_service
from web.services import google_oidc
from web.services import identities as identities_service
from web.services import login_throttle
from web.services import profiles as profiles_service
from web.services import registration_forms as forms
from web.services import totp as totp_service

account_bp = Blueprint("account", __name__)

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

ROLE_LABELS = {"admin": "Админ", "teacher": "Учител", "student": "Ученик"}

# Texts of /account/profile (design 1.2 and 7.4).
PROFILE_REQUIRED_MESSAGE = "Попълни профила си, за да започнеш теста."
PROFILE_SAVED_MESSAGE = "Профилът е запазен."
PROFILE_FAILURE_MESSAGE = "Нещо се обърка и нищо не беше записано. Опитай отново."

# Outcomes of a Google sign-in or link (docs/reviews/GOOGLE_OIDC_DESIGN.md, 1.9): the
# ``google`` code a redirect carries, its text, how it is shown and the pages that
# show it. A code is only ever looked up here and never echoed: an unknown value shows
# nothing, and no value shows anything while the provider is disabled.
SIGN_IN_PAGE = "sign-in"
ACCOUNT_PAGE = "account"
_BOTH_PAGES = frozenset({SIGN_IN_PAGE, ACCOUNT_PAGE})
_SIGN_IN_ONLY = frozenset({SIGN_IN_PAGE})
_ACCOUNT_ONLY = frozenset({ACCOUNT_PAGE})
GOOGLE_MESSAGES = {
    "cancelled": ("Входът с Google беше отказан.", "warning", _BOTH_PAGES),
    "failed": ("Входът с Google не успя. Опитай отново.", "error", _BOTH_PAGES),
    "expired": ("Времето изтече. Опитай отново.", "warning", _BOTH_PAGES),
    "unavailable": ("Входът с Google временно не е достъпен. Опитай по-късно.", "error", _BOTH_PAGES),
    "email_exists": (
        "Вече има профил с този имейл. Влез с имейл и парола и свържи Google от „Профил“.",
        "warning", _SIGN_IN_ONLY),
    # A disabled account and an admin identity alike: the person has proven control
    # of the Google identity, so the message tells nothing about anyone else.
    "not_allowed": ("Този профил не може да използва вход с Google.", "error", _BOTH_PAGES),
    "linked": ("Google профилът е свързан.", "success", _ACCOUNT_ONLY),
    "already_linked": ("Този Google профил вече е свързан с твоя профил.", "warning", _ACCOUNT_ONLY),
    "identity_taken": ("Този Google профил вече е свързан с друг профил.", "error", _ACCOUNT_ONLY),
    "email_other_account": (
        "Имейлът на този Google профил е на друг профил в LearnPilot. Влез в онзи профил и го свържи оттам.",
        "error", _ACCOUNT_ONLY),
    # The four findings of a cancellation. Each is a statement about the request that
    # answered with it and about the account as that request read it; none stands in
    # for another, and none says what follows.
    "link_cancelled": ("Свързването с Google беше отказано.", "warning", _ACCOUNT_ONLY),
    "link_not_pending": ("Няма свързване с Google, което тази заявка да прекрати.", "warning", _ACCOUNT_ONLY),
    "account_linked": ("Профилът ти вече е свързан с Google.", "warning", _ACCOUNT_ONLY),
    "cancel_unconfirmed": ("Не успяхме да потвърдим отказа.", "error", _ACCOUNT_ONLY),
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _google_code() -> str | None:
    """The ``google`` code of this request if it is a known one and the provider is enabled."""
    if not oauth_state.provider_enabled():
        return None
    code = request.args.get("google")
    return code if code in GOOGLE_MESSAGES else None


def _google_message(page: str) -> dict | None:
    """The message ``page`` shows for this request's ``google`` code, if any."""
    code = _google_code()
    if code is None:
        return None
    text, kind, pages = GOOGLE_MESSAGES[code]
    return {"text": text, "kind": kind} if page in pages else None


def _submitted_next() -> str:
    """The requested return target if it is a safe same-site path, else empty."""
    candidate = request.args.get("next") or request.form.get("next") or ""
    return candidate if candidate and safe_redirect_target(candidate, "/") == candidate else ""


def _login_page(error, next_url, email, status: int = 200):
    page = render_template(
        "login.html", error=error, next_url=next_url, email=email, google_message=_google_message(SIGN_IN_PAGE)
    )
    return page if status == 200 else (page, status)


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
        return _login_page(SECOND_FACTOR_NOT_ENROLLED_MESSAGE, next_url, "", 403)
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
        # Already signed in (a bookmarked or shared sign-in link, or the „Вход“ link
        # of a page that was rendered before this browser signed in): go on instead
        # of asking again. Only a fully validated account counts; a pending second
        # factor is not a principal and still gets the form.
        principal = current_principal()
        if principal.user_id is not None:
            # The outcome of a Google flow that ended here (the callback of a signed-in
            # browser, or a redirect issued by the proxy) is shown on the account page.
            google_code = _google_code()
            if google_code is not None:
                return redirect(url_for("account.overview", google=google_code))
            return redirect(safe_redirect_target(next_url, role_home(principal.role)))
        return _login_page(None, next_url, "")

    submitted_email = request.form.get("email", "")
    # Echoed back into the form on failure; never more than an email's length.
    shown_email = submitted_email[: accounts_service.MAX_EMAIL_LENGTH]
    conn = get_request_db()
    now = _utcnow()
    key = throttle_key(accounts_service.throttle_identifier(submitted_email))
    # Checked before any password hashing: a blocked identifier costs one read.
    if login_throttle.is_blocked(conn, key, now):
        return _login_page(THROTTLED_MESSAGE, next_url, shown_email, 429)

    user = accounts_service.authenticate(conn, submitted_email, request.form.get("password", ""))
    if user is None:
        login_throttle.record_failure(conn, key, now)
        conn.commit()
        return _login_page(INVALID_CREDENTIALS_MESSAGE, next_url, shown_email)

    if user["role"] == "admin":
        return _start_second_factor(conn, user, next_url, now)

    login_throttle.clear(conn, key)
    accounts_service.record_login(conn, user["id"])
    conn.commit()
    issue_account_session(user["id"], user["session_key"])
    return redirect(safe_redirect_target(next_url, role_home(user["role"])))


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
        return _login_page(SECOND_FACTOR_EXPIRED_MESSAGE, next_url, "")
    user, pending_since = pending

    key = account_throttle_key(user)
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
    return redirect(safe_redirect_target(next_url, role_home("admin")))


@account_bp.route("/logout", methods=["POST"])
@access_policy(PUBLIC)
@integrity_policy(SAME_ORIGIN)
def logout():
    # This browser only: session_key is not rotated, so other devices stay signed in.
    # Signing out of LearnPilot does not sign the browser out of Google either. The
    # destination is the home page for every account.
    end_session()
    return redirect(url_for("browsing.index"))


def _profile_summary(principal) -> dict | None:
    """What /account shows of the profile, or ``None`` while the gate is off (no section, no link)."""
    if not _profiles_enabled():
        return None
    conn = get_request_db()
    profile = profiles_service.get_profile(conn, principal.user_id)
    return {
        "profile": profile,
        "type_label": {forms.STUDENT: "Ученик", forms.TEACHER: "Учител"}.get(profile["account_type"]) if profile else None,
        "can_complete": profile is None and principal.role == "student",
        "required": profiles_service.needs_profile(conn, {"id": principal.user_id}, enabled=True),
    }


@account_bp.route("/account", methods=["GET"])
@access_policy(SIGNED_IN)
def overview():
    """The account's own page: who it is, how it signs in and, while the profile gate is on,
    what it declared. Reads only."""
    principal = current_principal()
    account = loaded_account()
    google_identities = [
        row for row in identities_service.list_identities(get_request_db(), principal.user_id)
        if row["provider"] == google_oidc.PROVIDER
    ]
    enabled = oauth_state.provider_enabled()
    can_use_google = principal.role != "admin"
    return render_template(
        "account.html",
        profile_summary=_profile_summary(principal),
        account_email=account["email"],
        has_password=bool(account["has_password"]),
        google_identities=google_identities,
        google_message=_google_message(ACCOUNT_PAGE),
        # While the provider is disabled no slot is read and nothing is offered.
        pending_link=enabled and pending_link_of_this_session(principal) is not None,
        can_link=enabled and can_use_google and account["has_password"] and not google_identities,
        can_use_google=can_use_google,
    )


def _profiles_enabled() -> bool:
    return profiles_service.enabled_in(current_app.config)


def _subject_choices() -> list[dict]:
    """The subjects of the registry a teacher-type profile can name, in registry order."""
    return [{"id": subject.id, "title": subject.title} for subject in subject_registry.all_subjects()]


def _profile_echo(profile: dict) -> dict:
    """The form values of a stored profile, in the shape ``validate_profile`` echoes."""
    echo = {"first_name": profile["first_name"], "last_name": profile["last_name"]}
    if profile["account_type"] == forms.STUDENT:
        echo["grade"] = str(profile["grade"])
    else:
        echo["school_name"] = profile["school_name"]
        echo["subject"] = [item["subject_id"] or forms.OTHER_SUBJECT for item in profile["subjects"]]
        echo["subject_other"] = next((item["label"] for item in profile["subjects"] if item["subject_id"] is None), "")
    return echo


def _profile_form_type(profile: dict | None, role) -> str | None:
    """The type of form the account is offered, or ``None`` for none.

    The stored profile's own ``account_type`` decides, whatever the role is now. Without
    a profile only a current student is offered the student form; a teacher or an admin
    is offered nothing, and no teacher onboarding exists here.
    """
    if profile is not None:
        return profile["account_type"]
    return forms.STUDENT if role == "student" else None


def _profile_page(conn, principal, *, echo=None, errors=None, status: int = 200, failure: str | None = None):
    profile = profiles_service.get_profile(conn, principal.user_id)
    form_type = _profile_form_type(profile, principal.role)
    if form_type is None:
        return render_template("account_profile.html", form_type=None, echo={}, errors={}, failure=failure,
                               required=False, saved=False, grades=forms.GRADES, subjects=[]), status
    if echo is None:
        echo = _profile_echo(profile) if profile is not None else {}
        # Only a target the form rules keep is echoed back; a rejected one never is, but the
        # fact that one was refused is, so that the submission still goes to the role's home.
        echo["next"] = forms.accepted_next(request.args.get("next")) or ""
        echo["next_refused"] = forms.next_refused(request.args.get("next"), None)
    required = profiles_service.needs_profile(conn, {"id": principal.user_id}, enabled=True)
    return render_template(
        "account_profile.html",
        form_type=form_type,
        echo=echo,
        errors=errors or {},
        failure=failure,
        required=required,
        saved=request.method == "GET" and request.args.get("saved") == "1",
        grades=forms.GRADES,
        subjects=_subject_choices(),
        has_profile=profile is not None,
    ), status


def _profile_return_target(accepted_next: str, refused: bool, role) -> str:
    """Where a saved profile goes on: by GET, never starting anything.

    A kept ``next`` is validated again here and used; a target that was offered and not
    kept (``refused``, which survives retries and the GET-then-POST round trip as a
    yes/no, never as the target) becomes the current role's home; with no ``next`` at all
    the form shows the saved profile.
    """
    home = url_for(role_home(role))
    if accepted_next:
        return safe_redirect_target(accepted_next, home)
    if refused:
        return home
    return url_for("account.profile", saved=1)


@account_bp.route("/account/profile", methods=["GET", "POST"])
@access_policy(PUBLIC)
@integrity_policy(SAME_ORIGIN)
def profile():
    """View and edit the account's profile (design 7.4).

    The policy is ``public`` on purpose: the access hook runs before the view, so a
    ``signed_in`` policy would send an anonymous visitor to the login before the gate was
    looked at. While the gate is off the answer is the generic 404 for everybody; the
    sign-in requirement of ``signed_in`` is then applied here, with the hook's own rule
    (``is_route_allowed``) and the same redirect. Same-origin protection is a separate
    check that runs before both.
    """
    if not _profiles_enabled():
        abort(404)
    principal = current_principal()
    if not is_route_allowed(SIGNED_IN, principal):
        return redirect(url_for("account.login", next=request.path))
    conn = get_request_db()
    if request.method == "GET":
        return _profile_page(conn, principal)

    form_type = _profile_form_type(profiles_service.get_profile(conn, principal.user_id), principal.role)
    if form_type is None:
        # A teacher or an admin without a profile: nothing is created, nothing is asked.
        return _profile_page(conn, principal, status=403)
    validated = forms.validate_profile(request.form, form_type)
    if not validated.ok:
        return _profile_page(conn, principal, echo=validated.echo, errors=validated.errors)

    now = _utcnow()
    values = validated.values
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Read again under the lock: a concurrent save or role change decides here, not
        # what was read before the form was validated.
        stored = profiles_service.get_profile(conn, principal.user_id)
        state = accounts_service.profile_requirement(conn, principal.user_id)
        if state is None or _profile_form_type(stored, state["role"]) != form_type:
            conn.rollback()
            return _profile_page(conn, principal, echo=validated.echo, failure=PROFILE_FAILURE_MESSAGE, status=409)
        if stored is None:
            profiles_service.complete_student_profile(
                conn, principal.user_id, first_name=values["first_name"], last_name=values["last_name"],
                grade=values["grade"], now=now)
        elif form_type == forms.STUDENT:
            profiles_service.save_profile(
                conn, principal.user_id, first_name=values["first_name"], last_name=values["last_name"],
                grade=values["grade"], now=now)
        else:
            profiles_service.save_profile(
                conn, principal.user_id, first_name=values["first_name"], last_name=values["last_name"],
                school_name=values["school_name"], subjects=values["subjects"], now=now)
        conn.commit()
    except BaseException as exc:
        if conn.in_transaction:
            try:
                conn.rollback()
            except sqlite3.Error as rollback_error:
                logger.warning("profile_rollback_failed error=%s", type(rollback_error).__name__)
        if not isinstance(exc, Exception):
            raise
        # Rolled back: neither the profile nor the display name was written.
        logger.warning("profile_save_failed user=%s error=%s", principal.user_id, type(exc).__name__)
        return _profile_page(conn, principal, echo=validated.echo, failure=PROFILE_FAILURE_MESSAGE, status=500)
    # The session is untouched: no key was rotated, so every device stays signed in.
    return redirect(_profile_return_target(values["next"] or "", values["next_refused"], principal.role), code=303)


@account_bp.route("/account/password", methods=["GET", "POST"])
@access_policy(SIGNED_IN)
@integrity_policy(SAME_ORIGIN)
def password():
    # An account that signs in only with Google has no password to change. Asking it
    # for one would count a throttle failure against it for an answer it cannot give.
    if not loaded_account()["has_password"]:
        return render_template("account_password.html", error=None, success=None, has_password=False)
    if request.method == "GET":
        return render_template("account_password.html", error=None, success=None, has_password=True)

    principal = current_principal()
    conn = get_request_db()
    user = accounts_service.get_user_by_id(conn, principal.user_id)
    now = _utcnow()
    # The same counter the login form uses for this account's email.
    key = account_throttle_key(user)
    if login_throttle.is_blocked(conn, key, now):
        return render_template(
            "account_password.html", error=THROTTLED_MESSAGE, success=None, has_password=True), 429

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
        return render_template("account_password.html", error=error, success=None, has_password=True)

    try:
        new_session_key = accounts_service.set_password(conn, user["id"], new_password)
        login_throttle.clear(conn, key)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    # Every other session of this account now carries a stale key.
    issue_account_session(user["id"], new_session_key, session.get("iat"))
    return render_template(
        "account_password.html", error=None, success=PASSWORD_CHANGED_MESSAGE, has_password=True)
