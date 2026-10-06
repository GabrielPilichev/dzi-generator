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

Application/request tests inherit `tests.support.IsolatedAppTestCase`. A class
overriding `setUpClass` must call `super().setUpClass()` before accessing the app
or creating its fixtures.

All app imports in tests go through `tests.support`, including pure helper tests.
It scopes `DZI_DB=:memory:` around the initial import because `web.app` runs a
legacy quiz migration at import time. The environment is immediately restored;
production import/startup behavior is not changed.

Each class receives its own temporary database, **built from migrations + `data/canonical`**,
not copied from the tracked seed (`data/questions.db`): see *Test database architecture* below.
No migrations or imports run against the repository database. Because the database is built
at the current migration level (000 recorded), `subject_fixtures.apply_pending_migrations` is a
no-op on it. `tests.support.adopt_baseline` and `copy_seed_database` remain only for the
transition tests listed in `tests/test_seed_transition.py`.
Both `app.config["DB_PATH"]` / `get_db()` and
`QUIZ_DB_PATH` / `quiz_db()` point to that temporary database. `DZI_DB` is set to
the same path for code that reads the environment.

Quiz logic lives in `web/services/quiz.py`, which never imports `web.app`;
`web.app` registers `QUIZ_DB_PATH`, `QUIZ_VAULT_PATH` and `PROJECT_ROOT` with it as
call-time providers, so patching those `web.app` globals still redirects quiz code.
To stub a quiz function (`quiz_db`, `quiz_pick_questions`, `quiz_current_timestamp`),
patch it on `web.services.quiz`: patching the name re-exported by `web.app` only
affects `web.app`'s own callers.

`QUIZ_VAULT_PATH` and `DZI_VAULT` point to a temporary vault. Assignment and attempt
notes are really generated there, not mocked away. Practical uploads also get a
temporary root. Templates, static assets, reviewed batches and reference files
remain read-only inputs from the repository. Tests needing custom practical
schemas/resource files may retain their own temporary fixture directories.

Configuration, environment variables and path globals are restored after every
test and class, including failed setup. Class cleanup removes the temporary DB,
SQLite sidecars and generated artifacts. Database rows are shared within a class
to preserve existing class-level question fixtures; classes do not share writes.

`tests/test_schema_baseline.py` is the schema-parity gate (a seed-transition test): an empty database plus
000-010 must equal the seed's schema (tables, ordered columns, types, NOT NULL,
defaults, primary/foreign keys, indexes, partial indexes, CHECKs, AUTOINCREMENT)
using `tests/schema_snapshot.py`. A difference there means 000 or a migration is
wrong; never loosen the comparison. The same module can compare two database
files: `python3 -m tests.schema_snapshot --left A.db --right B.db`.

`tests/test_canonical_content.py` is the G1 canonical-content gate (`docs/CANONICAL_DATA.md`):
table classification completeness, deterministic JSONL/manifest, legacy and
Physics/Biology id preservation, the explicit-id round trip (migrations + canonical
load, built twice), parity/hash/sequence/reference checks, update-safety and seed
immutability. It reads the seed only through `copy_seed_database` copies and asserts the
seed hash and sidecars are unchanged. Changing canonical content means re-exporting through
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
* `IsolatedAppTestCase`, `isolated_app()` and the other seed-copy call sites use it, so
  `tests` never mutate `data/questions.db`, `data/canonical` or the migrations (a test pins this).
* Profiles: `CANONICAL` (default) is the full canonical content, including the reviewed Physics and
  Biology. `IT_ONLY` is that template minus Physics/Biology, produced by the explicit fixture
  `tests.subject_fixtures.remove_subject_content`. Subject-preparation tests that pin "a subject
  without content renders as in preparation" opt in with `content_profile = IT_ONLY` (class
  attribute) or `build_test_database(path, IT_ONLY)`; do not use it to hide a real failure.
* Cost: the whole suite takes about the same as before G2 (about 65-75 s); the one-time template build
  is under 2 s and a clone is a ~3 MB file copy.

### Runtime fixtures

A built database has **no runtime rows** (assignments, attempts, answers, text answers,
practical submissions). The legacy seed's smoke rows (2 assignments, 4 attempts, 19 answers)
are not recreated anywhere. A test that needs runtime state creates exactly that with
`tests/runtime_fixtures.py` (`add_assignment`, `add_attempt`, `add_answer`,
`add_submitted_attempt`) on its own temporary database: fixed values, ids that depend only on the
canonical content, nothing shared between tests. Operational tables (`obsidian_notes`,
`generated_exams`, ...) are likewise empty; insert what a test needs.

### Seed-transition tests (until H)

`copy_seed_database` (a byte copy of the tracked seed) is still used by exactly the tests
listed in `tests/test_seed_transition.py`: canonical-vs-seed equivalence
(`test_canonical_content`), schema parity (`test_schema_baseline`), baseline adoption
(`test_production_deployment`) and the copy helper's own tests. That module fails if another test
starts copying the seed. The seed's SHA-256 and sidecar absence are pinned there too. These are
removed with the seed in H.

`tests/test_build_database.py` covers the builder (fresh/missing-parent/existing destination,
migrations, hashes, ids, sequences, FK, empty runtime/operational tables, determinism, no seed
access, failure cleanup, corrupt input); `tests/test_database_test_support.py` the template,
isolation and fixtures; `tests/test_importer_stable_ids.py` importer identity and the DZI
option/subquestion rules.

## Connection policy and the seed tripwire

Every connection the web app opens (`get_db()`, `quiz_db()`, the health check and
the browse-cache cold loaders) comes from `web.db.connect_db(path, *, query_only=False)`:
`sqlite3.Row`, `foreign_keys=ON`, a 5 second busy timeout, and `query_only` for the
read-only ones. It never sets a journal mode (WAL is chosen when a live database is
prepared, see `docs/DEPLOYMENT.md`) and it takes the path as an argument, so the
patched `DB_PATH` / `QUIZ_DB_PATH` values keep controlling which database is used.
`tests/test_web_db.py` and `tests/test_web_db_integration.py` pin this.

Two safeguards (`tests/seed_guard.py`) keep tests off the tracked seed,
`data/questions.db`:

- `tests.support.isolated_app()` wraps `web.db.connect_db` so that asking for the
  seed raises `SeedDatabaseAccess` instead of opening it (for example when a leaked
  `DB_PATH` points back at the repository). A test that really must read the seed
  through the factory wraps the call in `allow_seed_inspection()`; copying its bytes
  with `copy_seed_database` is always allowed.
- Importing the `tests` package records the sha256 of the seed and its `-wal`; at
  interpreter exit a difference prints a `SEED DATABASE TRIPWIRE` message and the
  process exits with status 1 even if every test passed. If it fires, find the
  writer (or a local server running against the seed) and `git restore
  data/questions.db`.

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
- Pure helper tests unnecessarily created seed copies and changed environment
  variables during discovery, despite using in-memory DBs or no DB at all.
- Practical tests already owned their custom DB/upload fixtures, but leaked some
  application configuration such as `TESTING`. The shared fixture now also
  restores this state.

Production configuration, authentication and scoring are unchanged. The helper
also scopes Flask's first-request marker so legacy test-only auth routes can be
registered regardless of which application test ran first.

For a recovery-sensitive run, compare the SHA-256 of `data/questions.db`, a
content/mtime manifest of `vault/Generated/Quizzes/`, and `git status --short`
before and after. `-B` avoids persistent bytecode artifacts.
