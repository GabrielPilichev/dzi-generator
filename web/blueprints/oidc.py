"""Google sign-in and account linking (docs/reviews/GOOGLE_OIDC_DESIGN.md, section 1).

``POST /auth/google/start`` and ``POST /auth/google/link`` send the browser to Google,
``GET /auth/google/callback`` receives it back, and ``/auth/google/link/confirm`` and
``POST /auth/google/link/cancel`` finish or end a link.

* The callback is the one view that declares ``OAUTH_STATE``. It runs only for a
  transaction that ``web.oauth_state.verify_oauth_state`` matched and spent, and it
  never renders a page: every outcome is a redirect to a local path.
* Intent is fixed at the start and travels inside the signed transaction. A sign-in is
  never turned into a link and a link never signs anyone in.
* A link callback writes no identity. Only the confirmation does, and only with the
  account's current password.
* An email never selects, signs in or links an account.

With the provider disabled every route here answers the generic 404 page.
The module imports neither ``web.app`` nor another controller.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timezone

from flask import Blueprint, abort, current_app, redirect, render_template, request, session, url_for

from web import oauth_state
from web.access import PUBLIC, SIGNED_IN, access_policy, current_principal
from web.account_session import (
    account_throttle_key,
    bound_to_this_session,
    issue_account_session,
    link_binding,
    loaded_account,
    pending_link_of_this_session,
    role_home,
)
from web.public_url import normalize_public_base_url
from web.redirects import safe_redirect_target
from web.request_db import get_request_db, request_db_if_open
from web.request_integrity import OAUTH_STATE, SAME_ORIGIN, integrity_policy
from web.services import accounts as accounts_service
from web.services import google_oidc, login_throttle, oidc_accounts

oidc_bp = Blueprint("oidc", __name__)

logger = logging.getLogger("learnpilot.oidc")

# The texts of the password forms (web.blueprints.account), which this form shares a
# throttle counter with.
THROTTLED_MESSAGE = "Твърде много опити. Опитай отново след няколко минути."
WRONG_CURRENT_PASSWORD_MESSAGE = "Грешна текуща парола."

# ``google`` codes this module answers with, beside the outcomes of the services. The
# texts are looked up on render by the page that shows them; no value is ever echoed.
CANCELLED = "cancelled"
FAILED = oauth_state.OUTCOME_FAILED
EXPIRED = oauth_state.OUTCOME_EXPIRED
UNAVAILABLE = oauth_state.OUTCOME_UNAVAILABLE
LINK_CANCELLED = "link_cancelled"
LINK_NOT_PENDING = "link_not_pending"
ACCOUNT_LINKED = "account_linked"
CANCEL_UNCONFIRMED = "cancel_unconfirmed"

MAX_CODE_LENGTH = 256  # Google documents 256 bytes as the largest authorization code


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _provider() -> google_oidc.ProviderConfig | None:
    return current_app.config.get(oauth_state.PROVIDER_CONFIG_KEY)


def _usable_provider() -> google_oidc.ProviderConfig:
    """The enabled provider, for a request made to the public address itself.

    On any other host the callback would arrive without this host's cookie, so a flow
    is not started there. Both cases answer the generic 404.
    """
    config = _provider()
    if config is None or normalize_public_base_url(request.host_url) != config.public_base_url:
        abort(404)
    return config


def _account_page(code: str) -> str:
    return url_for("account.overview", google=code)


def _login_page(code: str) -> str:
    return url_for("account.login", google=code)


def _to_google(config: google_oidc.ProviderConfig, transaction: dict):
    response = redirect(
        google_oidc.authorization_url(
            config, state=transaction["state"], nonce=transaction["nonce"], code_verifier=transaction["cv"]
        ),
        code=303,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


def _stored_next() -> str:
    """The return target of the sign-in form, kept only if it is a safe local path."""
    candidate = request.form.get("next") or ""
    if not candidate or len(candidate) > oauth_state.MAX_NEXT_LENGTH:
        return ""
    return candidate if safe_redirect_target(candidate, "/") == candidate else ""


@oidc_bp.app_context_processor
def inject_google_sign_in():
    """``google_sign_in_enabled`` for templates: whether the button and forms exist."""
    return {"google_sign_in_enabled": oauth_state.provider_enabled()}


# --------------------------------------------------------------------------- start


@oidc_bp.route("/auth/google/start", methods=["POST"])
@access_policy(PUBLIC)
@integrity_policy(SAME_ORIGIN)
def google_start():
    config = _usable_provider()
    next_target = _stored_next()
    principal = current_principal()
    if principal.user_id is not None:
        # Already signed in: nothing is sent to Google and no account is switched. A
        # pending second factor is not a principal and may start.
        return redirect(safe_redirect_target(next_target, role_home(principal.role)))
    if google_oidc.cooling_down():
        return redirect(_login_page(UNAVAILABLE))
    transaction = oauth_state.start_transaction(oauth_state.INTENT_SIGNIN, next_target=next_target)
    if transaction is None:
        return redirect(_login_page(FAILED))
    return _to_google(config, transaction)


@oidc_bp.route("/auth/google/link", methods=["POST"])
@access_policy(SIGNED_IN)
@integrity_policy(SAME_ORIGIN)
def google_link():
    # No form field is read and no password is asked here: the link is confirmed
    # after Google returns.
    config = _usable_provider()
    principal = current_principal()
    if principal.role not in oidc_accounts.LINKABLE_ROLES:
        return render_template("403.html"), 403
    account = loaded_account()
    if not account["has_password"] or oidc_accounts.has_google_identity(get_request_db(), principal.user_id):
        return redirect(_account_page(oidc_accounts.NOT_ALLOWED))
    if google_oidc.cooling_down():
        return redirect(_account_page(UNAVAILABLE))
    # Linking already in progress is retired first. The route goes on whatever that
    # achieved: both slots are empty afterwards in every case.
    oauth_state.retire_linking()
    transaction = oauth_state.start_transaction(oauth_state.INTENT_LINK, binding=link_binding(principal))
    if transaction is None:
        return redirect(_account_page(FAILED))
    return _to_google(config, transaction)


# --------------------------------------------------------------------------- callback


def _single_parameter(name: str) -> str | None:
    values = request.args.getlist(name)
    return values[0] if len(values) == 1 else None


def _submitted_code() -> str | None:
    code = _single_parameter("code")
    if code is None or not 0 < len(code) <= MAX_CODE_LENGTH or not all("!" <= ch <= "~" for ch in code):
        return None
    return code


def _log_callback(outcome: str, intent: str, *, user_id=None, reason=None) -> None:
    """One line per callback, in a fixed vocabulary: never a parameter, a claim, a
    subject, an email or an exception's text."""
    level = logging.INFO if outcome in ("signed_in", "created", "pending_link", CANCELLED) else logging.WARNING
    logger.log(
        level, "callback outcome=%s intent=%s user=%s reason=%s",
        outcome, intent, user_id if user_id is not None else "-", reason or "-",
    )


@oidc_bp.route("/auth/google/callback", methods=["GET"])
@access_policy(PUBLIC)
@integrity_policy(OAUTH_STATE)
def google_callback():
    config = _provider()
    transaction = oauth_state.matched_transaction()
    if config is None or transaction is None:
        # Disabled, or no transaction that the hook matched and recorded: this view
        # does not proceed on its own.
        abort(404)
    intent = transaction["intent"]
    try:
        target = _finish_callback(config, transaction)
    except Exception as exc:
        conn = request_db_if_open()
        if conn is not None and conn.in_transaction:
            oauth_state.rollback_own(conn)
        _log_callback(FAILED, intent, reason=type(exc).__name__)
        target = oauth_state.outcome_target(intent, FAILED)
    return oauth_state.callback_redirect(target)


def _finish_callback(config: google_oidc.ProviderConfig, transaction: dict) -> str:
    """Steps V1 to V7 of design 1.5. The transaction is already spent: a callback
    that carries an error, a malformed code or a wrong ``iss`` starts again."""
    intent = transaction["intent"]

    def refused(code: str, *, reason=None, user_id=None) -> str:
        _log_callback(code, intent, user_id=user_id, reason=reason)
        return oauth_state.outcome_target(intent, code)

    if request.args.getlist("error"):
        cancelled = _single_parameter("error") == "access_denied"
        return refused(CANCELLED if cancelled else FAILED, reason="provider_error_parameter")
    code = _submitted_code()
    if code is None:
        return refused(FAILED, reason="code_parameter")
    if request.args.getlist("iss") and _single_parameter("iss") != google_oidc.ISSUER:
        return refused(FAILED, reason="iss_parameter")

    # What needs the principal, before any network call.
    principal = current_principal()
    if intent == oauth_state.INTENT_SIGNIN:
        if principal.user_id is not None:
            # No linking happens and the session is not touched.
            return refused(FAILED, reason="signed_in", user_id=principal.user_id)
    elif not bound_to_this_session(transaction, principal):
        return refused(FAILED, reason="not_bound")

    if google_oidc.cooling_down():
        return refused(UNAVAILABLE, reason=google_oidc.REASON_COOLDOWN)
    # No SQLite transaction or lock is held while the request waits for Google.
    result = google_oidc.exchange(google_oidc.ExchangeRequest(
        code=code,
        code_verifier=transaction["cv"],
        nonce=transaction["nonce"],
        redirect_uri=config.redirect_uri,
        client_id=config.client_id,
        client_secret=config.client_secret,
    ))
    if result.outcome != google_oidc.OUTCOME_OK:
        return refused(result.outcome, reason=result.reason)
    profile = oidc_accounts.GoogleProfile(result.identity.subject, result.identity.email, result.identity.name)
    if intent == oauth_state.INTENT_SIGNIN:
        return _sign_in(transaction, profile)
    return _prepare_link(transaction, profile)


def _sign_in(transaction: dict, profile: oidc_accounts.GoogleProfile) -> str:
    """One write transaction: sign in the identity's account, or create a student."""
    conn = get_request_db()
    conn.execute("BEGIN IMMEDIATE")
    try:
        result = oidc_accounts.sign_in_or_onboard(conn, profile)
        if result.user is None:
            conn.rollback()
        else:
            conn.commit()
    except BaseException:
        oauth_state.rollback_own(conn)
        raise
    if result.user is None:
        _log_callback(result.outcome, oauth_state.INTENT_SIGNIN)
        return oauth_state.outcome_target(oauth_state.INTENT_SIGNIN, result.outcome)
    user = result.user
    # After the commit: the session is then exactly uid, sk, iat and the carried
    # attempt markers. No provider name and no claim is in it.
    issue_account_session(user["id"], user["session_key"])
    _log_callback(result.outcome, oauth_state.INTENT_SIGNIN, user_id=user["id"])
    # The role is the one read at the write, so a target chosen while anonymous
    # cannot pick the fallback.
    return safe_redirect_target(transaction["next"], role_home(user["role"]))


def _prepare_link(transaction: dict, profile: oidc_accounts.GoogleProfile) -> str:
    """The second validation of a link, read-only, and then the pending link.

    The exchange took time, so the account is read again and its binding checked
    before anyone is asked for a password. Nothing is written to the database here.
    """
    conn = get_request_db()
    uid = transaction["uid"]
    secret_key = current_app.config["SECRET_KEY"]
    if oidc_accounts.bound_account(
            conn, uid, transaction["skd"], transaction["siat"], secret_key=secret_key, wall_now=time.time()) is None:
        _log_callback(FAILED, oauth_state.INTENT_LINK, user_id=uid, reason="account_changed")
        return _account_page(FAILED)
    refusal = oidc_accounts.link_decision(conn, uid, profile)
    if refusal is not None:
        _log_callback(refusal, oauth_state.INTENT_LINK, user_id=uid)
        return _account_page(refusal)
    pending = oauth_state.store_pending_link(
        transaction,
        subject=profile.subject,
        email=oidc_accounts.reported_email(profile.email),
        name=oidc_accounts.cleaned_name(profile.name),
    )
    if pending is None:
        _log_callback(FAILED, oauth_state.INTENT_LINK, user_id=uid, reason="cookie_budget")
        return _account_page(FAILED)
    _log_callback("pending_link", oauth_state.INTENT_LINK, user_id=uid)
    return url_for("oidc.google_link_confirm")


# --------------------------------------------------------------------------- confirm and cancel


def _confirmation_page(pending: dict, error: str | None, status: int = 200):
    account = loaded_account()
    page = render_template(
        "account_link_confirm.html",
        error=error,
        google_name=pending["name"],
        google_email=pending["email"],
        account_email=account["email"],
    )
    return page, status


@oidc_bp.route("/auth/google/link/confirm", methods=["GET", "POST"])
@access_policy(SIGNED_IN)
@integrity_policy(SAME_ORIGIN)
def google_link_confirm():
    if _provider() is None:
        abort(404)
    principal = current_principal()
    if request.method == "POST" and principal.role not in oidc_accounts.LINKABLE_ROLES:
        return render_template("403.html"), 403
    pending = pending_link_of_this_session(principal)
    if pending is None:
        if request.method == "POST":
            oauth_state.drop_pending_link()
        return redirect(_account_page(EXPIRED))
    if request.method == "GET":
        return _confirmation_page(pending, None)

    conn = get_request_db()
    account = loaded_account()
    now = _utcnow()
    # The account's own counter: sign-in, password-change and confirmation guesses
    # share one limit.
    key = account_throttle_key(account)
    if login_throttle.is_blocked(conn, key, now):
        return _confirmation_page(pending, THROTTLED_MESSAGE, 429)
    password = request.form.get("current_password", "")
    if len(password) > accounts_service.MAX_PASSWORD_LENGTH \
            or not accounts_service.verify_password(conn, principal.user_id, password):
        login_throttle.record_failure(conn, key, now)
        conn.commit()
        # The pending link stays: the person can try again until it expires or the
        # counter blocks.
        return _confirmation_page(pending, WRONG_CURRENT_PASSWORD_MESSAGE)

    # The password was verified before the transaction, so the write lock is not held
    # during the hashing. A password changed in between rotates the session key,
    # which the write transaction checks.
    began = False
    try:
        monotonic_now = oauth_state.begin_write(conn)
        began = True
        login_throttle.clear(conn, key)
        outcome = oidc_accounts.confirm_link(
            conn,
            pending,
            secret_key=current_app.config["SECRET_KEY"],
            session_key=session.get("sk"),
            session_iat=session.get("iat"),
            now=monotonic_now,
            wall_now=time.time(),
        )
        conn.commit()
    except BaseException as exc:
        # Nothing was recorded, so the pending link is as unused as before and stays
        # in the session. The answer claims nothing about what reached the disk.
        if began:
            oauth_state.rollback_own(conn)
        if not isinstance(exc, Exception):
            raise
        code = UNAVAILABLE if isinstance(exc, sqlite3.Error) else FAILED
        logger.warning("link_confirm outcome=%s user=%s reason=%s", code, principal.user_id, type(exc).__name__)
        return redirect(_account_page(code))
    oauth_state.drop_pending_link()
    logger.log(
        logging.INFO if outcome == oidc_accounts.LINKED else logging.WARNING,
        "link_confirm outcome=%s user=%s reason=-", outcome, principal.user_id,
    )
    return redirect(_account_page(outcome))


def cancel_code(retirement: oauth_state.Retirement, has_google_identity: bool | None) -> str:
    """What a cancellation may say: the first row of design 1.6 that applies.

    ``has_google_identity`` is the one read of the account, ``None`` when it failed.
    ``link_cancelled`` is a statement about this request's own record: it is given
    only when every linking object the session held was invalidated by it and the
    account had no Google identity at that read. A consumed object, an empty session,
    a dead object, a record that could not be written and an account that could not
    be read never give it.
    """
    if has_google_identity is None:
        return CANCEL_UNCONFIRMED
    if has_google_identity:
        return ACCOUNT_LINKED
    if not retirement.complete:
        return CANCEL_UNCONFIRMED
    if retirement.all_invalidated:
        return LINK_CANCELLED
    return LINK_NOT_PENDING


@oidc_bp.route("/auth/google/link/cancel", methods=["POST"])
@access_policy(SIGNED_IN)
@integrity_policy(SAME_ORIGIN)
def google_link_cancel():
    if _provider() is None:
        abort(404)
    principal = current_principal()
    # Both slots leave this browser's session before anything below is decided.
    retirement = oauth_state.retire_linking()
    # After the retirement on purpose: a confirmation commits its record and its
    # identity together, so once the retirement has met that record, the identity is
    # visible to this read. One read, at one moment.
    try:
        linked = oidc_accounts.has_google_identity(get_request_db(), principal.user_id)
    except Exception as exc:
        logger.warning("cancel_account_read_failed user=%s error=%s", principal.user_id, type(exc).__name__)
        linked = None
    return redirect(_account_page(cancel_code(retirement, linked)))
