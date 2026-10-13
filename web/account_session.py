"""The account session: the three functions that replace or clear it.

``uid``, ``sk`` and ``iat`` are the whole identity of a signed-in account; role, name
and active state stay in the ``users`` row and are loaded per request by ``web.app``.
No other code clears the session.

Each of the three first tries to retire linking in progress
(:func:`web.oauth_state.retire_linking`): a Google link that was started in this
session must not stay usable in a copy of the cookie after a sign-out or a change of
account. None of them reads what that attempt achieved. A sign-in, a password change
and a sign-out go on whether or not the record could be written, and they claim
nothing about the link. Each is called only after its caller's own transaction on the
request's connection has ended.

A neutral module: the account and the Google sign-in controllers both use it, and no
controller imports another.
"""

from __future__ import annotations

import time

from flask import current_app, g, session

from web import oauth_state
from web.oauth_state import retire_linking
from web.services import login_throttle, oidc_accounts

ATTEMPT_OWNER_KEY_PREFIX = "quiz_attempt_owner:"

# Where a sign-in lands without a usable ``next`` (design §14).
ROLE_HOME_ENDPOINTS = {
    "admin": "teacher.teacher_dashboard",
    "teacher": "teacher.teacher_new",
    "student": "browsing.index",
}
DEFAULT_HOME_ENDPOINT = "browsing.index"


def role_home(role) -> str:
    """The endpoint an account of ``role`` lands on."""
    return ROLE_HOME_ENDPOINTS.get(role, DEFAULT_HOME_ENDPOINT)


def throttle_key(identifier: str) -> str:
    return login_throttle.identifier_hash(current_app.config["SECRET_KEY"], identifier)


def account_throttle_key(user: dict) -> str:
    """The counter of an account: the one its email is throttled under on the login form."""
    return throttle_key(user["email"] or f"\0uid\0{user['id']}")


def loaded_account() -> dict | None:
    """The ``users`` row behind the request's principal, as the loader read it.

    ``None`` for an anonymous request. It never holds the password hash;
    ``has_password`` tells whether a credential exists.
    """
    return g.get("account_user")


def link_binding(principal) -> dict:
    """What a Google link started now is bound to: this account session.

    The account's id, a digest of the session key (so the key is not in the cookie
    twice) and the ``iat`` of the session that starts the link.
    """
    return {
        "uid": principal.user_id,
        "skd": oidc_accounts.session_key_digest(current_app.config["SECRET_KEY"], session["sk"]),
        "siat": session["iat"],
    }


def bound_to_this_session(obj: dict, principal) -> bool:
    """Is a link transaction or a pending link bound to the session of this request?

    The first validation of a link: a signed-in student or teacher, whose id, session
    ``iat`` and session key are the ones the object was bound to. The principal was
    loaded from the row, so the account is active and its session is within its
    absolute lifetime.
    """
    session_key = session.get("sk")
    return (
        principal.user_id is not None
        and principal.role in oidc_accounts.LINKABLE_ROLES
        and obj.get("uid") == principal.user_id
        and obj.get("siat") == session.get("iat")
        and isinstance(session_key, str)
        and oidc_accounts.digests_match(
            oidc_accounts.session_key_digest(current_app.config["SECRET_KEY"], session_key), obj.get("skd"))
    )


def pending_link_of_this_session(principal) -> dict | None:
    """The session's pending link if it is live and bound to this session, else ``None``.

    A first look, for a page: the expiry and the account that bind are checked again
    under the write lock when the link is confirmed.
    """
    pending = oauth_state.live_pending_link()
    if pending is None or not bound_to_this_session(pending, principal):
        return None
    return pending


def _carried_markers() -> dict:
    return {key: value for key, value in session.items() if key.startswith(ATTEMPT_OWNER_KEY_PREFIX)}


def issue_account_session(user_id: int, session_key: str, issued_at: int | None = None) -> None:
    """Replace the session with the account identity.

    Everything set before sign-in is dropped; only the attempt-ownership markers
    carry over. ``issued_at`` is kept
    when a signed-in session is re-keyed, so re-keying never extends its lifetime.
    """
    retire_linking()
    carried = _carried_markers()
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
    retire_linking()
    carried = _carried_markers()
    session.clear()
    session.update(carried)
    session["mfa_uid"] = user_id
    session["mfa_sk"] = session_key
    session["mfa_iat"] = issued_at


def end_session() -> None:
    """Sign this browser out: the whole session goes, the attempt markers included."""
    retire_linking()
    session.clear()
