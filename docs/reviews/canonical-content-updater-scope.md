# B4 canonical content updater: implementation scope

Status: scope for the B4 updater implementation PR. It is adopted only with a READY independent review of these exact bytes. The review verdict and the SHA256 of each file are recorded outside these files, in the review handoff, its evidence manifest and the PR description. Every command, flag, exit code and report field is proposed until the implementation PR is reviewed.

This is one of four frozen documents that the implementation PR adds unchanged: this scope, the [implementation map](canonical-content-updater-implementation-map.md) (algorithm and sources), the [test matrix](canonical-content-updater-test-matrix.md) (future acceptance cases) and the [decisions annex](canonical-content-updater-decisions.md) (review decisions, source facts, external prerequisites). All four are normative and are read together. Labels: **S** = source fact (`file:line` at the base tree in section 0), **E** = binding engineering decision of this scope, **O** = external or operational item that stays unverified (the tool refuses or prints it as a prerequisite), **D** = additive clarification of the approved design. Source references in these documents are repository-relative `path:line` citations.

## 0. Baselines

| Item | Value |
| --- | --- |
| Required implementation base tree | `c889275e8e42871cdf8374678a08271890414648` (section 8) |
| Planning base | PR #362 merge `6d93d6f9d316e576157babc590e2ce07e512b8b6`; the planning source was read at `0073ca4a6dbfed4bd20c6fc66e96732b1c46a58e`, which has the same tree, so every `file:line` below holds at the base |
| Approved design | [`docs/CANONICAL_CONTENT_UPDATE_DESIGN.md`](../CANONICAL_CONTENT_UPDATE_DESIGN.md), SHA256 `4b895c08f23e7727508ba5ed5b3dd470297b257035f5826957b97b385dadc018` |
| Frozen design scope | [`docs/reviews/canonical-content-update-design-scope.md`](canonical-content-update-design-scope.md), SHA256 `64cea12cb1e264ead06a6f3cc8c305dcbf7d48d85da312ffe0f5e064fc774268` |
| Implementation branch / PR | none exists here; none is invented |
| Recommended branch | `feature/canonical-content-updater` (collision-check against local and remote refs first; fall back to `feature/canonical-content-updater-b4`) |
| Recommended worktree | `/Users/gabrielpilichev/dzi-generator-content-updater` (must not exist) |

## 1. Goal and non-goals

Goal: one PR adding the design's tool `python3 -m src.update_canonical_content` and its temporary-database tests (design section 8, last paragraph). The tool plans, and with explicit authority applies, an **additive, insert-only** update of canonical content into an existing populated SQLite database, preserving every runtime and operational row.

Non-goals (all refused or external): executing against any real database; backup, restore or snapshot, deployment, activation, service stop or start; running migrations; content collection, import, export or refresh; changing canonical data; a file copier or asset verifier; value edits, deletions, id remapping; DEPLOYMENT procedure text; the DZI gate; PR #327's history witness; any legal or provider policy.

## 2. Allowed files (exact)

New files only, except the one inventory row in item 7:

1. `src/update_canonical_content.py`: the tool. Standard library only; reads no environment variable; target and inputs by explicit arguments.
2. `tests/test_update_canonical_content.py`: plan, identity, apply, preservation, report, CLI.
3. `tests/test_update_canonical_content_history.py`: history rules, references, file references, redaction.
4. `tests/test_update_canonical_content_concurrency.py`: locks, WAL and rollback-journal, restore lock, stale plan, injected failure, COMMIT outcome.
5. `tests/update_canonical_fixtures.py`: stdlib-only synthetic fixture builders shared by the three modules. They do not import `tests.support`, which imports `web.app` (tests/support.py:23-27). This deviates on purpose from the roadmap rule "use the `tests/support.py` isolation" (docs/REFACTOR_ACCOUNTS_ROADMAP.md:124): that guard wraps only `web.db.connect_db` (tests/runtime_db_guard.py:35-40), which the tool never uses. The replacement guard: every new test module patches `sqlite3.connect` with a spy that fails on any path equal to, or `os.path.samefile` with, `tests.runtime_db_guard.RUNTIME_DB` (that module imports only the standard library, tests/runtime_db_guard.py:10-12).
6. `docs/reviews/canonical-content-updater-scope.md`, `docs/reviews/canonical-content-updater-implementation-map.md`, `docs/reviews/canonical-content-updater-test-matrix.md` and `docs/reviews/canonical-content-updater-decisions.md`: the reviewed, frozen scope and its three normative annexes. The implementation PR adds them unchanged.
7. `docs/SCRIPT_INVENTORY.md`: exactly one added row in **Active / currently used** for `src/update_canonical_content.py` (required: the inventory covers every tracked `src/` file, docs/SCRIPT_INVENTORY.md:3). No other line of that file changes.

The three test modules may be merged if the author finds the split artificial; the number of test files is not a requirement, the coverage in the [test matrix](canonical-content-updater-test-matrix.md) is. No other file may change.

## 3. Preserved (byte-identical unless listed in section 2)

`src/canonical_*.py`, `src/build_database.py`, `src/stable_ids.py`, `src/manage_migrations.py`, `src/schema_introspection.py`, `src/backup_*.py`, `web/**` (including `web/migrations/**` and `web/services/lifecycle.py`), `data/canonical/**` (manifest SHA256 `82b27216a6d25e36bfe706e568a452e6b25441281a43b3cfa6d78d9278ab0715`), existing tests including `tests/support.py`, `docs/CANONICAL_CONTENT_UPDATE_DESIGN.md`, `docs/CANONICAL_DATA.md`, `docs/DEPLOYMENT.md`, `docs/LAUNCH_CHECKLIST.md`, `AGENTS.md` and the roadmap. No schema, data, gate, configuration, dependency or provider change. No `data/questions.db` read or build. No vault or generated artifact.

Default: **no existing code changes**. Existing helpers are reused only as mapped in [implementation map section 2](canonical-content-updater-implementation-map.md#2-reuse-map); anything unsafe for a populated live target is wrapped or reimplemented locally, not edited.

## 4. Proposed CLI

```
python3 -m src.update_canonical_content --db PATH --source DIR [--preimage DIR]
    [--apply --expect-plan-sha256 HEX64] [--busy-timeout-ms N] [--format text|json]
```

- `--db` and `--source` are required, with no default path and no mention of `data/questions.db`.
  - `--db` must exist and `os.lstat` of the given path must show a regular file. A symlinked final component, a directory or a missing path is refused (exit 1) before any SQLite call, and nothing is created. Symlinked parent directories are accepted: the tool opens the resolved path.
  - The tool never creates the **target** database. It writes no row in a dry run, never uses `immutable` and never copies a live database file. Validation deliberately creates disposable **reference** databases inside a fresh temporary directory (implementation map section 3); those are not the target and are removed at the end of the run.
  - Opening an existing WAL-mode target with `mode=rw` may, through SQLite itself, create or remove `-wal` and `-shm` sidecars and checkpoint at last close. That can rewrite main-file bytes without changing any row (design section 6). The tool gives no byte-level or sidecar guarantee for a WAL target, only a logical one: no row changes.
  - The restore lock is checked for both `lifecycle.restore_lock_path(<absolute given path>)` and `lifecycle.restore_lock_path(<resolved path>)`. `restore_lock_path` uses `os.path.abspath`, not `realpath` (web/services/lifecycle.py:88-94), and the existing tools differ on which path they pass (src/backup_application.py:255-264 the resolved one, src/manage_accounts.py:731-732 the given one). Either lock present refuses.
  - If `<resolved db path>-journal` exists (`os.path.lexists`) the tool refuses before opening the target (exit 1). It is a best-effort check with a time-of-check window, not a hotness detector; see implementation map section 4.
- Dry run is the default and writes no row. `--apply` requires **both** `--preimage` and `--expect-plan-sha256`; `--expect-plan-sha256` without `--apply` is a usage error.
- `--preimage DIR` is a canonical directory. The owner release record that ties it to the target, and the complete snapshot, are **external and unverified**; no flag asserts them (design sections 2 and 6). The report states this verbatim in every mode.
- A dry run without `--preimage` still reports what it can, prints no plan hash, marks the plan incomplete and exits 1 (apply impossible).
- `--busy-timeout-ms` default 5000, accepted range 100..60000. No `--force`, `--yes`, `--create`, `--migrate`, `--ignore-*`, no environment variables.
- Usage errors exit **1**, not argparse's default 2 (that code means busy or stale here): a small `ArgumentParser.error` override.

**First canonical write.** Exit codes 1, 3 and 5 turn on one boundary. The *first canonical write* is the first statement in the write transaction that is an `INSERT` into a canonical table or an `INSERT`/`UPDATE` of `sqlite_sequence`. A sequence-only plan crosses it exactly as a row insert does.

Exit codes (design section 2, plus the accepted additive code 5, decision D1):

| Code | Meaning |
| --- | --- |
| 0 | Dry run: an applicable plan, or no change. Apply: the write footprint is empty (no write lock is taken and nothing is committed), or COMMIT succeeded and the receipt was written. |
| 1 | Refused **before the first canonical write**: usage, inputs and preflight, incompatible target, journal present, restore lock (before reading, or under the write lock before the first canonical write), conflicts, unsafe path, unresolved reference, missing preimage on apply, internal error before the first canonical write. Database untouched. |
| 2 | Busy beyond the timeout (read, `BEGIN IMMEDIATE`, or a COMMIT busy or locked error that the probe classifies as not committed), or the plan hash differs (P1 or the replan under the lock). Database untouched. |
| 3 | **After the first canonical write** and before COMMIT, any failure: injected or SQLite error, constraint backstop, in-transaction validation or preservation mismatch, restore lock found before COMMIT, pinned-copy re-hash mismatch. Also a COMMIT error that the probe classifies as not committed (other than busy or locked). The transaction was not committed. |
| 4 | Committed: COMMIT succeeded and the receipt could not be written, **or** COMMIT raised and the probe found every write-footprint item at its expected post-state. The report says COMMITTED and never "rolled back"; it never claims that this run's COMMIT is the one that made the rows visible. |
| 5 | COMMIT raised and the probe could not establish either state. Outcome unknown. Do not retry this plan or token; the next step is a fresh dry run under separate authority. (D1, accepted: an additive clarification of design section 2; the design note is not edited.) |

## 5. Report contract (bounded, redacted)

`learnpilot-canonical-update-report/1`, text or JSON from one dict: mode; status (`no_change`, `planned`, `incomplete`, `refused`, `not_committed`, `applied`, `outcome_unknown`; mapping in implementation map section 12); exit code; run code (one run-level code of implementation map section 12, or null on exit 0); plan sha256 or null; tool and plan-format version; source and preimage identity (manifest sha256, per-file sha256 combined hash, migration level); repository migration list hash; target schema-identity hash, journal mode, applied-migration count; per canonical table counts by action (`identical`, `insert`, `target_only`, `conflict`); sequence items; conflict totals by fixed code and **at most 20** listed entries (table, key, code, differing column names only); rows added under existing parents per table (`under_existing_parent`); `questions_added_to_existing_topics`; unresolved pre-existing reference counts per column; non-canonical preservation (table count, per-table row counts, equal yes or no); file-reference listing; unverified external prerequisites.

Rules: never credentials, runtime rows, personal data or conflicting values; never `--db`, `--source` or `--preimage` paths, restore-lock path or exception text (exception class name and SQLite error name only); text escapes control characters and truncates identifiers at 80 characters; no timestamps (deterministic). File references: for every inserted file reference the line `file availability: not checked`; at most 20 listed per column (path after escaping, recorded sha256 as stored); unsafe paths are counted and named by table, key and code only, never echoed. The tool never stats, opens, copies or verifies a referenced file. The report is not a history witness (PR #327) and claims no backup, availability or approval.

## 6. Behavior summary

Details, sources and algorithm: [implementation map](canonical-content-updater-implementation-map.md).

1. Pin source, preimage and repository migrations by reading each file once into a private 0700 temporary directory and hashing at read time; later reads use only the copy.
2. Validate the source with `canonical_export.check_directory(copy, migrations_copy)` plus local checks (primary key and AUTOINCREMENT metadata derived from the pinned reference schema, not from the manifest). Validate the preimage with a lighter local check.
3. Preflight in order: arguments, pin and validate, restore lock (given and resolved path), regular-file `lstat`, journal check, open with `mode=rw`. Compatibility = applied migrations exactly the repository list, shapes equal the migrated reference, no trigger, view or unclassified table. Never migrates.
4. Plan from one consistent read. The plan hash binds source, preimage, migrations, schema, canonical content digests, sequences and the write footprint (candidate rows and planned `sqlite_sequence` changes, each with pre-state and expected post-state). Runtime digests are not in the hash; runtime gates run in both phases.
5. Apply: consistent read, hash compare, `BEGIN IMMEDIATE`, restore-lock recheck, schema recheck, full replan, hash compare, one insert-only transaction in manifest FK order, in-transaction preservation and result validation, COMMIT, receipt. An empty footprint never takes the write lock. Outcome classification as in section 4.
6. Row policy: design matrix (absent, identical, differing, target-only, key collision, id-only, composite) with the id high-water rule, the non-id duplicate guard restricted to **insert candidates**, and the history rules in implementation map section 7 (new owner required for children of existing questions, tasks and exams; direct and indirect asset links; topic-section assignments need a new topic). Never `DELETE`, `INSERT OR REPLACE`, `ON CONFLICT`, remap or force.
7. Preservation: every non-canonical table in `TABLE_CLASSIFICATION` except `sqlite_sequence` is counted and digested before the first canonical write and before COMMIT with a storage-class-exact multiset digest; `sqlite_sequence` per entry (raise-only for canonical entries).
8. Fail closed on any pre-existing foreign-key violation or unresolved FK-less reference, using a local extractor that covers what the readers coerce; no cleanup.
9. File-reference columns are validated as inert metadata for insert candidates only; unsafe values refuse the plan.

## 7. Acceptance and checks (plan only; none run by this scope)

Meaningful temporary-database cases: the [test matrix](canonical-content-updater-test-matrix.md): 74 numbered cases in 11 groups A-K plus the sub-cases 12a-12e, 41a and 67a-67c, with synthetic fixtures and one real-canonical no-op control.

Author's checks, on temporary paths only:

- `py_compile` of every new Python file, and a run of the three new test modules. The new modules are standard-library only, so a failure to run them is a defect of the work, not an interpreter limit; they never need the application's third-party dependencies.
- Existing canonical regressions: `tests.test_canonical_content`, `tests.test_build_database`, `tests.test_importer_stable_ids`, `tests.test_canonical_question_integrity` and `tests.test_recovery_production` (its source scan reads every `src/*.py`, tests/test_recovery_production.py:104-110, so the new module must not contain `DZI_RECOVER` or `RECOVERY_ENABLED|ACTIVATION`). These import `tests.support` and therefore `web.app`, so they need the complete interpreter (Flask, segno and the rest of the application's dependencies). Interpreter capability has not been checked by this scope; a stub or an incomplete interpreter does not count.
- `python3 -m src.canonical_export --check` (tracked data only); the `AGENTS.md` pre-commit set that needs no database; `git status --short`, `git diff --stat`.
- The roadmap's full-suite-once rule (docs/REFACTOR_ACCOUNTS_ROADMAP.md:128) in a complete interpreter, never with a stubbed dependency.

The tool and tests must not require a newer Python than the repository modules they import already need: the repository states no minimum (production runs 3.12, docs/DEPLOYMENT.md:2224), `sqlite3.Error.sqlite_errorcode` exists from 3.11 only (the repository already reads it with `getattr`, web/services/oidc_consumption.py:209) and the tool must keep a message-based fallback that the tests exercise. This scope does not raise any project minimum. Building `data/questions.db` is not required and not part of this scope.

## 8. Exact-baseline condition

The implementation base's tree must equal `c889275e8e42871cdf8374678a08271890414648` (that of `6d93d6f`, the PR #362 merge, and of the planning HEAD `0073ca4`). Otherwise stop and re-plan. Facts that rest on that tree: the migration list ends at `025_password_recoveries.sql` (26 files); manifest SHA256 `82b27216a6d25e36bfe706e568a452e6b25441281a43b3cfa6d78d9278ab0715`; the non-id duplicate pairs 371/393 and 659/681 in `questions`; `asset_links.owner_type` values `question` 37, `exam_task` 37, `exam` 7. A new migration or canonical refresh before adoption invalidates the counts, not the design.

## 9. Open items

Owner and operational items stay open and are handled by safe refusal or an explicit external prerequisite ([decisions annex section 5](canonical-content-updater-decisions.md#5-owner-and-operational-unknowns-stay-open)). The review decisions D1, E5, E6, E7, E8, E17 and the closed engineering choices are recorded in the [decisions annex section 2a](canonical-content-updater-decisions.md#2a-review-decisions). No genuine design conflict exists, so the approved design note is not edited.
