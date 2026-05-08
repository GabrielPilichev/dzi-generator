# Mixed Quiz Behavior Plan

## Status

LearnPilot currently supports MC-first quiz flows. Backend groundwork exists for future open/fill-in support, including text normalization, fill-in eligibility, text-answer grading, recording/query helpers, and read-only open candidate discovery.

No behavior-changing mixed quiz route/template work should happen until the teacher/admin planning flow is explicit and testable.

## Goal

Introduce mixed quiz behavior in small PRs, starting with teacher/admin controls that can plan MC plus auto-gradable open questions without changing existing student submission or result behavior.

## Non-goals

- No DB schema changes.
- No migrations.
- No DZI imports.
- No asset imports or mapping.
- No generated quiz artifacts.
- No manual grading UI in this first sequence.
- No formula equivalence, synonym, regex, or homoglyph engine.
- No broad auth changes.

## Current State

- MC-only remains the default for all existing flows.
- `quiz_text_answers` support exists at the helper/schema level, but mixed route behavior is not wired yet.
- Open-answer readiness is limited and should be presented honestly in teacher/admin UI.
- Existing DZI auth default-deny behavior must be preserved for future DZI/admin pages.

## Behavior Principles

- Existing MC-only behavior remains unchanged unless the teacher/admin explicitly enables open questions.
- Open questions are opt-in by teacher/admin.
- The first implementation only uses auto-gradable open/fill-in candidates.
- Unsafe or incomplete open questions are excluded instead of shown with warnings.
- Student-facing mixed behavior should not be introduced until planning, candidate selection, and tests are stable.

## Teacher/Admin Controls

Initial controls should be simple and explicit:

- `include_open_questions`: toggle, default off.
- `closed_count`: MC question count.
- `open_count`: open/fill-in question count, active only when the toggle is on.
- `source_slug`: DZI source selector if the flow already supports source selection.

Teacher/admin UI must state that the auto-gradable open-answer pool is currently small and varies by source.

## Open Question Eligibility

Open candidates must come only from `fetch_open_question_candidates`.

Eligibility rules:

- Include only auto-gradable fill-in/open candidates.
- Exclude visual-dependent questions.
- Exclude practical tasks 26-28.
- Exclude questions missing accepted answers.
- Treat formula-like answers as plain strings only.
- Do not use formula equivalence, synonym, regex, or homoglyph matching in V1.

## Mixed Quiz Plan Rules

- MC candidates continue to use the existing MC-safe eligibility path.
- Open candidates come from `fetch_open_question_candidates`.
- `open_count = 0` means the existing MC-only path.
- `open_count > 0` requires explicit teacher/admin opt-in.
- If the requested open count is not available, show a clear shortfall and do not silently substitute unsafe questions.
- Early PRs may build an in-memory plan only before creating student-facing mixed attempts.

## Student Attempt Behavior

No student attempt behavior changes in the first planning/control PR.

When enabled later:

- MC questions render as they do today.
- Open/fill-in questions render one text input per subquestion/blank.
- Open inputs appear only for eligible planned questions.
- No answer-key data appears before submission.

## Submit and Grading Behavior

No submit/grading route changes in the first planning/control PR.

When enabled later:

- MC answers continue to write to `quiz_answers`.
- Open text answers write to `quiz_text_answers`.
- Raw submitted text is preserved.
- Normalized submitted text is stored.
- Accepted answers are snapshotted at grading time.
- Ordered and order-independent grading use existing helper behavior.
- Repeated identical answers in order-independent tasks cannot receive duplicate credit beyond the accepted multiset.

## Result Rendering Behavior

No result rendering changes in the first planning/control PR.

When enabled later:

- MC result behavior stays unchanged.
- Open answers are grouped under their parent question.
- Per-blank correctness and partial credit are visible.
- Existing skipped invalid question count behavior remains intact.
- No manual grading UI appears in this first sequence.

## Small PR Sequence

1. Read-only admin/open-candidates page.
   - Show readiness counts and candidate rows.
   - No writes.
   - Preserve auth rules.

2. Teacher quiz-create include-open-questions toggle, default off.
   - Add explicit controls.
   - Build/preview a mixed plan.
   - Keep MC-only generation unchanged by default.

3. Student text input on quiz attempt page.
   - Render open inputs only for planned eligible questions.
   - Keep MC rendering unchanged.

4. Grading on submit.
   - Record text answers in `quiz_text_answers`.
   - Preserve existing MC scoring semantics.
   - Add ordered and order-independent grading tests.

5. Mixed result rendering.
   - Display MC and open-answer results together.
   - Show per-blank partial credit.
   - No manual grading UI.

6. Teacher override V1 plan.
   - Store teacher/admin `teacher_override` and `teacher_note` on recorded `quiz_text_answers`.
   - Override changes update only open-answer review fields and must not change MC score semantics.
   - Reject updates for text-answer rows outside the current assignment.
   - Open-answer points remain informational until a separate scoring design explicitly includes them in final totals.

## Testing Expectations

- MC-only flow unchanged.
- Open toggle defaults off.
- Open controls require explicit teacher/admin action.
- Mixed planning uses only `fetch_open_question_candidates`.
- Visual-dependent, practical 26-28, and missing-answer questions are excluded.
- Source filtering works where exposed.
- Shortfalls are shown clearly.
- Auth guard tests remain green for admin/teacher pages.
- No generated quiz artifacts are changed by tests.

## Rollback / Safety Notes

- Keep `include_open_questions` off by default.
- Mixed planning can be disabled without data cleanup if no route writes text answers yet.
- Each behavior PR should be independently reversible.
- Run `git status --short` before and after each PR.
- Confirm `data/questions.db` is not modified unless a dedicated DB plan explicitly allows it.

## Decisions Locked Before Code

- MC-only remains default for all existing flows.
- Open questions are opt-in by teacher/admin.
- Teacher/admin UI must honestly say the auto-gradable open-answer pool is small.
- Open candidates must come only from `fetch_open_question_candidates`.
- Visual-dependent questions are excluded.
- Practical tasks 26-28 are excluded.
- Questions missing accepted answers are excluded.
- Formula-like answers are plain strings only.
- V1 has no formula equivalence, synonym, regex, or homoglyph engine.
- No manual grading UI in this first sequence.
- No DB schema changes.
- No DZI imports.
- No generated quiz artifacts.
- Existing MC-only behavior remains unchanged unless the teacher/admin explicitly enables open questions.
