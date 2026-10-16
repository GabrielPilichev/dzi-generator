"""Pending registrations, verification codes, the send ledger and student activation
(docs/reviews/EMAIL_REGISTRATION_DESIGN.md, sections 2, 3, 4.3, 6.3 to 6.5; PR 3).

Independent of Flask, ``web.app`` and the blueprints. Its callers are the
``registration`` blueprint (PR 4) and the operator commands of ``src.manage_accounts``;
the two activation gates of the build are the constants below. This module opens no
database, reads no environment and builds no mailer.

**Transactions belong to the caller.** No function commits or rolls back.

* Every writer (``create_pending``, ``resend``, ``verify``, ``cancel``,
  ``settle_send``, ``cleanup``) must be called inside a transaction the caller began
  (``BEGIN IMMEDIATE``); with none open it raises :class:`TransactionRequired` and
  writes nothing. The writer then takes SQLite's write lock itself, with a statement
  that changes no row, so the lock is held whichever way the transaction was begun.
* **Time is sampled under that lock.** A writer takes ``clock``, a callable returning
  the timezone-aware UTC wall clock, and calls it once, after the lock is held. A
  reading taken earlier in the request (before hashing, or while waiting for the lock)
  can therefore never stamp an issuance (design 3.4).
* A returned outcome is a decision inside the caller's open transaction. **Nothing is
  accepted until the caller's commit has returned**: ``ISSUED`` and ``ACTIVATED`` mean
  "would be, once committed". After any exception, and after a failed commit, the
  caller rolls back; the registration is then exactly as it was before the call.
* The caller commits after every returned outcome, refusals included, so that a
  counted attempt, an expiry or a voiding persists.
* A message is sent with **no transaction open**: commit, then :func:`dispatch`, then
  a second short transaction around :func:`settle_send`. ``dispatch`` refuses to send
  while the connection has a transaction open, and unless the result it is given is
  the issuance the committed rows describe: its random issuance identity, generation,
  registration, kind, recipient and message. ``dispatch`` invokes the mailer once
  **per call**, not once per generation: until the generation is settled, a second or
  a concurrent ``dispatch`` of the same issuance sends again. One call per committed
  issuance is the caller's obligation (approved: caller sequencing). There is no
  retry, no durable send claim and no exactly-once claim about SMTP.
* **Never settle after** :class:`NotSendable`. Settlement needs the issuance identity
  as well as the generation number, so a result kept from a rolled-back transaction
  settles nothing even when SQLite reused its numbers.
* Log lines written by a writer are written **before the caller's commit**. They name
  a decision (``activation_decided``), never a durable fact: the commit may still fail.

**Clock first.** In every writer but ``settle_send`` the restore hold (not for
``cancel`` and ``cleanup``, which only remove data) and the clock against the
high-water mark are decided before any expiry, scrub, voiding, deletion or other
registration change. When the check fails nothing is written at all, the mark
included, and no hold or clock incident is ever released here.

**Accounting is never given back.** A ledger row is written with every issuance, code
or notice, before the send. No outcome, failure or settlement deletes one, lowers
``send_count`` or reopens a cap. Settlement belongs to one send generation, the first
settlement wins, and only ``not_sent`` touches a pending row: the row whose live code
that same generation issued. A settlement that does not present the identity stored
with the generation changes nothing (ledger rows that predate migration 022 have none,
and can be neither sent nor settled).

The guessing bound of design 3.3 (at most 8 codes of an address in any 24 hours)
holds only while the wall clock is not stepped, the secret key is unchanged and no
ledger row of the address is removed other than by the 48-hour cleanup. Outside those
assumptions this module claims no number.

**The circuit assumes** that attempts are admitted one after another and each is
settled before the next is admitted (one synchronous worker). With several handlers
as many attempts as handlers can be admitted before a failure is settled, and an
attempt that is never settled stays ``started`` and is not counted; the
non-refundable hourly cap is then the bound that remains.

Never logged, returned in an error or put in an exception message: the address, names,
school, the password, its hash, the code, the token, a key or a message body. Log lines
(logger ``learnpilot.registration``) hold an event name and row ids.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Mapping

from web.services import accounts, profiles
from web.services import registration_forms as forms
from web.services import registration_scrub as scrub
from web.services.mail import Mailer, OutboundEmail, SendOutcome

logger = logging.getLogger("learnpilot.registration")

# Activation gates of the build (design 7.3, decision D14). While PRODUCTION_ACTIVATION is
# False a production start keeps every ``/register...`` route at the generic 404 whatever
# the mail settings hold. TEACHER_ACTIVATION covers the teacher form and the activation of
# a teacher registration (D5; the assignment quotas it needs exist since PR 7, and the gate
# is still off). No setting and no environment variable overrides either constant: a deploy
# changes them.
PRODUCTION_ACTIVATION = False
TEACHER_ACTIVATION = False

# Limits (design 3.2, decision D6). Constants of the service; no setting raises them.
PENDING_LIFETIME = timedelta(minutes=30)
CODE_LIFETIME = timedelta(minutes=10)
MAX_CODE_ATTEMPTS = 5
MAX_SENDS = 4  # the first and three resends
RESEND_INTERVAL = timedelta(seconds=60)
ADDRESS_HOURLY_CAP = 5
ADDRESS_DAILY_CAP = 8
HOUR = timedelta(hours=1)
DAILY_WINDOW = timedelta(hours=24, minutes=15)  # 24 h + the code lifetime + 5 min of margin
TOTAL_HOURLY_CAP = 300
CIRCUIT_FAILURES = 3
CIRCUIT_COOLDOWN = timedelta(minutes=5)
CLOCK_SKEW = timedelta(seconds=300)
FINAL_ROW_RETENTION = timedelta(hours=24)
LEDGER_RETENTION = timedelta(hours=48)

KIND_CODE = "code"
KIND_NOTICE = "notice"

CODE_SUBJECT = "Код за потвърждение в LearnPilot"
NOTICE_SUBJECT = "Опит за регистрация в LearnPilot"

# Account types whose registration can be activated at all, and the role literal of
# each. Never a submitted value. A teacher registration is activated only while
# ``teacher_enabled`` is true (D5, design 6.3 step 5); otherwise it is voided, and it is
# never turned into a student.
_ROLE_OF_ACCOUNT_TYPE = {forms.STUDENT: "student", forms.TEACHER: "teacher"}

_SETTLED_OUTCOMES = tuple(outcome.value for outcome in SendOutcome)
_CIRCUIT_HISTORY = (SendOutcome.ACCEPTED.value, SendOutcome.NOT_SENT.value, SendOutcome.UNKNOWN.value)
_CIRCUIT_FAILURE = (SendOutcome.NOT_SENT.value, SendOutcome.UNKNOWN.value)

_SCRUB = scrub.SCRUB_ASSIGNMENTS  # one definition, shared with the snapshot step (registration_scrub)
_PENDING_COLUMNS = (
    "id, binding_hash, account_type, state, is_shadow, email, first_name, last_name, grade, "
    "school_name, subjects_json, password_hash, code_hash, code_expires_at, code_send_id, "
    "code_attempts, send_count, resend_after, created_at, expires_at, closed_at"
)
_CODE_LEAD = "Кодът ти за потвърждение е: "
_ISSUANCE_ID_LENGTH = 32  # lower-case hexadecimal: 128 random bits (migration 022)
_STAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


class TransactionRequired(RuntimeError):
    """A writer was called with no transaction open. Nothing was written."""


class NotSendable(RuntimeError):
    """``dispatch`` was asked to send what is not the committed, unsettled, current
    issuance of a pending registration, exactly as it was issued. Nothing was sent."""


class Availability(Enum):
    """Why nothing may be admitted, or ``AVAILABLE``."""

    AVAILABLE = "available"
    HOLD = "hold"
    CLOCK_BEHIND = "clock_behind"
    SENDER_BUSY = "sender_busy"
    CIRCUIT_OPEN = "circuit_open"
    TOTAL_CAP = "total_cap"
    TEACHER_OFF = "teacher_off"


class Settlement(Enum):
    """What ``settle_send`` did. Only ``SETTLED`` wrote anything."""

    SETTLED = "settled"                      # this call recorded the outcome
    ALREADY_SETTLED = "already_settled"      # this issuance was settled before; nothing changed
    NOT_THIS_ISSUANCE = "not_this_issuance"  # no ledger row has this number and identity; nothing changed


class Outcome(Enum):
    ISSUED = "issued"                        # a code or notice was issued; send it after the commit
    UNAVAILABLE = "unavailable"              # hold, clock, circuit, total budget, sender, teacher gate
    ADDRESS_CAPPED = "address_capped"
    NO_PENDING = "no_pending"                # no row for this binding
    NOT_PENDING = "not_pending"              # the row is final; nothing was written
    EXPIRED = "expired"
    TOO_EARLY = "too_early"                  # resend inside the wait
    RESEND_EXHAUSTED = "resend_exhausted"
    CODE_EXPIRED = "code_expired"            # no live code; no attempt counted
    TOO_MANY = "too_many"
    WRONG_CODE = "wrong_code"
    EMAIL_TAKEN = "email_taken"
    ACTIVATED = "activated"                  # the account exists once the caller's commit returns
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class Result:
    """What a writer decided, inside the caller's still-open transaction.

    ``token`` (the browser binding, from ``create_pending`` only) and ``message`` (the
    code or notice to send, from ``create_pending`` and ``resend``) are secrets: they
    are left out of ``repr``, and must not be logged or serialized. The code exists
    nowhere else in clear. ``reason`` says why an ``UNAVAILABLE`` was answered, for a
    log line; the person is told the same thing in every case.

    ``send_id`` and ``issuance_id`` together name one issuance: the generation number
    and the random identity stored with it (32 lower-case hexadecimal characters, new
    for every issuance and resend). Both are needed to send and to settle; the number
    alone is not an identity, because SQLite reuses the numbers of a rolled-back
    transaction. ``issuance_id`` is kept out of ``repr`` and of log lines.

    ``final_state`` accompanies ``NOT_PENDING`` only: ``activated``, ``void`` or
    ``expired``, read from the caller's own binding row. It lets a caller tell a
    registration that finished (``activated``) from one that ended without an account.
    """

    outcome: Outcome
    pending_id: int | None = None
    send_id: int | None = None
    kind: str | None = None
    user_id: int | None = None
    reason: Availability | None = None
    final_state: str | None = None  # NOT_PENDING only: the state the row ended in
    issuance_id: str | None = field(default=None, repr=False, compare=False)
    token: str | None = field(default=None, repr=False, compare=False)
    message: OutboundEmail | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class CleanupResult:
    """``clock_behind`` means nothing was scrubbed, deleted or written at all."""

    clock_behind: bool
    scrubbed: int = 0
    deleted: int = 0
    deleted_sends: int = 0


# ------------------------------------------------------------------------- keyed hashes

def _key(secret_key) -> bytes:
    key = secret_key if isinstance(secret_key, bytes) else str(secret_key or "").encode("utf-8")
    if not key:
        raise ValueError("Липсва ключ.")
    return key


def _keyed(secret_key, label: bytes, *parts: bytes) -> str:
    return hmac.new(_key(secret_key), label + b"\0" + b"\0".join(parts), hashlib.sha256).hexdigest()


def binding_hash(secret_key, token: str) -> str:
    """The stored form of a browser-binding token (design 2.3)."""
    if not isinstance(token, str):
        raise ValueError("Невалиден маркер.")
    return _keyed(secret_key, b"registration-binding", token.encode("utf-8", errors="surrogatepass"))


def email_hash(secret_key, email: str) -> str:
    """The ledger key of a normalised address (design 3.4). No address is stored."""
    if not isinstance(email, str):
        raise ValueError("Невалиден имейл.")
    return _keyed(secret_key, b"registration-email", email.encode("utf-8", errors="surrogatepass"))


def _code_hash(secret_key, pending_id: int, secret: bytes) -> str:
    return _keyed(secret_key, b"registration-code", str(pending_id).encode("ascii"), secret)


def _is_hash(value) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(ch in "0123456789abcdef" for ch in value)


def _new_code() -> str:
    return f"{secrets.randbelow(10**6):06d}"


def _new_issuance_id() -> str:
    return secrets.token_hex(_ISSUANCE_ID_LENGTH // 2)


def _is_issuance_id(value) -> bool:
    return (isinstance(value, str) and len(value) == _ISSUANCE_ID_LENGTH
            and all(ch in "0123456789abcdef" for ch in value))


def _submitted_code(value) -> str | None:
    """Exactly six ASCII digits once leading and trailing whitespace is trimmed, or
    ``None`` (a wrong code). Whitespace inside the value is not removed: ``123 456`` is
    wrong, and so is a digit that is not one of ``0`` to ``9``."""
    if not isinstance(value, str):
        return None
    code = value.strip()
    return code if len(code) == 6 and all(ch in "0123456789" for ch in code) else None


# ------------------------------------------------------------------------- time

def _stamp(moment: datetime) -> str:
    # SQLite's CURRENT_TIMESTAMP layout in UTC, so stored values compare as text.
    return moment.astimezone(timezone.utc).strftime(_STAMP_FORMAT)


def _moment(stored) -> datetime | None:
    """A stored stamp as a moment, or ``None`` unless it is exactly what ``_stamp`` writes."""
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


def _writer_now(conn: sqlite3.Connection, clock: Callable[[], datetime]) -> datetime:
    """Take the write lock inside the caller's transaction, then read the clock."""
    if not callable(clock):
        raise TypeError("clock must be a callable returning the current UTC time")
    if not conn.in_transaction:
        raise TransactionRequired("registration writers need the caller's open transaction")
    # Changes no row and creates none; it makes this connection the writer if it is not yet.
    conn.execute("UPDATE registration_control SET id = id WHERE 0")
    return _checked_now(clock())


# ------------------------------------------------------------------------- reads

def _one(cursor: sqlite3.Cursor) -> dict | None:
    row = cursor.fetchone()
    if row is None:
        return None
    return dict(zip([col[0] for col in cursor.description], tuple(row)))


def _pending_row(conn: sqlite3.Connection, binding) -> dict | None:
    if not _is_hash(binding):
        return None
    return _one(conn.execute(
        f"SELECT {_PENDING_COLUMNS} FROM pending_registrations WHERE binding_hash = ?", (binding,)
    ))


def _blocked(conn: sqlite3.Connection, now: datetime, *, hold: bool = True) -> Availability | None:
    """The hold, then the clock against the high-water mark (design 3.8). Reads only."""
    control = conn.execute(
        "SELECT high_water_at, hold_reason FROM registration_control WHERE id = 1"
    ).fetchone()
    if control is None:
        return None
    if hold and control[1] is not None:
        logger.warning("restore_hold")
        return Availability.HOLD
    if _stamp(now + CLOCK_SKEW) < control[0]:
        logger.warning("clock_regressed")
        return Availability.CLOCK_BEHIND
    return None


def _count_since(conn: sqlite3.Connection, now: datetime, window: timedelta, address: str | None = None) -> int:
    # No upper bound: a row dated later than ``now`` counts inside every window.
    lower = _stamp(now - window)
    if address is None:
        cursor = conn.execute("SELECT COUNT(*) FROM registration_sends WHERE attempted_at >= ?", (lower,))
    else:
        cursor = conn.execute(
            "SELECT COUNT(*) FROM registration_sends WHERE email_hash = ? AND attempted_at >= ?",
            (address, lower),
        )
    return cursor.fetchone()[0]


def _circuit_open(conn: sqlite3.Connection, now: datetime) -> bool:
    """Design 3.5: by generation id, ``refused`` and ``started`` left out of the history."""
    marks = ", ".join("?" for _ in _CIRCUIT_HISTORY)
    rows = conn.execute(
        f"SELECT outcome, attempted_at FROM registration_sends WHERE outcome IN ({marks}) "
        "ORDER BY id DESC LIMIT ?",
        (*_CIRCUIT_HISTORY, CIRCUIT_FAILURES),
    ).fetchall()
    return (
        len(rows) == CIRCUIT_FAILURES
        and all(row[0] in _CIRCUIT_FAILURE for row in rows)
        and rows[0][1] > _stamp(now - CIRCUIT_COOLDOWN)
    )


def _checked_total_cap(total_cap) -> int:
    if type(total_cap) is not int or not 1 <= total_cap <= TOTAL_HOURLY_CAP:
        raise ValueError("Невалиден общ лимит.")
    return total_cap


def _admission(conn: sqlite3.Connection, now: datetime, sender_busy: bool, total_cap: int) -> Availability:
    blocked = _blocked(conn, now)
    if blocked is not None:
        return blocked
    if sender_busy:
        return Availability.SENDER_BUSY
    if _circuit_open(conn, now):
        logger.warning("circuit_open")
        return Availability.CIRCUIT_OPEN
    if _count_since(conn, now, HOUR) >= total_cap:
        logger.warning("total_cap_reached")
        return Availability.TOTAL_CAP
    return Availability.AVAILABLE


def availability(conn: sqlite3.Connection, now: datetime, *, sender_busy: bool,
                 total_cap: int = TOTAL_HOURLY_CAP) -> Availability:
    """Whether a submission or resend could be admitted now. Reads only.

    Decided in this order: restore hold, clock behind the high-water mark, an unreaped
    sender in this worker (``sender_busy``, from ``mail.sender_busy()``), the mail
    circuit, the total hourly budget. Read it before hashing a password, so a refused
    submission costs no scrypt work; ``create_pending`` and ``resend`` decide it again
    under the write lock.
    """
    return _admission(conn, _checked_now(now), bool(sender_busy), _checked_total_cap(total_cap))


def _address_capped(conn: sqlite3.Connection, address: str, now: datetime) -> bool:
    return (
        _count_since(conn, now, HOUR, address) >= ADDRESS_HOURLY_CAP
        or _count_since(conn, now, DAILY_WINDOW, address) >= ADDRESS_DAILY_CAP
    )


def address_capped(conn: sqlite3.Connection, address_hash: str, now: datetime) -> bool:
    """Whether the address has used its 5 sends an hour or 8 per 24 h 15 min. Reads only."""
    if not _is_hash(address_hash):
        raise ValueError("Невалиден ключ на адрес.")
    return _address_capped(conn, address_hash, _checked_now(now))


# ------------------------------------------------------------------------- writes

def _raise_mark(conn: sqlite3.Connection, now: datetime) -> None:
    conn.execute(
        "INSERT INTO registration_control (id, high_water_at) VALUES (1, ?) "
        "ON CONFLICT(id) DO UPDATE SET high_water_at = excluded.high_water_at "
        "WHERE excluded.high_water_at > registration_control.high_water_at",
        (_stamp(now),),
    )


def _close(conn: sqlite3.Connection, pending_id: int, state: str, now: datetime) -> None:
    """Leave ``pending`` for a final state and scrub, in one statement."""
    cursor = conn.execute(
        f"UPDATE pending_registrations SET state = ?, closed_at = ?, {_SCRUB} "
        "WHERE id = ? AND state = 'pending'",
        (state, _stamp(now), pending_id),
    )
    if cursor.rowcount != 1:
        raise RuntimeError("pending registration changed state unexpectedly")


def _is_expired(row: Mapping, now: datetime) -> bool:
    # Past its time, or dated in the future beyond the skew. The second case is reached
    # only when the clock is not behind the mark, which the caller has already decided.
    return row["expires_at"] <= _stamp(now) or row["created_at"] > _stamp(now + CLOCK_SKEW)


def _code_message(to: str, code: str) -> OutboundEmail:
    return OutboundEmail(to=to, subject=CODE_SUBJECT, text=(
        f"{_CODE_LEAD}{code}\n"
        "Валиден е 10 минути. Въведи го в страницата, от която започна регистрацията.\n"
        "Ако не си започвал(а) регистрация в LearnPilot, не прави нищо и не споделяй този код с никого.\n"
    ))


def _notice_message(to: str) -> OutboundEmail:
    return OutboundEmail(to=to, subject=NOTICE_SUBJECT, text=(
        "Някой се опита да създаде профил в LearnPilot с този имейл, но такъв вече съществува. "
        "Ако си ти, влез с имейла и паролата си. Ако не си, не е нужно да правиш нищо.\n"
    ))


def _issue(conn: sqlite3.Connection, *, pending_id: int, email: str, is_shadow: bool, expires_at: str,
           secret_key, now: datetime) -> tuple[int, str, str, OutboundEmail]:
    """One issuance: the ledger row with a new identity, then the code it owns.
    Returns generation, identity, kind, message."""
    if is_shadow:
        # A hash no six digits can produce, sent nowhere; the owner gets the notice (design 2.5).
        kind, message = KIND_NOTICE, _notice_message(email)
        stored = _code_hash(secret_key, pending_id, secrets.token_bytes(32))
    else:
        code = _new_code()
        kind, message = KIND_CODE, _code_message(email, code)
        stored = _code_hash(secret_key, pending_id, code.encode("ascii"))
    issuance_id = _new_issuance_id()
    send_id = conn.execute(
        "INSERT INTO registration_sends (pending_id, email_hash, kind, attempted_at, issuance_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (pending_id, email_hash(secret_key, email), kind, _stamp(now), issuance_id),
    ).lastrowid
    cursor = conn.execute(
        "UPDATE pending_registrations SET code_hash = ?, code_expires_at = ?, code_send_id = ?, "
        "code_attempts = 0, send_count = send_count + 1, resend_after = ? "
        "WHERE id = ? AND state = 'pending'",
        (stored, min(_stamp(now + CODE_LIFETIME), expires_at), send_id,
         _stamp(now + RESEND_INTERVAL), pending_id),
    )
    if cursor.rowcount != 1:
        raise RuntimeError("pending registration changed state unexpectedly")
    _raise_mark(conn, now)
    return send_id, issuance_id, kind, message


def _checked_fields(cleaned: Mapping[str, Any], account_type: str) -> dict[str, Any]:
    """The values a pending row stores, refused (never repaired) unless already clean."""
    if account_type not in (forms.STUDENT, forms.TEACHER):
        raise ValueError("Невалиден вид акаунт.")
    if not isinstance(cleaned, Mapping) or cleaned.get("account_type", account_type) != account_type:
        raise ValueError("Невалидни данни за регистрация.")
    email = cleaned.get("email")
    try:
        if not isinstance(email, str) or forms.public_email(email) != email:
            raise ValueError
    except ValueError:
        raise ValueError("Невалиден имейл.") from None
    first, last = cleaned.get("first_name"), cleaned.get("last_name")
    if forms.clean_name(first)[0] != first or forms.clean_name(last)[0] != last or first is None or last is None:
        raise ValueError("Невалидно име.")
    fields: dict[str, Any] = {"email": email, "first_name": first, "last_name": last,
                              "grade": None, "school_name": None, "subjects_json": None}
    if account_type == forms.STUDENT:
        grade = cleaned.get("grade")
        if type(grade) is not int or forms.clean_grade(grade) != grade:
            raise ValueError("Невалиден клас.")
        fields["grade"] = grade
    else:
        school, subjects = cleaned.get("school_name"), cleaned.get("subjects")
        if school is None or forms.clean_school(school) != school:
            raise ValueError("Невалидно училище.")
        # Shape only. The registry check is the profile service's, at activation.
        if (not isinstance(subjects, list) or not forms.MIN_SUBJECTS <= len(subjects) <= forms.MAX_SUBJECTS
                or any(not isinstance(item, dict) or set(item) != {"subject_id", "label"}
                       or not isinstance(item["label"], str)
                       or not (item["subject_id"] is None or isinstance(item["subject_id"], str))
                       for item in subjects)):
            raise ValueError("Невалидни предмети.")
        fields["school_name"] = school
        fields["subjects_json"] = json.dumps(subjects, ensure_ascii=False)
    return fields


def create_pending(
    conn: sqlite3.Connection,
    *,
    cleaned: Mapping[str, Any],
    account_type: str,
    password_hash: str,
    previous_binding: str | None,
    secret_key,
    clock: Callable[[], datetime],
    sender_busy: bool,
    total_cap: int = TOTAL_HOURLY_CAP,
    teacher_enabled: bool = False,
) -> Result:
    """Store a pending registration and issue its first code, or the notice of a shadow.

    ``cleaned`` is ``ValidationResult.values`` of the matching form; ``account_type`` is
    the literal of the route, never a submitted value. ``password_hash`` comes from
    ``accounts.hash_new_password`` (made before the lock is taken; for an address that
    already has an account it is discarded, design 2.5). ``previous_binding`` is the
    binding hash of the token this browser's session held, or ``None``.

    Order, under the lock: hold and clock; the teacher gate; sender, circuit and total
    budget; the address caps; and only then the inline cleanup, the voiding of this
    browser's previous row, the row, its ledger row and the high-water mark. A refusal
    (``UNAVAILABLE``, ``ADDRESS_CAPPED``) writes nothing.

    ``ISSUED`` carries ``token`` for the session, ``pending_id``, ``send_id`` (the send
    generation), ``issuance_id`` (its identity), ``kind`` and ``message``. Commit, then
    :func:`dispatch`, then settle. A refusal creates no row and no identity.
    A pending registration grants nothing and reserves no address.
    """
    fields = _checked_fields(cleaned, account_type)
    if not accounts.password_hash_is_current(password_hash):
        raise ValueError("Невалиден запис на парола.")
    total_cap = _checked_total_cap(total_cap)
    email = fields["email"]
    address = email_hash(secret_key, email)
    _notice_message(email)  # refuses an address no message can carry, before any write
    now = _writer_now(conn, clock)

    blocked = _blocked(conn, now)
    if blocked is not None:
        return Result(Outcome.UNAVAILABLE, reason=blocked)
    if account_type == forms.TEACHER and not teacher_enabled:
        return Result(Outcome.UNAVAILABLE, reason=Availability.TEACHER_OFF)
    admission = _admission(conn, now, bool(sender_busy), total_cap)
    if admission is not Availability.AVAILABLE:
        return Result(Outcome.UNAVAILABLE, reason=admission)
    if _address_capped(conn, address, now):
        return Result(Outcome.ADDRESS_CAPPED)

    _cleanup(conn, now)
    previous = _pending_row(conn, previous_binding)
    if previous is not None and previous["state"] == "pending":
        _close(conn, previous["id"], "void", now)

    is_shadow = conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone() is not None
    token = secrets.token_urlsafe(32)
    expires_at = _stamp(now + PENDING_LIFETIME)
    pending_id = conn.execute(
        "INSERT INTO pending_registrations (binding_hash, account_type, state, is_shadow, email, "
        "first_name, last_name, grade, school_name, subjects_json, password_hash, created_at, expires_at) "
        "VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (binding_hash(secret_key, token), account_type, 1 if is_shadow else 0, email,
         fields["first_name"], fields["last_name"], fields["grade"], fields["school_name"],
         fields["subjects_json"], None if is_shadow else password_hash, _stamp(now), expires_at),
    ).lastrowid
    send_id, issuance_id, kind, message = _issue(
        conn, pending_id=pending_id, email=email, is_shadow=is_shadow, expires_at=expires_at,
        secret_key=secret_key, now=now)
    logger.info("submission_decided pending=%d send=%d", pending_id, send_id)
    return Result(Outcome.ISSUED, pending_id=pending_id, send_id=send_id, kind=kind,
                  issuance_id=issuance_id, token=token, message=message)


def _teacher_refused(row: Mapping, teacher_enabled: bool) -> bool:
    return row["account_type"] != forms.STUDENT and not (
        teacher_enabled and row["account_type"] in _ROLE_OF_ACCOUNT_TYPE
    )


def _usable_row(conn: sqlite3.Connection, binding, now: datetime, teacher_enabled: bool):
    """Steps 1 to 5 of design 6.3, shared by ``verify`` and ``resend``.

    Returns ``(row, None)`` for a pending, unexpired, activatable row, else
    ``(None, Result)``. Reads come first and write nothing; the hold and the clock are
    decided before the row is expired or voided.
    """
    row = _pending_row(conn, binding)
    if row is None:
        return None, Result(Outcome.NO_PENDING)
    if row["state"] != "pending":
        return None, Result(Outcome.NOT_PENDING, pending_id=row["id"], final_state=row["state"])
    blocked = _blocked(conn, now)
    if blocked is not None:
        return None, Result(Outcome.UNAVAILABLE, pending_id=row["id"], reason=blocked)
    if _is_expired(row, now):
        _close(conn, row["id"], "expired", now)
        return None, Result(Outcome.EXPIRED, pending_id=row["id"])
    if _teacher_refused(row, teacher_enabled):
        _close(conn, row["id"], "void", now)
        logger.info("gate_off_decided pending=%d", row["id"])
        return None, Result(Outcome.UNAVAILABLE, pending_id=row["id"], reason=Availability.TEACHER_OFF)
    return row, None


def resend(
    conn: sqlite3.Connection,
    binding: str,
    *,
    secret_key,
    clock: Callable[[], datetime],
    sender_busy: bool,
    total_cap: int = TOTAL_HOURLY_CAP,
    teacher_enabled: bool = False,
) -> Result:
    """Replace the code of this browser's registration (or repeat a shadow's notice).

    At most three resends, at least 60 seconds after the previous issuance unless that
    one was settled ``not_sent``. Each is a new ledger row, a new send generation with
    a new issuance identity, and a new code with its own five attempts and an expiry
    never past the registration's.
    The previous code is replaced in the statement that stores the new one.
    """
    total_cap = _checked_total_cap(total_cap)
    now = _writer_now(conn, clock)
    row, refusal = _usable_row(conn, binding, now, teacher_enabled)
    if refusal is not None:
        return refusal
    admission = _admission(conn, now, bool(sender_busy), total_cap)
    if admission is not Availability.AVAILABLE:
        return Result(Outcome.UNAVAILABLE, pending_id=row["id"], reason=admission)
    if row["send_count"] >= MAX_SENDS:
        return Result(Outcome.RESEND_EXHAUSTED, pending_id=row["id"])
    if row["resend_after"] is not None and row["resend_after"] > _stamp(now):
        return Result(Outcome.TOO_EARLY, pending_id=row["id"])
    if _address_capped(conn, email_hash(secret_key, row["email"]), now):
        return Result(Outcome.ADDRESS_CAPPED, pending_id=row["id"])
    send_id, issuance_id, kind, message = _issue(
        conn, pending_id=row["id"], email=row["email"], is_shadow=bool(row["is_shadow"]),
        expires_at=row["expires_at"], secret_key=secret_key, now=now)
    logger.info("resend_decided pending=%d send=%d", row["id"], send_id)
    return Result(Outcome.ISSUED, pending_id=row["id"], send_id=send_id, kind=kind,
                  issuance_id=issuance_id, message=message)


def _quota_ledger_exists(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'assignment_creations'").fetchone() is not None


def _email_taken(conn: sqlite3.Connection, row: Mapping, now: datetime) -> Result:
    _close(conn, row["id"], "void", now)
    logger.info("email_taken_decided pending=%d", row["id"])
    return Result(Outcome.EMAIL_TAKEN, pending_id=row["id"])


def _code_is_live(conn: sqlite3.Connection, row: Mapping, now: datetime) -> bool:
    """Whether the row's code may be tried now, by the ledger row that issued it.

    The issuance time is ``registration_sends.attempted_at`` of ``code_send_id``, never
    a time worked back from ``code_expires_at``: an expiry is cut short at the
    registration's own deadline, so it does not say when the code was issued. The
    ledger row must exist, belong to this registration, be of the row's kind and carry
    a stamp this module wrote, and the stored expiry must be the one that issuance
    gives. Anything else is logged and is not a live code (fail closed).

    Live means: issued no more than the skew ahead of ``now``, and ``now`` before the
    expiry. A code dated further ahead is never tried, whatever the high-water mark was
    set to in the meantime.
    """
    if row["code_hash"] is None and row["code_expires_at"] is None:
        return False  # retired by a ``not_sent`` settlement: nothing to check
    send = conn.execute(
        "SELECT pending_id, kind, attempted_at FROM registration_sends WHERE id = ?",
        (row["code_send_id"],),
    ).fetchone()
    issued_at = _moment(send[2]) if send is not None else None
    if (
        issued_at is None
        or row["code_hash"] is None
        or send[0] != row["id"]
        or send[1] != (KIND_NOTICE if row["is_shadow"] else KIND_CODE)
        or row["code_expires_at"] != min(_stamp(issued_at + CODE_LIFETIME), row["expires_at"])
    ):
        logger.warning("code_evidence_invalid pending=%d", row["id"])
        return False
    return send[2] <= _stamp(now + CLOCK_SKEW) and _stamp(now) < row["code_expires_at"]


def verify(
    conn: sqlite3.Connection,
    binding: str,
    code,
    *,
    secret_key,
    clock: Callable[[], datetime],
    teacher_enabled: bool = False,
) -> Result:
    """Check a submitted code and, when it is right, activate the registration.

    The submitted value is trimmed of leading and trailing whitespace only and must
    then be exactly six ASCII digits; anything else, inner whitespace included, is a
    wrong code. The steps and their order are design 6.3. A wrong, malformed or
    missing code counts as one of the five attempts of the live code; a code that is
    not live (expired, retired, issued more than the skew ahead of the clock, or without
    consistent ledger evidence of its issuance) counts nothing. A correct code clears
    itself before anything else is decided, then creates the ``users`` row (the role literal of the row's account
    type, ``student`` or ``teacher``; ``email_verified_at`` set), the profile with a teacher's
    subjects, and marks the row ``activated`` and scrubbed, all in the caller's one
    transaction. No session is issued and nobody is
    signed in. ``ACTIVATED`` is a decision: the account exists once the caller's commit
    has returned, and not if it fails.

    ``EMAIL_TAKEN`` is answered in two cases only: the row is a shadow, or the insert
    met the uniqueness of ``users.email`` itself, which
    ``accounts.create_verified_user`` reports structurally (a conflict-targeted
    insert), not by reading an error message. Every exception propagates, an integrity
    error raised by a trigger, a check, a foreign key or another uniqueness included;
    the caller rolls back, which leaves the registration pending with its code and
    attempt count as before. A teacher registration is activated only with
    ``teacher_enabled``; otherwise it is voided and answered ``UNAVAILABLE`` (D5), and it
    is never turned into a student. An address that already has an account, of any
    role, is ``EMAIL_TAKEN`` and that account is not changed.
    """
    now = _writer_now(conn, clock)
    row, refusal = _usable_row(conn, binding, now, teacher_enabled)
    if refusal is not None:
        return refusal
    pending_id = row["id"]
    if not _code_is_live(conn, row, now):
        return Result(Outcome.CODE_EXPIRED, pending_id=pending_id)
    if row["code_attempts"] >= MAX_CODE_ATTEMPTS:
        return Result(Outcome.TOO_MANY, pending_id=pending_id)
    conn.execute(
        "UPDATE pending_registrations SET code_attempts = code_attempts + 1 WHERE id = ?", (pending_id,)
    )
    submitted = _submitted_code(code)
    expected = _code_hash(secret_key, pending_id, (submitted or "").encode("ascii"))
    if not hmac.compare_digest(expected, row["code_hash"]) or submitted is None:
        logger.info("wrong_code_decided pending=%d", pending_id)
        return Result(Outcome.WRONG_CODE, pending_id=pending_id)

    # Single use: the code is gone before anything else is decided.
    conn.execute(
        "UPDATE pending_registrations SET code_hash = NULL, code_expires_at = NULL WHERE id = ?",
        (pending_id,),
    )
    if row["is_shadow"]:
        return _email_taken(conn, row, now)
    if row["account_type"] == forms.TEACHER and not _quota_ledger_exists(conn):
        # A teacher role is never granted by a database that cannot count its assignments
        # (migration 024). Not reachable from a production start, which refuses pending
        # migrations; it makes the rule a property of this function and not of the deploy.
        raise RuntimeError("teacher activation needs the assignment creation ledger (migration 024)")
    user_id = accounts.create_verified_user(
        conn,
        email=row["email"],
        display_name=profiles.display_name(row["first_name"], row["last_name"]),
        role=_ROLE_OF_ACCOUNT_TYPE[row["account_type"]],
        password_hash=row["password_hash"],
        verified_at=now,
    )
    if user_id is None:  # the address already has an account; nothing was inserted
        return _email_taken(conn, row, now)
    # The account, its profile and its subject rows are one unit of this caller's
    # transaction: an exception anywhere in them, a single subject row included, leaves
    # nothing of the three, and the registration pending with its code and attempt count
    # as before. The profile service checks the subjects against the registry again.
    profiles.create_profile(
        conn, user_id, account_type=row["account_type"], first_name=row["first_name"],
        last_name=row["last_name"], grade=row["grade"], school_name=row["school_name"],
        subjects=None if row["subjects_json"] is None else json.loads(row["subjects_json"]), now=now,
    )
    _close(conn, pending_id, "activated", now)
    logger.info("activation_decided pending=%d user=%d", pending_id, user_id)
    return Result(Outcome.ACTIVATED, pending_id=pending_id, user_id=user_id)


def cancel(conn: sqlite3.Connection, binding: str, *, clock: Callable[[], datetime]) -> Result:
    """Void and scrub this browser's pending registration („Започни отново“).

    A final row is left as it is (``NOT_PENDING``). While the clock is behind the
    high-water mark nothing is voided (``UNAVAILABLE``, design 3.8). A restore hold does
    not stop a cancellation, which only removes data (approved by Gabriel with PR 4):
    the hold and the mark are left exactly as they were, so a cancellation never
    reopens registration.
    """
    now = _writer_now(conn, clock)
    row = _pending_row(conn, binding)
    if row is None:
        return Result(Outcome.NO_PENDING)
    if row["state"] != "pending":
        return Result(Outcome.NOT_PENDING, pending_id=row["id"], final_state=row["state"])
    blocked = _blocked(conn, now, hold=False)
    if blocked is not None:
        return Result(Outcome.UNAVAILABLE, pending_id=row["id"], reason=blocked)
    _close(conn, row["id"], "void", now)
    return Result(Outcome.CANCELLED, pending_id=row["id"])


# ------------------------------------------------------------------------- the verify page

@dataclass(frozen=True)
class PendingView:
    """What the verify page of the browser that holds the binding may show.

    ``email`` is the address that browser typed (design 1.2): personal data, never
    logged and kept out of ``repr``. ``code_retired`` is true when a ``not_sent``
    settlement retired the code, so the page says the message did not leave and offers
    a resend at once (design 4.3).
    """

    pending_id: int
    email: str = field(repr=False)
    code_retired: bool


def _clock_behind(conn: sqlite3.Connection, now: datetime) -> bool:
    control = conn.execute("SELECT high_water_at FROM registration_control WHERE id = 1").fetchone()
    return control is not None and _stamp(now + CLOCK_SKEW) < control[0]


def pending_view(conn: sqlite3.Connection, binding, now: datetime) -> PendingView | None:
    """This browser's registration as its verify page shows it, or ``None`` when there is
    none it could still complete: no row, a final row, or a pending row past its time.

    **Reads only.** Expiry is evaluated here and written only by the POSTs and the
    cleanup (design 6.2), so a GET never changes a row. While the clock is behind the
    high-water mark the row's own times are not judged (the POST answers "unavailable").
    Nothing is logged.
    """
    now = _checked_now(now)
    row = _pending_row(conn, binding)
    if row is None or row["state"] != "pending" or row["email"] is None:
        return None
    if not _clock_behind(conn, now) and _is_expired(row, now):
        return None
    return PendingView(pending_id=row["id"], email=row["email"], code_retired=row["code_hash"] is None)


# ------------------------------------------------------------------------- delivery

def _is_committed_issuance(conn: sqlite3.Connection, issued: Result, secret_key) -> bool:
    """Whether ``issued`` is, field for field, the issuance the committed rows describe."""
    found = conn.execute(
        "SELECT s.issuance_id, s.kind, s.email_hash, p.is_shadow, p.email, p.code_hash, p.binding_hash "
        "FROM registration_sends AS s JOIN pending_registrations AS p ON p.id = s.pending_id "
        "WHERE s.id = ? AND s.pending_id = ? AND s.outcome = 'started' "
        "AND p.state = 'pending' AND p.code_send_id = s.id",
        (issued.send_id, issued.pending_id),
    ).fetchone()
    if found is None:
        return False
    identity, kind, address, is_shadow, email, stored, binding = found
    message = issued.message
    if (
        identity is None  # a ledger row older than migration 022: never sent
        or not hmac.compare_digest(identity, issued.issuance_id)
        or issued.kind != kind
        or kind != (KIND_NOTICE if is_shadow else KIND_CODE)
        or stored is None
        or email is None
        or not hmac.compare_digest(address, email_hash(secret_key, email))
    ):
        return False
    if issued.token is not None and not hmac.compare_digest(binding, binding_hash(secret_key, issued.token)):
        return False
    if kind == KIND_NOTICE:
        return message == _notice_message(email)
    code = message.text[len(_CODE_LEAD):len(_CODE_LEAD) + 6]
    return (
        _submitted_code(code) == code
        and message == _code_message(email, code)
        and hmac.compare_digest(stored, _code_hash(secret_key, issued.pending_id, code.encode("ascii")))
    )


def dispatch(conn: sqlite3.Connection, mailer: Mailer, issued: Result, *, secret_key) -> SendOutcome:
    """Hand the message of a **committed** issuance to ``mailer``: one call to
    ``mailer.send`` per call of this function.

    Refuses (:class:`NotSendable`, nothing sent) while ``conn`` has a transaction open,
    and unless ``issued`` is exactly the issuance the committed rows describe. All of
    this must hold, read from the database and recomputed with ``secret_key``:

    * the ledger row of ``send_id`` holds ``issued.issuance_id``. Numeric ids are not
      enough: SQLite gives the ids of a rolled-back issuance to the next one, and the
      result of the rolled-back one has another identity whatever else coincides, the
      recipient, the six digits and the whole message included. A ledger row without
      an identity (older than migration 022) is never sent;
    * that row is for ``pending_id``, is still ``started``, and is of ``issued.kind``;
    * the registration is still ``pending``, of the matching kind, and ``send_id`` is
      its **current** generation (an older generation is not sent once a newer code
      replaced it);
    * the row's address hashes to the ledger row's address key;
    * the token, when the result carries one (a first issuance), hashes to the row's
      binding;
    * the whole message, subject and body included, equals the one this module builds
      for that address, and for a code the six digits in it hash to the row's stored
      code hash.

    **One mailer invocation per call, not per generation.** The read is not a claim:
    until the generation is settled, a repeated or concurrent call with the same
    issuance passes every check and sends again. The caller calls this once per
    committed issuance and never retries it. Nothing here claims that a message was
    sent once, or delivered.

    **After** ``NotSendable`` **the caller settles nothing**: nothing was handed to a
    mailer, and no outcome exists to record.

    The read assumes a connection in sqlite3's default transaction mode, where a
    ``SELECT`` opens no transaction. An exception escaping the mailer is ``UNKNOWN``
    (the code and the budgets stay), logged by class name only; ``KeyboardInterrupt``
    and ``SystemExit`` propagate. The outcome is not recorded here: settle it with
    :func:`settle_send` in a transaction of its own.
    """
    if (not isinstance(issued, Result) or issued.outcome is not Outcome.ISSUED
            or not isinstance(issued.message, OutboundEmail)
            or type(issued.send_id) is not int or type(issued.pending_id) is not int
            or not _is_issuance_id(issued.issuance_id)):
        raise NotSendable("not an issuance")
    _key(secret_key)
    if conn.in_transaction:
        raise NotSendable("a transaction is open: commit before sending")
    committed = _is_committed_issuance(conn, issued, secret_key)
    if conn.in_transaction:
        raise NotSendable("a transaction is open: commit before sending")
    if not committed:
        raise NotSendable("not the committed, unsettled, current issuance")
    logger.info("send_started pending=%d send=%d", issued.pending_id, issued.send_id)
    try:
        outcome = mailer.send(issued.message)
    except Exception as exc:
        logger.warning("sender_error error=%s", type(exc).__name__)
        return SendOutcome.UNKNOWN
    return outcome if isinstance(outcome, SendOutcome) else SendOutcome.UNKNOWN


def settle_send(conn: sqlite3.Connection, send_id: int, issuance_id: str, outcome, *,
                clock: Callable[[], datetime]) -> Settlement:
    """Record what became of one issuance, named by its generation number **and** its
    identity (``Result.send_id`` and ``Result.issuance_id`` of the issuance that was
    dispatched).

    Decided before anything is written, under the write lock:

    * ``NOT_THIS_ISSUANCE``: no ledger row has this number, or the row's identity is
      another one, or the row has none (older than migration 022). **Nothing is
      changed**: no outcome, no circuit history, no pending row. This is what a result
      kept from a rolled-back transaction gets when SQLite gave its numbers to a later
      issuance. It is logged as a warning with the number only. It says nothing about
      whether anything was sent.
    * ``ALREADY_SETTLED``: this issuance is no longer ``started``. The first settlement
      wins; nothing is changed, whatever outcome is reported now.
    * ``SETTLED``: the outcome and the time were recorded by this call.

    Settlement belongs to the ledger row, not to the registration's current state: an
    older committed generation settles its own row after a resend, a cancellation, an
    activation or an expiry. Only ``not_sent`` changes a pending row, and only the row
    whose **live code this generation issued**: the code is retired and the wait before
    a resend waived. A newer code, an activated, cancelled, replaced or expired row and
    every counter are never changed, and no budget is given back by any outcome. A
    generation whose settlement never runs stays ``started``: it counts in every cap,
    not in the circuit, and its code stays usable.

    ``ValueError`` (before any read) for an outcome that is not a settlement, a number
    that is not an ``int`` or an identity that is not 32 lower-case hexadecimal
    characters, ``None`` included: a caller with no issuance in hand has nothing to
    settle. Never call this after :class:`NotSendable`.
    """
    value = outcome.value if isinstance(outcome, SendOutcome) else outcome
    if value not in _SETTLED_OUTCOMES:
        raise ValueError("Невалиден резултат от изпращане.")
    if type(send_id) is not int or not _is_issuance_id(issuance_id):
        raise ValueError("Невалидно изпращане.")
    now = _writer_now(conn, clock)
    found = conn.execute(
        "SELECT issuance_id, outcome FROM registration_sends WHERE id = ?", (send_id,)
    ).fetchone()
    if found is None or found[0] is None or not hmac.compare_digest(found[0], issuance_id):
        logger.warning("settlement_refused send=%d", send_id)
        return Settlement.NOT_THIS_ISSUANCE
    if found[1] != "started":
        return Settlement.ALREADY_SETTLED
    settled = conn.execute(
        "UPDATE registration_sends SET outcome = ?, settled_at = ? "
        "WHERE id = ? AND issuance_id = ? AND outcome = 'started'",
        (value, _stamp(now), send_id, issuance_id),
    ).rowcount
    if settled != 1:
        raise RuntimeError("send ledger changed unexpectedly")
    if value == SendOutcome.NOT_SENT.value:
        conn.execute(
            "UPDATE pending_registrations SET code_hash = NULL, code_expires_at = NULL, resend_after = NULL "
            "WHERE state = 'pending' AND code_send_id = ?",
            (send_id,),
        )
    logger.info("settlement_decided send=%d outcome=%s", send_id, value)
    return Settlement.SETTLED


# ------------------------------------------------------------------------- cleanup

def _cleanup(conn: sqlite3.Connection, now: datetime) -> CleanupResult:
    stamp = _stamp(now)
    scrubbed = conn.execute(
        f"UPDATE pending_registrations SET state = 'expired', closed_at = ?, {_SCRUB} "
        "WHERE state = 'pending' AND (expires_at <= ? OR created_at > ?)",
        (stamp, stamp, _stamp(now + CLOCK_SKEW)),
    ).rowcount
    deleted = conn.execute(
        "DELETE FROM pending_registrations WHERE state != 'pending' AND created_at <= ?",
        (_stamp(now - FINAL_ROW_RETENTION),),
    ).rowcount
    deleted_sends = conn.execute(
        "DELETE FROM registration_sends WHERE attempted_at <= ?", (_stamp(now - LEDGER_RETENTION),)
    ).rowcount
    _raise_mark(conn, now)
    return CleanupResult(False, scrubbed, deleted, deleted_sends)


def cleanup(conn: sqlite3.Connection, *, clock: Callable[[], datetime]) -> CleanupResult:
    """Expire and scrub overdue pending rows; delete old final rows and ledger rows.

    Logical retention only (design 6.5): a pending row past its 30 minutes becomes
    ``expired`` and loses every personal and credential column; a final row is deleted
    24 hours after it was created and a ledger row 48 hours after its attempt. It needs
    no secret and ignores every gate and the restore hold. **While the clock is behind
    the high-water mark it does nothing** and says so (``clock_behind``); otherwise it
    raises the mark. The restore lock and the timer are the CLI's and PR 5's.
    """
    now = _writer_now(conn, clock)
    if _blocked(conn, now, hold=False) is not None:
        return CleanupResult(True)
    result = _cleanup(conn, now)
    logger.info("cleanup_decided scrubbed=%d deleted=%d sends=%d", result.scrubbed, result.deleted,
                result.deleted_sends)
    return result


# ------------------------------------------------------------------------- operator commands

STALE_PENDING_AGE = timedelta(hours=1)  # ``registrations-cleanup --check`` (design 6.5)


def stale_pending_count(conn: sqlite3.Connection, now: datetime) -> int:
    """How many registrations are still ``pending`` more than an hour after they were
    created: the cleanup has missed its logical-retention target for them (design 6.5).
    Reads only."""
    return conn.execute(
        "SELECT COUNT(*) FROM pending_registrations WHERE state = 'pending' AND created_at < ?",
        (_stamp(_checked_now(now) - STALE_PENDING_AGE),),
    ).fetchone()[0]


@dataclass(frozen=True)
class ClockAcceptance:
    """The high-water mark before and after ``accept_clock``; both ``None`` when no
    control row existed, in which case nothing was written."""

    previous: str | None
    current: str | None


def accept_clock(conn: sqlite3.Connection, *, clock: Callable[[], datetime]) -> ClockAcceptance:
    """``registrations-clock-accept`` (design 3.8, 6.6): set the high-water mark to the
    present, read under the write lock, once the operator has checked the time.

    The only writer that may lower the mark. It changes nothing else: no hold is set or
    cleared and no registration row or ledger row is touched. Without a control row
    there is no mark to accept and nothing is written.
    """
    now = _writer_now(conn, clock)
    row = conn.execute("SELECT high_water_at FROM registration_control WHERE id = 1").fetchone()
    if row is None:
        return ClockAcceptance(None, None)
    conn.execute("UPDATE registration_control SET high_water_at = ? WHERE id = 1", (_stamp(now),))
    logger.info("clock_accept_decided")
    return ClockAcceptance(previous=row[0], current=_stamp(now))


def clear_address_limit(conn: sqlite3.Connection, address_hash: str) -> int:
    """``clear-registration-limit`` (design 6.6): delete every ledger row of one address
    and return how many there were.

    **An exception to the guessing bound** (design 3.3, test DL12): the address is
    admitted eight further sends inside the same day. No other address's rows and no
    pending row are touched; a live code whose issuing row is deleted has no evidence of
    its issuance any more and is answered as expired (fail closed), and a resend
    replaces it. Needs the caller's open transaction; never commits.
    """
    if not _is_hash(address_hash):
        raise ValueError("Невалиден ключ на адрес.")
    if not conn.in_transaction:
        raise TransactionRequired("registration writers need the caller's open transaction")
    deleted = conn.execute("DELETE FROM registration_sends WHERE email_hash = ?", (address_hash,)).rowcount
    logger.info("limit_clear_decided sends=%d", deleted)
    return deleted


# ------------------------------------------------------------------------- restore (D20)

RESTORE_HOLD = "restore"
# The high-water mark of a control row the gate has to create: "no mark yet", which is what
# an absent row means. The gate takes the current time for the timestamps it writes (the
# closing time of a voided registration, the time of a new hold), but does not accept or
# reset the clock and never sets the mark from it.
NO_MARK = "1970-01-01 00:00:00"


@dataclass(frozen=True)
class RestoreGateResult:
    """What ``restore_gate`` did to the copy it was given."""

    voided: int           # pending registrations turned ``void`` and scrubbed
    hold_set_at: str      # when the hold was set: this run's time, or the copy's own if it carried one
    already_held: bool    # the copy carried a hold, which was kept as it was


@dataclass(frozen=True)
class RestoreStatus:
    """The state ``registrations-restore-status`` judges. Read-only."""

    pending: int
    control_present: bool
    held: bool
    hold_set_at: str | None
    high_water_at: str | None


@dataclass(frozen=True)
class ReleaseResult:
    """``released`` is False when there was no hold (nothing was written). ``clock_behind``
    says whether the wall clock is, right now, behind the high-water mark by more than 300
    seconds: the release does not change that, and registration still answers
    "unavailable" until the clock passes the mark or the operator runs
    ``registrations-clock-accept``."""

    released: bool
    hold_set_at: str | None
    clock_behind: bool
    high_water_at: str | None


def restore_gate(conn: sqlite3.Connection, *, clock: Callable[[], datetime]) -> RestoreGateResult:
    """The gate of design 3.9, applied by ``deploy/learnpilot-restore.sh`` to the **staged
    copy** of a snapshot, in the caller's ``BEGIN IMMEDIATE`` transaction.

    1. Every ``pending`` registration becomes ``void`` and is scrubbed (a restored snapshot
       could otherwise bring back a cancelled registration's code, or the attempts a code
       had used). The recovery credentials of PR 8 do not exist yet; when they do, they are
       voided here too.
    2. The hold ``restore`` is set (D20 (a)). A copy that already carries a hold keeps it as
       it is, original time included: **this function never clears a hold.** An absent
       control row is created with :data:`NO_MARK`; an existing row keeps its high-water
       mark, which this function neither raises nor lowers.

    It reads no other database. It takes the current time for the timestamps it writes (the
    closing time of each voided registration and the time of a hold that is new); that time
    is compared with nothing, does not accept or reset the clock, and never changes the
    high-water mark. Repeatable (a second call changes nothing) and
    atomic (the caller rolls back on any exception and the copy is as it was). It touches no
    account, profile, subject, ledger or canonical row.
    """
    now = _writer_now(conn, clock)
    voided = scrub.void_pending(conn, now)
    control = conn.execute("SELECT hold_reason, hold_set_at FROM registration_control WHERE id = 1").fetchone()
    if control is None:
        conn.execute(
            "INSERT INTO registration_control (id, high_water_at, hold_reason, hold_set_at) VALUES (1, ?, ?, ?)",
            (NO_MARK, RESTORE_HOLD, _stamp(now)),
        )
        held_since, already = _stamp(now), False
    elif control[0] is None:
        conn.execute(
            "UPDATE registration_control SET hold_reason = ?, hold_set_at = ? WHERE id = 1",
            (RESTORE_HOLD, _stamp(now)),
        )
        held_since, already = _stamp(now), False
    else:
        held_since, already = control[1], True
    logger.info("restore_gate_decided voided=%d already_held=%s", voided, already)
    return RestoreGateResult(voided, held_since, already)


def restore_status(conn: sqlite3.Connection) -> RestoreStatus:
    """Read the registration state a restore must leave (design 3.9, 6.6). Writes nothing."""
    pending = conn.execute("SELECT COUNT(*) FROM pending_registrations WHERE state = 'pending'").fetchone()[0]
    control = conn.execute(
        "SELECT hold_reason, hold_set_at, high_water_at FROM registration_control WHERE id = 1"
    ).fetchone()
    if control is None:
        return RestoreStatus(pending, False, False, None, None)
    return RestoreStatus(pending, True, control[0] is not None, control[1], control[2])


def release_hold(conn: sqlite3.Connection, *, clock: Callable[[], datetime]) -> ReleaseResult:
    """``registrations-release-hold --acknowledge-lost-history`` (design 3.9, 6.6): end the
    restore hold. The only way it ends; nothing releases it automatically.

    The caller has required the operator's acknowledgement that the issuance history since
    the snapshot is gone. Needs the caller's open transaction and never commits. It clears
    the hold and nothing else: **not** the high-water mark (a regressed clock stays
    refused until the clock passes the mark or ``accept_clock``), not the ledger, a cap,
    the circuit, or any registration row.
    """
    now = _writer_now(conn, clock)
    control = conn.execute(
        "SELECT hold_reason, hold_set_at, high_water_at FROM registration_control WHERE id = 1"
    ).fetchone()
    if control is None:
        return ReleaseResult(False, None, False, None)
    behind = _stamp(now + CLOCK_SKEW) < control[2]
    if control[0] is None:
        return ReleaseResult(False, None, behind, control[2])
    conn.execute(
        "UPDATE registration_control SET hold_reason = NULL, hold_set_at = NULL WHERE id = 1 "
        "AND hold_reason IS NOT NULL"
    )
    logger.info("hold_release_decided")
    return ReleaseResult(True, control[1], behind, control[2])
