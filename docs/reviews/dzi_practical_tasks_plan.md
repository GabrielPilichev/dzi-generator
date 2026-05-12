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

Practical tasks must NOT reuse the Part 1 JSON import format (`docs/dzi_question_import_format.md`). That format is intentionally narrow (MC + short-answer + optional asset references) and the importer's `task_number` range is hard-capped at 1..25. Practical tasks need a separate workflow:

- **PR A — Resource inventory and file-presence verification.** Read-only sweep of every resource ZIP, confirming each task has the expected files. Output is a docs update or a small read-only audit script. No DB writes. Inner-ZIP extraction needed for `aug_2024_v2`.
- **PR B — Define practical-task import format / representation.** Design a new JSON schema (e.g. `dzi_practical_task_import_format.md`) covering work environment, instructions, expected output files, grading rubric, resource asset list, and optional manual-grading metadata. Discuss whether/how it maps onto existing `practical_tasks`, `assets`, and `asset_links` tables, and whether new fields/tables are required.
- **PR C — Reviewed practical-task batch files.** Author per-source reviewed JSON batches for tasks 26–28 mirroring the Part 1 review process. No DB writes.
- **PR D — Planned DB import.** Run the new validator and importer, with the same dry-run-first discipline used for Part 1. May modify `data/questions.db` intentionally. No app changes.

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

## Out of scope for this PR

- Editing `data/questions.db`.
- Running migrations.
- Extracting, unzipping, copying, moving, or renaming any resource file.
- Creating `data/assets/exams/<source_slug>/` directories.
- Defining the practical-task JSON schema (deferred to PR B).
- Authoring practical-task batch files (deferred to PR C).
- Importing practical tasks (deferred to PR D).
- Any change to Python, templates, tests, CSS, JS, scoring, or routes.
