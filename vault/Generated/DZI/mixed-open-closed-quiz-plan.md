---
title: "Mixed Open/Closed Quiz Plan"
type: planning
tags: [learnpilot, dzi, quiz, planning]
---

# Mixed Open/Closed Quiz Plan

Do not build yet. This is future design for mixed closed and open generated quizzes.

## Decision checkpoint before implementation

DB shape:

- Use an additive `quiz_text_answers` table later.
- Do not modify `quiz_answers` for open answers.
- Store raw student text and normalized student text if/when implemented.

Grading unit:

- Store and grade internally per blank/subquestion.
- UI can still group results by question.
- Need final decision later: DZI-style final score partial per blank vs all-or-nothing per task.

V1 grading:

- Auto-grade only.
- No teacher-review UI in v1.
- Future teacher override should be possible from the data model, but not built now.

Normalization:

- Trim whitespace.
- Collapse internal whitespace.
- Casefold.
- Unicode normalize.
- Handle smart quotes vs straight quotes.
- Do not strip Bulgarian diacritics in v1.
- Punctuation handling remains undecided.
- Cyrillic/Latin homoglyph handling remains undecided and risky for IT terms.

Order-independent tasks:

- Tasks like `aug_2024_v2` 20, 24, and 25 cannot be graded by naive per-slot accepted-set matching.
- Future grader needs set/multiset matching so repeated same answer does not get full credit.
- This is required before implementation.

Formula tasks:

- Tasks like `aug_2024_v2` task 18 need explicit accepted formula alternatives.
- Formula normalization/equivalence is not solved in v1 unless manually listed.

Do not build yet:

- No migration yet.
- No mixed quiz UI yet.
- No manual grading UI yet.
- No synonym/regex answer engine yet.

## Additive migration design

Design-only proposal. Do not run this migration yet.

```sql
CREATE TABLE quiz_text_answers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id INTEGER NOT NULL,
    question_id INTEGER NOT NULL,
    subquestion_id INTEGER,
    subquestion_number INTEGER NOT NULL,
    response_order INTEGER,
    raw_answer TEXT NOT NULL DEFAULT '',
    normalized_answer TEXT NOT NULL DEFAULT '',
    grading_mode TEXT NOT NULL DEFAULT 'ordered'
        CHECK (grading_mode IN ('ordered', 'order_independent')),
    accepted_answers_json TEXT NOT NULL DEFAULT '[]',
    matched_answer TEXT,
    is_correct INTEGER NOT NULL DEFAULT 0 CHECK (is_correct IN (0, 1)),
    points_awarded REAL NOT NULL DEFAULT 0,
    points_possible REAL NOT NULL DEFAULT 1,
    graded_at TEXT DEFAULT CURRENT_TIMESTAMP,
    grader_version TEXT,
    teacher_override INTEGER NOT NULL DEFAULT 0 CHECK (teacher_override IN (0, 1)),
    teacher_note TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (attempt_id) REFERENCES quiz_attempts(id) ON DELETE CASCADE,
    FOREIGN KEY (question_id) REFERENCES questions(id) ON DELETE CASCADE,
    FOREIGN KEY (subquestion_id) REFERENCES fill_in_subquestions(id) ON DELETE SET NULL,
    UNIQUE (attempt_id, question_id, subquestion_number)
);

CREATE INDEX idx_quiz_text_answers_attempt
    ON quiz_text_answers(attempt_id);

CREATE INDEX idx_quiz_text_answers_question
    ON quiz_text_answers(question_id);

CREATE INDEX idx_quiz_text_answers_attempt_question
    ON quiz_text_answers(attempt_id, question_id);

CREATE INDEX idx_quiz_text_answers_correct
    ON quiz_text_answers(attempt_id, is_correct);
```

Constraints and intent:

- `quiz_answers` remains the closed-question table for MC answers.
- `quiz_text_answers` stores one row per blank/subquestion, not one row per task.
- `raw_answer` preserves exactly what the student submitted.
- `normalized_answer` stores the v1 normalized comparison value.
- `accepted_answers_json` snapshots the accepted answers used at grading time so old attempts remain explainable if source answers change later.
- `subquestion_id` is nullable so historical attempts remain readable even if content is later repaired.
- `teacher_override` and `teacher_note` are reserved for future review, not part of v1 UI.

## Worked rows

### 1. MC question remains in `quiz_answers`

For a mixed attempt containing MC question `124`, the answer remains only in `quiz_answers`:

```text
quiz_answers
id | attempt_id | question_id | chosen_letter | is_correct
---|------------|-------------|---------------|-----------
1  | 5001       | 124         | Б             | 1
```

No `quiz_text_answers` row is created for this MC question.

### 2. Ordered fill-in question

For `aug_2024_v2` task 16, each blank has its own expected answer position:

```text
quiz_text_answers
attempt_id | question_id | subquestion_number | raw_answer | normalized_answer | grading_mode | accepted_answers_json       | is_correct | points_awarded | points_possible
-----------|-------------|--------------------|------------|-------------------|--------------|-----------------------------|------------|----------------|----------------
5001       | 131         | 1                  | 300 лв.    | 300 лв.           | ordered      | ["300", "300 лв."]          | 1          | 1              | 1
5001       | 131         | 2                  | 540        | 540               | ordered      | ["540", "540 лв."]          | 1          | 1              | 1
5001       | 131         | 3                  | 800        | 800               | ordered      | ["900", "900 лв."]          | 0          | 0              | 1
```

### 3. Order-independent fill-in question

For tasks like `aug_2024_v2` 20, 24, and 25, a correct answer can appear in any slot. Store one submitted row per blank, but grade against the task-level accepted multiset.

Example for task 20 accepted set: `клиент`, `рецепционист`, `мениджър на хотела`.

```text
quiz_text_answers
attempt_id | question_id | subquestion_number | raw_answer          | normalized_answer   | grading_mode       | accepted_answers_json                                      | matched_answer      | is_correct | points_awarded
-----------|-------------|--------------------|---------------------|---------------------|--------------------|------------------------------------------------------------|---------------------|------------|---------------
5002       | 135         | 1                  | клиент              | клиент              | order_independent  | ["клиент", "рецепционист", "мениджър на хотела"]          | клиент              | 1          | 1
5002       | 135         | 2                  | клиент              | клиент              | order_independent  | ["клиент", "рецепционист", "мениджър на хотела"]          |                     | 0          | 0
5002       | 135         | 3                  | рецепционист        | рецепционист        | order_independent  | ["клиент", "рецепционист", "мениджър на хотела"]          | рецепционист        | 1          | 1
```

The repeated `клиент` receives credit once only. The unmatched duplicate row stays incorrect even though its text is individually accepted.

## Scoring model

- `score_total` should count gradable blanks/subquestions plus MC questions.
- Each MC question contributes 1 gradable item through `quiz_answers`.
- Each fill-in blank contributes 1 gradable item through `quiz_text_answers`.
- `score_correct` should sum correct MC rows plus correct text-answer rows.
- UI can group blank-level rows under their parent question while still showing partial credit.
- DZI-style point totals can be mapped later, but v1 should keep internal scoring per blank for clarity and simpler grading.

## Set and multiset matching

Order-independent grading must not use naive per-slot accepted-set matching.

Recommended v1 algorithm:

1. Normalize all submitted answers for the question.
2. Normalize all accepted answers for the question.
3. Build a multiset of accepted normalized answers.
4. Iterate submitted rows in stable `subquestion_number` order.
5. If a submitted normalized answer exists in the remaining accepted multiset, mark the row correct and consume one accepted occurrence.
6. If the accepted occurrence was already consumed, mark the repeated answer incorrect.

This handles repeated identical answers without awarding full credit. If a future task has legitimate duplicate accepted answers, the accepted multiset can include that answer multiple times.

## Normalization decisions accepted

- Trim leading and trailing whitespace.
- Collapse internal whitespace.
- Casefold.
- Unicode normalize.
- Normalize smart quotes and straight quotes.
- Do not strip Bulgarian diacritics in v1.

## Decisions still open

- Whether final DZI-style scoring should remain per blank or become all-or-nothing per task in some views.
- Punctuation normalization, especially for formulas, CSS, URLs, and Bulgarian free text.
- Cyrillic/Latin homoglyph handling; this is risky for IT terms and should not be automatic without tests.
- Whether accepted answer data needs a first-class `answer_group` concept instead of JSON snapshots.
- How to display partially correct order-independent answers without confusing students.

## Formula-answer limitation

`aug_2024_v2` task 18 needs explicit accepted formula alternatives. V1 should not attempt formula equivalence or spreadsheet parsing. Only manually reviewed variants such as delimiter, quote, or locale differences should be accepted, and they must be listed explicitly.

## What this migration does not do

- It does not change `quiz_answers`.
- It does not change existing quiz scoring semantics.
- It does not implement mixed quiz generation.
- It does not implement the student open-answer UI.
- It does not implement teacher review or manual grading.
- It does not add synonym, regex, OCR, or formula-equivalence grading.
- It does not import any DZI questions or assets.
- It does not solve practical tasks 26–28.

## Design decisions

- Current quiz code assumes multiple-choice only.
- Teacher UI recommendation: two explicit count fields, `closed_count` and `open_count`.
- Student UI recommendation: one text input per `fill_in_subquestion`.

## Implementation order

1. Migration for `quiz_text_answers`.
2. Normalization helper + tests.
3. Open eligibility helper.
4. `teacher_new` `open_count` field default `0`.
5. Mixed question picker.
6. Mixed quiz render.
7. POST grading branch.
8. Result render branch.
9. Pool-health open count.

## Open questions

- Sections with no open questions.
