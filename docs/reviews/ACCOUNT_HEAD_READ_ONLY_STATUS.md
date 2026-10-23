# Account forms: HEAD is a read (#348) — implementation and evidence

This is an implementation report by its implementer (Claude Opus 5.5); it is **not an approval**.
It was written for #348, which was then independently reviewed and merged (see Provenance). The
corrections of the follow-up (#349, last section) are **not** covered by that review; independent
review of them is pending. Deployment, activation and every provider or host operation remain
unauthorized. **Every production gate remains OFF.**

## Provenance

- Worktree `~/dzi-generator-account-head`, branch `fix/account-head-read-only`, draft PR #348.
- Starting HEAD `fd20ffd4cc3715332130bedf37756101202f78cf` (empty preparation commit) on
  `27c291e4aab5fc060ed198d8ca2e47eac5a107fb` (#347), with a clean status. When this report was
  first written the change was unstaged and uncommitted for review; the implementer staged,
  committed, pushed and merged nothing.
- #347 (registration) was approved by an independent review at `a22ce214eeb580bde20a1832f456eec1128d8143`
  and squash-merged as `27c291e` with the same tree (`git diff a22ce214 27c291e` is empty). That
  approval covers the registration handlers only, **not** this change
  (`docs/reviews/EMAIL_REGISTRATION_PR4_STATUS.md`, "Correction: HEAD is a read").
- #348 was afterwards committed as `bb6da8ef20bd883ea495dbd89f11c005702728fb` (parent `fd20ffd`),
  independently reviewed and approved at that commit, and squash-merged as
  `55a4a64ba5881e5527bf97518dc387fa9f6dd040` on `27c291e` with the same tree (both `6803db47…`;
  `git diff bb6da8e 55a4a64` is empty). That approval covers #348 as merged, **not** the
  follow-up edits below.

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
one per test), Google is the in-process fake, no network. Each `HEAD` carries a form body (an empty
one where that is the case under test). Two helpers carry the comparisons:

- `assertInert`: every pass-through spy of the family uncalled; a digest of every table and the
  session equal to before the request (for the link confirmation also the raw session cookie); no
  `Set-Cookie`.
- `assertAsGet`: sends the GET of the same request and requires an empty HEAD body, the GET's
  status, and the GET's `Location`, `Content-Type`, `Content-Length`, `Cache-Control`,
  `Referrer-Policy` and `Set-Cookie`.

Most HEADs are checked with both helpers and an explicit status (or redirect target). The
exceptions assert a subset:

- `assertInert` without the GET comparison: the fresh browser's HEAD in the login second-factor case
  (session also asserted empty; no status asserted); the password throttle case and the link
  blocked-counter case (status 200); the HEAD before the POST in the profile-requirement case (no
  status asserted); the anonymous HEAD of the link role/provider case (redirect target).
- `assertAsGet` without an explicit status: the profile `saved` notice (the status is compared with
  the GET's).
- Neither helper: the login stale-identity case and the profile disabled-account HEAD (below).

Per family:

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
  absent). A malformed binding, a well-formed binding of another instance and an expired binding:
  each HEAD redirects as the GET does and is inert, the slot is exactly as it was after the HEAD
  and after the GET, and only the POST that follows drops it. The other-instance slot is the live
  one with only `inst` changed to a different 32-character lowercase hexadecimal value, asserted
  well-formed (`is_pending_link`), unexpired, different from `oauth_state.INSTANCE` and otherwise
  equal to the live slot, so the instance is the only reason it is refused. Each case writes its own
  slot and sets its own clock; the clock is reset to the test's start after each. A teacher promoted
  to admin while pending, the provider disabled (404), anonymous.
  Spies: `verify_password`, `check_password_hash`, `is_blocked`, `record_failure`, `clear`,
  `confirm_link`, `begin_write`, `drop_pending_link`.
- **POST journeys** are asserted unchanged in every family: cross-origin `Origin` and `Referer` POSTs
  are 403 and write nothing, and same-origin POSTs sign in, change the password, save the profile and
  link.
- **Session cleanup is not a form action.** A cookie with an identity that no longer resolves is
  dropped by `load_current_user` on any request. The login stale-identity case therefore sends the
  HEAD and the GET with the same stale cookie and requires the same of each: status 200, spies
  uncalled, every table unchanged and the session left empty (no account session issued). It does
  not compare their headers and does not claim the cookie unchanged. Likewise a disabled account's
  identity is dropped by the loader, not by the profile view: that HEAD asserts the login redirect,
  every table unchanged and the session left empty, without spies or a GET comparison.

`tests/test_registration_head.py` (#347) cleanup: the unused `client` parameter of `assertInert`
removed; the comment that put a HEAD "after the same-origin check" corrected; the slot of another
shape is now asserted unchanged (absent stays absent; held stays the same).

## Evidence for #348 (historical)

Recorded for #348 before it was committed, against its working tree as then reviewed. These runs
predate the follow-up below and were not repeated for it.

Environment: macOS (Darwin 25.5.0, arm64), CPython 3.12.15, SQLite 3.53.4, Flask 3.1.3,
Werkzeug 3.1.9, in a fresh scratch virtual environment built with `uv` from `requirements-dev.txt`
(outside the checkout). Runner as in `docs/TESTING.md`: `python -B -m unittest`.

| Run | Result |
| --- | --- |
| `tests.test_account_head` | 23 tests, OK |
| `tests.test_account_head tests.test_registration_head` | 39 tests, OK |
| Original handlers (export of `fd20ffd` plus #348's test files) | 39 tests: **FAILED (failures=37, errors=2)**; 13 of the 23 new tests fail, the 16 #347 tests pass |
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

## Follow-up (#349): review-note corrections

Two non-blocking notes from the #348 review, corrected in worktree `~/dzi-generator-account-head-notes`,
branch `test/account-head-review-notes`, draft PR #349, starting HEAD
`37ce0cc34d3871ae6cfa1641d0c969a7d6b212c6` (empty preparation commit) on `55a4a64`, with a clean
status. Only `tests/test_account_head.py` and this record changed; no runtime code. The edits are
left unstaged and uncommitted for review.

1. **Foreign-instance fixture.** The "other instance" case set `inst` to `"another-instance"`, which
   is not 32 hexadecimal characters: it tested a malformed slot, and the test still passed with the
   instance comparison removed. It now uses the well-formed foreign slot described under Tests; the
   test is renamed `test_a_malformed_foreign_or_expired_binding_is_left_exactly_as_it_was`. The
   malformed and expired cases are kept, each case now ends with the POST that drops its slot, and
   the clock no longer carries over from one case to the next.
2. **Evidence wording.** The Tests section said every HEAD performs every listed comparison; it now
   describes what the helpers and the individual cases assert. This record also notes the review
   and merge of #348 and marks its evidence as historical.

Verification of the follow-up. Environment: macOS 26.5.2 (Darwin 25.5.0, arm64), CPython 3.12.15,
SQLite 3.53.4, Flask 3.1.3, Werkzeug 3.1.9, Authlib 1.9.1, segno 1.6.6, in a fresh scratch virtual
environment (`python3.12 -m venv`, `pip install -r requirements-dev.txt`) outside the checkout,
with `PYTHONDONTWRITEBYTECODE=1`.

| Run | Result |
| --- | --- |
| `python -B -m unittest -v tests.test_account_head tests.test_registration_head` | 39 tests, OK |
| Instance mutation, corrected test (below) | 23 tests: **FAILED (failures=1)** |
| Instance mutation, #348's test (below) | 23 tests, OK: the gap the note described |
| Unmutated export of `37ce0cc` plus the corrected test | 23 tests, OK |
| `python -B -m unittest tests.test_auth_guard tests.test_quiz_attempt_render` (AGENTS.md) | 125 tests, OK |
| `python -m py_compile tests/test_account_head.py` (cache outside the checkout) | OK |

**Instance mutation.** In scratch exports of `37ce0cc` (`git archive`), never in the working tree,
`oauth_state.live_pending_link` was changed from
`if not is_pending_link(pending) or pending["inst"] != INSTANCE:` to
`if not is_pending_link(pending):`, the only place a pending link's instance is compared before the
confirmation page. With the corrected test, `tests.test_account_head` fails once, in subtest
`binding='other instance'`, at the HEAD's redirect assertion: `(200, None) != (302,
'/account?google=expired')`. The well-formed foreign link was accepted and its confirmation page
served. The malformed and expired subtests still pass. With #348's test the same mutant passes all
23 tests.

AGENTS.md database checks: no `data/questions.db` exists in this worktree and none was created.
A temporary database was built in the scratch directory (`python -m src.build_database --output
<scratch>/questions.db`); `pragma_foreign_key_check` on it returned no rows.
`src/audit_dzi_state.py --db <scratch>/questions.db --source-slug may_2025_v2` exited 1:
`may_2025_v2` is `PART1_IMPORTED` with 28 tasks, 100 points, 15 + 10 + 3 tasks, 25 Part 1 question
links and 0 foreign-key rows, and its one missing asset file is the official PDF
`data/reference/dzi/official_pdfs/may_2025_v2_exam.pdf`, which is not in the checkout. No asset was
imported. The full suite and the adjacent selection were not rerun: only a test fixture and this
record changed.

## Remaining prerequisites

Independent review of the follow-up corrections; evidence on the host (Nginx forwarding of `HEAD`,
deployed response headers, rate-limit behaviour) on a deployed build; no Linux run was made.
Nothing here approves deployment or activation; every gate stays off.
