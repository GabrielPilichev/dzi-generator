"""Password recovery by emailed code: the service (docs/reviews/PASSWORD_RECOVERY_DESIGN.md,
sections 1.3, 2.1 to 2.5, 3.2 to 3.6, 4, 5; scope 8.2).

Independent of Flask, ``web.app`` and the blueprints. Scope 8.2 added it with no caller;
since scope 8.3 exactly one production module calls it, the blueprint
``web.blueprints.recovery`` (the routes, the session slot, the settings and the build gate
``PRODUCTION_ACTIVATION`` below), and nothing else may (``tests/test_recovery_no_caller.py``).
This module opens no database, reads no environment and builds no mailer.

**Transactions belong to the caller**, exactly as for ``web.services.registration``:

* Every writer (``request``, ``resend``, ``redeem``, ``cancel``, ``settle_send``) must be
  called inside a transaction the caller began (``BEGIN IMMEDIATE``); with none open it
  raises ``registration.TransactionRequired`` and writes nothing. The writer takes
  SQLite's write lock itself (``registration.writer_now``) and **reads the clock once,
  after the lock is held**.
* A returned outcome is a decision inside the caller's open transaction. **Nothing is
  accepted until the caller's commit has returned**: ``ISSUED`` and ``REDEEMED`` mean
  "would be, once committed". After any exception, and after a failed commit, the caller
  rolls back and must not present a reset as made: a commit that raised has an outcome
  this module cannot establish (design 2.4).
* The caller commits after every returned outcome, refusals included, so that a counted
  attempt, an expiry or a voiding persists.
* A message is sent with **no transaction open**: commit, then :func:`dispatch` exactly
  once, then a second short transaction around :func:`settle_send`. ``dispatch`` invokes
  the mailer once per call; there is no retry, no durable send claim and no exactly-once
  claim about SMTP. Never settle after :class:`NotSendable`.
* Log lines written by a writer name a decision (``..._decided``), never a durable fact.

**One order for every writer** (design 3.5): the write lock, the clock, the reads of this
browser's own row (none or final: answered, nothing written), then the shared restore hold
(not for ``cancel``) and the clock against the shared high-water mark, and only then
anything that writes. When the hold or the clock refuses, nothing is written at all.

**Shared accounting** (RD5, RD6): every issuance is a row of ``registration_sends`` with
``recovery_id`` set and ``pending_id`` NULL, keyed with ``registration.email_hash``, so
registration's per-address caps, total budget, circuit, high-water mark, restore hold and
48-hour ledger cleanup count both purposes through registration's own code. No counter
lives here. Nothing is refunded by any outcome.

**Eligibility** (design 1.3, RD1), at every issuance and again at redemption, under the
writer lock: an active ``student`` or ``teacher`` with a password and ``email_verified_at``
set; at redemption also the same account, the same ``users.email`` and a keyed digest of
the same ``session_key``. Admins and password-less or Google-only accounts never get a
code. Every other well-formed address gets a **shadow** row whose stored code hash no
digits can match, and one neutral notice to the typed address (RD2 (a)), issued, counted,
dispatched and settled like a code. No equal provider timing is claimed (design 1.4).

**What a reset changes** (RD3, RD8 (a), RD10, RD17): one transaction checks the code, then
eligibility and the digest, then calls the unchanged ``accounts.set_password`` (scrypt only
after a correct code and a passed recheck) and closes this row. No session is issued;
``login_throttle`` is neither read nor written, and wrong codes are not counted there. No
other recovery row is read or written: the rotated session key makes every other pending
code of the account unable to reset anything (its correct code ends ``INVALIDATED``), so
another browser sees nothing change before it presents a correct code.

**Settlement** (RD7, amendment AM2) records the ledger outcome only and never changes a
recovery row: a ``not_sent`` code is not retired and its wait is not waived.

Never logged, returned in an error or put in an exception message: the address, the code,
the token, the password, any hash, the session key or its digest, the issuance identity,
the message, or whether a row is a shadow. Log lines (logger ``learnpilot.recovery``) hold
an event name and row ids; ``user=`` appears on ``redeemed_decided`` only.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Mapping

from web.services import accounts, registration
from web.services import recovery_scrub as scrub
from web.services import registration_forms as forms
from web.services.mail import Mailer, OutboundEmail, SendOutcome
from web.services.registration import Availability, Settlement, TransactionRequired
from web.services.registration_scrub import stamp as _stamp

logger = logging.getLogger("learnpilot.recovery")

# Activation gate of the build (design 7.1, scope 8.3), a constant like the three of
# registration design 7.3. While it is False a production start keeps every ``/recover...``
# route at the generic 404 whatever the mail settings hold, builds no recovery settings and
# shows no link on ``/login``. No setting and no environment variable overrides it: a deploy
# changes it, and only when Gabriel authorizes the activation.
PRODUCTION_ACTIVATION = False

# Limits (design 5.4, amendment AM1): registration's D6 values, with eight-digit codes.
CODE_DIGITS = 8
RECOVERY_LIFETIME = registration.PENDING_LIFETIME  # 30 minutes
CODE_LIFETIME = registration.CODE_LIFETIME  # 10 minutes
MAX_CODE_ATTEMPTS = registration.MAX_CODE_ATTEMPTS  # 5
MAX_SENDS = registration.MAX_SENDS  # the first and three resends
RESEND_INTERVAL = registration.RESEND_INTERVAL  # 60 seconds
CLOCK_SKEW = registration.CLOCK_SKEW
TOTAL_HOURLY_CAP = registration.TOTAL_HOURLY_CAP

ELIGIBLE_ROLES = ("student", "teacher")

KIND_CODE = registration.KIND_CODE
KIND_NOTICE = registration.KIND_NOTICE

# Proposed wording (design 2.8); RD15 keeps the exact texts open until activation.
CODE_SUBJECT = "Код за нова парола в LearnPilot"
NOTICE_SUBJECT = "Заявка за нова парола в LearnPilot"
_CODE_LEAD = "Кодът ти за нова парола е: "

__all__ = [
    "PRODUCTION_ACTIVATION", "Availability", "Settlement", "TransactionRequired", "NotSendable", "Outcome", "Result",
    "PendingView", "binding_hash", "session_key_digest", "request", "resend", "redeem", "cancel",
    "pending_view", "dispatch", "settle_send",
]

_ROW_COLUMNS = (
    "id, binding_hash, state, is_shadow, email, user_id, session_key_digest, code_hash, "
    "code_expires_at, code_send_id, code_attempts, send_count, resend_after, created_at, expires_at, "
    "closed_at"
)
_ISSUANCE_ID_LENGTH = 32  # lower-case hexadecimal: 128 random bits (migration 022)
_STAMP_FORMAT = "%Y-%m-%d %H:%M:%S"
_SETTLED_OUTCOMES = tuple(outcome.value for outcome in SendOutcome)


class NotSendable(RuntimeError):
    """``dispatch`` was asked to send what is not the committed, unsettled, current issuance
    of a pending recovery, exactly as it was issued. Nothing was sent; settle nothing."""


class Outcome(Enum):
    ISSUED = "issued"                  # a code or notice was issued; send it after the commit
    UNAVAILABLE = "unavailable"        # hold, clock, sender, circuit, total budget
    ADDRESS_CAPPED = "address_capped"
    NO_PENDING = "no_pending"          # no row for this binding; nothing written
    NOT_PENDING = "not_pending"        # the row is final; nothing written
    EXPIRED = "expired"
    TOO_EARLY = "too_early"            # resend inside the wait
    RESEND_EXHAUSTED = "resend_exhausted"
    CODE_EXPIRED = "code_expired"      # no live code; no attempt counted
    TOO_MANY = "too_many"
    WRONG_CODE = "wrong_code"
    INVALIDATED = "invalidated"        # correct code, but the account or its key changed
    REDEEMED = "redeemed"              # the password is set once the caller's commit returns
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class Result:
    """What a writer decided, inside the caller's still-open transaction.

    ``token`` (the browser binding, first issuance only), ``message`` (the code or notice
    to dispatch) and ``issuance_id`` (the random identity of the send generation) are
    secrets: left out of ``repr`` and never logged. ``kind`` is left out of ``repr`` too,
    because it says whether a row is a shadow. ``send_id`` and ``issuance_id`` together
    name one issuance; the number alone is not an identity, because SQLite reuses the
    numbers of a rolled-back transaction. ``user_id`` accompanies ``REDEEMED`` only.
    ``final_state`` accompanies ``NOT_PENDING`` only. ``reason`` says why an
    ``UNAVAILABLE`` was answered, for a log line.
    """

    outcome: Outcome
    recovery_id: int | None = None
    send_id: int | None = None
    user_id: int | None = None
    reason: Availability | None = None
    final_state: str | None = None
    kind: str | None = field(default=None, repr=False)
    issuance_id: str | None = field(default=None, repr=False, compare=False)
    token: str | None = field(default=None, repr=False, compare=False)
    message: OutboundEmail | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class PendingView:
    """What the verify page of the browser holding the binding may show: the address that
    browser typed (personal data, never logged, out of ``repr``). Nothing about the account
    or about whether the row is a shadow."""

    recovery_id: int
    email: str = field(repr=False)


# ------------------------------------------------------------------------- keyed values

def _key(secret_key) -> bytes:
    key = secret_key if isinstance(secret_key, bytes) else str(secret_key or "").encode("utf-8")
    if not key:
        raise ValueError("Липсва ключ.")
    return key


def _keyed(secret_key, label: bytes, *parts: bytes) -> str:
    return hmac.new(_key(secret_key), label + b"\0" + b"\0".join(parts), hashlib.sha256).hexdigest()


def binding_hash(secret_key, token: str) -> str:
    """The stored form of a recovery browser-binding token (design 2.2, 3.2)."""
    if not isinstance(token, str):
        raise ValueError("Невалиден маркер.")
    return _keyed(secret_key, b"recovery-binding", token.encode("utf-8", errors="surrogatepass"))


def session_key_digest(secret_key, session_key: str) -> str:
    """The keyed digest of ``users.session_key`` a live code is bound to (design 1.3, 3.2).
    Labelled for recovery: it equals no other purpose's digest of the same key."""
    if not isinstance(session_key, str) or not session_key:
        raise ValueError("Невалиден ключ на сесия.")
    return _keyed(secret_key, b"recovery-session-key", session_key.encode("utf-8", errors="surrogatepass"))


def _code_hash(secret_key, recovery_id: int, secret: bytes) -> str:
    return _keyed(secret_key, b"recovery-code", str(recovery_id).encode("ascii"), secret)


def _is_hash(value) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(ch in "0123456789abcdef" for ch in value)


def _new_code() -> str:
    return f"{secrets.randbelow(10**CODE_DIGITS):0{CODE_DIGITS}d}"


def _new_issuance_id() -> str:
    return secrets.token_hex(_ISSUANCE_ID_LENGTH // 2)


def _is_issuance_id(value) -> bool:
    return (isinstance(value, str) and len(value) == _ISSUANCE_ID_LENGTH
            and all(ch in "0123456789abcdef" for ch in value))


def _submitted_code(value) -> str | None:
    """Exactly eight ASCII digits once leading and trailing whitespace is trimmed, or
    ``None`` (a wrong code). Inner whitespace and other digits are not repaired."""
    if not isinstance(value, str):
        return None
    code = value.strip()
    return code if len(code) == CODE_DIGITS and all(ch in "0123456789" for ch in code) else None


# ------------------------------------------------------------------------- time

def _moment(stored) -> datetime | None:
    if not isinstance(stored, str):
        return None
    try:
        moment = datetime.strptime(stored, _STAMP_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return moment if _stamp(moment) == stored else None


def _checked_now(now) -> datetime:
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("Невалидно време.")
    return now.astimezone(timezone.utc).replace(microsecond=0)


def _checked_total_cap(total_cap) -> int:
    if type(total_cap) is not int or not 1 <= total_cap <= TOTAL_HOURLY_CAP:
        raise ValueError("Невалиден общ лимит.")
    return total_cap


# ------------------------------------------------------------------------- reads

def _one(cursor: sqlite3.Cursor) -> dict | None:
    row = cursor.fetchone()
    if row is None:
        return None
    return dict(zip([col[0] for col in cursor.description], tuple(row)))


def _row(conn: sqlite3.Connection, binding) -> dict | None:
    if not _is_hash(binding):
        return None
    return _one(conn.execute(f"SELECT {_ROW_COLUMNS} FROM password_recoveries WHERE binding_hash = ?",
                             (binding,)))


def _is_expired(row: Mapping, now: datetime) -> bool:
    # Past its time, or dated in the future beyond the skew; the second case is reached only
    # once the clock is not behind the mark, which every caller has decided first.
    return row["expires_at"] <= _stamp(now) or row["created_at"] > _stamp(now + CLOCK_SKEW)


def _eligible(candidate: Mapping | None) -> bool:
    """Design 1.3, items 1 to 4 (RD1: only a verified ``users.email``)."""
    return (
        candidate is not None
        and candidate["is_active"] == 1
        and candidate["role"] in ELIGIBLE_ROLES
        and bool(candidate["has_password"])
        and candidate["email_verified_at"] is not None
        and isinstance(candidate["email"], str)
        and isinstance(candidate["session_key"], str)
        and bool(candidate["session_key"])
    )


def _code_is_live(conn: sqlite3.Connection, row: Mapping, now: datetime) -> bool:
    """Whether the row's code may be tried now, by the ledger row that issued it (design 3.4).

    The ledger row of ``code_send_id`` must exist, belong to this recovery (``recovery_id``
    set to it, ``pending_id`` NULL), be of the kind the row's ``is_shadow`` implies, carry a
    stamp in this module's layout, and give exactly the stored ``code_expires_at``.
    Anything else is logged and is not a live code (fail closed), so a ledger row removed
    by ``clear-registration-limit`` kills the code. Live: issued no more than the skew
    ahead of ``now``, and ``now`` before the expiry. Never reads ``users``.
    """
    send = conn.execute(
        "SELECT recovery_id, pending_id, kind, attempted_at FROM registration_sends WHERE id = ?",
        (row["code_send_id"],),
    ).fetchone()
    issued_at = _moment(send[3]) if send is not None else None
    if (
        issued_at is None
        or row["code_hash"] is None
        or send[0] != row["id"]
        or send[1] is not None
        or send[2] != (KIND_NOTICE if row["is_shadow"] else KIND_CODE)
        or row["code_expires_at"] != min(_stamp(issued_at + CODE_LIFETIME), row["expires_at"])
    ):
        logger.warning("code_evidence_invalid recovery=%d", row["id"])
        return False
    return send[3] <= _stamp(now + CLOCK_SKEW) and _stamp(now) < row["code_expires_at"]


# ------------------------------------------------------------------------- messages

def _code_message(to: str, code: str) -> OutboundEmail:
    return OutboundEmail(to=to, subject=CODE_SUBJECT, text=(
        f"{_CODE_LEAD}{code}\n"
        "Валиден е 10 минути. Въведи го в страницата, от която поиска нова парола.\n"
        "Ако не си поискал(а) нова парола, не прави нищо: паролата ти остава същата.\n"
        "Не споделяй този код с никого.\n"
    ))


def _notice_message(to: str) -> OutboundEmail:
    return OutboundEmail(to=to, subject=NOTICE_SUBJECT, text=(
        "Някой поиска нова парола в LearnPilot за този имейл. За този имейл не може да се зададе "
        "нова парола с код. Ако имаш профил в LearnPilot, обърни се към администратора на сайта. "
        "Ако не си поискал(а) това, не прави нищо.\n"
    ))


# ------------------------------------------------------------------------- writes

def _close(conn: sqlite3.Connection, recovery_id: int, state: str, now: datetime) -> None:
    """Leave ``pending`` for a final state and scrub, in one statement (design 2.1)."""
    cursor = conn.execute(
        f"UPDATE password_recoveries SET state = ?, closed_at = ?, {scrub.SCRUB_ASSIGNMENTS} "
        "WHERE id = ? AND state = 'pending'",
        (state, _stamp(now), recovery_id),
    )
    if cursor.rowcount != 1:
        raise RuntimeError("password recovery changed state unexpectedly")


def _issue(conn: sqlite3.Connection, *, recovery_id: int, email: str, expires_at: str, secret_key,
           now: datetime) -> tuple[int, str, str, OutboundEmail]:
    """One issuance (design 3.3): eligibility decided now, then the ledger row with a new
    identity, then the live code and its binding in one statement, then the shared mark.
    Real and shadow rows run the same statements. Returns generation, identity, kind and
    message."""
    candidate = accounts.recovery_candidate(conn, email=email)
    if _eligible(candidate):
        code = _new_code()
        user_id, digest = candidate["id"], session_key_digest(secret_key, candidate["session_key"])
        kind, message = KIND_CODE, _code_message(candidate["email"], code)
        stored = _code_hash(secret_key, recovery_id, code.encode("ascii"))
    else:
        # A hash no eight digits can produce; the typed address gets the notice (RD2 (a)).
        user_id, digest = None, None
        kind, message = KIND_NOTICE, _notice_message(email)
        stored = _code_hash(secret_key, recovery_id, secrets.token_bytes(32))
    issuance_id = _new_issuance_id()
    send_id = conn.execute(
        "INSERT INTO registration_sends (recovery_id, email_hash, kind, attempted_at, issuance_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (recovery_id, registration.email_hash(secret_key, email), kind, _stamp(now), issuance_id),
    ).lastrowid
    cursor = conn.execute(
        "UPDATE password_recoveries SET is_shadow = ?, user_id = ?, session_key_digest = ?, code_hash = ?, "
        "code_expires_at = ?, code_send_id = ?, code_attempts = 0, send_count = send_count + 1, "
        "resend_after = ? WHERE id = ? AND state = 'pending'",
        (0 if user_id is not None else 1, user_id, digest, stored,
         min(_stamp(now + CODE_LIFETIME), expires_at), send_id, _stamp(now + RESEND_INTERVAL), recovery_id),
    )
    if cursor.rowcount != 1:
        raise RuntimeError("password recovery changed state unexpectedly")
    registration.raise_mark(conn, now)
    return send_id, issuance_id, kind, message


def request(
    conn: sqlite3.Connection,
    *,
    email: str,
    previous_binding: str | None,
    secret_key,
    clock: Callable[[], datetime],
    sender_busy: bool,
    total_cap: int = TOTAL_HOURLY_CAP,
) -> Result:
    """Create this browser's recovery row and issue its first code, or a shadow's notice.

    ``email`` must already be the stored form ``registration_forms.public_email`` returns
    (``ValueError`` otherwise, before anything is read). ``previous_binding`` is the binding
    hash of the token this browser's session held, or ``None``.

    Order, under the lock (design 2.3, 3.5): the shared hold and clock, sender, circuit and
    total budget (``registration.availability``); the shared address caps; and only then
    the inline recovery cleanup (recovery rows only), the voiding of this browser's
    previous row, the row, eligibility, the ledger row, the code and the mark. A refusal
    (``UNAVAILABLE``, ``ADDRESS_CAPPED``) writes nothing.

    ``ISSUED`` carries ``token`` (for the session slot), ``recovery_id``, ``send_id``,
    ``issuance_id``, ``kind`` and ``message``. Commit, then :func:`dispatch`, then settle.
    """
    try:
        if not isinstance(email, str) or forms.public_email(email) != email:
            raise ValueError
    except ValueError:
        raise ValueError("Невалиден имейл.") from None
    total_cap = _checked_total_cap(total_cap)
    address = registration.email_hash(secret_key, email)
    _notice_message(email)  # refuses an address no message can carry, before any write
    now = registration.writer_now(conn, clock)

    admission = registration.availability(conn, now, sender_busy=bool(sender_busy), total_cap=total_cap)
    if admission is not Availability.AVAILABLE:
        return Result(Outcome.UNAVAILABLE, reason=admission)
    if registration.address_capped(conn, address, now):
        return Result(Outcome.ADDRESS_CAPPED)

    scrub.cleanup(conn, now)
    previous = _row(conn, previous_binding)
    if previous is not None and previous["state"] == "pending":
        _close(conn, previous["id"], "void", now)

    token = secrets.token_urlsafe(32)
    expires_at = _stamp(now + RECOVERY_LIFETIME)
    # Inserted as a row with no live code; the issuance below decides real or shadow.
    recovery_id = conn.execute(
        "INSERT INTO password_recoveries (binding_hash, state, is_shadow, email, created_at, expires_at) "
        "VALUES (?, 'pending', 1, ?, ?, ?)",
        (binding_hash(secret_key, token), email, _stamp(now), expires_at),
    ).lastrowid
    send_id, issuance_id, kind, message = _issue(
        conn, recovery_id=recovery_id, email=email, expires_at=expires_at, secret_key=secret_key, now=now)
    logger.info("request_decided recovery=%d send=%d", recovery_id, send_id)
    return Result(Outcome.ISSUED, recovery_id=recovery_id, send_id=send_id, kind=kind,
                  issuance_id=issuance_id, token=token, message=message)


def _usable_row(conn: sqlite3.Connection, binding, now: datetime):
    """Reads first (none or final: answered, nothing written), then the hold and the clock,
    then expiry. Returns ``(row, None)`` for a pending, unexpired row, else ``(None, Result)``."""
    row = _row(conn, binding)
    if row is None:
        return None, Result(Outcome.NO_PENDING)
    if row["state"] != "pending":
        return None, Result(Outcome.NOT_PENDING, recovery_id=row["id"], final_state=row["state"])
    blocked = registration.control_blocked(conn, now)
    if blocked is not None:
        return None, Result(Outcome.UNAVAILABLE, recovery_id=row["id"], reason=blocked)
    if _is_expired(row, now):
        _close(conn, row["id"], "expired", now)
        logger.info("expired_decided recovery=%d", row["id"])
        return None, Result(Outcome.EXPIRED, recovery_id=row["id"])
    return row, None


def resend(
    conn: sqlite3.Connection,
    binding: str,
    *,
    secret_key,
    clock: Callable[[], datetime],
    sender_busy: bool,
    total_cap: int = TOTAL_HOURLY_CAP,
) -> Result:
    """Replace this browser's code (or repeat a shadow's notice), deciding eligibility again.

    Order (design 2.3, 3.5): the row of the binding; the hold and the clock; expiry; sender,
    circuit and total budget; at most four sends; 60 seconds after the previous issuance
    (never waived, RD7); the shared address caps; then eligibility is decided afresh (RD11)
    and the issuance replaces the code, its attempts and its binding: an eligible account
    gets a code bound to its current id and session-key digest, anything else the shadow
    treatment. ``ISSUED`` carries no token.
    """
    total_cap = _checked_total_cap(total_cap)
    now = registration.writer_now(conn, clock)
    row, refusal = _usable_row(conn, binding, now)
    if refusal is not None:
        return refusal
    admission = registration.availability(conn, now, sender_busy=bool(sender_busy), total_cap=total_cap)
    if admission is not Availability.AVAILABLE:
        return Result(Outcome.UNAVAILABLE, recovery_id=row["id"], reason=admission)
    if row["send_count"] >= MAX_SENDS:
        return Result(Outcome.RESEND_EXHAUSTED, recovery_id=row["id"])
    if row["resend_after"] is not None and row["resend_after"] > _stamp(now):
        return Result(Outcome.TOO_EARLY, recovery_id=row["id"])
    if registration.address_capped(conn, registration.email_hash(secret_key, row["email"]), now):
        return Result(Outcome.ADDRESS_CAPPED, recovery_id=row["id"])
    send_id, issuance_id, kind, message = _issue(
        conn, recovery_id=row["id"], email=row["email"], expires_at=row["expires_at"],
        secret_key=secret_key, now=now)
    logger.info("resend_decided recovery=%d send=%d", row["id"], send_id)
    return Result(Outcome.ISSUED, recovery_id=row["id"], send_id=send_id, kind=kind,
                  issuance_id=issuance_id, message=message)


def redeem(
    conn: sqlite3.Connection,
    binding: str,
    code,
    password: str,
    *,
    secret_key,
    clock: Callable[[], datetime],
) -> Result:
    """Check the code and, when it is right and the account still may recover, set the
    new password: one step, in the caller's one transaction (design 2.4, RD3).

    ``password`` is the exact submitted string, never stripped or normalised; the caller
    has already applied ``accounts.password_problem(password, view.email)`` and the
    confirmation (a password problem here is ``ValueError`` before anything is read).

    Steps, in order: (1) no row: ``NO_PENDING``; (2) final row: ``NOT_PENDING``, nothing
    written; (3) the shared hold or the clock behind the mark: ``UNAVAILABLE``, nothing
    written; (4) past its time: expired and scrubbed, ``EXPIRED``; (5) no live code:
    ``CODE_EXPIRED``, no attempt counted; (6) five attempts used: ``TOO_MANY``; (7) the
    attempt is counted; (8) the code compared (trimmed at the ends, then exactly eight
    ASCII digits; a shadow never matches): ``WRONG_CODE``; (9) the code is cleared; (10)
    eligibility and the binding to the same account, ``users.email`` and session-key
    digest, read now: any failure voids and scrubs the row, ``INVALIDATED``; (11)
    ``accounts.set_password``, unchanged (scrypt, ``password_changed_at``, rotated key);
    (12) the row ``redeemed`` and scrubbed: ``REDEEMED`` with ``user_id``.

    Steps 1 to 9 read neither ``users`` nor any other recovery row, and run the same
    statements for a real and a shadow row. No other recovery row, no session and no
    ``login_throttle`` row is read or written. Every exception propagates (an error raised
    by ``set_password`` included); the caller rolls back, which leaves the code and its
    attempt count as they were. ``REDEEMED`` is a decision: the password is set only once
    the caller's commit has returned.
    """
    if accounts.password_problem(password) is not None:
        raise ValueError("Невалидна парола.")
    now = registration.writer_now(conn, clock)
    row, refusal = _usable_row(conn, binding, now)
    if refusal is not None:
        return refusal
    recovery_id = row["id"]
    if not _code_is_live(conn, row, now):
        return Result(Outcome.CODE_EXPIRED, recovery_id=recovery_id)
    if row["code_attempts"] >= MAX_CODE_ATTEMPTS:
        return Result(Outcome.TOO_MANY, recovery_id=recovery_id)
    conn.execute("UPDATE password_recoveries SET code_attempts = code_attempts + 1 WHERE id = ?", (recovery_id,))
    submitted = _submitted_code(code)
    expected = _code_hash(secret_key, recovery_id, (submitted or "").encode("ascii"))
    if not hmac.compare_digest(expected, row["code_hash"]) or submitted is None:
        logger.info("wrong_code_decided recovery=%d", recovery_id)
        return Result(Outcome.WRONG_CODE, recovery_id=recovery_id)

    # Single use: the code is gone before anything else is decided.
    conn.execute("UPDATE password_recoveries SET code_hash = NULL, code_expires_at = NULL WHERE id = ?",
                 (recovery_id,))
    candidate = accounts.recovery_candidate(conn, user_id=row["user_id"]) if row["user_id"] is not None else None
    if (
        row["is_shadow"]
        or not _eligible(candidate)
        or candidate["id"] != row["user_id"]
        or candidate["email"] != row["email"]
        or row["session_key_digest"] is None
        or not hmac.compare_digest(session_key_digest(secret_key, candidate["session_key"]),
                                   row["session_key_digest"])
    ):
        _close(conn, recovery_id, "void", now)
        logger.info("invalidated_decided recovery=%d", recovery_id)
        return Result(Outcome.INVALIDATED, recovery_id=recovery_id)
    accounts.set_password(conn, row["user_id"], password)
    _close(conn, recovery_id, "redeemed", now)
    logger.info("redeemed_decided recovery=%d user=%d", recovery_id, row["user_id"])
    return Result(Outcome.REDEEMED, recovery_id=recovery_id, user_id=row["user_id"])


def cancel(conn: sqlite3.Connection, binding: str, *, clock: Callable[[], datetime]) -> Result:
    """Void and scrub this browser's pending recovery (design 2.5).

    A final row is left as it is (``NOT_PENDING``), an absent one is ``NO_PENDING``; the
    caller answers all three alike. A restore hold does **not** refuse (RD12): the hold and
    the mark are left exactly as they were. A clock behind the mark refuses
    (``UNAVAILABLE``), with nothing voided. Reads neither ``users`` nor another row.
    """
    now = registration.writer_now(conn, clock)
    row = _row(conn, binding)
    if row is None:
        return Result(Outcome.NO_PENDING)
    if row["state"] != "pending":
        return Result(Outcome.NOT_PENDING, recovery_id=row["id"], final_state=row["state"])
    blocked = registration.control_blocked(conn, now, hold=False)
    if blocked is not None:
        return Result(Outcome.UNAVAILABLE, recovery_id=row["id"], reason=blocked)
    _close(conn, row["id"], "void", now)
    logger.info("cancel_decided recovery=%d", row["id"])
    return Result(Outcome.CANCELLED, recovery_id=row["id"])


# ------------------------------------------------------------------------- the verify page

def _clock_behind(conn: sqlite3.Connection, now: datetime) -> bool:
    # The shared mark, read without the hold and without a log line (a GET logs nothing).
    control = conn.execute("SELECT high_water_at FROM registration_control WHERE id = 1").fetchone()
    return control is not None and _stamp(now + CLOCK_SKEW) < control[0]


def pending_view(conn: sqlite3.Connection, binding, now: datetime) -> PendingView | None:
    """This browser's recovery as its verify page shows it, or ``None`` when there is none
    it could still complete: no row, a final row, or a pending row past its time.

    **Reads only**, and never ``users``: what another browser did to the account changes
    nothing here (RD17, design 1.4). Expiry is evaluated, written only by the POSTs and the
    cleanup. While the clock is behind the mark the row's times are not judged (the POST
    answers "unavailable"). Nothing is logged.
    """
    now = _checked_now(now)
    row = _row(conn, binding)
    if row is None or row["state"] != "pending" or row["email"] is None:
        return None
    if not _clock_behind(conn, now) and _is_expired(row, now):
        return None
    return PendingView(recovery_id=row["id"], email=row["email"])


# ------------------------------------------------------------------------- delivery

def _is_committed_issuance(conn: sqlite3.Connection, issued: Result, secret_key) -> bool:
    """Whether ``issued`` is, field for field, the issuance the committed rows describe."""
    found = conn.execute(
        "SELECT s.issuance_id, s.kind, s.email_hash, r.is_shadow, r.email, r.user_id, r.code_hash, "
        "r.binding_hash FROM registration_sends AS s JOIN password_recoveries AS r ON r.id = s.recovery_id "
        "WHERE s.id = ? AND s.recovery_id = ? AND s.pending_id IS NULL AND s.outcome = 'started' "
        "AND r.state = 'pending' AND r.code_send_id = s.id",
        (issued.send_id, issued.recovery_id),
    ).fetchone()
    if found is None:
        return False
    identity, kind, address, is_shadow, email, user_id, stored, binding = found
    # The same read for both kinds of row; only a real row's result is used (A4 at send time).
    account = conn.execute("SELECT email FROM users WHERE id = ?", (user_id,)).fetchone()
    message = issued.message
    if (
        identity is None  # a ledger row older than migration 022: never sent
        or not hmac.compare_digest(identity, issued.issuance_id)
        or issued.kind != kind
        or kind != (KIND_NOTICE if is_shadow else KIND_CODE)
        or stored is None
        or email is None
        or not hmac.compare_digest(address, registration.email_hash(secret_key, email))
    ):
        return False
    if kind == KIND_CODE and (user_id is None or account is None or account[0] != email):
        return False
    if issued.token is not None and not hmac.compare_digest(binding, binding_hash(secret_key, issued.token)):
        return False
    if kind == KIND_NOTICE:
        return message == _notice_message(email)
    code = message.text[len(_CODE_LEAD):len(_CODE_LEAD) + CODE_DIGITS]
    return (
        _submitted_code(code) == code
        and message == _code_message(email, code)
        and hmac.compare_digest(stored, _code_hash(secret_key, issued.recovery_id, code.encode("ascii")))
    )


def dispatch(conn: sqlite3.Connection, mailer: Mailer, issued: Result, *, secret_key) -> SendOutcome:
    """Hand the message of a **committed** recovery issuance to ``mailer``: one call to
    ``mailer.send`` per call of this function (design 3.6).

    Refuses (:class:`NotSendable`, nothing sent) while ``conn`` has a transaction open, and
    unless all of this holds, read from the database and recomputed with ``secret_key``:
    the ledger row of ``send_id`` holds ``issued.issuance_id`` (numbers alone are reused
    after a rollback), names ``issued.recovery_id`` with ``pending_id`` NULL, is still
    ``started`` and of ``issued.kind``; the recovery is ``pending`` and ``send_id`` is its
    current generation; the row's address hashes to the ledger's address key; for a code,
    the account the code is bound to still has ``users.email`` equal to that address; the
    token, when the result carries one, hashes to the binding; and the whole message equals
    the one this module builds for that address, with the eight digits hashing to the
    stored code hash. A registration result, ledger row or purpose-less row is never sent.

    The read is not a claim: until settled, a repeated call sends again. The caller calls
    this once per committed issuance and never retries. An exception escaping the mailer
    is ``UNKNOWN``, logged by class name; ``KeyboardInterrupt`` and ``SystemExit``
    propagate. The outcome is settled with :func:`settle_send` in a transaction of its own.
    """
    if (not isinstance(issued, Result) or issued.outcome is not Outcome.ISSUED
            or not isinstance(issued.message, OutboundEmail)
            or type(issued.send_id) is not int or type(issued.recovery_id) is not int
            or issued.kind not in (KIND_CODE, KIND_NOTICE)
            or not _is_issuance_id(issued.issuance_id)):
        raise NotSendable("not a recovery issuance")
    _key(secret_key)
    if conn.in_transaction:
        raise NotSendable("a transaction is open: commit before sending")
    committed = _is_committed_issuance(conn, issued, secret_key)
    if conn.in_transaction:
        raise NotSendable("a transaction is open: commit before sending")
    if not committed:
        logger.warning("dispatch_refused recovery=%d send=%d", issued.recovery_id, issued.send_id)
        raise NotSendable("not the committed, unsettled, current issuance")
    logger.info("send_started recovery=%d send=%d", issued.recovery_id, issued.send_id)
    try:
        outcome = mailer.send(issued.message)
    except Exception as exc:
        logger.warning("dispatch_failed error=%s", type(exc).__name__)
        return SendOutcome.UNKNOWN
    return outcome if isinstance(outcome, SendOutcome) else SendOutcome.UNKNOWN


def settle_send(conn: sqlite3.Connection, send_id: int, issuance_id: str, outcome, *,
                clock: Callable[[], datetime]) -> Settlement:
    """Record what became of one recovery issuance, named by its generation number **and**
    identity (design 3.6). Changes the ledger row only (RD7, amendment AM2).

    * ``NOT_THIS_ISSUANCE``: no ledger row has this number with ``recovery_id`` set, or its
      identity is another one, or it has none. Nothing is changed. A registration or a
      purpose-less ledger row is never settled here.
    * ``ALREADY_SETTLED``: the row is no longer ``started``; the first settlement wins.
    * ``SETTLED``: the outcome and the time were recorded by this call.

    **It never changes a recovery row**, for any outcome: a ``not_sent`` code is not
    retired, its expiry, attempts and the 60-second wait stay, and nothing the browser can
    see changes. A late settlement of an older generation settles its own ledger row and
    nothing else. No hold or clock decides a settlement (as for registration). ``ValueError``
    before any read for an outcome that is not a settlement, a number that is not an
    ``int`` or a malformed identity. Never call this after :class:`NotSendable`.
    """
    value = outcome.value if isinstance(outcome, SendOutcome) else outcome
    if value not in _SETTLED_OUTCOMES:
        raise ValueError("Невалиден резултат от изпращане.")
    if type(send_id) is not int or not _is_issuance_id(issuance_id):
        raise ValueError("Невалидно изпращане.")
    now = registration.writer_now(conn, clock)
    found = conn.execute(
        "SELECT issuance_id, outcome FROM registration_sends WHERE id = ? AND recovery_id IS NOT NULL",
        (send_id,),
    ).fetchone()
    if found is None or found[0] is None or not hmac.compare_digest(found[0], issuance_id):
        logger.warning("settlement_refused send=%d", send_id)
        return Settlement.NOT_THIS_ISSUANCE
    if found[1] != "started":
        return Settlement.ALREADY_SETTLED
    settled = conn.execute(
        "UPDATE registration_sends SET outcome = ?, settled_at = ? "
        "WHERE id = ? AND issuance_id = ? AND outcome = 'started' AND recovery_id IS NOT NULL",
        (value, _stamp(now), send_id, issuance_id),
    ).rowcount
    if settled != 1:
        raise RuntimeError("send ledger changed unexpectedly")
    logger.info("settlement_decided send=%d outcome=%s", send_id, value)
    return Settlement.SETTLED
