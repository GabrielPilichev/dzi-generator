# Password recovery by emailed code — scope (registration PR 8)

Design: `docs/reviews/PASSWORD_RECOVERY_DESIGN.md`.
Test plan: `docs/reviews/PASSWORD_RECOVERY_TEST_PLAN.md`.
Parent: `docs/reviews/EMAIL_REGISTRATION_DESIGN.md`, decision D10 and
section 5.2; `docs/reviews/EMAIL_REGISTRATION_SCOPE.md`.

## Status

**Documentation only: a proposal, revision 2, not approved.** Revision 1 was
written on branch `docs/password-recovery-design` at `513b36c` (whose code
equals `3d4306a`, PR #341 merged) and committed as `745a952`; its independent
review returned **REQUEST CHANGES** (findings F1–F11, mapped in design 0.4).
Revision 2 answers them and is not yet reviewed. No application code,
migration, route or configuration exists for recovery, and no migration
number is reserved. Implementation, deployment and activation are not
authorized. Every production gate stays off (student registration, teacher
registration, profiles, Google, and the proposed recovery gate).

Approved before this addendum, and only restated by it (design 0.1, quoting
the approved documents): D6, D7, D10 and D20 of the registration design, the
requirements of its section 5.2, its section 3.4 on `login_throttle`, and the
addendum's brief (A1–A16). Everything else in the three documents is a
proposal. The eighteen open decisions RD1–RD18 (design 0.3) each carry a
recommendation; none is selected. Three of them would **amend** approved
requirements and are marked so: RD4 (eight-digit recovery codes, against
D6's six digits), RD7 (no `not_sent` retirement, against the settlement of
registration design 4.3), RD10 (recovery guesses kept out of
`login_throttle`, against registration design 3.4). Two rest on an
**interpretation** of approved wording that Gabriel must confirm: RD2 (a)
(A4 governs codes, not a credential-free notice) and RD17 (another pending
code is "voided" by making it unable to reset, not by writing its row).

## In scope of the design

1. Threat model and eligibility, including legacy accounts that were never
   verified, admins, demoted admins with TOTP rows, disabled, password-less
   and Google-only accounts, and the invariant later email-change work must
   keep for `email_verified_at`.
2. Lifecycle and UX: request, verify-and-reset in one step, resend, cancel,
   expiry, restart; states, session slot, routes, safe return, Bulgarian
   texts and messages (proposed wording); what another browser can observe
   while the owner resets.
3. Storage: one new table, one nullable column on the existing send ledger,
   keyed values, issuance identity, one read/gate/clock order for every
   writer, transaction boundaries, settlement and concurrency.
4. The account API as it is and the few additions recovery needs; password
   timestamp and hash policy; session, throttle and account-wide invalidation;
   interaction with pending registrations.
5. Budgets, clocks and holds shared with registration: one ledger, one total
   budget, one per-address budget, one circuit, one high-water mark, one
   restore hold; the quantitative guessing bound for mixed purposes and its
   accumulating exceptions.
6. Operations: cleanup, restore gate and status, hold release, snapshot scrub
   and fail-closed schema checks, through the existing commands and modules;
   the old passwords and session keys a restore brings back, and a read-only
   comparison runbook (RD18).
7. Rollout and compatibility: the gate, settings, schema compatibility,
   old-code hazards, deployment order, the Nginx locations.
8. Adversarial test plan and a sequential implementation plan.

## Out of scope (A14 and D15, unchanged)

- Changing an account's email; adding a password to a Google-only account;
  identity linking, unlinking or merging; admin recovery by any web path; TOTP
  enrolment or reset by any web path. The CLI keeps all of them as they are.
- A sign-in code of any kind (D7).
- A recovery link (only a typed code).
- A "password changed" notice (RD9 recommends deferring it).
- An operator command that attests an old address as verified (mentioned as an
  alternative to RD1, not designed).
- A mail queue, a worker pool, bounce processing, a new provider decision.
- Any change to `deploy/learnpilot-restore.sh` or its ownership protocol.
- Privacy decisions (D16), which must be amended for recovery before
  activation.

## Summary of the proposal

- **Eligibility** at every issuance and again at redemption, under the writer
  lock: active, `student` or `teacher`, has a password, `email_verified_at`
  set (RD1, the approved default), and at redemption the same account, the same
  `users.email` and the same session key (as a keyed digest) as at issuance.
  Admins never.
- **Every well-formed address gets the same visible treatment.** A
  non-eligible address gets a shadow row whose code no digits match, and either
  a neutral notice (RD2 (a), recommended; needs Gabriel to confirm that A4
  governs codes) or nothing with identical accounting (RD2 (b): with current
  synchronous dispatch, eligibility likely distinguishable by timing, not
  measured; send-related log lines exist for real rows only).
- **One step**: code and new password in one POST, one transaction, using
  `accounts.set_password` unchanged; scrypt only after a correct code; no
  session.
- **Other pending codes of the account** stop being able to reset anything
  because the reset rotates the session key their digest was bound to; their
  rows are not written, so another browser's reads, wrong codes, resends and
  cancellation reveal nothing before a correct code (RD17).
- **Throttle**: a reset does not clear `login_throttle` (RD8 (a), changed in
  revision 2), because clearing it is observable at `/login`; recovery codes
  are not counted in it (RD10).
- **Limits**: registration's D6 values, with an **8-digit** code proposed for
  recovery (RD4, an amendment). At most eight guessable codes of an address
  in a day under the stated assumptions, at most eight of them recovery codes.
- **Accounting**: recovery issuances are rows of `registration_sends`
  (`recovery_id` set, at most one purpose per row), so registration's existing
  caps, total budget, circuit and ledger cleanup count both purposes with no
  new counter.
- **Settlement** records the ledger outcome only; a `not_sent` recovery send
  changes nothing the browser can see (RD7, an amendment).
- **Operations** through the existing `registrations-*` commands and the
  backup module; the restore script is not edited. After a restore, a
  read-only comparison of the snapshot with the kept replaced database and a
  reviewed password replacement or disable (not a sign-out alone) precede
  reopening (RD18).
- **Gate** `recovery.PRODUCTION_ACTIVATION = False`.

## Integration points (all proposals)

| Where | Change | Scope |
| --- | --- | --- |
| New migration (next free number) | `password_recoveries`; `registration_sends.recovery_id` with the at-most-one-purpose CHECK; one index | 8.1 |
| `src/canonical_tables.py` | `password_recoveries`: runtime | 8.1 |
| `web/services/recovery_scrub.py` (new, standard library) | scrubbed and known columns (the allow-list of RD13), schema check, `void_pending`, `cleanup`, snapshot scrub | 8.1 |
| `src/manage_accounts.py` | `registrations-cleanup`, `-restore-gate`, `-restore-status`, `-release-hold`, `clear-registration-limit` cover recovery; `require_registration_tables`; `restore-credential-diff` (new, read-only) only under RD18 (a) | 8.1 |
| `src/backup_sqlite.py` | `scrub_pending_recoveries` before `quick_check` | 8.1 |
| `web/services/registration.py` | `settle_send` refuses recovery rows | 8.1 |
| `deploy/learnpilot-restore.sh` | **Not edited**; static tests pin the existing calls | 8.1 |
| `docs/DEPLOYMENT.md` | Recovery operations; the restore credential runbook (RD18) | 8.1 |
| `web/services/registration.py` | public `control_blocked`, `raise_mark`, `writer_now` | 8.2 |
| `web/services/accounts.py` | `recovery_candidate(...)` (read under the caller's lock); `set_password` unchanged | 8.2 |
| `web/services/recovery.py` (new) | request, resend, redeem, cancel, `pending_view`, dispatch, settle; no caller | 8.2 |
| `web/blueprints/recovery.py` (new), templates | Routes of design 2.6 | 8.3 |
| `web/blueprints/account.py`, `login.html` | „Забравена парола?“ link and the `recovery=done` notice, while enabled | 8.3 |
| `web/app.py`, `web/production.py` | Settings under their own key; production behind the gate | 8.3 |
| `deploy/nginx/learnpilot.conf` | Design 7.5: `location = /recover` and `= /recover/resend` in `lp_register`, `= /recover/verify` in `lp_login`, a `/recover/` prefix with no limit; each with the repeated security headers, one `no-referrer` and `proxy_hide_header Referrer-Policy` | 8.3 |
| Documents | `AUTH_ROUTE_CONTRACT.md`, `ACCOUNTS_DESIGN.md` §12, `TESTING.md` with the implementation | 8.3 |

## Implementation scopes (design 10)

Sequential, never in parallel; each implemented by **Claude Opus, High** and
reviewed independently by a **separate Claude Opus, High session** at its exact
SHA before the next begins. The order serves one rule: **no build capable of
creating a recovery row may lack the cleanup, restore and snapshot
integration.**

1. **8.1** storage and operations: the migration together with the scrub, the
   schema allow-list (RD13), the cleanup, the restore gate and status, the
   backup scrub, the CLI, the restore-script static tests and the deployment
   document. Nothing in it creates a recovery row (test RZ1).
2. **8.2** the service, with no caller (test RZ2: no production import,
   route, CLI subcommand, startup hook or deploy entry point reaches it). Not
   argued safe because routes are absent: safe because 8.1's integration is
   already there.
3. **8.3** routes, templates, gate, Nginx and contract documents.

## Decisions: what blocks what

| Blocks | Decisions |
| --- | --- |
| Implementation of 8.1 | RD3, RD13; RD18 only for the command under (a) |
| Implementation of 8.2 | RD1 (only if changed from the default), RD2, RD4, RD5, RD6, RD7, RD8, RD10, RD11, RD12, RD17 |
| Implementation of 8.3 | RD14, RD16 |
| Activation only | RD15 (wording); the RD18 runbook rehearsed; D16 amended for recovery; the configuration of D17; the evidence of design 9 |
| Nothing unless approved | RD9 (a later scope if yes) |

## Activation prerequisites (none done)

Design section 9: the registration design's mail, capacity, Nginx and Linux
evidence, now for both purposes under one budget; recovery-specific Linux
evidence for the cleanup timer, restore and snapshot step; the restore
credential runbook rehearsed on staging, including unavailable replaced
evidence; under RD2 (a) measured timing of a recovery code and notice, under
RD2 (b) the recorded acceptance that eligibility is likely distinguishable by
timing; the real Nginx accepting the recovery locations with one
`no-referrer` each; the lock time of `set_password` on the server; D16
amended; the decisions, amendments, interpretations and wording approved;
independent security review of every scope.

## Execution

Revisions 1 and 2 written by Claude Opus 5.5, High. Revision 2 needs its own
independent review before implementation scope 8.1 starts.
