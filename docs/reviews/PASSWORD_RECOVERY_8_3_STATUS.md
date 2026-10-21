# Password recovery 8.3 — implementation and evidence

Gabriel authorized **scope 8.3 only** (the routes, templates, session slot, gate, settings,
Nginx locations, the audit adaptation and the contract documents). This is an implementation
report by its implementer (Claude Sonnet 5.5, High); it is **not an approval** and nothing in it
has been independently reviewed. Deployment, activation and every provider or host operation
remain unauthorized. **Every production gate remains OFF.** The implementation is **uncommitted and
unstaged**, left for Gabriel.

## Provenance and boundaries

- Worktree: `/Users/gabrielpilichev/dzi-generator-recovery-flow`. Branch:
  `feature/password-recovery-flow`.
- Starting point: HEAD `1eac2acb40b6319ada9dc73e783b5bed5eb36ee4` ("Start gated password recovery
  flow", an empty preparation commit) on `4f88f7c`, with a clean status. Nothing was staged,
  committed, pushed, merged or posted.
- **Reviewed SHAs are kept apart from later changes.** Scope 8.1 merged as `09fdb68` (PR #343); its
  review covered `ba4b9cd` only. Scope 8.2 merged as PR #344, squash commit `4f88f7c`; the independent
  reviews covered `19815bb` (the service) and `421993b` (the stricter audit caller rule), each exactly. The
  tree of `4f88f7c` equals the branch head `dd61938`, which differs from `421993b` in three documentation
  files (`docs/TESTING.md`, `PASSWORD_RECOVERY_8_2_STATUS.md`, `PASSWORD_RECOVERY_SCOPE.md`) only: those
  corrections, and everything in this report, are **outside every review**. The 8.2 code this scope builds
  on is the reviewed code, changed only by one added constant (below).
- No real `data/questions.db`, credentials, vault content, provider, running service, tunnel, server,
  deployment or activation was used or changed. Every database was a temporary migration/canonical build;
  mail was `InMemoryMailer`; no socket was opened. No application server, `cloudflared` or Flask process was
  started, stopped or killed.
- No migration or schema change; migration 025, the canonical JSONL, `deploy/learnpilot-restore.sh` and its
  ownership protocol have no diff against the starting HEAD (`git diff` shows none; a test also pins the restore
  script to its pre-recovery content). The only deploy change is the Nginx template's `/recover` block; with that
  block removed the template is byte-identical to its pre-recovery content (also pinned).
- No `nginx` binary exists on this Mac (checked), so the template was **not** run through `nginx -t`: the
  Nginx checks are static analysis only.

## What scope 8.3 adds

### Routes (`web/blueprints/recovery.py`, blueprint `recovery`)

| Route | Endpoint | Methods | Access | Integrity |
| --- | --- | --- | --- | --- |
| `/recover` | `recovery.start` | GET, POST | `public` | `same_origin` |
| `/recover/verify` | `recovery.verify` | GET, POST | `public` | `same_origin` |
| `/recover/resend` | `recovery.resend` | POST | `public` | `same_origin` |
| `/recover/cancel` | `recovery.cancel` | POST | `public` | `same_origin` |

Order on every route: the application's same-origin hook (every unsafe method) → the gate (the
generic 404 when `app.config["RECOVERY"]` is not a `RecoverySettings`) → the signed-in redirect (`303`
to the role home; a POST starts nothing) → the slot. Redemption: slot → `pending_view` →
`accounts.password_problem(password, view.email)` → the exact confirmation → `BEGIN IMMEDIATE` and
`recovery.redeem`. A test pins each step by recording the collaborators' calls (`ValidationOrderTest`).

- **Slot `rec`**: `{"v": 1, "t": <43 url-safe characters>, "next": <validated path or "">}`, nothing else;
  any other shape is ignored (25 malformed forms are swept on every route). No email, password or code is
  in a cookie, a URL or a redirect. `next` is validated (`registration_forms.accepted_next`) when a form
  is rendered, when it is stored, when the slot is read and again through `/login`'s own rule.
- **Transactions** (`_write`): `BEGIN IMMEDIATE`, one service writer, `COMMIT`. Every decision —
  `ISSUED`, `REDEEMED`, a counted wrong code — is acted on only after the commit returned: the slot, the
  durable log line, the dispatch and the redirect all follow it. Failures are told apart: an exception before
  the commit with a rollback that returned is **certain** (the page says the password was not changed); a
  commit that raised, or a rollback that raised, is **unknown** (the page says it could not be established).
  No failed commit claims a reset.
- **Sending**: the request that committed an issuance calls `recovery.dispatch` once with that very `Result`
  and no transaction open, then `recovery.settle_send` in a transaction of its own with the generation number
  **and** the issuance identity. A `NotSendable` or any other dispatch failure is logged, settles nothing and is
  never retried; a failed settlement leaves the ledger row `started`. Nothing reads a `started` generation back
  to send it.
- **Success** drops the slot and redirects to `/login?next=<validated>&recovery=done`; it signs no one in,
  starts nothing and does not touch `login_throttle` (no statement mentions it on any recovery route).
- **Headers**: `Cache-Control: no-store` and `Referrer-Policy: no-referrer` on every address under
  `/recover` (an `after_app_request` hook that tests the path, so the 403, the disabled 404, a 405 and an
  unknown path under `/recover/` carry them too).
- **Reads are reads**: a `HEAD` never submits (the handlers branch on `POST`), a GET writes nothing, and a GET
  with no usable recovery sets no cookie. The application's own stale-session cleanup is unchanged, so no claim
  is made that no `Set-Cookie` is ever sent: a stale account identity is still removed from the cookie.

### Pages and texts

`recover_request.html`, `recover_verify.html`, `recover_message.html` (new) and `login.html` (the link and the
notice). Every control has a label, errors are `role="alert"` and tied to their field by `aria-describedby` and
`aria-invalid`, the code input is `autocomplete="one-time-code"` with `inputmode="numeric"`, the password inputs
`autocomplete="new-password"`, all output is Jinja-escaped (including a kept `next`). The wording is the **proposed
wording of design 2.7 and 2.8**; its exactness is **not** approved (RD15 stays open). Texts the design leaves to the
route, and so are proposals of the same status: the request page's sentence, „Нещо се обърка и нищо не беше
записано. Опитай отново.“ and „Не успяхме да потвърдим дали заявката е записана. Опитай отново след малко.“ (a
failure of a request, a resend or a cancellation), and the composition of the login notice from design 2.7's two
sentences.

### Gate, configuration and `/login`

- `web/services/recovery.py`: `PRODUCTION_ACTIVATION = False`, its `__all__` entry and the docstring sentence that said
  the service has no caller (the only changes to the reviewed service; no behaviour changed); nothing in `web`, `src` or
  `deploy` assigns it or reads an environment variable for it.
- `web/recovery_settings.py` (new): `RecoverySettings(mailer, total_cap)` under `app.config["RECOVERY"]`, built from
  the same `mail.load_settings` / `mail.build_mailer`. `web/production.py`: `recovery_errors` and
  `recovery_production_settings` (the registration pattern for the recovery gate; mail problems are reported once
  while either gate is on), `configure_recovery_logging`. `web/app.py`: development enables recovery as it enables
  registration and warns by name when the settings are unusable.
- `/login` shows „Забравена парола?“ only while enabled, and `?recovery=done` shows a fixed notice by lookup, never by
  echo, only while enabled. It changes no state and proves no reset (a hand-built URL shows it).
- Registration, teacher, profile and Google gates are unchanged and independent (all 16 combinations of the first
  three and recovery are asserted).

### Nginx (`deploy/nginx/learnpilot.conf`)

`location = /recover` and `= /recover/resend` in `lp_register`; `= /recover/verify` in `lp_login`; the `/recover/`
prefix with no limit; each with the repeated `X-Content-Type-Options` and `X-Frame-Options`, exactly one
`Referrer-Policy: no-referrer` and `proxy_hide_header Referrer-Policy`. `/recovery` and `/recover-x` fall to
`location /`. No `proxy_set_header`, no maintenance configuration (RD18 is an operator step on the host).

## Audit adaptation (obligation 3 of the 8.2 status)

`tests/recovery_audit.py` (the opt-in whole-suite audit, never imported by the application):

- **Caller policy, narrow and explicit.** A service INSERT is allowed when the first caller outside the audit and
  the contiguous service frames is a recovery test/fixture module (unchanged, plus the new fixture module
  `tests.recovery_route_support`), **or** is exactly `web.blueprints.recovery` **and** a recovery test/fixture module is
  also up the same stack. A route reached from any other test, a lookalike module name, and every production-named
  intermediary between a recovery test or the blueprint and the service are denied and counted. "Any caller" is not the
  policy and the audit is not disabled. The zero-rows check of nonfixture databases and the counters-only output are
  unchanged.
- **The carried regression.** An INSERT prepared on a fixture connection and allowed, then the identical SQL text from a
  disallowed module on the same connection: with the audit as installed it is denied and counted as one violation
  (`test_a_prepared_insert_allowed_for_one_caller_is_authorized_again_for_the_next`); with the statement cache restored
  it is stored and **not** counted (`test_with_the_statement_cache_the_same_regression_goes_unseen`), which pins the
  limitation instead of leaving it a sentence in a document.
- **`cached_statements=0` assessed and adopted for the audit's connections** (`install(..., fresh_prepare=True)`, the
  default): each execution prepares afresh, so SQLite's authorizer sees each caller. Effect, measured on this machine with single alternating runs (indicative, not a benchmark): the 123 query-budget tests (those that count statements with `set_trace_callback`: browse, subject-ownership, teacher/new and quiz-start batching, account-owned attempts, account UI state) pass **123/123 in all three modes** — no audit, audit with the cache, audit with `cached_statements=0` — because the trace fires per execution and not per preparation, so the budgets are unaffected; wall time 5.0 s, 5.7 s and 6.3 s. 222 recovery and registration service tests: 12.4 s, 12.4 s and 12.9 s, all passing, 0 violations. The cost is a re-preparation of every statement, paid only in an interpreter that installs the opt-in audit; the application and the ordinary suite run (no audit) are unaffected. The full audited run below is a further data point, not a paired comparison.
- **What the audit still does not cover**, stated honestly: connections not opened through `sqlite3.connect` in an
  interpreter that loads the hook (the hook is per interpreter; `-S` probes and deployment interpreters are outside it);
  writes that are not an authorizer-visible `INSERT` of `password_recoveries` (the static scans cover those, and they do
  not match schema-qualified writes); a connection opened while a recovery test is on the stack is exempt from the
  emptiness check (8.1); the frames above the blueprint are only required to contain a recovery test, not to be a
  particular framework path. One machine, threads and temporary databases.

`tests/test_recovery_no_caller.py` replaces the obsolete "no caller" assertions (RZ2) by precise ones: only
`web/blueprints/recovery.py` (the public API only) and `web/production.py` (the gate constant only) name the service; the
CLI, backup, restore, WSGI and deploy files do not; the blueprint contains no SQL beyond `BEGIN IMMEDIATE` and commits
only in its one helper; a fresh interpreter importing the application shows exactly the four recovery rules and a disabled
recovery in production, and one importing the operator commands loads neither the service nor the blueprint.

## Test-plan mapping

New files (all temporary databases, `InMemoryMailer`, injected clock):

| Cases | Where |
| --- | --- |
| RB3, RB4, RB5 (route level), RV5, RT1/RT2 (route level) | `test_recovery_routes` (`RedeemTest`, `NextPreservationTest`, `SlotShapeTest`) |
| RW1 | `test_recovery_routes.MethodAndIntegrityTest`; the sweeps of `test_access_policy`, `test_session_cookie_samesite`, `test_dzi_blueprint`, `test_account_principal_bridge`, `test_account_login` (each lists the four endpoints) |
| RW2, RW7, accessibility | `test_recovery_routes` (`HeadersTest`, `VerifyPageTest`, `AccessibilityTest`) |
| RW3, RP4 | `test_recovery_routes` (`DisabledTest`, `GateTurnedOffTest`) |
| RW4 | `test_recovery_routes.SignedInTest` |
| RW5 | `test_recovery_routes` (`RedeemTest`, `SlotShapeTest`) |
| RW6 | `test_recovery_routes.SessionTransitionTest` |
| RW8, RW9, RW10 | `test_recovery_routes` (`RedeemTest`, `LoginIntegrationTest`, `ResendAndCancelTest`) |
| RG1, RG2, RG5, RG7 | `test_recovery_enumeration.SameAnswerTest` |
| RG3, RG4, RG6 | `test_recovery_enumeration.SendOutcomeTest` |
| RPoll1 to RPoll8 (HTTP level) | `test_recovery_enumeration.PollingTest` (RPoll6 and RPoll8 included), `PollingLogTest` (RLog5) |
| RE1 to RE8, RE11 (a), RK1, RK2, "correct code first" | `test_recovery_enumeration.CorrectCodeFirstTest` |
| RLog1, RLog2, RLog4 | `test_recovery_enumeration.LoggingTest` |
| RV4, RT4 (the throttle, through `/login`) | `test_recovery_enumeration.PollingTest` |
| RF1 to RF4, validation order, caller obligations 1 to 5 | `test_recovery_transactions` (`ValidationOrderTest`, `FailureTest`, `DispatchTest`) |
| RX at the routes, repeated and concurrent POSTs | `test_recovery_transactions.ConcurrencyTest` |
| RH1 to RH3, RCl1, RL1 to RL4, RL7 (released hold, key rotation, clearing; accumulating), RL9 | `test_recovery_limits` |
| RP1 to RP3, RP5, RP6 and the development start | `test_recovery_production` |
| RZ2 (replaced), the audit regressions | `test_recovery_no_caller` |

Not 8.3 (unchanged evidence): RA, RN, RR, RQ, RZ1 (8.1); RB1, RB2, RC, RV1 to RV3, RV6 to RV8, RS, RL5, RL6, RT3, RCl2 to RCl5 (8.2, service level). RL7's
**restore script** and every Linux/Nginx/provider case stay activation evidence (design 9); the released-hold test here drives the real
CLI (`registrations-restore-gate`, `registrations-release-hold --acknowledge-lost-history`) on a snapshot copy, not the script.

Existing tests changed (lists and one count, no behaviour assertion weakened): `test_access_policy` (matrix), `test_dzi_blueprint` (url map
snapshot), `test_session_cookie_samesite` (both sweeps), `test_account_principal_bridge` and `test_account_login` (the endpoint sets; the
latter excludes `/recover` from the "no reset, verify" path scan as it excludes `/register`), `test_registration_production` (the zone
counts 4 to 5 and 3 to 5), `test_production_deployment` (the `lp_login` count 4 to 5 and the verify location),
`test_google_oidc_production` (the list of modules that assign a `PRODUCTION_ACTIVATION` constant), `test_auth_blueprint` (the
list of registered blueprints and its count), `test_recovery_no_caller` (RZ2 replaced, the Nginx/restore pin, new audit cases), `recovery_audit`.

## Deviations and points needing judgement

Points where a choice was made or the documents do not fully agree. None is presented as approved.

1. **A retry after an unknown commit shows the ended page, not the redirect (design ambiguity).** Design 2.4 says the
   failure text for an unknown outcome is safe to retry because the retry is `NOT_PENDING (redeemed)` and redirects to `/login`
   (also test plan RF2 (ii)); but the same section's pre-check (step 2) says a row that is final, absent or past its time is
   "the same 'no usable recovery' page", and the brief fixes the order slot → `pending_view` → password rules → confirmation →
   service. `pending_view` returns `None` for a `redeemed` row, so a retry after a commit that took effect reaches the ended page
   („Заявката за нова парола е изтекла…“) and the service's `NOT_PENDING (redeemed)` redirect is reached only when the row
   becomes final between the pre-check and the transaction (two tabs; tested). Nothing is written, no success is claimed and
   the password stays the new one; the failure text itself tells the person to try to sign in. **A choice is needed** if the
   redirect is wanted: let a final row through to `redeem` (the service answers without writing), or add a read of the final
   state. I implemented the literal order and pinned the observed behaviour (`FailureTest`, `test_a_repeat_after_a_success…`).
2. **`HEAD` is a read here, and not in registration (observation, not changed).** The handlers branch on `POST`, so a `HEAD`
   carrying a body submits nothing. `registration`'s routes branch on `GET`: a `HEAD` with a form body **does** submit a
   registration and send a message (reproduced in a scratch probe: one pending row, one message). Browsers cannot send that
   cross-site, and the same-origin hook covers only unsafe methods, so this is an inconsistency to know about, not a finding I
   can size; registration was not touched (out of scope).
3. **A wrong method on a POST-only recovery route is a 405, not the generic 404, even when disabled** (routing precedes the view),
   exactly as for `/register/resend`. Design 2.6 says every `/recover...` address answers the generic 404; I followed the
   registration precedent for the method check and the gate for every method the route accepts.
4. **"Preserve next through … gate-off paths."** With the gate off every route is the generic 404 and carries nothing, so there is
   nothing to preserve; the failure, unavailable, ended and invalidated pages and the cancel redirect do preserve the validated
   `next`, validated again where used. Read this way; say if a different "gate-off path" was meant.
5. **Texts the design leaves to the route** (see above) are proposals of the same status as RD15's: the request page's sentence,
   the two failure texts for a request, resend or cancel, and the composition of the `recovery=done` notice. The `INVALIDATED`
   page answers 200 (the design gives no status for it).
6. **No `gate_off` log event.** Design 8.1 lists one; a disabled route is a 404 before anything else, and a line per probe would
   log nothing useful. The `*_decided` lines of the service and the route's durable lines (`requested`, `resent`, `wrong_code`,
   `redeemed`, `invalidated`, `expired`, `cancelled`, `unavailable`) are both written, so `user=` appears on two lines per reset
   (`redeemed_decided` and `redeemed`), both after a correct code.
7. **The response headers are set by an `after_app_request` hook that tests the path**, not by the blueprint's own
   `after_request`, so that a 403, a 405, the disabled 404 and an unknown path under `/recover/` carry them too (a blueprint
   hook does not run when no rule matched).
8. **Settings and starts.** `recovery_errors` / `recovery_production_settings` mirror registration's; a production start with
   both gates on reports the (shared) mail problems once. A development run with unusable settings prints a second warning,
   `Password recovery is disabled: ...`, beside registration's unchanged one.
9. **`pending_view` is judged first for an expired but still `pending` row.** A POST for it shows the ended page and writes
   nothing; the row is expired and scrubbed by the next cleanup (tested), as design 2.4 step 2 says.
10. **Audit.** The blueprint is allowed only while a recovery test drives it (the fixture list gained
    `tests.recovery_route_support`); the audit opens its connections with `cached_statements=0` by default. Both are opt-in test
    instrumentation. `fresh_prepare=False` is kept only so that the statement-cache blind spot stays pinned by a test; it is not a
    mode to run.
11. **The reviewed service changed by a constant.** `PRODUCTION_ACTIVATION = False` and one `__all__` entry, and the docstring
    sentence that said "Scope 8.2 has no caller". No behaviour changed.
12. **Test layout.** The plan proposed `test_recovery_routes`, `_enumeration` and `_production`; I added `_transactions` and
    `_limits` (the fault, dispatch and concurrency cases, and the budget/hold/clock cases) and `recovery_route_support`.

## Execution evidence

Python 3.12.15, SQLite 3.53.4, a fresh scratch venv from `requirements-dev.txt` (PyPI): Flask 3.1.3, Werkzeug 3.1.9, Gunicorn
23.0.0, PyOTP 2.10.0, cryptography 50.0.2, segno 1.6.6, Authlib 1.9.1, **joserfc 1.7.7**, requests 2.34.2, urllib3 2.8.0,
python-docx 1.2.0, lxml 6.1.3; `pip check`: no broken requirements. No dependency was pinned. All runs used
`PYTHONDONTWRITEBYTECODE=1`, `python -B` and a pycache prefix outside the worktree. Logs are in the session scratchpad, not committed.

- **New or rewritten modules** (tests): `test_recovery_routes` 72, `test_recovery_enumeration` 27, `test_recovery_transactions` 32,
  `test_recovery_limits` 8, `test_recovery_production` 31, `test_recovery_no_caller` 16 (12 before).
- **Mutation checks** on scratch copies, 26 mutants (password rules without the row's address, an unknown commit told as certain, a
  rollback failure told as certain, the slot kept after success, a `HEAD` handled as a write, no headers, success to `next`, the slot
  checked before the signed-in redirect, the redirect before the gate, the notice shown while disabled, a double dispatch, a settlement
  after `NotSendable`, an address in a log line, the slot stored before the commit, the slot dropped on a wrong code, the throttle cleared
  on success, the confirmation not compared, the password stripped, `next` not validated, a missing row not stopping, cancel not dropping
  the slot, a resend not dispatched, a reset that voids other rows (RD17), the prefix block without `proxy_hide_header`, verify in the
  register zone, the gate on): **all 26 killed** by the route or production modules.
- **First adjacent run** (the 70 recovery, registration, account, mail, production and sweep modules, audit installed): 1845 tests, 683.8 s,
  2 failures, both stale sweep lists (`test_production_deployment`: the `lp_login` count; `test_google_oidc_production`: the modules that
  assign a gate constant). Fixed; the two modules re-run: 90 tests OK.
- **Nginx, static**: no `nginx` binary. A strict `crossplane` 0.5.8 parse (a separate scratch venv) of the template inside
  `events {} http { include ... }` reports only two errors, the `map` entries (`default`, `POST`), identical in the pre-recovery template;
  no unknown directive, context or argument error in the new block (+4 locations, +3 `limit_req`, +4 `proxy_hide_header`). That is a parser
  check, **not** `nginx -t`.
- **First full run** (audit installed): 4724 tests, 1342.8 s, `FAILED (failures=2)`, both in `test_auth_blueprint` (the registered-blueprint list
  and count), a sweep I had not included in the adjacent set. Fixed (test only); the module re-run: 20 tests OK. Audit: 7413 process
  reports, 0 violations.
- **Final full run, on the final tree** (`python -B -m unittest discover -s tests`, audit installed, `cached_statements=0` default):
  **4724 tests, OK**, 1330.8 s, exit 0, no skip reported. Audit: 7414 process reports, 40876 connections, 35501 empty recovery-storage
  checks, 2094 fixture-storage, 3262 absent-storage and 22 unreadable observations, **0 violations** (counters, not distinct databases). For
  comparison only, the 8.2 audited full run had 4548 tests in 958 s on an unspecified machine state: this run added 176 tests and is not a
  paired comparison.
- `python3 -m py_compile` of every changed Python file passed; `git diff --check` is clean. The audit `.pth` hook was removed from the scratch
  venv afterwards and the two bytecode files it had written into `tests/__pycache__` were deleted; `git status --ignored` shows nothing else.
- Limits: one machine, threads and temporary databases; no browser, Linux, provider, Gunicorn-worker or real-Nginx evidence; documentation
  edits after the final run (this section only) were not followed by a rerun.

## Remaining gaps and activation evidence (none produced)

- **No `nginx` binary** was available, so `deploy/nginx/learnpilot.conf` was not run through `nginx -t`; the Nginx evidence is
  static (the effective location, headers and limits of every `/recover` address, resolved as Nginx resolves them). The real
  `nginx -t`, one `Referrer-Policy: no-referrer` observed on each address (a 404 and a 429 included) and the class-sized run
  mixing registration and recovery remain activation evidence.
- **No browser evidence**: autocomplete hints on a phone and a laptop, password managers, the back button after success, a second tab,
  the visual states and contrast of the new pages. The markup follows the existing pages' classes; nothing was rendered in a browser.
- **No Linux, systemd or provider evidence**: the cleanup timer, the restore script with a service-made pending recovery, the
  credential runbook rehearsed with the required maintenance isolation (no isolation rehearsal was done), delivery and timing of a
  recovery code and a notice (no equal timing is claimed), the capacity of `set_password` under the writer lock on the server.
- **SQLite 3.53.4** in the tests; the deployed library was not examined. Concurrency tests use threads with one connection per request
  on one machine, not Gunicorn workers.
- RD15 (the exact Bulgarian wording), D16 (privacy, extended to recovery and the neutral notices), the configuration of D17 and an
  independent security review of this scope are open. `AGENTS.md`'s route list was not edited.
- The audit's limits (above). The static scans do not match schema-qualified writes.
- The independent review of this scope, and Gabriel's decisions on points 1 to 4 above, are outstanding. This report approves nothing.

## Changed-file inventory

Tracked, modified (25):

- Code: `web/app.py`, `web/production.py`, `web/services/recovery.py`, `web/templates/login.html`, `deploy/nginx/learnpilot.conf`
- Tests: `tests/recovery_audit.py`, `tests/test_recovery_no_caller.py`, `tests/test_access_policy.py`, `tests/test_account_login.py`,
  `tests/test_account_principal_bridge.py`, `tests/test_auth_blueprint.py`, `tests/test_dzi_blueprint.py`,
  `tests/test_google_oidc_production.py`, `tests/test_production_deployment.py`, `tests/test_registration_production.py`,
  `tests/test_session_cookie_samesite.py`
- Docs: `docs/ACCOUNTS_DESIGN.md`, `docs/AUTH_ROUTE_CONTRACT.md`, `docs/DEPLOYMENT.md`, `docs/TESTING.md`,
  `docs/reviews/PASSWORD_RECOVERY_8_1_STATUS.md`, `PASSWORD_RECOVERY_8_2_STATUS.md`, `PASSWORD_RECOVERY_DESIGN.md`,
  `PASSWORD_RECOVERY_SCOPE.md`, `PASSWORD_RECOVERY_TEST_PLAN.md`

Untracked, new (12): `web/blueprints/recovery.py`, `web/recovery_settings.py`, `web/templates/recover_message.html`,
`web/templates/recover_request.html`, `web/templates/recover_verify.html`, `tests/recovery_route_support.py`,
`tests/test_recovery_routes.py`, `tests/test_recovery_enumeration.py`, `tests/test_recovery_transactions.py`,
`tests/test_recovery_limits.py`, `tests/test_recovery_production.py`, `docs/reviews/PASSWORD_RECOVERY_8_3_STATUS.md` (this file).

`git --no-pager diff --stat`: 25 files changed, 747 insertions(+), 133 deletions(-) (tracked files only). Not touched: migrations, canonical
JSONL, `deploy/learnpilot-restore.sh`, `src/`, `AGENTS.md`, `data/`, `vault/`.
