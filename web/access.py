"""Explicit route access policy (roadmap PRs D1/D2).

Authorization only: given who the current account is, may it use a route?
Authentication (sign-in, sign-out, session creation) stays in
``web.blueprints.account`` and ``web.app``'s ``load_current_user`` hook.
Request integrity (same-origin checks for unsafe methods) is declared
separately in ``web.request_integrity``. DZI content identity
(``web.services.dzi.is_dzi_question``) is not an access decision.

Policies attach to view functions, not endpoint strings, so aliases and
blueprint-prefixed endpoint names resolve to the same policy. An endpoint
without a declared policy is denied.

These declarations are the only route-level authorization: the
``enforce_access_policy`` before-request hook in ``web.app`` applies them to the
resolved view (D2 removed the old endpoint-prefix guard).

This module must not import ``web.app``.
"""

from __future__ import annotations

from dataclasses import dataclass

from flask import g

# Access levels in use today.
PUBLIC = "public"
TEACHER = "teacher"  # a teacher account (or an admin account) satisfies it
ADMIN = "admin"
# Any active account.
SIGNED_IN = "signed_in"
# Object-level: any session reaches the view, which then checks attempt
# ownership itself and answers 404 (not 403) so attempt ids cannot be probed.
ATTEMPT_OWNER = "attempt_owner"
# Assignment management (Accounts PR7): the route admits an admin or a teacher
# account; the view then loads the assignment and checks it with
# ``can_manage_assignment``, answering 404 so ids cannot be probed.
ASSIGNMENT_OWNER = "assignment_owner"

ACCESS_LEVELS = frozenset({PUBLIC, TEACHER, ADMIN, SIGNED_IN, ATTEMPT_OWNER, ASSIGNMENT_OWNER})

# Flask's built-in static view cannot be decorated; it is public.
STATIC_ENDPOINT = "static"

_POLICY_ATTRIBUTE = "access_policy"


@dataclass(frozen=True)
class Principal:
    """Who the current session is, as far as authorization is concerned.

    ``user_id``, ``role`` and ``display_name`` are set for every principal loaded
    from an account; only ``ANONYMOUS`` has none of them.
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
    teacher account. Teacher capability without an account id is not enough."""
    return principal.is_admin or (principal.is_teacher and principal.user_id is not None)


def can_manage_assignment(principal: Principal, owner_user_id: int | None) -> bool:
    """Strict assignment-management predicate (edit, duplicate, results, export,
    review of the assignment's answers; practical review reuses it).

    An admin manages every assignment. A teacher account manages only rows it owns.
    Everything else is denied: anonymous, students, a principal without an account
    id and an unowned (NULL) row for a teacher. Both ids must be present before
    they are compared, so a missing account id never matches a missing owner.
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


def current_principal() -> Principal:
    """The request's principal.

    ``g.principal`` is set by ``web.app``'s ``load_current_user`` hook whenever the
    session carried an account identity (valid, or ``ANONYMOUS`` when stale). It is
    the only source of authority: a request without it is anonymous whatever else
    its cookie holds.
    """
    if "principal" in g:
        return g.principal
    return ANONYMOUS


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


def is_route_allowed(level: str | None, principal: Principal) -> bool:
    """Route-level decision. An undeclared or unknown policy is denied."""
    if level in (PUBLIC, ATTEMPT_OWNER):
        return True
    if level == TEACHER:
        return principal.is_teacher
    if level == ADMIN:
        return principal.is_admin
    if level == SIGNED_IN:
        return principal.user_id is not None
    if level == ASSIGNMENT_OWNER:
        return can_list_assignments(principal)
    return False
