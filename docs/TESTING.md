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

Each class receives a temporary copy of the existing seed database and its WAL,
if present. The source is read as files, never opened through SQLite: even a
read-only SQLite connection can update its shared-memory sidecar. Copying fails
if the seed changes during the copy; retry when development writes are idle.
No migrations or imports run against the repository database.
Both `app.config["DB_PATH"]` / `get_db()` and
`QUIZ_DB_PATH` / `quiz_db()` point to that temporary database. `DZI_DB` is set to
the same path for code that reads the environment.

`QUIZ_VAULT_PATH` and `DZI_VAULT` point to a temporary vault. Assignment and attempt
notes are really generated there, not mocked away. Practical uploads also get a
temporary root. Templates, static assets, reviewed batches and reference files
remain read-only inputs from the repository. Tests needing custom practical
schemas/resource files may retain their own temporary fixture directories.

Configuration, environment variables and path globals are restored after every
test and class, including failed setup. Class cleanup removes the temporary DB,
SQLite sidecars and generated artifacts. Database rows are shared within a class
to preserve existing class-level question fixtures; classes do not share writes.

Pure helper tests pass explicit in-memory connections and need no application
fixture. Migration/import tests use in-memory or temporary databases and files;
keep that boundary when adding tests. Never call an import or migration tool with
its default repository database from a test.

## Previous isolation problems

- `web.app` invokes `quiz_apply_migration()` at import time, before unittest class
  setup. Even a test that only imported a formatting helper could open the real
  database if another module had not first configured the environment.
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
