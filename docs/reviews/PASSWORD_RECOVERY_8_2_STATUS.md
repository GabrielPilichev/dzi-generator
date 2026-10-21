# Password recovery 8.2 — implementation and evidence

Gabriel authorized **scope 8.2 only** (the recovery service, with no caller). This
is an implementation report by its implementer (Claude Opus 5.5); the sections
below were written before the independent review and are not an approval. The
independent review of exact SHA `19815bb257124fcf3f2ea97f490db1d463637ea7` is
recorded under "Independent review and later changes". Scope 8.3 (routes,
templates, session slot, gate, settings, Nginx), deployment, activation and every
provider or host operation remain unauthorized. Every production gate remains OFF.

## Independent review and later changes

- **Reviewed commit.** The implementation was committed as
  `19815bb257124fcf3f2ea97f490db1d463637ea7` ("Add password recovery service
  without production callers", PR #344). An independent Claude review of exactly
  that SHA returned **APPROVE WITH NON-BLOCKING NOTES**. The review covers
  `19815bb` only; everything after it (the merge below and the cleanup at the end
  of this section) is outside it.
- **The 13 deviations** below were classified by the reviewer as requiring **no
  substantive product approval**. That is the reviewer's classification, not a
  separate approval by Gabriel, and the deviations are not restated as decisions.
- **The earlier full-suite error is not a green run.** The audit-instrumented full
  run before the commit (4546 tests, `FAILED (errors=1)`, see "Execution
  evidence") ended in an error, attributed to the `joserfc` 1.7.7 behaviour of one
  Google test fixture. No green final full run was claimed for `19815bb`.
- **PR #345 corrected that fixture.** `0a6835f` ("Fix Google algorithm-confusion test
  fixture", PR #345) builds the deliberately forged HS256/public-PEM token with
  standard-library HMAC instead of `OctKey.import_key` of a public key. It is a
  test-only change (no dependency pin, no skip, no production change). Per the PR's
  description, an independent Opus review approved its exact SHA `ac35e5e`, and
  the 85-test service suite passed on `joserfc` 1.6.1, 1.7.5, 1.7.6 and 1.7.7; it
  made no full-suite repeat. `origin/main` contains `0a6835f`; this branch merged it
  as `43cd1d0`. The error described above is therefore expected to be gone, but it
  is established only by the "Current verification" recorded below, not by PR #345.
- **Audit cleanup after the review (unstaged and uncommitted, for Gabriel).**
  `tests/recovery_audit.py` allowed a service insert whenever any recovery
  test/fixture frame was anywhere up the stack, so a recovery test calling a
  production-named intermediary that called `recovery.request` was allowed. It now
  walks past the audit's own frames and the contiguous `web.services.recovery`
  frames and requires the first external caller to be a recovery test/fixture
  module; the intermediary is denied. `tests/test_recovery_no_caller.py` gained
  deterministic subprocess regressions for direct allowed calls (service and
  fixture inserts), non-recovery-test denial and production-intermediary denial,
  and keeps the AST/no-production-caller checks and the counters-only audit output.
  No service behaviour changed.
- **Current verification** is recorded in "Current verification (after the
  review)" at the end of the execution evidence; it does not reuse the earlier
  runs.

## Provenance and boundaries

- Worktree: `/Users/gabrielpilichev/dzi-generator-recovery-service`.
- Branch: `feature/password-recovery-service` (its draft PR already existed).
- Starting point: HEAD `4d9d9d8e47faff4d179857d806ca00d05af6a6ae` ("Start password
  recovery service foundation", an empty preparation commit) on `09fdb68`, with a
  clean status.
- Scope 8.1 is merged at `09fdb68` (PR #343). Its independent review was of
  `ba4b9cd` (APPROVE WITH NON-BLOCKING NOTES); the same-file hardening that
  followed that review passed focused tests and was merged **without** a repeated
  whole-suite audit (`PASSWORD_RECOVERY_8_1_STATUS.md`).
- The implementation was **uncommitted and unstaged** while its evidence below was
  produced, then committed as `19815bb`. Every count below is therefore
  pre-commit verification by the implementer; nothing binds it to `19815bb`
  cryptographically. The audit cleanup above is uncommitted and unstaged. No commit,
  push, merge or GitHub post was made by the cleanup.
- No real `data/questions.db`, credentials, vault content, provider, running
  service, tunnel, server, deployment or activation was used or changed. Every
  database was a temporary migration/canonical build; mail was `InMemoryMailer`.
- Migration 025, canonical JSONL, the restore script and the Nginx template are
  unchanged (no diff); the approved schema was not modified.

## What scope 8.2 adds

### `web/services/registration.py`: three public wrappers (design 4.2)

| API | Behaviour |
| --- | --- |
| `control_blocked(conn, now, *, hold=True)` | `_blocked` on a validated `now`: the shared restore hold (unless `hold=False`), then the clock against the shared high-water mark; `HOLD`, `CLOCK_BEHIND` or `None`. Reads only; logs `restore_hold`/`clock_regressed` as registration does. |
| `writer_now(conn, clock)` | `_writer_now`: `TransactionRequired` with no transaction open, else the lock-taking no-op `UPDATE`, then one clock read. |
| `raise_mark(conn, now)` | `_raise_mark` on a validated `now`; never lowers, sets no hold; `TransactionRequired` with no transaction open. |

No registration behaviour changes; every existing registration suite passes unchanged.

### `web/services/accounts.py`: `recovery_candidate(conn, *, email=None, user_id=None)`

Exactly one selector (`ValueError` otherwise); `RuntimeError` when no transaction is
open (the caller owns the transaction and must already hold the write lock; the
function takes none). Returns `id`, `email`, `role`, `is_active`, `has_password`,
`email_verified_at`, `session_key` read from the row as it is now, or `None`; never
the hash. Email is compared exactly (the stored form). Below migration 021 the
missing column raises. `_USER_COLUMNS` and `set_password` are unchanged.

### `web/services/recovery.py` (new; standard library plus the existing services)

Public: `request`, `resend`, `redeem`, `cancel`, `pending_view`, `dispatch`,
`settle_send`, `binding_hash`, `session_key_digest`, `Outcome`, `Result`,
`PendingView`, `NotSendable`; it re-exports registration's `Availability`,
`Settlement` and `TransactionRequired`.

- **Writers** (`request`, `resend`, `redeem`, `cancel`, `settle_send`) need the
  caller's open transaction, take the write lock through `registration.writer_now`,
  read the clock once under it, never commit or roll back, and log decisions only
  (`..._decided`).
- **One order** (design 3.5): lock, clock, this browser's row (none/final: answered,
  nothing written), the shared hold (not for `cancel`) and clock, then writes.
  `request`: `registration.availability` (hold, clock, sender, circuit, total) and
  `registration.address_capped` before the inline recovery cleanup
  (`recovery_scrub.cleanup`), the voiding of the browser's previous row, the row and
  its issuance.
- **Eligibility** (design 1.3, RD1) via `recovery_candidate` at every issuance and
  again at redemption: active, `student`/`teacher`, a password, `email_verified_at`
  set; at redemption also the same `user_id`, the same `users.email` (exact) and
  `HMAC(SECRET_KEY, "recovery-session-key\0" + session_key)` compared with
  `compare_digest`. Admins, disabled, password-less and Google-only accounts (no
  `users.email`) never get a code; `user_identities` is never read.
- **Shadow rows** (RD2 (a)): a code hash of 32 random bytes that no eight digits can
  match, ledger kind `notice`, the neutral notice of design 2.8 to the typed address;
  issued, counted, dispatched and settled exactly like a code. Real and shadow
  requests, resends and dispatches run the same SQL statements (tested).
- **Codes**: `secrets.randbelow(10**8)`, zero-padded to eight digits (RD4); input
  trimmed at the ends, then exactly eight ASCII digits; keyed hashes with the row id.
- **Redemption** (RD3): design 2.4 steps 1–12 in order; scrypt only inside
  `accounts.set_password`, reached only after a correct code and a passed recheck;
  no session; no `login_throttle` statement (RD8 (a), RD10); no other recovery row
  read or written (RD17).
- **Resend** decides eligibility afresh (RD11): real ↔ shadow both ways; the wait is
  never waived (RD7).
- **Cancel** is allowed during the restore hold and refused while the clock is
  behind the mark (RD12); it leaves the hold and mark untouched.
- **Accounting** (RD5, RD6): ledger rows with `recovery_id` set and `pending_id`
  NULL, keyed by `registration.email_hash`; registration's caps, total budget,
  circuit, mark, hold and 48-hour cleanup count them. No parallel counter.
- **Dispatch** (design 3.6): refuses (`NotSendable`, nothing sent) with a
  transaction open and unless the result is, field for field, the committed,
  unsettled, current issuance: identity, generation, recovery id, `pending_id`
  NULL, kind, address key, the bound account's `users.email` for a code, token →
  binding, exact message and digits → code hash. One mailer call per call; no retry;
  mailer exceptions are `UNKNOWN`.
- **Settlement** (RD7): `NOT_THIS_ISSUANCE` unless the row has that identity and
  `recovery_id` set; first settlement wins; changes the ledger row only, never a
  recovery row or a registration row.

## Caller obligations (for scope 8.3; nothing calls the service now)

1. `BEGIN IMMEDIATE` before each writer; commit after **every** returned outcome
   (refusals included); roll back after any exception.
2. Act on nothing before the commit has returned: no session slot written, nothing
   dispatched, no success page. Outcomes are decisions until then.
3. A commit (or rollback) that raised has an outcome the service cannot establish:
   never present it as a successful reset; answer the uncertain text of design 2.4.
   A retry with the same code is safe either way (tested: it redeems, or is
   `NOT_PENDING (redeemed)` and writes nothing).
4. After a committed `ISSUED`: `dispatch` exactly once with no transaction open,
   then `settle_send` in a transaction of its own. Never settle after `NotSendable`;
   never retry a dispatch.
5. Before `redeem`, validate in this order: the session slot, then `pending_view`,
   then `accounts.password_problem(password, view.email)`, then the exact
   confirmation (the service only refuses a password that fails `password_problem`
   without the address, before reading anything). See the 8.3 checklist below.
6. `email` passed to `request` must already be `registration_forms.public_email`'s
   output; `sender_busy` comes from `mail.sender_busy()`; `total_cap` from the
   settings; the unlocked `availability`/`address_capped` pre-reads of design 2.3
   are the caller's.
7. Log the durable events of design 8.1 after the commit; never log the address,
   code, token, password, any hash, key or digest, identity, message or the
   shadow state.

## Obligations carried into the 8.3 checklist

Recorded with the review of `19815bb`; none is built or authorized by this report.
Scope 8.3 starts only when Gabriel authorizes it.

1. **Route validation order.** The redeem route validates, in this order: the
   session slot → `recovery.pending_view` → `accounts.password_problem(password,
   view.email)` → the exact confirmation, and only then calls `redeem` inside
   `BEGIN IMMEDIATE` (caller obligation 5). A test pins the order, including that a
   refusal at an earlier step answers before a later step reads anything.
2. **Real route tests for the service-only seams of deviation 11.** The 8.2 tests
   drive the calls a route would make; 8.3 replaces each with a request through the
   real route: `/account/password` (RK1), TOTP enrolment/confirmation, and
   `/login`'s throttle (RV4, RT4, RPoll8). RL7's released hold remains
   restore-script evidence of design 9, not a route test.
3. **Audit policy is adapted deliberately when routes become legitimate callers.**
   The audit denies a service insert unless the first caller outside the audit and
   the service is a recovery test/fixture module, so a route-driven insert (whose
   first external caller is the blueprint) will be denied by design. 8.3 must change
   `tests/recovery_audit.py` and `tests/test_recovery_no_caller.py` on purpose
   (RZ2 is replaced by RW3 and RP1, design 10): name the one blueprint module as an
   allowed caller, keep every other production-named intermediary denied with
   regression tests, keep the zero-rows check of nonfixture databases and the
   counters-only output, and have the change reviewed. The audit is not disabled,
   and "any caller allowed" is not an acceptable adaptation.

## Approved-case mapping (test plan → 8.2 evidence)

| Cases | Where (all new unless noted) |
| --- | --- |
| RB1, RB2 | `test_recovery_service` (`RequestTest.test_rb1…`, `InputRuleTest`) |
| RB3, RV1, RV2, RV4, RV5, RV7 | `test_recovery_redeem` |
| RC1–RC7 | `test_recovery_service` (`RequestTest`, `ResendTest`) |
| RC8 | `test_recovery_redeem.WriterOrderTest` (statement sequence captured) |
| RE1–RE8, RE10 (8.2 part), RE11, RK1–RK3, RV8 | `test_recovery_eligibility` (CLI `set-role`, `disable`, `enable`, `sign-out`, `set-password`, `totp-reset` through `manage_accounts.main`) |
| RE9 | `test_recovery_service` (Google `email_at_link`, no `user_identities` statement) |
| RV3, RV6, RPoll1–RPoll8, RLog5 | `test_recovery_polling` (service level) |
| RX1–RX8 | `test_recovery_concurrency` (threads, one connection each, barrier) |
| RF1, RF2, RF4 | `test_recovery_redeem.FailureTest` (RF2 (i) is a real SQLite commit failure) |
| RF3, RF5 | `test_recovery_service.TransactionContractTest` |
| RI1–RI4 | `test_recovery_service.IssuanceIdentityTest` |
| RS1–RS5, RA3 (service rows), RA6 (service part) | `test_recovery_ledger` |
| RL1–RL9 | `test_recovery_ledger` (RL6 random mixed schedules, docstring states the three assumptions; RL7 two clock steps → 13 codes, key rotations, clearings, a restore-hold release) |
| RT1–RT4 | `test_recovery_redeem.LimitsTest` |
| RCl1–RCl5, RH1–RH3 | `test_recovery_redeem.HoldAndClockTest` |
| RLog1–RLog3 | `test_recovery_service.LoggingTest` |
| RR1–RR3, RR5, RN1–RN4, RQ1 (8.2 part) | `test_recovery_service_operations` (service-made rows, real CLI and `backup_sqlite`) |
| RZ1 (narrowed), RZ2 | `test_recovery_no_caller` (modified; see deviation 9) |
| Design 4.2 helpers | `test_recovery_service.PublicHelpersTest` |

Not 8.2 (scope 8.3): RB4, RB5, RG1–RG7, RW1–RW10, RP1–RP6, RLog4 and the
route-level halves of RPoll, RF1/RF2 (texts, status codes, slot).

## Deviations and points needing judgement

1. **Password pre-check in `redeem`.** The design leaves the password checks to the
   blueprint. `redeem` additionally raises `ValueError` before reading anything when
   `accounts.password_problem(password)` (without the address) refuses the value, so
   a malformed password can never consume an attempt or the code. Equality with the
   address is still decided by `set_password` at step 11 (an exception and rollback,
   never a success).
2. **`pending_view`'s clock check** reads the shared mark itself (quietly) instead of
   calling `registration.control_blocked`, which logs; registration's own
   `pending_view` does the same with its private `_clock_behind`. The comparison
   (`stamp(now + CLOCK_SKEW) < high_water_at`) is duplicated in one read-only place.
3. **`recovery_candidate` raises without an open transaction.** It cannot verify the
   write lock itself (a deferred `BEGIN` also counts as open).
4. **Dispatch reads `users` for every row.** Design 3.6 requires the `users.email`
   check for a real row; the same `SELECT … WHERE id = ?` runs for a shadow (with
   NULL, matching nothing) so the statement sequence does not depend on the kind.
   The result is used only for a code.
5. **`Result.kind` is excluded from `repr`** (it reveals the shadow state), beyond
   RLog3's list.
6. **Request inserts the row with no live code** (`is_shadow = 1`, no code) and the
   issuance's single `UPDATE` sets real or shadow, in the same transaction, so both
   kinds run identical statements.
7. **`NotSendable` is recovery's own class** (a registration `NotSendable` is not
   raised for recovery results); `Settlement`, `Availability` and
   `TransactionRequired` are registration's.
8. **No build gate yet.** `recovery.PRODUCTION_ACTIVATION` is design 7.1 but design
   10 assigns the gate to 8.3; it is not defined in 8.2 (nothing could read it).
9. **RZ1 narrowed, deliberately.** The 8.1 test asserted that
   `web/services/recovery.py` does not exist and that no `web`/`src` module inserts
   into recovery storage. It now asserts that the service is the **only** such
   writer and that its writes are exactly the two inserts of design 3.3, its own
   row updates and the guarded ledger settlement (no `DELETE`, no `users` update,
   no throttle/TOTP/identity/registration-row statement). RZ2 adds static scans of
   every `web`, `src` and `deploy` file (imports, string references, writer calls,
   CLI handlers) and fresh-interpreter imports of `web.app`, `web.wsgi`,
   `web.production`, every blueprint and the operational commands, asserting
   `web.services.recovery` is not loaded, no URL rule or request hook refers to
   recovery, and the populated production database still has zero recovery rows
   and zero recovery-purpose ledger rows. Legacy `src` scripts with optional
   dependencies are scanned statically, not imported.
10. **Audit adapted, not disabled.** `tests/recovery_audit.py` allows a service
    insert only when the first caller outside the audit and the contiguous service
    frames is a recovery test or fixture module; any other path is denied and
    counted, which subprocess tests prove. Its emptiness check now also counts
    recovery-purpose ledger rows. (`19815bb` allowed it whenever such a module was
    anywhere on the thread's stack; the stricter rule is the post-review cleanup
    above, outside the reviewed commit.)
11. **Test seams instead of routes**, where the plan names route behaviour:
    `/account/password` (RK1) is exercised through the service calls the route makes;
    TOTP enrolment/confirmation through `totp.enroll`/`confirm` (the CLI requires an
    admin, which is already ineligible); `/login`'s throttle (RV4, RT4, RPoll8)
    through the `login_throttle` calls `/login` makes. RL7's released hold is a
    snapshot copy gated and released through the real CLI, not the restore script.
12. **Test file layout.** The plan's proposed `test_recovery_settlement.py` is
    folded into `test_recovery_ledger.py`; `test_recovery_service_operations.py`
    holds the repeated operations tests.
13. **Log event names** are decisions (`request_decided`, `resend_decided`,
    `wrong_code_decided`, `invalidated_decided`, `redeemed_decided`,
    `cancel_decided`, `expired_decided`, `settlement_decided`) plus `send_started`,
    `dispatch_refused`, `dispatch_failed`, `settlement_refused`,
    `code_evidence_invalid`; design 8.1's durable names are the 8.3 caller's.

## Execution evidence

The runs up to "Current verification" are the implementer's **pre-commit**
verification of the tree that became `19815bb`. They were not repeated for the
commit, the PR #345 merge or the audit cleanup, and none of them is green as a
whole suite.

Python 3.12.15, SQLite 3.53.4, fresh scratch venv from `requirements-dev.txt` (PyPI).

- New recovery suites passed individually during development; focused + adjacent
  run (49 modules): **1612 tests OK** in 598.6 s.
- Mutation checks on scratch copies: 9 real mutants (import from a blueprint,
  any-purpose settlement, no digest compare, `not_sent` retiring the code, throttle
  clearing, voiding other rows, expiry before the gate, unverified eligible, no
  account check in dispatch) all killed.
- **First full run** (audit-instrumented): 4546 tests, failures=1, errors=1 in
  1289.7 s; audit 7398 process reports, 0 violations. The failure was a **test
  defect** in RX6 `[together]`: the subtests' three `not_sent` settlements opened
  the shared circuit, so the raced resend could be refused when the settlement won.
  Fixed in the test only (an accepted ledger row between subtests); 8 repeated runs
  of RX6 passed. No service code changed after the first run.
- **Final full run, on the final state** (audit-instrumented,
  `PYTHONDONTWRITEBYTECODE=1 python -B -m unittest discover -s tests`): **4546
  tests, FAILED (errors=1)** in 1275.0 s. Audit: 7397 process reports, 39232
  connections, 35438 empty recovery-storage checks, 513 fixture-storage, 3262
  absent-storage and 22 unreadable observations, **0 violations** (counters, not
  distinct databases; limits as in 8.1).
- The one error, `test_google_oidc_service` ... `test_a_token_whose_signature_does_not_verify_is_refused`,
  was **pre-existing and environmental** at the time: the fresh venv installed
  `joserfc` 1.7.7 (allowed by `requirements.txt`, `>=1.6.1,<2`), which refuses
  `OctKey.import_key` of a PEM public key; 8.1's evidence used 1.7.5. The same test
  errored identically on an export of the untouched base `4d9d9d8` with the same
  venv, and all 85 `test_google_oidc_service` tests passed on this tree with
  `joserfc` 1.7.5. It was left unfixed in 8.2 (outside its scope). **PR #345
  (`0a6835f`) has since corrected the fixture** (see "Independent review and later
  changes"); this run is not re-labelled green.
- Versions: Flask 3.1.3, Werkzeug 3.1.9, Gunicorn 23.0.0, PyOTP 2.10.0, cryptography
  50.0.2, segno 1.6.6, Authlib 1.9.1, joserfc 1.7.7 (`pip freeze` kept with the
  local logs).
- Local logs (temporary, not committed): `<scratchpad>/focused-adjacent.log`,
  `full-suite.log`, `full-suite-2.log`, `full-audit.jsonl`, `full-audit-2.jsonl`,
  `pip-freeze.txt`. The suite ran in place; the only stray file (bytecode written by
  a `pip freeze` run while the audit hook was installed) was removed.

### Current verification (after the review)

On the working tree at `43cd1d0` (which contains `19815bb` and PR #345's `0a6835f`)
plus the unstaged audit cleanup and documentation changes of this section. These
runs are new; they do not reuse any run above.

- Python 3.12.15, SQLite 3.53.4, a new scratch venv from `requirements-dev.txt`
  (PyPI): Flask 3.1.3, Werkzeug 3.1.9, Gunicorn 23.0.0, PyOTP 2.10.0,
  cryptography 50.0.2, segno 1.6.6, Authlib 1.9.1, **joserfc 1.7.7**, requests
  2.34.2, urllib3 2.8.0, python-docx 1.2.0, lxml 6.1.3. `pip install -r` met
  `requirements.txt` and `pip check` reported no broken requirements. No dependency
  was pinned and no production Google code changed.
- Audit setup as documented in `TESTING.md`: a `.pth` line in the scratch venv
  installs `tests.recovery_audit` (report file outside the repository).
- **Focused run** (all 12 `tests/test_recovery_*.py` modules, audit installed): **190
  tests OK**; the audit reported 0 violations.
- **Regression check.** The new production-intermediary regression was run against
  `19815bb`'s `tests/recovery_audit.py` in a scratch copy: it **failed** (three
  chains, each wrongly allowed and storing one row); it passes with the stricter
  rule. The direct-allowed and non-recovery-test cases pass under both rules.
- **Final full run, once**, on the final code and documentation state
  (`PYTHONDONTWRITEBYTECODE=1 python -B -m unittest discover -s tests`, audit
  installed): **4548 tests, OK**, 957.97 s, exit status 0, no skips reported.
  Audit: 7397 process reports, 39234 connections, 35439 empty recovery-storage
  checks, 515 fixture-storage, 3262 absent-storage and 21 unreadable observations,
  **0 violations** (counters, not distinct databases; limits as in 8.1). This
  result was written afterwards; the documentation-only edit was not followed by a
  rerun.
- Compilation of the changed Python files and `git diff --check` passed.
- Limits: the audit sees only connections made through `sqlite3.connect` in
  interpreters that load its hook, and a connection opened while a recovery test is
  on the stack is exempt from the emptiness check (8.1); the run is one machine,
  threads and temporary databases, no Linux, provider or real-database evidence.

## Remaining gaps and activation evidence (none produced)

- Scope 8.3 entirely: routes, templates, the `rec` slot, `/login` link and notice,
  the gate and its production/development wiring, settings, Nginx locations and
  the route-level enumeration, polling, header and production tests.
- Design 9, unchanged: Linux/systemd evidence (cleanup timer, restore with a
  service-made pending recovery, snapshot step), the restore credential runbook
  rehearsed with required maintenance isolation, measured timing of a recovery code
  and a notice through the chosen provider (no equal timing is claimed), the
  measured lock time of `set_password` on the server, real `nginx -t`, D16 extended
  to recovery and its neutral notices, D17 configured and RD15 decided. The
  independent review of `19815bb` is recorded above; it does not cover later
  changes, and design 9's independent security review of every scope before
  activation is not claimed by it.
- The deployed SQLite version was not examined; tests ran on SQLite 3.53.4.
- Concurrency tests use threads with one connection each on one machine; they are
  not evidence of behaviour under Gunicorn workers.
- The audit observes only connections opened through `sqlite3.connect` in
  interpreters that load its hook, and the static scans do not match
  schema-qualified writes (`INSERT INTO main.password_recoveries`); both limits are
  as in 8.1.

## Changed-file inventory

### Committed as `19815bb` (the reviewed implementation)

Tracked modifications:

- `web/services/registration.py`, `web/services/accounts.py`
- `tests/recovery_audit.py`, `tests/test_recovery_no_caller.py`
- `docs/ACCOUNTS_DESIGN.md`, `docs/DEPLOYMENT.md`, `docs/TESTING.md`
- `docs/reviews/EMAIL_REGISTRATION_DESIGN.md`, `docs/reviews/EMAIL_REGISTRATION_SCOPE.md`
- `docs/reviews/PASSWORD_RECOVERY_8_1_STATUS.md`, `docs/reviews/PASSWORD_RECOVERY_DESIGN.md`,
  `docs/reviews/PASSWORD_RECOVERY_SCOPE.md`, `docs/reviews/PASSWORD_RECOVERY_TEST_PLAN.md`

New files:

- `web/services/recovery.py`
- `tests/recovery_service_support.py`
- `tests/test_recovery_service.py`, `tests/test_recovery_eligibility.py`,
  `tests/test_recovery_redeem.py`, `tests/test_recovery_ledger.py`,
  `tests/test_recovery_concurrency.py`, `tests/test_recovery_polling.py`,
  `tests/test_recovery_service_operations.py`
- `docs/reviews/PASSWORD_RECOVERY_8_2_STATUS.md`

### Uncommitted and unstaged since the review (for Gabriel)

- `tests/recovery_audit.py`: first-external-caller validation of service inserts.
- `tests/test_recovery_no_caller.py`: subprocess regressions for direct allowed
  calls, production-intermediary denial and non-recovery-test denial.
- `docs/TESTING.md`: the audit rule as it now is.
- `docs/reviews/PASSWORD_RECOVERY_8_2_STATUS.md` (this file) and
  `docs/reviews/PASSWORD_RECOVERY_SCOPE.md`: provenance, review record, 8.3
  obligations and verification record.
