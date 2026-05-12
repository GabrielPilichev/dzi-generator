# DZI Practical Tasks 26–28 — Planning Document

This is a docs-only planning checkpoint. It does not change schema, app code,
templates, tests, scoring, the DB, or any assets. It records the inventory and
the staged plan for handling official DZI Part 2 practical tasks (tasks 26, 27,
28) across the seven tracked sources.

## 1. Scope

- Practical tasks **26, 27, 28 only**.
- No DB import in this PR.
- No schema, app, UI, route, scoring, auth, or template changes in this PR.
- No resource ZIP extraction in this PR.
- No new batch JSON files in this PR.
- No DB writes in this PR.

The reviewed Part 1 (tasks 1–25) import workflow stays unchanged and is already
complete for every tracked source (see `dzi_expansion_log.md`).

## 2. Current audit baseline

Run on the current branch (read-only):

- DZI exams: 7
- `PART1_IMPORTED`: 7
- `READY_FOR_PART1_IMPORT`: 0
- `NEEDS_ATTENTION`: 0
- foreign key check rows: 0
- `total assets`: 7 (only the seven official Part 1 PDFs)
- `missing asset files`: 0

For every tracked source, `q_links_26_28 = 0`. The `exam_tasks` skeleton already
includes task rows 26, 27, 28 with `task_kind` values `practical_spreadsheet`,
`practical_graphics`, `practical_web` (mapped via `practical_tasks` to
`work_environment` `spreadsheet`, `graphics`, `web` respectively), but no
`exam_task_questions` links exist yet for tasks 26–28. Practical tasks have not
been imported.

## 3. Resource ZIP/folder inventory

The repo-local resource ZIPs are filesystem-only; read-only listings below were
collected with `zipinfo -1`. `__MACOSX/` metadata entries are filtered out for
clarity. No ZIP was extracted, modified, or moved.

### aug_2022_v2

- source_slug: `aug_2022_v2`
- official PDF: `data/reference/dzi/official_pdfs/aug_2022_v2_exam.pdf`
- expected resource folder: `data/reference/Август 22/`
- resource ZIP path: `data/reference/Август 22/ДЗИ-ИТ-Август-2022-Ресурси.zip`
- present? yes
- ZIP contents (high level):
  - `task_26/Onlinemag.xlsx`
  - `task_28/WebСompany/face_1.jpg`
  - `task_28/WebСompany/face_2.jpg`
  - `task_28/WebСompany/face_3.jpg`
- notes: no `task_27/` folder present in the ZIP; task 27 resources are not bundled in this archive. Needs follow-up to confirm whether task 27 is purely descriptive (no provided assets) or whether the resource ZIP is incomplete.

### may_2022_v1

- source_slug: `may_2022_v1`
- official PDF: `data/reference/dzi/official_pdfs/may_2022_v1_exam.pdf`
- expected resource folder: `data/reference/Май 22/`
- resource ZIP path: `data/reference/Май 22/ДЗИ-ИТ-Май-2022-Ресурси.zip`
- present? yes
- ZIP contents (high level):
  - `task_26/zad.26_NOIT.xlsx`
  - `task_27/alaska-iceburg-1515457.jpg`
  - `task_27/bear.jpg`
  - `task_28/Web_ chicken/Recept.docx`
  - `task_28/Web_ chicken/chicken-shashlik.jpg`
- notes: resources for all three practical tasks appear present. Task 28 folder name contains a space (`Web_ chicken`); preserve verbatim if/when assets are referenced.

### aug_2023_v2

- source_slug: `aug_2023_v2`
- official PDF: `data/reference/dzi/official_pdfs/aug_2023_v2_exam.pdf`
- expected resource folder: `data/reference/Август 23/`
- resource ZIP path: `data/reference/Август 23/ДЗИ-ИТ-Август-2023-Ресурси.zip`
- present? yes
- ZIP contents (high level):
  - `task_26/Zoomag.xlsx`
  - `task_27/Laguna-unsplash.jpg`, `Osnova-unsplash.jpg`, `Viktori-unsplash.jpg`, `Tuiti-unsplash.jpg`, `Piperoni-unsplash.jpg`, `pizzas.docx`
  - `task_28/bedroom.png`, `living.png`, `kitchen.png`, `furniture.jpg`
- notes: resources for all three practical tasks appear present.

### may_2023_v2

- source_slug: `may_2023_v2`
- official PDF: `data/reference/dzi/official_pdfs/may_2023_v2_exam.pdf`
- expected resource folder: `data/reference/Май 23/`
- resource ZIP path: `data/reference/Май 23/ДЗИ-ИТ-Май-2023-Ресурси.zip`
- present? yes
- ZIP contents (high level):
  - `task_26/Icecream.xlsx`
  - `task_27/Earth/Picture_1.jpg` … `Picture_5.jpg`, `Picture_9.jpg`, `Picture_11.jpg`–`Picture_14.jpg`
  - `task_28/BG/animals.jpg`, `map.png`, `text.docx`, `logo.png`, `mountain.jpg`
- notes: resources for all three practical tasks appear present.

### aug_2024_v2

- source_slug: `aug_2024_v2`
- official PDF: `data/reference/dzi/official_pdfs/aug_2024_v2_exam.pdf`
- expected resource folder: `data/reference/Август 24/`
- resource ZIP path: `data/reference/Август 24/Август-2024-г.zip`
- present? yes (the outer ZIP)
- ZIP contents (high level, top-level folder name returns mojibake from `zipinfo` – the original Cyrillic header is not decoded):
  - `<cyrillic-folder>/it_23.08.2024_ zad26.xlsx`
  - `<cyrillic-folder>/task_26 (2).pdf`
  - `<cyrillic-folder>/task_26_resources (2).zip`
  - `<cyrillic-folder>/task_27 (2).pdf`
  - `<cyrillic-folder>/task_27_resources (2).zip`
  - `<cyrillic-folder>/task_28 (1).pdf`
  - `<cyrillic-folder>/task_28_resources (2).zip`
- notes: this source uses a **nested-ZIP** layout — each practical task has its own `*_resources*.zip` plus a per-task PDF inside the outer archive. The actual task-resource files are inside those inner ZIPs and are not visible without extraction. PR A (inventory) must verify each inner ZIP can be opened with read-only tools; a future extraction step will be needed before referencing individual files. Also note the mojibake top-level folder name — the on-disk archive name (`Август-2024-г.zip`) is correct UTF-8 but the central-directory entries appear to use a different encoding.

### may_2024_v1

- source_slug: `may_2024_v1`
- official PDF: `data/reference/dzi/official_pdfs/may_2024_v1_exam.pdf`
- expected resource folder: `data/reference/Май 24/`
- resource ZIP path: `data/reference/Май 24/dzi-it-may-2024.zip`
- present? yes
- ZIP contents (high level):
  - `task_26_resources/Choices.xlsx`
  - `task_27_resources/Images_flyer/` (16 image files: avocado, backgrounds 1–3, coconut, cucumber, fish, grapes, green-salad, kiwi, lemon, orange, peach, pomegranate, raspberries, tomato, watermelon)
  - `task_28_resources/PZ_3_Resources/28-text.docx`, `bee1.png`, `bee2.png`, `bee3.png`, `bg.png`, `logo.png`
- notes: resources for all three practical tasks appear present. Folder naming convention here is `task_NN_resources/` (different from `task_NN/` used in earlier years) — preserve verbatim.

### may_2025_v2

- source_slug: `may_2025_v2`
- official PDF: `data/reference/dzi/official_pdfs/may_2025_v2_exam.pdf`
- expected resource folder: `data/reference/Май 25/`
- resource ZIP path: `data/reference/Май 25/May_2025 (1).zip`
- present? yes — and the ZIP **has already been extracted in-place** to `data/reference/Май 25/May_2025/`. ZIP contents and on-disk contents match (`zad_26/`, `zad_27/`, `zad_28/`).
- ZIP contents (high level):
  - `May_2025/zad_26/Shipments.xlsx`
  - `May_2025/zad_27/Background_1.png`, `Background_2.jpg`, `flower_1.jpg`–`flower_7.jpg`
  - `May_2025/zad_28/info.txt`, `logo.png`, `Ornament.png`, `salad1.png`, `salad2.png`, `soup1.png`, `soup2.png`
- notes: the only source whose ZIP is already unpacked on disk. The unpacked tree under `data/reference/Май 25/May_2025/` is read-only reference; it is **not** under `data/assets/exams/may_2025_v2/`, so any future import step that links assets should either copy/reference these files from `data/reference/` or relocate them under `data/assets/exams/may_2025_v2/` (decision deferred to a later PR — do not move files now). Folder naming uses `zad_NN/` (Bulgarian-language `zad...` for `задача`).

### Naming pattern summary

The folder/file conventions inside the resource ZIPs are inconsistent across years:

- 2022–2023 (both seasons): top-level `task_26/`, `task_27/`, `task_28/`
- May 2024 v1: `task_NN_resources/` with a per-task subfolder
- Aug 2024 v2: outer ZIP with per-task PDFs and **nested** `task_NN_resources*.zip` files
- May 2025 v2: `May_2025/zad_NN/`

A future practical-task batch format must accommodate these naming differences — either by recording the original relative path verbatim or by introducing a canonical normalised path layout (e.g. `data/assets/exams/<source_slug>/practical/task_<n>/`) and tracking both sides. This is a representation question, not a Part 1 importer change.

## 4. Data/import plan

Practical tasks must NOT reuse the Part 1 JSON import format (`docs/dzi_question_import_format.md`). That format is intentionally narrow (MC + short-answer + optional asset references) and the importer's `task_number` range is hard-capped at 1..25. Practical tasks need a separate workflow.

> The data/import staging below covers data preparation only. The end-to-end student/teacher workflow (download → local work → upload → teacher review) is documented in sections 7–13 below, and the consolidated implementation PR sequence lives in section 13. Treat section 13 as the canonical roadmap; section 4 below describes the data-layer prerequisites.

- **Data-layer staging A — Resource inventory and file-presence verification.** Read-only sweep of every resource ZIP, confirming each task has the expected files. Output is a docs update or a small read-only audit script. No DB writes. Inner-ZIP extraction needed for `aug_2024_v2`.
- **Data-layer staging B — Define practical-task import format / representation.** Design a new JSON schema (e.g. `dzi_practical_task_import_format.md`) covering work environment, instructions, expected output files, grading rubric, resource asset list, and optional manual-grading metadata. Discuss whether/how it maps onto existing `practical_tasks`, `assets`, and `asset_links` tables, and whether new fields/tables are required.
- **Data-layer staging C — Reviewed practical-task batch files.** Author per-source reviewed JSON batches for tasks 26–28 mirroring the Part 1 review process. No DB writes.
- **Data-layer staging D — Planned DB import.** Run the new validator and importer, with the same dry-run-first discipline used for Part 1. May modify `data/questions.db` intentionally. No app changes.

Practical tasks likely do **not** fit the current MC/open auto-gradable model and may require:

- asset-linked task rows with multiple resource files per task
- manual or rubric-based grading (no auto-grade), or partial auto-checks (e.g. file existence, MIME, hash of expected outputs)
- structured project/task instructions, not a short prompt
- an attachment-aware student workflow for submitting `*.xlsx`, image, and archive deliverables
- different scoring semantics — task 26 is 15 points, task 27 is 20 points, task 28 is 20 points (totalling 55, matching the Part 2 share of the official 100-point exam)
- explicit time-allocation metadata (Part 2 is 150 minutes vs. Part 1's 90)

The new representation should also keep `is_ai_generated = 0` and `quality_score = 1.0` invariants once reviewed, consistent with Part 1 imports.

## 5. Risks

- **Missing resource files.** Some ZIPs may not contain every expected per-task folder. `aug_2022_v2` already shows no `task_27/` folder. PR A must enumerate gaps before any batch design.
- **ZIP contents not extracted yet.** All resource files currently live only inside ZIPs (except `may_2025_v2`, which is already extracted in-place but under `data/reference/`, not `data/assets/`). Extraction policy is undecided and out of scope for this PR.
- **Nested ZIPs (`aug_2024_v2`).** The outer archive contains per-task `*_resources*.zip` files that have not been opened. Their internal layout is unknown.
- **Asset path drift.** If/when assets are copied into `data/assets/exams/<source_slug>/`, the official relative paths (e.g. `task_28/Web_ chicken/Recept.docx`) must be preserved verbatim or recorded with an explicit mapping. Renaming, transliterating, or "tidying" Cyrillic/space-bearing names will break parity with the official exam.
- **Cyrillic / encoding issues.** Several ZIPs use Cyrillic folder names; one (`aug_2024_v2`) returns mojibake under `zipinfo`. Any tooling must handle UTF-8 names safely and avoid silent re-encoding.
- **Visual-/file-dependent prompts.** Most practical tasks reference embedded screenshots, provided documents, and image files that the prompt assumes the student can see/use locally. Auto-grading is unlikely to apply.
- **Different scoring semantics for tasks 26–28.** Points and time allowances differ from Part 1. Reusing Part 1 quiz UI / grading code without adjustment would mis-score these tasks.
- **No guessing or reconstructing files.** If a resource is missing, the practical-task batch for that source must stop and report — never substitute a similar file.
- **Accidental DB changes.** The Part 1 importer must not be invoked for any practical-task JSON; the importer already enforces `1 <= task_number <= 25`, but the new workflow should keep this barrier explicit and rejection-tested.
- **Asset uniqueness.** `assets.local_path` is the keying column for `assets`. If multiple sources happen to share filenames (e.g. two `logo.png` files), the local_path must include the source-specific subdirectory.

## 6. Checklist table by source

Statuses for `resources present?` reflect *outer ZIP presence and basic listing*, not deep verification. `q_links_26_28 status` is the current `exam_task_questions` link count for tasks 26–28 from `audit_dzi_state.py`.

| source_slug | official PDF path | resource folder / ZIP path | resources present? | q_links_26_28 status | task 26 notes | task 27 notes | task 28 notes | next action |
|---|---|---|---|---|---|---|---|---|
| aug_2022_v2 | `data/reference/dzi/official_pdfs/aug_2022_v2_exam.pdf` | `data/reference/Август 22/ДЗИ-ИТ-Август-2022-Ресурси.zip` | partial | 0 / 3 | `task_26/Onlinemag.xlsx` present | **no `task_27/` folder in ZIP — investigate** | `task_28/WebСompany/face_1..3.jpg` present | PR A: confirm whether task 27 has no provided assets or whether the archive is incomplete |
| may_2022_v1 | `data/reference/dzi/official_pdfs/may_2022_v1_exam.pdf` | `data/reference/Май 22/ДЗИ-ИТ-Май-2022-Ресурси.zip` | yes | 0 / 3 | `task_26/zad.26_NOIT.xlsx` | `task_27/alaska-iceburg-1515457.jpg`, `bear.jpg` | `task_28/Web_ chicken/Recept.docx`, `chicken-shashlik.jpg` (folder name has space) | PR A: file-presence + path-preservation check |
| aug_2023_v2 | `data/reference/dzi/official_pdfs/aug_2023_v2_exam.pdf` | `data/reference/Август 23/ДЗИ-ИТ-Август-2023-Ресурси.zip` | yes | 0 / 3 | `task_26/Zoomag.xlsx` | `task_27/*-unsplash.jpg`, `pizzas.docx` (6 files) | `task_28/bedroom.png`, `living.png`, `kitchen.png`, `furniture.jpg` | PR A: file-presence check |
| may_2023_v2 | `data/reference/dzi/official_pdfs/may_2023_v2_exam.pdf` | `data/reference/Май 23/ДЗИ-ИТ-Май-2023-Ресурси.zip` | yes | 0 / 3 | `task_26/Icecream.xlsx` | `task_27/Earth/Picture_1..14.jpg` (10 files; non-contiguous) | `task_28/BG/animals.jpg`, `map.png`, `text.docx`, `logo.png`, `mountain.jpg` | PR A: file-presence check; confirm Picture numbering matches PDF |
| aug_2024_v2 | `data/reference/dzi/official_pdfs/aug_2024_v2_exam.pdf` | `data/reference/Август 24/Август-2024-г.zip` | yes (outer) — **nested inner ZIPs not opened** | 0 / 3 | `it_23.08.2024_ zad26.xlsx` + `task_26 (2).pdf` + `task_26_resources (2).zip` | `task_27 (2).pdf` + `task_27_resources (2).zip` | `task_28 (1).pdf` + `task_28_resources (2).zip` | PR A: open inner ZIPs with read-only tools and record their contents; resolve mojibake folder name |
| may_2024_v1 | `data/reference/dzi/official_pdfs/may_2024_v1_exam.pdf` | `data/reference/Май 24/dzi-it-may-2024.zip` | yes | 0 / 3 | `task_26_resources/Choices.xlsx` | `task_27_resources/Images_flyer/*.jpg` (16 files) | `task_28_resources/PZ_3_Resources/28-text.docx`, 4 `*.png`, 1 `bee*.png` set | PR A: file-presence check; note alternate `task_NN_resources/` naming |
| may_2025_v2 | `data/reference/dzi/official_pdfs/may_2025_v2_exam.pdf` | `data/reference/Май 25/May_2025 (1).zip` (already extracted to `data/reference/Май 25/May_2025/`) | yes (ZIP + extracted) | 0 / 3 | `zad_26/Shipments.xlsx` | `zad_27/Background_1.png`, `Background_2.jpg`, `flower_1..7.jpg` | `zad_28/info.txt`, `logo.png`, `Ornament.png`, `salad1..2.png`, `soup1..2.png` | PR A: decide whether to leave extracted tree under `data/reference/` or copy to `data/assets/exams/may_2025_v2/`; do not move yet |

## 7. Student flow

Practical tasks are not auto-gradable quizzes. The student-facing flow is closer to a take-home assignment with file deliverables. The site must support:

1. **Open practical assignment/task page.** Student navigates from their dashboard / open assignment list to the practical task. Task 26/27/28 each render as their own page or sub-route, scoped to the assignment + attempt + student identity.
2. **Read task instructions on the site.** The full official task text (Bulgarian) is shown inline with any embedded screenshots/figures preserved. Sub-bullets, code blocks, and example tables should remain readable. Time-allowance reminder (Part 2 = 150 minutes) shown on the page.
3. **Download required official resource files.** The page lists every resource file linked to the task with its original (Bulgarian/Cyrillic-safe) filename, file size, and a download button. Files are streamed from a server route, never from a raw filesystem URL.
4. **Work locally.** The student opens the downloaded resources in the appropriate software (e.g. LibreOffice Calc / MS Excel for task 26, raster/vector graphics editor for task 27, browser + plain-text editor for task 28). Local work is outside the site's scope.
5. **Upload created output files.** The page accepts one or more file uploads, with a clear hint about expected output (e.g. `*.xlsx`, `*.png`, `.zip` archive of the website folder). The student can replace or add files before final submission.
6. **Submit for teacher/admin review.** A clearly labelled "Submit" action freezes the current upload set, records a submission timestamp, and marks the practical attempt as awaiting review. Subsequent edits, if allowed at all, should create a new submission revision rather than silently overwrite the prior one.

The student should never have to upload to a shared drive, email, or external service. The site is the source of truth for both the resources they download and the deliverables they upload.

## 8. Teacher/admin flow

Teachers/admins need a per-student review surface analogous to the existing assignment-results pages, but with file-aware fields:

1. **List practical submissions per student.** From the assignment results page, the teacher sees which students have submitted practical tasks 26/27/28 and which are pending.
2. **Download uploaded files.** Each submission shows the uploaded file set with original filenames, sizes, and download links. The teacher can pull the bundle (or individual files) to review locally.
3. **Review manually.** The grading is judgmental — visual inspection of the produced spreadsheet/graphic/website, comparing to the rubric in the official source key. No automatic correctness checking.
4. **Enter score/note.** The teacher records a numeric score (bounded by official maximum: 15 for task 26, 20 for task 27, 20 for task 28) and an optional Bulgarian note explaining the score. The score is **manual**, never auto-derived.
5. **Practical score is manual, not auto-graded.** Existing scoring code paths that auto-score MC and open answers must not touch practical-task rows. Until a combined practical-score design is approved, the manual practical score is displayed and stored separately from the Part 1 auto-score.

Admin-only operations (e.g. re-opening a frozen submission, deleting a faulty upload) should reuse existing admin auth and be auditable.

## 9. Resource/download requirements

Download links must be safe, deterministic, and traceable to the imported task:

- Resource files must be linked from **repo-managed/imported paths only** (e.g. files under `data/assets/exams/<source_slug>/practical/...` after PR B's import). The student-facing download route must look up the file via an internal asset id, not via a path passed from the client.
- Download URLs must not expose arbitrary filesystem paths — no `?path=` style parameters that accept user input. Use an asset-id route like `/dzi/practical/asset/<asset_id>` that the server resolves to `assets.local_path`.
- Preserve official filenames where possible. The `Content-Disposition: attachment; filename*=UTF-8''…` header should carry the original (Cyrillic-safe) name. Internal storage names can differ; user-visible names should not.
- Validate that referenced files exist before showing download links. If a referenced asset is missing on disk, render the link as disabled with a clear "файлът липсва" message — never 500. The Part 1 importer already records `sha256`, `mime_type`, and `file_size`; the practical-task importer should do the same.
- Downloads should require the same auth as the rest of the assignment flow (logged-in student tied to that attempt, or teacher/admin with explicit access). No public download routes for official exam resources.
- Set conservative cache headers (`Cache-Control: private, no-store`) to discourage offline mirroring and accidental leakage.

## 10. Upload requirements

Student uploads are user-generated content and must be handled with stricter discipline than the official resource files:

- **Storage location.** Uploads live **outside source-controlled folders** (e.g. `data/uploads/practical/<assignment_id>/<attempt_id>/<task_number>/`) and **must not be committed**. Add the upload root to `.gitignore` as part of the future implementation PR.
- **Validate file size.** Per-file and per-submission size caps must be enforced server-side (e.g. 25 MB per file, 100 MB per submission as a starting point — tune from observed practical-task deliverable sizes).
- **Validate allowed extensions.** Per task kind: task 26 → `xlsx`, `xls`, `ods`, `csv`; task 27 → `png`, `jpg`, `jpeg`, `tif`, `psd`, `svg`, `pdf`, `zip`; task 28 → `html`, `htm`, `css`, `js`, `zip`. Check both extension and MIME (best-effort) — reject mismatches.
- **Prevent path traversal.** Never use the client-provided filename in any filesystem path. Generate the internal name (e.g. UUID + extension) and store the original filename only in DB metadata.
- **Preserve original filename as metadata.** Keep `upload_original_filename` for display in teacher review and in `Content-Disposition` when the teacher re-downloads.
- **Associate uploads with the full context.** Each upload row carries `assignment_id`, `attempt_id`, `student_id`, `exam_task_id` (or `practical_task_id`), `submission_id`, plus storage path and integrity fields (sha256, file size, MIME, uploaded_at).
- **Allow multiple files if the official task expects more than one output.** Tasks 27 (graphics flyer + ZIP archive) and 28 (full website archive) frequently expect multiple deliverables. The model must not assume 1-file-per-task.
- **Reject suspicious inputs.** Disallow zero-byte uploads. Strip/ignore executable extensions outright. Do not trust the `Content-Type` header alone.
- **Auditability.** Record uploader user_id and source IP/user-agent on each upload row. Soft-delete rather than hard-delete to preserve teacher review history.

## 11. Data/model implications

The current schema has `practical_tasks` (a per-task metadata row), `assets`, and `asset_links` tables (see `docs/dzi_question_import_format.md` and `audit_dzi_state.py`). Practical-task support will likely require additional tables — design only, **do not implement here**:

- `practical_task_resources` — links a practical task to one or more resource files. Columns plausibly: `id`, `task_id` (FK to `exam_tasks`), `asset_id` (FK to `assets`), `display_order`, `display_name_bg`, `is_required`. Could reuse `asset_links` with a new `owner_type='practical_task'` and a `role='resource'` instead of a new table — pick one in PR B.
- `practical_submissions` — one row per student submission per task. Columns plausibly: `id`, `attempt_id` (FK to `quiz_attempts`), `student_id`, `task_id`, `submission_number`, `submitted_at`, `status` (`draft` / `submitted` / `under_review` / `graded`), `manual_score`, `manual_score_max`, `teacher_note_bg`, `graded_by`, `graded_at`.
- `practical_submission_files` — one row per uploaded file. Columns plausibly: `id`, `submission_id`, `original_filename`, `stored_path`, `mime_type`, `file_size`, `sha256`, `uploaded_at`, `uploader_user_id`, `soft_deleted_at`.
- **Relation to existing tables.** `quiz_assignments` and `quiz_attempts` already model the assignment + attempt grain. Practical submissions hang off `quiz_attempts.id`, similar to how MC answers do, but with a separate auto/manual scoring path. `exam_tasks.id` already exists for tasks 26–28 with `task_kind` values `practical_spreadsheet`, `practical_graphics`, `practical_web`, so per-task identity is already encoded — no new task identity table is required.
- **Optional: teacher score history.** If grading revisions are expected (e.g. a teacher re-grades after a student dispute), add `practical_submission_grades` keyed by `submission_id` to keep an append-only history rather than overwriting `manual_score`.

These are sketches for PR B/C discussion, not commitments. The minimum viable design (one submission row + one files table) is enough for a first end-to-end pass.

## 12. Scoring implications

- **No auto-scoring for practical tasks.** Practical tasks must not enter the MC/open auto-score path. The auto-scorer should explicitly skip rows whose `exam_tasks.task_kind` starts with `practical_`.
- **Display manual score separately.** Until a combined-score design is approved, the practical manual score is shown as a separate line on the result page (e.g. "Част 1: 25 / 25 точки" and "Част 2 (ръчно): 38 / 55 точки"), not summed into the Part 1 total.
- **Do not mix practical tasks into existing mixed/open quiz flow.** The current `open_count`-driven mixed assignment creation produces auto-gradable Part 1 quizzes. Adding practical tasks to that mix would break the auto-grade contract and the inline `quiz/<id>` UI. Practical tasks need their own assignment type, route, and UI before they can be combined with Part 1 in a single student view.
- **Score ceilings come from the official key.** Task 26 = 15, task 27 = 20, task 28 = 20. The grading UI must clamp inputs to the per-task max. Optional partial-credit sub-rubrics can be modelled in `grading_criteria_json` on `practical_tasks` if PR B decides to record rubrics.
- **Final grade composition (out of scope for this PR).** Whether to expose a combined "100 точки" final view or keep Part 1 / Part 2 separate is a teacher/UX decision; design it explicitly in the implementation PRs (likely PR F).

## 13. Recommended implementation PR sequence

This sequence supersedes the older A–D plan in section 4. Each PR must remain narrow and reversible; each ends with a clear validation step.

- **PR A — Validator support for practical-task JSON / resource references.** Add a `src/validate_practical_task_batch.py` (or extend the existing validator) that loads the new practical-task JSON, verifies referenced resource files exist on disk, checks asset path safety, and rejects practical task numbers outside 26–28. Read-only DB usage with `mode=ro`. Dry-run only.
- **PR B — Reviewed practical-task JSON for one source.** Author the first reviewed practical-task batch (start with `may_2025_v2`, since its resources are already extracted on disk). One file per task: e.g. `data/import_batches/practical/may_2025_v2_task_26.json`, `..._task_27.json`, `..._task_28.json`. No DB writes. Validate against PR A.
- **PR C — DB/schema support for resources/submissions.** Add additive migration(s) for `practical_task_resources` (or new `asset_links` role), `practical_submissions`, and `practical_submission_files`. Strictly additive: no dropping, no rename. Includes any necessary indexes and FK constraints. No app code change beyond what the migration requires.
- **PR D — Render practical task + download resources.** New route(s) under `/dzi/practical/...` (or `/quiz/<id>/practical/<task>`) that render the task text and list its resources with safe asset-id-based download links. Read-only — no uploads yet. Reuses existing student auth.
- **PR E — Upload completed files.** Add the upload endpoint, size/extension/MIME validation, server-side rename + storage outside source-controlled folders, and submission-state transitions (`draft` → `submitted`). Include CSRF protection and same-origin checks consistent with existing auth.
- **PR F — Teacher review / manual grading.** Add the teacher-facing list and detail pages for practical submissions, the manual score + note form, score-ceiling clamping, and the separate practical-score display on the student result page. No auto-grading of practical tasks.

Soft prerequisites: each PR's tests must cover the new behaviour without weakening existing Part 1 quiz tests. CI run, FK check, and the standard `audit_dzi_state.py` / `audit_open_question_readiness.py` audits must remain clean throughout.

## Out of scope for this PR

- Editing `data/questions.db`.
- Running migrations.
- Extracting, unzipping, copying, moving, or renaming any resource file.
- Creating `data/assets/exams/<source_slug>/` directories.
- Defining the practical-task JSON schema (deferred to implementation PR A / data-layer staging B).
- Authoring practical-task batch files (deferred to implementation PR B / data-layer staging C).
- Implementing the student download UI (deferred to implementation PR D).
- Implementing student uploads (deferred to implementation PR E).
- Implementing teacher manual grading (deferred to implementation PR F).
- Importing practical tasks (deferred to data-layer staging D, after the workflow PRs land).
- Any change to Python, templates, tests, CSS, JS, scoring, routes, or schema.
