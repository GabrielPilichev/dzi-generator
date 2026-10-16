"""Student and teacher registration by email and password
(docs/reviews/EMAIL_REGISTRATION_DESIGN.md, sections 1, 2.3, 3.10, 4.3, 6.2 to 6.4 and 7.1;
PR 4 and PR 7).

``/register`` (chooser), ``/register/student`` and ``/register/teacher`` (form and
submission), ``/register/verify``, ``POST /register/resend`` and ``POST /register/cancel``.
``/register/teacher`` and everything that belongs to a teacher registration (its chooser
entry, its resend, its verification and its activation) are behind
``registration.TEACHER_ACTIVATION`` (D5, D14), read at each request: while it is off the
address is the generic 404, nothing links to it, and a teacher registration that is
pending is voided at its next resend or verification and never becomes a student. Both
forms share one submit, one verify and one settlement path; the account type is the
route's literal and no submitted field decides it or the role.

* **Disabled** (no :class:`~web.registration_settings.RegistrationSettings` in the
  configuration, which in production needs the build's gate): every address here is
  the generic 404 after the same-origin check, and no page links to it.
* **The browser binding** (design 2.3) is the session slot ``reg``:
  ``{"v": 1, "t": <token>, "next": <validated path or "">}``. It holds no personal
  data, no code and no password, and it is not an account key: nothing here signs
  anyone in, and sign-in, the pending second factor and sign-out drop it with the rest
  of the session (``web.account_session``). A slot of any other shape is ignored.
* **The service decides; this module owns the transactions.** Each writer runs inside
  ``BEGIN IMMEDIATE`` begun here and reads its clock itself under the write lock. A
  returned outcome is a decision until ``COMMIT`` has returned: the session, the
  response and every log line of a durable event follow the commit. Any exception, a
  failed commit included, rolls everything back and is answered with the generic
  failure; it never claims an account, an "address taken" or a sent code.
* **One dispatch per issuance.** Only the request that committed an issuance hands it
  to the mailer, once, with no transaction open, and then settles it in a transaction
  of its own with the same generation number and identity. Nothing here reads a
  ``started`` generation back from the database to send or settle it, and nothing is
  retried: a repeated or concurrent request is a new issuance, admitted or refused by
  the service's limits, or no issuance at all. Nothing is settled after
  ``NotSendable``. A crash between the commit and the send can abandon an issuance
  (its ledger row stays ``started``); no exactly-once delivery is claimed.
* **No session is issued.** A correct code creates the account and redirects to
  ``/login`` (decision D7); sign-in is email and password, as for every account.

Log lines (logger ``learnpilot.registration``) hold an event name and row ids: never an
address, a name, a password, a code, the binding token, the issuance identity or a
message. The module imports neither ``web.app`` nor another controller.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone

from flask import Blueprint, abort, current_app, redirect, render_template, request, session, url_for

from src import subject_registry
from web.access import PUBLIC, access_policy, current_principal
from web.account_session import role_home
from web.registration_settings import CONFIG_KEY, RegistrationSettings
from web.request_db import get_request_db
from web.request_integrity import SAME_ORIGIN, integrity_policy
from web.services import accounts as accounts_service
from web.services import mail
from web.services import registration as registration_service
from web.services import registration_forms as forms

registration_bp = Blueprint("registration", __name__)

logger = logging.getLogger("learnpilot.registration")

Outcome = registration_service.Outcome

SLOT = "reg"
SLOT_VERSION = 1
_SLOT_KEYS = frozenset({"v", "t", "next"})
_TOKEN_LENGTH = 43  # secrets.token_urlsafe(32)
_URLSAFE = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")

# Texts of design 1.2.
VERIFY_INTRO = "Изпратихме 6-цифрен код на {email}. Въведи го, за да потвърдиш имейла си."
WRONG_CODE_MESSAGE = "Грешен или изтекъл код."
TOO_MANY_MESSAGE = "Твърде много грешни опити. Поискай нов код."
RESENT_MESSAGE = "Изпратихме нов код."
TOO_EARLY_MESSAGE = "Можеш да поискаш нов код след малко."
RESEND_EXHAUSTED_MESSAGE = "Не можеш да поискаш повече кодове. Започни регистрацията отново."
ADDRESS_CAPPED_MESSAGE = "Твърде много имейли към този адрес. Опитай отново по-късно."
NOT_SENT_MESSAGE = "Не успяхме да изпратим имейла. Опитай отново след малко."
NO_REGISTRATION_MESSAGE = "Регистрацията е изтекла или е започната от друг браузър. Започни отново."
ACTIVATED_MESSAGE = "Имейлът е потвърден. Влез с имейл и парола."
EMAIL_TAKEN_MESSAGE = "Вече има профил с този имейл. Влез с имейл и парола."
UNAVAILABLE_MESSAGE = "Регистрацията временно не е достъпна. Опитай по-късно."
FAILURE_MESSAGE = "Нещо се обърка и нищо не беше записано. Опитай отново."

# What ``/login?registration=<code>`` shows (D7): looked up, never echoed, and only
# while registration is enabled.
LOGIN_NOTICES = {
    "activated": (ACTIVATED_MESSAGE, "success"),
    "email_taken": (EMAIL_TAKEN_MESSAGE, "warning"),
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _settings() -> RegistrationSettings | None:
    settings = current_app.config.get(CONFIG_KEY)
    return settings if isinstance(settings, RegistrationSettings) else None


def _enabled_settings() -> RegistrationSettings:
    """The settings of an enabled registration; the generic 404 otherwise."""
    settings = _settings()
    if settings is None:
        abort(404)
    return settings


def _teacher_enabled() -> bool:
    """The teacher gate, read at the request (design 7.3): true only for the value ``True``."""
    return registration_service.TEACHER_ACTIVATION is True


def _enabled_teacher_settings() -> RegistrationSettings:
    """The settings of an enabled registration whose teacher gate is on; else the generic 404."""
    settings = _enabled_settings()
    if not _teacher_enabled():
        abort(404)
    return settings


def _signed_in_redirect():
    """A signed-in account starts nothing here and is sent to its home (design 6.2).

    Only the ``/register...`` routes ask this; account and profile pages do not.
    """
    principal = current_principal()
    if principal.user_id is None:
        return None
    return redirect(url_for(role_home(principal.role)), code=303)


@registration_bp.app_context_processor
def inject_registration():
    """``registration_enabled`` and ``registration_login_notice`` for templates."""
    return {"registration_enabled": _settings() is not None, "registration_login_notice": _login_notice}


def _login_notice() -> dict | None:
    if _settings() is None:
        return None
    found = LOGIN_NOTICES.get(request.args.get("registration") or "")
    return {"text": found[0], "kind": found[1]} if found else None


@registration_bp.after_request
def _no_store(response):
    # Pages of a registration in progress show an address and carry a binding: they
    # are not cached and send no referrer (design 6.2).
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
    return registration_service.binding_hash(current_app.config["SECRET_KEY"], slot["t"])


def _store_slot(token: str, next_target: str | None) -> None:
    session[SLOT] = {"v": SLOT_VERSION, "t": token, "next": next_target or ""}


def _drop_slot() -> None:
    if SLOT in session:
        session.pop(SLOT)


def _query_next() -> str:
    return forms.accepted_next(request.args.get("next")) or ""


# --------------------------------------------------------------------------- transactions


def _rollback(conn: sqlite3.Connection) -> None:
    try:
        if conn.in_transaction:
            conn.rollback()
    except sqlite3.Error as exc:
        logger.warning("rollback_failed error=%s", type(exc).__name__)


def _write(conn: sqlite3.Connection, writer, *args, **kwargs):
    """``BEGIN IMMEDIATE``, one service writer, ``COMMIT``.

    The writer takes the write lock and reads its clock inside this transaction. Any
    exception, the commit's included, rolls back and propagates: what the writer
    returned is then not true of the database, and nothing may act on it.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        result = writer(conn, *args, **kwargs)
        conn.commit()
    except BaseException:
        _rollback(conn)
        raise
    return result


def _send(conn: sqlite3.Connection, settings: RegistrationSettings, issued) -> None:
    """Hand the issuance this request committed to the mailer, once, then settle it.

    Called once per committed issuance and from nowhere else, with no transaction open.
    ``dispatch`` verifies that ``issued`` is the committed, unsettled, current issuance
    exactly as issued; when it refuses (``NotSendable``) or fails before the mailer is
    reached, nothing was sent and nothing is settled. A mailer's own exception is
    ``unknown`` inside ``dispatch``. The outcome is never retried.
    """
    try:
        outcome = registration_service.dispatch(
            conn, settings.mailer, issued, secret_key=current_app.config["SECRET_KEY"])
    except registration_service.NotSendable:
        logger.warning("dispatch_refused pending=%d send=%d", issued.pending_id, issued.send_id)
        return
    except Exception as exc:
        logger.warning("dispatch_failed pending=%d send=%d error=%s", issued.pending_id, issued.send_id,
                       type(exc).__name__)
        return
    _settle(conn, issued, outcome)


def _settle(conn: sqlite3.Connection, issued, outcome: mail.SendOutcome) -> None:
    """Record the outcome of the issuance just dispatched, by its number and its identity.

    A failure of this transaction is logged and left: the ledger row stays ``started``,
    which is safe (counted in every cap, not in the circuit, its code usable).
    """
    try:
        settled = _write(conn, registration_service.settle_send, issued.send_id, issued.issuance_id, outcome,
                         clock=_utcnow)
    except Exception as exc:
        logger.warning("settlement_failed send=%d error=%s", issued.send_id, type(exc).__name__)
        return
    if settled is registration_service.Settlement.SETTLED:
        logger.info("send_settled send=%d outcome=%s", issued.send_id, outcome.value)


# --------------------------------------------------------------------------- pages


def _student_form(echo: dict, *, errors: dict | None = None, notice: str | None = None, status: int = 200):
    page = render_template(
        "register_student.html", echo=echo, errors=errors or {}, notice=notice, grades=forms.GRADES)
    return page, status


def _subject_choices() -> list[dict]:
    """The registry's subjects a teacher can name, in registry order (as the profile page lists them)."""
    return [{"id": subject.id, "title": subject.title} for subject in subject_registry.all_subjects()]


def _teacher_form(echo: dict, *, errors: dict | None = None, notice: str | None = None, status: int = 200):
    page = render_template(
        "register_teacher.html", echo=echo, errors=errors or {}, notice=notice,
        subjects=_subject_choices(), school_hint=forms.SCHOOL_HINT)
    return page, status


def _message(text: str, kind: str, status: int, *, next_target: str = ""):
    page = render_template("register_message.html", message=text, kind=kind, next_url=next_target)
    return page, status


def _unavailable(next_target: str = ""):
    return _message(UNAVAILABLE_MESSAGE, "error", 503, next_target=next_target)


def _failure(next_target: str = ""):
    return _message(FAILURE_MESSAGE, "error", 500, next_target=next_target)


def _no_registration(next_target: str = ""):
    return _message(NO_REGISTRATION_MESSAGE, "warning", 200, next_target=next_target)


def _verify_page(slot: dict, *, error: str | None = None, notice: str | None = None, status: int = 200,
                 code_error: bool = False):
    """The verify page of this browser's registration, read from its own row only.

    ``code_error`` says the ``error`` is about the code that was typed, so that the field
    is marked invalid; the other refusals (wait, cap, not sent) are not about the field.
    When the code was retired by a NOT_SENT delivery, that explanation is shown instead of
    any other error and the field is not marked, so the text and the aria attributes agree.
    """
    view = registration_service.pending_view(get_request_db(), _binding(slot), _utcnow())
    if view is None:
        return _no_registration(slot["next"])
    not_sent = NOT_SENT_MESSAGE if view.code_retired else None
    page = render_template(
        "register_verify.html",
        intro=VERIFY_INTRO.format(email=view.email),
        error=not_sent or error,
        code_error=code_error and not not_sent,
        notice=None if not_sent else notice,
    )
    return page, status


def _login_url(code: str | None, next_target: str) -> str:
    return url_for("account.login", next=next_target or None, registration=code)


# --------------------------------------------------------------------------- routes


@registration_bp.route("/register", methods=["GET"])
@access_policy(PUBLIC)
def choose():
    _enabled_settings()
    signed_in = _signed_in_redirect()
    if signed_in is not None:
        return signed_in
    return render_template("register_choose.html", next_url=_query_next(), teacher_enabled=_teacher_enabled())


@registration_bp.route("/register/student", methods=["GET", "POST"])
@access_policy(PUBLIC)
@integrity_policy(SAME_ORIGIN)
def student():
    settings = _enabled_settings()
    signed_in = _signed_in_redirect()
    if signed_in is not None:
        return signed_in
    if request.method == "GET":
        return _student_form({"next": _query_next()})
    validated = forms.validate_student(request.form)
    if not validated.ok:
        return _student_form(validated.echo, errors=validated.errors)
    return _submit(settings, validated, account_type=forms.STUDENT, render_form=_student_form)


def _submit(settings: RegistrationSettings, validated, *, account_type: str, render_form):
    """Design 6.4, "Submit": the reads, the hash, one write transaction, the slot, the
    send, the settlement.

    ``account_type`` is the literal of the calling route and ``render_form`` its form; the
    validated values of the other form are never read here.
    """
    conn = get_request_db()
    secret_key = current_app.config["SECRET_KEY"]
    email = validated.values["email"]
    now = _utcnow()
    # Read before any hashing, so a refused submission costs no scrypt work (design 5.1).
    # Both are decided again by the service under the write lock.
    available = registration_service.availability(
        conn, now, sender_busy=mail.sender_busy(), total_cap=settings.total_cap)
    next_target = validated.values["next"] or ""     # accepted_next() already ran in the form
    if available is not registration_service.Availability.AVAILABLE:
        return _unavailable(next_target)
    if registration_service.address_capped(conn, registration_service.email_hash(secret_key, email), now):
        return render_form(validated.echo, notice=ADDRESS_CAPPED_MESSAGE, status=429)

    password_hash = accounts_service.hash_new_password(validated.password, email)
    previous = _slot()
    try:
        issued = _write(
            conn, registration_service.create_pending,
            cleaned=validated.values,
            account_type=account_type,  # the route's literal; no submitted field is read
            password_hash=password_hash,
            previous_binding=_binding(previous) if previous is not None else None,
            secret_key=secret_key,
            clock=_utcnow,
            sender_busy=mail.sender_busy(),
            total_cap=settings.total_cap,
            teacher_enabled=_teacher_enabled(),
        )
    except Exception as exc:
        logger.warning("submit_failed error=%s", type(exc).__name__)
        return _failure(next_target)

    if issued.outcome is Outcome.UNAVAILABLE:
        return _unavailable(next_target)
    if issued.outcome is Outcome.ADDRESS_CAPPED:
        return render_form(validated.echo, notice=ADDRESS_CAPPED_MESSAGE, status=429)
    if issued.outcome is not Outcome.ISSUED:
        logger.warning("submit_unexpected outcome=%s", issued.outcome.value)
        return _failure(next_target)
    # Committed: the registration exists. The slot, the log line and the send follow.
    logger.info("submitted pending=%d send=%d", issued.pending_id, issued.send_id)
    _store_slot(issued.token, validated.values["next"])
    _send(conn, settings, issued)
    return redirect(url_for("registration.verify"), code=303)


@registration_bp.route("/register/teacher", methods=["GET", "POST"])
@access_policy(PUBLIC)
@integrity_policy(SAME_ORIGIN)
def teacher():
    # Behind the teacher gate (D5, D14): the generic 404, after the same-origin check, while
    # the gate is off, and no page links here then. The account type below is this route's
    # literal; a submitted field named role or account_type is never read.
    settings = _enabled_teacher_settings()
    signed_in = _signed_in_redirect()
    if signed_in is not None:
        return signed_in
    if request.method == "GET":
        return _teacher_form({"next": _query_next()})
    validated = forms.validate_teacher(request.form)
    if not validated.ok:
        return _teacher_form(validated.echo, errors=validated.errors)
    return _submit(settings, validated, account_type=forms.TEACHER, render_form=_teacher_form)


@registration_bp.route("/register/verify", methods=["GET", "POST"])
@access_policy(PUBLIC)
@integrity_policy(SAME_ORIGIN)
def verify():
    _enabled_settings()
    signed_in = _signed_in_redirect()
    if signed_in is not None:
        return signed_in
    slot = _slot()
    if request.method == "GET":
        if slot is None:
            return redirect(url_for("registration.choose"))
        return _verify_page(slot, notice=RESENT_MESSAGE if request.args.get("resent") == "1" else None)
    if slot is None:
        # Another browser, or a slot of another shape: no row is read by code or address.
        return _no_registration()

    conn = get_request_db()
    try:
        result = _write(
            conn, registration_service.verify, _binding(slot), request.form.get("code", ""),
            secret_key=current_app.config["SECRET_KEY"], clock=_utcnow,
            teacher_enabled=_teacher_enabled(),
        )
    except Exception as exc:
        # Rolled back: no account, the registration pending with its code and attempts
        # as before. The slot stays, so the same code can be tried again.
        logger.warning("activation_failed error=%s", type(exc).__name__)
        return _failure(slot["next"])

    outcome = result.outcome
    if outcome is Outcome.ACTIVATED:
        logger.info("activated pending=%d user=%d", result.pending_id, result.user_id)
        _drop_slot()
        return redirect(_login_url("activated", slot["next"]), code=303)
    if outcome is Outcome.EMAIL_TAKEN:
        logger.info("email_taken pending=%d", result.pending_id)
        _drop_slot()
        return redirect(_login_url("email_taken", slot["next"]), code=303)
    if outcome is Outcome.WRONG_CODE:
        logger.info("wrong_code pending=%d", result.pending_id)
        return _verify_page(slot, error=WRONG_CODE_MESSAGE, code_error=True)
    if outcome is Outcome.CODE_EXPIRED:
        return _verify_page(slot, error=WRONG_CODE_MESSAGE, code_error=True)
    if outcome is Outcome.TOO_MANY:
        return _verify_page(slot, error=TOO_MANY_MESSAGE, code_error=True)
    if outcome is Outcome.NOT_PENDING and result.final_state == "activated":
        # A repeat of a verification that succeeded (design 6.2): nothing written. Only an
        # activated row sends the person on silently; an expired, cancelled or replaced
        # one has no account and gets the "start again" page below.
        _drop_slot()
        return redirect(_login_url(None, slot["next"]), code=303)
    return _ended_or_unavailable(result, slot)


def _ended_or_unavailable(result, slot: dict):
    """The answers verify and resend share for a registration that cannot go on."""
    if result.outcome is Outcome.UNAVAILABLE:
        if result.reason is registration_service.Availability.TEACHER_OFF:
            # The service voided the row: the binding is spent.
            logger.info("gate_off pending=%d", result.pending_id)
            _drop_slot()
        return _unavailable(slot["next"])    # read from the slot dict, which outlives the dropped session key
    # EXPIRED, NO_PENDING and a NOT_PENDING that is not activated: the binding has nothing left to complete.
    _drop_slot()
    return _no_registration(slot["next"])


@registration_bp.route("/register/resend", methods=["POST"])
@access_policy(PUBLIC)
@integrity_policy(SAME_ORIGIN)
def resend():
    settings = _enabled_settings()
    signed_in = _signed_in_redirect()
    if signed_in is not None:
        return signed_in
    slot = _slot()
    if slot is None:
        return _no_registration()

    conn = get_request_db()
    try:
        issued = _write(
            conn, registration_service.resend, _binding(slot),
            secret_key=current_app.config["SECRET_KEY"], clock=_utcnow, sender_busy=mail.sender_busy(),
            total_cap=settings.total_cap, teacher_enabled=_teacher_enabled(),
        )
    except Exception as exc:
        logger.warning("resend_failed error=%s", type(exc).__name__)
        return _failure(slot["next"])

    outcome = issued.outcome
    if outcome is Outcome.ISSUED:
        # Committed: a new generation with a new code. Sent once, by this request.
        logger.info("resent pending=%d send=%d", issued.pending_id, issued.send_id)
        _send(conn, settings, issued)
        return redirect(url_for("registration.verify", resent=1), code=303)
    if outcome is Outcome.TOO_EARLY:
        return _verify_page(slot, error=TOO_EARLY_MESSAGE)
    if outcome is Outcome.RESEND_EXHAUSTED:
        return _verify_page(slot, error=RESEND_EXHAUSTED_MESSAGE)
    if outcome is Outcome.ADDRESS_CAPPED:
        return _verify_page(slot, error=ADDRESS_CAPPED_MESSAGE, status=429)
    return _ended_or_unavailable(issued, slot)


@registration_bp.route("/register/cancel", methods=["POST"])
@access_policy(PUBLIC)
@integrity_policy(SAME_ORIGIN)
def cancel():
    _enabled_settings()
    signed_in = _signed_in_redirect()
    if signed_in is not None:
        return signed_in
    slot = _slot()
    if slot is None:
        return redirect(url_for("registration.choose"), code=303)

    conn = get_request_db()
    try:
        result = _write(conn, registration_service.cancel, _binding(slot), clock=_utcnow)
    except Exception as exc:
        logger.warning("cancel_failed error=%s", type(exc).__name__)
        return _failure(slot["next"])
    if result.outcome is Outcome.UNAVAILABLE:
        # The clock is behind the high-water mark: nothing was voided, the slot stays.
        return _unavailable(slot["next"])
    if result.outcome is Outcome.CANCELLED:
        logger.info("cancelled pending=%d", result.pending_id)
    _drop_slot()
    # Back to the chooser, not to a form: the binding does not say which form began the
    # registration, and the student form must not be offered to someone who began as a
    # teacher. The stored next passed accepted_next when the slot was read.
    return redirect(url_for("registration.choose", next=slot["next"] or None), code=303)
