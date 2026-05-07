---
title: "LearnPilot DZI Expansion Status"
type: dzi_status
tags: [dzi, expansion, status]
---

# LearnPilot DZI Expansion Status

## Goal

Expand LearnPilot's **Подготовка за матура** into a real DZI preparation engine.

## 2026-05-07 aug_2024_v2 Part 1 imported

- Import batches completed:
  - tasks 1–5
  - tasks 6–10
  - tasks 11–15
  - tasks 16–20
  - tasks 21–25
- `q_links_1_25 = 25`
- Status: `PART1_IMPORTED`
- Overall DZI source state: 2 imported, 5 ready, 0 attention.
- `PART1_IMPORTED` sources:
  - `may_2025_v2`
  - `aug_2024_v2`
- Known asset/fidelity gaps for `aug_2024_v2`:
  - task 3 likely needs database Design view visual
  - task 9 likely needs web page schematic image
  - task 16 spreadsheet/table context may need visual asset
  - task 17 chart-dependent
  - task 19 pivot-table visual-dependent
  - task 21 database table visual likely needed
- Asset audit snapshot recorded in [[dzi-asset-mapping-plan|DZI Asset Mapping Plan]].
- Part 2/practical tasks 26–28 are not imported as questions yet.

## 2026-05-07 live tester progress

- LearnPilot branding is active.
- Warm dark UI palette applied.
- Homepage dashboard added.
- Quiz answer cards and result page improved.
- Tester password now allows normal test creation.
- Admin-only DZI inspection remains protected.
- Section pages now show **Преговорен режим**.
- Advanced review controls are collapsed under **Опции за преглед**.
- DZI prep now has clearer **Преглед / Създай тест** flow.
- New quiz attempts filter invalid old-bank questions:
  - missing correct answers
  - invalid MC options
  - visual-dependent questions without usable image/asset
- Result page can show skipped invalid question count when needed.
- May 2025 v2 Part 1 is imported: 25 linked questions.
- DZI pool wording clarified:
  - 25 total imported Part 1
  - 15 MC quiz-ready
  - 10 short-answer/not-yet-quiz-ready

Known gaps:

- DZI visual assets/images still not attached for some tasks.
- Mixed open/closed quizzes are not implemented yet.
- Real deployment is not done; current tester sharing uses Cloudflare quick tunnel.
- Repo/folder rename from `dzi-generator` to LearnPilot is deferred.
- Planning notes added: [[mixed-open-closed-quiz-plan|Mixed Open/Closed Quiz Plan]] and [[dzi-asset-mapping-plan|DZI Asset Mapping Plan]].

## Exam format

- Part 1: 25 tasks, 90 minutes, 45 points
  - Tasks 1–15: multiple choice, 1 point each
  - Tasks 16–25: short/free answer, 3 points each
- Part 2: 3 practical tasks, 150 minutes, 55 points
  - Task 26: spreadsheets, 15 points
  - Task 27: computer graphics, 20 points
  - Task 28: web design, 20 points
- Total: 28 tasks, 100 points

## Completed

- DZI task/asset/blueprint schema added
- `dzi_it_pp_2025_format` blueprint seeded
- Official DZI skeletons imported for 2022–2025 PDFs
- Official PDFs inventoried as sources/assets/links
- Part 1 JSON import format documented
- Part 1 JSON importer created

## Source PDFs

Official PDFs are stored in:

`data/reference/dzi/official_pdfs/`

## Design decisions

- PDFs are source/reference.
- Reviewed JSON is the structured import path.
- No direct OCR-to-DB import.
- Images/files stay on disk, not inside SQLite.
- Practical tasks 26–28 will use a separate resource/rubric format later.

## Pending

- Import one real official exam Part 1 through reviewed JSON
- Add `/dzi` and `/dzi/source/<source_slug>` web inspection pages
- Add Obsidian notes for each official exam source
