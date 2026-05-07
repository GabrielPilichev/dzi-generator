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

## Design decisions

- Current quiz code assumes multiple-choice only.
- Teacher UI recommendation: two explicit count fields, `closed_count` and `open_count`.
- Student UI recommendation: one text input per `fill_in_subquestion`.
- Grading recommendation for v1: auto-grade only, with a data model that allows future teacher override.
- Normalization: trim, collapse whitespace, casefold, Unicode normalize; do not strip Bulgarian diacritics in v1.
- DB recommendation: additive new `quiz_text_answers` table instead of changing `quiz_answers`.
- `score_total` should count gradable items, preferably per blank.

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

- Per blank vs per question scoring.
- Punctuation normalization.
- Sections with no open questions.
- Teacher override later.
