---
title: "Mixed Open/Closed Quiz Plan"
type: planning
tags: [learnpilot, dzi, quiz, planning]
---

# Mixed Open/Closed Quiz Plan

Do not build yet. This is future design for mixed closed and open generated quizzes.

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
