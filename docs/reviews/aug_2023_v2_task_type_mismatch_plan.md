# aug_2023_v2 Task-Type Mismatch Plan

## Problem

`aug_2023_v2` does not follow the current 2025-format Part 1 task-type split used by the importer skeleton.

- Official PDF tasks 1-10 are multiple choice.
- Official PDF tasks 11-13 are written-answer tasks.
- Official PDF tasks 14-18 are multiple choice.
- Official PDF tasks 19-25 are written-answer tasks.
- Current `exam_tasks` skeleton and importer validation expect tasks 1-15 as `multiple_choice` and tasks 16-25 as `short_answer`.

This means tasks 11-20 cannot be represented truthfully in the current strict JSON import flow. Forcing them into the current schema would either fail validation or misrepresent the official exam.

## Already Safe

PR #73 added dry-run-valid reviewed batch chunks for:

- `data/import_batches/aug_2023_v2_part1_tasks_1_5.json`
- `data/import_batches/aug_2023_v2_part1_tasks_6_10.json`
- `data/import_batches/aug_2023_v2_part1_tasks_21_25.json`

These chunks match both the official PDF and the current skeleton/importer expectations.

## Not Yet Safe

Do not create or import these chunks under the current assumptions:

- `data/import_batches/aug_2023_v2_part1_tasks_11_15.json`
- `data/import_batches/aug_2023_v2_part1_tasks_16_20.json`

Tasks 11-13 need `short_answer`, while the skeleton expects `multiple_choice`. Tasks 16-18 need `multiple_choice`, while the skeleton expects `short_answer`. Tasks 19-20 are `short_answer` and could fit the current skeleton, but they live in the same chunk as mismatched tasks 16-18, so the chunk should wait for the mismatch fix.

## Recommended Next PR

The smallest safe implementation PR should be source-specific and should not weaken validation globally.

1. Add an explicit `aug_2023_v2` task-kind override map for tasks 11-18 in the DZI skeleton/import validation path.
2. Update the `aug_2023_v2` `exam_tasks` skeleton in `data/questions.db` in a planned DB/data PR so tasks 11-13 are `short_answer` and tasks 16-18 are `multiple_choice`.
3. Update `validate_question_batch.py` and `import_dzi_questions_json.py` only as needed to validate against the corrected per-source skeleton. Keep the default strict check that JSON `task_kind` must match `exam_tasks.task_kind`.
4. Update `tests/test_aug_2023_v2_batch_scaffold.py` so its expected task kinds are source-aware for `aug_2023_v2` instead of assuming the 2025 split.
5. Create and validate the remaining reviewed batch chunks after the skeleton and tests agree with the official PDF.

Avoid changing batch chunk naming unless the importer starts supporting partial chunks. The existing five-file naming is still useful for review, but validation of `16_20` requires tasks 16-18 to be corrected to multiple choice first.

## Test Gate

The next implementation PR should include:

- A focused unit test proving `aug_2023_v2` tasks 11-13 validate as `short_answer`.
- A focused unit test proving `aug_2023_v2` tasks 16-18 validate as `multiple_choice`.
- An updated `tests/test_aug_2023_v2_batch_scaffold.py` that expects the official task-kind split for this source.
- Dry-run validation for all existing and newly prepared `aug_2023_v2` batch files.
- `python3 src/audit_dzi_state.py`
- `python3 src/audit_open_question_readiness.py`
- Read-only SQLite foreign key check.
- Full test discovery.

## Still Forbidden

- Do not guess answers.
- Do not use OCR as source of truth.
- Do not import into `data/questions.db` before dry-run validation passes.
- Do not force visual-dependent open tasks into auto-gradable answers.
- Do not weaken the importer so task-type mismatches pass silently for other sources.
