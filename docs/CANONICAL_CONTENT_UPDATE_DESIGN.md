# Canonical content update: B4 design note

Status: proposal ([scope](reviews/canonical-content-update-design-scope.md)); no updater exists and every command, flag and exit code is proposed. The deployed commit, target content, assets and schema level are unknown, not inferred. **Observed** = source today; **Recommend** = proposed v1; **Open** = undecided.

## 1. Purpose and reuse

**Observed.** [`build_database`](../src/build_database.py#L143) makes a new file and refuses an existing destination. [CANONICAL_DATA](CANONICAL_DATA.md) says an existing database is migrated, never replaced. An update keeps accounts, assignments and attempts; replacement discards them.

Reusable: [`check_directory`](../src/canonical_export.py#L151), normalization ([`serialize_row`](../src/canonical_tables.py#L191)), raise-only [`apply_sequence`](../src/canonical_load.py#L53), [`runtime_reference_problems`](../src/canonical_parity.py#L155).

Limits:
- [`insert_rows`](../src/canonical_load.py#L32) skips rows with equal Python tuples (`1 == 1.0`), yet the canonical hash keeps them apart; it aborts on the first difference.
- [`load_canonical`](../src/canonical_load.py#L77) uses `sqlite3.connect`, which creates a missing file; [`open_snapshot`](../src/canonical_export.py#L41) is `immutable=1`, which ignores an active WAL.
- [`compare_to_canonical`](../src/canonical_parity.py#L71) requires equal counts, hashes, sequences and empty runtime/operational tables (except `lifecycle_clock`, `schema_migrations`, `sqlite_sequence`): it rejects any populated target.
- [`TABLE_CLASSIFICATION`](../src/canonical_tables.py#L35) governs; CANONICAL_DATA's is abbreviated.

## 2. Interface and trust

**Recommend** `python3 -m src.update_canonical_content`:
- Explicit `--db` (no default or implicit `data/questions.db`; a missing target is refused) and `--source`.
- Dry run by default. Writing needs separate `--apply`, `--preimage` and `--expect-plan-sha256` of the reviewed plan.
- Preflight before reading the target: format, version, manifest and file hashes, classification, source migration level, no restore lock.
- Trusted preimage: the canonical directory a dated owner release record ties to this target. A repository SHA names files, not database content, and the tool proves no provenance. Without one apply is refused; dry run still reports.
- Report: counts per table and action, at most 20 conflicts, plan sha256. Exit 0 success or no change; 1 refused before writing; 2 busy or stale; 3 failed, rolled back; 4 committed, report failed.

## 3. Policy matrix

"Identical" means equal normalized lines: `1` and `1.0` differ.

- Absent in target and preimage, id above target maximum and sequence: insert with explicit id.
- Absent in target, present in preimage: refuse (lost or deleted).
- Id at or below target sequence, row absent: refuse (possible reuse).
- Same primary key: identical is a no-op; any difference (timestamps, approval flags, text) is refused.
- Natural key or non-id content present under another id: refuse (duplicate).
- Target-only row: keep, report; refuse only on collision. Preimage row missing from source: refuse.

Lacking a usable `NATURAL_KEYS` key, `exams`, `official_exam_sources`, `curriculum_modules`, `question_topic_assignments` and questions without `source_number` rely on the non-id content check against duplicates. Five tables have no `id`: `assessment_event_sections`, `topic_classes`, `topic_prerequisites`, `exam_task_questions`, `practical_tasks`. They use their full declared primary key for matching, collision and absence; no surrogate id or sequence; id rules apply only where an `id` exists. Never DELETE, `INSERT OR REPLACE`, `ON CONFLICT DO UPDATE`, delete/reinsert, remapping, id reuse or force.

**Observed.** Attempts keep ids, seed, chosen letter, an `is_correct` snapshot and scores; prompts, options and keys are read from current rows; option order is seed-shuffled over them ([`quiz.py`](../web/services/quiz.py#L1242), [lifecycle §2.8](ASSIGNMENT_LIFECYCLE_DESIGN.md)). An edit or added option changes past results though ids resolve and `foreign_key_check` stays clean. Result pages render current `asset_links` owned by the question or a linked task ([lookup](../web/services/question_visuals.py#L36), [`quiz_load_result_questions`](../web/services/quiz.py#L1920), [template](../web/templates/quiz_result.html#L110)). Deleting a question cascades to text answers ([005](../web/migrations/005_quiz_text_answers.sql#L25)), a task to submissions ([008](../web/migrations/008_practical_submissions.sql#L13)).

**Recommend.** v1 is additive: value changes and removals are unsupported. Rows added under an existing `questions` or `exam_tasks` row (options, subquestions, task links, practical rows) are refused unless the owner is new in the plan. New FK-less `asset_links` are refused if they reach an existing question or task, directly or via any task link, including one added in the plan. Unrecognized divergence is refused and listed; edits, removals and id gaps need separately reviewed reconciliation. No production database is promised updatable. **Open:** an apply receipt is not an accepted history witness; PR #327 decides.

## 4. Preservation

**Recommend.** Writes touch canonical tables and canonical `sqlite_sequence` entries only. In the write transaction, count and digest every non-canonical table except `sqlite_sequence` in [`TABLE_CLASSIFICATION`](../src/canonical_tables.py#L35) (accounts, TOTP, identities, profiles, registrations, recoveries, ledgers, attempts, operational) before the first write and COMMIT; any difference rolls back. `sqlite_sequence` is checked per entry instead: non-canonical unchanged, canonical at most raised to the manifest value, never lowered. Target-only rows and FK-less references (`questions.topic_id`, `asset_links.owner_id`, question ids in attempts, plans, `generated_exams`) stay.

Validate the resulting database, not fresh-build counts: source rows present and identical; other canonical rows equal to pre-state; sequences at least max(id, manifest); no new foreign-key violations; `quick_check`; embedded and runtime references resolve.

## 5. Consistency

**Recommend.**
- The plan binds source and preimage hashes, migrations, schema shape, canonical digests and sequences; source files are read once.
- Apply uses a bounded busy timeout and `BEGIN IMMEDIATE`, then rechecks restore lock, schema and migrations and recomputes the plan; a differing plan hash exits 2, writing nothing. One transaction, FK order; any error rolls back.
- Compatible schema: migrations equal the repository's, shapes equal the migrated reference, and no trigger exists. Pending migrations are refused, never applied ([DEPLOYMENT §9](DEPLOYMENT.md)).
- After commit the old plan is stale: repeating it exits 2; a fresh reviewed plan finds no change: exit 0.
- Rollback undoes one uncommitted transaction; a snapshot restore ([§17](DEPLOYMENT.md)) discards later runtime writes and is not this tool's remedy. The receipt is written after COMMIT; its failure is exit 4, "committed", never "rolled back".

## 6. SQLite, backup, authority

**Recommend.**
- Dry run: `mode=rw` with `query_only` as [`backup_sqlite`](../src/backup_sqlite.py#L139), reading in one transaction (one WAL snapshot). It writes no rows but may create `-wal`/`-shm` and delay checkpoints. Never `immutable`, never a live-file copy.
- Restore lock ([`lifecycle.py`](../web/services/lifecycle.py#L88), DEPLOYMENT §17): refuse before reading and under the write lock, like [`backup_application`](../src/backup_application.py#L264) and [`manage_accounts`](../src/manage_accounts.py#L732).
- Prerequisite, unverified: a complete snapshot (§16, §20 step 3). Snapshots scrub pending registrations and recoveries and carry old credentials: not an exact runtime baseline, not a credential-neutral restore (§37).
- Execution, backup/restore, deployment, activation and content release stay separately authorized. A valid plan or apply satisfies neither review D6/D7 ([roadmap](REFACTOR_ACCOUNTS_ROADMAP.md#current-roadmap-update--2026-11-18)) nor the DZI gate.

## 7. Assets and reports

- File references: `assets.local_path`, `practical_task_resources.resource_path`, `official_exam_sources.local_path`, `exams.source_file`, `questions.image_path`, `curriculum_topics.note_path`, `moc_filename`. Their rows may record hash/size; `asset_links` only links an asset to an owner. Applying rows creates no files; reference files are outside snapshots (§16). v1 copies and checks nothing: for every inserted file reference the report says "file availability: not checked" and lists paths and recorded hashes; absolute or `..` paths are refused.
- Reports name tables, keys, columns and counts; never credentials, runtime rows, personal data or conflicting values. No content generation, import or canonical refresh.

## 8. Acceptance plan and follow-up

Future temporary-database tests (none run here): first apply, stale retry, no-op repeat; composite/no-id primary key; populated state preserved; bad manifest/hash/format/schema, missing target, pending migration, trigger; restore lock before reading and under the write lock; primary-key, natural-key, NULL/id-only, value (`1` vs `1.0`, timestamp, flag) and sequence conflicts; target-only rows; attempts whose question gains an option or (directly or via a new task) an asset link; unsafe file paths; dry run beside a WAL writer; concurrent writer/stale plan/busy; injected rollback; failure after COMMIT.

Follow-up: one PR adding `src/update_canonical_content.py` and tests; the DEPLOYMENT procedure is separate. **Open owner decisions:** preimage record form; edits of unreferenced or metadata rows; history witness (#327); apply with the service up; asset verifier.
