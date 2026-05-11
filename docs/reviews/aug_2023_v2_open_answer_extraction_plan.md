# aug_2023_v2 Open Answer Extraction Plan

## Scope

- Source slug: `aug_2023_v2`
- Official PDF: `data/reference/dzi/official_pdfs/aug_2023_v2_exam.pdf`
- Scope: Part 1 tasks 16-25 only
- Target output after manual review: `data/import/dzi/aug_2023_v2_part1_tasks_16_20.json` and `data/import/dzi/aug_2023_v2_part1_tasks_21_25.json`, or one equivalent reviewed batch file.

## Current Findings

- `aug_2023_v2` open questions: 10
- `fill_in_subquestions`: 9
- Questions missing subquestion rows: 7
- Subquestions missing `correct_answer`: 0
- Audit candidates: 1
- Visual-dependent tasks by audit pattern: 3
- No reviewed/extracted JSON batch currently exists under `data/import/dzi/`.

Existing DB rows for tasks 19, 20, and 23 have some answer text, but they still need manual confirmation against official/source material before a complete reviewed batch is prepared.

## Safety Rules

- Do not guess answers.
- Do not use OCR as source of truth.
- Accepted answers must be manually confirmed from official/source material.
- DB import must happen in a later planned DB/data PR only after review.
- Do not import into `data/questions.db` while preparing this review scaffold.

## Task Checklist

| Task | Current status | Manual action needed | Notes |
|---|---|---|---|
| 16 | Existing DB question row; no usable subquestion rows or accepted answers | Manually extract prompt, subquestion structure, official accepted answers, topic/section metadata | Special attention item |
| 17 | Existing DB question row; no usable subquestion rows or accepted answers | Manually extract prompt, subquestion structure, official accepted answers, topic/section metadata | Special attention item |
| 18 | Existing DB question row; no usable subquestion rows or accepted answers | Manually extract prompt, subquestion structure, official accepted answers, topic/section metadata | Special attention item |
| 19 | Existing DB question row with some answer text | Manually confirm prompt, all accepted answers, alternatives, and whether visual/source context is required | Existing answer text is not enough for reviewed batch approval |
| 20 | Existing DB question row with some answer text | Manually confirm prompt, all accepted answers, alternatives, and topic/section metadata | Existing answer text is not enough for reviewed batch approval |
| 21 | Existing DB question row; no usable subquestion rows or accepted answers | Manually extract prompt, subquestion structure, official accepted answers, topic/section metadata | Special attention item |
| 22 | Existing DB question row; no usable subquestion rows or accepted answers | Manually extract prompt, subquestion structure, official accepted answers, topic/section metadata | Special attention item |
| 23 | Existing DB question row with some answer text | Manually confirm prompt, all accepted answers, alternatives, and whether visual/source context is required | Existing answer text is not enough for reviewed batch approval |
| 24 | Existing DB question row; no usable subquestion rows or accepted answers | Manually extract prompt, subquestion structure, official accepted answers, topic/section metadata | Special attention item; audit pattern marks as visual-dependent |
| 25 | Existing DB question row; no usable subquestion rows or accepted answers | Manually extract prompt, subquestion structure, official accepted answers, topic/section metadata | Special attention item |

## Next Steps

1. Manually extract task text and accepted answers for tasks 16-25 from the official/source material.
2. Confirm alternatives, scoring points, and any visual dependency for each subquestion.
3. Create reviewed JSON batch file(s) under `data/import/dzi/` using the documented format in `docs/dzi_question_import_format.md`.
4. Validate each batch with:

   ```bash
   python3 src/validate_question_batch.py --json data/import/dzi/<batch-file>.json
   ```

5. Import into `data/questions.db` only in a separate planned DB/data PR after review approval.
