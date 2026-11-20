# C2.1 practice runtime storage: scope

Status: implementation boundary for the first C2 slice, after merged C1 PR
#364. This PR adds storage and its tooling integration only. No route, UI,
service start/submit flow, progress query or activation is introduced.

## Baseline and authority

- Base: `c9251645ee1c1199431a99c7e553f9db5077fabd` (merged PR #364).
- Branch: `feature/practice-runtime-storage`, dedicated worktree.
- Follow `AGENTS.md`, the approved 2026-11-18 roadmap update and
  `docs/STUDENT_PRACTICE_DESIGN.md`. Independent Opus/High approved C1 at
  `5a57aa81b2282cd417929220f89b7e439d354270`; the merge has its exact tree.
- Carry C1 review N5 and N7 into this slice: explicit foreign-key rules and
  both runtime question-reference validators. C2 service and HTTP/UI slices
  follow separately; C3/C4/C5 remain deferred.

## Allowed implementation files

- Add `web/migrations/026_practice_runtime.sql`.
- Update `src/canonical_tables.py`, `src/canonical_parity.py` and
  `src/update_canonical_content.py` only for the two new runtime tables and
  their reference/preservation integration.
- Update `data/canonical/manifest.json` only for the new migration metadata
  and runtime classification/exclusions. All content rows, table metadata,
  hashes, identities and canonical sequences stay unchanged.
- Add `tests/test_practice_runtime_migration_sql.py`.
- Update `tests/test_canonical_content.py`, `tests/test_schema_baseline.py`,
  `tests/test_build_database.py`, `tests/test_update_canonical_content.py`,
  `tests/test_update_canonical_content_history.py`,
  `tests/test_update_canonical_content_concurrency.py` and
  `tests/update_canonical_fixtures.py` only for affected expectations,
  preservation/reference coverage and fixture migration numbering.
- This scope, the C1 note and every other source/document stay unchanged.
  If a necessary change falls outside this list, report the concrete
  dependency to the coordinator before editing it.

## Schema contract

Use an additive migration through the existing runner. Do not rebuild or
alter an existing table, seed a practice row or backfill legacy attempts.

`practice_sessions` stores the C1 fields: an AUTOINCREMENT integer primary
key; account `user_id`; `section_id`; nullable `topic_id`; seed;
`question_ids_json`; `started_at`; nullable `submitted_at` and `abandoned_at`;
nullable `score_correct` and `score_total`. Account ownership is NOT NULL and
references `users(id) ON DELETE RESTRICT`. Section and topic reference their
canonical curriculum tables with RESTRICT. IDs must be positive (nullable
topic excepted), making the open-key sentinel 0 unambiguous. Required seed,
plan and start fields cannot be NULL. Store no name, question text or blobs.

`practice_answers` stores `session_id`, `question_id`, nullable
`chosen_letter` and a 0/1 `is_correct`. Enforce uniqueness per session and
question. Reference the practice session and canonical question with
RESTRICT; no implicit cleanup, deletion or retention policy. Do not invent
an A–D-only option-letter contract: reuse the existing letter representation.

Enforce the small row-local invariants in the schema: a session cannot be
both submitted and abandoned; unsubmitted sessions have no score; submitted
scores are present, nonnegative, correct <= total, and total > 0. Answers
have a non-NULL Boolean verdict and positive referenced IDs. Use ordinary
SQLite constraints compatible with the existing stack, without new features
or dependencies. JSON shape, topic membership, question eligibility, access,
state transitions and scoring are later service responsibilities; this PR
must not claim that table constraints alone implement them.

The partial unique index is on account, section and the NULL-safe topic key
while neither terminal timestamp is set. It must reject two open section
sessions (NULL topic) and two open sessions in the same topic; allow another
account/scope and allow a fresh open session after submission or abandonment.
Add only indexes justified by these invariants or the planned owner/scope
lookup. No trigger-based application behavior, timer or retention mechanism.

## Canonical and updater integration

Classify both tables RUNTIME, never canonical. Fresh builds contain no
practice rows. Export must exclude them; all existing canonical JSONL bytes
remain identical. Refresh manifest metadata from a disposable database only.

Add `practice_sessions.question_ids_json` and
`practice_answers.question_id` to both `canonical_parity` runtime reference
validation and the updater's fail-closed reference gate. Preserve the
updater's existing statuses, parsing/type rules, pinned-input validation,
transaction/rollback/commit-outcome handling and old-table checks. Avoid a
general refactor. Confirm valid practice rows and their sequence are
byte/logically unchanged by a genuine insert-only update and by a no-op;
malformed or unresolved practice references must refuse before writes.

Existing tests use synthetic "026" future migrations. Move only those
affected markers to a truly later number as needed, preserving what each
test proves. Never weaken a failing assertion or change content counts to
hide an integration error.

## Verification and handoff

All databases are disposable scratch or repository test fixtures. Test an
upgrade from populated migration 025: existing rows/IDs/values and prior
schema objects remain unchanged, except the expected migration ledger row;
new practice tables start empty. Test idempotent migration, constraints,
NULL/topic open uniqueness and fresh-build/export parity. Run foreign-key
and integrity checks on scratch databases. Test both reference validators
and updater preservation/refusal with practice rows, including meaningful
negative controls. Do not claim service concurrency or browser behavior is
tested by these schema checks.

Run the new migration suite and affected schema, builder, canonical and
three updater suites; also the repo's auth-guard/attempt-render regressions.
Compile changed Python to scratch. Run one full unittest suite after final
code/tests, as the roadmap requires; repeat only for a justified new failure
or correction. The known missing official PDF may keep the scratch DZI audit
at its base result; disclose rather than fabricate a source. Use real local
packages, no stubs; validate the available interpreter before tests.

Leave edits unstaged/uncommitted. Report exact branch/HEAD, files, final
hashes, tests and limits separately from independent review. Provide a
staging list, multi-paragraph commit/PR text and stable evidence manifest.
Coordinator commits/pushes in a new Terminal; fresh Opus/High reviews the
exact pushed SHA before merge.

## Exclusions

No real runtime database or sidecars, live import/migration, provider or
credentials, network research, deployment/VPS/backup/restore, generated
vault, servers/tunnels or gate activation. Do not read/copy/hash
`data/questions.db`. Do not change auth, scoring, teacher quota/lifecycle,
retention, registration, recovery, content, route contracts, templates or
configuration. No practice routes, services or user-facing activation yet.
