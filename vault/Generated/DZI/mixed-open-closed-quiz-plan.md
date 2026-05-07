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

## Completed groundwork

- Normalization helper merged.
- Fill-in eligibility helper merged.
- `quiz_text_answers` migration file added but not run.
- Migration SQL text test added.
- In-memory migration execution test added.
- `data/questions.db` has not been migrated.
- Quiz generation behavior has not changed.
- Answer submission, grading, and result rendering have not changed.

Next gated step:

- Running the migration against `data/questions.db` is a separate explicit step and should not happen without a dedicated plan/checkpoint.

## Migration runbook draft

Pre-checks:

- `git status --short` must be clean.
- Run the current 38-test suite.
- Run read-only FK check:
  - `sqlite3 "file:data/questions.db?mode=ro" "SELECT * FROM pragma_foreign_key_check;"`
- Run `python3 src/audit_dzi_state.py` and confirm expected totals:
  - `PART1_IMPORTED: 2`
  - `READY_FOR_PART1_IMPORT: 5`
  - `NEEDS_ATTENTION: 0`
  - `foreign key check rows: 0`

Backup step:

- Copy `data/questions.db` to a timestamped backup outside git or to a clearly named local backup, for example `data/questions.db.backup-YYYYMMDD-HHMMSS`.
- Do not continue unless the backup exists and can be restored.

Migration execution step:

- No general migration runner currently exists for numbered `web/migrations/*.sql`.
- `web/app.py` only applies `web/migrations/001_quiz_tables.sql` automatically.
- Running `web/migrations/005_quiz_text_answers.sql` against `data/questions.db` needs a separate migration-runner decision/checkpoint.

Post-checks:

- Inspect new table shape:
  - `PRAGMA table_info(quiz_text_answers);`
  - `PRAGMA foreign_key_list(quiz_text_answers);`
  - `PRAGMA index_list(quiz_text_answers);`
- Run read-only FK check:
  - `sqlite3 "file:data/questions.db?mode=ro" "SELECT * FROM pragma_foreign_key_check;"`
- Run the current test suite.
- Run `python3 src/audit_dzi_state.py` and confirm expected totals remain:
  - `PART1_IMPORTED: 2`
  - `READY_FOR_PART1_IMPORT: 5`
  - `NEEDS_ATTENTION: 0`
  - `foreign key check rows: 0`

Rollback:

- Stop the app/processes using the DB.
- Restore the DB backup over `data/questions.db`.
- Re-run the pre-check FK/audit/test commands.

Explicit non-goals:

- No quiz generation changes.
- No answer submission changes.
- No grading/result rendering changes.
- No imports/assets.
- No mixed quiz UI.

## Implementation checklist draft

1. Schema migration file
   - Files likely touched: `migrations/` or the repo's current SQLite migration path; migration README/log if present.
   - Tests to add or update: migration smoke on a copied DB; foreign key check; schema introspection for table, indexes, and constraints.
   - Explicit non-goals: no data backfill, no changes to `quiz_answers`, no generated quiz behavior change.
   - Rollback/safety note: additive-only migration; rollback can drop `quiz_text_answers` before any production open-answer attempts exist.

2. Normalization helper + unit tests
   - Files likely touched: `web/app.py` or a small quiz utility module if one already exists; focused unit test file.
   - Tests to add or update: trim, whitespace collapse, casefold, Unicode normalize, smart quote handling, Bulgarian diacritics preserved.
   - Explicit non-goals: no punctuation engine, no Cyrillic/Latin homoglyph conversion, no formula equivalence.
   - Rollback/safety note: helper can be unused behind later phases until grading writes text answers.

3. Fill-in eligibility helper
   - Files likely touched: `web/app.py`; possibly `src/audit_dzi_state.py` or pool-health helpers later.
   - Tests to add or update: fill-in question with complete subquestions is eligible; missing/blank accepted answer is excluded; visual-dependent fill-in without asset remains blocked only where open generation requires assets.
   - Explicit non-goals: no mixed picker yet, no open-answer UI, no teacher override.
   - Rollback/safety note: keep helper separate from existing MC eligibility until generation path is explicitly switched.

4. Generation path changes
   - Files likely touched: `web/app.py`; teacher assignment creation templates if count controls are introduced in the same phase.
   - Tests to add or update: closed-only generation remains unchanged; mixed generation selects requested `closed_count` and `open_count`; missing-visual MC remains excluded; no-open-section behavior is explicit.
   - Explicit non-goals: no grading changes beyond storing selected question IDs; no practical tasks 26–28.
   - Rollback/safety note: keep `open_count` default `0` so existing assignments remain closed-only.

5. Answer submission handling
   - Files likely touched: `web/app.py`; quiz attempt template; result/attempt form parsing helpers.
   - Tests to add or update: MC answers still insert into `quiz_answers`; fill-in answers insert into `quiz_text_answers`; empty text answers are stored and graded as incorrect; stale question IDs do not crash.
   - Explicit non-goals: no manual grading UI, no teacher edits, no answer-key privacy behavior change.
   - Rollback/safety note: submissions can branch by `question_type`; closed-only attempts should not touch the new table.

6. Grading logic
   - Files likely touched: `web/app.py` or quiz grading helper module.
   - Tests to add or update: ordered fill-in grading; order-independent set/multiset grading; repeated identical answers receive credit once; accepted-answer snapshot is stored; task 18 formula variants only match manually listed alternatives.
   - Explicit non-goals: no regex/synonym engine, no spreadsheet formula parser, no all-or-nothing task scoring unless explicitly decided later.
   - Rollback/safety note: preserve existing MC score semantics and compute mixed totals from separate MC/text rows.

7. Result rendering
   - Files likely touched: quiz result template; `web/app.py` result query/context.
   - Tests to add or update: result groups text blanks under parent question; partial credit is visible; MC-only result stays unchanged; skipped invalid-count behavior still works.
   - Explicit non-goals: no teacher review controls, no DZI 100-point conversion unless designed separately.
   - Rollback/safety note: render text-answer blocks only when rows exist for the attempt.

8. Teacher assignment controls
   - Files likely touched: teacher assignment/new templates; `web/app.py`; CSS only if existing controls cannot cover the layout.
   - Tests to add or update: default closed-only assignment; explicit `closed_count`/`open_count`; validation for negative counts and insufficient pools; tester/admin auth behavior unchanged.
   - Explicit non-goals: no per-student tokens, no broad auth refactor, no answer-key visibility redesign.
   - Rollback/safety note: keep old `question_count` path valid while new fields are introduced.

9. Audit/backward compatibility checks
   - Files likely touched: `src/audit_dzi_state.py`, `src/audit_dzi_assets.py`, AGENTS/docs if wording changes.
   - Tests to add or update: MC pool-health numbers remain pinned; open-ready count is separate; old attempts without `quiz_text_answers` still render; foreign key check passes.
   - Explicit non-goals: no new imports, no asset mapping, no cleanup of historical attempts.
   - Rollback/safety note: audits should be read-only and report mixed-readiness separately from current MC readiness.

## Open questions

- Sections with no open questions.
