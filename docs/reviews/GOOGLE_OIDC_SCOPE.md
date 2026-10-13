# Google OIDC sign-in

## Prerequisites
Merged PR #329: SameSite=Lax cookies.
Merged PR #330: external identity storage, migration 019.
Merged PR #332: single-use consumption storage (decision 1 below), migration
020 and web/services/oidc_consumption.py. Merged on main at 008c236; not
deployed. It is integrated in this branch: the reconciliation with main is
complete at b5a30b6. The implementation (uncommitted) calls it.
The storage is specified by what PR #332 merged:
docs/reviews/OIDC_CONSUMPTION_SCOPE.md, ACCOUNTS_DESIGN.md §5 and §13, and
DEPLOYMENT.md §28.

## Status
The implementation exists in the uncommitted working tree of this branch. It
is not committed, merged, deployed or activated, and the activation gate
(PRODUCTION_ACTIVATION) is False. An independent implementation review found
no code blockers; the documentation corrections it asked for were made
afterwards and have not themselves been independently reviewed (design 0.6).
The deployment probes are written in DEPLOYMENT.md §29 and have not been run.
The roadmap is updated separately, in PR #333.
Follow ACCOUNTS_DESIGN.md §13's approved session-bound design, as amended
there on 2026-10-11.

## Product decision
Gabriel approved automatic student onboarding:
- First-time Google sign-in may create a student account.
- Existing students and teachers may explicitly link Google while signed in.
- Existing roles remain unchanged; no teacher/admin self-registration.
- Admin authentication remains password plus TOTP; no Google linking or sign-in.
- Matching email never authenticates or automatically links an existing account.
- Email collisions direct the user to sign in through the existing method
  and explicitly link Google.

## Decisions approved after the design review (Gabriel, 2026-10-11)
1. Consumption storage. Transactions are literally single-use. State, nonce,
   PKCE verifier and browser binding stay in the signed session. A dedicated
   server-side consumption table records use: hash-only identifiers, written
   for every matched callback before any other callback parameter is examined
   and before outbound calls, persisting whatever follows; duplicates and
   storage errors fail closed. It is delivered by a separate prerequisite PR
   (migration, classification, caller-owned service; since merged as
   PR #332). login_throttle is not used for it. This amends §13's "no table"
   sentence without adopting a server-side OAuth transaction store.
2. Linking is confirmed after Google returns. Starting a link asks for no
   password. After the ID token is validated, a confirmation page shows the
   Google identity and the LearnPilot account; the identity is stored only
   after the current LearnPilot password is given on a same-origin POST, with
   the existing account throttle, and after account, role, active state,
   session key, issuance binding, expiry and identity ownership are checked
   again inside the write transaction. The confirmation is single-use in its
   own right. An account without a password cannot add an identity in this
   release.
3. Google-created accounts keep users.email NULL. The operator CLI selects by
   --id for disable, enable, sign-out and set-role, with mutually exclusive
   selectors and the account id in its output. A Google-only account cannot
   yet add a local password or another provider identity.
4. The email collision rule covers every usable provider email, verified or
   not. An already linked identity is resolved by issuer and subject first.
   An unusable email is omitted; it is never an identifier or an account
   email.
5. Operator recovery for mistaken links is required before the provider is
   activated. It is a separate follow-up milestone with its own design; this
   PR implements no unlinking. Activation stays blocked until it exists.
6. One enforced total network budget for the provider calls of a callback,
   covering name resolution, connections, response bodies and a signing-key
   fetch. Abandoned work is bounded, cannot change accounts, sessions or
   request state, and its late result is discarded.

## Decisions approved by Gabriel (recorded 2026-10-12)
These were the pending choices P1-P4, T5 and M1-M3 (design 0.2, 0.5).
Gabriel approved each of them. They approve neither the implementation nor
production activation.
- P1: a link is refused when the email Google reports belongs to another
  LearnPilot account.
- P2: the build-time activation gate, as proposed: a constant in the build.
  While it is off, production starts normally, Google sign-in stays disabled
  and one warning is logged. It never refuses to start merely because
  credentials are present.
- P3: sign-out keeps redirecting to the home page.
- P4: „Ученик“ is the display name when Google supplies no usable name for a
  student.
- T5: set-role --role admin is refused for an account without a password.
- M1: a cancellation whose outcome is not established answers with the code
  link_not_pending and the text „Няма свързване с Google, което тази заявка
  да прекрати.“ That is the case when the session held nothing, or the
  object had already been used, had expired or could not be accepted.
- M2: a cancellation that finds the account already linked to Google answers
  with the code account_linked and the text „Профилът ти вече е свързан с
  Google.“
- M3: a cancellation that could not be recorded, or whose check of the
  account failed, answers with the code cancel_unconfirmed and the text „Не
  успяхме да потвърдим отказа.“
Unchanged by these: in none of the three cases does the route report a
completed cancellation (design 1.6); the operator CLI's --id selection
(decision 3); automatic student onboarding.
Each message describes its own request, or the account as the route read it.
None is a statement about later requests. Two orderings are documented
limits: a callback of the same browser that answers after the cancellation
can set a pending link again, and another session of the account can link
it after the cancellation's read (design 1.6, "Two limits").

## Implementation scope
- Authlib authorization-code flow with PKCE S256, state and nonce.
- Validate ID-token signature, issuer, audience and time claims, with finite
  integer times and strict aud/azp.
- Session-bound transaction containing state, nonce, PKCE verifier,
  validated return target, expiry and explicit sign-in/link intent.
- Remove transaction state on callback success or failure, and record its
  use in the consumption table (decision 1).
- GET callback declares OAUTH_STATE; initiation, linking, confirmation and
  cancellation require same-origin POSTs. Ordinary unsafe-request protection
  remains intact. A callback that does not match leaves the stored
  transaction alone.
- Exact configured callback URI derived from DZI_PUBLIC_HOST.
- Select and document one stable Google issuer representation.
- Create the student and identity atomically; handle concurrent callbacks
  without orphan accounts or identity reassignment.
- Linking by confirmation (decision 2). It remains bound to the initiating
  active account and session key. Expiry and revocation invalidate it for
  every copy of the cookie. Cancellation, sign-out and account switching
  remove it from the browser and record its end on the server. When that
  record cannot be written, the result says so: cancellation reports a
  failure and never a completed cancellation; sign-out and account switching
  go on. A captured copy then stays usable until its expiry, and confirming
  it still needs the account's password on an unchanged account and session
  key (design 4.4).
- A cancellation reports a completed cancellation only when its own record
  ended the link and the account had no Google identity when the route read
  it; it reads the account, once, to know that. A link that was already
  completed, an object that
  had already been used and an empty session are never reported as
  cancelled, and neither is a cancellation whose check of the account
  failed. Retiring a transaction does not invalidate a pending link that a
  copy of the cookie already obtained from it; that pending link stays
  behind the password, the account and session-key binding, the issuing run
  and its expiry (design 1.6, 4.4).
- Preserve local password sign-in, admin TOTP, account ownership,
  session security and confined return redirects.
- Provider disabled by default without configured credentials, and not
  activated in production until recovery tooling exists (decision 5; the
  mechanism is the approved activation gate, P2).
- Defined handling of provider redirects and malformed responses; no retry of
  the code exchange; the network budget of decision 6.
- No authorization code, state, token or credential in Nginx, Gunicorn or
  application logs, error responses and error logs included.
- Operator CLI selection by --id (decision 3).
- Bulgarian login/linking UI and deployment/configuration instructions,
  including the warning that signing out of LearnPilot does not sign out of
  Google.
- Update privacy documentation for external identity metadata.
- Isolated tests covering real token-validation logic and adversarial flows.

## Design
docs/reviews/GOOGLE_OIDC_DESIGN.md (revision 2.3 with the corrections of its
review) and its companion docs/reviews/GOOGLE_OIDC_TEST_PLAN.md. The design
specifies this PR and the acceptance criteria of the recovery milestone. The
storage prerequisite is specified by what PR #332 merged; it was reviewed
separately, and that review does not approve this design.
Revision 2.2 specified what a retirement that cannot be recorded does and
may claim (design 4.4). Its review asked for one required change, which
revision 2.3 makes: a cancellation no longer reports a cancellation for a
link that was completed or an object that had already been used (design
1.6, 4.4).
Revision 2.3 was reviewed independently: approve with non-blocking notes,
for the technical specification only. That review approved no product
decision (P1-P4, T5, M1-M3; Gabriel approved those afterwards) and no
deployment check.
Its seven notes are corrected in the design and the test plan (design 0.3,
last table). Three of those corrections go beyond wording and are named for
a focused review (design 0.4): the slot rule of 5.7, the control request of
the direct log probe in 8.7, and the note on a read-only database in
section 9.
A Codex review of those corrections returned approve with non-blocking
notes, for the technical documentation only. Three wording corrections were
made after it (design 9; design 8.7, twice; listed in design 0.3) and have
not been independently reviewed.
Gabriel has since approved P1-P4, T5 and M1-M3 (above). The recording of
those decisions in the three documents has not been independently reviewed.
Production activation remains unapproved.
Do not claim cookie deletion alone prevents replay of an older signed cookie.
Flag any conflict with the approved architecture before changing it.

## Activation prerequisites (none of them done)
Migration 020 deployed (PR #332 is merged and integrated here, not deployed); recovery milestone
delivered; privacy-policy page and the data-controller questions for minors
settled; log changes checked on the server; real-provider smoke checks.
The server log check includes two malformed-body probes. They are specified
in design 8.7 and have not been run: exact raw bytes, synthetic markers
only, no real email, password, cookie, state or other credential, a short
client timeout, one run of each inside the deployment window, and stated
pass criteria with every other result not a pass. They are never repeated
with a real state, and they are now in DEPLOYMENT.md section 29, still unrun.
Three settings are required assumptions of the log statements (design 8.4):
Gunicorn bound to loopback, the sync worker class with one thread, and the
request-wide body limit. Tests pin all three. The first two are also facts
about the server, which the log check verifies; neither has been verified
there.

Added by the implementation review (design 0.6): real provider responses must
be compatible with the identity encoding; the google_oidc_production and
google_oidc_service tests must run against the dependency versions the server
actually installs; installation from the declared requirements must be
validated in a clean environment (the tests so far used assembled existing
packages and did not validate the dependency floors); the Google button
branding must be checked. Open decision before activation: whether outcome
messages from URL codes should be bound to the session (they change no
account). Documented limits: an abandoned worker's lifetime is not bounded by
the response deadline; development-server logs can expose callback query
strings, unlike the reviewed production logging configuration.

## Exclusions
Microsoft, Apple, open password registration, teacher self-registration,
admin OAuth, unlinking and identity recovery (follow-up milestone), billing,
entitlements, DZI gating, question imports, migrations (the consumption table
arrived through PR #332), provider activation and production deployment.

## Execution
Claude Local Opus 5.5, Extra High reasoning.
Independent security review in a separate Claude session.
Claude must not stage, commit, push, create PRs or merge.
Tests use temporary databases and mocked provider network responses.
