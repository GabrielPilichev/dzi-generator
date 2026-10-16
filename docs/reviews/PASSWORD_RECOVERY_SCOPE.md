# Password recovery by emailed code — scope (registration PR 8)

Design: `docs/reviews/PASSWORD_RECOVERY_DESIGN.md`.
Test plan: `docs/reviews/PASSWORD_RECOVERY_TEST_PLAN.md`.
Parent: `docs/reviews/EMAIL_REGISTRATION_DESIGN.md`, decision D10 and
section 5.2; `docs/reviews/EMAIL_REGISTRATION_SCOPE.md`.

## Status

**Documentation only: a proposal, not reviewed and not approved.** Written on
branch `docs/password-recovery-design` at `513b36c` (whose code equals
`3d4306a`, PR #341 merged). No application code, migration, route or
configuration exists for recovery, and no migration number is reserved.
Implementation, deployment and activation are not authorized. Every production
gate stays off (student registration, teacher registration, profiles, Google,
and the proposed recovery gate).

Approved before this addendum, and only restated by it (design 0.1): D7 and
D10 of the registration design and the requirements of its section 5.2 and of
the addendum's brief (A1–A14). Everything else in the three documents is a
proposal. The sixteen open decisions RD1–RD16 (design 0.3) each carry a
recommendation; none is selected.

## In scope of the design

1. Threat model and eligibility, including legacy accounts that were never
   verified, admins, disabled, password-less and Google-only accounts.
2. Lifecycle and UX: request, verify-and-reset in one step, resend, cancel,
   expiry, restart; states, session slot, routes, safe return, Bulgarian
   texts and messages (proposed wording).
3. Storage: one new table, one nullable column on the existing send ledger,
   keyed values, issuance identity, transaction boundaries, settlement and
   concurrency.
4. The account API as it is and the few additions recovery needs; password
   timestamp and hash policy; session, throttle and account-wide invalidation;
   interaction with pending registrations.
5. Budgets, clocks and holds shared with registration: one ledger, one total
   budget, one per-address budget, one circuit, one high-water mark, one
   restore hold; the quantitative guessing bound and its exceptions.
6. Operations: cleanup, restore gate and status, hold release, snapshot scrub
   and fail-closed schema checks, through the existing commands and modules.
7. Rollout and compatibility: the gate, settings, schema compatibility,
   old-code hazards, deployment order.
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
  governs codes) or nothing with identical accounting (RD2 (b), which leaves a
  timing oracle).
- **One step**: code and new password in one POST, one transaction, using
  `accounts.set_password` unchanged; scrypt only after a correct code; every
  other pending recovery of the account voided; no session.
- **Limits**: registration's D6 values, with an **8-digit** code recommended
  for recovery (RD4: 4×10⁻⁷ instead of 4×10⁻⁵ per targeted account per day).
- **Accounting**: recovery issuances are rows of `registration_sends`
  (`recovery_id` set), so registration's existing caps, total budget, circuit
  and ledger cleanup count both purposes with no new counter.
- **Settlement** records the ledger outcome only; a `not_sent` recovery send
  changes nothing the browser can see (RD7).
- **Operations** through the existing `registrations-*` commands and the
  backup module; the restore script is not edited.
- **Gate** `recovery.PRODUCTION_ACTIVATION = False`.

## Integration points (all proposals)

| Where | Change |
| --- | --- |
| New migration (next free number) | `password_recoveries`; `registration_sends.recovery_id` with a cross-column CHECK; three indexes |
| `src/canonical_tables.py` | `password_recoveries`: runtime |
| `web/services/accounts.py` | `recovery_candidate(...)` (read under the caller's lock); `set_password` unchanged |
| `web/services/registration.py` | public `control_blocked`, `raise_mark`, `writer_now`; `settle_send` refuses recovery rows |
| `web/services/recovery.py` (new) | request, resend, redeem, cancel, `pending_view`, dispatch, settle, cleanup |
| `web/services/recovery_scrub.py` (new, standard library) | scrubbed and known columns, schema check, `void_pending` |
| `src/manage_accounts.py` | `registrations-cleanup`, `-restore-gate`, `-restore-status`, `-release-hold`, `clear-registration-limit` cover recovery; `require_registration_tables` |
| `src/backup_sqlite.py` | `scrub_pending_recoveries` before `quick_check` |
| `deploy/learnpilot-restore.sh` | **Not edited**; static tests pin the existing calls |
| `web/blueprints/recovery.py` (new), templates | Routes of design 2.6 |
| `web/blueprints/account.py`, `login.html` | „Забравена парола?“ link and the `recovery=done` notice, while enabled |
| `web/app.py`, `web/production.py` | Settings under their own key; production behind the gate |
| `deploy/nginx/learnpilot.conf` | `/recover` and `/recover/resend` in `lp_register`; `/recover/verify` in `lp_login` |
| Documents | `AUTH_ROUTE_CONTRACT.md`, `ACCOUNTS_DESIGN.md` §12, `DEPLOYMENT.md`, `TESTING.md` with the implementation |

## Implementation scopes (design 10)

Sequential, never in parallel; each implemented by **Claude Opus, High** and
reviewed independently by a **separate Claude Opus, High session** at its exact
SHA before the next begins:

1. **8.1** storage and service (no route);
2. **8.2** operations (CLI, backup scrub, schema checks, restore-script static
   tests, deployment document);
3. **8.3** routes, templates, gate, Nginx and contract documents.

## Decisions: what blocks what

| Blocks | Decisions |
| --- | --- |
| Implementation of 8.1 | RD1 (only if changed from the default), RD2, RD3, RD4, RD5, RD6, RD7, RD8, RD10, RD11, RD12 |
| Implementation of 8.2 | RD13 |
| Implementation of 8.3 | RD14, RD16 |
| Activation only | RD15 (wording); D16 amended for recovery; the configuration of D17; the evidence of design 9 |
| Nothing unless approved | RD9 (a later scope if yes) |

## Activation prerequisites (none done)

Design section 9: the registration design's mail, capacity, Nginx and Linux
evidence, now for both purposes under one budget; recovery-specific Linux
evidence for the cleanup timer, restore and snapshot step; measured timing of
a recovery code and notice (or the recorded acceptance of RD2 (b)'s timing
oracle); the lock time of `set_password` on the server; D16 amended; the
decisions and wording approved; independent security review of every scope.

## Execution

Addendum written by Claude Opus 5.5, High. It needs its own independent review
before implementation scope 8.1 starts.
