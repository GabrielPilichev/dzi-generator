# Google OIDC sign-in

## Prerequisites
Merged PR #329: SameSite=Lax cookies.
Merged PR #330: external identity storage, migration 019.
Follow ACCOUNTS_DESIGN.md §13's approved session-bound design.

## Product decision
Gabriel approved automatic student onboarding:
- First-time Google sign-in may create a student account.
- Existing students and teachers may explicitly link Google while signed in.
- Existing roles remain unchanged; no teacher/admin self-registration.
- Admin authentication remains password plus TOTP; no Google linking or sign-in.
- Matching email never authenticates or automatically links an existing account.
- Email collisions direct the user to sign in through the existing method
  and explicitly link Google.

## Implementation scope
- Authlib authorization-code flow with PKCE S256, state and nonce.
- Validate ID-token signature, issuer, audience and time claims.
- Session-bound transaction containing state, nonce, PKCE verifier,
  validated return target, expiry and explicit sign-in/link intent.
- Remove transaction state on callback success or failure.
- GET callback declares OAUTH_STATE; initiation and linking require
  same-origin POSTs. Ordinary unsafe-request protection remains intact.
- Exact configured callback URI derived from DZI_PUBLIC_HOST.
- Select and document one stable Google issuer representation.
- Create the student and identity atomically; handle concurrent callbacks
  without orphan accounts or identity reassignment.
- Linking remains bound to the initiating active account and session key;
  account switching, expiry or revocation invalidates it.
- Preserve local password sign-in, admin TOTP, account ownership,
  session security and confined return redirects.
- Provider disabled by default without configured credentials.
- Bulgarian login/linking UI and deployment/configuration instructions.
- Update privacy documentation for external identity metadata.
- Isolated tests covering real token-validation logic and adversarial flows.

## Design checks before implementation
Resolve and document transaction replay/concurrency guarantees under
signed-cookie sessions, account-switch handling, provider-email/name
handling, request timeouts, throttling, and logging redaction.
Do not claim cookie deletion alone prevents replay of an older signed cookie.
Flag any conflict with the approved architecture before changing it.

## Exclusions
Microsoft, Apple, open password registration, teacher self-registration,
admin OAuth, unlinking, billing, entitlements, DZI gating, question imports,
runtime migrations and production deployment.

## Execution
Claude Local Opus 5.5, Extra High reasoning.
Independent security review in a separate Claude session.
Claude must not stage, commit, push, create PRs or merge.
Tests use temporary databases and mocked provider network responses.
