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

from flask import current_app, g, session

# Access levels in use today.
PUBLIC = "public"
TEACHER = "teacher"  # the tester session (or an admin session) satisfies it
ADMIN = "admin"
# Any active account. A legacy tester/admin session is not an account.
SIGNED_IN = "signed_in"
# Object-level: any session reaches the view, which then checks attempt
# ownership itself and answers 404 (not 403) so attempt ids cannot be probed.
ATTEMPT_OWNER = "attempt_owner"
# Assignment management (Accounts PR7): the route admits an admin or a teacher
# account; the view then loads the assignment and checks it with
# ``can_manage_assignment``, answering 404 so ids cannot be probed. A legacy
# tester session has no account id and is not admitted.
ASSIGNMENT_OWNER = "assignment_owner"
# Special case kept from the old guard: switching to the admin UI profile
# needs an admin session; every other target is public.
PROFILE_SWITCH = "profile_switch"

ACCESS_LEVELS = frozenset({
    PUBLIC, TEACHER, ADMIN, SIGNED_IN, ATTEMPT_OWNER, ASSIGNMENT_OWNER, PROFILE_SWITCH,
})

# Flask's built-in static view cannot be decorated; it is public.
STATIC_ENDPOINT = "static"

_POLICY_ATTRIBUTE = "access_policy"

# The legacy shared-password bridge (Accounts PR9). While it is disabled, account
# authentication is the only authority: the legacy routes answer 404 and the legacy
# session keys are ignored. Production always runs with it disabled
# (``web.production``); a missing configuration value also means disabled.
LEGACY_SHARED_LOGIN_ENV = "DZI_LEGACY_SHARED_LOGIN"
LEGACY_SHARED_LOGIN_CONFIG = "LEGACY_SHARED_LOGIN"
LEGACY_AUTH_SESSION_KEYS = ("tester_authenticated", "admin_authenticated")
_LEGACY_SHARED_LOGIN_ATTRIBUTE = "legacy_shared_login"
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


def parse_legacy_shared_login(raw: str | None) -> bool | None:
    """An explicit ``DZI_LEGACY_SHARED_LOGIN`` value as a boolean.

    ``None`` for an unset or empty value (the caller's default applies). Raises
    ``ValueError`` for any other spelling: a non-empty string is never simply truthy.
    """
    normalized = (raw or "").strip().lower()
    if not normalized:
        return None
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise ValueError(f"{LEGACY_SHARED_LOGIN_ENV} must be one of: 1/true/yes/on or 0/false/no/off")


def legacy_shared_login_enabled() -> bool:
    """Whether the legacy shared-password bridge may grant authority in this app."""
    return current_app.config.get(LEGACY_SHARED_LOGIN_CONFIG) is True


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


def can_list_assignments(principal: Principal) -> bool:
    """Whether the principal may use assignment management at all: an admin, or a
    teacher account. The legacy tester session (teacher capability without an
    account id) may not."""
    return principal.is_admin or (principal.is_teacher and principal.user_id is not None)


def can_manage_assignment(principal: Principal, owner_user_id: int | None) -> bool:
    """Strict assignment-management predicate (edit, duplicate, results, export,
    review of the assignment's answers; practical review reuses it).

    An admin manages every assignment. A teacher account manages only rows it owns.
    Everything else is denied: anonymous, students, the legacy tester and an
    unowned (NULL) row for a teacher. Both ids must be present before they are
    compared, so a missing account id never matches a missing owner.
    Pure: no session, request or database access.
    """
    if principal.is_admin:
        return True
    if not principal.is_teacher or principal.user_id is None or owner_user_id is None:
        return False
    return owner_user_id == principal.user_id


def assignment_management_scope(principal: Principal, owner_column: str) -> tuple[str, tuple]:
    """SQL condition and parameters limiting rows to assignments the principal manages.

    The SQL counterpart of ``can_manage_assignment`` for listings, so filtering
    happens in the query. ``owner_column`` is a fixed column reference chosen by
    the caller (e.g. ``"qa.owner_user_id"``), never user input.
    """
    if principal.is_admin:
        return "1 = 1", ()
    if principal.is_teacher and principal.user_id is not None:
        return f"{owner_column} = ?", (principal.user_id,)
    return "1 = 0", ()


def principal_from_session(session_state: Mapping) -> Principal:
    """The only place access decisions read session keys.

    ``ui_profile`` is a cosmetic display choice and never grants access. These are
    the legacy shared-login keys: ``current_principal`` consults them only while the
    bridge is enabled.
    """
    is_admin = bool(session_state.get("admin_authenticated"))
    is_teacher = bool(session_state.get("tester_authenticated")) or is_admin
    return Principal(is_teacher=is_teacher, is_admin=is_admin)


def current_principal() -> Principal:
    """The request's principal.

    ``g.principal`` is set by ``web.app``'s ``load_current_user`` hook whenever the
    session carried an account identity (valid, or ``ANONYMOUS`` when stale), and
    then wins over any legacy key. Without it the legacy session keys apply, but
    only while the legacy shared-login bridge is enabled: with it disabled a session
    without a resolved account is anonymous whatever else its cookie holds.
    """
    if "principal" in g:
        return g.principal
    if not legacy_shared_login_enabled():
        return ANONYMOUS
    return principal_from_session(session)


def access_policy(level: str):
    """Declare who may use a view function. Place it below ``@app.route``."""
    if level not in ACCESS_LEVELS:
        raise ValueError(f"Unknown access level: {level!r}")

    def decorate(view_func):
        setattr(view_func, _POLICY_ATTRIBUTE, level)
        return view_func

    return decorate


def legacy_shared_login_route(view_func):
    """Mark a view as part of the legacy shared-password bridge.

    ``web.app``'s policy hook answers 404 for it, on every method and before any
    access decision, while the bridge is disabled.
    """
    setattr(view_func, _LEGACY_SHARED_LOGIN_ATTRIBUTE, True)
    return view_func


def is_legacy_shared_login_view(view_func) -> bool:
    return getattr(view_func, _LEGACY_SHARED_LOGIN_ATTRIBUTE, False) is True


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
    if required == ASSIGNMENT_OWNER:
        return can_list_assignments(principal)
    return False


def denial_login_endpoint(
    level: str | None, view_args: Mapping | None = None, *, legacy_shared_login: bool = False
) -> str:
    """Where a denied request is sent to sign in.

    Always the account login unless the legacy bridge is enabled. With the bridge:
    tester login for teacher routes, the account login for account-only routes
    (``signed_in`` and ``assignment_owner``), admin login for everything else
    (including undeclared routes)."""
    required = _required_level(level, view_args)
    if not legacy_shared_login or required in (SIGNED_IN, ASSIGNMENT_OWNER):
        return "account.login"
    return "auth.tester_login" if required == TEACHER else "auth.admin_login"
