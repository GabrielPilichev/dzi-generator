"""Explicit route access policy (roadmap PRs D1/D2).

Authorization only: given who the current session is, may it use a route?
Authentication (logins, logout, session creation) stays in ``web.app``.
Request integrity (same-origin checks for unsafe methods) is declared
separately in ``web.request_integrity``. DZI content identity
(``web.services.dzi.is_dzi_question``) is not an access decision.

Policies attach to view functions, not endpoint strings, so legacy aliases and
blueprint-prefixed endpoint names resolve to the same policy. An endpoint
without a declared policy is denied.

These declarations are the only route-level authorization: the
``enforce_access_policy`` before-request hook in ``web.app`` applies them to the
resolved view (D2 removed the old endpoint-prefix guard).

This module must not import ``web.app``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from flask import g, session

# Access levels in use today.
PUBLIC = "public"
TEACHER = "teacher"  # the tester session (or an admin session) satisfies it
ADMIN = "admin"
# Any active account. A legacy tester/admin session is not an account.
SIGNED_IN = "signed_in"
# Object-level: any session reaches the view, which then checks attempt
# ownership itself and answers 404 (not 403) so attempt ids cannot be probed.
ATTEMPT_OWNER = "attempt_owner"
# Special case kept from the old guard: switching to the admin UI profile
# needs an admin session; every other target is public.
PROFILE_SWITCH = "profile_switch"

ACCESS_LEVELS = frozenset({PUBLIC, TEACHER, ADMIN, SIGNED_IN, ATTEMPT_OWNER, PROFILE_SWITCH})

# Flask's built-in static view cannot be decorated; it is public.
STATIC_ENDPOINT = "static"

_POLICY_ATTRIBUTE = "access_policy"


@dataclass(frozen=True)
class Principal:
    """Who the current session is, as far as authorization is concerned.

    ``user_id``, ``role`` and ``display_name`` are set only for an account-backed
    principal; the legacy tester/admin session has none of them.
    """

    is_teacher: bool
    is_admin: bool
    user_id: int | None = None
    role: str | None = None
    display_name: str | None = None


ANONYMOUS = Principal(is_teacher=False, is_admin=False)

# Capability of each account role (is_teacher, is_admin).
_ROLE_CAPABILITIES = {
    "student": (False, False),
    "teacher": (True, False),
    "admin": (True, True),
}


def account_principal(user_id: int, role: str, display_name: str) -> Principal:
    """The principal of a loaded account row. An unknown role gets no capability."""
    capabilities = _ROLE_CAPABILITIES.get(role)
    if capabilities is None:
        return ANONYMOUS
    is_teacher, is_admin = capabilities
    return Principal(
        is_teacher=is_teacher,
        is_admin=is_admin,
        user_id=user_id,
        role=role,
        display_name=display_name,
    )


def principal_from_session(session_state: Mapping) -> Principal:
    """The only place access decisions read session keys.

    ``ui_profile`` is a cosmetic display choice and never grants access.
    """
    is_admin = bool(session_state.get("admin_authenticated"))
    is_teacher = bool(session_state.get("tester_authenticated")) or is_admin
    return Principal(is_teacher=is_teacher, is_admin=is_admin)


def current_principal() -> Principal:
    """The request's principal.

    ``g.principal`` is set by ``web.app``'s ``load_current_user`` hook whenever the
    session carried an account identity (valid, or ``ANONYMOUS`` when stale), and
    then wins over any legacy key. Without it the legacy session keys apply.
    """
    if "principal" in g:
        return g.principal
    return principal_from_session(session)


def access_policy(level: str):
    """Declare who may use a view function. Place it below ``@app.route``."""
    if level not in ACCESS_LEVELS:
        raise ValueError(f"Unknown access level: {level!r}")

    def decorate(view_func):
        setattr(view_func, _POLICY_ATTRIBUTE, level)
        return view_func

    return decorate


def policy_for_view(view_func) -> str | None:
    return getattr(view_func, _POLICY_ATTRIBUTE, None)


def policy_for_endpoint(app, endpoint: str | None) -> str | None:
    if endpoint == STATIC_ENDPOINT:
        return PUBLIC
    view_func = app.view_functions.get(endpoint or "")
    if view_func is None:
        return None
    return policy_for_view(view_func)


def _required_level(level: str | None, view_args: Mapping | None) -> str | None:
    if level == PROFILE_SWITCH:
        target = (view_args or {}).get("profile")
        return ADMIN if target == "admin" else PUBLIC
    return level


def is_route_allowed(level: str | None, principal: Principal, view_args: Mapping | None = None) -> bool:
    """Route-level decision. An undeclared or unknown policy is denied."""
    required = _required_level(level, view_args)
    if required in (PUBLIC, ATTEMPT_OWNER):
        return True
    if required == TEACHER:
        return principal.is_teacher
    if required == ADMIN:
        return principal.is_admin
    if required == SIGNED_IN:
        return principal.user_id is not None
    return False


def denial_login_endpoint(level: str | None, view_args: Mapping | None = None) -> str:
    """Where a denied request is sent to sign in: tester login for teacher
    routes, the account login for account-only routes, admin login for
    everything else (including undeclared routes)."""
    required = _required_level(level, view_args)
    if required == SIGNED_IN:
        return "account.login"
    return "auth.tester_login" if required == TEACHER else "auth.admin_login"
