# Decisions and prerequisites for the canonical content updater

Status: scope for the B4 updater implementation PR. It is adopted only with a READY independent review of these exact bytes. The review verdict and the SHA256 of each file are recorded outside these files, in the review handoff, its evidence manifest and the PR description. Every command, flag, exit code and report field is proposed until the implementation PR is reviewed.

Normative annex of the [scope](canonical-content-updater-scope.md); the algorithm is in the [implementation map](canonical-content-updater-implementation-map.md) and the acceptance cases in the [test matrix](canonical-content-updater-test-matrix.md). Labels: **S** source fact (`file:line` at the base tree of scope section 0), **E** binding engineering decision (the implementer follows it; nothing is left to choose), **O** open owner or operational item (the tool either safely refuses the case or prints an external prerequisite; nothing is settled), **D** additive clarification of the approved design.

## 1. Design result

Reviewed: NEEDS CORRECTIONS at `8bcaacdb…` (the earlier draft of the scope); the corrections are applied here. No genuine conflict with the approved design was found. One additive clarification (D1) is recorded as accepted; no decision below needs the design note edited.

## 2. Design gap

**D1 — COMMIT-outcome-uncertain has no exit code. Accepted.** Design section 2 defines 3 = "failed, rolled back" and 4 = "committed, report failed"; section 5 forbids reporting a post-commit failure as rollback. A `COMMIT` that raises proves neither (SQLite can report an I/O or busy error with the transaction still open, or after the commit became durable). The narrowest fail-closed boundary is **exit 5 = COMMIT outcome unknown, do not retry**, used only on the COMMIT-error path, when the probe cannot confirm either state (implementation map section 9 step 4). Rejected alternatives: reuse 3 (claims a rollback it cannot prove) or 4 (claims a commit it cannot prove).

## 2a. Review decisions

Decisions of the independent review, adopted by this scope. Wording is the review's, with line references into the earlier draft replaced by the tracked annex sections.

| Item | Decision | Source / reason |
| --- | --- | --- |
| **D1** exit 5 for an uncertain COMMIT | **ACCEPT.** This is an additive clarification of design section 2 and does not amend it. | Design L24 defines 0–4. Design L56 forbids reporting a commit as a rollback, and the scope forbids the reverse. A COMMIT that raises proves neither outcome. Reusing 3 or 4 would claim something that cannot be proven. Exit 5 is used only on the COMMIT-error path. The boundary is the single "first canonical write" rule and the probe of implementation map section 9 step 4. |
| **E5** `topic_section_assignments` | **NARROW.** A new TSA row is allowed only when its **topic is a candidate**. A new topic with an existing section is permitted and reported. An existing topic with any section (existing or new) is refused, as `history_child_of_existing`. | An earlier draft reasoned that a new row "changes an existing section's pool", which equally condemns new questions in existing topics, and those stay permitted. The real effect on existing rows is through ownership. `topic_ownership` reads **every** TSA membership of a topic (src/subject_content.py:77-111, 200-222). A new section with a different subject, or with `is_primary` mismatched, makes an existing topic `conflict`. That removes its existing questions from scope-checked pools and from plan validation (src/subject_assessment.py:66-78, 80-84; web/services/quiz.py:1234-1235). Pool growth from new questions is permitted and reported. Started attempts keep their stored ids (quiz.py:2152-2162, 2406-2409). Implementation map section 7. |
| **E6** `question_topic_assignments` under an existing question | **ACCEPT** as the design's literal rule, not an extension. Not open to later relaxation in v1. | Design L41 refuses rows "added under an existing `questions` … row". QTA has an enforced FK to `questions` (web/migrations/000_baseline.sql:296-299). No web reader was found (grep over `web/`). That makes refusing safe, not unnecessary. |
| **E7** additions under existing parents | **NARROW.** New `exam_tasks` under an **existing** `exams` row are refused (`history_child_of_existing`). Topics, sections, sources, blueprint slots and exam-owned `asset_links` under existing parents stay permitted and are counted; the permitted tables are enumerated in implementation map section 7. | An earlier draft claimed "no reader that alters stored attempts/results was found". That claim is false for tasks: an existing practical attempt's page lists the exam's tasks **at render time**, via `fetch_practical_attempt_page` → `fetch_practical_tasks`, which selects `exam_tasks` by `exam_id`, `task_number BETWEEN 26 AND 28` and `task_kind LIKE 'practical_%'` (web/services/practical.py:127-170, 247-280). A new task appears on old attempts and accepts uploads there. The permitted rows rest on a qualified static search at the base tree, not a proof. |
| **E17** `docs/SCRIPT_INVENTORY.md` row | **ACCEPT, required** (exactly one row, no other inventory edit). | The inventory says it covers tracked files under `src/` (docs/SCRIPT_INVENTORY.md:3). A new module without a row would make that false. This is not DEPLOYMENT procedure text, which stays separate (design L75). No test enforces the inventory. |
| Duplicate-guard timestamps | **Literal: every non-id column, timestamps included.** No option remains. | Design L34. |
| E8 fail-closed pre-state | **ACCEPT,** with the extractor of implementation map section 7. | Post-validation already requires that "embedded and runtime references resolve" (design L47), so a dangling pre-existing reference could never pass anyway. Refusing early is consistent. |
| Stdlib test isolation instead of `tests/support.py` | **ACCEPT, recorded as an explicit deviation** with a replacement guard (scope section 2 item 5). | tests/support.py:23-27 imports `web.app`. The roadmap rule at docs/REFACTOR_ACCOUNTS_ROADMAP.md:124 names that isolation, and its guard only wraps `web.db.connect_db`, which the tool never uses. |

No missing essential owner choice remains. O1–O9 (section 5) stay external.

## 2b. Further closed choices

These were open or inconsistent in the earlier draft and are now settled; each is stated once in the document named.

| Choice | Closed as | Where |
| --- | --- | --- |
| Exit-code boundary | One boundary: refused before the first canonical write (row `INSERT` into a canonical table, or `INSERT`/`UPDATE` of `sqlite_sequence`) is 1 or 2; any failure after it and before COMMIT is 3; a COMMIT error is 2, 3, 4 or 5 by the probe; a receipt failure after a successful COMMIT is 4 | scope section 4; map sections 9 and 10 |
| Write footprint | Candidate rows plus planned canonical `sqlite_sequence` changes, each with pre-state and expected post-state, hashed in P1 and P2; an empty footprint is "no change" with no write lock and no COMMIT; a sequence-only plan takes the full write path | map section 5; matrix 12, 12a-12e |
| COMMIT-error probe | Through the injectable `connect`; `ROLLBACK` once, `in_transaction`, journal gate, fresh read-only probe; all items at post-state is 4 (visible, never "durable" or "this run committed"), all at pre-state is 3 or 2, anything else 5 | map section 9 step 4; matrix 65-67c |
| Target contract | Never creates the **target** database; refuses a symlinked final component, a directory or a missing path; restore lock checked for the given absolute and the resolved path; refuses any `<db>-journal`; no byte or sidecar guarantee for a WAL target, logical guarantee only; validation creates disposable **reference** databases in a temporary directory | scope section 4; map section 4 |
| Legacy owner type `questions` | Refused at plan time (`asset_link_legacy_owner_type`); a target already holding such a link is refused by the pre-state gate until a separate reconciliation review | map section 7; matrix 38, 42 |
| Reference extractor | Local, fail closed, covering what the readers coerce | map section 7; matrix 42 |
| Key and AUTOINCREMENT metadata | Derived from, and validated against, the pinned reference schema for source and preimage; manifest declarations never bypass the high-water rule | map sections 3 and 6; matrix 23, 24 |
| Base condition | Exact tree equality only | scope section 8 |
| Reference-database connects | Allowed under a `tempfile` directory created during the run | map section 11; matrix 27, 72 |
| Test isolation | Standard-library fixtures and a replacement real-runtime-database guard; full-interpreter regressions run and reported separately | scope sections 2 and 7; matrix 73 |

### Optional refinements

| Refinement | Disposition | Where |
| --- | --- | --- |
| N1 authorizer allowlists and the transition before `BEGIN IMMEDIATE` | Accepted | map section 4; matrix 34 |
| N2 sorted row digests, storage-class-exact bytes | Accepted | map section 9 step 1; matrix 31 |
| N3 static guards match SQL forms | Accepted | map section 11; matrix 71 |
| N4 pinned supported partial-index forms | Accepted | map section 6; matrix 17 |
| N5 dry-run affinity prediction | **Not adopted.** The reviewed post-insert identity check is the safety boundary; a prediction would add an algorithm and a prototype to a scope that must stay implementable without one. The known limitation is recorded. | map section 13; matrix 20 |
| N6 preflight order | Accepted | map section 4 |
| N7 "designed for" concurrency, unmeasured writer delay | Accepted | map section 4; O4 below |
| N8 missing sequence as 0, filenames not `applied_at`, non-id guard only for primary key `["id"]` | Accepted | map sections 5 and 6; matrix 10, 18, 26 |
| N9 Python error-attribute fallback | Accepted in its narrowest form: a message fallback, no raised project minimum, no interpreter capability assumed | scope section 7; map section 4; matrix 55, 65 |
| N10 fresh unique temporary paths, `reference_columns` cache caveat | Accepted | map sections 2 and 3; matrix 27 |

## 3. Source facts that shape the scope

- S1 `canonical_load.load_canonical` creates a missing database (`sqlite3.connect`, src/canonical_load.py:77), skips rows equal as Python tuples so `1 == 1.0` (src/canonical_load.py:44), aborts on the first difference, assumes an empty target. `insert_rows` is not usable for a populated target.
- S2 `canonical_export.open_snapshot` is `immutable=1` (src/canonical_export.py:43): wrong for a live WAL database.
- S3 `parity.compare_to_canonical` rejects any non-empty runtime or operational table except the migration-seeded `lifecycle_clock` (src/canonical_parity.py:95-103); `build_database.logical_fingerprint` only counts non-canonical rows (src/build_database.py:122-124): neither is a preservation check.
- S4 `check_directory` asserts the repository's *current* migration list and classification (src/canonical_export.py:164-173): unusable for an older preimage; it also builds a private reference database (src/canonical_export.py:52-60), which is fine for source validation. It compares only `columns` with the reference (src/canonical_export.py:183-184) and takes `primary_key` and `autoincrement` from the manifest (src/canonical_export.py:198-208).
- S5 `export.schema_problems` compares only the last applied migration (src/canonical_export.py:77); `schema_migrations` stores no checksum (src/manage_migrations.py:193-195). Applied-file identity is provable only by filenames plus schema shape.
- S6 `parity._reference_shapes` is hard-wired to `ct.MIGRATIONS_DIR` (src/canonical_parity.py:35-43).
- S7 `ct.serialize_row` raises on blobs and NaN (src/canonical_tables.py:196-198); runtime columns are plain SQLite columns that may hold any storage class.
- S8 Manifest facts (static read of `data/canonical/manifest.json`, schema level `025_password_recoveries.sql`, 26 migrations): 27 canonical tables; five without `id` (the design's list); `practical_tasks` has a single INTEGER primary key `task_id` but is not AUTOINCREMENT, with `max_id` 196; id gaps exist (`exams` 11 rows, max id 12; `questions` 1709 rows, max 1714; `multiple_choice_options` 6399 rows, max 6795).
- S9 Static scan of canonical data: non-id duplicate pairs only in `questions` (371/393, 659/681); 579 questions have NULL `source_number`; `asset_links.owner_type` takes `question` (37), `exam_task` (37), `exam` (7); the readers also accept `questions` (web/services/question_visuals.py:56) which `parity.OWNER_TABLES` does not (src/canonical_parity.py:110, 122-125).
- S10 Results use current rows: option order from a seeded shuffle of current options (web/services/quiz.py:1242-1245), correct text from current `is_correct` (web/services/quiz.py:1920-1948). Plan-less assignments choose questions at attempt start from `topic_section_assignments` (web/services/quiz.py:2160-2162, 1093, 1221-1239); `question_topic_assignments` is read only by offline `src` tools (`audit_dzi_training_pool.py`, `generate_dzi_training_set.py`), not by `web/`. An existing practical attempt lists its exam's tasks at render time (web/services/practical.py:127-170, 247-280). A topic's ownership comes from all its section memberships (src/subject_content.py:77-111, 200-222).
- S11 FK-less canonical ids in runtime rows: `quiz_answers.question_id` (web/migrations/001_quiz_tables.sql:28), JSON ids in attempts, plans and generated exams (src/canonical_parity.py:170-180). The readers coerce each stored id with `int(qid)` (web/services/quiz.py:1410-1421, 1496-1516, 1566-1572), so a numeric string or a float attaches like an integer. Enforced FKs: `quiz_text_answers` (web/migrations/005_quiz_text_answers.sql:25), `practical_submissions` (web/migrations/008_practical_submissions.sql:13), `quiz_assignments.section_id` (web/migrations/001_quiz_tables.sql).
- S12 Existing file-reference conventions: `exams.source_file` has three trailing-slash directory values; practical resource paths use `archive.zip::member`, include Cyrillic and spaces, all NFC; resolver rules in src/practical_resource_resolution.py:49-61; web checks file existence at runtime (web/services/quiz.py:985).
- S13 Restore lock: a name `restore-in-progress` next to the database (file or directory; `lexists`), checked before reading and again after `BEGIN IMMEDIATE` by existing tools (web/services/lifecycle.py:85-94; src/manage_accounts.py:732, 748-753; src/backup_application.py:264, 324). `restore_lock_path` uses `abspath`, not `realpath` (lifecycle.py:88-94); `backup_application` passes the resolved path (src/backup_application.py:255-264), `manage_accounts` the given one (src/manage_accounts.py:731-732).
- S14 argparse errors exit 2 by default, which collides with the design's exit 2 (busy or stale): the module needs its own usage-error mapping to 1.
- S15 `tests/support.py` imports `web.app` (tests/support.py:23-27); tests for a stdlib tool should not.
- S16 `tests/test_recovery_production.py:104-110` scans every `src/*.py` for `DZI_RECOVER` and `RECOVERY_ENABLED|ACTIVATION`.
- S17 `canonical_export.reference_columns` connects inside a `learnpilot-reference-*` temporary directory under the system temporary directory (src/canonical_export.py:54-60) and is `lru_cache`d by the directory string (src/canonical_export.py:51).
- S18 `apply_sequence` writes `sqlite_sequence` by `INSERT` when the table has no entry and by `UPDATE` otherwise, and never lowers an entry (src/canonical_load.py:53-63).
- S19 `sqlite3.Error.sqlite_errorcode` exists from Python 3.11; the repository documents the older-Python fallback (docs/DEPLOYMENT.md:2224-2228) and already reads the attribute with `getattr` (web/services/oidc_consumption.py:209). The repository declares no Python minimum.

## 4. Engineering decisions (binding)

- E1 Pin inputs by one read into a private temporary copy; run `check_directory` on the copy (no existing code change).
- E2 Local preimage validation instead of `check_directory` (S4).
- E3 Require `applied migrations == repository list` exactly; refuse triggers, views, unclassified tables, shape mismatch (design section 5 wording "migrations equal the repository's").
- E4 Refuse (exit 1, before opening) whenever a path named `<db>-journal` exists (`os.path.lexists`), hot or not. Hotness cannot be decided without opening and locking, and production uses WAL (docs/DEPLOYMENT.md:206-211). This is best effort: a journal that appears after the check is handled by SQLite itself. Refuse before reading when the restore lock exists (given and resolved path); recheck under the lock and before COMMIT.
- E5 Narrowed (section 2a): a new TSA row needs a candidate topic.
- E6 Accepted (section 2a): the new-owner rule for options, subquestions and QTA.
- E7 Narrowed (section 2a): new `exam_tasks` under an existing exam are refused; the other additions under existing parents are enumerated, permitted and counted.
- E8 Accepted (section 2a): any pre-existing foreign-key violation or unresolved FK-less reference refuses the apply (also the lowered-sequence case); counts only; no cleanup; local extractor.
- E9 The id rule uses the reference schema's AUTOINCREMENT, the target's and the preimage's max and sequence; `sequence_below_preimage` refuses.
- E10 Unique-key pre-check from the pinned reference schema's declared unique indexes, not only `NATURAL_KEYS`; SQLite's error is only a backstop; unsupported index forms refuse.
- E11 Non-id duplicate guard on insert candidates only, including candidate-against-candidate; every non-id column, timestamps included; only for tables whose primary key is `["id"]`.
- E12 Preservation digest: storage-class-exact, order-independent (sorted per-row digests), blob-safe (S7); runtime digests excluded from the plan hash so a service-up plan is not stale by construction.
- E13 File references: validate candidate values as inert metadata, refuse unsafe values without rewriting or echoing them; advisory count for unexpected roots; never stat, open or copy.
- E14 Report: bounded (20 conflicts, 20 paths per column), redacted (no paths, exception text, runtime rows, values), deterministic (no timestamps), receipt on stdout only, never stored in the database.
- E15 Defense in depth: read-phase authorizer allowlist; write-phase allowlist limited to inserts into canonical tables and `sqlite_sequence` and updates of `sqlite_sequence`; source-level static guards.
- E16 Tests are standard library only (S15) with synthetic fixtures, one real-canonical no-op control, and a replacement real-runtime-database guard.
- E17 Accepted (section 2a): exactly one docs row in `docs/SCRIPT_INVENTORY.md`.
- E18 One first-canonical-write boundary, one write footprint, one COMMIT-error probe (D1; section 2b).
- E19 The target contract of section 2b: the tool never creates the target database; validation creates disposable reference databases; no byte or sidecar guarantee for a WAL target.
- E20 The legacy asset owner type `questions` is refused at plan time; unknown owner types too.
- E21 Key and AUTOINCREMENT metadata come from the pinned reference schema; the manifest is validated against it, never trusted.
- E22 The implementation base must have exactly the tree of scope section 8; there is no hash-map alternative.

## 5. Owner and operational unknowns (stay open)

- O1 **Preimage-record form.** The tool accepts a canonical directory and states that the owner's dated release record tying it to the target is unverified. Which record, signed or not, is an owner decision; the tool proves no provenance (a repository SHA names files, not database content).
- O2 **Metadata edits** of unreferenced or metadata rows. v1 refuses every differing existing row (`value_differs`); nothing is edited. A later reviewed amendment may add a narrow case.
- O3 **History witness (PR #327).** An apply receipt is not an accepted witness; the report says so. Whether insert-only additions can be proven not to disturb past attempts, beyond the refusal rules here, is #327's decision.
- O4 **Execution with the service up.** The tool is designed for concurrent writers (write lock, replan, stale refusal) and under WAL does not block readers. The write lock is held for the whole replan, insert and validation phase, which delays service writers by an unmeasured amount. Whether an operator may run `--apply` while the service is up, stopped or in a maintenance window is a DEPLOYMENT-procedure decision. The tool cannot detect a running service and does not claim to.
- O5 **Asset verifier.** v1 states `file availability: not checked`; nothing copies or verifies files; whether and how a later verifier runs is open.
- O6 **Backup, snapshot and release-record prerequisites.** A complete snapshot (DEPLOYMENT sections 16, 20) is **unverified and external**; snapshots scrub pending registrations and recoveries and carry old credentials (design section 6, DEPLOYMENT section 37), so they are neither an exact runtime baseline nor a credential-neutral restore. No flag in the tool asserts them.
- O7 Whether a target at a different migration level than the repository must first go through `manage_migrations`: yes, separately; the tool refuses, never migrates.
- O8 Review D6/D7 content gating and the DZI gate: a valid plan or apply satisfies neither (design section 6; roadmap 2026-11-18).
- O9 Actual production database content and baseline, deployed commit, assets, schema level: unknown, not inferred. No production database is promised updatable.

## 6. Not decided here

Legal and provider facts, live operational procedure, DEPLOYMENT text, restore protocol, activation, any real execution, any canonical content refresh.

## 7. Next step

The coordinator verifies that the base tree is `c889275e8e42871cdf8374678a08271890414648`, creates the branch, worktree and draft PR, and assigns a fresh implementer to the four frozen files.
