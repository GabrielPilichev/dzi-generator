"""Request-integrity declarations (roadmap PRs D1/D2).

Request integrity answers how an unsafe or external request proves it is
legitimate. It is not authorization: a route's access policy
(``web.access``) never exempts it from an integrity check, and an integrity
mechanism never grants access.

Every view that accepts an unsafe method declares its mechanism here. The
same-origin check itself is ``protect_same_origin_unsafe_methods`` in
``web.app``; it applies to every unsafe request, runs before the access check,
and has no route exemptions.

This module must not import ``web.app``.
"""

from __future__ import annotations

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Ordinary browser form posts: Origin (or Referer) must match this host.
SAME_ORIGIN = "same_origin"
# Reserved for future external flows. No route may use them until their
# verifiers exist; they are integrity mechanisms, not access levels.
OAUTH_STATE = "oauth_state"
PROVIDER_SIGNATURE = "provider_signature"

INTEGRITY_POLICIES = frozenset({SAME_ORIGIN, OAUTH_STATE, PROVIDER_SIGNATURE})

_INTEGRITY_ATTRIBUTE = "request_integrity_policy"


def integrity_policy(mechanism: str):
    """Declare how unsafe requests to a view prove legitimacy. Place it below
    ``@app.route``."""
    if mechanism not in INTEGRITY_POLICIES:
        raise ValueError(f"Unknown request-integrity policy: {mechanism!r}")

    def decorate(view_func):
        setattr(view_func, _INTEGRITY_ATTRIBUTE, mechanism)
        return view_func

    return decorate


def integrity_policy_for_view(view_func) -> str | None:
    return getattr(view_func, _INTEGRITY_ATTRIBUTE, None)
