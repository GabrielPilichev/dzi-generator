# Email registration, PR 6 — account profile flow: status and contract

Design: `docs/reviews/EMAIL_REGISTRATION_DESIGN.md` (sections 1.4, 6.1, 6.3, 7.3, 7.4 and 9, PR 6;
decisions D2 and D11).
Tests: `docs/reviews/EMAIL_REGISTRATION_TEST_PLAN.md`, A2, I5, I6, J5 to J13, J15, K3.
Earlier PRs: `EMAIL_REGISTRATION_PR1_STATUS.md` to `PR5_STATUS.md` (merged: #335 to #339).
Routes: `docs/AUTH_ROUTE_CONTRACT.md`, "Account profile". Operator text: `docs/DEPLOYMENT.md`, section 35.

**Status:** implemented on `feature/account-profile-flow` (draft PR), on top of `4197818`
(`419781831e925b5dcf4a7ca35b92ed688864fcef`, which holds no change). The working tree is **unstaged and
uncommitted**, **not independently reviewed and not approved**. The profile flow is **not activated**: the
build gate `profiles.PRODUCTION_ACTIVATION` is `False`, so a production start keeps `/account/profile`, its link,
the redirect after Google onboarding, the mark and the new-attempt check switched off whatever is configured.
Registration (`registration.PRODUCTION_ACTIVATION`, `TEACHER_ACTIVATION`) and Google sign-in
(`google_oidc.PRODUCTION_ACTIVATION`) are untouched and stay off. Approving this implementation is not
approving an activation: privacy (D16), the provider, capacity and Linux/systemd evidence are all outstanding, and
PR 7 (quota values) and PR 8 (recovery) are not started. No real database, deployment, provider, real message,
server or tunnel was involved.

## What PR 6 contains

| Part | File |
| --- | --- |
| Migration: one nullable column `users.profile_required_at`, no backfill | `web/migrations/023_profile_required.sql` (new) |
| Manifest level and migration list (no canonical row changes) | `data/canonical/manifest.json` |
| The gate constant, `enabled_in`, `needs_profile`, `complete_student_profile`, `save_profile` | `web/services/profiles.py` |
| `set_display_name`, `mark_profile_required`, `profile_requirement` | `web/services/accounts.py` |
| The profile form validator (`validate_profile`) | `web/services/registration_forms.py` |
| `/account/profile`, the profile summary on `/account` | `web/blueprints/account.py`, `web/templates/account_profile.html` (new), `web/templates/account.html`, `web/static/css/ui-pass9.css` |
| The mark at Google onboarding, the redirect to the profile | `web/services/oidc_accounts.py`, `web/blueprints/oidc.py` |
| The profile check at the locked "would create" decision | `web/services/quiz.py` (`quiz_start_or_resume_attempt`, `QuizAttemptStart`), `web/blueprints/quiz.py` |
| Gate wiring: development setting, production constant | `web/app.py`, `web/production.py` |
| Documents | `AUTH_ROUTE_CONTRACT.md`, `ACCOUNTS_DESIGN.md`, `DEPLOYMENT.md` (section 35), `TESTING.md`, the design, scope and test plan, and `EMAIL_REGISTRATION_PR5_STATUS.md` (a correction, below) |

**Migration number: 023** (the next free one: `000` to `022` exist, and a test pins that the files are numbered
without a gap). It is additive (`ALTER TABLE users ADD COLUMN profile_required_at TEXT`), not a table rebuild.
Existing accounts get NULL; nothing is backfilled or marked retroactively. `users` stays classified runtime.

## The gate

`profiles.PRODUCTION_ACTIVATION = False` is a build constant in `web/services/profiles.py`, assigned nowhere else.
The effective value of a running application is `app.config["PROFILES_ENABLED"]`
(`profiles.CONFIG_KEY`), read with `profiles.enabled_in(config)` (true only for the value `True`):

* **Production** (`web.production.create_production_app`) sets it from the constant alone
  (`profiles_production_enabled`). No environment variable and no setting is read to decide it. If
  `DZI_PROFILES_ENABLED` is present while the constant is off, one warning names it (never its value) and the start
  goes on; it is never a startup error.
* **Development** (`web.app`, not under a production start) sets it from `DZI_PROFILES_ENABLED`
  (`1`, `true`, `yes`, `on`); unset or anything else is off.

While it is off (the build's default): `/account/profile` is the generic 404 for a signed-in account of every role,
and the 404 is the same for a POST after the same-origin check (a cross-origin POST is 403); `/account` shows no profile
section and no link; Google onboarding redirects as before and writes no mark; attempt creation is as before and never
reads the mark; a mark that exists stays stored and is ignored. All of it is tested, in-process and in fresh
interpreters (`tests/test_profile_gate.py`).

## Behaviour

**`/account/profile`** (`account.profile`, `signed_in`, `same_origin`).

* Student without a profile: first name, last name and class 8 to 12. No email and no password field is rendered,
  read, echoed or stored; no field named `role`, `account_type`, `is_admin`, `email_verified_at` or
  `profile_required_at` is read. Completion creates the profile and sets `display_name` to
  `"<first_name> <last_name>"` in one caller-owned transaction.
* Account with a profile: the form of the **stored** `account_type` (student: names and class; teacher: names, school and
  one to three subjects), whatever the role is now. A student-type profile cannot store a school or subjects, and a teacher-type
  one cannot store a class, with the role equal to the type and different from it.
* Teacher or admin without a profile: a page saying there is none, no form; a POST answers 403 and creates nothing. No teacher
  onboarding exists.
* Editing sets `display_name` again only when the profile's names changed, so an operator's rename is not overwritten by an edit
  of the class alone.
* A failed write rolls back everything (the view runs `BEGIN IMMEDIATE`, commit, rollback after any exception, as the
  registration view does) and answers the generic failure (500). The role, email, `email_verified_at`, active state and
  `session_key` are never written, so every session stays valid; `quiz_attempts.user_id`, the `quiz_attempt_owner:*` markers
  and every stored `student_name` are untouched. Later attempts copy the new `display_name`.
* After a save: a `next` that the form rules kept and `safe_redirect_target` accepts again at use is followed (a GET); a
  submitted `next` that was refused becomes the role's home and is never echoed; with no `next` the saved profile is shown
  (`?saved=1`). Completing never starts an attempt.

**`profiles.needs_profile(conn, user, *, enabled)`** is true only when `enabled` is exactly `True`, the account's current role
is `student`, `profile_required_at` is a non-empty text, and no `user_profiles` row of any type exists. It reads the role and
the mark from the row (using only `user["id"]`), so it decides on the state under the caller's lock. It writes nothing. A role
change never writes, deletes or reinterprets a profile or the mark (`set-role` is tested at CLI level).

**The mark.** `accounts.mark_profile_required` refuses (`LookupError`, nothing written) anything that is not an unmarked student.
`oidc_accounts.sign_in_or_onboard(..., require_profile=...)` calls it only for an account it just created, in the same
transaction as the account and the identity; the callback then redirects to `/account/profile?next=<target>`, where the target is
`safe_redirect_target(transaction next, role home)`. A failure of any of the three writes (a failed mark, a failed commit) rolls all
of them back and ends as before (`/login?google=failed` or `unavailable`). Not marked: an existing Google account signing in again,
a student that existed, a CLI-created account, a teacher, an admin, an email collision. No command marks existing accounts (there
is no `require-profile`; a test pins that the mark has exactly two writers, the account service and the onboarding service).

**The new-attempt check.** Inside `quiz_start_or_resume_attempt`, under its existing writer lock, with the new keyword
`profile_gate` (default `False`, so every other caller is unchanged): an existing attempt resumes or shows its result, closed or
not; a closed assignment without one shows its closed notice; only then, where an attempt would be created (the assignment is open
at the instant the insert uses, asked with the same `OPEN_PREDICATE_SQL`), `needs_profile` is read, and when true the service
returns `QuizAttemptStart(profile_required=True)` and inserts nothing (the transaction is discarded, the clock's high-water mark
included). The route passes `profiles.enabled_in(current_app.config)`, performs no attempt lookup of its own on a POST, and answers
a profile-required outcome with `303` to `/account/profile?next=/quiz/s/<token>`. Attempt pages, saving, submitting, results,
"Моите опити", browsing and the account pages are never blocked.

## Deviations and interpretations, for the reviewer

1. **`accounts._USER_COLUMNS` is not widened** with `profile_required_at`, although design 6.1 says PR 6 widens it. Nothing would
   read the column from the loaded account: `needs_profile` reads the row at the point of decision, which under the writer lock is
   the correct state, and widening would make every request of a database below migration 023 fail (the reason design 6.1 gives
   for not widening it earlier). The CLI's `REQUIRED_USER_COLUMNS` is therefore unchanged too. A test pins `_USER_COLUMNS`.
2. **The gate is an `app.config` value, and a development-only setting `DZI_PROFILES_ENABLED` exists.** The design names only
   the constant. Registration and Google can be enabled in development by their own settings; without one the profile flow could not
   be tried locally. The setting is read by `web.app` only and has no effect in production (tested in a fresh interpreter). If you
   prefer no development setting, delete the `web/app.py` block and set the key in tests only.
3. **`needs_profile` takes the gate as a required keyword (`enabled`)** instead of reading it: the service has no Flask. The design's
   `needs_profile(conn, user)` signature is the shape; the gate argument is the difference.
4. **An anonymous visitor on `/account/profile` with the gate off is sent to `/login?next=/account/profile`**, not 404, because the
   route's policy is `signed_in` (the design's table) and the access hook runs before the view. Every signed-in account gets the 404.
   Making it 404 for everyone would need the policy to be `public` with a sign-in check in the view.
5. **A POST by a teacher or an admin without a profile answers 403 with the "no profile" page** (the design says only that nothing is
   created).
6. **A teacher-type form exists** (school and a subject checklist with „Друг предмет“), because the brief requires an account with a
   teacher-type profile to see and edit its own fields even when its role differs. The design leaves teacher-form UX to PR 7; no
   teacher onboarding or registration is added.
7. **No-`next` save redirects to `/account/profile?saved=1`** instead of the role's home, which the brief assigns to a refused
   target. A refused target does go to the role's home.
8. **A role changed between the form being read and the write** is decided under the lock: the save is refused with the generic
   failure (409) and nothing is written.
9. **`display_name` follows an edit only when the names changed** (above).
10. **CSS and a template were added** for the new page (`account_profile.html`, `ui-pass9.css`: a checklist and a definition list,
    colours from the shared tokens only). The page has not been looked at in a browser.
11. **Existing tests changed** only where PR 6 makes them untrue: the route-policy matrix, the URL-map snapshot
    (`tests/test_dzi_blueprint.py`), the account blueprint's route list and the two sweeps of
    `tests/test_session_cookie_samesite.py` gain `account.profile`; the baseline and build tests move to migration 023; the
    owner-migration test expects a second trailing NULL on `users`; the test that lists the modules that assign a
    `PRODUCTION_ACTIVATION` constant now lists `web/services/profiles.py`; the production start probe's word filter
    ("profile", written to catch the retired `/profile/<profile>` switch) now expects `/account/profile`; and the fence of
    `tests/test_profiles_service.py` that lists the files allowed to name the profile tables also lists the account
    blueprint and `web/app.py`. Two non-test adjustments came from existing static tests: `web/production.py` imports
    the services in a parenthesised block (a test forbids the text `import re` there) and the new CSS avoids
    `overflow-wrap: anywhere` (a test scopes it to machine text).
12. **Drive-by corrections of status text, from `git`:** the scope, `ACCOUNTS_DESIGN.md` and the test plan called PR 4 and PR 5 draft or
    unreviewed; they are merged (#338, #339). The PR 5 status text is corrected as the brief asks (next section).

## Correction of the PR 5 status document

`EMAIL_REGISTRATION_PR5_STATUS.md` described its fifth-review fixes as "unstaged and uncommitted ... awaiting independent review".
They are committed as `65bee1e156baab8aa0d5395b9df0bf9d5871312a` and merged on `main` as the squash commit
`3d2e0d56ef2b57d66d65d640fb289c9c9041e6ce` (#339), whose tree is identical to that of `65bee1e`. The reviewed SHA is `65bee1e`, per the
squash commit's message, which records an independent Codex follow-up there with the verdict approve with non-blocking notes. The
document now says so, with the provenance as `git` shows it. **No Linux, root, `flock`, `setpriv`, `sudo`, systemd, GNU `sync` or power-loss
validation is claimed or was done.**

## Tests

Commands (from the worktree root, with a scratch virtual environment built from `requirements-dev.txt`; `python -B`,
`PYTHONDONTWRITEBYTECODE=1`):

```sh
python -B -m unittest tests.test_profile_required_migration_sql tests.test_profile_requirement \
  tests.test_account_profile tests.test_profile_google_onboarding tests.test_profile_attempt_gate \
  tests.test_profile_gate tests.test_registration_cli tests.test_profiles_service
python -B -m unittest discover -s tests
```

Results, on macOS with a scratch virtual environment from `requirements-dev.txt` (real `segno`, `pyotp`, `cryptography`,
Authlib; no stubs): Python 3.12.15, SQLite 3.53.4, Flask 3.1.3, Werkzeug 3.1.9, segno 1.6.6.

* The eight modules above: **218 tests, OK** (the six new modules hold 157; `test_registration_cli` gains 3).
* The full suite, once, on the final state (docs edited afterwards only for this results paragraph): **4120 tests, OK**,
  about 14 minutes. An earlier full run of the same tree found six failures in existing static and snapshot tests that PR 6 makes
  untrue (listed in deviation 11); they were fixed and the whole suite was then run again.

What the new tests cover, against the brief: additive migration and upgrade/fresh parity, existing accounts NULL, no canonical change
(`test_profile_required_migration_sql`); every `needs_profile` combination and the role and profile mismatches, the atomic profile functions and
the validators (`test_profile_requirement`); the gate in production and development, in fresh interpreters (`test_profile_gate`); gate-off
behaviour, the page for each account, create and edit, unchanged role, session, ownership markers, attempt ownership and historical names, later
attempts copying the name, safe returns, same-origin refusal, escaping, atomicity (an injected failure after the profile insert, a failed `COMMIT`,
a failed rollback) and the independence of the three gates (`test_account_profile`); CREATED versus existing accounts, the same-transaction mark,
rollback (`test_profile_google_onboarding`); resume, result, closed-notice precedence, no row on a profile-required outcome, the writer lock (a
profile, an attempt, a closure, an unmark and a role change made while the start waits for the lock), concurrent starts and role changes
(`test_profile_attempt_gate`); the operator commands (`test_registration_cli`). A mutation check (removing the open-assignment test before
`needs_profile`) fails five of the attempt-gate tests.

## Not verified / remaining

* Not independently reviewed. Nothing was looked at in a browser: the markup renders through the test client, and the colours come from the
  shared tokens, but the contrast of the new warning, the checklist and the definition list has not been checked on a screen.
* Nothing ran on Linux or under the real unit; no real SQLite database, deployment, provider or message was involved.
* Activation prerequisites unchanged and not done: the privacy policy and D16 (purpose, visibility and retention of the profile fields, minors),
  the amendment of `ACCOUNTS_DESIGN.md` §15 (only a pointer is added here), the provider and sender, capacity, the Linux/systemd evidence of PR 5,
  PR 7 (quota values) and PR 8 (recovery).
* The page's text about what the data is used for is deliberately absent: none may be invented before D16.
* Not covered: a browser's behaviour with the `next` parameter beyond the server's rules; a profile edit racing a `set-password` on another device
  (neither rotates the other's key, and the writes are on different columns).
