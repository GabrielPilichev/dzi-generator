"""Password recovery by an emailed code (docs/reviews/PASSWORD_RECOVERY_DESIGN.md, sections 2.2 to
2.7, 3.5, 3.6, 7.1, 7.5 and 8.1; scope 8.3).

``/recover`` (the form and the request), ``/recover/verify`` (the code, the new password and its
confirmation in one POST), ``POST /recover/resend`` and ``POST /recover/cancel``.

* **Disabled** (no :class:`~web.recovery_settings.RecoverySettings` in the configuration, which in
  production needs the build's gate ``recovery.PRODUCTION_ACTIVATION``): every address here is the
  generic 404 after the same-origin check, for every visitor, and no page links to it.
* **Order of every route**: the same-origin check (the application's own hook, which runs first
  for every unsafe method), the gate, the signed-in redirect, the session slot; a redemption then
  reads this browser's row (``pending_view``), checks the password against the address that row
  holds, checks the confirmation exactly, and only then calls the service in one transaction.
  A signed-in visitor starts nothing and is sent to the home of the role; recovery is for a
  signed-out browser.
* **The browser binding** (design 2.2) is the session slot ``rec``:
  ``{"v": 1, "t": <token>, "next": <validated path or "">}``. It holds no address, no code, no
  password and no account key: nothing here signs anyone in, and sign-in, the pending second
  factor and sign-out drop it with the rest of the session (``web.account_session``). A slot of
  any other shape is ignored. Nothing but the slot, and the fixed query values ``resent=1`` and
  ``recovery=done`` (looked up, never echoed), carries recovery state; no address, code or
  password is in a URL, a cookie or a redirect.
* **The service decides; this module owns the transactions.** Each writer runs inside
  ``BEGIN IMMEDIATE`` begun here and reads its clock itself under the write lock. A returned
  outcome (``ISSUED``, ``REDEEMED``, a counted wrong code, a voiding) is a decision until
  ``COMMIT`` has returned: the session slot, the response, the dispatch and every log line of a
  durable event follow the commit. A failure is told apart: an exception **before** the commit
  followed by a rollback that returned means nothing took effect (the failure text says the
  password was not changed); a commit that raised, or a rollback that raised, leaves an outcome
  this module cannot establish, and the text says so. No failed commit claims a reset.
* **One dispatch per issuance.** Only the request that committed an issuance hands that very
  result to the mailer, once, with no transaction open, and then settles it in a transaction of
  its own with the same generation number and identity. Nothing here reads a ``started``
  generation back from the database to send or settle it, and nothing is retried: a repeated or
  concurrent request is a new issuance, admitted or refused by the service's limits. Nothing is
  settled after ``NotSendable``. A crash between the commit and the send can abandon an issuance
  (its ledger row stays ``started`` and counts); no exactly-once delivery is claimed.
* **No session is issued.** A correct code sets the password and redirects to ``/login``
  (decision D7); sign-in is email and password, as for every account, and the sign-in throttle is
  neither read nor written here (RD8 (a), RD10).
* **No GET writes to the database**, and a GET with no usable recovery sets no cookie. The
  stale-session cleanup of the application's own ``load_current_user`` hook is separate and
  unchanged: a request that carries an account identity that no longer resolves may still have
  its identity keys removed from the cookie, so no claim is made that no ``Set-Cookie`` is ever
  sent.

Log lines (logger ``learnpilot.recovery``) hold an event name and row ids: never an address, a
password, a code, the binding token, any hash, key or digest, the issuance identity, a message,
or whether a row is a shadow. The module imports neither ``web.app`` nor another controller.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone

from flask import Blueprint, abort, current_app, redirect, render_template, request, session, url_for

from web.access import PUBLIC, access_policy, current_principal
from web.account_session import role_home
from web.recovery_settings import CONFIG_KEY, RecoverySettings
from web.request_db import get_request_db
from web.request_integrity import SAME_ORIGIN, integrity_policy
from web.services import accounts as accounts_service
from web.services import mail
from web.services import recovery as recovery_service
from web.services import registration as registration_service
from web.services import registration_forms as forms

recovery_bp = Blueprint("recovery", __name__)

logger = logging.getLogger("learnpilot.recovery")

Outcome = recovery_service.Outcome

SLOT = "rec"
SLOT_VERSION = 1
_SLOT_KEYS = frozenset({"v", "t", "next"})
_TOKEN_LENGTH = 43  # secrets.token_urlsafe(32)
_URLSAFE = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
_PATH_PREFIX = "/recover"

# Texts of design 2.7 and 2.8: proposed wording, RD15 keeps the exact texts open until
# activation. The last four are the same kind of text for the cases the design leaves to the
# route (a failure of a request, a resend or a cancellation); they are proposed as well.
VERIFY_INTRO = ("Ако за {email} може да се зададе нова парола с код, изпратихме на този адрес 8-цифрен код. "
                "Въведи го заедно с новата парола.")
REQUEST_INTRO = ("Въведи имейла на профила си. Ако за него може да се зададе нова парола с код, "
                 "ще изпратим 8-цифрен код на този адрес.")
WRONG_CODE_MESSAGE = "Грешен или изтекъл код."
TOO_MANY_MESSAGE = "Твърде много грешни опити. Поискай нов код."
RESENT_MESSAGE = "Изпратихме нов код."
TOO_EARLY_MESSAGE = "Можеш да поискаш нов код след малко."
RESEND_EXHAUSTED_MESSAGE = "Не можеш да поискаш повече кодове. Започни отначало."
ADDRESS_CAPPED_MESSAGE = "Твърде много имейли към този адрес. Опитай отново по-късно."
NO_RECOVERY_MESSAGE = "Заявката за нова парола е изтекла или е започната от друг браузър. Започни отново."
INVALIDATED_MESSAGE = "Не успяхме да зададем нова парола. Започни отново."
UNAVAILABLE_MESSAGE = "Възстановяването на парола временно не е достъпно. Опитай по-късно."
REDEEM_FAILURE_MESSAGE = "Нещо се обърка и паролата не беше сменена. Опитай отново."
REDEEM_UNCERTAIN_MESSAGE = ("Не успяхме да потвърдим дали паролата е сменена. Опитай да влезеш с новата парола; "
                            "ако не става, започни отначало.")
FAILURE_MESSAGE = "Нещо се обърка и нищо не беше записано. Опитай отново."
UNCERTAIN_MESSAGE = "Не успяхме да потвърдим дали заявката е записана. Опитай отново след малко."
INVALID_EMAIL_MESSAGE = forms.MESSAGES["email"]
PASSWORD_MISMATCH_MESSAGE = forms.MESSAGES["password_mismatch"]

# What ``/login?recovery=done`` shows (RD16): a fixed text selected by the URL, never echoed, only
# while recovery is enabled. It changes no account state and is **not proof that a reset
# occurred**: a hand-built URL shows it too. Nothing decides anything from it.
DONE_MESSAGE = ("Паролата е сменена. Влез с новата парола. "
                "Ако видиш съобщението „Твърде много опити“, изчакай няколко минути.")
LOGIN_NOTICES = {"done": (DONE_MESSAGE, "success")}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _settings() -> RecoverySettings | None:
    settings = current_app.config.get(CONFIG_KEY)
    return settings if isinstance(settings, RecoverySettings) else None


def _enabled_settings() -> RecoverySettings:
    """The settings of an enabled recovery; the generic 404 otherwise (design 2.6)."""
    settings = _settings()
    if settings is None:
        abort(404)
    return settings


def _signed_in_redirect():
    """A signed-in account starts nothing here and is sent to its home (design 2.6)."""
    principal = current_principal()
    if principal.user_id is None:
        return None
    return redirect(url_for(role_home(principal.role)), code=303)


@recovery_bp.app_context_processor
def inject_recovery():
    """``recovery_enabled`` and ``recovery_login_notice`` for templates."""
    return {"recovery_enabled": _settings() is not None, "recovery_login_notice": _login_notice}


def _login_notice() -> dict | None:
    if _settings() is None:
        return None
    found = LOGIN_NOTICES.get(request.args.get("recovery") or "")
    return {"text": found[0], "kind": found[1]} if found else None


@recovery_bp.after_app_request
def _no_store(response):
    # Every address under /recover, including a 403 from the same-origin check, the generic 404
    # of a disabled gate and an unknown path, is not cached and sends no referrer (design 2.6).
    # In production Nginx replaces the referrer header with its own single value (design 7.5).
    path = request.path
    if path == _PATH_PREFIX or path.startswith(_PATH_PREFIX + "/"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
    return response


# --------------------------------------------------------------------------- the session slot


def _slot() -> dict | None:
    """The binding this browser holds, or ``None`` for no slot or one of another shape."""
    value = session.get(SLOT)
    if not isinstance(value, dict) or set(value) != _SLOT_KEYS:
        return None
    version, token, next_target = value["v"], value["t"], value["next"]
    if type(version) is not int or version != SLOT_VERSION:
        return None
    if not (isinstance(token, str) and len(token) == _TOKEN_LENGTH and set(token) <= _URLSAFE):
        return None
    if not isinstance(next_target, str) or (next_target and forms.accepted_next(next_target) != next_target):
        return None
    return value


def _binding(slot: dict) -> str:
    return recovery_service.binding_hash(current_app.config["SECRET_KEY"], slot["t"])


def _store_slot(token: str, next_target: str | None) -> None:
    session[SLOT] = {"v": SLOT_VERSION, "t": token, "next": next_target or ""}


def _drop_slot() -> None:
    if SLOT in session:
        session.pop(SLOT)


def _query_next() -> str:
    return forms.accepted_next(request.args.get("next")) or ""


def _next_of(slot: dict | None) -> str:
    """The return target the slot holds, validated again at the point of use."""
    return (forms.accepted_next(slot["next"]) or "") if slot is not None else ""


# --------------------------------------------------------------------------- transactions


class WriteFailed(Exception):
    """A write transaction did not complete. ``uncertain`` says whether its outcome is unknown.

    ``uncertain`` is false only when nothing took effect for certain: the transaction could not
    begin, or the writer raised before any commit was attempted and the rollback returned. It is
    true when the commit raised (an I/O error reported during a commit does not by itself prove
    that nothing reached the database) or when a rollback raised (the connection is then in an
    unknown state). Only the exception's class name is ever logged, never its text.
    """

    def __init__(self, error: BaseException, *, uncertain: bool):
        super().__init__(type(error).__name__)
        self.error = error
        self.uncertain = uncertain


def _rollback(conn: sqlite3.Connection) -> bool:
    """Roll back; whether the connection is known to hold nothing (a rollback that raised is not)."""
    try:
        if conn.in_transaction:
            conn.rollback()
    except sqlite3.Error as exc:
        logger.warning("rollback_failed error=%s", type(exc).__name__)
        return False
    return True


def _write(conn: sqlite3.Connection, writer, *args, **kwargs):
    """``BEGIN IMMEDIATE``, one service writer, ``COMMIT``.

    The writer takes the write lock and reads its clock inside this transaction. Whatever the
    writer returned is a decision until the commit has returned. Any exception, the commit's
    included, rolls back and is raised as :class:`WriteFailed`, which says whether the outcome
    is known; nothing may act on the writer's result then.
    """
    try:
        conn.execute("BEGIN IMMEDIATE")
    except Exception as exc:
        raise WriteFailed(exc, uncertain=False) from exc
    try:
        result = writer(conn, *args, **kwargs)
    except Exception as exc:
        raise WriteFailed(exc, uncertain=not _rollback(conn)) from exc
    except BaseException:
        _rollback(conn)
        raise
    try:
        conn.commit()
    except Exception as exc:
        _rollback(conn)
        raise WriteFailed(exc, uncertain=True) from exc
    except BaseException:
        _rollback(conn)
        raise
    return result


def _send(conn: sqlite3.Connection, settings: RecoverySettings, issued) -> None:
    """Hand the issuance this request committed to the mailer, once, then settle it.

    Called once per committed issuance, with that very result, from nowhere else, and with no
    transaction open. ``dispatch`` verifies that ``issued`` is the committed, unsettled, current
    issuance exactly as issued; when it refuses (``NotSendable``) or fails before the mailer is
    reached, nothing was sent and nothing is settled, and it is never tried again. A mailer's own
    exception is ``unknown`` inside ``dispatch``. The outcome is never retried.
    """
    try:
        outcome = recovery_service.dispatch(
            conn, settings.mailer, issued, secret_key=current_app.config["SECRET_KEY"])
    except recovery_service.NotSendable:
        return  # the service logged the refusal; nothing was sent, so nothing is settled
    except Exception as exc:
        logger.warning("dispatch_failed error=%s", type(exc).__name__)
        return
    _settle(conn, issued, outcome)


def _settle(conn: sqlite3.Connection, issued, outcome: mail.SendOutcome) -> None:
    """Record the outcome of the issuance just dispatched, by its number and its identity.

    A failure of this transaction is logged and left: the ledger row stays ``started``, which is
    safe (counted in every cap, not in the circuit, its code usable). Never retried.
    """
    try:
        settled = _write(conn, recovery_service.settle_send, issued.send_id, issued.issuance_id, outcome,
                         clock=_utcnow)
    except WriteFailed as failure:
        logger.warning("settlement_failed error=%s", type(failure.error).__name__)
        return
    if settled is recovery_service.Settlement.SETTLED:
        logger.info("send_settled send=%d outcome=%s", issued.send_id, outcome.value)


# --------------------------------------------------------------------------- pages


def _request_form(*, email: str = "", next_target: str = "", error: str | None = None,
                  notice: str | None = None, status: int = 200):
    page = render_template("recover_request.html", intro=REQUEST_INTRO, email=email, next_url=next_target,
                           error=error, notice=notice)
    return page, status


def _message(text: str, kind: str, status: int, *, next_target: str = ""):
    page = render_template("recover_message.html", message=text, kind=kind, next_url=next_target)
    return page, status


def _unavailable(next_target: str = ""):
    return _message(UNAVAILABLE_MESSAGE, "error", 503, next_target=next_target)


def _no_recovery(next_target: str = ""):
    return _message(NO_RECOVERY_MESSAGE, "warning", 200, next_target=next_target)


def _failure(failure: WriteFailed, next_target: str = "", *, redeeming: bool = False):
    if redeeming:
        text = REDEEM_UNCERTAIN_MESSAGE if failure.uncertain else REDEEM_FAILURE_MESSAGE
    else:
        text = UNCERTAIN_MESSAGE if failure.uncertain else FAILURE_MESSAGE
    return _message(text, "error", 500, next_target=next_target)


def _render_verify(slot: dict, view, *, error: str | None = None, errors: dict | None = None,
                   notice: str | None = None, status: int = 200):
    """The verify page of this browser's recovery: the address it typed, from its own row only.

    ``errors`` are per field (``code``, ``password``, ``confirm_password``), so that the field is
    marked invalid and described; ``error`` is a refusal that is not about a field (a wait, a cap).
    Neither the code nor a password is ever echoed.
    """
    page = render_template(
        "recover_verify.html",
        intro=VERIFY_INTRO.format(email=view.email),
        error=error, errors=errors or {}, notice=notice, next_url=_next_of(slot),
    )
    return page, status


def _verify_page(slot: dict, **kwargs):
    view = recovery_service.pending_view(get_request_db(), _binding(slot), _utcnow())
    if view is None:
        return _no_recovery(_next_of(slot))
    return _render_verify(slot, view, **kwargs)


def _login_url(next_target: str, *, done: bool) -> str:
    return url_for("account.login", next=next_target or None, recovery="done" if done else None)


# --------------------------------------------------------------------------- routes


@recovery_bp.route("/recover", methods=["GET", "POST"])
@access_policy(PUBLIC)
@integrity_policy(SAME_ORIGIN)
def start():
    settings = _enabled_settings()
    signed_in = _signed_in_redirect()
    if signed_in is not None:
        return signed_in
    if request.method != "POST":    # a HEAD is a read, whatever it carries
        return _request_form(next_target=_query_next())
    return _submit(settings)


def _submit(settings: RecoverySettings):
    """Design 2.3, "Request": the address rule, the reads, one write transaction, the slot, the
    send, the settlement."""
    submitted = request.form.get("email")
    next_target = forms.accepted_next(request.form.get("next")) or ""
    shown = submitted[: accounts_service.MAX_EMAIL_LENGTH] if isinstance(submitted, str) else ""
    try:
        email = forms.public_email(submitted)
    except ValueError:
        return _request_form(email=shown, next_target=next_target, error=INVALID_EMAIL_MESSAGE)

    conn = get_request_db()
    secret_key = current_app.config["SECRET_KEY"]
    now = _utcnow()
    # Reads, without the lock, before any write or process; the service decides them again under
    # the lock (design 2.3, caller obligation 6).
    available = registration_service.availability(
        conn, now, sender_busy=mail.sender_busy(), total_cap=settings.total_cap)
    if available is not registration_service.Availability.AVAILABLE:
        return _unavailable(next_target)
    if registration_service.address_capped(conn, registration_service.email_hash(secret_key, email), now):
        return _request_form(email=shown, next_target=next_target, notice=ADDRESS_CAPPED_MESSAGE, status=429)

    previous = _slot()
    try:
        issued = _write(
            conn, recovery_service.request,
            email=email,
            previous_binding=_binding(previous) if previous is not None else None,
            secret_key=secret_key,
            clock=_utcnow,
            sender_busy=mail.sender_busy(),
            total_cap=settings.total_cap,
        )
    except WriteFailed as failure:
        logger.warning("request_failed error=%s", type(failure.error).__name__)
        return _failure(failure, next_target)

    if issued.outcome is Outcome.UNAVAILABLE:
        logger.info("unavailable reason=%s", issued.reason.value)
        return _unavailable(next_target)
    if issued.outcome is Outcome.ADDRESS_CAPPED:
        return _request_form(email=shown, next_target=next_target, notice=ADDRESS_CAPPED_MESSAGE, status=429)
    if issued.outcome is not Outcome.ISSUED:
        logger.warning("request_unexpected outcome=%s", issued.outcome.value)
        return _message(FAILURE_MESSAGE, "error", 500, next_target=next_target)
    # Committed: the recovery exists. The log line, the slot and the send follow.
    logger.info("requested recovery=%d send=%d", issued.recovery_id, issued.send_id)
    _store_slot(issued.token, next_target)
    _send(conn, settings, issued)
    return redirect(url_for("recovery.verify"), code=303)


@recovery_bp.route("/recover/verify", methods=["GET", "POST"])
@access_policy(PUBLIC)
@integrity_policy(SAME_ORIGIN)
def verify():
    _enabled_settings()
    signed_in = _signed_in_redirect()
    if signed_in is not None:
        return signed_in
    slot = _slot()
    if request.method != "POST":
        if slot is None:
            return redirect(url_for("recovery.start"))
        return _verify_page(slot, notice=RESENT_MESSAGE if request.args.get("resent") == "1" else None)
    if slot is None:
        # Another browser, or a slot of another shape: no row is read by code or address.
        return _no_recovery()
    return _redeem(slot)


def _redeem(slot: dict):
    """Design 2.4: the slot, this browser's row, the password rules, the exact confirmation, and only
    then one transaction. Nothing before it writes or counts an attempt."""
    conn = get_request_db()
    binding = _binding(slot)
    next_target = _next_of(slot)
    view = recovery_service.pending_view(conn, binding, _utcnow())
    if view is None:
        # None, final or past its time by the read: the binding has nothing left to complete.
        _drop_slot()
        return _no_recovery(next_target)

    password = request.form.get("password")
    confirmation = request.form.get("confirm_password")
    errors = {}
    problem = accounts_service.password_problem(password if password is not None else "", view.email)
    if problem:
        errors["password"] = problem
    # A password that is not text has its own error; the confirmation is judged only against text.
    if isinstance(password, str) and password != confirmation:
        errors["confirm_password"] = PASSWORD_MISMATCH_MESSAGE
    if errors:
        return _render_verify(slot, view, errors=errors)

    try:
        result = _write(
            conn, recovery_service.redeem, binding, request.form.get("code", ""), password,
            secret_key=current_app.config["SECRET_KEY"], clock=_utcnow,
        )
    except WriteFailed as failure:
        # Rolled back for certain, or not established: the page says which (design 2.4). The slot
        # stays, so the same code can be tried again; a retry is safe either way.
        logger.warning("redeem_failed error=%s", type(failure.error).__name__)
        return _failure(failure, next_target, redeeming=True)

    outcome = result.outcome
    if outcome is Outcome.REDEEMED:
        logger.info("redeemed recovery=%d user=%d", result.recovery_id, result.user_id)
        _drop_slot()
        return redirect(_login_url(next_target, done=True), code=303)
    if outcome is Outcome.WRONG_CODE:
        logger.info("wrong_code recovery=%d", result.recovery_id)
        return _render_verify(slot, view, errors={"code": WRONG_CODE_MESSAGE})
    if outcome is Outcome.CODE_EXPIRED:
        return _render_verify(slot, view, errors={"code": WRONG_CODE_MESSAGE})
    if outcome is Outcome.TOO_MANY:
        return _render_verify(slot, view, errors={"code": TOO_MANY_MESSAGE})
    if outcome is Outcome.INVALIDATED:
        logger.info("invalidated recovery=%d", result.recovery_id)
        _drop_slot()
        return _message(INVALIDATED_MESSAGE, "error", 200, next_target=next_target)
    if outcome is Outcome.NOT_PENDING and result.final_state == "redeemed":
        # A repeat of a reset that succeeded (design 2.4): nothing was written. It is not a new
        # success, so the notice of the URL is not added.
        _drop_slot()
        return redirect(_login_url(next_target, done=False), code=303)
    return _ended_or_unavailable(result, next_target)


def _ended_or_unavailable(result, next_target: str):
    """The answers verify and resend share for a recovery that cannot go on."""
    if result.outcome is Outcome.UNAVAILABLE:
        logger.info("unavailable reason=%s", result.reason.value)
        return _unavailable(next_target)    # the hold or the clock: the slot stays
    if result.outcome is Outcome.EXPIRED:
        logger.info("expired recovery=%d", result.recovery_id)
    # EXPIRED, NO_PENDING and a NOT_PENDING that is not a reset: nothing is left to complete.
    _drop_slot()
    return _no_recovery(next_target)


@recovery_bp.route("/recover/resend", methods=["POST"])
@access_policy(PUBLIC)
@integrity_policy(SAME_ORIGIN)
def resend():
    settings = _enabled_settings()
    signed_in = _signed_in_redirect()
    if signed_in is not None:
        return signed_in
    slot = _slot()
    if slot is None:
        return _no_recovery()

    conn = get_request_db()
    next_target = _next_of(slot)
    try:
        issued = _write(
            conn, recovery_service.resend, _binding(slot),
            secret_key=current_app.config["SECRET_KEY"], clock=_utcnow, sender_busy=mail.sender_busy(),
            total_cap=settings.total_cap,
        )
    except WriteFailed as failure:
        logger.warning("resend_failed error=%s", type(failure.error).__name__)
        return _failure(failure, next_target)

    outcome = issued.outcome
    if outcome is Outcome.ISSUED:
        # Committed: a new generation with a new code (or a repeated notice). Sent once, by
        # this request, with this very result.
        logger.info("resent recovery=%d send=%d", issued.recovery_id, issued.send_id)
        _send(conn, settings, issued)
        return redirect(url_for("recovery.verify", resent=1), code=303)
    if outcome is Outcome.TOO_EARLY:
        return _verify_page(slot, error=TOO_EARLY_MESSAGE)
    if outcome is Outcome.RESEND_EXHAUSTED:
        return _verify_page(slot, error=RESEND_EXHAUSTED_MESSAGE)
    if outcome is Outcome.ADDRESS_CAPPED:
        return _verify_page(slot, error=ADDRESS_CAPPED_MESSAGE, status=429)
    return _ended_or_unavailable(issued, next_target)


@recovery_bp.route("/recover/cancel", methods=["POST"])
@access_policy(PUBLIC)
@integrity_policy(SAME_ORIGIN)
def cancel():
    _enabled_settings()
    signed_in = _signed_in_redirect()
    if signed_in is not None:
        return signed_in
    slot = _slot()
    if slot is None:
        return redirect(url_for("recovery.start"), code=303)

    conn = get_request_db()
    next_target = _next_of(slot)
    try:
        result = _write(conn, recovery_service.cancel, _binding(slot), clock=_utcnow)
    except WriteFailed as failure:
        logger.warning("cancel_failed error=%s", type(failure.error).__name__)
        return _failure(failure, next_target)
    if result.outcome is Outcome.UNAVAILABLE:
        # The clock is behind the high-water mark: nothing was voided, the slot stays.
        logger.info("unavailable reason=%s", result.reason.value)
        return _unavailable(next_target)
    if result.outcome is Outcome.CANCELLED:
        logger.info("cancelled recovery=%d", result.recovery_id)
    # The same answer whether the row was pending, final or absent (design 2.5).
    _drop_slot()
    return redirect(url_for("recovery.start", next=next_target or None), code=303)
