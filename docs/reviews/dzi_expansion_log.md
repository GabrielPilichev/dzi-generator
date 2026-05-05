# DZI Expansion Log

## JSON Part 1 Importer Safety Note

The official DZI Part 1 JSON importer was created as `src/import_dzi_questions_json.py`.

The initial dry run against `data/import_samples/dzi_sample_part1.json` worked and validated the intended import path.

An accidental real sample import was then run against `data/questions.db`. Because the sample used the real `may_2025_v2` source slug and task numbers, it updated tasks 1 and 16.

Those two rows were restored from `data/questions.backup-before-dzi-expansion.db`:

- `may_2025_v2` task 1 was restored to the official prompt about reclamations and `COUNTIF`, with 4 options.
- `may_2025_v2` task 16 was restored to the official fill-in prompt about the sales chart, with 3 fill-in subquestions.

Future sample imports should be dry-run only. Sample JSON fixtures should be marked with `_sample_only: true`, and the importer rejects those files unless `--dry-run` is used.

## P0 Safety Fixes

P0 safety fixes were implemented before importing any real official Part 1 exam JSON.

- Added `web/migrations/004_dzi_safety_constraints.sql`.
- Added the unique index `uniq_questions_source_exam_number` on `questions(source_exam, source_number)` for non-null source identities.
- Changed the sample import slug from a real exam source to the fictional `sample_2099_v0`.
- Added `src/audit_exam_provenance.py`, a read-only provenance audit for duplicate exam identities, stale DZI formats, missing source files, and DZI rows with missing task skeletons.

Audit result summary: `src/audit_exam_provenance.py` currently exits with `audit_exit=1` and reports 12 warnings. There are no duplicate exam identities and no missing DZI `source_file` values. The warnings are for four legacy `modern_2023` DZI exam rows that have zero `exam_tasks`, have questions but zero `exam_tasks`, and do not use `dzi_it_pp_2025_format`: ids 7, 1, 4, and 2. These are legacy rows and should not be deleted as part of the safety work.

## P1 Import Readiness

P1 readiness work was implemented before importing May 2025 Part 1.

- Added `src/audit_dzi_state.py`, a read-only readiness audit for official DZI sources.
- Confirmed importer asset handling stores SHA-256, file size, and MIME type for existing asset files, and now warns clearly when `--allow-missing-assets` permits a missing file.
- Added `--allow-unknown-topic` and `--allow-unknown-section` to the JSON importer. Default behavior remains strict.
- Added a rollback message for failures inside the real DB write transaction: `ROLLED BACK — no DB changes committed`.
- Documented the re-import caveat: re-importing a task replaces options/subquestions for that official question, which can affect historical quiz attempt interpretation.

Audit result summary: `src/audit_dzi_state.py` exits with `audit_dzi_exit=0`. All seven `dzi_it_pp_2025_format` official DZI sources are `READY_FOR_PART1_IMPORT` by the skeleton/PDF criteria: each has 28 `exam_tasks`, 100 points, 3 practical rows, at least one official `exam_pdf` source, at least one source PDF asset, no missing `source_file`, and no missing linked asset files. The audit also shows that most Part 1 question links are still missing, which is expected before real official Part 1 JSON import.

## Immediate Non-Schema Fixes

After May 2025 v2 Part 1 was imported, immediate non-schema fixes were applied before importing another official exam.

- Fixed DZI inspection answer rendering so JSON-encoded `fill_in_subquestions` answers display as plain accepted values instead of raw JSON text.
- Fixed sample-only dry-run behavior so fictional sample source slugs can validate JSON structure without resolving an exam.
- Gated `/dzi` and `/dzi/source/<source_slug>` behind existing admin authentication because the pages reveal official answers.
- Updated `src/audit_dzi_state.py` to distinguish `PART1_IMPORTED` from `READY_FOR_PART1_IMPORT`.

## Legacy Slug Safety Cleanup

Added `src/cleanup_legacy_dzi_question_slugs.py` as a conservative data-only cleanup tool for known legacy DZI question slugs such as `may_2022` and `may_2024`. The script supports dry-run mode, refuses mappings when the canonical target already has rows, verifies the canonical exam row, and does not delete anything.

Importer sample dry-run output was clarified so sample-only fixtures report structural validation and explicitly state that no DB writes are planned. The old compatibility path for `exam_tasks.question_id` was removed because `exam_task_questions` is the canonical link table. The DZI state audit now counts filled Part 1 task slots rather than distinct linked question rows.
