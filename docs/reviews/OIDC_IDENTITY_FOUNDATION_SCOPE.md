# OIDC identity foundation

Prerequisite: merged SameSite=Lax PR #329.

## Approved design
Follow docs/ACCOUNTS_DESIGN.md sections 5 and 13.
External identities are keyed by exact issuer + subject, never email.

## Implementation scope
- Add the next available numbered user_identities migration.
- Follow the approved schema and UNIQUE (issuer, subject) constraint.
- Classify user_identities as runtime data.
- Add a small storage service following existing accounts-service conventions:
  explicit connections and caller-owned transactions.
- Preserve identity ownership; never silently reassign an existing identity.
- No automatic linking or sign-in through matching email.
- Update schema/classification manifest metadata as required, preserving
  canonical content rows and hashes.
- Test migration integrity, foreign keys, identity uniqueness, transaction
  rollback, runtime exclusion, and preservation of existing accounts.
- No HTTP route, authentication, session, role, or UI behavior changes.

## Exclusions
No provider callbacks, Authlib dependency, registration, linking UI,
account creation policy, unlinking, OAuth transaction table, real database
migration, deployment, question imports, or unrelated refactoring.

## Execution
Claude Local Opus 5.5, Extra High reasoning.
Independent review in a separate Claude session.
Claude must not stage, commit, push, create PRs, or merge.
Tests use isolated temporary databases only.
