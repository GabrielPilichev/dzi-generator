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
