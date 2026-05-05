# DZI Expansion Log

## JSON Part 1 Importer Safety Note

The official DZI Part 1 JSON importer was created as `src/import_dzi_questions_json.py`.

The initial dry run against `data/import_samples/dzi_sample_part1.json` worked and validated the intended import path.

An accidental real sample import was then run against `data/questions.db`. Because the sample used the real `may_2025_v2` source slug and task numbers, it updated tasks 1 and 16.

Those two rows were restored from `data/questions.backup-before-dzi-expansion.db`:

- `may_2025_v2` task 1 was restored to the official prompt about reclamations and `COUNTIF`, with 4 options.
- `may_2025_v2` task 16 was restored to the official fill-in prompt about the sales chart, with 3 fill-in subquestions.

Future sample imports should be dry-run only. Sample JSON fixtures should be marked with `_sample_only: true`, and the importer rejects those files unless `--dry-run` is used.
