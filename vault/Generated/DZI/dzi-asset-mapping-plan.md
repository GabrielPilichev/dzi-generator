---
title: "DZI Asset Mapping Plan"
type: planning
tags: [learnpilot, dzi, assets, planning]
---

# DZI Asset Mapping Plan

## Known gap

May 2025 v2 Part 1 is imported, but visual assets/images are not fully attached.

Current audit shows exam-level asset status, but not per-task missing visual needs.

## First audit result — may_2025_v2

Command run:

`python3 src/audit_dzi_assets.py --source-slug may_2025_v2`

Result:

- total tasks audited: 28
- linked questions: 25
- visual-dependent linked questions: 1
- tasks with asset links: 0
- tasks with missing asset files: 0
- quiz-blocking visual gaps: 0

Interpretation:

- Tasks 1–15 are MC and currently not blocked by asset gaps.
- Task 16 is `fill_in` and visual-dependent, with `link_missing`, but not quiz-blocking.
- Tasks 26–28 have no linked questions yet, so asset audit cannot fully evaluate them yet.
- May 2025 v2 current generated MC tests are not blocked by missing assets.

Next step:

- No urgent asset patch for May 2025 v2 MC quizzes.
- Asset work should wait until open/fill-in support or practical tasks 26–28 are being modeled.
- When doing asset work, start with task 16 and practical tasks 26–28.

## aug_2024_v2 asset notes after Part 1 import

Likely visual tasks:

- 3: database Design view visual
- 9: web page schematic image
- 16: spreadsheet/table context
- 17: chart-dependent
- 19: pivot-table visual-dependent
- 21: database table visual

No asset files were added during import. Assets remain future work.

Current MC quiz generation should only be blocked by visual-dependent MC if eligibility detects it. Task 9 is the main MC visual candidate to inspect first.

## Proposed audit

Add a read-only audit script later:

`src/audit_dzi_assets.py`

The script should report per task:

- `task_number`
- `question_id`
- `question_type`
- whether prompt appears visual-dependent
- whether question/task has asset links
- whether linked files exist
- `asset_status`: `not_required`, `present`, `link_missing`, `file_missing`, `extra_unused`
- `quiz_blocking`: yes/no

## Triage rules

- Short-answer visual assets can wait if not used in MC quizzes.
- Pure text rewrite via reviewed JSON if visual dependency can be safely removed.
- Manual crop/extract when genuine visual is required.
- Link existing asset if file already exists.

## Hard rules

- No OCR into DB.
- No automated bulk cropping for now.
- No placeholder images.
- No bulk fix.
- Reviewed JSON/import path only for content/asset DB changes.

## Open questions

- Which DZI exam next.
- Whether PDFs exist in `data/reference/dzi/official_pdfs`.
- Whether there is any existing manual asset list.
- Whether current prompts were rewritten to drop visual dependence.
