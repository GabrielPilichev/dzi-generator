# Password recovery 8.3 — implementation and evidence

Gabriel authorized **scope 8.3 only** (the routes, templates, session slot, gate, settings,
Nginx locations, the audit adaptation and the contract documents). This is an implementation
report by its implementer (Claude Sonnet 5.5, High); it is **not an approval**. Deployment,
activation and every provider or host operation remain unauthorized. **Every production gate
remains OFF.**

**Status, in order of authority:**

1. The implementation was committed as `dd39615019ae6726717b8479eb08848543732c21` ("Add password
   recovery routes behind disabled gate", parent `1eac2ac`), PR #346. An independent review (Claude
   Opus) of exactly that SHA returned **REQUEST CHANGES**: one blocking finding (F1, a repeated
   redemption), four lower findings and one separate, pre-existing registration finding. See
   "Independent review of `dd39615`".
2. The cleanup that corrects F1 and the documents was committed as
   `2c2a579daef2a8aa1ee36e7e0c82018502dc5307` ("Redirect repeated recovery redemption to login"; its single
   parent is `dd39615`). An independent follow-up review of exactly that SHA returned **APPROVE WITH
   NON-BLOCKING NOTES**; **the original repeat-redemption blocker (F1) is resolved.** That verdict approves
   neither deployment nor activation, nor the RD15 wording, nor any activation prerequisite. See
   "Follow-up review of `2c2a579`".
3. Documentation edits made **after** `2c2a579` (this status block and the sections that record the
   follow-up, the alignment of design 2.4 step 2, the retention wording, the provenance lines of this report,
   the scope document and the route contract) are **not part of the reviewed SHA and are outside both
   reviews.** This sentence does not say whether they are committed; the git history does.
4. **Evidence is kept apart.** The sections below headed "Execution evidence" are the **author's** and
   describe the tree before `dd39615`; the author's 4724-test full run and 26-mutant campaign **predate the
   correction** and were not repeated. The follow-up reviewer's own runs are in "Follow-up review of
   `2c2a579`"; the reviewer did not repeat the full suite or the mutation campaign either.

## Provenance and boundaries

- Worktree: `/Users/gabrielpilichev/dzi-generator-recovery-flow`. Branch:
  `feature/password-recovery-flow`.
- Starting point of the original implementation: HEAD `1eac2acb40b6319ada9dc73e783b5bed5eb36ee4`
  ("Start gated password recovery flow", an empty preparation commit) on `4f88f7c`, with a clean
  status. The implementation was left uncommitted by its author and was **later committed as
  `dd39615`** (the SHA that was reviewed first). The cleanup of the review's finding started from `dd39615`
  with a clean status and was **later committed as `2c2a579`** (the SHA reviewed second); its author staged,
  committed, pushed, merged and posted nothing.
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
| RB3, RB4, RB5 (route level), RT1/RT2 (route level) | `test_recovery_routes` (`RedeemTest`, `NextPreservationTest`, `SlotShapeTest`) |
| RV5 (route level) | `test_recovery_routes.RepeatedRedemptionTest` (all seven tests) and `RedeemTest.test_a_repeat_after_a_success_whose_response_was_lost_goes_to_login_and_writes_nothing`; the cookie-copy test in `SlotShapeTest`. **Corrected in the cleanup**: at `dd39615` this row named `RedeemTest`, whose repeat test asserted the opposite of RV5 (the ended page, 200). |
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
| RF1 to RF4, validation order, caller obligations 1 to 5 | `test_recovery_transactions` (`ValidationOrderTest`, `FailureTest`, `DispatchTest`). RF2 (ii) (a commit that took effect and then raised, then a retry to `/login` writing nothing) is `FailureTest.test_a_commit_that_took_effect_and_then_raised_is_told_as_unknown_and_its_retry_goes_to_login`, **corrected in the cleanup**; at `dd39615` it asserted the ended page. |
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

1. **A retry after an unknown commit (and any repeat of a redemption) showed the ended page, not the
   redirect. Reported by the author as a design ambiguity; the review found it a defect (F1); corrected in
   the cleanup committed as `2c2a579`.** Design 2.4's step 2 reads "None, final, or past its time by the read →
   the same 'no usable recovery' page", while its failure paragraph, its outcome table (`NOT_PENDING` with
   `redeemed` → `303` to `/login?next=<next>`), test-plan RV5 and RF2 (ii) and the 8.2 caller obligation 3
   all expect the redirect. The author followed step 2 literally and pinned the opposite; that was a wrong
   reading. The correction follows the explicit outcome table, failure paragraph and test plan. Step 2's
   wording, the sentence the author misread, was not touched by `2c2a579`; it was **aligned afterwards, in a
   documentation-only edit outside the reviewed SHA**, to resolve the contradictory wording in favour of the
   requirement that already existed. No new product approval is claimed.
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

## Execution evidence (the author's, on the tree before `dd39615`; not repeated for the cleanup)

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

## Independent review of `dd39615` (REQUEST CHANGES)

Independent technical review by Claude Opus of exactly `dd39615019ae6726717b8479eb08848543732c21`
(the branch head; `4f88f7c` and `1eac2ac` are ancestors, and only `1eac2ac` and `dd39615` sit on top of
`main`). Verdict: **REQUEST CHANGES**, for one reason: approved cases RV5 and RF2 (ii) were not met and the
PR's own tests and contract text described the opposite behaviour as correct. It approves neither
deployment nor activation. The report is the reviewer's final message of its session; it is not stored in
the repository, and this section is a summary of it, not a quotation. The findings, with what this cleanup
did about each:

| Finding | Severity | Reviewer's statement | Handled in the cleanup |
| --- | --- | --- | --- |
| F1 | Medium, **blocking** | A repeated redemption after a reset that worked showed the "expired" page (200) instead of redirecting to `/login`. `pending_view` returns `None` for a `redeemed` row; the redirect branch was reached only in a race. Pinned by `test_recovery_transactions` (RF2 (ii)), `test_recovery_routes` (the repeat test and the cookie-copy test) and written as contract text; the status table named a test asserting the opposite for RV5. | Corrected in `2c2a579`; the follow-up review confirms it resolved (below). |
| F2 | Low | With the gate off, `OPTIONS` answers 200 with `Allow`, `PUT`/`PATCH`/`DELETE` and `GET`/`HEAD` on the POST-only routes answer 405, an unknown path under `/recover/` answers 404; also in a real production start. Registration behaves the same. Design 2.6 and the contract say the generic 404. Deviation 3 covered the 405s, not the `OPTIONS` 200. | **Not changed. Unresolved** (below). |
| F3 | Low | Stale provenance: "uncommitted" in this report and in `PASSWORD_RECOVERY_SCOPE.md`, but the work is `dd39615`. | Corrected in both in `2c2a579`. The follow-up review found the same defect again in the lines that cleanup added ("uncommitted" at a SHA where it was committed); corrected by the later documentation edit. |
| F4 | Low, routine (deviation 9) | A POST for an expired but still `pending` row shows the ended page and drops the slot but does not mark the row expired; the typed address and code hash stay until the next cleanup. Design 2.4 step 2 and 2.5 disagree slightly. | Not changed; recorded (below). |
| F5 | Info | The verify page's sentence „Не получи код? … след минута.“ is another route-chosen text not listed in deviation 5; the verify form has `autocomplete="off"` with field-level hints, so whether browsers offer the one-time code and new-password suggestions needs a browser check. | Not changed; recorded (below). |
| Registration `HEAD` | Medium, latent, **pre-existing, not this PR** | See "Prerequisite before registration or recovery activation". | Not changed here; recorded. |

**What the reviewer verified without finding a defect** (its words, summarized): the transactions and
delivery boundaries (the route's own `BEGIN IMMEDIATE` … `COMMIT`, certain rollback and uncertain outcome told
apart, dispatch only after commit, identity-matched settlement, a stale issuance refused with
`NotSendable`); credentials (no hash for wrong codes, exactly one for the correct one, the password stored
exactly, no sign-in, `login_throttle` unchanged, the account's other browser signed out, a password change
through `/account/password` before redemption making the code `INVALIDATED`); enumeration (eligible
student and teacher, absent, admin, unverified and disabled identical in status, `Location`, page with the
address masked, header names, cookie attributes, session shape and every log line; only the email subject
differs, by design); the full method sweep, `next` round trips, cancel, expiry, hold, clock refusal and
cross-browser polling; production wiring (a production start with SMTP and six override-looking variables
kept the settings `None` and every route 404); the audit adaptation (four callers denied and counted, the
recovery-test-driven blueprint allowed, the cached INSERT reuse visible with the cache and closed without
it); the Nginx template by reading it only; and the templates' labels and escaping.

**The reviewer's evidence, kept apart from the author's:** Python 3.12.15, SQLite 3.53.4, Flask 3.1.3,
joserfc 1.7.7, a fresh scratch environment, temporary databases, the Flask test client and threads only.
The 15 new or changed modules without the audit: 541 tests OK (55.8 s). 48 adjacent modules with the audit
installed: 1516 tests OK (592.3 s), 4354 process reports, 11,897 connections, 9344 empty-storage checks,
0 violations. Per-module counts matched the author's (72 / 27 / 32 / 8 / 31 / 16). Nine probe modules of
its own, and a trial fix on a separate copy that passed its probe and needed only the three pinned tests
and the RZ2 service-function list changed. **The reviewer did not reproduce** the author's 4724-test full
run or the 26 killed mutants. Limits: no browser, Linux, Gunicorn, real Nginx, provider or SMTP-timing
evidence; the request-timing comparison (about 1.4 ms in both cases, in-memory mailer) was indicative only.
The author's claims in the sections above (full run, mutants) therefore rest on the author's own runs.

## Cleanup after the review (committed as `2c2a579`)

**Status: committed as `2c2a579daef2a8aa1ee36e7e0c82018502dc5307` on top of `dd39615`, and independently
reviewed at exactly that SHA (APPROVE WITH NON-BLOCKING NOTES, below). The description in this section is
the author's account of the cleanup as it was written; the evidence under "Verification of the cleanup" is the
author's. All production gates remain OFF.** Scope: F1 and the documents only.

**Code**

- `web/services/recovery.py`: one new read-only function, `was_redeemed(conn, binding) -> bool`, added to
  `__all__`. It reads the one row of the keyed binding hash through the existing `_row` (a malformed,
  non-text or non-hash binding is `False` without a query), never `users`, writes nothing and logs
  nothing. It answers `True` only for state `redeemed`; `pending`, `expired`, `void` and an absent row are
  all `False`, so no other state of an ended row is told. It does add one read to the reviewed 8.2 service.
- `web/blueprints/recovery.py` (`_redeem`): the order is unchanged (gate, signed-in redirect, slot,
  `pending_view`, password rules, confirmation, transaction). When `pending_view` returns `None`, the slot
  is dropped as before, and then, if `was_redeemed(conn, binding)` (the binding is the one derived from the
  validated slot, never a raw token or request target), the answer is `303` to
  `/login?next=<validated next>` **without** `recovery=done`. It happens before the password, the
  confirmation or the code are judged: no write, no mail, no hash, no session, no `login_throttle`
  statement, no transaction. Every other `None` is the ended page as before. The race branch
  (`NOT_PENDING` with `final_state == "redeemed"` from the service) and every transaction and dispatch
  boundary are untouched.

**Tests** (79 in `test_recovery_routes`, was 72; 32 in `test_recovery_transactions`, unchanged count)

- Corrected (they asserted the opposite of RV5 and RF2 (ii)):
  `RedeemTest.test_a_repeat_after_a_success_whose_response_was_lost_goes_to_login_and_writes_nothing`, the
  last assertion of the cookie-copy test in `SlotShapeTest`, and
  `FailureTest.test_a_commit_that_took_effect_and_then_raised_is_told_as_unknown_and_its_retry_goes_to_login`.
  `FailureTest.test_a_commit_that_raised_without_effect…` now also asserts the retry's `Location`.
  `ValidationOrderTest` records `was_redeemed`; a missing row's call sequence is now
  `pending_view, was_redeemed`.
- New `RepeatedRedemptionTest` (7): a double click using a **copy of the original browser's cookie** (the
  copy redeems; the original's repeat is `303 /login?next=…`, no `recovery=done`, slot dropped, an unchanged
  digest of the recovery tables, the ledger, the accounts and `login_throttle`, no new message, no
  `generate_password_hash`, `redeem`, `dispatch` or `request` call, no session; the login page shows no
  success notice and keeps `next`); a repeat carrying a wrong code, a weak password or a mismatched
  confirmation is still the redirect and judges none of them; the extra read happens only after
  `pending_view` found nothing (a usable row and a correct redemption never reach it); a browser with no
  slot or a well-formed slot of no row gets the ended page; **expired, cancelled, invalidated and missing
  rows keep the ended page** with the slot dropped and nothing written; a malformed, extended, other-version,
  truncated or `next`-tampered slot never acquires the redirect; and `was_redeemed` itself (true only for
  `redeemed`, false for `pending`/`void`/`expired` and for seven malformed bindings, `total_changes`
  unchanged).
- **How long the redirect lasts.** The repeat redirect is available **while the redeemed row exists**. Cleanup
  deletes final rows older than 24 hours since creation (design 6.1; `recovery_scrub.cleanup`), but elapsed
  time alone removes nothing: a redeemed row that cleanup has not yet run over still gives the redirect, past
  24 hours as well. After the row has actually been deleted the same request is the ended page. The redirect is
  a courtesy to a double click or a lost response, not a record of the reset. Retention policy and cleanup
  code are unchanged.
- Commit semantics: a commit that **took effect and then raised** gives the uncertain text (500, slot kept,
  password really changed), and the retry is `303 /login?next=…` with the digest, the message count and the
  hash and `redeem` spies unchanged; a commit that **failed without effect** gives the uncertain text with
  the database unchanged, and the retry completes the reset (`303 /login?recovery=done`, the new password
  works).
- `test_recovery_no_caller`: the narrow allow-list of service functions the blueprint may name gained
  `was_redeemed` and nothing else.

**Regression evidence.** The new and corrected tests were run (51 tests: `RepeatedRedemptionTest`,
`RedeemTest`, all of `test_recovery_transactions`) against a scratch copy with the previous
`web/blueprints/recovery.py` and `web/services/recovery.py` from `dd39615`. Unmodified, 28 error out
because the recorder patches the missing `was_redeemed`; that is noise. With a stub
`was_redeemed(...) -> False` appended to the old service (so only behaviour differs), **10 fail (4
failures, 6 errors)**: the double-click test, the three "judges nothing" subtests, the corrected `RedeemTest`
repeat test, the corrected RF2 (ii) test, the read-order test, the browser-without-slot read count, the
`was_redeemed` unit test and the missing-row call sequence. The negative tests (other ended states,
malformed slots) pass on the old code, as intended: they pin that the redirect did not widen. A mutant
that makes `was_redeemed` true for every final state fails three tests (the cancelled and invalidated
subtests and the unit test). One mutant, one machine; the author's earlier 26-mutant campaign was not
repeated.

**Documents.** `docs/AUTH_ROUTE_CONTRACT.md`: the repeat-redemption paragraph, which said the ended page was
correct, is replaced by the behaviour above, and a sentence records the unresolved gate-off discrepancy and
the review status. `PASSWORD_RECOVERY_SCOPE.md`: the "uncommitted" provenance, and the service row. This
report: the status block, provenance, deviation 1 (now a corrected defect), the RV5 and RF2 mapping,
this section and the next. **At `2c2a579` the design and the test plan were not edited**; design 2.4 step 2 was
aligned afterwards (below).

**Verification of the cleanup (the author's, run on the working tree before it was committed as `2c2a579`)** (Python 3.12.15, SQLite 3.53.4, Flask 3.1.3, Werkzeug 3.1.9, segno 1.6.6,
joserfc 1.7.7 from `requirements-dev.txt` in a fresh scratch venv, `pip check` clean; temporary databases,
`InMemoryMailer`, no socket; `python -B`, `PYTHONDONTWRITEBYTECODE`, a pycache prefix outside the worktree):

- All 17 `test_recovery_*` modules, no audit, run in the worktree: **373 tests, all OK** (concurrency 9,
  eligibility 18, enumeration 27, ledger 28, limits 8, migration_sql 4, no_caller 16, operations 9,
  polling 12, production 31, redeem 28, restore_credentials 18, routes 79, scrub 4, service 40,
  service_operations 10, transactions 32).
- Those 17 plus 11 adjacent modules (`test_access_policy`, `test_auth_blueprint`,
  `test_session_cookie_samesite`, `test_account_login`, `test_account_principal_bridge`, `test_dzi_blueprint`,
  `test_production_deployment`, `test_registration_production`, `test_google_oidc_production`,
  `test_registration_routes`, `test_auth_guard`) with the opt-in audit installed in a scratch copy of the
  working tree: **834 tests OK**, 82.2 s. Audit: 71 process reports, 4667 connections, 2395 empty-storage
  checks, 2168 fixture-storage, 103 absent-storage and 2 unreadable observations, **0 violations**
  (counters, not distinct databases). The adjacent modules were chosen because they enumerate routes,
  blueprints, gate constants and the login page; this change adds none of those, so this is a
  consistency check, not a claim about every module. A first attempt failed one `test_recovery_no_caller`
  test only because the scratch copy lacked a `.git` for its `git show`; it passed in the worktree and in
  the rerun.
- The full suite and the mutation campaign were **not** repeated (no concrete failure warranted it).
- Compilation of the five changed Python files and `git diff --check`: clean. (A `py_compile` call run
  inside the worktree wrote four ignored `__pycache__` directories; they were deleted, and
  `git status --ignored` shows nothing but the modified files.)
- Limits: Flask test client, threads and temporary databases; no browser, Linux, Gunicorn, real Nginx,
  provider or SMTP evidence. The audit is test instrumentation, not proof that production cannot reach the
  code.

## Follow-up review of `2c2a579` (APPROVE WITH NON-BLOCKING NOTES)

Independent follow-up review of exactly `2c2a579daef2a8aa1ee36e7e0c82018502dc5307`. Verdict: **APPROVE WITH
NON-BLOCKING NOTES. The original repeat-redemption blocker (F1: RV5 and RF2 (ii)) is resolved.** The verdict
does not approve deployment or activation, the RD15 wording, or any activation prerequisite. As for the first
review, the report is the reviewer's final message of its session and is not stored in the repository; this is
a summary, not a quotation. The reviewer, in turn, could read only this report's summary of the first review,
not the original, and so could not check that summary against it.

**Provenance it established.** The worktree head and `origin/feature/password-recovery-flow` were both
`2c2a579`; its single parent is `dd39615`; the worktree was clean, ignored files included. The diff touches the
8 files of the cleanup and nothing in registration, Nginx, migrations, the design or the test plan. The review
ran in two scratch clones (head, and `dd39615`) with a fresh venv from `requirements-dev.txt` (Python 3.12.15,
SQLite 3.53.4, Flask 3.1.3, Werkzeug 3.1.9, joserfc 1.7.7), temporary databases and the in-memory mailer, and
made no edits, commits, pushes or posts and started no service.

**What it confirmed**, by its own probes:

- Only the browser's own binding can trigger the redirect: it runs after `_slot()` accepted the slot and the
  binding is derived from the slot's token; nothing is looked up by code or address. A tampered cookie,
  another browser's pending row on the same account, a rotated secret key and seven malformed slot shapes
  did not get it.
- The repeat is `303` to `/login` keeping the validated `next` (encoded as on success), never
  `recovery=done`; unsafe targets given at `/recover` were already dropped there.
- It precedes every other judgement (wrong code, weak password, mismatch, empty form, list-valued fields all
  redirect) and has no side effects: a statement trace of the repeat shows exactly two `SELECT … FROM
  password_recoveries WHERE binding_hash = …` (on the unique index), no write, no `BEGIN`, no `users` or
  `login_throttle` statement; the digest, the mail count and the hash/set-password/redeem/request/resend/
  dispatch/settle spies were unchanged and no session was created.
- Expired, lazily expired, cancelled, invalidated and missing rows, and a redeemed row forced to `void` or
  `expired`, keep the ended page (200, slot dropped, nothing written).
- The existing order is intact (cross-origin 403, gate, signed-in redirect, then the new branch); a hold or a
  clock behind the mark still gives the redirect, as the service's own order does; the race branch and the
  transaction and dispatch code are untouched.
- Scenarios: a double submission using a copy of the raw cookie bytes; a commit that took effect then raised
  (500 with the uncertain text, slot kept, then a retry with the same code, a wrong code, a weak password or a
  mismatch each gives `303 /login?next=…` without side effects); a commit that failed without effect (database
  unchanged, the retry completes); a rollback that raised; deletion of the row by `recovery_scrub.cleanup`
  (the repeat then gets the ended page, keeping `next`); and three simulated races between the two reads, none
  able to produce a false success notice.
- No account-state disclosure beyond the design: the redirect is visible only to the holder of the binding, only
  a correct mailed code makes a row `redeemed`, a shadow row can never be redeemed, `was_redeemed` never reads
  `users`, and no production module but the blueprint calls it. The allow-list is narrow: two mutants (the
  blueprint calling `recovery_service._row`; `web/blueprints/account.py` naming `was_redeemed`) each failed
  tests and were reverted.

**The reviewer's evidence, kept apart from the author's:**

| Run (reviewer's) | Result |
| --- | --- |
| The 3 changed test modules | 127 OK (79 + 32 + 16) |
| All 17 `test_recovery_*` modules, no audit | **373 OK**, the author's count |
| The author's 28-module selection (overlapping, so not independent of the author's choice) plus the reviewer's own 34-test probe, audit installed | **868 OK** (834 + 34); 72 process reports, 4880 connections, 2395 empty-storage checks, 2381 fixture-storage, 103 absent-storage, 2 unreadable, **0 violations** |
| Head's tests against the `dd39615` code with only a `was_redeemed → False` stub (56 tests in `RepeatedRedemptionTest`, `RedeemTest`, `SlotShapeTest`, `test_recovery_transactions`) | 11 fail (7 errors, each a `Location` lookup on a 200 page, and 4 failures: read counts, call order, the unit test); the negative tests pass, as they should. The author's run had 10: the extra one is the cookie-copy test in `SlotShapeTest`. |
| The reviewer's probe on the unmodified `dd39615` | 11 behavioural failures, each "200 instead of 303"; all 34 pass at head |
| Compilation of the 5 changed Python files; `git diff --check dd39615 2c2a579` | clean |

The empty-storage, absent-storage and unreadable counts equal the author's; the extra connections and
fixture-storage come from the probe. The audit hook had to be installed through a `.pth` (Homebrew's stdlib
`sitecustomize` shadowed a first attempt), and an environment-gated hook missed seven subprocesses that run
with a cleared environment, so the counted run used an unconditional hook. The host was heavily loaded during
the run; the test results were not affected. **The reviewer did not repeat the full suite or the mutation
campaign.** The author's earlier 4724-test full run and 26 killed mutants predate the correction, and no
run of either exists for `2c2a579`. Limits: one macOS machine, the Flask test client, threads and temporary
databases; no browser, Linux, Gunicorn, real Nginx, provider or SMTP evidence.

**The reviewer's non-blocking notes and what this report does with them:**

1. *The cleanup's own status lines were stale at `2c2a579` ("uncommitted and unstaged", the "task report"
   reference in the inventory; also in `PASSWORD_RECOVERY_SCOPE.md`).* Corrected by the later documentation
   edit; the inventory below is now self-contained.
2. *Design 2.4 step 2 still contradicted its outcome table.* The reviewer thought a design-owner-approved
   amendment would be appropriate and approved no wording. Step 2 was aligned afterwards, in a documentation
   edit outside the reviewed SHA, to resolve the contradiction in favour of the existing requirement (outcome
   table, failure paragraph, RV5, RF2 (ii), caller obligation 3). It is described there as an alignment, not a
   new decision, and this report claims no new product approval.
3. *No route test covers a row whose state is `expired`* (the author's "expired" route case is a lazily
   expired pending row; only the unit test covers the `expired` state). The reviewer's own route-level probe of
   it passes. **No test was added**; recorded as a coverage note.
4. *The redirect lasts until cleanup actually deletes the row, not until 24 hours by the clock* (a redeemed row
   left uncleaned after 48 hours still redirects). The retention wording in this report and in the route
   contract is corrected to say so; the retention policy and the cleanup code are unchanged.
5. *Observation, outside RV5 and unchanged by the diff:* a resend on a redeemed binding answers the 200 ended
   page, and a `GET /recover/verify` with that slot answers 200.

## Open discrepancies and decisions (still unresolved, except item 4's wording)

1. **Gate-off method behaviour (review F2) — unresolved.** With the gate off, `OPTIONS` answers **200**
   (with `Allow`) and a wrong method on a route answers **405** (`PUT`, `PATCH`, `DELETE`, and `GET`/`HEAD`
   on `/recover/resend` and `/recover/cancel`), while an unknown path answers 404; design 2.6 and the contract
   say every `/recover…` address is the generic 404. Deviation 3 above documents only the 405s. Registration
   behaves the same. **No decision by Gabriel is recorded and none is claimed.** Neither blueprint's method
   policy was changed. The choice the reviewer named: accept it as the registration precedent and document it,
   or change both blueprints in a separate PR.
2. **Lazy expiry (review F4, deviation 9).** A POST for a row that is still `pending` but past its time shows
   the ended page and drops the slot but does not mark the row expired (a GET and `pending_view` write nothing,
   by design); the row's typed address and code hash remain until the next cleanup expires and scrubs it
   (about 45 minutes after the request; design 6.1, logical retention). Design 2.4 step 2 and 2.5 read
   slightly differently on whether a POST should close it. Not changed; no product approval is claimed.
3. **Additional proposed wording (review F5).** The verify page's „Не получи код? Провери папката за нежелана
   поща. Можеш да поискаш нов код след минута.“ is a text chosen by the route and copied from the registration
   page; it was missing from deviation 5's list and its wording is open under RD15 like the others. The verify
   form carries `autocomplete="off"` with field-level `one-time-code` / `new-password` hints (as registration
   does); whether browsers and password managers still offer a one-time code and a new password needs a
   browser check that was not done.
4. **Design 2.4 step 2's wording** ("None, final, or past its time … the same page") contradicted the design's own
   outcome table; it is the sentence deviation 1 misread. It has been **aligned** in a documentation edit after
   `2c2a579` (outside the reviewed SHA), to match the existing requirement; no new product approval is claimed
   and the follow-up reviewer approved no wording. The sentence in design 6.1 about the deletion of final rows
   is unchanged.

## Prerequisite before registration or recovery activation: registration `HEAD` with a body

Found by the review of `dd39615`; **pre-existing, in registration, not in this PR, and not changed by this
cleanup** (neither registration nor Nginx was touched). Registration is gated off in production, so it
cannot happen there today. It must be fixed (a separate, narrow PR) **before registration or recovery is
activated**.

- **Cause.** The registration handlers branch on `request.method == "GET"` (`web/blueprints/registration.py`
  lines 349, 422 and 439), so a `HEAD` is treated as a submission when it carries a form body. `HEAD` is not
  in `UNSAFE_METHODS` (`web/request_integrity.py:18`: POST, PUT, PATCH, DELETE), so it skips the same-origin
  check; and the Nginx rate-limit map counts only `POST` (`deploy/nginx/learnpilot.conf`, the
  `$learnpilot_post_client` map at the top), so a `HEAD` is outside the `lp_*` limits.
- **Reproduction (reviewer's, temporary database, in-memory sender, Flask test client).** A cross-origin
  `HEAD /register/student` with a form body gave `303`, one pending row, one ledger row, one message, one
  scrypt hash and a `Set-Cookie`; the same body as a `POST` gave `403`. `HEAD /register/verify` counted a
  code attempt, and with the correct code activated an account.
- **Not verified.** Whether a real Nginx and Gunicorn forward a `HEAD` request body to the application at
  all. The author's earlier scratch probe (deviation 2 above) reproduced one pending row and one message
  the same way.
- **Impact.** Not CSRF (a browser cannot send this cross-site). It lets a script bypass the POST-only Nginx
  limits on registration and code checks: unthrottled scrypt work and mail, up to the application's own
  caps. The hourly mail budget and the circuit are shared with recovery (RD5/RD6), so once both are active
  this could deny recovery.
- **Recovery is not affected.** Its handlers branch on `POST`; a `HEAD` with a body changed nothing on
  `/recover` or `/recover/verify` (the reviewer's probe, and `MethodAndIntegrityTest`).
- **Suggested shape of the separate PR** (the reviewer's, not done): `request.method != "POST"` in the three
  registration handlers, as recovery already does, plus tests; optionally `HEAD` in the Nginx map.

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
- The review of `dd39615` requested changes; the cleanup `2c2a579` that answers its blocking finding was approved with
  non-blocking notes by an independent follow-up (both above). Neither review approves deployment, activation, the RD15
  wording or an activation prerequisite. Gabriel's decisions on points 2 to 4 above and on the open discrepancies above,
  the registration `HEAD` prerequisite and every item of "activation evidence" in this section remain outstanding. This
  report approves nothing.

## Changed-file inventory

Self-contained record, by commit; the counts are `git show --stat` / `git diff --stat` figures.

1. **`dd39615`** (the first reviewed SHA): 37 files, 5230 insertions(+), 133 deletions(-): the 25
   tracked-modified and 12 new files of the original inventory below (the 747/133 figure there counts the
   tracked files only). Not touched: migrations, canonical JSONL, `deploy/learnpilot-restore.sh`, `src/`,
   `AGENTS.md`, `data/`, `vault/`.
2. **`2c2a579`** (the second reviewed SHA, on `dd39615`): 8 modified files, no new file, 494 insertions(+),
   59 deletions(-): `web/services/recovery.py`, `web/blueprints/recovery.py`,
   `tests/test_recovery_no_caller.py`, `tests/test_recovery_routes.py`, `tests/test_recovery_transactions.py`,
   `docs/AUTH_ROUTE_CONTRACT.md`, `docs/reviews/PASSWORD_RECOVERY_SCOPE.md`,
   `docs/reviews/PASSWORD_RECOVERY_8_3_STATUS.md`. Not touched: registration, Nginx, either blueprint's method
   policy, the design, the test plan, migrations, `src/`, `deploy/`, `data/`, `vault/`.
3. **After `2c2a579`** (documentation only, outside both reviews; `git diff 2c2a579` shows exactly what
   changed): `docs/reviews/PASSWORD_RECOVERY_8_3_STATUS.md` (provenance and status, the follow-up review, the
   retention wording, this inventory), `docs/reviews/PASSWORD_RECOVERY_SCOPE.md` (provenance),
   `docs/reviews/PASSWORD_RECOVERY_DESIGN.md` (design 2.4 step 2 and the status line of its header),
   `docs/AUTH_ROUTE_CONTRACT.md` (review status and the retention sentence). No executable, test,
   configuration, migration or gate change.

### Original inventory (at `dd39615`)

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
