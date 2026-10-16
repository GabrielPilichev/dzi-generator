# Email registration, PR 7 — teacher quotas and gated teacher registration: status and contract

Scope and interpretations: `docs/reviews/EMAIL_REGISTRATION_PR7_SCOPE.md`.
Design: `docs/reviews/EMAIL_REGISTRATION_DESIGN.md` (sections 6.3, 7.1, 7.3 and 9, PR 7; decisions D3, D4, D5, D14).
Tests: `docs/reviews/EMAIL_REGISTRATION_TEST_PLAN.md` (D3, D10, F16, I2, I3, I7, K5).
Earlier PRs: `EMAIL_REGISTRATION_PR1_STATUS.md` to `PR6_STATUS.md` (merged: #335 to #340).
Routes: `docs/AUTH_ROUTE_CONTRACT.md`, "Creation limits" and "Student registration". Operator text: `docs/DEPLOYMENT.md`, section 36.

## Status

**Review history and provenance** (PR #341):

1. `b15d02b067459ceded9dc4d096f7e2082ce7c81d` ("Add teacher registration and atomic assignment quotas", on top of `fd44c8f`) was independently reviewed at that exact SHA and returned
   **REQUEST CHANGES** with three findings (P1 rollback creates unaccounted quota history; P2 the migration's epoch fallback refunds recent legacy creations; P2 teacher restart links open
   the student form). Gabriel approved admin-exempt recording and no teacher development switch after that review.
2. The fixes for those three were committed as `3537f4f3ad9a28ee9aa147a6d93af4e6863da1ac`. That exact SHA was independently reviewed and returned **REQUEST CHANGES** with two P2 findings:
   fractional timestamps had their fraction discarded and could refund an allowance early (including at the future-tolerance bound); the failure, unavailable and gate-off pages dropped the validated return target.
3. The fixes for those two (precision rules and return-target handling, below) were committed as `e39fa9fe6224528a2bda202654f0e18ce17c85be` ("Preserve fractional quota history and registration return targets").
   The independent Claude review of exactly that SHA returned **APPROVE WITH NON-BLOCKING NOTES**.
4. The cleanup after that review was **committed** as `5f834aed6fbbaa64e1f161baa923dde03667f25a` ("Restrict quota timestamps to ASCII and record approved policies"): the legacy-timestamp parser accepts ASCII digits only
   (below), with its tests, and the policies Gabriel approved after the review of `e39fa9f` are recorded in the documents. The independent Claude review of exactly that SHA returned
   **APPROVE WITH NON-BLOCKING NOTES**. That is the reviewed content.
5. **Subsequent provenance-only edits** to the documents (this correction of the commit and review state), unstaged and uncommitted when written, are **outside the reviewed commit** and have not been reviewed.
   They change no executable file and no test.
6. **Final local full-suite verification** of the executable state `5f834aed6fbbaa64e1f161baa923dde03667f25a`: 4358 tests, **OK** (Tests, "Final full-suite verification"). This is a local test run,
   **not a review**: it adds no review verdict, and the independent reviews above are not test runs. Its record here is one more provenance-only document edit under item 5.

A review verdict is not an activation, a merge or an approval of a gate.

`registration.TEACHER_ACTIVATION`, `registration.PRODUCTION_ACTIVATION`, `profiles.PRODUCTION_ACTIVATION` and `google_oidc.PRODUCTION_ACTIVATION` are all
still `False`, and no setting or environment variable overrides any of them in production. No real `data/questions.db`, vault, credential, provider, running
service, deployment or tunnel was involved. Canonical JSONL content is unchanged. The quota limits, unlike the gates, apply as soon as this build runs
(DEPLOYMENT section 36). Approving this implementation will not be an activation.

## Approved decisions

Per teacher: at most 20 assignment creations in a rolling 24 hours; at most 10 open assignments; closing frees an open slot and refunds no creation; enforced atomically across creation paths; a verified
teacher registration receives role `teacher` only when the teacher gate permits activation; every production gate stays off. **Approved after the review of `b15d02b`:** admin creations are recorded and
exempt from refusal; no development switch for the teacher gate. **Approved after the review of `e39fa9f`** (full list and chronology in the scope, "Approved decisions"):

* the rolling window `(now − 24 hours, now]`, with conservative counting of tolerated future rows (I1); the whole-second accounting's possible sub-second difference of the window is accepted;
* date-only timestamps map to 23:59:59 UTC; timestamps before 2000-01-01 are rejected; the future tolerance is one day; nonzero fractions round upward (I13);
* held assignments count as open and a passed deadline frees an open slot (I2); the daily refusal takes precedence over the open-limit refusal (I3);
* statuses: daily 429, open 409, unreliable accounting 503;
* creation-ledger history is kept for the account's lifetime, **subject to the unresolved privacy and retention activation prerequisite (D16)**, which is not settled and is no legal conclusion;
* after known lost history, creation stays closed until a separately reviewed recovery procedure exists: an **operational requirement**, not a claim that an automatic hold or recovery mechanism exists.

**Not approved:** I4, I5, I7, I8, I9, the remaining parts of I10 (typed values kept, no `Retry-After`) and of I12 (the reconciliation mechanism; only its 503 is approved), and **the exact wording of the three refusal
messages**: approving a status or a behaviour does not approve its wording. The messages as built (`assignment_quota.MESSAGES_BG`):

* daily (429): 'Достигна лимита от 20 нови теста за 24 часа. Затварянето на тест не връща този лимит. Опитай отново по-късно.'
* open (409): 'Имаш 10 отворени теста, колкото е позволено. Затвори някой от тях, за да създадеш нов.'
* accounting (503): 'Създаването на нови тестове е временно спряно за този профил, докато се проверят данните за досегашните му тестове. Обърни се към администратор.'

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
| Restart links to the chooser, with the validated return target on every message page | `web/blueprints/registration.py`, `web/templates/register_message.html` |
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

`normalize_legacy_timestamp(value, now)`, full list in its docstring and the scope (I13): UTC throughout; canonical, ISO `T`, fractional seconds of any number of digits, `Z` and offsets up to 14 h converted **first**;
**no offset = UTC**; **date-only = 23:59:59 UTC of that day**. Rejected (the owner is refused, never treated as old): NULL, empty or padded text, numbers, other layouts, impossible dates or offsets,
before 2000-01-01 (the epoch included), **more than one day after the writer clock**. **Future values, explicitly:** a value up to a day ahead is kept as it is and counts until it is a day old, so the
count is conservative there; the window count itself has no upper bound by design (a lifecycle row is never ahead of a later `now`), and the one-day bound lives in the normalizer so an implausible future date cannot hold an
account for years. Operator handling is in DEPLOYMENT section 36.

**Precision (review of `3537f4f`, finding 1).** Before, the fraction was matched and dropped, so `2026-03-01T09:00:00.999999999Z` counted as `09:00:00`, which left the window at 2026-03-02 09:00:00 although the creation was
a second younger than 24 hours. Now:

* the instant is judged exactly, to the last digit given; the fraction is never converted to a number (its digits are compared as text), so no `datetime` microsecond limit and no length of fraction (nine digits, forty, five thousand) discards a significant digit;
* the ledger counts whole seconds, so a **nonzero** fraction is rounded **up** to the next second and a zero fraction (`.0`, `.000000000`) is the second itself. Rounding up can only keep an allowance spent longer than the exact instant would, never refund it early;
* an offset is applied before the rounding, so a fraction that carries a second across a UTC minute, hour, day, month or year boundary is rounded in UTC (`2026-03-02T01:59:59.1+02:00` is `2026-03-02 00:00:00`);
* the lower bound is checked on the instant (`1999-12-31T23:59:59.9` is refused although it rounds to 2000-01-01); the future bound too: `now` plus exactly one day is accepted, any positive excess, however small, is refused, including when the bound has microseconds itself;
* ordinary whole-second timestamps behave exactly as before; a refusal still writes nothing.

The round-up, the date-only mapping, the 2000-01-01 cutoff and the one-day tolerance are **approved**. The 14-hour offset limit and reading a time without an offset as UTC are not separately approved.

**ASCII digits only (cleanup after `e39fa9f`, committed as `5f834ae`, which was independently reviewed: APPROVE WITH NON-BLOCKING NOTES).** `_LEGACY_TIMESTAMP` is `[0-9]` with `re.ASCII`. Python's `\d` also matches the decimal digits of other scripts and `int()` reads them (`٢٠٢٦-03-01`
would have been read as 2026), so a value no `CURRENT_TIMESTAMP` could have written was given an age. Such a value, in the year, month, day, hour, minute, second, offset or fraction, is now unplaceable like any
unusable value: the account's creation is refused (503) until an operator corrects the value, and nothing is written. The supported ASCII forms, the exact fractions, the offset conversion, the bounds and the
conservative rounding are unchanged. The start-up report's clock is not changed.

### Interpretations not separately approved by Gabriel (from the scope)

The independent review of `5f834ae` classified the remaining interpretations below as **routine implementation details**. That is the reviewer's classification, not an approval by Gabriel; no additional
product approval is recorded here, and the exact Bulgarian copy of the three messages remains Gabriel's decision.

I4 account-based counting, a copy counts for the copier; I5 the role is read under the lock and quotas are enforced unless it is `admin`; I7 the limits apply to every teacher account, operator-created or not; I8 no history is
written by the migration and the first admission records it; I9 unowned rows; the rest of I10 (typed values kept, no `Retry-After`); the reconciliation design of I12; the wording of the three messages. **Approved, no longer open:**
I1, I2, I3, the statuses of I10 and I12, the timestamp policies of I13, ledger retention (subject to D16), the lost-history requirement, I6 (admins recorded, exempt) and I11 (no development switch).

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
* **Return target on the unavailable, failure and gate-off pages (review of `3537f4f`, finding 2).** The shared helpers `_unavailable` and `_failure` now take the target like `_no_registration` did.
  Submit passes the validated form value (`accepted_next` already ran in the form); verify, resend and cancel pass the revalidated slot's `next`, which is read from the slot dict **before** the binding is
  retired, so the gate-off page of a teacher who began at `/teacher/new` still carries it although the session key is gone. A missing slot, a slot of another shape or a forged `next` gives a targetless page
  (no row is read, nothing is echoed); no raw request value is ever used. Binding, lifecycle, no-session and dispatch behaviour are unchanged.

## Tests

Environment: macOS, a scratch virtual environment built from `requirements-dev.txt` (real `segno`, `pyotp`, `cryptography`, Authlib; no stubs):
Python 3.12.15, SQLite 3.53.4, Flask 3.1.3, Werkzeug 3.1.9, segno 1.6.6, Authlib 1.9.1, cryptography 50.0.2, pyotp 2.10.0, python-docx 1.2.0.

**Earlier full run (at b15d02b's content), reported accurately:** 4280 tests, about 14 minutes, **1 failure**: a stale PR 6 assertion
(`test_profile_required_migration_sql...test_a_fresh_canonical_build_has_the_column_and_no_account`, "023 is the last migration"). It was loosened and the four migration-level
modules were re-run (88 tests, OK). **That was not a green final full run, and the full suite was not repeated at that time.** It ran as a background task (the foreground limit is 10 minutes). The later green full run at
`5f834ae` is recorded below ("Final full-suite verification"); this failed run stays as history and is not superseded in what it records.

**For the review of `3537f4f` (committed as `e39fa9f`), no full-suite repeat** (the change is confined to the timestamp normalizer, the message helpers of the registration blueprint and documents; the
affected suites below cover what reads them). Environment as above, temporary databases only:

* New and changed modules: `test_assignment_accounting` 57 (44 before; 13 new), `test_assignment_quota` 57, `test_assignment_creations_migration_sql` 16, `test_teacher_registration` 85 (75 before; 10 new, plus one gate-off pin corrected): **OK**.
* Affected suites, one batch of 33 modules with real dependencies: **1325 tests OK** (quota, accounting, migration, all registration modules, teacher registration, profile and profile-gate modules, access policy, auth guard, lifecycle static and clock,
  baseline, build, canonical, test support, production deployment, subject registry, owner routes, teacher lifecycle routes). `py_compile` of the four changed Python files and `git diff --check`: clean.
* **The new regressions reject the previous implementation.** The tree of `3537f4f` exported with `git archive` and the current two test modules copied over it: 142 tests, **57 failures and 3 errors**, **23 distinct tests** (20 new and 3 whose
  old expectations were corrected; 23 tests are new in all): the reproduction (twenty assignments at `…09:00:00.999999999Z`), the exact lower boundary with zero and nonzero fractions, more than six fractional digits, offset-bearing fractions, UTC day and year crossings, the exact future
  bound and any positive excess, refusal atomicity, and every return-target case (failed verification, resend, cancel and submission; submission and resend or verify during a restore hold; a hold appearing between the read and the write;
  a cancel refused by the clock; gate-off verify and resend). Three of the new tests are guards and pass on both (ordinary timestamps at the window edge; a binding retired by expiry keeps its target; an unsafe target is never carried).
* **Mutation checks** (23 mutants applied to a scratch copy, run against the normalizer, fractional allowance, legacy-format, navigation and route modules, and reverted by a script; not part of the suite): fraction truncated instead of rounded up; zero fractions rounded up too; positivity
  judged on six digits; the fraction limited to nine or to six digits; future bound ignoring the fraction; exact future bound excluded; `>` made `>=` in the fraction comparison; sub-microsecond digits ignored in that comparison; lower bound checked after rounding;
  offset sign flipped; offset ignored; and eleven navigation mutants (verify, resend, cancel and submit failure or unavailable pages each dropping the target; the unavailable page after a hold between read and write;
  the submit trusting the raw request `next`; the message page echoing a raw request `next`): **all killed**, after one added test killed the submit's second unavailable page, which the first run showed unpinned. One mutant (the resend failure reading the
  session value instead of the slot dict, which is the same value) is equivalent and survives. The mutation lists of earlier rounds (this file, TESTING.md) were not re-run.

**Earlier rounds, unchanged.** Review of `b15d02b`: 977 and 555 tests OK in two batches; the new regressions at that time failed against `b15d02b` (62 failures, 54 errors, every rollback test in rollback-journal and WAL mode, which are separate classes each asserting
`PRAGMA journal_mode`); 16 mutants on `reconcile`, the normalizer, the inconsistency check, partial recording, the future bound, the anti-join, the navigation and the start-up notice, all killed except one equivalent (`str()` coercion of non-strings).

**Subsequent ASCII-digit cleanup (committed as `5f834ae`, independently reviewed there), no full-suite or full-audit repeat when it was recorded.** Real dependencies, temporary databases only:

* `test_assignment_accounting` **60** (57 before; 3 new), `test_assignment_quota` **57**, `test_assignment_creations_migration_sql` **16**, `test_teacher_registration` **85**: **all OK**. `ast` parse of the two changed Python files and `git diff --check`: clean.
* New tests: every digit position of five ASCII bases (date only; canonical; `T` with fraction and offset; `T` with a long fraction and `Z`; canonical with fraction) replaced in turn by the same digit of Arabic-Indic, fullwidth, Devanagari
  and Bengali (304 subtests) must be `None`; the ASCII forms, exact fractions and bounds are unchanged (a separate test); and an assignment dated with Arabic-Indic digits refuses the account (503), writes nothing and is admitted
  once corrected.
* **The Unicode-digit regression rejects the previous parser.** The tree of `e39fa9f` exported with `git archive` and the new accounting test module copied over it: 60 tests, **305 failures** in the two Unicode tests (304 subtests and the database test);
  the ASCII-preservation test passes on both.
* **Mutation checks** (scratch copy, reverted by a script): restoring `\d` in the year, in the month and day, in the time, in the fraction, in the offset, and everywhere without `re.ASCII`: **all killed**.

The statement above that the cleanup had no full-suite repeat is true of that time and is kept as it was; the full suite ran afterwards, as follows.

**Final full-suite verification (local, after the independent review of `5f834ae`).**

* Branch `feature/teacher-registration-quotas`; tested executable state `5f834aed6fbbaa64e1f161baa923dde03667f25a`.
* Command: `python -B -m unittest discover -s tests`.
* Result: **4358 tests, OK**: zero failures, zero errors; 843.6 seconds.
* Run as a background tool session, waited on until it completed. Real dependencies; temporary databases only.
* This is local verification, separate from the independent review: the review verdict (APPROVE WITH NON-BLOCKING NOTES) is not a test result, and this run is not a review. No audit was repeated with it,
  and no executable file or test was changed to obtain it.

## Not verified / remaining

* **Reviewed content:** `e39fa9f` and, with the ASCII-digit cleanup, `5f834ae` were each independently reviewed at their exact SHA (APPROVE WITH NON-BLOCKING NOTES). Later documentation edits are outside the reviewed commit. **Local full suite:** 4358 tests OK at `5f834ae` (above); that is local verification, not a review. Nothing was looked at in a browser; the teacher form uses existing classes. Nothing ran on Linux or under the real unit; no real SQLite database, deployment, provider or message was involved.
* Concurrency was tested with threads on macOS temporary databases in rollback-journal and WAL mode; production's WAL under the real Gunicorn worker is not tested.
* The migration has run only on temporary databases. The accounting rules have not met a real database: how many real assignments have an unplaceable `created_at`, and how many teachers start above 10 open assignments, is unknown.
* **Recovery limitations:** deleted unrecorded history **cannot be reconciled**; a build that creates without the admission, concurrently with PR 7 builds, is **prohibited** (it can create unlimited assignments while it runs, and recovery only counts the surviving rows afterwards);
  after a **known history loss** (rows deleted before they were recorded, a restore to an older snapshot, a replaced database) **creation stays closed until a separately reviewed recovery procedure exists** (an approved operational requirement). No such procedure is written, and the code
  can neither detect the loss nor hold creation: keeping creation closed is an operator action (the Nginx block of DEPLOYMENT section 36). Nothing here is a quota exception. The restore returns the ledger to the snapshot's moment.
* **Needs Gabriel:** the wording of the three refusal messages, the interpretations listed above as not separately approved (classified by the review of `5f834ae` as routine implementation details, which is not an approval by Gabriel), the recovery procedure itself, the ledger's retention period and purpose together with D16, and whether the restart should instead remember the form the person began (it would need the binding to carry a type, which this fix does not change).
* Activation prerequisites unchanged and not done: the privacy policy and D16 (purpose, visibility and retention of the school and subject fields, minors, and the ledger), the amendment of `ACCOUNTS_DESIGN.md` §15, the provider and sender, capacity, the Linux/systemd evidence of PR 5, password recovery
  (PR 8), and the approval of this PR (reviewed content at `5f834ae`: APPROVE WITH NON-BLOCKING NOTES, as it was at `e39fa9f`; later documentation edits are outside it). Teacher verification does not exist: the role is a verified mailbox plus self-declaration.
* Not covered: a quota override or operator command; per-IP or per-person limits; a `Retry-After`; showing the remaining allowance on `/teacher/new`.
