# DZI Image DB Linking Plan

## Completed Confirmed-Image Pipeline

Current source of truth is the reviewed official-PDF mapping file:

```text
docs/reviews/confirmed_dzi_image_question_mappings.json
```

The mapping keys are `source_slug + official_question_number`, where
`official_question_number` is the task number printed in the official DZI PDF.
It is not a LearnPilot DB question ID. Use the mapped `db_question_id` only
after validation confirms that the official PDF task number still resolves to
the same DB question.

Confirmed DZI question images live under:

```text
web/static/img/questions/dzi/
```

Filenames must match the mapping exactly:

```text
<source_slug>_q<official_question_number>.png
```

Example: `may_2025_v2_q22.png`.

Use this validation/check sequence for the completed pipeline:

```bash
python3 src/validate_confirmed_dzi_image_mappings.py
python3 src/validate_confirmed_dzi_asset_files.py --strict
python3 src/apply_confirmed_dzi_image_mappings.py --check
python3 src/audit_rendered_question_images.py
sqlite3 "file:data/questions.db?mode=ro" "SELECT * FROM pragma_foreign_key_check;"
```

The intentional DB-linking command for a dedicated data PR is:

```bash
python3 src/apply_confirmed_dzi_image_mappings.py
```

Run it only in a PR that explicitly owns `data/questions.db` changes, then
re-run the read-only `--check`, rendered-image audit, FK check, and tests.

Safe PR order for future official DZI exams:

1. Add/confirm official PDFs and review image-dependent PDF task numbers.
2. Add/update the confirmed mapping JSON; validate mappings before adding files.
3. Add extracted PNG assets under `web/static/img/questions/dzi/` using the
   filename convention above; run strict asset validation.
4. Link assets to DB questions and official exam tasks in a dedicated data PR.
5. Verify student rendering with `audit_rendered_question_images.py`.
6. Add or adjust teacher/admin indicators only after linked images render for
   students; do not change scoring or answer submission.

The older sections below document the design path that led to the completed
pipeline and are kept as historical context.

This plan covers the future DB and rendering step for showing confirmed DZI
question images in the app. It is documentation only: no DB writes, migrations,
template changes, route changes, or asset imports are part of this PR.

Official question numbers in the mapping file are PDF task numbers, not
LearnPilot DB question IDs. Any future DB update must use the confirmed
`db_question_id` values from
`docs/reviews/confirmed_dzi_image_question_mappings.json`.

## Current State

- The confirmed mapping file has 37 image-needed mappings and 2 explicit
  no-image notes. All entries currently validate with `confidence: exact`.
- Extracted image files are expected under:

  ```text
  web/static/img/questions/dzi/
  ```

- The actual DB schema already has:
  - `questions.has_image` and `questions.image_path`
  - `assets.local_path`, keyed uniquely per physical file
  - `asset_links`, which can link one asset to a `question` and an `exam_task`
  - `exam_tasks.has_assets`
  - `exam_task_questions`, linking official task slots to DB questions
- Read-only inspection showed the current DB only has the seven official source
  PDF assets linked as `owner_type='exam'`, `role='source_pdf'`. It has no
  linked question image assets under `web/static/img/questions/dzi/`.
- `web/app.py` already treats a question as visually usable when a linked
  `assets.asset_type IN ('image', 'pdf_crop')` file exists through either the
  question link or the exam task link. Current quiz and DZI templates render
  prompts/options only, so linked assets would unblock eligibility but would not
  yet be visible to students.

## Storage Decision

Use the existing `assets` and `asset_links` tables as the source of truth. No
schema migration is needed for the confirmed DZI question images.

For each ready mapping, the future DB update should store:

```text
assets.asset_type = 'pdf_crop'
assets.original_filename = <source_slug>_q<official_question_number>.png
assets.local_path = web/static/img/questions/dzi/<source_slug>_q<official_question_number>.png
assets.mime_type = image/png
assets.file_size = actual file size
assets.sha256 = actual file hash
assets.notes = confirmed DZI official PDF crop for <source_slug> Q<official_question_number>
```

Then link the same asset twice:

```text
asset_links.owner_type = 'question'
asset_links.owner_id = <db_question_id>
asset_links.role = 'pdf_crop'
asset_links.display_order = 0

asset_links.owner_type = 'exam_task'
asset_links.owner_id = <resolved exam_tasks.id>
asset_links.role = 'pdf_crop'
asset_links.display_order = 0
```

Treat `questions.has_image` and `exam_tasks.has_assets` as compatibility flags
derived from linked assets. Set both to `1` for mapped rows in the future data
PR, but do not populate `questions.image_path` for this flow. Keeping
`questions.image_path` empty avoids a second source of truth and matches the
current DZI JSON importer, which sets `has_image` for tasks with assets but
stores asset paths in `assets.local_path`.

## Future DB Update Strategy

Add a future dry-run-first script, for example
`src/link_confirmed_dzi_question_images.py`. Its default mode should be
read-only and should print every planned write. A separate explicit flag should
be required for writes.

For each mapping where `needs_image: true` and `confidence` is `exact` or
`likely`, the script should:

1. Validate the mapping file with
   `src/validate_confirmed_dzi_image_mappings.py`.
2. Validate image files with
   `src/validate_confirmed_dzi_asset_files.py --strict`.
3. Resolve the DB question by `db_question_id`.
4. Require `questions.source_exam = source_slug`.
5. Require `questions.source_number = official_question_number`.
6. Resolve `source_slug` to one `exams` row using the existing DZI slug
   convention: `<session>_<year>_v<variant>`.
7. Resolve exactly one `exam_tasks` row by that exam ID and
   `official_question_number`.
8. Require `exam_task_questions` to link that task to the same
   `db_question_id` with `role='primary'`.
9. Require the PNG file to exist under `web/static/img/questions/dzi/` with the
   exact `expected_asset_filename`.
10. Upsert one `assets` row by `local_path`.
11. Upsert the `question` and `exam_task` links in `asset_links`.
12. Set `questions.has_image = 1` and `exam_tasks.has_assets = 1`.

The write mode should run all 37 ready mappings in one transaction and abort the
transaction on the first validation or foreign-key error. It must not change
prompt text, options, accepted answers, scoring fields, assignments, attempts,
or historical quiz answers.

## Render Strategy

Rendering should read visual assets from `assets` plus `asset_links`, not from
hard-coded filenames or client-provided paths.

Add a helper in a future app-code PR, for example `fetch_question_visual_assets`,
that returns safe render data for a list of question IDs:

```text
question_id
local_path
static_filename = local_path with the leading "web/static/" removed
caption_bg
source_page
display_order
```

Only accept `assets.local_path` values under `web/static/img/questions/dzi/`.
Convert them to static URLs with:

```python
url_for("static", filename="img/questions/dzi/<filename>")
```

Templates that should render the visual near the prompt:

- `web/templates/quiz_attempt.html`
- `web/templates/quiz_result.html`
- `web/templates/dzi_source.html`
- `web/templates/section.html`, if section review pages should show the same
  question context as quizzes

The image should appear immediately after the prompt and before answer inputs or
options. Use meaningful alt text derived from source and task number, such as
`DZI <source_slug> task <official_question_number> image`. Do not expose DB IDs
or filesystem paths in template text.

Because current eligibility already checks linked `image` / `pdf_crop` assets,
the future render PR should land with or before the DB-linking PR. Otherwise the
questions could become eligible without the visual information being shown.

## Validation Gates

Before any intentional DB update:

```bash
python3 src/validate_confirmed_dzi_image_mappings.py
python3 src/validate_confirmed_dzi_asset_files.py --strict
sqlite3 "file:data/questions.db?mode=ro" "SELECT * FROM pragma_foreign_key_check;"
python3 src/link_confirmed_dzi_question_images.py --dry-run
python3 -m unittest discover -s tests
```

After an intentional DB update in a future data PR:

```bash
python3 src/validate_confirmed_dzi_image_mappings.py
python3 src/validate_confirmed_dzi_asset_files.py --strict
sqlite3 "file:data/questions.db?mode=ro" "SELECT * FROM pragma_foreign_key_check;"
python3 -m unittest discover -s tests
```

Add a post-link validator or mode that reports:

- ready mappings in JSON
- mapped assets present in `assets`
- mapped `question` links present in `asset_links`
- mapped `exam_task` links present in `asset_links`
- duplicate `assets.local_path` conflicts
- duplicate question or task links
- mapped questions with `has_image != 1`
- mapped exam tasks with `has_assets != 1`
- any mapped local file missing from disk

The expected ready count for this confirmed set is 37.

## Rollback Safety

The future data PR should produce a rollback manifest before writing. The
manifest should include:

- mapped `db_question_id`
- resolved `exam_task_id`
- inserted or reused `asset_id`
- previous `questions.has_image`
- previous `questions.image_path`, even though this plan does not change it
- previous `exam_tasks.has_assets`
- inserted `asset_links.id` values

Rollback should be a single transaction that:

1. Deletes only `asset_links` created for the confirmed DZI question images.
2. Deletes only `assets` rows whose `local_path` is in the confirmed mapping set
   and that have no remaining links.
3. Restores previous `questions.has_image` values.
4. Restores previous `exam_tasks.has_assets` values.
5. Leaves prompts, options, answers, assignments, attempts, and quiz results
   untouched.

The future PR should also keep a DB backup outside the repo before the write is
run, because `data/questions.db` is a binary artifact and rollback review is
easier with both SQL output and a file-level backup.

## Tests Needed

Future tests should cover:

- mapping-to-DB resolution by `db_question_id`, `source_slug`, and official task
  number
- refusal when the mapping question and `exam_task_questions` primary link do
  not agree
- safe filename and local path enforcement
- dry-run output without DB writes
- idempotent upsert behavior for `assets` and `asset_links`
- transaction rollback on mid-run failure
- render helper conversion from `web/static/img/questions/dzi/<file>.png` to
  `img/questions/dzi/<file>.png`
- quiz attempt and result rendering with linked images
- DZI source rendering with linked images
- explicit no-image notes remaining unlinked
- no duplicate links after running the linker twice

These tests should run without requiring new image extraction, DB migrations, or
network access.
