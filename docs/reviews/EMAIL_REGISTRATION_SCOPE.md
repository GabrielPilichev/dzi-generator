# Verified email/password registration — scope

Design: `docs/reviews/EMAIL_REGISTRATION_DESIGN.md`.
Test plan: `docs/reviews/EMAIL_REGISTRATION_TEST_PLAN.md`.

## Status

Design only, **not approved**. No application code, test, migration, database,
deployment or provider was touched. Nothing here is a decision until Gabriel
approves it; section 0.2 of the design lists what needs his answer (D1–D20,
none approved).

Revisions 1, 2 and 3 were each independently reviewed and returned with
REQUEST CHANGES, the third narrowly (two defects in the sender contract and
twelve notes). **Revision 4** answers the third review (design, section 0.6;
the earlier ones are answered in 0.4 and 0.5). It has not been reviewed, and
its author does not approve it.

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
  with email and password; this is not passwordless sign-in. Whether the
  request that verifies the code also issues the first session, or the person
  signs in at `/login` afterwards, is **not** part of the requirement: it is
  decision D7.
- Google stays the one planned external provider, activation off. Microsoft
  is excluded.
- Existing teacher/admin password accounts keep working. Public registration
  never creates an admin. Email verification proves email ownership, not
  teacher status.
- Google and password accounts are never merged because emails match.

The required fields are authorised. Their purpose, who may see them, how long
they are kept, the town hint for the school and any enforcement on accounts
that already exist are not, and stay with D4, D11 and D16.

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

## Proposed, pending approval (summary of the design)

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
- **First session (D7):** either none, with a link to `/login`
  (recommended for the first release), or automatic sign-in through the
  normal session helper after the activation has been committed. Both are
  legitimate.
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
  pages and in earlier backups.
- A restore voids every pending credential before the restored database goes
  live. What happens to the send accounting is D20: hold registration after
  every restore until the operator releases it, or carry the ledger over with
  a validated merge. No hold is released automatically.
- **Password recovery (D10):** proposed as self-service by emailed code, in
  its own PR, before registration is opened to the public. Operator-assisted
  recovery, as today, is a supported alternative, sufficient for a bounded
  pilot. Neither is required in order to create accounts safely.
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

- **Implementation decisions** (design, 0.2): what Gabriel has to choose
  before a given PR can be written. D1–D4, D12 and D13 hold up PR 1; D19
  PR 2; D6 and D8 PR 3; D7, D9 and D14 PR 4; D18 and D20 PR 5; D11 PR 6;
  D5 PR 7; D10 PR 8.
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
- A delivery provider and a sender domain with SPF, DKIM and DMARC, chosen by
  Gabriel (D17); delivery checked to real mailboxes. Acceptance by the relay
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
- Password recovery in the shape chosen under D10.
- Teacher quotas decided and implemented before the **teacher** gate is
  turned on. They do not hold up student registration.
- Independent security review of the implementation PRs marked for it.
- Google's own activation prerequisites, unchanged and separate.

## Execution

Design by Claude Opus 5.5, High. Independent review in a separate session;
revision 4 awaits it. The implementation order and suggested executors are in
the design, section 9: one PR at a time, never in parallel.
