"""Admin second factor: TOTP credentials and recovery codes (Accounts PR T).

Independent of Flask, ``web.app`` and the blueprints. Every function takes an open
``sqlite3`` connection and never commits or rolls back: the caller owns the
transaction. Time is always passed in (``now``), so nothing reads the clock.

* The TOTP algorithm is pyotp's (RFC 6238: SHA-1, 6 digits, 30-second steps). This
  module only decides which time steps are acceptable: the current one and one on
  either side, and never a step at or below the last one accepted.
* The base32 secret is stored only as a Fernet token under ``DZI_TOTP_KEY``. There is
  no fallback key: without a usable key every operation that needs the secret raises
  :class:`TotpKeyError` and the caller fails closed.
* Recovery codes are random, shown once by the CLI, stored only as scrypt hashes
  and consumed individually.
* A credential is used for sign-in only once ``confirmed_at`` is set, which happens
  only after a code from the authenticator was verified against it.

Secrets, codes, keys and hashes are never logged, returned by lookups or put into
exception messages.
"""

from __future__ import annotations

import hmac
import re
import secrets
import sqlite3
from datetime import datetime, timezone

import pyotp
from cryptography.fernet import Fernet, InvalidToken
from werkzeug.security import check_password_hash, generate_password_hash

from web.services import accounts

TOTP_KEY_ENV = "DZI_TOTP_KEY"
ISSUER = "LearnPilot"
# Accepted time steps: current ±1 (30 s each), i.e. a code is good for about 90 s.
VERIFY_WINDOW_STEPS = 1
# A password-verified admin must complete the second factor within this time.
PENDING_LIFETIME_SECONDS = 5 * 60

RECOVERY_CODE_COUNT = 10
RECOVERY_CODE_LENGTH = 12  # 60 bits from a 32-symbol alphabet
RECOVERY_CODE_GROUP = 4
# Crockford-style base32 without i, l, o, u: nothing is easily misread.
RECOVERY_CODE_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
RECOVERY_HASH_METHOD = "scrypt"
# Anything longer than this is not a code; it is rejected before any work is done.
MAX_SUBMITTED_CODE_LENGTH = 64

_TOTP_CODE = re.compile(r"[0-9]{6}")
_SEPARATORS = re.compile(r"[\s-]+")


class TotpKeyError(RuntimeError):
    """``DZI_TOTP_KEY`` is missing or not a Fernet key. The message never holds a value."""


class TotpSecretError(RuntimeError):
    """A stored secret cannot be decrypted with the configured key (wrong key or corrupt row)."""


# --------------------------------------------------------------------------- key


def key_problem(key) -> str | None:
    """Why ``key`` cannot encrypt TOTP secrets, or ``None`` when it can."""
    if not key:
        return f"{TOTP_KEY_ENV} is not set"
    try:
        Fernet(key)
    except (TypeError, ValueError):
        return f"{TOTP_KEY_ENV} must be a Fernet key (32 url-safe base64-encoded bytes)"
    return None


def _fernet(key) -> Fernet:
    problem = key_problem(key)
    if problem:
        raise TotpKeyError(problem)
    return Fernet(key)


def _encrypt(key, secret: str) -> str:
    return _fernet(key).encrypt(secret.encode("ascii")).decode("ascii")


def _decrypt(key, ciphertext: str) -> str:
    fernet = _fernet(key)
    try:
        return fernet.decrypt(ciphertext.encode("ascii")).decode("ascii")
    except (InvalidToken, UnicodeError, ValueError, AttributeError) as exc:
        raise TotpSecretError("stored TOTP secret cannot be decrypted with the configured key") from exc


# --------------------------------------------------------------------------- codes


def timestamp(moment: datetime) -> str:
    """SQLite's CURRENT_TIMESTAMP layout in UTC, so stored values compare as text."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _compact(value) -> str | None:
    if not isinstance(value, str) or len(value) > MAX_SUBMITTED_CODE_LENGTH:
        return None
    return _SEPARATORS.sub("", value)


def normalize_totp_code(value) -> str | None:
    """Six ASCII digits (spaces ignored), or ``None``."""
    compact = _compact(value)
    return compact if compact is not None and _TOTP_CODE.fullmatch(compact) else None


def normalize_recovery_code(value) -> str | None:
    """A recovery code without separators, lower-case, or ``None`` when it cannot be one."""
    compact = _compact(value)
    if compact is None:
        return None
    compact = compact.lower()
    if len(compact) != RECOVERY_CODE_LENGTH or any(ch not in RECOVERY_CODE_ALPHABET for ch in compact):
        return None
    return compact


def _new_recovery_code() -> str:
    return "".join(secrets.choice(RECOVERY_CODE_ALPHABET) for _ in range(RECOVERY_CODE_LENGTH))


def format_recovery_code(code: str) -> str:
    return "-".join(code[i:i + RECOVERY_CODE_GROUP] for i in range(0, len(code), RECOVERY_CODE_GROUP))


def _matching_step(secret: str, code: str, now: datetime, after_step) -> int | None:
    """The newest step within the window whose code is ``code`` and that is after ``after_step``."""
    totp = pyotp.TOTP(secret)
    current = totp.timecode(now)
    matched = None
    for step in range(current - VERIFY_WINDOW_STEPS, current + VERIFY_WINDOW_STEPS + 1):
        if step < 0 or (after_step is not None and step <= after_step):
            continue
        if hmac.compare_digest(totp.generate_otp(step), code):
            matched = step
    return matched


# --------------------------------------------------------------------------- storage


def credential(conn: sqlite3.Connection, user_id: int) -> dict | None:
    """The credential row of an account (ciphertext included, for this module's use)."""
    cursor = conn.execute(
        "SELECT user_id, secret_ciphertext, confirmed_at, last_used_step, last_verified_at "
        "FROM totp_credentials WHERE user_id = ?",
        (user_id,),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return dict(zip([col[0] for col in cursor.description], tuple(row)))


def credential_state(conn: sqlite3.Connection, user_id: int) -> str | None:
    """``"confirmed"``, ``"unconfirmed"`` or ``None`` (no credential). No secret is read."""
    row = conn.execute(
        "SELECT confirmed_at IS NOT NULL FROM totp_credentials WHERE user_id = ?", (user_id,)
    ).fetchone()
    if row is None:
        return None
    return "confirmed" if row[0] else "unconfirmed"


def has_confirmed_credential(conn: sqlite3.Connection, user_id: int) -> bool:
    return credential_state(conn, user_id) == "confirmed"


def secret_is_readable(conn: sqlite3.Connection, user_id: int, key) -> bool:
    """Whether the account's stored secret decrypts with ``key``. Reads only.

    ``False`` without a credential or when the ciphertext does not decrypt (wrong key
    or corrupt row). Raises :class:`TotpKeyError` when ``key`` itself is unusable. The
    secret is discarded; no step is consumed and nothing is written.
    """
    fernet = _fernet(key)
    row = credential(conn, user_id)
    if row is None:
        return False
    try:
        fernet.decrypt(row["secret_ciphertext"].encode("ascii"))
    except (InvalidToken, UnicodeError, ValueError, AttributeError):
        return False
    return True


def _replace_recovery_codes(conn: sqlite3.Connection, user_id: int) -> list[str]:
    conn.execute("DELETE FROM totp_recovery_codes WHERE user_id = ?", (user_id,))
    codes = [_new_recovery_code() for _ in range(RECOVERY_CODE_COUNT)]
    conn.executemany(
        "INSERT INTO totp_recovery_codes (user_id, code_hash) VALUES (?, ?)",
        [(user_id, generate_password_hash(code, method=RECOVERY_HASH_METHOD)) for code in codes],
    )
    return [format_recovery_code(code) for code in codes]


# --------------------------------------------------------------------------- lifecycle (CLI)


def enroll(conn: sqlite3.Connection, user_id: int, key, account_name: str) -> str:
    """Start (or restart) an unconfirmed enrolment; return the ``otpauth://`` URI.

    The URI carries the secret and is the only time it leaves this module; the caller
    shows it once. A confirmed credential is never replaced here (reset it first).
    The account's session key is rotated.
    """
    if credential_state(conn, user_id) == "confirmed":
        raise ValueError("TOTP is already confirmed for this account; reset it first.")
    secret = pyotp.random_base32()
    ciphertext = _encrypt(key, secret)
    conn.execute("DELETE FROM totp_recovery_codes WHERE user_id = ?", (user_id,))
    conn.execute(
        "INSERT INTO totp_credentials (user_id, secret_ciphertext) VALUES (?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET secret_ciphertext = excluded.secret_ciphertext, "
        "confirmed_at = NULL, last_used_step = NULL, last_verified_at = NULL, "
        "created_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP",
        (user_id, ciphertext),
    )
    accounts.rotate_session_key(conn, user_id)
    return pyotp.TOTP(secret).provisioning_uri(name=account_name, issuer_name=ISSUER)


def confirm(conn: sqlite3.Connection, user_id: int, key, code, now: datetime) -> list[str]:
    """Activate an unconfirmed credential with a current code; return new recovery codes.

    The confirming step is recorded as used, so the same code cannot also sign in.
    The account's session key is rotated.
    """
    row = credential(conn, user_id)
    if row is None:
        raise LookupError("No TOTP enrolment for this account; run totp-enroll first.")
    if row["confirmed_at"] is not None:
        raise ValueError("TOTP is already confirmed for this account.")
    normalized = normalize_totp_code(code)
    step = _matching_step(_decrypt(key, row["secret_ciphertext"]), normalized, now, None) if normalized else None
    if step is None:
        raise ValueError("The code does not match; check the authenticator and the clock.")
    cursor = conn.execute(
        "UPDATE totp_credentials SET confirmed_at = CURRENT_TIMESTAMP, last_used_step = ?, "
        "updated_at = CURRENT_TIMESTAMP WHERE user_id = ? AND confirmed_at IS NULL",
        (step, user_id),
    )
    if cursor.rowcount != 1:
        raise ValueError("TOTP enrolment changed concurrently; start again.")
    codes = _replace_recovery_codes(conn, user_id)
    accounts.rotate_session_key(conn, user_id)
    return codes


def regenerate_recovery_codes(conn: sqlite3.Connection, user_id: int) -> list[str]:
    """Replace every recovery code of a confirmed credential; return the new ones."""
    if not has_confirmed_credential(conn, user_id):
        raise LookupError("No confirmed TOTP for this account.")
    return _replace_recovery_codes(conn, user_id)


def reset(conn: sqlite3.Connection, user_id: int) -> bool:
    """Remove the credential and its recovery codes and rotate the session key.

    Returns whether a credential existed. Every existing session and pending sign-in
    of the account stops working.
    """
    conn.execute("DELETE FROM totp_recovery_codes WHERE user_id = ?", (user_id,))
    existed = conn.execute("DELETE FROM totp_credentials WHERE user_id = ?", (user_id,)).rowcount == 1
    accounts.rotate_session_key(conn, user_id)
    return existed


def unused_recovery_code_count(conn: sqlite3.Connection, user_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM totp_recovery_codes WHERE user_id = ? AND used_at IS NULL", (user_id,)
    ).fetchone()[0]


# --------------------------------------------------------------------------- sign-in


def pending_is_spent(row: dict, pending_since: str) -> bool:
    """Has a second factor completed for this account since the pending sign-in began?"""
    return row["last_verified_at"] is not None and row["last_verified_at"] >= pending_since


def verify_totp(conn: sqlite3.Connection, user_id: int, key, code: str, now: datetime,
                pending_since: str) -> bool:
    """Accept a normalized TOTP code for a pending sign-in started at ``pending_since``.

    On success the step becomes ``last_used_step`` in the same conditional UPDATE that
    checks it, so two concurrent requests cannot both use one step, and the pending
    sign-in is spent. Raises :class:`TotpKeyError` / :class:`TotpSecretError` when the
    secret is unavailable; the caller fails closed.
    """
    row = credential(conn, user_id)
    if row is None or row["confirmed_at"] is None or pending_is_spent(row, pending_since):
        return False
    step = _matching_step(_decrypt(key, row["secret_ciphertext"]), code, now, row["last_used_step"])
    if step is None:
        return False
    cursor = conn.execute(
        "UPDATE totp_credentials SET last_used_step = ?, last_verified_at = ?, "
        "updated_at = CURRENT_TIMESTAMP "
        "WHERE user_id = ? AND confirmed_at IS NOT NULL "
        "AND (last_used_step IS NULL OR last_used_step < ?) "
        "AND (last_verified_at IS NULL OR last_verified_at < ?)",
        (step, timestamp(now), user_id, step, pending_since),
    )
    return cursor.rowcount == 1


def _unused_recovery_codes(conn: sqlite3.Connection, user_id: int) -> list[tuple[int, str]]:
    return [
        (row[0], row[1])
        for row in conn.execute(
            "SELECT id, code_hash FROM totp_recovery_codes WHERE user_id = ? AND used_at IS NULL "
            "ORDER BY id",
            (user_id,),
        )
    ]


def verify_recovery_code(conn: sqlite3.Connection, user_id: int, code: str, now: datetime,
                         pending_since: str) -> bool:
    """Consume a normalized recovery code for a pending sign-in started at ``pending_since``.

    Needs no TOTP key. The code is marked used, and the pending sign-in spent, by
    conditional UPDATEs; ``False`` after the first one means the caller must roll back.
    """
    row = credential(conn, user_id)
    if row is None or row["confirmed_at"] is None or pending_is_spent(row, pending_since):
        return False
    matched_id = None
    for code_id, code_hash in _unused_recovery_codes(conn, user_id):
        if check_password_hash(code_hash, code):
            matched_id = code_id
            break
    if matched_id is None:
        return False
    used = conn.execute(
        "UPDATE totp_recovery_codes SET used_at = ? WHERE id = ? AND user_id = ? AND used_at IS NULL",
        (timestamp(now), matched_id, user_id),
    ).rowcount == 1
    if not used:
        return False
    return conn.execute(
        "UPDATE totp_credentials SET last_verified_at = ?, updated_at = CURRENT_TIMESTAMP "
        "WHERE user_id = ? AND confirmed_at IS NOT NULL "
        "AND (last_verified_at IS NULL OR last_verified_at < ?)",
        (timestamp(now), user_id, pending_since),
    ).rowcount == 1
