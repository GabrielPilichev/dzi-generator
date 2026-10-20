# Running tests without changing development data

From the repository root:

```sh
python3 -B -m unittest discover -s tests
```

Individual modules also work, including when imported before any other tests:

```sh
python3 -B -m unittest tests.test_favicon tests.test_auth_guard
```

## Application fixtures

### Password recovery scope 8.1

Focused operations suites (SQL fixture rows only, no issuance API):

```sh
python3 -B -m unittest tests.test_recovery_migration_sql tests.test_recovery_scrub \
  tests.test_recovery_operations tests.test_recovery_restore_credentials tests.test_recovery_no_caller
```

The approved-case mapping, versions, final counts and evidence gaps are in
`docs/reviews/PASSWORD_RECOVERY_8_1_STATUS.md`. The tests use real dependencies
and temporary databases. Static checks pin the unchanged restore script/Nginx
template, no production recovery inserts, schema allow-list, settlement purpose
guard and runbook requirements. The comparison tests include 5,760 combinations
of identity, password/timestamp, key, state and second-factor differences.

For RZ1 whole-suite instrumentation, a **dedicated scratch interpreter** may
load `tests.recovery_audit.install(<temporary-jsonl-report>)` through a `.pth`
import line after adding this repository to its import path. This also reaches
Python subprocesses that sanitize their environment. Never install that hook in
an application/deployment interpreter. It denies nonfixture recovery INSERTs
and checks zero recovery rows in nonfixture databases at connection close or
collection; summaries contain counts only. Aggregate all process summaries and
require zero violations. Missing historical storage and intentionally unreadable
SQLite rejection fixtures are separately counted, not represented as empty.
The audit temporarily disables the caller's trace callback for its own two
inspection queries, then restores it; application SQL still reaches the callback
unchanged, so query-budget and transaction-order assertions remain meaningful.
Static scans cover every `web`/`src` module; fresh-interpreter import tests cover
the application, WSGI, every blueprint and the operational command entry points.
This does not establish Linux isolation, real Nginx behavior, physical erasure,
provider delivery or scope 8.3 route behavior.

Since scope 8.2 the audit also allows an INSERT into `password_recoveries` whose
immediate caller is the service `web.services.recovery`, **only** while a recovery
test or fixture module is on the same thread's stack; the same service call from
anything else (an application, CLI or other test path) is denied and counted as a
violation (`tests.test_recovery_no_caller` proves the denial in a subprocess). The
close/collection check now also requires zero recovery-purpose ledger rows
(`registration_sends.recovery_id` set) in every nonfixture database.

### Password recovery scope 8.2

Service suites (temporary databases, in-memory mail, injected clocks; no route,
template or provider):

```sh
python3 -B -m unittest tests.test_recovery_service tests.test_recovery_eligibility \
  tests.test_recovery_redeem tests.test_recovery_ledger tests.test_recovery_concurrency \
  tests.test_recovery_polling tests.test_recovery_service_operations tests.test_recovery_no_caller
```

`tests/recovery_service_support.py` builds accounts through the real account
functions (verified accounts as registration activates them) and drives every
writer inside `BEGIN IMMEDIATE`, committing or rolling back as the 8.3 caller
must. The mapping to the test plan, deviations and remaining gaps are in
`docs/reviews/PASSWORD_RECOVERY_8_2_STATUS.md`. RZ2 (no production caller) is
static and import-level; it is not the reason the build is safe (design 7.3).

### Shared application fixtures

Application/request tests inherit `tests.support.IsolatedAppTestCase`. A class
overriding `setUpClass` must call `super().setUpClass()` before accessing the app
or creating its fixtures.

All app imports in tests go through `tests.support`, including pure helper tests.
It scopes `DZI_DB=:memory:` around the initial import because `web.app` runs a
legacy quiz migration at import time. The environment is immediately restored;
production import/startup behavior is not changed.

Each class receives its own temporary database, **built from migrations + `data/canonical`**,
not copied from a repository database (`data/questions.db` is untracked and may not exist): see *Test database architecture* below.
No migrations or imports run against the repository database. Because the database is built
at the current migration level (000 recorded), `subject_fixtures.apply_pending_migrations` is a
no-op on it. A database that predates the baseline is built synthetically with
`tests.support.build_legacy_database` (see *Baseline adoption tests*).
Both `app.config["DB_PATH"]` / `get_db()` and
`QUIZ_DB_PATH` / `quiz_db()` point to that temporary database. `DZI_DB` is set to
the same path for code that reads the environment.

Quiz logic lives in `web/services/quiz.py`, which never imports `web.app`;
`web.app` registers `QUIZ_DB_PATH`, `QUIZ_VAULT_PATH` and `PROJECT_ROOT` with it as
call-time providers, so patching those `web.app` globals still redirects quiz code.
To stub a quiz function (`quiz_db`, `quiz_pick_questions`),
patch it on `web.services.quiz`: patching the name re-exported by `web.app` only
affects `web.app`'s own callers.

**Controlling time.** Do not patch `quiz_current_timestamp` by hand. Starting,
submitting, uploading, creating, editing, duplicating and closing are lifecycle
writes, and each checks the wall clock against the database's `lifecycle_clock`
row and against a boot-time clock (`docs/ASSIGNMENT_LIFECYCLE_DESIGN.md`, 3.6).
A patched clock that disagrees with that row is, correctly, a clock fault: the
request gets 503 and the database stays `suspect` for the rest of the class.
Use `tests.support.lifecycle_time(start)` instead:

```python
with lifecycle_time(datetime(2026, 5, 15, 10, 0, 0)) as clock:
    ...                      # the application clock reads 10:00:00
    clock.advance(90)        # time passes: wall clock and boot time together
    clock.step_wall(-600)    # a clock step: only the wall clock moves (a fault)
```

It moves the application clock, the test clock environment and the clock row
together and restores real time and an unanchored clock row on exit.
`tests.support` installs that environment (`TEST_CLOCK`: one fixed boot, a
synchronised clock, real time unless frozen) for the whole test process, so
tests that do not control time need nothing. Stored timestamps a test writes
directly (an attempt's `started_at`, a closure) must not be later than the
clock of the request that reads them, for the same reason.

`tests/lifecycle_support.LifecycleTestCase` is the base of the lifecycle tests:
a fresh database and a frozen clock per test, direct fixtures for assignments
and attempts in an exact lifecycle state, and `self.cli(...)` for
`src.manage_lifecycle`. A hand-built minimal schema gets the lifecycle columns
and the clock row from `tests.support.add_lifecycle_schema(conn)`. Forms that
create, edit or duplicate an assignment must state their deadline choice; tests
that are not about deadlines post `**tests.support.NO_DEADLINE_FORM`.

`tests/test_lifecycle_restore_script.py` runs the documented restore,
`deploy/learnpilot-restore.sh`, as a real `bash` process against a temporary
state directory. It needs `bash`, the `sqlite3` command-line tool and `tar` on
`PATH` (all part of macOS and of the Ubuntu server image). `sudo`, `systemctl`
and `curl` are replaced by stand-ins in a temporary directory that comes first
in `PATH`; nothing outside the test's own directory is touched and no service
is started or stopped. The other tools, the pre-flight check and the operator
CLI are the real ones, behind a wrapper that makes one chosen call fail, or
kills the script at that call (`kill -9`: no handler runs, which is what a
power cut leaves on the disk), or lets the call do its work and only then
sends the script a signal or reports failure (`RESTORE_TEST_AFTER`: a rename
that happened before the script could note it). That is how the tests show
that the service is never started after a failed step, that an interrupted
run is never completed from what it left behind, that what is taken back is
decided by what is on the disk, and that the run after it loses nothing. The
stand-in for `systemctl` answers `show` from files in the test's directory:
the unit state of the service, the backup service and its timer, and the
service's `EnvironmentFiles=` and `Environment=`. The environment file the
tests write contains a secret, and every run fails if the script prints it.
Mount points cannot be made in a test; the pre-flight check for a filesystem
mounted inside `uploads` or `vault` is tested by replacing the three lookups
it asks (`_is_mount`, `_device`, `_mount_points`), and symbolic links for
real. The module runs the script well over a hundred times and takes about a
minute and a half.

`QUIZ_VAULT_PATH` and `DZI_VAULT` point to a temporary vault. Assignment and attempt
notes are really generated there, not mocked away. Practical uploads also get a
temporary root. Templates, static assets, reviewed batches and reference files
remain read-only inputs from the repository. Tests needing custom practical
schemas/resource files may retain their own temporary fixture directories.

Configuration, environment variables and path globals are restored after every
test and class, including failed setup. Class cleanup removes the temporary DB,
SQLite sidecars and generated artifacts. Database rows are shared within a class
to preserve existing class-level question fixtures; classes do not share writes.

`tests/test_schema_baseline.py` is the schema gate: an empty database plus
000-011 must equal a synthetic pre-baseline database after adopting 000 and applying the rest (tables, ordered columns, types, NOT NULL,
defaults, primary/foreign keys, indexes, partial indexes, CHECKs, AUTOINCREMENT)
using `tests/schema_snapshot.py`. A difference there means 000 or a migration is
wrong; never loosen the comparison. The same module can compare two database
files: `python3 -m tests.schema_snapshot --left A.db --right B.db`.

`tests/test_canonical_content.py` is the G1 canonical-content gate (`docs/CANONICAL_DATA.md`):
table classification completeness, deterministic JSONL/manifest, legacy and
Physics/Biology id preservation, the explicit-id round trip (migrations + canonical
load, built twice), parity/hash/sequence/reference checks and update-safety. It never reads
`data/questions.db`. The parity contract is: migrations + canonical -> a deterministic logical
database, and canonical export -> no drift. Changing canonical content means re-exporting through
`python3 -m src.canonical_export --replace-canonical` so the manifest hashes stay true.

Pure helper tests pass explicit in-memory connections and need no application
fixture. Migration/import tests use in-memory or temporary databases and files;
keep that boundary when adding tests. Never call an import or migration tool with
its default repository database from a test.

## Test database architecture (G2)

```
src.build_database  (migrations 000..current + data/canonical, ~1.5 s, once per process)
        |
        v
read-only template in a temp dir (sha256 recorded, re-verified on every clone)
        |  tests.support.build_test_database(path, profile)   # byte clone
        v
isolated writable database per test class / test
```

* `tests.support.canonical_template(profile)` builds the template on first use and never hands
  it to a test; `build_test_database(destination, profile=CANONICAL)` returns an isolated writable
  copy (rollback-journal, no sidecars). It raises if the template's bytes ever changed. The template
  lives only in a per-process temporary directory (removed at exit) and is never committed; it is
  rebuilt for each test process from the tracked inputs, so there is no timestamp cache to go stale.
* `IsolatedAppTestCase`, `isolated_app()` and every DB-backed test use it, so
  `tests` never read or mutate `data/questions.db`, `data/canonical` or the migrations (a test pins this).
* Profiles: `CANONICAL` (default) is the full canonical content, including the reviewed Physics and
  Biology. `IT_ONLY` is that template minus Physics/Biology, produced by the explicit fixture
  `tests.subject_fixtures.remove_subject_content`. Subject-preparation tests that pin "a subject
  without content renders as in preparation" opt in with `content_profile = IT_ONLY` (class
  attribute) or `build_test_database(path, IT_ONLY)`; do not use it to hide a real failure.
* Cost: the whole suite takes about the same as before G2 (about 65-75 s); the one-time template build
  is under 2 s and a clone is a ~3 MB file copy.

### Runtime fixtures

A built database has **no runtime rows** (assignments, attempts, answers, text answers,
practical submissions). The retired seed's smoke rows (2 assignments, 4 attempts, 19 answers)
are not recreated anywhere. A test that needs runtime state creates exactly that with
`tests/runtime_fixtures.py` (`add_assignment`, `add_attempt`, `add_answer`,
`add_submitted_attempt`) on its own temporary database: fixed values, ids that depend only on the
canonical content, nothing shared between tests. Operational tables (`obsidian_notes`,
`generated_exams`, ...) are likewise empty; insert what a test needs.

### Baseline adoption tests

Migration 000 lets an existing pre-baseline database adopt the baseline without running it.
`tests.support.build_legacy_database(path)` builds the smallest deterministic fixture for that:
the 000 DDL plus migrations 001-010 executed directly, `schema_migrations` recording 001-010 but
not 000, and a couple of rows. `test_schema_baseline` (dry run, apply, idempotence, refusal
until adopted, incompatible schemas) and the startup test in `test_production_deployment` use it.
No binary fixture or historical content is committed.

### The tracked seed is retired (H)

`data/questions.db` is no longer tracked. `tests/test_seed_retirement.py` pins that it and its
SQLite sidecars are ignored, that `data/canonical` and the migrations are not, that test
discovery and `import web.app` work without the file (and do not create it), and that no seed
copy/hash helper remains. The seed-transition tests (seed SHA pins, byte-copy helper tests,
canonical-vs-seed equivalence, seed immutability) and `src/canonical_reconcile.py` were removed.

`tests/test_build_database.py` covers the builder (fresh/missing-parent/existing destination,
migrations, hashes, ids, sequences, FK, empty runtime/operational tables, determinism, no seed
access, failure cleanup, corrupt input); `tests/test_database_test_support.py` the template,
isolation and fixtures; `tests/test_importer_stable_ids.py` importer identity and the DZI
option/subquestion rules.

## Connection policy and the runtime-database guard

Every connection the web app opens (`get_db()`, `quiz_db()`, the health check and
the browse-cache cold loaders) comes from `web.db.connect_db(path, *, query_only=False)`:
`sqlite3.Row`, `foreign_keys=ON`, a 5 second busy timeout, and `query_only` for the
read-only ones. It never sets a journal mode (WAL is chosen when a live database is
prepared, see `docs/DEPLOYMENT.md`) and it takes the path as an argument, so the
patched `DB_PATH` / `QUIZ_DB_PATH` values keep controlling which database is used.
`tests/test_web_db.py` and `tests/test_web_db_integration.py` pin this.

`tests/runtime_db_guard.py` keeps tests off a developer's local runtime database,
`data/questions.db`: `tests.support.isolated_app()` wraps `web.db.connect_db` so that asking
for that path raises `RuntimeDatabaseAccess` instead of opening it (for example when a leaked
`DB_PATH` points back at the checkout). The guard never reads, hashes or copies the file and
behaves the same whether or not it exists.

## Mail sender tests

`tests/test_mail_service.py` and `tests/test_mail_sender_deadline.py` send no
message and connect to nothing. The first runs the sender script's SMTP code in
the test process against a scripted fake peer under the real `smtplib`. The
second starts short-lived stand-in child processes from a temporary directory
and measures real time against a shortened deadline; it takes about 25 seconds,
nine of them the real script's own alarm. Each stand-in arms an alarm of its
own, so a failed run leaves no process behind. **Never import
`web/services/mail_sender.py`**: its first statements arm an alarm that ends
the importing process. The tests execute its source with those three calls
intercepted.

## Registration service tests

`tests/test_registration_service.py`, `tests/test_registration_ledger.py` and
`tests/test_registration_settlement.py` share `tests/registration_support.py`.
Each test clones the migrated canonical template into a temporary directory
and never opens `data/questions.db`. Time is a value the test sets and hands
to the service as its `clock`; nothing sleeps for a limit or reads the real
clock. Mail is `InMemoryMailer` or a small fake, so nothing is sent and
nothing is connected to. The service never commits: the fixture's `tx` begins
`BEGIN IMMEDIATE` and commits, or rolls back after an exception, as a caller
must. Concurrency tests use threads with one connection each to the same
file. One test starts `python -m src.manage_accounts create` as a process
against the temporary database. The property test of the guessing bound is
seeded and states its three assumptions in its docstring. Tests that compare
the codes of two issuances inject them (`RegistrationTestCase.codes`). The
failed-commit tests use a real `SQLITE_BUSY`: the temporary file is in
rollback-journal mode, a second connection holds a read transaction, and the
writer's `COMMIT` is refused with a zero busy timeout.
`tests/test_registration_issuance_migration_sql.py` upgrades a temporary
database from migration 021, with accounts, profiles, registrations and
ledger rows already in it, to 022, and compares it with a fresh build.

## Registration route, production and operator tests

`tests/test_registration_routes.py` (registration PR 4) gives every test its own
temporary database built from migrations and canonical data and enables
registration with `InMemoryMailer` in the application's configuration; nothing
is sent and no socket is opened. The registration clock is replaced
(`web.blueprints.registration._utcnow`), so nothing sleeps for a limit. A failed
`COMMIT` is injected by wrapping the request's own connection
(`FlakyCommits`): the chosen commits raise and the code under test rolls back.
Concurrency tests use threads with their own test clients, one connection per
request, against the same file. `tests/test_registration_production.py` starts
`web.wsgi` and `web.app` in fresh interpreters with synthetic mail settings
(only GETs and refused POSTs are made, so no sender process is ever started),
and checks the Nginx template statically: that is not evidence that a real
Nginx accepts it. `tests/test_registration_cli.py` runs the operator commands
of `src.manage_accounts` against temporary databases. The Nginx checks resolve
locations from the template text; `nginx -t` was not run against it.

**Tunnels.** The development app enables registration whenever usable mail
settings are present, so a public tunnel exposes working `/register` routes.
Test through a tunnel only with a fake or development backend
(`DZI_MAIL_BACKEND=console`, or `file` into a scratch directory), never real SMTP.

## Account profile tests

Registration PR 6 (`docs/reviews/EMAIL_REGISTRATION_PR6_STATUS.md`). Every database is a
temporary clone or build from migrations and canonical data; none is `data/questions.db`,
no mail is sent and no socket is opened.

- `tests/test_profile_required_migration_sql.py` upgrades a temporary database from
  migration 022, with accounts of every kind in it, to 023 and compares it with a fresh
  build; every existing account stays NULL. It also pins the file (one `ALTER TABLE`), the
  manifest level and that no canonical file changes.
- `tests/test_profile_requirement.py` tests `profiles.needs_profile` over every
  combination of gate, role, mark and profile type, the accounts additions
  (`mark_profile_required`, `set_display_name`, `profile_requirement`), the atomic profile
  functions and the profile form validators. A failed second write is injected and the
  caller rolls back, as a view does.
- `tests/test_account_profile.py` gives every test its own database and switches the gate
  on through the application configuration (`app.config[profiles.CONFIG_KEY]`). It covers
  the gate-off 404 and invisible link, the page for each account kind, creation and
  editing, unchanged roles, sessions, ownership and historical names, `next`, same-origin
  refusal, escaping, atomicity (an injected failure after the profile insert and a failed
  `COMMIT` through `FlakyCommits`) and the independence of the three gates.
- `tests/test_profile_google_onboarding.py` reuses the fake Google of
  `tests/google_oidc_support.py`: the mark of a created student, the same transaction
  (traced statements), rollback, and the accounts that are never marked.
- `tests/test_profile_attempt_gate.py` uses `tests/lifecycle_support.py` (a frozen
  lifecycle clock) and covers the order of the decision (resume, closed notice, then the
  profile), no attempt row on a profile-required outcome, and concurrency: the profile
  check is evaluated under the writer lock, which the tests hold from a second connection
  while the start waits, and two simultaneous starts create nothing. A test patches
  `lifecycle._begin_write` only to learn that the start reached the lock; nothing sleeps.
- `tests/test_profile_gate.py` starts `web.wsgi` and `web.app` in fresh interpreters: the
  build constant is the only thing that can turn the flow on in production, whatever
  `DZI_PROFILES_ENABLED` holds, and a development run reads that setting.

**Tunnels.** `DZI_PROFILES_ENABLED` turns the flow on in a development run only. Do not set
it when sharing a localhost tunnel unless that is the point of the test.

## Assignment quota and teacher registration tests

Registration PR 7 (`docs/reviews/EMAIL_REGISTRATION_PR7_STATUS.md`). Every database is a temporary
clone or build from migrations and canonical data and every vault is a temporary directory; none is
`data/questions.db` or the repository's `vault/`. Mail is `InMemoryMailer`: nothing is sent and no socket is opened.

- `tests/test_assignment_creations_migration_sql.py` upgrades a temporary database from migration 023, with owned,
  unowned, closed and badly dated assignments in it, to 024: no ledger row is written, nothing else changed, the table's
  constraints, a deletion of an assignment leaving the ledger alone, and parity with a fresh build.
- `tests/test_assignment_accounting.py` (review findings 1 and 2): the timestamp normalizer's rules; reconciliation of legacy shapes at the
  window edge; the unplaceable cases; admins; and `tests/legacy_creation.py`, a verbatim copy of the pre-PR 7 `quiz_create_assignment`
  (compared with git when the base commit is available), run against the PR 7 schema in rollback-journal and WAL mode in two classes
  (`RollbackRegressionJournalTest`, `RollbackRegressionWalTest`). The old function stamps `created_at` with SQLite's own clock, so those
  classes move the frozen lifecycle clock to the real present. The start-up notice is tested on private databases.
- `tests/test_assignment_quota.py` runs on a frozen lifecycle clock (`tests.support.lifecycle_time`). The service tests call
  `quiz_create_assignment` as the routes do and cover the exact window and open-count boundaries, closing versus the
  daily allowance, deadlines, holds, roles and role changes, existing assignments, unowned rows, and atomicity (a
  refusal changes no table, the clock row included; a failure injected with a real trigger after the insert consumes no
  allowance). The concurrency tests use threads with one connection each: the last daily slot and the last open slot go to
  exactly one of four simultaneous requests, and a request that waits for the lock is judged at the time it gets it
  (a second connection holds `BEGIN IMMEDIATE`, the test clock moves, nothing sleeps for a limit; one 0.4 s pause lets the
  waiting thread reach the lock). The route tests go through the application: both kinds of `/teacher/new`, the copy form,
  status, message, kept values, no vault note in the temporary vault, authorization before the quota, and a 500 for an
  injected storage failure. The static pins keep the one insert, the one decision before it, and the one writer of the ledger.
- `tests/test_teacher_registration.py` has a service layer (activation of a teacher: account, profile and subject rows together; a
  trigger-injected failure at every subject write, at the account, the profile and the final state; no role from submitted
  data; existing addresses of every role; gate off; a database without the ledger) and a route layer on the registration route
  fixture (`tests.test_registration_routes.RegistrationRouteTestCase`) with the teacher gate patched on
  (`registration_service.TEACHER_ACTIVATION`; no setting exists): the form, validation, journey, sign-in, access matrix,
  gate-off pending rows, activation failures and dispatch and settlement (one dispatch per issuance, the same send number and
  issuance identity settled, nothing settled after `NotSendable`, no retry).
- `tests/test_teacher_registration.py::RestartNavigationTest` pins that cancel and every ended, unavailable or failed registration page
  restart at the chooser, for both flows, with the validated `next`: a failed verification, resend, cancel or submission, a submission or
  resend or verify during a restore hold, a hold that appears between the submit's read and its write, a cancel refused by the clock, and a
  teacher's verify or resend after the gate went off (the target is read before the binding is retired); a missing or forged binding stays
  targetless and an unsafe target is never echoed.
- `tests/test_assignment_accounting.py::NormalizeTimestampTest` also pins that only ASCII digits are read: every digit position of five ASCII
  bases is replaced in turn by the same digit of four other scripts (304 subtests) and must be unplaceable, a database-level test refuses the
  account (503) on such a `created_at`, and a separate test keeps the ASCII forms, exact fractions and bounds unchanged.
- `tests/test_assignment_accounting.py::NormalizeTimestampTest` and `FractionalAllowanceTest` (review of `3537f4f`) pin the exact-instant rules:
  fractions of any length are rounded up, never truncated, so the reproduction (twenty creations at `…09:00:00.999999999Z`) still counts; the
  window edge with zero and nonzero fractions, offset-bearing fractions, UTC day and year crossings, the lower bound and the exact future bound.
- Existing tests that pinned "a teacher registration is never activated" or "`/register/teacher` is always 404" now pin the
  gate-off half and point at the new module; the migration level, the runtime-table lists and the manifest moved to 024.

```sh
python -B -m unittest tests.test_assignment_creations_migration_sql tests.test_assignment_quota \
  tests.test_assignment_accounting tests.test_teacher_registration
python -B -m unittest discover -s tests
```

Mutation checks used while writing them (each applied, run and reverted by a script; not part of the suite): `>=` to `>` in either limit,
the window's exclusive bound made inclusive, the exemption removed, the two limits reported in the other order, the ledger write
or the decision removed, the open predicate weakened, a pre-lock time used, the refusal mapped to a deadline error, the teacher role
entry removed, the subjects or school not passed to the profile, the ledger check removed, the gate always true or ignored at
verification, the chooser entry always shown, and `admin` added to the registration roles: all fail at least one test.

Mutation checks for the review of `3537f4f` (a scratch copy, each mutant applied, run and reverted by a script): the fraction truncated, a zero fraction
rounded up, positivity judged on six digits, the fraction limited to six or nine digits, the future bound ignoring or excluding the exact instant,
the fraction comparison made inclusive or blind below a microsecond, the lower bound checked after rounding, the offset flipped or ignored, and
every return-target pass-through removed or replaced by a raw request value: all fail at least one test (one equivalent mutant survives). For the ASCII
restriction (scratch copy): `\d` restored in the year, the month and day, the time, the fraction, the offset, and everywhere without `re.ASCII`: all fail.

**Tunnels.** The teacher gate has no development setting. Nothing in a localhost tunnel can turn the teacher form on.

## Previous isolation problems

- `web.app` used to run `quiz_apply_migration()` at import time, before unittest
  class setup, so a test that only imported a formatting helper could open and
  change the real database. That migration is gone (schema changes are only made
  by `python3 -m src.manage_migrations`); importing `web.app` with a real `DZI_DB`
  now only does a read-only pending-migration check that logs a warning.
- Import-time `DZI_DB` / `DZI_VAULT` assignments depended on which test first
  imported `web.app`; later environment changes did not update captured globals.
- Browsing used `app.config["DB_PATH"]`, which those environment assignments did
  not change. Auth, homepage, favicon, mobile, teacher and other route tests could
  open the repository DB through `get_db()` and its WAL pragma.
- Assignment/mixed/quiz tests wrote through whichever `QUIZ_DB_PATH` and vault
  globals happened to be active. Several classes reassigned these without
  restoring them, leaking state into subsequent tests.
- Pure helper tests unnecessarily created database copies and changed environment
  variables during discovery, despite using in-memory DBs or no DB at all.
- Practical tests already owned their custom DB/upload fixtures, but leaked some
  application configuration such as `TESTING`. The shared fixture now also
  restores this state.

Production configuration, authentication and scoring are unchanged. The helper
also scopes Flask's first-request marker so legacy test-only auth routes can be
registered regardless of which application test ran first.

For a recovery-sensitive run, compare a
content/mtime manifest of `vault/Generated/Quizzes/`, and `git status --short`
before and after. `-B` avoids persistent bytecode artifacts.
