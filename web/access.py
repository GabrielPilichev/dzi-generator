"""Explicit route access policy (roadmap PR D1).

Authorization only: given who the current session is, may it use a route?
Authentication (logins, logout, session creation) stays in ``web.app``.
Request integrity (same-origin checks for unsafe methods) is declared
separately in ``web.request_integrity``. DZI content identity
(``web.services.dzi.is_dzi_question``) is not an access decision.

Policies attach to view functions, not endpoint strings, so legacy aliases and
blueprint-prefixed endpoint names resolve to the same policy. An endpoint
without a declared policy is denied.

D1 declares these policies beside ``protect_admin_routes`` in ``web.app``,
which still enforces access; tests assert both agree for every endpoint. PR D2
replaces the prefix guard with this module.

This module must not import ``web.app``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from flask import session

# Access levels in use today.
PUBLIC = "public"
TEACHER = "teacher"  # the tester session (or an admin session) satisfies it
ADMIN = "admin"
# Object-level: any session reaches the view, which then checks attempt
# ownership itself and answers 404 (not 403) so attempt ids cannot be probed.
ATTEMPT_OWNER = "attempt_owner"
# Special case kept from the old guard: switching to the admin UI profile
# needs an admin session; every other target is public.
PROFILE_SWITCH = "profile_switch"

ACCESS_LEVELS = frozenset({PUBLIC, TEACHER, ADMIN, ATTEMPT_OWNER, PROFILE_SWITCH})

# Flask's built-in static view cannot be decorated; it is public.
STATIC_ENDPOINT = "static"

_POLICY_ATTRIBUTE = "access_policy"


@dataclass(frozen=True)
class Principal:
    """Who the current session is, as far as authorization is concerned."""

    is_teacher: bool
    is_admin: bool


ANONYMOUS = Principal(is_teacher=False, is_admin=False)


def principal_from_session(session_state: Mapping) -> Principal:
    """The only place access decisions read session keys.

    ``ui_profile`` is a cosmetic display choice and never grants access.
    """
    is_admin = bool(session_state.get("admin_authenticated"))
    is_teacher = bool(session_state.get("tester_authenticated")) or is_admin
    return Principal(is_teacher=is_teacher, is_admin=is_admin)


def current_principal() -> Principal:
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
    return False


def denial_login_endpoint(level: str | None, view_args: Mapping | None = None) -> str:
    """Where a denied request is sent to sign in: tester login for teacher
    routes, admin login for everything else (including undeclared routes)."""
    return "tester_login" if _required_level(level, view_args) == TEACHER else "admin_login"
