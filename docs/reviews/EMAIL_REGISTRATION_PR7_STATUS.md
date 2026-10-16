# Email registration, PR 7 — teacher quotas and gated teacher registration: status and contract

Scope and interpretations: `docs/reviews/EMAIL_REGISTRATION_PR7_SCOPE.md`.
Design: `docs/reviews/EMAIL_REGISTRATION_DESIGN.md` (sections 6.3, 7.1, 7.3 and 9, PR 7; decisions D3, D4, D5, D14).
Tests: `docs/reviews/EMAIL_REGISTRATION_TEST_PLAN.md` (D3, D10, F16, I2, I3, I7, K5).
Earlier PRs: `EMAIL_REGISTRATION_PR1_STATUS.md` to `PR6_STATUS.md` (merged: #335 to #340).
Routes: `docs/AUTH_ROUTE_CONTRACT.md`, "Creation limits" and "Student registration". Operator text: `docs/DEPLOYMENT.md`, section 36.

## Status

**Review history.** `b15d02b067459ceded9dc4d096f7e2082ce7c81d` (PR #341, "Add teacher registration and atomic assignment quotas", on top of `fd44c8f`) was
independently reviewed at that exact SHA and returned **REQUEST CHANGES** with three findings (P1 rollback creates unaccounted quota history; P2 the
migration's epoch fallback refunds recent legacy creations; P2 teacher restart links open the student form). **The fixes described below are
unstaged and uncommitted, were made after that review, and await independent review.** Nothing is approved, merged or activated.

`registration.TEACHER_ACTIVATION`, `registration.PRODUCTION_ACTIVATION`, `profiles.PRODUCTION_ACTIVATION` and `google_oidc.PRODUCTION_ACTIVATION` are all
still `False`, and no setting or environment variable overrides any of them in production. No real `data/questions.db`, vault, credential, provider, running
service, deployment or tunnel was involved. Canonical JSONL content is unchanged. The quota limits, unlike the gates, apply as soon as this build runs
(DEPLOYMENT section 36). Approving this implementation will not be an activation.

## Approved decisions (input)

Per teacher: at most 20 assignment creations in a rolling 24 hours; at most 10 open assignments; closing frees an open slot and refunds no
creation; enforced atomically across creation paths; a verified teacher registration receives role `teacher` only when the teacher gate permits
activation; every production gate stays off. **Approved after the review of #341:** admin creations are recorded and exempt from refusal; no
development switch for the teacher gate. **Not approved** (interpretations, scope I1 to I3 and I5, I7 to I10, I12, I13): the exact boundary semantics, held and
deadline behaviour of the open count, refusal precedence, the accounting and timestamp rules below, and the refusal statuses.

## What PR 7 contains

| Part | File |
| --- | --- |
| Migration 024: `assignment_creations` and its index; **no rows** | `web/migrations/024_assignment_creations.sql` (new) |
| Manifest level and lists, table classification | `data/canonical/manifest.json`, `src/canonical_tables.py` |
| The limits and, since the review, the ledger check: constants, `enforce`, `reconcile`, `normalize_legacy_timestamp`, `record`, `accounting_report`, messages, statuses | `web/services/assignment_quota.py` (new) |
| The decision inside the one insert; `transaction` argument; `begin_write` | `web/services/quiz.py` (`quiz_create_assignment`), `web/services/lifecycle.py` |
| The open count without writing the open predicate twice | `web/services/lifecycle.py` (`UNCLOSED_STATE_SQL`, `count_unclosed_owned`; `OPEN_PREDICATE_SQL` is unchanged text) |
| Refusals on `/teacher/new` (both kinds) and the copy form | `web/blueprints/teacher.py` |
| Start-up notice for unplaceable history (never refuses to start) | `web/production.py` (`assignment_accounting_warning`) |
| Restart links to the chooser | `web/blueprints/registration.py`, `web/templates/register_message.html` |
| Teacher activation: role entry, school and subjects to the profile, ledger check | `web/services/registration.py`, `web/services/accounts.py` |
| Teacher route, form, chooser entry, shared submit | `web/blueprints/registration.py`, `web/templates/register_teacher.html` (new), `web/templates/register_choose.html` |
| Tests | `tests/test_assignment_quota.py`, `tests/test_assignment_creations_migration_sql.py`, `tests/test_teacher_registration.py`, `tests/test_assignment_accounting.py` and `tests/legacy_creation.py` (new); pins updated in `tests/test_registration_routes.py`, `test_registration_service.py`, `test_account_profile.py`, `test_schema_baseline.py`, `test_build_database.py`, `test_canonical_content.py`, `test_database_test_support.py` |
| Documents | `EMAIL_REGISTRATION_PR7_SCOPE.md` (new), this file (new), the design, scope and test plan, `AUTH_ROUTE_CONTRACT.md`, `ACCOUNTS_DESIGN.md`, `DEPLOYMENT.md` (section 36), `TESTING.md`, and the two corrections to `EMAIL_REGISTRATION_PR6_STATUS.md` |

## Migration 024

**Number 024** (the next free one: `000` to `023` exist; a test pins the files are numbered without a gap). Additive: one `CREATE TABLE` and one
`CREATE INDEX`; `quiz_assignments` and `users` are not rebuilt or altered, no trigger is created, nothing is deleted, **no row is written**. It runs
inside the runner's `BEGIN ... COMMIT`, so a failure leaves nothing.

```
assignment_creations (
    id INTEGER PRIMARY KEY,
    owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    assignment_id INTEGER NOT NULL UNIQUE CHECK (typeof = 'integer' AND > 0),   -- no foreign key, on purpose
    created_at TEXT NOT NULL CHECK ('YYYY-MM-DD HH:MM:SS', 19 characters and 19 bytes, GLOB)
)
INDEX idx_assignment_creations_owner ON (owner_user_id, created_at)
```

* **What changed since b15d02b.** The first version copied history in SQL and turned `2025-01-01`, ISO `T` forms and NULL into the epoch, so twenty recent closed
  assignments counted as zero (finding 2). Now the migration writes nothing, and the history is recorded, by one Python normalizer, by the first admission of
  each owner (below). A SQL copy would have been a second set of timestamp rules; one set is the smallest safe approach, and a migration that cannot fail on
  data it is not going to trust has no partial state to leave.
* **Compatibility.** Code from before this release names its columns and still runs against the schema, but it records nothing and enforces nothing. **A build that
  keeps migration 024 must keep the quota admission** (DEPLOYMENT, "Rollback"); the instruction that allowed ordinary old-code creation against 024 is removed.
* **Retention.** Kept as long as the account; accounts are disabled, never deleted (`ON DELETE RESTRICT`). It holds an account id, an assignment
  number and a time: no name, email or text. No retention period for it is decided, and none is invented. The assignment retention cleanup that
  `docs/ASSIGNMENT_LIFECYCLE_DESIGN.md` describes (not written) must never touch it; a test pins that only the quota module writes it.
* **Why not `quiz_assignments.created_at`?** A creation must stay counted after the assignment is closed and after its row is deleted.
* **Classification.** Runtime, never exported to `data/canonical` (manifest and `src/canonical_tables.py`).

## The quotas

* **One decision.** `quiz_create_assignment` is the only `INSERT INTO quiz_assignments` in `web/` and `src/` (a static test). Its callers are
  the two helpers of `web/blueprints/teacher.py`: new test (plain and mixed) and copy. `/teacher/dzi-training`, the token and lifecycle tools and the importers create no
  assignment. Inside the lifecycle writer transaction (`BEGIN IMMEDIATE`, clock health, `now` sampled after the lock): the daily check, the open
  check, the insert, the deadline write, the ledger row; **first the ledger is reconciled (below)**. A caller that already holds a `LifecycleWrite` passes it as `transaction`; any other
  caller gets one from `lifecycle.begin_write`, which leaves it open, so the caller still owns the commit as before.
* **Refusal.** `QuotaExceeded(reason)` before anything is written; the transaction rolls back, so the clock mark is not raised either. `/teacher/new`
  and the copy form answer again with the typed values, a Bulgarian message and 429 (daily), 409 (open) or 503 (the account's history cannot be placed). A diagnostic line
  `lifecycle event=create_refused actor_user_id=<id> reason=<reason>` is logged; nothing else.
* **Order.** Authorization (404/403/redirect) and input validation come first, then the deadline, then the limits.
* **Per-account, not per-person.** No claim of abuse prevention per person is made.
* **Not covered.** A creation by a principal without an account id (tests and the retired shared sign-in only; no request produces one) is unowned and
  unaccounted. Nothing counts or limits a student, a visitor or an unowned row.

### Accounting recovery (finding 1)

* **The reproduction.** Using the exact pre-PR 7 `quiz_create_assignment` against the 024 schema left a ledger with no row; returning to PR 7 granted a second 20
  (rollback-journal and WAL). `tests/legacy_creation.py` is a verbatim copy of that function (git blob `8d45d3f0…` of `web/services/quiz.py` at `ede0c0b`); a test compares it
  with git when the commit is available.
* **Strategy chosen: reconcile under the writer lock at every admission, fail closed otherwise.** `assignment_quota.enforce` first calls `reconcile`, for every account,
  admins included (their history belongs to the account after a role change), before the exemption and the limits. It records the account's assignments that have no
  ledger row, all or nothing, from the age `normalize_legacy_timestamp` finds in `created_at`. It refuses the account's creation (`quota_accounting`, 503, nothing written, the
  transaction rolled back) when an unrecorded assignment cannot be placed or a ledger row disagrees with its assignment's owner. A deleted ledger row whose assignment still exists is
  re-recorded. The first admission after a restart, a rollback or a restore is therefore safe, and two racing first admissions record each assignment once (tested).
* **Recoverable / not.** Every surviving assignment row is recovered. An assignment row deleted before any admission recorded it cannot be reconstructed; a test pins that stated limit.
* **Why not a trigger or a start-up check alone.** A trigger would make old code self-recording, but it is the repository's first trigger, uses SQLite's clock rather than the writer
  clock, and would change the meaning of every raw owned insert in tests and tools. A start-up check cannot see rows created after start. A start-up notice exists
  (`production.assignment_accounting_warning`: counts only, never refuses to start, never an id or value) as an operator aid. (A production `web.wsgi` of an *older* build already refuses to
  start on migrations it does not ship; the exposed path is a recovery build that keeps the migration, which DEPLOYMENT now forbids from dropping the admission.)

### Timestamp rules (finding 2)

`normalize_legacy_timestamp(value, now)`, full list in its docstring and the scope (I13): UTC throughout; canonical, ISO `T`, fractional seconds cut, `Z` and offsets up to 14 h converted;
**no offset = UTC**; **date-only = 23:59:59 UTC of that day**. Rejected (the owner is refused, never treated as old): NULL, empty or padded text, numbers, other layouts, impossible dates or offsets,
before 2000-01-01 (the epoch included), **more than one day after the writer clock**. **Future values, explicitly:** a value up to a day ahead is kept as it is and counts until it is a day old, so the
count is conservative there; the window count itself has no upper bound by design (a lifecycle row is never ahead of a later `now`), and the one-day bound lives in the normalizer so an implausible future date cannot hold an
account for years. Operator handling is in DEPLOYMENT section 36.

### Interpretations that still need Gabriel (from the scope)

I1 window `(now − 24 h, now]`; I2 held-but-open counts as open and a passed deadline frees a slot; I3 the daily limit is reported first; I4 account-based counting, a copy counts for the copier; I5 the role
is read under the lock and quotas are enforced unless it is `admin`; I7 the limits apply to every teacher account, operator-created or not; I8 no history is written by the migration; I9 unowned rows; I10 status
codes (429/409/503) and no `Retry-After`; I12 and I13 the accounting and timestamp rules above. **Approved, no longer open:** I6 (admins recorded, exempt) and I11 (no development switch).

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
* **Restart navigation (finding 3).** Cancel, and every page that ends a registration (expired, cancelled elsewhere, replaced, no binding, teacher gate off, unavailable, failure), now
  links to the chooser `/register` with the stored `next` (revalidated when the slot is read and again by the chooser), never to `/register/student` or `/register/teacher`; with the teacher gate
  off the chooser offers the student form only. The binding, expiry, void and scrub rules are unchanged. A restart from the teacher flow therefore costs one more click, for both flows.

## Tests

Environment: macOS, a scratch virtual environment built from `requirements-dev.txt` (real `segno`, `pyotp`, `cryptography`, Authlib; no stubs):
Python 3.12.15, SQLite 3.53.4, Flask 3.1.3, Werkzeug 3.1.9, segno 1.6.6, Authlib 1.9.1, cryptography 50.0.2, pyotp 2.10.0, python-docx 1.2.0.

**Earlier full run (at b15d02b's content), reported accurately:** 4280 tests, about 14 minutes, **1 failure**: a stale PR 6 assertion
(`test_profile_required_migration_sql...test_a_fresh_canonical_build_has_the_column_and_no_account`, "023 is the last migration"). It was loosened and the four migration-level
modules were re-run (88 tests, OK). **That was not a green final full run, and the full suite has not been repeated since.** It ran as a background task (the foreground limit is 10 minutes).

**After the review of b15d02b (this change), no full-suite repeat** (the change is confined to the quota module, one migration comment, the registration restart links and a start-up notice, and
the adjacent suites below cover what reads them):

* New and changed modules: `test_assignment_accounting` 44 (new), `test_assignment_quota` 57, `test_assignment_creations_migration_sql` 16, `test_teacher_registration` 75 (10 are `RestartNavigationTest`): **OK**.
* Affected suites in two batches, real dependencies, temporary databases: **977 tests OK** (quota, accounting, migration, registration, profile, access policy, route snapshots, lifecycle static, baseline, build, canonical,
  test-support) and **555 tests OK** (assignment ownership, owner routes, duplicate, tokens, teacher blueprint, admin TOTP, practical review, lifecycle clock, production deployment, registration restore, application backup, and others).
* **Both journal modes.** The rollback regressions run in two classes, rollback-journal (`DELETE`) and `WAL`; each asserts the mode with `PRAGMA journal_mode` before it starts.
* **The new regressions reject the previous implementation.** An export of `b15d02b` with only an import shim (`REASON_ACCOUNTING`) run against the new tests: 62 failures and 54 errors (the errors are
  the normalizer and start-up notice that did not exist), including every rollback test in both modes, every legacy-format allowance, the unplaceable cases and 13 navigation tests.
* **Mutation checks** (each applied, run against the accounting, navigation and quota modules, and reverted by a script; not part of the suite): removing `reconcile`; running it only after the admin exemption; date-only as
  start of day; unplaceable values as year 2000; offset sign flipped; the future tolerance made huge; ISO `T` unsupported; the inconsistency check removed; partial recording of placeable rows; the future bound removed; the
  recorded-row anti-join narrowed to open assignments; the chooser links changed back to the student form (cancel, no binding, the message page); the start-up notice made fatal: **all killed**. One mutant, coercing non-strings with `str()`,
  survives and is equivalent (the regular expression rejects what it produces). The earlier mutation list for quotas and teacher registration (TESTING.md) was not re-run.

## Not verified / remaining

* **Fixes await independent review.** Nothing was looked at in a browser; the teacher form uses existing classes. Nothing ran on Linux or under the real unit; no real SQLite database, deployment, provider or message was involved.
* Concurrency was tested with threads on macOS temporary databases in rollback-journal and WAL mode; production's WAL under the real Gunicorn worker is not tested.
* The migration has run only on temporary databases. The accounting rules have not met a real database: how many real assignments have an unplaceable `created_at`, and how many teachers start above 10 open assignments, is unknown.
* **Gaps by design:** an assignment row deleted before it was recorded; a build without the admission can create unlimited assignments while it runs (it can only be recovered afterwards); the restore returns the ledger to the snapshot's moment.
* **Needs Gabriel:** the unapproved interpretations listed above (notably I2, I3, I12, I13 and the 503 for an unplaceable history), the wording of the three refusal messages, and whether the restart should instead remember the form the person began (it would need the binding to carry a type, which this fix does not change).
* Activation prerequisites unchanged and not done: the privacy policy and D16 (purpose, visibility and retention of the school and subject fields, minors, and the ledger), the amendment of `ACCOUNTS_DESIGN.md` §15, the provider and sender, capacity, the Linux/systemd evidence of PR 5, password recovery
  (PR 8), and independent review of this PR. Teacher verification does not exist: the role is a verified mailbox plus self-declaration.
* Not covered: a quota override or operator command; per-IP or per-person limits; a `Retry-After`; showing the remaining allowance on `/teacher/new`.
