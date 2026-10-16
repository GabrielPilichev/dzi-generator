# Email registration, PR 7 — teacher quotas and gated teacher registration: status and contract

Scope and interpretations: `docs/reviews/EMAIL_REGISTRATION_PR7_SCOPE.md`.
Design: `docs/reviews/EMAIL_REGISTRATION_DESIGN.md` (sections 6.3, 7.1, 7.3 and 9, PR 7; decisions D3, D4, D5, D14).
Tests: `docs/reviews/EMAIL_REGISTRATION_TEST_PLAN.md` (D3, D10, F16, I2, I3, I7, K5).
Earlier PRs: `EMAIL_REGISTRATION_PR1_STATUS.md` to `PR6_STATUS.md` (merged: #335 to #340).
Routes: `docs/AUTH_ROUTE_CONTRACT.md`, "Creation limits" and "Student registration". Operator text: `docs/DEPLOYMENT.md`, section 36.

## Status

**Implemented on `feature/teacher-registration-quotas`, started from `fd44c8f16e7d3fbe021b25d65ec53387ea1bfaff`, unstaged and
uncommitted. Not independently reviewed, not approved, not merged, not activated.** `registration.TEACHER_ACTIVATION`,
`registration.PRODUCTION_ACTIVATION`, `profiles.PRODUCTION_ACTIVATION` and `google_oidc.PRODUCTION_ACTIVATION` are all still `False`, and no
setting or environment variable overrides any of them in production. No real `data/questions.db`, deployment, provider, real message,
credential, server or tunnel was involved. Canonical JSONL content is unchanged. The quota limits, unlike the gates, apply as soon as this
build runs (DEPLOYMENT section 36). Approving this implementation will not be an activation.

## Approved decisions (input)

Per teacher: at most 20 assignment creations in a rolling 24 hours; at most 10 open assignments; closing frees an open slot and refunds no
creation; enforced atomically across creation paths; a verified teacher registration receives role `teacher` only when the teacher gate permits
activation; every production gate stays off. Nothing else here is an approval.

## What PR 7 contains

| Part | File |
| --- | --- |
| Migration 024: `assignment_creations` and its index, filled from owned assignments | `web/migrations/024_assignment_creations.sql` (new) |
| Manifest level and lists, table classification | `data/canonical/manifest.json`, `src/canonical_tables.py` |
| The limits: constants, `enforce`, `record`, messages, statuses | `web/services/assignment_quota.py` (new) |
| The decision inside the one insert; `transaction` argument; `begin_write` | `web/services/quiz.py` (`quiz_create_assignment`), `web/services/lifecycle.py` |
| The open count without writing the open predicate twice | `web/services/lifecycle.py` (`UNCLOSED_STATE_SQL`, `count_unclosed_owned`; `OPEN_PREDICATE_SQL` is unchanged text) |
| Refusals on `/teacher/new` (both kinds) and the copy form | `web/blueprints/teacher.py` |
| Teacher activation: role entry, school and subjects to the profile, ledger check | `web/services/registration.py`, `web/services/accounts.py` |
| Teacher route, form, chooser entry, shared submit | `web/blueprints/registration.py`, `web/templates/register_teacher.html` (new), `web/templates/register_choose.html` |
| Tests | `tests/test_assignment_quota.py`, `tests/test_assignment_creations_migration_sql.py`, `tests/test_teacher_registration.py` (new); pins updated in `tests/test_registration_routes.py`, `test_registration_service.py`, `test_account_profile.py`, `test_schema_baseline.py`, `test_build_database.py`, `test_canonical_content.py`, `test_database_test_support.py` |
| Documents | `EMAIL_REGISTRATION_PR7_SCOPE.md` (new), this file (new), the design, scope and test plan, `AUTH_ROUTE_CONTRACT.md`, `ACCOUNTS_DESIGN.md`, `DEPLOYMENT.md` (section 36), `TESTING.md`, and the two corrections to `EMAIL_REGISTRATION_PR6_STATUS.md` |

## Migration 024

**Number 024** (the next free one: `000` to `023` exist; a test pins the files are numbered without a gap). Additive: one `CREATE TABLE`, one
`CREATE INDEX` and one `INSERT ... SELECT`; `quiz_assignments` and `users` are not rebuilt or altered, no trigger is created, nothing is deleted.

```
assignment_creations (
    id INTEGER PRIMARY KEY,
    owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    assignment_id INTEGER NOT NULL UNIQUE CHECK (typeof = 'integer' AND > 0),   -- no foreign key, on purpose
    created_at TEXT NOT NULL CHECK ('YYYY-MM-DD HH:MM:SS', 19 characters and 19 bytes, GLOB)
)
INDEX idx_assignment_creations_owner ON (owner_user_id, created_at)
```

* **Compatibility.** Code from before this release names its columns and ignores the table; it creates assignments without recording them (a
  window in which creations are unrecorded). A rollback of the code needs no schema change and must keep the migration (DEPLOYMENT, "Rollback").
  A fresh build and an upgrade give the same definition (tested).
* **Backfill and existing data.** One row per assignment that has an owner, with the owner and the `created_at` it already has; a `created_at`
  not in the layout (the column was never constrained: ISO `T` forms, `2025-01-01`, NULL) becomes the epoch, which no window reaches. An
  assignment with no owner gets no row. Re-running the copy outside the runner fails whole on the UNIQUE `assignment_id` (tested).
* **Retention.** Kept as long as the account; accounts are disabled, never deleted (`ON DELETE RESTRICT`). It holds an account id, an assignment
  number and a time: no name, email or text. No retention period for it is decided, and none is invented. The assignment retention cleanup that
  `docs/ASSIGNMENT_LIFECYCLE_DESIGN.md` describes (not written) must never touch it; the table has no foreign key to `quiz_assignments` so a
  deletion cannot cascade into it, and a test pins that only the quota module writes it.
* **Why not `quiz_assignments.created_at`?** A creation must stay counted after the assignment is closed and after its row is deleted.
* **Classification.** Runtime, never exported to `data/canonical` (manifest and `src/canonical_tables.py`).

## The quotas

* **One decision.** `quiz_create_assignment` is the only `INSERT INTO quiz_assignments` in `web/` and `src/` (a static test). Its callers are
  the two helpers of `web/blueprints/teacher.py`: new test (plain and mixed) and copy. `/teacher/dzi-training`, the token and lifecycle tools and the importers create no
  assignment. Inside the lifecycle writer transaction (`BEGIN IMMEDIATE`, clock health, `now` sampled after the lock): the daily check, the open
  check, the insert, the deadline write, the ledger row. A caller that already holds a `LifecycleWrite` passes it as `transaction`; any other
  caller gets one from `lifecycle.begin_write`, which leaves it open, so the caller still owns the commit as before.
* **Refusal.** `QuotaExceeded(reason)` before anything is written; the transaction rolls back, so the clock mark is not raised either. `/teacher/new`
  and the copy form answer again with the typed values, a Bulgarian message and 429 (daily) or 409 (open). A diagnostic line
  `lifecycle event=create_refused actor_user_id=<id> reason=<reason>` is logged; nothing else.
* **Order.** Authorization (404/403/redirect) and input validation come first, then the deadline, then the limits.
* **Per-account, not per-person.** No claim of abuse prevention per person is made.
* **Not covered.** A creation by a principal without an account id (tests and the retired shared sign-in only; no request produces one) is unowned and
  unaccounted. Nothing counts or limits a student, a visitor or an unowned row.

### Interpretations that need Gabriel (from the scope)

I1 window `(now − 24 h, now]`; I2 held-but-open counts as open and a passed deadline frees a slot; I3 the daily limit is reported first; I4 account-based
counting, a copy counts for the copier; I5 the role is read under the lock and quotas are enforced unless it is `admin`; **I6 admin accounts are
recorded and not refused (unresolved)**; I7 the limits apply to every teacher account, operator-created or not; I8 the migration copies history and
nothing is closed; I9 unowned rows; I10 status codes and no `Retry-After`; **I11 no development switch for the teacher gate (question)**.

## Teacher registration

* **The gate** is `registration.TEACHER_ACTIVATION`, read per request as exactly `True`. Off: `/register/teacher` is the generic 404 after the
  same-origin check; the chooser has no entry; `create_pending` refuses a teacher row; `resend` and `verify` of a pending teacher row void and scrub it,
  answer the „временно не е достъпна“ page and drop the binding; cancel and cleanup still scrub it. A voided row stays voided when the gate is
  turned on again, and no teacher row becomes a student.
* **The form** (`register_teacher.html`): first name, last name, email, school with the town hint, the registry's subjects as checkboxes and „Друг предмет“
  with its name, the exact password and its confirmation, hidden `next`. The validators, the messages, the `next` rule, the echo (never a password),
  the escaping and the same-origin check are the existing ones. Errors are tied to their fields (`aria-invalid`, `aria-describedby`; the subject error to its fieldset).
  No privacy, consent or purpose text was added (D16).
* **Activation.** The shared `verify` creates, in the caller's one transaction, the `users` row (role from the account type's literal, `teacher`; never admin), the
  profile with the school, and the one to three `teacher_subjects` rows, then marks the row `activated` and scrubs it. Any exception, a single subject row included,
  rolls everything back (the row stays pending with its code and attempt count; the answer is the generic 500). A database without `assignment_creations` refuses to
  activate a teacher. Email collisions are the existing structural `ON CONFLICT(email) DO NOTHING`: an address with an account of any role answers `email_taken`,
  and that account's role, hash, key and profile are untouched.
* **Dispatch and settlement** are the existing path, shared: commit, then one dispatch per issuing handler result, no retry, settlement by send number and issuance
  identity, nothing settled after `NotSendable`.
* **Authorization.** A registered teacher gets exactly the teacher matrix: owned assignments only; 403 on `/teacher`, `/dzi...`; 404 on another's assignments.

## Tests

Environment: macOS, a scratch virtual environment built from `requirements-dev.txt` (real `segno`, `pyotp`, `cryptography`, Authlib; no stubs):
Python 3.12.15, SQLite 3.53.4, Flask 3.1.3, Werkzeug 3.1.9, segno 1.6.6, Authlib 1.9.1, cryptography 50.0.2, pyotp 2.10.0, python-docx 1.2.0.

Commands (worktree root, `python -B`, `PYTHONDONTWRITEBYTECODE=1`):

```sh
python -B -m unittest tests.test_assignment_creations_migration_sql tests.test_assignment_quota tests.test_teacher_registration
python -B -m unittest discover -s tests
```

* The three new modules: **140 tests, OK** (migration 18, quota 57, teacher registration 65).
* A focused run of the new modules plus 38 affected existing modules: 1163 tests; the one failure it found (a PR 6 test of the three gates that pinned
  `/register/teacher` as always 404) was updated and its module re-run (69 tests, OK).
* **The full suite, once: 4280 tests, 1 failure, about 14 minutes (843.8 s).** The failure was
  `test_profile_required_migration_sql.ProfileRequiredClassificationTest.test_a_fresh_canonical_build_has_the_column_and_no_account`, which asserted that
  `023_profile_required.sql` was the *last* applied migration. That pin was loosened to "023 is applied" (a later migration may follow). **The full suite was not
  repeated after that one-line test change**; the modules that read migration levels (`test_profile_required_migration_sql`,
  `test_registration_issuance_migration_sql`, `test_registration_migration_sql`, `test_assignment_creations_migration_sql`) were re-run: 88 tests, OK. The full run
  therefore was not green on the final state, and no claim of a green full suite is made. The suite was run as a background task, because the foreground limit
  of the tool is 10 minutes; it was waited for, not polled.
* Mutation checks (each applied, run and reverted by a script; not part of the suite) are listed in `docs/TESTING.md`; all of them fail at least one test.


## Not verified / remaining

* Not independently reviewed. Nothing was looked at in a browser: the teacher form uses the existing classes and the shared tokens; its contrast and the
  checklist on a screen have not been checked. Nothing ran on Linux or under the real unit, and no real SQLite database, deployment, provider or message was involved.
* Concurrency was tested with threads on a macOS rollback-journal temporary database. The production database is WAL; its behaviour under the real Gunicorn worker is not tested.
* The migration was applied only to temporary databases built by the repository's runner. It has not run against any real database; the "starts above the limit" teachers of
  a real database are unknown.
* **Needs Gabriel:** I6 (admin accounts), I11 (a development switch for the teacher gate), and the wording of the two refusal messages.
* Activation prerequisites unchanged and not done: the privacy policy and D16 (purpose, visibility and retention of the school and subject fields, minors, and now the ledger),
  the amendment of `ACCOUNTS_DESIGN.md` §15 (a note is added here, not the amendment), the provider and sender, capacity, the Linux/systemd evidence of PR 5, password recovery
  (PR 8), and independent review of this PR. Teacher verification (that someone teaches) does not exist: the role is a verified mailbox plus self-declaration.
* Not covered: a quota override or operator command; per-IP or per-person limits; a `Retry-After`; showing the remaining allowance on `/teacher/new`.
