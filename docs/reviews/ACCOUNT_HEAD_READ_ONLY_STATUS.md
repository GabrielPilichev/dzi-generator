# Account forms: HEAD is a read (#348) — implementation and evidence

This is an implementation report by its implementer (Claude Opus 5.5); it is **not an approval**.
Independent review of this change is pending. Deployment, activation and every provider or host
operation remain unauthorized. **Every production gate remains OFF.**

## Provenance

- Worktree `~/dzi-generator-account-head`, branch `fix/account-head-read-only`, draft PR #348.
- Starting HEAD `fd20ffd4cc3715332130bedf37756101202f78cf` (empty preparation commit) on
  `27c291e4aab5fc060ed198d8ca2e47eac5a107fb` (#347), with a clean status. The change is left
  unstaged and uncommitted for review; nothing was staged, committed, pushed or merged.
- #347 (registration) was approved by an independent review at `a22ce214eeb580bde20a1832f456eec1128d8143`
  and squash-merged as `27c291e` with the same tree (`git diff a22ce214 27c291e` is empty). That
  approval covers the registration handlers only, **not** this change
  (`docs/reviews/EMAIL_REGISTRATION_PR4_STATUS.md`, "Correction: HEAD is a read").

## Defect

Flask routes `HEAD` to a view that accepts `GET`. Four GET/POST form views took their read path only
for `request.method == "GET"`, so a `HEAD` carrying a form body ran the `POST` action:

| View | What a HEAD reached |
| --- | --- |
| `account.login` (`/login`) | throttle check, password verification, failure recording or clearing, `last_login_at`, the account session, or an admin's pending second factor; a signed-in session could be replaced |
| `account.profile` (`/account/profile`) | form validation, the profile and `display_name` writes, completion of the profile requirement |
| `account.password` (`/account/password`) | current-password verification, failure recording, hashing, the password change, `session_key` rotation and session re-issue |
| `oidc.google_link_confirm` (`/auth/google/link/confirm`) | throttle check, password verification, failure recording, clearing, the consumption record, identity insertion and the drop of the pending link |

`HEAD` is not in `UNSAFE_METHODS`, so the same-origin check never met it, and the Nginx rate-limit
keys count `POST` only (as recorded for #347; not re-checked on a host). The previous reviewer reproduced a cross-origin `HEAD /login`
writing `login_throttle` and signing in with valid credentials; the other three were found by
source inspection and are now each reproduced by the tests below. `/account/profile` was not in
that review's list and is included.

## Correction

`web/blueprints/account.py` (login, profile, password) and `web/blueprints/oidc.py`
(`google_link_confirm`): the read branch is `request.method != "POST"`, as in registration and
recovery. The methods other than `POST` that reach these views are `GET` and `HEAD`. Everything
before the branch is unchanged: the same-origin hook, the access hook, the profile gate and its
`is_route_allowed` check, the passwordless notice of `/account/password`, the provider 404, the
link route's POST-only admin 403 and POST-only drop of an unusable slot, and the pending-link read.
Everything after it is unchanged: validation, return targets, browser binding, throttle, transactions.

One display-only comparison changed with it: the `saved` flag of `_profile_page`
(`request.method == "GET"` → `!= "POST"`). A `HEAD /account/profile?saved=1` now takes the GET's
read path, and with the old comparison its page (and `Content-Length`) would differ from the GET's.
A POST that re-renders the form still never shows the notice.

Deliberately not changed:

- `GET /auth/google/callback`: `oauth_state.verify_oauth_state` keeps answering `HEAD` and `OPTIONS`
  with 405 before the view (its own semantics: a `HEAD` must never redeem a code).
- The `request.method == "POST"` branches of `quiz.py` and `teacher.py` already treat `HEAD` as a
  read; the link route's two `== "POST"` checks (admin 403, slot drop) are POST-only by design.
- `UNSAFE_METHODS`, Nginx rate maps, the `OPTIONS`/405 policy, schemas, gates.

## Tests

`tests/test_account_head.py`, 23 tests. Every database is temporary (migrations plus canonical;
one per test), Google is the in-process fake, no network. Each `HEAD` carries a real form body and
asserts: status as the GET of the same request; `Location`, `Content-Type`, `Content-Length`,
`Cache-Control`, `Referrer-Policy` and `Set-Cookie` equal to the GET's; an empty body; a digest of
every table unchanged; the session unchanged; no `Set-Cookie`; and pass-through spies uncalled.

- **Login**: valid and invalid credentials for student, teacher and admin, same-origin, cross-origin
  `Origin` and cross-origin `Referer`; existing throttle state (3 failures, and blocked at 10) neither
  counted nor cleared, while the POST is still 429; a signed-in account sent on and never replaced;
  an admin's pending second factor neither reissued (the clock moved) nor replaced, a fresh
  browser's HEAD starts none, and the pending sign-in still completes. Spies: `authenticate`,
  `check_password_hash`, `generate_password_hash`, `record_login`, `is_blocked`, `record_failure`,
  `clear`, `issue_account_session`, `issue_pending_second_factor`.
- **Password**: correct and incorrect current passwords for each role, same- and cross-origin;
  hash, `session_key` and `password_changed_at` unchanged; this browser's `sk` and another device's
  session still valid; existing and blocked throttle untouched (the POST is still 429); a passwordless
  account; anonymous redirect. Spies add `verify_password`, `set_password`, `password_problem`.
- **Profile** (gate on): valid bodies for a student without a profile (marked and unmarked), with a
  student profile, a teacher with a teacher profile, and a teacher and an admin without one (a POST
  is 403); `display_name`, `profile_required_at`, `user_profiles` and `teacher_subjects` unchanged and
  `needs_profile` unchanged; invalid bodies, an empty one and one naming `role`/`account_type`; the `saved` notice; anonymous and disabled
  accounts (login redirect); gate off (404 for same- and cross-origin HEAD, cross-origin POST still
  403). Spies: `validate_profile`, `complete_student_profile`, `save_profile`, `create_profile`.
- **Google-link confirmation**: a live pending link for a student and a teacher with the correct, an
  incorrect and an over-long password, same- and cross-origin, with an existing throttle failure:
  page shown, link slot and cookie unchanged, failure count kept, no identity or consumption row;
  the legitimate POST then links and clears the counter. A blocked counter. A missing binding (stays
  absent); a malformed, other-instance and expired binding (each left exactly as it was; only the
  POST drops it). A teacher promoted to admin while pending, the provider disabled (404), anonymous.
  Spies: `verify_password`, `check_password_hash`, `is_blocked`, `record_failure`, `clear`,
  `confirm_link`, `begin_write`, `drop_pending_link`.
- **POST journeys** are asserted unchanged in every family: cross-origin `Origin` and `Referer` POSTs
  are 403 and write nothing, and same-origin POSTs sign in, change the password, save the profile and
  link.
- **Session cleanup is not a form action.** A cookie with an identity that no longer resolves is
  dropped by `load_current_user` on any request; the test compares the HEAD with the GET (both drop
  it, neither writes or issues anything) instead of claiming the cookie unchanged. Likewise a disabled
  account's identity is dropped by the loader, not by the profile view.

`tests/test_registration_head.py` (#347) cleanup: the unused `client` parameter of `assertInert`
removed; the comment that put a HEAD "after the same-origin check" corrected; the slot of another
shape is now asserted unchanged (absent stays absent; held stays the same).

## Evidence

Environment: macOS (Darwin 25.5.0, arm64), CPython 3.12.15, SQLite 3.53.4, Flask 3.1.3,
Werkzeug 3.1.9, in a fresh scratch virtual environment built with `uv` from `requirements-dev.txt`
(outside the checkout). Runner as in `docs/TESTING.md`: `python -B -m unittest`.

| Run | Result |
| --- | --- |
| `tests.test_account_head` | 23 tests, OK |
| `tests.test_account_head tests.test_registration_head` | 39 tests, OK |
| Original handlers (export of `fd20ffd` plus the final test files) | 39 tests: **FAILED (failures=37, errors=2)**; 13 of the 23 new tests fail, the 16 #347 tests pass |
| Adjacent selection (below) | 902 tests, OK, 132.6 s |
| `tests.test_auth_guard tests.test_quiz_attempt_render` (AGENTS.md) | 125 tests, OK |

Adjacent selection: `test_account_head test_registration_head test_account_login test_account_profile
test_account_ui_state test_account_auth_cutover test_admin_totp_login test_auth_guard test_auth_blueprint
test_access_policy test_google_oidc_routes test_google_oidc_replay test_google_oidc_production
test_profile_gate test_profile_attempt_gate test_profile_google_onboarding test_registration_routes
test_teacher_registration test_recovery_routes test_session_cookie_samesite test_error_handlers`.
The full suite was not run: the change is four branch conditions and one display flag, and this
selection covers every module that drives these routes.

**Mutation probes.** Each corrected condition was reverted alone to `== "GET"` in its own scratch
export (`git archive HEAD` plus the working-tree files), never in the working tree; `tests.test_account_head`:

| Reverted | Result |
| --- | --- |
| `login` read branch | failures=16, errors=2 (5 tests) |
| `profile` read branch | failures=11 (4 tests) |
| `_profile_page` `saved` flag | failures=1 (`test_the_saved_notice_is_read_as_by_the_get`) |
| `password` read branch | failures=7 (2 tests) |
| `google_link_confirm` read branch | failures=3 (2 tests) |

Also checked: `python -m py_compile` of the changed modules, `git diff --check` clean.
`data/questions.db` does not exist in this worktree; no runtime database was read or written.

## Findings outside this change

- **OPTIONS on `signed_in` routes** is answered by the access hook before Flask's automatic answer:
  anonymous `OPTIONS /account/password` and `/auth/google/link/confirm` are 302 to the login, while
  `OPTIONS /login` and `/account/profile` (public at the hook) are 200 with `Allow`. Pre-existing,
  runs no view and writes nothing; the `OPTIONS` policy is out of scope and unchanged.
- **`_submitted_next()` of `/login`** also reads `next` from a form body on a `GET` or `HEAD`. It is
  validated by `safe_redirect_target` and only echoed into the form or used as a redirect for a
  signed-in account; no change made.

## Remaining prerequisites

Independent review of this change; evidence on the host (Nginx forwarding of `HEAD`, deployed
response headers, rate-limit behaviour) on a deployed build; no Linux run was made. Nothing here
approves deployment or activation; every gate stays off.
