# Email registration, PR 4 — student flow integration: status and contract

Design: `docs/reviews/EMAIL_REGISTRATION_DESIGN.md` (sections 1, 2.3, 3.10, 4.3,
6.2 to 6.6, 7.3 to 7.5, 9).
Tests: `docs/reviews/EMAIL_REGISTRATION_TEST_PLAN.md`, sections F, G, H, I, K and
the PR 4 halves of C17, DL10, DL12, N4 and N5.
Earlier PRs: `EMAIL_REGISTRATION_PR1_STATUS.md`, `EMAIL_REGISTRATION_PR2_STATUS.md`,
`EMAIL_REGISTRATION_PR3_STATUS.md` (all merged: #335, #336, #337).

**Status:** implemented in draft PR #338 on `feature/student-registration-flow`
(base `232c832`). An independent review (Claude Cloud) returned **APPROVE WITH
NON-BLOCKING NOTES**; its notes are addressed in "Review follow-up" below. The PR
is **not approved by Gabriel**. Registration is **not
activated**: both build gates are off, so a production start keeps every
`/register...` route at the generic 404 whatever is configured. No migration,
no deployment, no provider, no real database and no real message were involved.

## Decision recorded with this PR (approved by Gabriel)

**Cancellation during a hold.** A registration hold does not block the
cancellation of pending data. `POST /register/cancel` voids and scrubs this
browser's pending row while a `restore` hold is set; the hold and the
high-water mark are preserved, so a cancellation never reopens registration. A
clock behind the mark still refuses a cancellation (`UNAVAILABLE`, nothing
voided) under the existing service contract. Recorded in design 0.7 and in
`EMAIL_REGISTRATION_PR3_STATUS.md` (its deviation 7). Tests:
`AvailabilityTest.test_a_hold_refuses_submit_resend_and_verify_but_not_cancel`,
`test_a_regressed_clock_refuses_cancel_too`.

## What PR 4 contains

| Part | File |
| --- | --- |
| Routes, browser slot, transactions, dispatch and settlement sequencing | `web/blueprints/registration.py` (new) |
| Pages (Bulgarian) | `web/templates/register_choose.html`, `register_student.html`, `register_verify.html`, `register_message.html` (new); `login.html` (link and outcome notice) |
| Whether registration is enabled, and with which mailer | `web/registration_settings.py` (new) |
| Development wiring; blueprint registration | `web/app.py` |
| Production gate, startup errors, warning, logger | `web/production.py` |
| Build gates; read-only verify-page view; operator helpers | `web/services/registration.py` (additions) |
| The public `next` rule (one definition) | `web/services/registration_forms.py` (`accepted_next`, an alias) |
| Declared profile types for `list`, read through the profile service | `web/services/profiles.py` (`declared_types`, read only) |
| Operator commands, `list` columns | `src/manage_accounts.py` |
| Nginx zone and locations | `deploy/nginx/learnpilot.conf` |
| Tests | `tests/test_registration_routes.py`, `tests/test_registration_production.py`, `tests/test_registration_cli.py` (new) |
| Sweep lists of existing tests (new routes, blueprint, zone, gate constant, `list` columns) | `tests/test_access_policy.py`, `tests/test_session_cookie_samesite.py`, `tests/test_account_login.py`, `tests/test_account_principal_bridge.py`, `tests/test_auth_blueprint.py`, `tests/test_dzi_blueprint.py`, `tests/test_google_oidc_production.py`, `tests/test_manage_accounts.py`, `tests/test_production_deployment.py` |
| Documents | this file; design 0.7; scope status; PR 3 status (the decision above); `docs/AUTH_ROUTE_CONTRACT.md`; `docs/ACCOUNTS_DESIGN.md` (status, §10 amendment); `docs/DEPLOYMENT.md` 33; `docs/TESTING.md` |

Not changed: every migration, `data/canonical`, `web/services/mail.py`,
`mail_sender.py`, `accounts.py`, `web/account_session.py`,
`web/access.py`, the account and Google blueprints, `web/wsgi.py`,
`deploy/systemd`, `deploy/gunicorn.conf.py`, the env templates, the vault.

## Routes and policies

| Route | Methods | Access | Integrity | Behaviour |
| --- | --- | --- | --- | --- |
| `/register` | GET | `public` | — | Chooser („Ученик съм“). Reads `next`. |
| `/register/student` | GET, POST | `public` | `same_origin` | Form; submission (303 to `/register/verify`). |
| `/register/teacher` | GET, POST | `public` | `same_origin` | **Always the generic 404** until PR 7 (D5), whatever `TEACHER_ACTIVATION` says. |
| `/register/verify` | GET, POST | `public` | `same_origin` | Code page; verification. |
| `/register/resend` | POST | `public` | `same_origin` | New code (303 to `/register/verify?resent=1`). |
| `/register/cancel` | POST | `public` | `same_origin` | „Грешен имейл? Започни отново“ (303 to the form). |

In this order, on every route: same-origin check (the existing hook) → access
policy (public) → **disabled: the generic 404** → **signed in: 303 to the role's
home, nothing started** → the route. Every response of the blueprint carries
`Cache-Control: no-store` and `Referrer-Policy: no-referrer`. No GET writes a
row or sets a cookie; the verify page reads only the caller's own row through
`registration.pending_view` (expiry evaluated, not written).

Status codes: a form with errors 200; the address cap 429 (form re-rendered,
or the verify page after a resend); unavailable (hold, clock, circuit, total
budget, unreaped sender, a teacher row) 503; the generic failure 500; "no
usable registration" 200.

## The browser slot

`session["reg"] = {"v": 1, "t": <token>, "next": <accepted path or "">}`, set
only after the issuance's `COMMIT` returned. A slot is used only if it has
exactly those keys, `v` is the integer 1, `t` is 43 URL-safe characters and
`next` is empty or passes the registration `next` rule; anything else is
ignored (treated as no slot). The row is found by
`binding_hash(SECRET_KEY, t)` and by nothing else. No address, name, code,
password or issuance identity is in the cookie. The slot is dropped when the
row left `pending` (activated, address taken, expired, voided by the teacher
gate, cancelled, or found final) and kept after a failure or an "unavailable"
answer. `issue_account_session`, `issue_pending_second_factor` and
`end_session` are unchanged and drop it with the rest of the session.

## Transactions, dispatch and session, exactly

**Submit** (`POST /register/student`, valid form):

1. `availability(conn, now, sender_busy=mail.sender_busy(), total_cap)` and
   `address_capped(...)` with no transaction open. Refused → 503 or 429, **no
   hashing**, nothing written.
2. `accounts.hash_new_password(<exact password>, email)`.
3. `BEGIN IMMEDIATE` → `create_pending(..., clock=_utcnow, sender_busy=mail.sender_busy(), ...)`
   (the service takes the lock and reads the clock under it) → `COMMIT`. Any
   exception, the commit's included → `ROLLBACK`, log `submit_failed
   error=<class>`, 500. Nothing was sent, no slot was set.
4. `UNAVAILABLE` / `ADDRESS_CAPPED` (decided again under the lock) → 503 / 429.
5. `ISSUED` and committed → log `submitted pending= send=`, set the slot.
6. **Dispatch once:** `registration.dispatch(conn, mailer, issued, ...)` with no
   transaction open. `NotSendable` → log `dispatch_refused`, **no settlement**.
   Any other exception from `dispatch` (raised before the mailer is reached;
   the mailer's own exceptions are `unknown` inside `dispatch`) → log
   `dispatch_failed`, no settlement.
7. **Settle in its own transaction:** `BEGIN IMMEDIATE` →
   `settle_send(conn, send_id, issuance_id, outcome, clock)` → `COMMIT`. A
   failure → rollback, log `settlement_failed`, the row stays `started` (safe).
   `send_settled send= outcome=` only after this commit returned.
8. 303 to `/register/verify`. The page then shows "not sent" (with the resend
   button) exactly when the committed row's code was retired by a `not_sent`
   settlement; otherwise the ordinary page.

**Resend:** one `BEGIN IMMEDIATE` around `resend` → `COMMIT` → if `ISSUED`, the
same steps 5 to 7 (log `resent`) → 303. Refusals render the verify page with
their text (`TOO_EARLY`, `RESEND_EXHAUSTED`, 429 for the cap) or 503.

**Verify:** one `BEGIN IMMEDIATE` around `verify` → `COMMIT`. Exception or failed
commit → rollback, log `activation_failed`, 500, **slot kept**, no "confirmed"
or "address taken" text, no session; the same code works on retry. After the
commit: `ACTIVATED` → log `activated pending= user=`, drop the slot, 303 to
`/login?next=<slot next>&registration=activated`, **no session issued**;
`EMAIL_TAKEN` → drop, 303 to `/login?...&registration=email_taken`;
`WRONG_CODE`/`CODE_EXPIRED` → „Грешен или изтекъл код.“; `TOO_MANY`;
`NOT_PENDING` with `final_state == "activated"` (a repeat of a verification that
succeeded) → drop, 303 to `/login`; `NOT_PENDING` of an expired, cancelled or
replaced row, `EXPIRED` and `NO_PENDING` → drop, "no usable registration" with
the validated `next`; `UNAVAILABLE` → 503
(slot dropped only for the teacher gate, which voided the row).

**Cancel:** one `BEGIN IMMEDIATE` around `cancel` → `COMMIT` → drop the slot,
303 to the form. `UNAVAILABLE` (clock behind) → 503, slot kept. A hold does not
stop it (decision above).

**One dispatch per issuance.** `dispatch` is called in exactly two places
(submit and resend), each time with the `Result` that this request's own
committed writer returned, once, and never in a loop or a retry. No code path
reads a `started` generation back from the database to send or settle it. A
repeated or concurrent POST is a new call of the writer: either a new
issuance, with a new generation and identity and its own single dispatch, or
a refusal (`TOO_EARLY` under the lock) that sends nothing. Exactly-once SMTP
is not claimed: a worker that dies after the commit and before or during the
send abandons that issuance (its ledger row stays `started`; the person can
resend after the wait).

## Gates, settings and startup

- `web/services/registration.py`: `PRODUCTION_ACTIVATION = False`,
  `TEACHER_ACTIVATION = False`. The module reads no environment.
- Enabled means `app.config[registration_settings.CONFIG_KEY]` holds a
  `RegistrationSettings(mailer, total_cap)`; otherwise every route is 404.
- **Production** (`web.production`): while the gate is off,
  `registration_errors` is empty whatever the settings hold (never a reason to
  refuse to start), `registration_production_settings` returns `None` and logs
  one warning naming the configured settings, never their values. With the gate
  on (only patched in tests): a configured backend must pass
  `mail.settings_problems(..., production=True)`, so `console`, `file` and every
  malformed value are startup errors naming the setting; no backend means
  disabled. `configure_registration_logging()` gives `learnpilot.registration`
  a stderr handler like the lifecycle and OIDC loggers.
- **Development** (`web.app`): usable settings with `production=False` enable
  it (`DZI_MAIL_BACKEND=console` shows the code in the terminal); unusable ones
  give one `RuntimeWarning` naming the settings and leave it disabled. It never
  refuses to start. During a production start `web.app` sets nothing.
- No sender address, host or credential exists in the code or the templates.

## Logging

Logger `learnpilot.registration`. Durable events, written by the blueprint only
after the relevant `COMMIT` returned: `submitted pending= send=`, `resent
pending= send=`, `send_settled send= outcome=`, `wrong_code pending=`,
`activated pending= user=`, `email_taken pending=`, `cancelled pending=`,
`gate_off pending=`. Failures: `submit_failed`, `resend_failed`,
`activation_failed`, `cancel_failed`, `settlement_failed`, `dispatch_failed`
(each `error=<exception class>`), `dispatch_refused pending= send=`,
`rollback_failed`. The service's own lines keep their `*_decided` names (written
before the commit, not evidence of durability) and its refusal warnings
(`restore_hold`, `clock_regressed`, `circuit_open`, `total_cap_reached`). Never
logged: address, names, password, code, binding token, issuance identity,
message, `next`.

## Operator commands (`src/manage_accounts.py`)

| Command | Behaviour |
| --- | --- |
| `registrations-cleanup` | `registration.cleanup` in one `BEGIN IMMEDIATE`; no secret needed; refuses while the restore lock exists (`lifecycle.restore_lock_present`); clock behind the mark → exit 1, nothing changed. A held write lock → exit 1 (`database is locked`), nothing scrubbed. |
| `registrations-cleanup --check` | Reads only (`registration.stale_pending_count`); exit 1 when a pending row is older than an hour. Also refused during a restore. |
| `registrations-clock-accept` | `registration.accept_clock`: sets the mark to the present under the write lock; no control row → nothing written; holds untouched. |
| `clear-registration-limit --email` | Needs `DZI_SECRET_KEY`; prints the consequence (8 more codes the same day) **before** deleting; `registration.clear_address_limit` deletes only that address's ledger rows; no pending row is touched. |
| `list` | Adds `verified` (`users.email_verified_at`) and `type` (`profiles.declared_types`, the profile service), `-` when absent or NULL, and reads neither when the schema predates migration 021. |

All three new commands check for the storage of migrations 021 and 022 and
otherwise fail with a named error, no traceback.

## Account readers (`email_verified_at`)

`accounts._USER_COLUMNS` and `REQUIRED_USER_COLUMNS` are **not** widened. No
account reader in this PR needs the column: sign-in must not consult it (D7;
legacy NULL accounts and every CLI password account sign in exactly as before,
`JourneyTest.test_admin_second_factor_and_cli_accounts_are_unchanged`), the
session loader and the account page do not show it, and `manage_accounts list`
reads it with its own query, guarded by a schema check, so a migration-012
database still lists (`SchemaTest`). The migration-012 fixtures of the
existing suites are untouched. NULL is shown as `-` and is not reinterpreted.
The obligation of design 6.1 stays with the first PR that reads the column
through `_USER_COLUMNS`.

## Deviations and interpretations, for the reviewer

Only the cancellation decision above is approved. The rest are readings.

1. **The profile gate is not added here.** D14 lists three gates "built by
   PR 4"; section 9 puts "the profile gate" with PR 6 together with the route it
   gates. Nothing in this PR has a profile route or check to gate, so only the
   registration and teacher gates exist. **Approved by Gabriel:** the profile-gate
   constant is deferred to PR 6, alongside profile enforcement.
2. **`/register/teacher` is an unconditional 404**, not "404 while the teacher
   gate is off": the teacher form is PR 7, so a test that turns the constant on
   still gets 404 (`test_teacher_route_is_404_even_with_its_gate_patched_on`).
   `TEACHER_ACTIVATION` is passed to `resend` and `verify` as `teacher_enabled`.
3. **Activation redirects to `/login` instead of rendering a confirmation
   page** with a link (design 6.2 says "the confirmation page links to
   `/login?next=`"; the brief asks for a redirect). The confirmation text is
   shown on the sign-in page for `registration=activated`, looked up from a
   fixed table while registration is enabled, like the Google outcome codes. As
   with those, anyone can link to `/login?registration=activated`; it shows a
   fixed text and changes nothing. **Approved by Gabriel:** the fixed
   URL-selected notice stays. It changes no account state and reflects no user
   input. A hand-built URL can show the notice without any flow; this is an
   accepted display limitation, not evidence of activation.
4. **A repeat verification of a registration that was activated**
   (`NOT_PENDING`, `Result.final_state == "activated"`) redirects to `/login` with
   no notice (design 6.2). A `NOT_PENDING` of any other final state (`expired`,
   `void`) is not a success: it gets the "expired or started elsewhere" page, so
   unrelated traffic that expired the row (inline cleanup, the cleanup service) or
   a cancellation or replacement in another tab never looks like a silent
   sign-in redirect. `final_state` is read from the row of the caller's own
   binding and is `None` for every other outcome. A POST without a slot
   (another browser, F8) gets the "started in another browser" page. The two
   are told apart by whether the browser holds a slot.
5. **Status codes** are this PR's choice (above); the design fixes only 303
   for a submission and 500 for the generic failure.
6. **`registrations-cleanup` refuses while the restore lock exists.** PR 3's
   status assigned that refusal to PR 5; design 6.4 and 6.6 make it part of the
   command, so the command is not shipped without it. The restore gate, status
   and hold-release commands, the timer and the restore script are PR 5 and
   are not here.
7. **`registrations-clock-accept`** asks for no confirmation flag (design 6.6
   names none); it prints the old and new mark.
8. **`clear-registration-limit`** prints its warning and acts in one run (design
   6.6: "says before it acts"); there is no confirmation flag. A live code whose
   ledger row it deleted fails closed (`CODE_EXPIRED`); a resend replaces it.
9. **Service additions** (no lifecycle logic in the blueprint):
   `pending_view` (read-only page state), `stale_pending_count`, `accept_clock`,
   `clear_address_limit`, and the two gate constants. PR 3's functions are
   unchanged. `profiles.declared_types` is a read for `list`, so that the profile
   tables stay the profile service's (`test_profiles_service` pins that).
10. **Extra log events** beyond 3.10's list: the failure events above and
    `cancelled`, `dispatch_refused`, `rollback_failed`.
11. **The Nginx `lp_register` zone also covers `/register/teacher`** (404 today)
    so that PR 7 needs no Nginx change; `/register/cancel` is unlimited like
    other ordinary POSTs. Values are the design's proposal. Every address under
    `/register` has its own `location` with `add_header Referrer-Policy
    "no-referrer" always` and `proxy_hide_header Referrer-Policy`, so exactly one
    value is sent instead of the server-level `strict-origin-when-cross-origin`
    plus the application's copy (see "Review follow-up", 2).

## Tests

`python3 -B -m unittest tests.test_registration_routes tests.test_registration_production tests.test_registration_cli`.

| Plan | Where |
| --- | --- |
| F1, F2 | `test_access_policy` (matrix), `test_session_cookie_samesite` (both sweeps, registration disabled); `DisabledAndGetTest.test_cross_origin_posts_are_refused_and_write_nothing` (enabled) |
| F3, F4, F14, F16, K6 | `DisabledAndGetTest` |
| F5, F5a, F6, F7, J1–J3 (sign-in, TOTP, CLI accounts unchanged), B8 at route level | `JourneyTest` |
| F8–F12, F15 | `BindingTest` |
| F13, B10, B11, I2 | `FormTest`, `RoleTest` |
| F17, DA5 route level, commit failures (issuance, resend, activation, cancel, settlement), NotSendable, single dispatch, stale identity after a rolled-back issuance, concurrency (resend copies, verify copies) | `TransactionTest`, `DeliveryTest` |
| H1–H7 | `DeliveryTest` |
| K7, C17 (registration half), DL10 (no scrypt on a refusal), hold/clock/cancel decision, teacher gate at verify (K5 route half) | `AvailabilityTest` |
| G1–G4, G7, G8, E7 route level | `EnumerationTest` |
| I1, I4 | `RoleTest`, `FormTest` |
| Logging (3.10) | `LoggingTest`, and the log assertions of `TransactionTest` |
| K1–K4 | `test_registration_production` |
| DL12, N4 (CLI half), N5, J4 (`list`) | `test_registration_cli` |

G5 is covered by `EnumerationTest.test_the_application_steps_of_a_submission_and_a_resend_do_not_depend_on_the_account`
(added in the review follow-up, below). Not done here, and why: I3, I5–I7, J6–J15
belong to PR 6 and PR 7; DR*, N6, N7 and the timer are PR 5; section L is
activation evidence. The route tests were checked against five deliberate
mutations of the blueprint (a second dispatch, the slot set before the commit,
a settlement after `NotSendable`, a success claimed after a failed activation,
a cancellation that ignores "unavailable"): each one fails the suite.

## Review follow-up (non-blocking notes of the independent review)

**Verdict recorded:** the independent review (Claude Cloud) of PR #338 at
`1b17ee4` was **APPROVE WITH NON-BLOCKING NOTES**: no gating, transaction,
dispatch, cancellation or logging blocker. Those reviewed contracts are
unchanged. This is a review verdict, not Gabriel's approval. The notes and what
was done:

1. **An ended registration was answered like a finished one.** `verify`
   redirected every `NOT_PENDING` silently to `/login`, including an expired,
   cancelled or replaced registration that has no account. `Result` now carries
   `final_state` (`activated`, `void` or `expired`) for `NOT_PENDING` only; the
   route redirects silently only for `activated`, and otherwise drops the slot
   and shows the „Регистрацията е изтекла…“ page with the validated `next`. No
   session is issued, the transaction stays the caller's, and the state read is
   that of the browser's own binding row. Tests: `CodeTest` (expired by inline
   cleanup, expired by the cleanup service, cancelled in another tab, replaced in
   another tab, repeat after activation, a binding that is not this browser's) and
   `test_registration_service` (`final_state` for every writer).
2. **Referrer-Policy at Nginx.** The server block sends
   `strict-origin-when-cross-origin`, and the registration locations inherited it
   beside the application's `no-referrer`. Each registration address now has an
   exact location (`/register`, `/register/student`, `/register/teacher`,
   `/register/resend`, `/register/verify`) or the `/register/` prefix location
   (`/register/cancel` and anything else under it), following the Google
   callback: the other two security headers are repeated, `no-referrer` is added
   and the upstream copy is hidden. Rate limits, `proxy_pass` and the
   inherited `proxy_*` directives are unchanged; `/register` and
   `/register/cancel` still have no `limit_req`. `NginxTemplateTest` resolves each
   address the way Nginx selects a location (exact, longest prefix, regular
   expression) and checks the effective headers, limits and that every other path
   keeps the server-level policy. **Not run:** `nginx` is not installed on the
   machine that made this change, so `nginx -t` on this template (and on an
   isolated copy of it) was not run; a real Nginx must still parse it, and the
   response headers must be seen once on a deployed server.
3. **G5 latency coverage.** `EnumerationTest.test_the_application_steps_of_a_submission_and_a_resend_do_not_depend_on_the_account`
   records the application's own steps (availability, caps, hashing, writer,
   dispatch, clock reads, settlement, the mailer call and any `time.sleep`) for a
   new and for an existing address, with a fake backend told to take 7 s for a
   notice and 0.5 s for a code (stated, never slept). It asserts the same
   sequence, one send, one dispatch, nothing but the settlement after the send,
   and no application sleep; it asserts no elapsed time. **A mocked test cannot
   prove equal timing.** The provider's real latency, the application's measured
   response times and the capacity of a classroom behind one NAT stay
   activation evidence (test plan section L).
4. **Verify-page accessibility.** After a wrong, expired or too-many code, the
   code field has `aria-invalid="true"` and `aria-describedby="code-error"`,
   pointing at the error message; wait, cap and "not sent" messages do not mark the
   field.
5. **Test counts.** Before these fixes the review counted **58 route tests**
   (`tests.test_registration_routes`) and **82 tests for the PR** (58 route, 13
   production, 11 operator-command). After the fixes: 66 route, 16 production, 11
   operator-command = **93**, plus one new test in `test_registration_service` (91
   in that module). Other counts are not repeated here.

**Decisions approved by Gabriel (behaviour unchanged):** the profile-gate
constant is deferred to PR 6 (deviation 1); the fixed, URL-selected activation
notice on `/login` is kept (deviation 3). Neither is open any longer. Production
activation stays OFF and every deployment prerequisite below still applies.

**Known limitation kept:** the resend notice „Изпратихме нов код.“ (`RESENT_MESSAGE`)
is unchanged. It is a statement about what the application did, and the
application never learns whether a message was delivered. Better wording is
pending a decision and is not part of this change.

**Still open for activation** (unchanged by this pass): PR 5 (cleanup timer,
restore gate, snapshot scrub); a provider, sender address and credentials; the
`nginx -t` and header check on the server; a classroom-sized run to tune the
`lp_register` values and measure real provider latency; and the other
prerequisites of design 0.3. No delivery was attempted and none is claimed.

**Testing through a tunnel.** In development, usable mail settings enable
registration (`web.app`), so a public tunnel can expose working `/register`
routes. Use a fake or development backend (`DZI_MAIL_BACKEND=console`, or
`file` into a scratch directory), never real SMTP, when the app is reachable
through Cloudflare or any other tunnel.

## Not verified

Everything ran on macOS (Darwin 25.5.0, arm64), CPython 3.12.15, SQLite 3.53.4,
in a scratch virtual environment built from `requirements-dev.txt`. No Linux
run, no server, no Nginx (`nginx -t` was not run), no provider, no real
database, no message sent. Activation prerequisites (design 0.3) are unchanged
and none is done; PR 5 (timer, restore gate, snapshot scrub) must precede any
activation.
