# Question Image Mapping Plan

## Current state

- The DB schema has legacy `questions.has_image` / `questions.image_path`
  fields, plus the newer `assets` and `asset_links` tables from
  `web/migrations/003_dzi_tasks_assets_blueprint.sql`.
- The Part 1 DZI JSON format already supports optional task `assets` in
  `docs/dzi_question_import_format.md`. `src/import_dzi_questions_json.py`
  validates asset paths, writes `assets`, links them to both `question` and
  `exam_task`, and sets `questions.has_image` / `exam_tasks.has_assets`.
- Runtime quiz eligibility already treats visual-dependent multiple-choice
  questions as usable only when `questions.image_path` exists or a linked
  `image` / `pdf_crop` asset exists. See `question_has_usable_visual()` in
  `web/app.py`.
- `src/audit_dzi_assets.py` is the current read-only audit for DZI visual
  assets. It uses conservative prompt patterns such as "изображението",
  "диаграмата", "фигурата", and table/diagram phrases to flag likely
  image-dependent tasks.
- `data/assets/exams/` exists as the intended repo-managed asset root, but this
  planning PR must not add files, import assets, or modify `data/questions.db`.

## Goal

Create a safe, reviewed workflow that maps every image-dependent question to
the correct image/crop/resource before any DB write. The workflow should make
questions answerable in quizzes without changing scoring, answers, assignment
behavior, or unrelated DZI import semantics.

## Data model assumptions

- Prefer the `assets` + `asset_links` model for new work. Treat
  `questions.image_path` as legacy compatibility, not the primary mapping
  target.
- Store one `assets` row per physical file, keyed by repo-relative
  `assets.local_path`.
- Link question images with:
  - `asset_links.owner_type = 'question'`
  - `asset_links.owner_id = questions.id`
  - `asset_links.role = 'image'` or `'pdf_crop'`
- Also link the same asset to the official task slot when possible:
  - `asset_links.owner_type = 'exam_task'`
  - `asset_links.owner_id = exam_tasks.id`
  - same `role`, `caption_bg`, `source_page`, and `source_bbox_json`
- Keep `questions.has_image` and `exam_tasks.has_assets` as derived flags from
  linked assets. Do not rely on them alone as proof that a file exists.
- Practical tasks 26-28 have a separate resource workflow. This plan is for
  question images/crops needed to answer Part 1 questions.

## Audit workflow

1. Run the existing read-only visual audit for one source:

   ```bash
   python3 src/audit_dzi_assets.py --source-slug may_2025_v2
   ```

2. Review rows where:
   - `prompt_needs_visual = yes`
   - `asset_status` is `link_missing` or `file_missing`
   - `quiz_blocking = yes`

3. Cross-check the prompt against the official PDF manually. Pattern detection
   is intentionally conservative and should not be the final authority.

4. Audit existing links with a read-only query before creating new mappings:

   ```sql
   SELECT
       et.task_number,
       q.id AS question_id,
       q.has_image,
       q.image_path,
       COUNT(DISTINCT al.asset_id) AS linked_assets
   FROM exam_tasks et
   JOIN exam_task_questions etq ON etq.task_id = et.id AND etq.role = 'primary'
   JOIN questions q ON q.id = etq.question_id
   LEFT JOIN asset_links al
     ON (
       (al.owner_type IN ('question', 'questions') AND al.owner_id = q.id)
       OR (al.owner_type = 'exam_task' AND al.owner_id = et.id)
     )
   WHERE et.exam_id = ?
   GROUP BY et.id, q.id
   ORDER BY et.task_number;
   ```

5. Record findings in a source-specific review note before any import, for
   example `docs/reviews/<source_slug>_question_image_audit.md`.

## Asset naming/location rules

- Store new image files under:

  ```text
  data/assets/exams/<source_slug>/
  ```

- Use these subfolders:
  - `images/` for official standalone image files.
  - `pdf_crops/` for crops extracted from the official PDF.
  - `resources/` only for non-image supporting files that belong to a question.

- Use deterministic names:
  - `images/task_<NN>.<ext>`
  - `images/task_<NN>_part_<label>.<ext>`
  - `pdf_crops/task_<NN>_fig_<n>.png`

- Paths stored in JSON/DB must be repo-relative, never absolute.
- Do not use `..`, generated temp paths, or user-provided filenames as storage
  paths.
- Keep each source isolated under its own `<source_slug>` directory so repeated
  names like `logo.png` or `diagram.png` never collide across exams.
- Preserve official filenames in `assets.original_filename` or link captions
  when the file comes from an official resource package.

## Mapping workflow

For not-yet-imported Part 1 batches:

1. Add reviewed `assets` entries directly to the existing task JSON.
2. Validate with `src/validate_question_batch.py`.
3. Import only in a later planned DB/data PR.

For already-imported questions:

1. Do not re-import full question batches just to attach images if the question
   has already been used in quizzes.
2. Create a reviewed mapping manifest first. Recommended shape:

   ```json
   {
     "source_slug": "may_2025_v2",
     "mappings": [
       {
         "task_number": 18,
         "asset_type": "pdf_crop",
         "file_path": "data/assets/exams/may_2025_v2/pdf_crops/task_18_fig_1.png",
         "caption_bg": "Фигура към задача 18",
         "source_page": 5,
         "source_bbox_json": "{\"x\": 84, \"y\": 192, \"width\": 420, \"height\": 210}"
       }
     ]
   }
   ```

3. A future asset-link-only importer should resolve `source_slug + task_number`
   to `exam_tasks.id` and the primary `questions.id`, then upsert `assets` and
   `asset_links` without touching prompt text, options, accepted answers, or
   scoring fields.

## Validation checklist

- The source slug resolves to exactly one DZI `exams` row.
- Each mapped `task_number` resolves to one `exam_tasks` row.
- Each mapped task has exactly one primary linked question, or the importer
  reports the ambiguity and stops.
- Every `file_path` is repo-relative and under `data/assets/exams/<source_slug>/`.
- Every mapped file exists, is a regular file, has a known extension, and has
  recorded `sha256`, MIME type, and file size.
- `asset_type` is `image` or `pdf_crop` for visual question attachments.
- `source_bbox_json`, when present, parses as JSON and contains numeric crop
  coordinates.
- Visual-dependent multiple-choice questions in the eligible quiz pool have at
  least one usable linked `image` / `pdf_crop`.
- Non-visual questions are not given unrelated assets unless there is a
  documented reason.
- Missing files must fail validation. Do not use `--allow-missing-assets` for a
  release import.
- After the future import PR, run `pragma_foreign_key_check` and the existing
  DZI asset audit again.

## Manual vs scripted work

Manual:

- Confirm whether a question is actually image-dependent by reading the
  official PDF, not just by matching prompt keywords.
- Choose the correct screenshot/diagram/crop and verify that it contains all
  information needed to answer the question.
- Confirm captions, source page, and crop bounding boxes.
- Decide whether a borderline table/code fragment should be transcribed into
  prompt text or attached as a crop.

Scripted:

- Detect likely visual-dependent prompts.
- List current `questions.image_path`, `assets`, and `asset_links` coverage.
- Validate paths, file existence, hashes, MIME/file size, and owner resolution.
- Produce a dry-run plan showing which `assets` and `asset_links` would be
  inserted or updated.
- Re-run audits after import to confirm there are no quiz-blocking visual gaps.

## Suggested PR sequence

1. **Docs plan only.** Add this plan. No code, assets, migrations, or DB writes.
2. **Read-only audit report.** Run or improve read-only audit tooling and add
   source-specific findings under `docs/reviews/`. No DB writes and no assets.
3. **Asset file PR.** Add reviewed image/crop files under
   `data/assets/exams/<source_slug>/` plus a mapping manifest. No DB writes.
4. **Validator/importer PR.** Add tests and an asset-link-only dry-run validator
   for the manifest. The default path must be read-only/dry-run.
5. **Intentional data PR.** Run the reviewed importer against
   `data/questions.db`, committing DB changes only if that PR explicitly owns
   data updates.
6. **Rendering/UX PR if needed.** Adjust templates only if linked images are not
   already shown where students need them. Keep scoring and assignment behavior
   unchanged.
