---
title: "DZI Expansion Status"
type: dzi_status
tags: [dzi, expansion, status]
---

# DZI Expansion Status

## Goal

Expand **Подготовка за матура** into a real DZI preparation engine.

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
