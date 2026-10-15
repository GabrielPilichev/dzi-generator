"""Accounts and Google identities: sign-in, onboarding and linking
(docs/reviews/GOOGLE_OIDC_DESIGN.md, sections 4 and 6).

Independent of Flask, ``web.app`` and the blueprints. Every function takes an open
``sqlite3`` connection and never commits or rolls back: the caller owns the
transaction, and for every write here that transaction was begun with ``BEGIN
IMMEDIATE``. ``web.services.identities`` stays the storage of identities and has no
``users`` write; this module is where a ``users`` row and an identity meet.

* An account is found by ``(issuer, subject)`` and by nothing else. An email never
  selects, signs in or links an account.
* The email and the name Google reports are metadata. Nothing about them can fail a
  sign-in, and neither is invented, truncated into an identifier or refreshed later.
* Only a ``student`` is ever created here. An admin never signs in through Google.

Subjects, emails and names are never logged or put into exception messages.
"""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
import unicodedata
from dataclasses import dataclass

from web.services import accounts, google_oidc, identities, oidc_consumption

# Outcomes. The same words are the ``google`` codes the pages show a message for.
LINKED = "linked"
ALREADY_LINKED = "already_linked"
IDENTITY_TAKEN = "identity_taken"
NOT_ALLOWED = "not_allowed"
EMAIL_EXISTS = "email_exists"
EMAIL_OTHER_ACCOUNT = "email_other_account"
EXPIRED = "expired"
FAILED = "failed"
# ``sign_in_or_onboard`` only: which of the two a successful sign-in was.
SIGNED_IN = "signed_in"
CREATED = "created"

# The display name of a student whose Google profile supplies no usable name.
DEFAULT_STUDENT_NAME = "Ученик"
# The roles that may link Google or sign in through it.
LINKABLE_ROLES = ("student", "teacher")

_LINK_BINDING_PREFIX = b"oidc-link\0"


@dataclass(frozen=True, repr=False)
class GoogleProfile:
    """A verified Google identity as this module takes it.

    ``subject`` is the verified ``sub``. ``email`` and ``name`` are the claims as
    Google reported them, or ``None``; the rules below decide what becomes of them.
    The issuer is always ``google_oidc.ISSUER``.
    """

    subject: str
    email: str | None = None
    name: str | None = None


# --------------------------------------------------------------------------- email and name


def comparable_email(value) -> str | None:
    """The normalized form the collision rule compares, or ``None``.

    Comparable: a string of at most 254 characters that ``accounts.normalize_email``
    accepts. Whether Google marked the address verified plays no part.
    """
    if not isinstance(value, str) or len(value) > accounts.MAX_EMAIL_LENGTH:
        return None
    try:
        return accounts.normalize_email(value)
    except ValueError:
        return None


def storable_email(value) -> str | None:
    """What ``email_at_link`` holds: the address as Google reported it, or ``None``.

    Storable: comparable, and the reported value itself passes the rule of the
    identity storage for that column (printable, no whitespace).
    """
    if comparable_email(value) is None:
        return None
    if not value.isprintable() or any(ch.isspace() for ch in value):
        return None
    return value


def reported_email(value) -> str:
    """The address as Google reported it when it is comparable, else ``""``.

    What a pending link carries: the confirmation page shows it, and the collision
    rule and ``email_at_link`` are derived from it again at the write.
    """
    return value if comparable_email(value) is not None else ""


def cleaned_name(value) -> str:
    """A reported name without control and format characters, at most 80 characters.

    ``""`` when Google reported no string. Characters of Unicode category C (control,
    format, the bidirectional overrides among them) are removed, whitespace is
    collapsed, and a longer name is cut.
    """
    if not isinstance(value, str):
        return ""
    visible = "".join(ch for ch in value if not unicodedata.category(ch).startswith("C"))
    name = " ".join(visible.split())
    if len(name) > accounts.MAX_DISPLAY_NAME_LENGTH:
        name = name[: accounts.MAX_DISPLAY_NAME_LENGTH].strip()
    return name


def student_display_name(value) -> str:
    """The display name of an account created through Google."""
    name = cleaned_name(value)
    return name if len(name) >= accounts.MIN_DISPLAY_NAME_LENGTH else DEFAULT_STUDENT_NAME


# --------------------------------------------------------------------------- sign-in


@dataclass(frozen=True, repr=False)
class SignIn:
    """``outcome`` is SIGNED_IN or CREATED with the ``user`` row to issue a session
    for, or a refusal (NOT_ALLOWED, EMAIL_EXISTS) with no user.

    ``profile_required`` is true only for CREATED when the caller asked for the mark
    (``require_profile``): the account was written with ``profile_required_at`` set."""

    outcome: str
    user: dict | None = None
    profile_required: bool = False


def sign_in_or_onboard(conn: sqlite3.Connection, profile: GoogleProfile, *,
                       require_profile: bool = False) -> SignIn:
    """Sign in the account of a Google identity, or create a student for a new one.

    The caller holds the write lock and commits after SIGNED_IN or CREATED; after a
    refusal or an exception it rolls back. The user and the identity are written in
    that one transaction, so neither exists without the other.

    ``require_profile`` is the profile gate (docs/reviews/EMAIL_REGISTRATION_DESIGN.md,
    7.4, decision D11). When true, and only for an account created here, the account is
    marked ``profile_required_at`` in the same transaction. An account that is signed in,
    refused or already existing is never marked, whatever the flag says.
    """
    # First, and the email plays no part in it.
    found = identities.find_identity(conn, google_oidc.ISSUER, profile.subject)
    if found is not None:
        user = accounts.get_user_by_id(conn, found["user_id"])
        # Role and active state are the row's, read at the write: a disabled account
        # gets no second account, and an admin signs in with password and TOTP only.
        if user is None or not user["is_active"] or user["role"] not in LINKABLE_ROLES:
            return SignIn(NOT_ALLOWED)
        identities.record_identity_login(conn, found["id"])
        accounts.record_login(conn, user["id"])
        return SignIn(SIGNED_IN, user)

    email = comparable_email(profile.email)
    if email is not None and accounts.get_user_by_email(conn, email) is not None:
        # An account with that sign-in email exists. Nothing is created or linked.
        return SignIn(EMAIL_EXISTS)

    # No email and no password: users.email is the identifier of the local password
    # credential and stays NULL. The role is a literal.
    user_id = accounts.create_user(conn, display_name=student_display_name(profile.name), role="student")
    identity_id = identities.add_identity(
        conn,
        user_id,
        issuer=google_oidc.ISSUER,
        subject=profile.subject,
        provider=google_oidc.PROVIDER,
        email_at_link=storable_email(profile.email),
    )
    identities.record_identity_login(conn, identity_id)
    accounts.record_login(conn, user_id)
    if require_profile:
        accounts.mark_profile_required(conn, user_id)
    return SignIn(CREATED, accounts.get_user_by_id(conn, user_id), profile_required=bool(require_profile))


# --------------------------------------------------------------------------- linking


def session_key_digest(secret_key, session_key: str) -> str:
    """What a link is bound to instead of the session key itself.

    ``HMAC-SHA256(secret_key, "oidc-link\\0" + session_key)`` in hex, so that the key
    is not in the cookie twice.
    """
    key = secret_key if isinstance(secret_key, bytes) else str(secret_key).encode("utf-8")
    return hmac.new(key, _LINK_BINDING_PREFIX + session_key.encode("utf-8"), hashlib.sha256).hexdigest()


def digests_match(left, right) -> bool:
    """Constant-time comparison of two binding digests; ``False`` for anything else."""
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def has_google_identity(conn: sqlite3.Connection, user_id) -> bool:
    return any(row["provider"] == google_oidc.PROVIDER for row in identities.list_identities(conn, user_id))


def bound_account(conn: sqlite3.Connection, uid, skd, siat, *, secret_key, wall_now) -> dict | None:
    """The account a link is bound to, read now, or ``None`` when the binding is gone.

    Refused: a missing, inactive or admin row; a row whose current session key is not
    the one the link was bound to (a password change, a disable, a role change, an
    operator sign-out and a TOTP reset each rotate it); a bound session past its
    absolute lifetime. Read-only.
    """
    if not isinstance(uid, int) or isinstance(uid, bool):
        return None
    user = accounts.get_user_by_id(conn, uid)
    if user is None or not user["is_active"] or user["role"] not in LINKABLE_ROLES:
        return None
    if not user["session_key"] or not digests_match(session_key_digest(secret_key, user["session_key"]), skd):
        return None
    if accounts.session_expired(user["role"], siat, wall_now):
        return None
    return user


def link_decision(conn: sqlite3.Connection, uid: int, profile: GoogleProfile) -> str | None:
    """Why this account may not be linked to this identity, or ``None`` when it may.

    Read-only, and the one function behind both the callback's look and the write, so
    both run the same checks in the same order. The identity comes first, as at
    sign-in.
    """
    found = identities.find_identity(conn, google_oidc.ISSUER, profile.subject)
    if found is not None:
        return ALREADY_LINKED if found["user_id"] == uid else IDENTITY_TAKEN
    user = accounts.get_user_by_id(conn, uid)
    # An account without a password cannot add an identity in this release, and an
    # account has at most one Google identity.
    if user is None or not user["has_password"] or has_google_identity(conn, uid):
        return NOT_ALLOWED
    email = comparable_email(profile.email)
    if email is not None:
        owner = accounts.get_user_by_email(conn, email)
        if owner is not None and owner["id"] != uid:
            return EMAIL_OTHER_ACCOUNT
    return None


def confirm_link(conn: sqlite3.Connection, pending: dict, *, secret_key, session_key, session_iat,
                 now: int, wall_now) -> str:
    """Store the identity of a pending link, once. Returns an outcome.

    Called after the account's password was verified, inside a transaction the caller
    began with ``BEGIN IMMEDIATE``; ``now`` is the caller's one reading of the
    monotonic clock, taken after that statement returned. The caller commits after
    every outcome and rolls back after an exception.

    The record of use comes first and is written with the pending link's own expiry.
    From then on it persists with the caller's commit, so the pending link is spent
    whether or not an identity was stored.
    """
    key = oidc_consumption.consumption_key(secret_key, oidc_consumption.PURPOSE_LINK_CONFIRM, pending["id"])
    profile = GoogleProfile(pending["sub"], pending["email"] or None, pending["name"] or None)
    uid = pending["uid"]
    try:
        oidc_consumption.consume(conn, key, now=now, expires_at=pending["exp"])
    except oidc_consumption.Expired:
        return EXPIRED
    except oidc_consumption.AlreadyConsumed:
        found = identities.find_identity(conn, google_oidc.ISSUER, profile.subject)
        return ALREADY_LINKED if found is not None and found["user_id"] == uid else FAILED

    # The session that presents the link is the one it was bound to, and that session
    # is still the account's: the key, the role and the active state are the row's.
    if session_iat != pending["siat"] or not isinstance(session_key, str):
        return FAILED
    if not digests_match(session_key_digest(secret_key, session_key), pending["skd"]):
        return FAILED
    if bound_account(conn, uid, pending["skd"], pending["siat"], secret_key=secret_key, wall_now=wall_now) is None:
        return FAILED

    refusal = link_decision(conn, uid, profile)
    if refusal is not None:
        return refusal
    identities.add_identity(
        conn,
        uid,
        issuer=google_oidc.ISSUER,
        subject=profile.subject,
        provider=google_oidc.PROVIDER,
        email_at_link=storable_email(profile.email),
    )
    return LINKED
