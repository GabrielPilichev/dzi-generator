# Password recovery by emailed code — scope (registration PR 8)

Design: `docs/reviews/PASSWORD_RECOVERY_DESIGN.md`.
Test plan: `docs/reviews/PASSWORD_RECOVERY_TEST_PLAN.md`.
Parent: `docs/reviews/EMAIL_REGISTRATION_DESIGN.md`, decision D10 and
section 5.2; `docs/reviews/EMAIL_REGISTRATION_SCOPE.md`.

## Status

**Scope 8.1 implementation authorized by Gabriel; changes prepared for independent
review. 8.2/8.3, deployment and activation remain unauthorized.**
Evidence and limitations: `PASSWORD_RECOVERY_8_1_STATUS.md`. Design revision 3
with post-review corrections and Gabriel's decisions recorded.
Revision 1 was
written on branch `docs/password-recovery-design` at `513b36c` (whose code
equals `3d4306a`, PR #341 merged) and committed as `745a952`; its independent
review returned **REQUEST CHANGES** (findings F1–F11, mapped in design 0.4).
Revision 2 (`727bd4d`) answered them and received a **second** independent
review, of the cumulative six-document proposal rather than of its changes
alone, which returned a narrow **REQUEST CHANGES** (N1–N5 and notes, mapped in
design 0.5). Revision 3 answered it and was committed as
`48ee5bfe9847c018f6c93ab08dfc5f361b3c64b8`; an independent review of exactly
that SHA returned **APPROVE WITH NON-BLOCKING NOTES** (R1–R8) and left
RD1–RD18 proposed. The documentation corrections answering R1–R8 (design 0.6)
were made after that SHA and are **outside the review of `48ee5bf`**.
Gabriel's selections of RD1–RD14 and RD16–RD18 were then recorded on
2026-10-18 (design 0.3, 0.7); that recording and the alignment edits it made
are outside that review too. No review approved a decision: each is
approved because Gabriel selected it. Scope 8.1 adds migration 025, storage and
operations; no recovery-row creation, route, issuance/redemption or mail sending
exists. Scopes 8.2/8.3, deployment and activation are not authorized.
Every production gate stays off (student registration, teacher
registration, profiles, Google, and the designed recovery gate, which is not built).

Approved before this addendum, and restated by it (design 0.1, quoting the
approved documents): D6, D7, D10 and D20 of the registration design, the
requirements of its section 5.2, its section 3.4 on `login_throttle`, and the
addendum's brief (A1–A16). **Selected by Gabriel** (design 0.3): RD1–RD14 and
RD16–RD18, RD18 in its three parts (the repair by the read-only command and
categories; every account uncertain when evidence is unavailable; verified
maintenance isolation required, with no fallback to an exposed window).
Approved with them, for recovery only (design 0.1.1): three **amendments**
of approved requirements, RD4 (eight-digit recovery codes, amending D6's six
digits; registration codes unchanged), RD7 (a `not_sent` recovery send
changes only the send ledger, amending the settlement of registration design
4.3) and RD10 (wrong recovery codes not counted toward the sign-in throttle,
amending registration design 3.4); and two **interpretations**, RD2 (a) (the
destination restriction of A4 governs recovery codes; a neutral notice goes
to the typed address) and RD17 (another pending code is "voided" by the
session-key digest mismatch, without writing its row). **Still open:** RD15
(the exact Bulgarian wording), D16 (privacy, extended to recovery), the
configuration of D17 and every activation prerequisite.

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
- A "password changed" notice (RD9: none in PR 8).
- An operator command that attests an old address as verified (an alternative
  to RD1, not selected and not designed).
- A mail queue, a worker pool, bounce processing, a new provider decision.
- Any change to `deploy/learnpilot-restore.sh` or its ownership protocol.
- A maintenance configuration in the repository's Nginx template: the
  required maintenance isolation of RD18 is an operator step on the host, documented in the runbook (its form, its
  ACME handling and its argument-free access log, design 6.6); the template
  gains only the `/recover` locations of 8.3.
- Operating any tunnel, forward, firewall or service: the isolation evidence
  of RD18 is read-only inspection in a later runbook.
- Privacy decisions (D16), which must be extended to recovery (including the
  neutral notices of RD2 (a)) before activation.

## Summary of the selected design

- **Eligibility** at every issuance and again at redemption, under the writer
  lock: active, `student` or `teacher`, has a password, `email_verified_at`
  set (RD1: only verified-email accounts; legacy and CLI-created accounts
  keep operator recovery), and at redemption the same account, the same
  `users.email` and the same session key (as a keyed digest) as at issuance.
  Admins never.
- **Every well-formed address gets the same visible treatment.** A
  non-eligible address gets a shadow row whose code no digits match and one
  neutral notice to the typed address, issued, counted, sent and settled like
  a code (RD2 (a)); recovery codes go only to an eligible account's
  `users.email`.
- **One step** (RD3): code and new password in one POST, one transaction, using
  `accounts.set_password` unchanged; scrypt only after a correct code; no
  session.
- **Other pending codes of the account** stop being able to reset anything
  because the reset rotates the session key their digest was bound to; their
  rows are not written, so other browsers' rows are not visibly retired and
  another browser's reads, wrong codes, resends and cancellation reveal
  nothing before a correct code (RD17).
- **Throttle**: a reset does not clear `login_throttle` (RD8 (a)). The
  documented residual stands: the owner's next successful sign-in clears it
  as any sign-in does, which an observer counting failures can notice, and
  sustained relocking still denies sign-in. Wrong recovery codes are not
  counted in it (RD10).
- **Limits**: registration's D6 values, with an **8-digit** recovery code
  (RD4). At most eight guessable codes of an address in a day under the
  stated assumptions, at most eight of them recovery codes.
- **Resend and cancel**: every resend rechecks eligibility (RD11);
  cancellation is allowed during a restore hold, and a clock behind the mark
  still refuses it (RD12).
- **Accounting**: recovery issuances are rows of `registration_sends`
  (`recovery_id` set, at most one purpose per row), so registration's existing
  per-address caps, total budget, circuit and ledger cleanup count both
  purposes with no new counter (RD5, RD6).
- **Settlement** records the ledger outcome only; a `not_sent` recovery send
  neither retires the code nor waives the wait, and changes nothing the
  browser can see (RD7).
- **Success notice**: `/login?recovery=done` shows a fixed notice selected by
  the URL; it changes no account state and is not proof that a reset
  occurred (RD16).
- **No change notice** in PR 8 (RD9).
- **Operations** through the existing `registrations-*` commands and the
  backup module; the restore script is not edited. The script starts the
  service before it returns, so restored old passwords would be usable from
  that start unless public access were blocked beforehand. **Verified
  maintenance isolation is required** (RD18): an external Nginx maintenance
  block, established and verified before the restore under conditions the
  host must meet (the script's direct loopback health check kept), removed
  only after the repair has been validated; the access log can only check
  afterwards. There is no fallback to an exposed window: if isolation cannot
  be established, the restore is postponed, and if it fails afterwards,
  public reopening is postponed. After a restore, the read-only
  `restore-credential-diff` compares two offline inputs (the original
  published snapshot and a rollback-mode copy of the replaced database) and
  the repair follows by category: a password replacement or a disable (not a
  sign-out alone) for an account with a restored password, a sign-out or a
  disable (never a granted password) for one without; when evidence is
  unavailable every account is uncertain. The repair is validated on pre-
  and post-repair snapshots against a reference time captured after activity
  stopped.
- **Gate** `recovery.PRODUCTION_ACTIVATION = False`.

## Integration points (8.1 implemented for review; later scopes deferred)

| Where | Change | Scope |
| --- | --- | --- |
| Migration `025_password_recoveries.sql` | `password_recoveries`; `registration_sends.recovery_id` with the at-most-one-purpose CHECK; two indexes (one per table) | 8.1 |
| `src/canonical_tables.py` | `password_recoveries`: runtime | 8.1 |
| `web/services/recovery_scrub.py` (new, standard library) | scrubbed and known columns (the allow-list of RD13), schema check, `void_pending`, `cleanup`, snapshot scrub | 8.1 |
| `src/manage_accounts.py` | `registrations-cleanup`, `-restore-gate`, `-restore-status`, `-release-hold`, `clear-registration-limit` cover recovery; `require_registration_tables`; `restore-credential-diff --snapshot --replaced` (new, read-only, offline inputs, RD18 (a)), dispatched before and apart from `open_database`, the live `--db` only `stat`ed | 8.1 |
| `src/backup_sqlite.py` | `scrub_pending_recoveries` before `quick_check` | 8.1 |
| `web/services/registration.py` | `settle_send` refuses recovery rows | 8.1 |
| `deploy/learnpilot-restore.sh` | **Not edited**; static tests pin the existing calls | 8.1 |
| `docs/DEPLOYMENT.md` | Recovery operations; the restore credential runbook (RD18), including the preparation of the offline inputs, the repair categories, the validation, and the **required** maintenance isolation with its postponement rule and no exposed-window option (test RR15) | 8.1 |
| `web/services/registration.py` | public `control_blocked`, `raise_mark`, `writer_now` | 8.2 |
| `web/services/accounts.py` | `recovery_candidate(...)` (read under the caller's lock); `set_password` unchanged | 8.2 |
| `web/services/recovery.py` (new) | request, resend, redeem, cancel, `pending_view`, dispatch, settle; no caller | 8.2 |
| `web/blueprints/recovery.py` (new), templates | Routes of design 2.6 | 8.3 |
| `web/blueprints/account.py`, `login.html` | „Забравена парола?“ link and the `recovery=done` notice, while enabled | 8.3 |
| `web/app.py`, `web/production.py` | Settings under their own key; production behind the gate | 8.3 |
| `deploy/nginx/learnpilot.conf` | Design 7.5: `location = /recover` and `= /recover/resend` in `lp_register`, `= /recover/verify` in `lp_login`, a `/recover/` prefix with no limit; each with the repeated security headers, one `no-referrer` and `proxy_hide_header Referrer-Policy` | 8.3 |
| Documents | `AUTH_ROUTE_CONTRACT.md`, `ACCOUNTS_DESIGN.md` §12, `TESTING.md` with the implementation | 8.3 |

## Implementation scopes (design 10)

Sequential, never in parallel; scope 8.1 assigned to Codex by Gabriel's current
request (superseding its older proposed executor), and
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

## Decisions and what they govern

Gabriel's selections no longer block the definition of any scope: each
scope's content is defined by design 10 with the selected behaviour.
**8.1 implementation is authorized.** Each later scope starts only when Gabriel
authorizes it, after the previous one is merged and independently reviewed.

| Scope | Selected decisions it implements |
| --- | --- |
| 8.1 | RD3 (states and columns), RD13, RD18 repair (a) (the command), RD18 (unavailable evidence), RD18 (exposure) (documentation and RR15 only; no code) |
| 8.2 | RD1, RD2 (a), RD3, RD4, RD5, RD6, RD7, RD8 (a), RD10, RD11, RD12, RD17 |
| 8.3 | RD14, RD16 |
| No scope | RD9 (no notice in PR 8) |

| Still open | Blocks |
| --- | --- |
| RD15, the exact Bulgarian wording of design 2.7 and 2.8 (the proposed wording is used until then) | Activation only |
| D16 (privacy, minors, retention), extended to recovery data and the neutral notices of RD2 (a) | Activation only |
| The configuration of D17 (sender address and domain, credentials, provider or relay) | Activation only |
| The activation evidence of design 9, including the RD18 runbook rehearsed with the required maintenance isolation | Activation only |

## Activation prerequisites (none done)

Design section 9: the registration design's mail, capacity, Nginx and Linux
evidence, now for both purposes under one budget; recovery-specific Linux
evidence for the cleanup timer, restore and snapshot step; the restore
credential runbook rehearsed on staging with the required maintenance
isolation (the block's conditions verified on the host, by read-only inspection of listeners, established backend
connections, tunnel, forwarding and reverse-SSH processes and NAT rules,
renewal handled as the chosen form provides, argument-free logging kept, and
Gunicorn's access log read before reopening, and the postponement rule
walked through), the repair validated against a
reference time captured after activity stopped, including unavailable
replaced evidence and a snapshot from before migration 016;
measured timing of a recovery code and notice (RD2 (a)); the real Nginx accepting the recovery locations with one
`no-referrer` each; the lock time of `set_password` on the server; D16
extended to recovery; D17 configured; RD15 decided; independent security
review of every scope.

## Execution

Revisions 1, 2 and 3, the corrections of design 0.6 and the recording of
Gabriel's selections (design 0.7) written by Claude Opus 5.5, High. The
independent review of `48ee5bf` (APPROVE WITH NON-BLOCKING NOTES) covers
revision 3 only; the R1–R8 corrections and the decision recording came
after that SHA and are outside it. No review is an approval of a decision;
the decisions are approved by Gabriel's selection. Gabriel has now authorized
implementation scope 8.1 only; implementation is not self-approved and still
requires independent review. See `PASSWORD_RECOVERY_8_1_STATUS.md`.
