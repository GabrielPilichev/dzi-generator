# Practical Task Schema / Import Plan — DZI Tasks 26–28

This is a docs-only planning checkpoint. It does **not** modify
`data/questions.db`, run migrations, import any data, create batch JSON files,
extract or move resource files, or add any UI/route. It defines the smallest
safe DB/schema path for the next implementation PRs covering official DZI Part 2
practical tasks (26, 27, 28).

The broader practical-task workflow lives in
[`dzi_practical_tasks_plan.md`](./dzi_practical_tasks_plan.md). This document
narrows that plan into a concrete schema/import recommendation that the next
implementation PR can pick up.

## 1. Current state

- All seven tracked DZI Part 1 batches are imported. `audit_dzi_state.py` shows
  every official source as `PART1_IMPORTED` (28 `exam_tasks` rows, 100 points,
  3 practical-task skeleton rows, no missing assets, no foreign-key check
  failures).
- `q_links_26_28 = 0` for every source. The `exam_tasks` skeleton already
  contains rows for tasks 26, 27, 28 with `task_kind` values
  `practical_spreadsheet`, `practical_graphics`, `practical_web` and matching
  `practical_tasks(task_id, work_environment)` rows, but no
  `exam_task_questions` links and no resource-asset links exist yet.
- `may_2025_v2` has a reviewed practical-task JSON batch on disk at
  `data/import_batches/may_2025_v2_practical_tasks_26_28.json`. Resource files
  for that source are already extracted in-place under
  `data/reference/Май 25/May_2025/` (`zad_26/`, `zad_27/`, `zad_28/`).
- `src/validate_practical_task_batch.py` validates that batch as a dry-run with
  `mode=ro`, enforces `task_number ∈ {26, 27, 28}`, requires
  `grading_mode == "manual"`, requires `task_kind` to match the existing
  `exam_tasks.task_kind`, and verifies `resource_files` exist on disk inside
  the allowed roots `data/reference/` and `data/assets/`.
- No student upload/download UI exists yet. There is no `data/uploads/`
  directory, and `data/uploads/` is **not** currently listed in `.gitignore`.

## 2. Storage / design needs

Practical tasks 26–28 are different from Part 1 tasks. Any storage design must
cover:

- **Task instructions on the site.** Long Bulgarian instruction text per task,
  preserving sub-bullets, code snippets, and references to embedded figures
  from the official PDF. The existing `data/import_batches/...practical...json`
  already records this as `prompt_bg` + `instructions_bg`.
- **Official resource file references for downloads.** A list of one or more
  files per task that the student must be able to download (e.g.
  `Shipments.xlsx`, `flower_*.jpg`, `logo.png`). Files come from
  `data/reference/<source>/` (or, in future, `data/assets/exams/<source_slug>/practical/...`).
- **Future student-uploaded output files.** Multi-file per task (e.g. task 27
  outputs both a native graphics file and a `.png`, packed into a `.zip`).
- **Teacher/admin manual score and note.** Per-task numeric score (clamped to
  the official maximum: 15 for task 26, 20 for task 27, 20 for task 28) plus a
  free-text Bulgarian teacher note.
- **No auto-grading.** Practical tasks must never enter the existing MC / open
  auto-score path.

## 3. Recommended smallest schema path

Recommendation: **the next implementation PR should add three new tables
covering resources, submissions, and submission files**, and reuse the existing
`practical_tasks` row for the per-task instruction/grading metadata. No new
top-level "batches" table is needed — `data/import_batches/*.json` already
serves that role at the file layer, and the importer writes per-task rows
keyed by `exam_tasks.id`.

The recommended additive shape is below. **Do not implement in this PR.** Field
lists are sketches; PR A may rename or add columns as needed (e.g. SHA-256,
source-slug denormalisation, or display ordering).

### 3.1 `practical_task_resources` (new)

Links a practical task to one or more official resource files.

| column | type | notes |
|---|---|---|
| `id` | INTEGER PK | autoincrement |
| `exam_task_id` | INTEGER NOT NULL | FK → `exam_tasks(id)` ON DELETE CASCADE |
| `asset_id` | INTEGER NOT NULL | FK → `assets(id)` ON DELETE RESTRICT |
| `display_order` | INTEGER NOT NULL DEFAULT 0 | stable ordering for the UI |
| `display_name_bg` | TEXT NULL | optional Bulgarian label; falls back to `assets.original_filename` |
| `is_required` | INTEGER NOT NULL DEFAULT 1 | reserved for future "optional reference" assets |
| `created_at` | TEXT DEFAULT CURRENT_TIMESTAMP | |
| | UNIQUE | `(exam_task_id, asset_id)` |

The `assets` row carries `local_path`, `original_filename`, `sha256`,
`mime_type`, `file_size`, and `asset_type` (already present in the schema). The
download route resolves by `practical_task_resources.id` (or a join through
`exam_task_id`), never by raw client-supplied path. We do not need a separate
`source_slug` / `task_number` column on this table because both are reachable
through `exam_task_id → exam_tasks → exams`.

**Alternative considered.** Reuse `asset_links` with
`owner_type = 'practical_task'` and `role = 'resource'`. This avoids a new
table but couples display semantics (`display_name_bg`, `is_required`) into
`asset_links` which is currently a generic linker. PR A should pick one and
document the choice; the recommendation here is the dedicated table because
the per-task UI will lean on `display_name_bg` / `is_required` and adding
those columns to `asset_links` would broaden a shared table for a single
caller.

### 3.2 No `practical_task_batches` table

A `practical_task_batches` table is **not** recommended. Imports are tracked
file-side under `data/import_batches/` and the existing review/import logs in
`docs/reviews/`. Adding a DB-side batch table would duplicate that without a
caller. If audit is required later, the importer can write to a generic
`import_log` table (out of scope here).

### 3.3 Per-task instruction storage — reuse, do not add

Practical task instructions (the `prompt_bg` / `instructions_bg` fields in the
batch JSON) should land in the **existing** `exam_tasks.prompt` / `rubric`
columns rather than a new table:

- `exam_tasks.prompt` ← official short prompt (`prompt_bg` from the batch).
- `exam_tasks.rubric` ← long instruction body (`instructions_bg` from the
  batch). The column is already TEXT and unused for practical rows today.
- `practical_tasks.expected_outputs_json` ← `expected_outputs` from the batch.
- `practical_tasks.notes` ← reviewer-facing `notes` from the batch.
- `practical_tasks.grading_criteria_json` ← reserved for an optional rubric
  breakdown if PR B chooses to record one; otherwise leave NULL.

This avoids a new "practical task instructions" table, keeps task identity in
`exam_tasks`, and means the only structural change in the schema PR is adding
the three new tables below.

### 3.4 `practical_submissions` (new)

One row per student submission per task per attempt.

| column | type | notes |
|---|---|---|
| `id` | INTEGER PK | autoincrement |
| `quiz_attempt_id` | INTEGER NOT NULL | FK → `quiz_attempts(id)` ON DELETE CASCADE |
| `exam_task_id` | INTEGER NOT NULL | FK → `exam_tasks(id)` ON DELETE RESTRICT |
| `submission_number` | INTEGER NOT NULL DEFAULT 1 | bumped if a re-submit is allowed |
| `submitted_at` | TEXT NULL | NULL while `status='draft'` |
| `status` | TEXT NOT NULL | CHECK in (`'draft'`, `'submitted'`, `'under_review'`, `'graded'`) |
| `created_at` | TEXT DEFAULT CURRENT_TIMESTAMP | |
| | UNIQUE | `(quiz_attempt_id, exam_task_id, submission_number)` |

Grading fields are **not** kept on this row — they live on
`practical_submission_grades` (3.6) so a regrade history is preserved.

### 3.5 `practical_submission_files` (new)

One row per uploaded file. Stored outside source-controlled folders.

| column | type | notes |
|---|---|---|
| `id` | INTEGER PK | autoincrement |
| `practical_submission_id` | INTEGER NOT NULL | FK → `practical_submissions(id)` ON DELETE CASCADE |
| `stored_path` | TEXT NOT NULL UNIQUE | server-generated path under `data/uploads/practical/...` |
| `original_filename` | TEXT NOT NULL | as supplied by the student, for display only |
| `size_bytes` | INTEGER NOT NULL | |
| `mime_type` | TEXT NULL | best-effort sniff, not trusted |
| `sha256` | TEXT NULL | computed server-side after write |
| `uploaded_at` | TEXT DEFAULT CURRENT_TIMESTAMP | |
| `uploader_user_id` | INTEGER NULL | reserved for when student auth is wired through |
| `soft_deleted_at` | TEXT NULL | preserve teacher review history |

`stored_path` is generated server-side (e.g. UUID + extension); the client
filename is never used in the path.

### 3.6 `practical_submission_grades` (new)

Append-only per-grade event. Recommended over a single `manual_score` column
on `practical_submissions` so a teacher regrade does not silently overwrite
the prior score.

| column | type | notes |
|---|---|---|
| `id` | INTEGER PK | autoincrement |
| `practical_submission_id` | INTEGER NOT NULL | FK → `practical_submissions(id)` ON DELETE CASCADE |
| `score` | INTEGER NOT NULL | clamped server-side to `max_score` |
| `max_score` | INTEGER NOT NULL | mirror of `exam_tasks.points` at grade time |
| `teacher_note_bg` | TEXT NULL | free Bulgarian text |
| `reviewed_by` | TEXT NOT NULL | teacher/admin identifier; align with existing admin-auth identity |
| `reviewed_at` | TEXT DEFAULT CURRENT_TIMESTAMP | |

The student-facing display uses the most recent row per submission. PR F can
cap visible grades to "latest" while keeping history queryable.

### 3.7 What we are explicitly NOT adding now

- No `final_score` column anywhere mixing Part 1 + Part 2 totals.
- No new score columns on `quiz_attempts`.
- No "rubric criteria" table — `grading_criteria_json` on `practical_tasks` is
  enough until a UI needs structured per-criterion entry.
- No `student` table; student identity stays where it currently lives in
  `quiz_attempts.student_name` / existing auth.

## 4. Import model

The next importer (a separate PR; this doc does not write it) should:

- **Link practical task JSON to the existing `exam_tasks` rows for tasks 26,
  27, 28** for the resolved `source_slug`. It must not create new `exam_tasks`
  rows. The skeleton already carries `task_kind ∈ {practical_spreadsheet,
  practical_graphics, practical_web}` and the official `points`, and the
  validator already enforces both match the JSON.
- **Store one `assets` row per resource file**, keyed by `local_path`, populating
  `original_filename`, `mime_type`, `file_size`, `sha256`, and an
  `asset_type` of `spreadsheet` / `image` / `archive` / `other` per
  `dzi_question_import_format.md`.
- **Insert one `practical_task_resources` row per (exam_task_id, asset_id)**
  pair, in the order the JSON lists them, using `display_order = index` and
  optionally a `display_name_bg` derived from the original filename when no
  explicit label is provided.
- **Write task instructions into `exam_tasks.prompt` / `exam_tasks.rubric` and
  `practical_tasks.expected_outputs_json` / `practical_tasks.notes`**, as
  described in 3.3. No new table for instructions.
- **Run for one source at a time** (start with `may_2025_v2`, since its
  resources are already extracted and the reviewed JSON is already on disk).
- **Stay strictly out of `questions` and `exam_task_questions`.** Practical
  tasks are not `questions`-table rows. The Part 1 importer's existing 1–25
  `task_number` cap continues to protect that boundary; the practical importer
  must enforce its own 26–28 cap symmetrically (the validator already does).
- **Avoid adding practical tasks to MC/open mixed quiz selection** until the
  upload + scoring UI lands. The existing `open_count`-driven mixed assignment
  picker assumes auto-gradable rows; including practical rows would silently
  break the auto-grade contract. The selector should explicitly skip rows
  whose `exam_tasks.task_kind` starts with `practical_` until PR F.
- **Be re-run-safe.** Re-importing the same practical batch should be an
  upsert on `practical_task_resources` keyed by `(exam_task_id, asset_id)` and
  a no-op on instruction text if unchanged. If a referenced resource has
  disappeared from disk, the importer must reject the file rather than
  silently delete the link row.

## 5. Download / upload security implications

The download and upload paths are user-facing surfaces and need the same
discipline already applied in `validate_practical_task_batch.py`.

**Downloads (official resources):**

- **Resolve by internal id, not raw path.** The student download route is
  `/dzi/practical/resource/<practical_task_resources.id>` (or equivalent). It
  joins to `assets.local_path` server-side. There is no `?path=` query
  parameter and no client-supplied filename in the lookup.
- **Resource paths must stay inside allowed repo-managed dirs.** The validator
  already enforces `data/reference/` and `data/assets/` only; the download
  handler must apply the same `is_under_allowed_root` check before opening
  the file, defending against any DB row that was inserted before the check
  existed.
- **Path traversal protection.** `..` segments must be rejected at validate
  time and re-checked at serve time. Absolute paths in `assets.local_path`
  must be rejected.
- **File-existence / file-type guards.** The download handler must `stat` the
  file before sending. If missing, render a disabled "файлът липсва" link, not
  a 500. Set `Content-Disposition: attachment; filename*=UTF-8''…` with the
  original Cyrillic-safe filename.
- **Auth.** Reuse the assignment/attempt auth already in place for quiz
  routes. No public download URLs for official resources. Set
  `Cache-Control: private, no-store`.

**Uploads (student-produced files):**

- **Stored outside source-controlled folders.** Recommended root:
  `data/uploads/practical/<assignment_id>/<attempt_id>/<task_number>/`.
- **`data/uploads/` must be added to `.gitignore`.** It is not currently
  ignored. Add it as part of the schema PR (PR A) so a stray `git add` cannot
  commit student work. The repo also already gitignores
  `questions.backup-*.db`, so prior art exists.
- **File extension / size / MIME validation** is deferred to the upload PR
  (PR D below) but must be present before the endpoint is exposed. Per-file
  and per-submission caps need server-side enforcement.
- **Path traversal protection.** Never use the client filename in any
  filesystem path. Generate a server-side stored name (UUID + sanitised
  extension) and keep the original filename only in the
  `practical_submission_files.original_filename` column.
- **No arbitrary filesystem access.** Upload handlers must write only inside
  the upload root, never follow symlinks, and must reject zero-byte and
  oversized payloads. Disallow executable extensions outright.

## 6. Recommended PR sequence after this doc

This is the planned sequence after this docs PR merges. Each PR remains
narrow and reversible.

- **PR A — Migration / schema for practical task resources only, no UI.** Adds
  the additive migration creating `practical_task_resources`,
  `practical_submissions`, `practical_submission_files`, and
  `practical_submission_grades` (or a subset if PR scope is split further).
  No app code beyond what the migration requires. Adds `data/uploads/` to
  `.gitignore`. Includes FK + unique-index definitions and a foreign-key
  check in the test suite. No DB writes outside the migration itself.
- **PR B — Practical task importer for resource metadata + instructions, one
  source only (`may_2025_v2`).** Reads the existing reviewed batch JSON,
  validates it via the existing dry-run validator, then writes `assets` +
  `practical_task_resources` rows and populates `exam_tasks.prompt` /
  `exam_tasks.rubric` / `practical_tasks.*` for tasks 26–28 of `may_2025_v2`.
  Re-run-safe upsert. Read/write `data/questions.db`. No UI.
- **PR C — Render practical task instructions + resource downloads.** Adds
  the read-only student-facing route(s) that show task text and offer
  asset-id-based download links (no uploads, no grading). Reuses existing
  student auth.
- **PR D — Upload student files.** Adds the upload endpoint + storage outside
  source-controlled folders, file/MIME/size validation, server-side rename,
  and `draft → submitted` state transition.
- **PR E — Teacher / admin review and manual grading.** Adds the teacher list
  + detail pages, manual score + note form (clamped to per-task max),
  history-preserving writes to `practical_submission_grades`, and a separate
  practical-score line on the student result page (no auto-mix into the
  Part 1 total yet).

This is a slight relabelling of the sequence in
`dzi_practical_tasks_plan.md` §13 (which uses A–F counting the validator as
PR A). Both sequences agree on the order; this doc starts numbering from the
schema PR because the validator (`src/validate_practical_task_batch.py`) and
the first reviewed batch (`may_2025_v2_practical_tasks_26_28.json`) are
already merged.

## 7. Explicit non-goals

This planning PR does not, and the next schema PR (PR A) should not:

- Make any DB changes in this docs PR.
- Modify `data/questions.db`.
- Add upload or download UI.
- Implement the student submission flow.
- Integrate practical scores into the final quiz score.
- Add practical tasks to the mixed / open assignment picker.
- Author or modify any practical-task batch JSON files.
- Unzip, extract, copy, move, or rename any resource file.
- Touch Python sources, templates, tests, CSS, or JS.
- Run migrations.
- Commit, push, or merge.
