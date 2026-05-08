# Question Batch Review Pipeline

Use this PR only to prepare and review JSON batches. Do not import into
`data/questions.db` yet.

## Safe Existing Path

The safest existing path is the official DZI Part 1 JSON format:

- Format docs: `docs/dzi_question_import_format.md`
- Existing reviewed data: `data/import/dzi/*.json`
- Importer used later: `src/import_dzi_questions_json.py`
- Dry-run-only review wrapper: `src/validate_question_batch.py`

The wrapper opens the DB with SQLite `mode=ro`, calls the importer validation
logic with `dry_run=True`, and refuses any attempted DB change.

## Prepare a Batch

Create a JSON file under `data/import_batches/` using one official
`source_slug` per file. Include:

- `source_slug` and `tasks`.
- `task_number`, `task_kind`, `points`, and `prompt` for every task.
- `topic_slug`, `section_slug`, and optional `grade` for curriculum validation.
- Four `options` for `multiple_choice`, with exactly one `is_correct: true`.
- `answers` or `subquestions` for `short_answer`.
- Optional `assets` that point to files on disk, not SQLite blobs.

Tasks 1-15 must remain `multiple_choice`. Tasks 16-25 must remain
`short_answer`. Practical tasks 26-28 are outside this batch format.

## Run Dry-Run Review

```bash
python3 src/validate_question_batch.py --json data/import_batches/<batch>.json
```

The command reports:

- Whether the source exam and task skeleton resolve.
- Whether topics and sections resolve.
- Whether optional `grade` values match the resolved section class.
- Whether MC options and open accepted answers are valid.
- Which questions would be inserted or updated.
- Which options, fill-in subquestions, task links, and assets would be touched.

Do not run the real importer in this PR. A later import PR can run
`src/import_dzi_questions_json.py` intentionally after review approval.
