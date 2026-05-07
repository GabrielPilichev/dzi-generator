---
title: "DZI Asset Mapping Plan"
type: planning
tags: [learnpilot, dzi, assets, planning]
---

# DZI Asset Mapping Plan

## Known gap

May 2025 v2 Part 1 is imported, but visual assets/images are not fully attached.

Current audit shows exam-level asset status, but not per-task missing visual needs.

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
