# Phase 2: SameSite=Lax session cookie — scope and verification

This is a prerequisite for Google/Microsoft OIDC, not an OIDC implementation.

## Existing behavior

- `web/app.py` currently sets `SESSION_COOKIE_SAMESITE = "Strict"`.
- Account login, TOTP, POST-only logout, account-owned attempts and authorization exist.
- Request-integrity enforcement rejects unsafe cross-origin requests by checking Origin or Referer. `web/request_integrity.py` reserves OAUTH_STATE but has no OAuth verifier yet.
- Session cookies are HttpOnly and Secure in production. Local HTTP smoke tests may use `DZI_SESSION_COOKIE_SECURE=0`.
- `docs/ACCOUNTS_DESIGN.md` §6 and §13 explicitly sequence a standalone SameSite=Lax PR before OIDC.

## Implementation limits

- Switch the normal session cookie to `SameSite=Lax` while preserving `Secure`, `HttpOnly`, session expiry, CSRF controls and login/TOTP session semantics.
- Update relevant tests and current configuration/contract documentation; remove obsolete expectations of Strict, but preserve historical notes where marked as historical.
- Do not add any Google/Microsoft login code, OAuth callback, new cookie, migrations, access changes, registration or billing.
- Audit GET endpoints for accidental state-changing behavior under cross-site top-level navigation; if safety issues appear, stop and report rather than silently broadening the PR.
- Check that same-origin enforcement rejects cross-origin POSTs to existing state-changing endpoints (including logout), without relying on SameSite alone.
- Test cookie attributes from actual Set-Cookie headers for student, teacher and admin account sign-in, including TOTP completion; verify that anonymous public navigation still does not set a session cookie and signed-in GET navigation does not rewrite one.
- Preserve local dev behavior under plain HTTP without weakening production's Secure default.

## Verification and release

Codex implements; Claude independently reviews Codex's committed changes. Both should verify tests, security invariants, URL and behavior parity, and whether a cross-site top-level GET can cause any unexpected state change.

No deployment, DB writes/imports, migrations or server/tunnel operations. Existing production is unchanged until a separately approved deployment.
