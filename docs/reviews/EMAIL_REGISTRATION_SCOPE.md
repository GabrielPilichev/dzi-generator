# Verified email/password registration — scope

Design: `docs/reviews/EMAIL_REGISTRATION_DESIGN.md`.
Test plan: `docs/reviews/EMAIL_REGISTRATION_TEST_PLAN.md`.

## Status

Design with **decisions recorded as approved by Gabriel**: D1–D15 and
D18–D20, and the sender behaviour of D17 (design, section 0.2). D16 and the
configuration of D17 are activation prerequisites; no legal conclusion is
recorded. **Activation is not authorized.** The implementation proceeds one PR
at a time (design, section 9). **PR 1** (migration 021, the profile service and
the form validators; storage and validation only, no route) is merged (#335):
`docs/reviews/EMAIL_REGISTRATION_PR1_STATUS.md`. **PR 2** (the mail interface,
the isolated sender script, the backends and settings validation; no caller, no
route, no database change) is merged (#336):
`docs/reviews/EMAIL_REGISTRATION_PR2_STATUS.md`. **PR 3** (the registration
service: pending lifecycle, codes, send ledger, caps, circuit, settlement,
clock policy, cleanup and the activation of student registrations; no route,
no gate, no caller; migration 022 adds one nullable column and one index to
the send ledger) is merged (#337):
`docs/reviews/EMAIL_REGISTRATION_PR3_STATUS.md`. After the first review of
#337 Gabriel approved four contract points, recorded in design 0.7 and
implemented there: one send per generation is the caller's sequencing
(`dispatch` invokes the mailer once per call; PR 4 prevents duplicates; no
retry, send-claim state or exactly-once claim); a stored random issuance
identifier; verification-code input trimmed at the ends only, then six ASCII
digits; settlement by the same issuance identity, and never after
`NotSendable`. **PR 4** (the student routes, templates and browser slot, the
transactions and single dispatch of each issuance, the build gates, kept off,
the production wiring of the mail settings, the registration logger, the Nginx
zone and three operator commands; no migration) is merged (#338):
`docs/reviews/EMAIL_REGISTRATION_PR4_STATUS.md`.
With it Gabriel approved that a registration hold does not block the
cancellation of pending data (the hold and the clock mark are preserved; a
regressed clock still refuses; design 0.7). **PR 5** (operations: the cleanup unit and timer, the restore script's
cleanup coordination and registration gate, the status and release commands, and
the logical snapshot scrub; decisions D18 (b) and D20 (a); no migration) is merged
(#339, squash commit `3d2e0d56ef2b57d66d65d640fb289c9c9041e6ce`, whose tree equals that of
the reviewed `65bee1e`): `docs/reviews/EMAIL_REGISTRATION_PR5_STATUS.md`. Its Linux and
systemd evidence has not been produced. **PR 6** (the account profile flow: migration
`023_profile_required.sql`, `/account/profile` and the profile on `/account`, the build gate
`profiles.PRODUCTION_ACTIVATION`, kept off, `profiles.needs_profile`, the mark written at Google
onboarding for a newly created student only, and the profile check at the locked decision to
create a new attempt) is merged (#340, squash `ede0c0b36ee3d163e317a93da744ff146b2c7c1e`, whose tree equals that of
`c1d3b8f6a6b7d2651c4b4c3db152acd2c49655aa`, independently reviewed there: APPROVE WITH NON-BLOCKING NOTES) and
**not activated**: `docs/reviews/EMAIL_REGISTRATION_PR6_STATUS.md`. **PR 7** (per-account assignment
quotas, migration `024_assignment_creations.sql`, and gated teacher registration, with the teacher gate kept off) is
implemented on `feature/teacher-registration-quotas` (PR #341; committed as `b15d02b`, `3537f4f`, `e39fa9f` and `5f834ae`; the first two were each independently reviewed at their
exact SHA and returned **REQUEST CHANGES**, and `e39fa9f` and `5f834ae` each returned **APPROVE WITH NON-BLOCKING NOTES**; `5f834ae` holds the ASCII-digit cleanup of the timestamp parser and the recording of Gabriel's
policies; later provenance-only edits to the documents are outside that reviewed commit), **not
activated**: `docs/reviews/EMAIL_REGISTRATION_PR7_SCOPE.md` and `EMAIL_REGISTRATION_PR7_STATUS.md`. Approval of its
implementation will not be activation. PR 8 (recovery) is not started, and no deployment, provider or real
database was touched.

Revisions 1, 2 and 3 were each independently reviewed and returned with
REQUEST CHANGES, the third narrowly (two defects in the sender contract and
twelve notes). **Revision 4** answers the third review (design, section 0.6;
the earlier ones are answered in 0.4 and 0.5).

An independent review of commit `2b29519e6483b707950023fd199ca947d861381c`
returned **APPROVE WITH NON-BLOCKING NOTES**: the two blockers resolved,
N1–N12 accepted, no technical blocker remaining; it approved none of D1–D20.
Six wording and test-plan corrections (R1–R6, design 0.7) and then the
recording of Gabriel's decisions were made **after** that review and are
**not independently reviewed**; no further broad review is required for
them. Still open, and unaffected by the verdict: Linux
unit, signal, descriptor and worker-replacement evidence is outstanding;
provider behaviour, delivery capacity, timers and restore, and privacy are
activation prerequisites; teacher quotas are a prerequisite for the teacher
gate (values approved with PR 7: 20 creations per rolling 24 hours and 10 open
assignments; implemented, independent review outstanding). Nothing here authorizes an
implementation, a deployment or an activation.

## Prerequisites (all merged, none activated)

- Accounts, sessions, throttle, admin TOTP: `docs/ACCOUNTS_DESIGN.md`,
  `docs/AUTH_ROUTE_CONTRACT.md`.
- Google sign-in, PR #331 at fe72d02, production activation off
  (`docs/reviews/GOOGLE_OIDC_SCOPE.md`).
- Roadmap update of 2026-10-13 (`docs/REFACTOR_ACCOUNTS_ROADMAP.md`).

## Approved product requirements (input, not designed here)

- A student registers with first name, last name, email, class and password.
- A teacher registers with first name, last name, email, password, school
  name and subject taught.
- Both prove ownership of the email with a code sent to it. Accounts sign in
  with email and password; this is not passwordless sign-in. Decision
  D7 (approved): the code is verified once, at registration, and the person
  is then sent to `/login`; registration issues no session, and sign-in never
  asks for a code. Recovery is a separate flow.
- Google stays the one planned external provider, activation off. Microsoft
  is excluded.
- Existing teacher/admin password accounts keep working. Public registration
  never creates an admin. Email verification proves email ownership, not
  teacher status.
- Google and password accounts are never merged because emails match.

The required fields are authorised, and D4 and D11 are decided (school as free
text with a town hint; profile completion for new Google students only). Their
purpose, who may see them and how long they are kept are not, and stay with D16.

## In scope of the design

1. The two forms: fields, limits, validation, Bulgarian labels and errors.
2. Profile storage: names apart from the `display_name` snapshot; class,
   school and subjects.
3. Pending registration, verification code, activation, expiry; duplicate and
   concurrent registrations; browser binding.
4. Abuse controls per pending registration, per email and per IP; a send
   ledger and a non-refundable work budget; enumeration, replay, races,
   delivery failure and its settlement, logging, clocks, restore.
5. A replaceable mail-delivery interface, a contained sender process, and
   its configuration. No provider is chosen.
6. Password policy and confirmation; what password recovery must do in
   either of its two possible shapes.
7. Additive schema, table classification, routes, policies, service APIs and
   transaction ownership.
8. Logical retention of unverified registrations: scheduled cleanup,
   scrubbing, backups, and what remains on disk regardless.
9. Teacher self-declaration, quotas, the three gates, Google compatibility
   and profile completion for accounts created through Google.
10. Acceptance tests and a sequential implementation order of small PRs.

## Summary of the design (decisions of design 0.2 applied)

- A registration is a row in a new `pending_registrations` table, **not** a
  `users` row. It has no principal and no privilege, reserves no email, and
  can be completed for 30 minutes. Any number may exist for one address.
- The code is 6 digits, stored only as a keyed hash, valid 10 minutes, five
  attempts, replaced by a resend.
- Every send attempt is recorded in a ledger, stamped under the write lock.
  An address gets at most 5 attempts an hour and 8 in 24 h 15 min; the whole
  site 300 an hour. Nothing is given back after a failure.
- **Guessing bound, with its assumptions:** while the server clock is not
  stepped, the secret key is not changed and the ledger is intact (no
  restore, released hold or operator clearing), at most 8 codes of an address
  can be guessed in any 24 hours: 40 guesses in a million. Outside those
  assumptions no number is claimed; the design gives the known cases (65
  guesses with two small clock steps, 80 around a key rotation or a released
  hold).
- A pending registration is bound to the browser that submitted the form, by
  a random token in its signed session. Only that browser can verify it, and
  only with the code from the mailbox. Reading the code on another device and
  typing it into the first browser works; typing it into another browser
  does not.
- Activation creates the `users` row, the profile and the verification
  timestamp in one write transaction. The role is a literal of the route
  (`student` or `teacher`), never a submitted value. Only a real email
  collision is reported as "address taken"; any other failure rolls back and
  fails generically.
- **First session (D7, approved):** none. The confirmation page links to
  `/login`; later sign-in is email and password only, with no code at login,
  and admin TOTP is unchanged.
- **Delivery (D17 behaviour, approved):** the application sends the code
  automatically from a configured no-reply address; address, domain,
  credentials and provider or relay are configured later with the VPS, and
  none is assumed. Production `console` and `file` delivery is prohibited.
- An address that already has an account gets the same pages and one notice
  email to its owner. Nothing is linked or merged. The design claims the same
  visible answers and accounting for both cases, not the same delivery
  outcome or duration.
- A send runs in a short-lived, standalone sender process. The request
  exchanges at most 16 KiB of input and 16 bytes of output with it without
  blocking, and ends it after 8 seconds of communication; process creation and reaping are not
  separately bounded, a worker has one sender at a time, and senders can
  overlap briefly when a worker is replaced. An outcome that is not certain
  is recorded as unknown: the code is kept, the budget is spent, nothing is
  retried.
- Unverified registrations are scrubbed by a timer that does not depend on
  traffic. That is a logical-retention target (about 45 minutes) which
  depends on the timer, the database and the clock; it is not erasure, and
  old bytes can remain in the WAL, in free pages, in unused space inside
  pages and in earlier backups. Snapshots are scrubbed logically in the isolated
  backup copy before publication (D18 (b)); no offline rebuild is selected.
- A restore voids every pending credential before the restored database goes
  live. The send accounting is held (D20, approved): registration is closed after
  every restore until the operator releases it, acknowledging the lost
  history. No hold is released automatically.
- **Password recovery (D10, approved):** self-service by emailed code, in its
  own PR (PR 8), before registration is opened to the public. It is not
  registration verification repeated. It is not required in order to create
  accounts safely.
- Production stays off behind three build constants (student registration,
  teacher registration, profiles), as Google sign-in does.

## Out of scope

- Implementation, migrations, deployment, activation, any provider account.
- Verification links. Only the typed code is specified.
- Passwordless or code-only sign-in.
- Email change, adding a password to an account created through Google,
  unlinking and account merging. Each is deferred with its limits and support
  handling stated in the design, section 5.3. A Google identity's stored
  email is never a login identifier, a recovery address or a merging key.
- Teacher verification (proof that someone is a teacher).
- A retention period or erasure tooling for accounts and profiles (undecided
  product/legal question, `docs/ACCOUNTS_DESIGN.md` §15). Retention of
  **unverified** registrations is in scope.
- DZI gate, entitlements, billing, Microsoft, Apple, an admin user UI.
- Bounce or complaint processing; any provider webhook.
- A mail queue, a task framework or a second worker.

## Implementation decisions and activation prerequisites

These are two different lists and are kept apart in the design.

- **Implementation decisions** (design, 0.2): all approved; none holds up
  PR 1–7. What remains open for implementation: the **addendum** of PR 8
  (including recovery for never-verified addresses). PR 7's quota values are
  approved and its short scope is `EMAIL_REGISTRATION_PR7_SCOPE.md`, which
  also lists the one choice still needing Gabriel (admin accounts). **D15** (deferral of email change, a password for
  a Google-only account, and merging) blocks no PR: the limits and support
  handling of design 5.3 apply while the deferral stands. Approval does not
  authorize starting a PR.
- **Activation prerequisites** (design, 0.3): evidence and outside work
  needed before a gate is turned on. They do not hold up any implementation
  PR, because every PR ships with its gate off.

## Activation prerequisites outside the code (none done)

- A public privacy-policy page, and Gabriel's decisions as data controller
  for minors' data: lawful basis, the consent age and whether parental
  consent applies to the youngest students, the purpose and visibility of the
  profile fields, and the retention period, backups included (D16).
- `docs/ACCOUNTS_DESIGN.md` §15 amended: names, class, school and subjects
  become collected data; today it lists school and class as *not collected*.
- The sender address and domain, credentials and provider or relay,
  configured with the VPS (D17's behaviour is decided; none is assumed), with
  SPF, DKIM and DMARC; delivery checked to real mailboxes. Acceptance by the relay
  is not delivery.
- Evidence, on the server under the real unit, that a send is contained and
  that one synchronous worker carries a class: the forced-hang check, a
  worker killed during a send, descriptor isolation of the sender, and the
  capacity measurement of the design, 0.3 and 3.5. Verified TLS; development
  mail backends refused in production.
- Nginx: the registration zone tuned, trusted client-IP handling confirmed,
  the backend reachable only through Nginx. A burst allowance lets a class
  through at once; it does not spread out the mail work. Shared-NAT use and
  deliberate exhaustion of the total budget assessed explicitly.
- The cleanup timer, the full restore script with its gate (including an
  interrupted run and a run during a cleanup) and the snapshot step exercised
  on Linux, with a test restore.
- Password recovery (PR 8, D10) delivered before public opening.
- Teacher quotas decided and implemented before the **teacher** gate is
  turned on (decided and implemented by PR 7; independent review outstanding).
  They do not hold up student registration.
- Independent security review of the implementation PRs marked for it.
- Google's own activation prerequisites, unchanged and separate.

## Execution

Design by Claude Opus 5.5, High. Independent review in a separate session
(see Status for its verdict on revision 4 and for the later corrections it did
not cover). The implementation order and suggested executors are in
the design, section 9: one PR at a time, never in parallel.
