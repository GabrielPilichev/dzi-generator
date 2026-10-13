"""OAuth state kept in the signed session, and the record that makes it single-use
(docs/reviews/GOOGLE_OIDC_DESIGN.md, sections 1.5, 2, 3, 4.4 and 9).

Two objects can be in a session:

* the **transaction** (``session["oidc_tx"]``): state, nonce, PKCE verifier, intent,
  validated return target, expiry, the service instance that issued it and, for
  linking, the binding to the account session that started it;
* the **pending link** (``session["oidc_link"]``): a validated Google identity that
  waits for the account's password.

Neither ever holds the client secret, an authorization code or a token. The cookie is
signed, not encrypted, and the server cannot take a cookie back: removing a slot from
a response revokes no earlier copy of the cookie. What stops a copy is the consumption
record written here, the instance and the expiry.

``verify_oauth_state`` is the request-integrity mechanism of the one view that
declares ``OAUTH_STATE``. It is not an alternative to the same-origin check: that view
accepts no unsafe method, and every unsafe request is still checked first.

Flask's session and request, the request's connection, the consumption service and the
standard library only: no blueprint, no account service and no provider code is
imported here.
"""

from __future__ import annotations

import logging
import secrets
import sqlite3
import time
from dataclasses import dataclass
from hmac import compare_digest

from flask import current_app, g, redirect, request, session
from werkzeug.exceptions import MethodNotAllowed

from web.request_db import get_request_db
from web.request_integrity import OAUTH_STATE, integrity_policy_for_view
from web.services import oidc_consumption

logger = logging.getLogger("learnpilot.oidc")

# app.config key of the enabled provider (a ``google_oidc.ProviderConfig``). Absent or
# ``None``: Google sign-in is disabled, and nothing here reads a slot.
PROVIDER_CONFIG_KEY = "GOOGLE_OIDC"

TX_SLOT = "oidc_tx"
LINK_SLOT = "oidc_link"
INTENT_SIGNIN = "signin"
INTENT_LINK = "link"

# Lifetimes in seconds of the monotonic clock.
SIGNIN_LIFETIME = 600
LINK_LIFETIME = 300
PENDING_LINK_LIFETIME = 180

LAYOUT_VERSION = 1
TOKEN_LENGTH = 43  # secrets.token_urlsafe(32)
VERIFIER_LENGTH = 86  # secrets.token_urlsafe(64): a valid PKCE verifier
INSTANCE_LENGTH = 32
DIGEST_LENGTH = 64
MAX_NEXT_LENGTH = 512
MAX_QUERY_BYTES = 2048
MAX_SUBJECT_LENGTH = 255
MAX_EMAIL_LENGTH = 254
MAX_NAME_LENGTH = 80
# A session cookie value above this is refused: browsers accept about 4096 bytes.
COOKIE_BUDGET = 3000
# Exclusive bound of an expiry: the consumption storage's own bound for a time.
_TIME_LIMIT = 2**62
_ROW_ID_LIMIT = 2**63

_URLSAFE = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
_HEX = frozenset("0123456789abcdef")
_TX_KEYS = frozenset({"v", "intent", "state", "nonce", "cv", "next", "inst", "exp"})
_BINDING_KEYS = frozenset({"uid", "skd", "siat"})
_PENDING_KEYS = frozenset({"v", "id", "inst", "exp", "sub", "email", "name"}) | _BINDING_KEYS

# The running service instance. A transaction or a pending link is accepted only by a
# process whose value equals its ``inst``: after a restart, a reboot or a restore no
# process has the old value, so everything outstanding ends.
INSTANCE = secrets.token_hex(16)


def monotonic_seconds() -> int:
    """The one clock of this flow: expiry, acceptance and the consumption records."""
    return int(time.monotonic())


def provider_enabled() -> bool:
    return current_app.config.get(PROVIDER_CONFIG_KEY) is not None


# --------------------------------------------------------------------------- shapes


def _is_token(value, length: int) -> bool:
    return isinstance(value, str) and len(value) == length and all(ch in _URLSAFE for ch in value)


def _is_hex(value, length: int) -> bool:
    return isinstance(value, str) and len(value) == length and all(ch in _HEX for ch in value)


def _is_time(value) -> bool:
    # ``type(...) is int``: a bool is an int, and True must not pass as second 1.
    return type(value) is int and 0 <= value < _TIME_LIMIT


def _is_binding(obj: dict) -> bool:
    return (
        type(obj.get("uid")) is int
        and 0 < obj["uid"] < _ROW_ID_LIMIT
        and _is_hex(obj.get("skd"), DIGEST_LENGTH)
        and type(obj.get("siat")) is int
        and obj["siat"] > 0
    )


def is_transaction(obj) -> bool:
    """A well-formed version-1 transaction (design 2.1); anything else is malformed."""
    if type(obj) is not dict:
        return False
    intent = obj.get("intent")
    if intent not in (INTENT_SIGNIN, INTENT_LINK):
        return False
    if set(obj) != (_TX_KEYS | _BINDING_KEYS if intent == INTENT_LINK else _TX_KEYS):
        return False
    if type(obj["v"]) is not int or obj["v"] != LAYOUT_VERSION:
        return False
    if not (_is_token(obj["state"], TOKEN_LENGTH) and _is_token(obj["nonce"], TOKEN_LENGTH)
            and _is_token(obj["cv"], VERIFIER_LENGTH)):
        return False
    if not isinstance(obj["next"], str) or len(obj["next"]) > MAX_NEXT_LENGTH:
        return False
    if not _is_hex(obj["inst"], INSTANCE_LENGTH) or not _is_time(obj["exp"]):
        return False
    if intent == INTENT_LINK:
        return obj["next"] == "" and _is_binding(obj)
    return True


def is_pending_link(obj) -> bool:
    """A well-formed version-1 pending link (design 2.2); anything else is malformed."""
    if type(obj) is not dict or set(obj) != _PENDING_KEYS:
        return False
    if type(obj["v"]) is not int or obj["v"] != LAYOUT_VERSION:
        return False
    if not _is_token(obj["id"], TOKEN_LENGTH) or not _is_hex(obj["inst"], INSTANCE_LENGTH):
        return False
    if not _is_time(obj["exp"]) or not _is_binding(obj):
        return False
    subject = obj["sub"]
    if not isinstance(subject, str) or not 0 < len(subject) <= MAX_SUBJECT_LENGTH \
            or not all("!" <= ch <= "~" for ch in subject):
        return False
    return (
        isinstance(obj["email"], str) and len(obj["email"]) <= MAX_EMAIL_LENGTH
        and isinstance(obj["name"], str) and len(obj["name"]) <= MAX_NAME_LENGTH
    )


# --------------------------------------------------------------------------- creation


def _fits_the_cookie() -> bool:
    """Would the session, as it is now, fit a cookie? Measured with the application's
    own signing serializer."""
    serializer = current_app.session_interface.get_signing_serializer(current_app)
    return serializer is not None and len(serializer.dumps(dict(session))) <= COOKIE_BUDGET


def start_transaction(intent: str, *, next_target: str = "", binding: dict | None = None) -> dict | None:
    """Create a transaction and put it into the session, replacing any earlier one.

    ``intent`` is fixed here by the route and never changes afterwards. ``binding``
    (``uid``, ``skd``, ``siat``) is given for a link and for nothing else. Returns the
    transaction, or ``None`` when the session would not fit a cookie; nothing of it
    is left in the session then.
    """
    lifetime = LINK_LIFETIME if intent == INTENT_LINK else SIGNIN_LIFETIME
    transaction = {
        "v": LAYOUT_VERSION,
        "intent": intent,
        "state": secrets.token_urlsafe(32),
        "nonce": secrets.token_urlsafe(32),
        "cv": secrets.token_urlsafe(64),
        "next": next_target if intent == INTENT_SIGNIN else "",
        "inst": INSTANCE,
        "exp": monotonic_seconds() + lifetime,
    }
    if intent == INTENT_LINK:
        transaction.update({name: binding[name] for name in sorted(_BINDING_KEYS)})
    session[TX_SLOT] = transaction
    if not _fits_the_cookie():
        session.pop(TX_SLOT, None)
        return None
    return transaction


def store_pending_link(transaction: dict, *, subject: str, email: str, name: str) -> dict | None:
    """Put the pending link of a validated ``link`` callback into the session.

    The binding is copied from the transaction, which is gone from the session by
    now. Returns the pending link, or ``None`` when it would not fit a cookie.
    """
    pending = {
        "v": LAYOUT_VERSION,
        "id": secrets.token_urlsafe(32),
        "inst": INSTANCE,
        "exp": monotonic_seconds() + PENDING_LINK_LIFETIME,
        "uid": transaction["uid"],
        "skd": transaction["skd"],
        "siat": transaction["siat"],
        "sub": subject,
        "email": email,
        "name": name,
    }
    if not is_pending_link(pending):
        return None
    session[LINK_SLOT] = pending
    if not _fits_the_cookie():
        session.pop(LINK_SLOT, None)
        return None
    return pending


def live_pending_link() -> dict | None:
    """The session's pending link if it is well-formed, of this instance and, at a
    first look, not expired. The expiry that binds is the one checked under the write
    lock when the link is confirmed."""
    pending = session.get(LINK_SLOT)
    if not is_pending_link(pending) or pending["inst"] != INSTANCE:
        return None
    if monotonic_seconds() >= pending["exp"]:
        return None
    return pending


def drop_pending_link() -> None:
    if LINK_SLOT in session:
        session.pop(LINK_SLOT)


def _drop_slots() -> None:
    for slot in (TX_SLOT, LINK_SLOT):
        if slot in session:
            session.pop(slot)


# --------------------------------------------------------------------------- recording use


def begin_write(conn: sqlite3.Connection) -> int:
    """``BEGIN IMMEDIATE``, then one reading of the clock.

    The only place where a call site obtains the ``now`` it passes on: the lock comes
    first, so a caller that waited for it does not hold a stale reading. A call site
    owns a transaction from the moment this has returned, and not before.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        return monotonic_seconds()
    except BaseException:
        conn.rollback()
        raise


def record_use(conn: sqlite3.Connection, purpose: str, obj: dict, now: int) -> None:
    """Record that a transaction or a pending link was used, with its own expiry.

    The key is derived from the object's own secret value (the ``state`` of a
    transaction, the ``id`` of a pending link). The expiry is the object's, read from
    the verified cookie and passed unchanged: it is never computed at the time of use.
    """
    value = obj["state"] if purpose == oidc_consumption.PURPOSE_CALLBACK else obj["id"]
    key = oidc_consumption.consumption_key(current_app.config["SECRET_KEY"], purpose, value)
    oidc_consumption.consume(conn, key, now=now, expires_at=obj["exp"])


def rollback_own(conn: sqlite3.Connection) -> None:
    """Roll back a transaction that the calling site began itself.

    A rollback that fails is logged by its class name and changes no outcome. Whatever
    is then still open is that call site's own and goes with the request's connection.
    """
    try:
        conn.rollback()
    except Exception as exc:
        logger.warning("rollback_failed error=%s", type(exc).__name__)


# --------------------------------------------------------------------------- the callback hook

MATCHED_TRANSACTION = "oidc_matched_tx"

OUTCOME_FAILED = "failed"
OUTCOME_EXPIRED = "expired"
OUTCOME_UNAVAILABLE = "unavailable"


def callback_redirect(target: str):
    """The only kind of answer the callback gives: a redirect to a local path that
    carries no secret, never stored and never a referrer."""
    response = redirect(target)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def outcome_target(intent: str, code: str) -> str:
    """Where a callback that did not succeed leaves the address with the code and state."""
    page = "/account" if intent == INTENT_LINK else "/login"
    return f"{page}?google={code}"


def _submitted_state() -> str | None:
    if len(request.query_string) > MAX_QUERY_BYTES:
        return None
    values = request.args.getlist("state")
    if len(values) != 1 or not _is_token(values[0], TOKEN_LENGTH):
        return None
    return values[0]


def verify_oauth_state():
    """Before-request hook: match and spend the transaction of an OAuth callback.

    It acts only for a view that declares ``OAUTH_STATE``, and runs before the
    principal is loaded. A request that does not match is not that transaction's
    callback: it is answered at once, writes nothing (no cookie, no row, no outbound
    request) and leaves the stored transaction alone, since clearing it would let any
    site cancel a sign-in in progress. A request that matches spends the transaction
    in its own committed write, before any other parameter is looked at.
    """
    if request.routing_exception is not None:
        return None
    view = current_app.view_functions.get(request.endpoint or "")
    if view is None or integrity_policy_for_view(view) != OAUTH_STATE:
        return None
    if request.method != "GET":
        # HEAD and OPTIONS resolve to a GET view. Neither may run it or redeem a code.
        raise MethodNotAllowed(valid_methods=["GET"])
    if not provider_enabled():
        return None  # the view answers 404

    state = _submitted_state()
    transaction = session.get(TX_SLOT) if state is not None else None
    if state is None or not is_transaction(transaction) \
            or not compare_digest(transaction["state"].encode("ascii"), state.encode("ascii")):
        return callback_redirect(outcome_target(INTENT_SIGNIN, OUTCOME_FAILED))

    intent = transaction["intent"]
    if transaction["inst"] != INSTANCE:
        # No instance, now or later, accepts it. Nothing is recorded.
        session.pop(TX_SLOT)
        return callback_redirect(outcome_target(intent, OUTCOME_EXPIRED))

    # Match. The slot goes first; then the use is recorded and committed.
    session.pop(TX_SLOT)
    outcome = _spend(transaction)
    if outcome is not None:
        return callback_redirect(outcome_target(intent, outcome))
    setattr(g, MATCHED_TRANSACTION, transaction)
    return None


def _spend(transaction: dict) -> str | None:
    """Record the use of a matched transaction. ``None``: accepted.

    Accepted only if the lock, the clock, the record and the commit all returned
    normally. Every other result ends the request with no outbound call and no account
    or session change. No error is read as "not used yet".
    """
    conn = None
    began = False
    outcome = None
    try:
        conn = get_request_db()
        now = begin_write(conn)
        began = True
        record_use(conn, oidc_consumption.PURPOSE_CALLBACK, transaction, now)
        conn.commit()
        return None
    except oidc_consumption.AlreadyConsumed:
        outcome = OUTCOME_FAILED
    except oidc_consumption.Expired:
        # Nothing was recorded, and nothing needs to be: it is expired at every later
        # moment too.
        outcome = OUTCOME_EXPIRED
    except sqlite3.Error as exc:
        outcome = OUTCOME_UNAVAILABLE
        logger.warning("callback_record_failed intent=%s error=%s", transaction["intent"], type(exc).__name__)
    except Exception as exc:
        outcome = OUTCOME_FAILED
        logger.warning("callback_record_failed intent=%s error=%s", transaction["intent"], type(exc).__name__)
    except BaseException:
        if began:
            rollback_own(conn)
        raise
    if began:
        rollback_own(conn)
    return outcome


def matched_transaction() -> dict | None:
    """The transaction the hook matched and recorded for this request, if any."""
    return g.get(MATCHED_TRANSACTION)


# --------------------------------------------------------------------------- retiring

NONE = "none"
INVALIDATED = "invalidated"
CONSUMED = "consumed"
DEAD = "dead"
NOT_RECORDED = "not_recorded"

REASON_CALLER_TRANSACTION = "caller_transaction"


@dataclass(frozen=True)
class Retirement:
    """What a retirement achieved, for each of the two objects a session can hold.

    * ``none``: the session held no such object. That establishes nothing.
    * ``invalidated``: this call's record is committed. The object had not been used,
      and it is refused from now on in every copy of a cookie that holds it.
    * ``consumed``: a committed record existed before this call. The object was used;
      by what, and with what result, this call does not know.
    * ``dead``: it cannot be accepted any more for a reason that involves no record
      (malformed, another instance, expired). Whether it was used before is not known.
    * ``not_recorded``: live as far as this process can tell, and no record of it was
      committed. Nothing that this call did refuses it.
    """

    transaction: str = NONE
    pending: str = NONE

    @property
    def complete(self) -> bool:
        """No object was left both live and unrecorded. It does not say that linking
        was cancelled, that nothing was linked or that no other linking object exists."""
        return NOT_RECORDED not in (self.transaction, self.pending)

    @property
    def all_invalidated(self) -> bool:
        """The session held at least one linking object, and this call's own record
        ended every one it held."""
        outcomes = (self.transaction, self.pending)
        return INVALIDATED in outcomes and all(outcome in (INVALIDATED, NONE) for outcome in outcomes)


def _classify(slot: str) -> str | None:
    """An outcome that needs no database, or ``None`` for a live linking object."""
    if slot not in session:
        return NONE
    obj = session[slot]
    if slot == TX_SLOT:
        if not is_transaction(obj):
            return DEAD
        if obj["intent"] != INTENT_LINK:
            return NONE  # a sign-in is not linking: it goes with its slot, unrecorded
    elif not is_pending_link(obj):
        return DEAD
    # An object of another run is refused before the storage is asked.
    return DEAD if obj["inst"] != INSTANCE else None


def _log_not_recorded(objects: list[str], reason: str, user_id) -> None:
    logger.warning(
        "retire_not_recorded objects=%s reason=%s user=%s",
        ",".join(objects), reason, user_id if type(user_id) is int else "-",
    )


def retire_linking() -> Retirement:
    """Try to record that the linking in progress in this session has ended.

    Both slots are removed from the session on every path that returns. What that
    achieved on the server is the result, object by object, and a failure is never a
    retirement: every error leaves the live objects ``not_recorded``. Nothing is
    raised to the caller except an exception that is not an ``Exception``.

    The caller must have no pending transaction on the request's connection. If it
    has one, no statement is executed on that connection, so the caller's writes are
    exactly as they were.
    """
    if not provider_enabled():
        return Retirement()
    outcomes = {"transaction": _classify(TX_SLOT), "pending": _classify(LINK_SLOT)}
    live = [
        (name, purpose, session[slot])
        for name, slot, purpose in (
            ("transaction", TX_SLOT, oidc_consumption.PURPOSE_CALLBACK),
            ("pending", LINK_SLOT, oidc_consumption.PURPOSE_LINK_CONFIRM),
        )
        if outcomes[name] is None
    ]
    if live:
        _record_retirement(live, outcomes)
    _drop_slots()
    return Retirement(**outcomes)


def _record_retirement(live: list, outcomes: dict) -> None:
    names = [name for name, _, _ in live]
    user_id = live[0][2].get("uid")

    def not_recorded(reason: str) -> None:
        left = [name for name in names if outcomes[name] not in (DEAD, CONSUMED)]
        for name in left:
            outcomes[name] = NOT_RECORDED
        if left:
            _log_not_recorded(left, reason, user_id)

    try:
        conn = get_request_db()
        if conn.in_transaction:
            # A transaction of the caller: nothing is executed on it.
            not_recorded(REASON_CALLER_TRANSACTION)
            return
        now = begin_write(conn)
    except Exception as exc:
        # No transaction was begun here, so there is none to roll back.
        not_recorded(type(exc).__name__)
        return

    recorded = []
    try:
        for name, purpose, obj in live:
            try:
                record_use(conn, purpose, obj, now)
            except oidc_consumption.Expired:
                outcomes[name] = DEAD
            except oidc_consumption.AlreadyConsumed:
                outcomes[name] = CONSUMED
            else:
                recorded.append(name)
        conn.commit()
    except BaseException as exc:
        # Both records are in one transaction: a failure leaves neither, also the one
        # whose record had already been written.
        rollback_own(conn)
        not_recorded(type(exc).__name__)
        if not isinstance(exc, Exception):
            raise
        return
    for name in recorded:
        outcomes[name] = INVALIDATED
