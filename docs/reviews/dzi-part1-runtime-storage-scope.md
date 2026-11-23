# C3.1 DZI Part 1 runtime storage scope

Base 0fdf87d87269808e8eb9f8141603d5e16267a703 (merged #375). Branch feature/dzi-part1-runtime-storage
in a dedicated worktree. Follow the reviewed storage proposal in
docs/DZI_PART1_SIMULATION_DESIGN.md §§5, 7, 9–11. This slice adds only
runtime storage and its canonical tooling integration.

## Allowed paths

- New web/migrations/027_dzi_part1_runtime.sql.
- src/canonical_tables.py, src/canonical_parity.py,
  src/update_canonical_content.py and data/canonical/manifest.json for
  the new runtime tables and references.
- New tests/test_dzi_part1_runtime_migration_sql.py.
- Existing tests/test_schema_baseline.py, tests/test_build_database.py,
  tests/test_canonical_content.py, tests/test_database_test_support.py,
  tests/test_update_canonical_content.py,
  tests/test_update_canonical_content_history.py,
  tests/test_update_canonical_content_concurrency.py and
  tests/update_canonical_fixtures.py only for directly affected fixture
  levels, preservation, reference and build assertions.
- Existing tests/test_practice_runtime_migration_sql.py only for directly
  affected 026 fixtures: the upgrade test applies migrations through 026 in
  its own scratch directory, and the manifest level is the latest actual
  migration file.

## Contract

Add dzi_part1_attempts and dzi_part1_answers following the reviewed
field and invariant descriptions. A populated 026 database upgrades
without table rebuilds, seed rows or backfill. New rows are account
owned, with positive IDs and explicit RESTRICT foreign keys to
users, the blueprint, attempt and question. Persist the planned
maximum, duration, start, deadline, submit cutoff, flat question-ID
plan and separate slot-format JSON. Keep all terminal timestamps
mutually exclusive and score fields coupled to submitted state.
Enforce the reviewed fixed-width lifecycle timestamp shape and
the start/deadline/cutoff and submitted/expired orderings. Do not
order abandoned_at against started_at: recovery may abandon a row
whose start stamp is later than verified time.
One open row per account under a partial unique index. A new open
can follow any terminal state. The history index is for owned reads.

Answer storage pins slot/subquestion keys, kind-consistent fields,
whole possible and awarded points, binary verdict and grader version.
The reviewed row-local unique key prevents duplicate question parts.
Only the later service enforces exact plan membership, one question
per slot, answer length limits and transaction transitions. No
raw-answer length CHECK, storage for accepted answers, lease, timer,
clock coupling, grading, triggers, new route, UI or feature gate.
Clock recovery integration and open-row abandonment land with or
before the later first C3 writer. No recovery or inspect change here.

## Canonical and updater compatibility

Classify both tables RUNTIME, excluded from canonical export and
empty on fresh build. Manifest changes are limited to migration
metadata and classifications. Preserve every canonical JSONL byte.
Register question_ids_json and answers.question_id in both runtime
reference validators. slot_format_json holds no question IDs and
must stay out of both. Keep existing parser/type, fail-closed refusal,
pinned inputs and update transaction behavior. Shift synthetic
future fixture markers only where 027 actually conflicts.

## Verification

Use only disposable migration/canonical fixtures. Test upgrading a
populated 026 database, repeated migration with the existing runner, RESTRICT and relevant negative
constraints, open uniqueness, all terminal states, fresh build and
export equality, FK and integrity. Verify genuine insert-only update
and no-op preserve runtime rows and sequences, and unresolved or
malformed JSON references refuse before mutation per existing parser.
Verify slot-format integer values do not enter the reference check.
Use affected migration, canonical, updater and app suites. Since the
migration changes the shared fixture schema, run one standard
site-enabled full unittest on the final frozen bytes if practical.
Record exact commands, modes, UTC times and actual exit codes.

Leave only intended edits unstaged. Report handoff, exact staged
paths, hashes and stable evidence plus commit/PR text. Fresh Opus /
High reviews the exact pushed SHA. Later C3 scope handles pools,
timing/clock, requests, scoring and UI. No runtime real database or
sidecar access, live migration/import, credentials, provider,
server/tunnel, generated vault, deployment or activation.
