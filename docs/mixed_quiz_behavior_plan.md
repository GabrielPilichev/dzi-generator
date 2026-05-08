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

Persisted mixed/open assignments use `quiz_assignments.question_plan_json`.
The field stays `NULL` for MC-only assignments. When a mixed/open assignment is enabled later, it should store the assignment-level plan object that each student attempt copies into `quiz_attempts.question_ids_json`.
At quiz start, `NULL` plans keep the existing MC-only picker. A valid mixed/open assignment plan is copied into `quiz_attempts.question_ids_json`; malformed non-empty plans fail closed and should not create attempts.
Teacher/admin mixed planning can create an experimental persisted mixed/open assignment only through an explicit create action. Previewing a mixed plan still does not create anything, and MC-only assignment creation continues to store `question_plan_json = NULL`.
Admins can optionally enable a display-only combined score while creating that mixed/open assignment. The option defaults off, stores `include_open_answers_in_final_score: true` only when checked, and does not change stored MC score semantics.
The planned object shape is:

```json
{
  "mixed_open_enabled": true,
  "question_ids": [123, 456],
  "open_question_ids": [456],
  "include_open_answers_in_final_score": false
}
```

`include_open_answers_in_final_score` defaults to `false`; combined score display must remain explicit opt-in.

When enabled later:

- MC questions render as they do today.
- Open/fill-in questions render one text input per subquestion/blank.
- Open inputs appear only for eligible planned questions.
- No answer-key data appears before submission.

## Mixed/Open Visibility

Mixed/open assignments are surfaced read-only on teacher and student pages: the teacher dashboard recent list, the teacher assignments list (with an optional `?type=mc|mixed|all` filter), the assignment detail page, the assignment results header, the student `quiz_start` hero (open-question count pill, optional combined-score pill, short honest hint), and a top "quiz-mixed-banner" card on `quiz_attempt` while a mixed/open quiz is in progress. The mid-attempt per-question fill-in warning text adapts to the assignment's combined-score state so the message stays honest. There is no separate student-facing assignment list page — students reach assignments via shared `/quiz/<id>` links. MC-only assignments show no extra indicators on either student surface. A non-empty malformed `question_plan_json` is treated as not mixed, with a small "невалиден план" note on teacher pages and no hint for students.

## Duplicating Assignments

Admin users can duplicate any assignment from the assignments list or its detail page via `POST /teacher/assignment/<id>/duplicate`. The action is admin-only — testers see no button on the detail page and the route redirects unauthenticated/tester requests to admin login. The duplicate copies `section_id`, `title_bg` (with a " (копие)" suffix), `question_count`, `time_limit_minutes`, and `question_plan_json` verbatim; `created_at` is fresh. `quiz_attempts`, `quiz_answers`, and `quiz_text_answers` are not copied. A malformed `question_plan_json` on the source is copied byte-for-byte to the duplicate, so the duplicate inherits the same "невалиден план" indicator and the same `quiz_start` rejection — the duplicate operation does not validate or rewrite the plan.

## Editing Assignment Metadata

Admin users can edit an assignment's `title_bg` and `time_limit_minutes` from the detail page via `POST /teacher/assignment/<id>/edit`. The action is admin-only and surfaced only when `admin_authenticated` is true. `question_count` and `question_plan_json` are deliberately not editable: changing `question_count` after creation would either leave existing attempts inconsistent or, for mixed/open assignments, diverge from the persisted plan's `question_ids` length and confuse the indicator and renderer; changing `question_plan_json` would invalidate already-running attempts that copied the plan. Validation rejects empty titles, titles longer than 200 characters, non-integer time, time below 1 minute, and time above 600 minutes — on rejection the original row is preserved and the detail page re-renders with an error. `quiz_attempts`, `quiz_answers`, and `quiz_text_answers` are never touched by the edit. After a successful edit `quiz_write_assignment_note` runs to keep the vault note in sync, mirroring the create and duplicate flows.

## Exporting Assignment Results (CSV)

Admin users can download a CSV of submitted attempts and recorded open answers from the assignment results page via `GET /teacher/assignment/<id>/results.csv`. The endpoint is admin-only — testers and unauthenticated users are redirected to admin login. The export is read-only: no `quiz_attempts`, `quiz_answers`, or `quiz_text_answers` rows are modified. The default (full) export skips unfinished attempts (`submitted_at IS NULL`) and ignores any `q`/`status`/`open`/`sort` query params, so existing automation against `results.csv` continues to behave as before; the response filename remains `assignment_<id>_results.csv`.

When called with `filtered=1` (and any of `q`, `status`, `open`, `sort`), the same endpoint applies the visible-page filters and sort to the export and uses the filename `assignment_<id>_results_filtered.csv`. Filter values that are unknown fall back to the same defaults as the page; mixed-only sorts (`open_desc`/`open_asc`) fall back to `default` for non-mixed assignments. Filter and sort logic are shared with the page route via three small module-level helpers (`quiz_parse_results_filter_args`, `quiz_filter_results_attempts`, `quiz_sort_results_attempts`), so a filtered export and the visible filtered page see the exact same set of attempts in the exact same order. When `status=unsubmitted` (or `status=all` with unsubmitted rows in scope), unsubmitted attempts are emitted as a single `attempt` row each with `submitted_at`, `mc_score_correct`, `mc_score_total`, `mc_percent`, `mixed_open_enabled`, `include_open_answers_in_final_score`, `open_subtotal_*`, and `combined_*` left blank, and `open_answer_count = 0`; no `open_answer` rows are emitted for them. The CSV schema, column order, omission of `accepted_answers_json`, and `text/csv; charset=utf-8` content type are unchanged across both modes.

The teacher results page surfaces a second "Експортирай CSV (филтрирано)" button alongside the existing full-export button, but only when at least one filter or non-default sort is active. The filtered-export link carries the current `q`, `status`, `open`, and `sort` query params plus `filtered=1`. The original full-export button is preserved verbatim and remains the default action.

## Results Page Analytics Summary

The assignment results page renders a compact "Аналитика на резултатите" card derived from the same submitted-attempt and open-answer data that already drives the page. Metrics: `submitted_attempt_count`, `highest_mc_percent`, `lowest_mc_percent`, plus — for mixed/open assignments only — `open_answer_attempt_count`, `open_answer_total`, `open_answer_auto_matched_count`, `open_answer_teacher_override_count`, and the informational open subtotal awarded/possible. Wording is honest: MC numbers come from the stored MC score, open-answer stats are review/visibility data and don't change the stored MC score, and the combined score (when active) is display-only. Unfinished attempts are excluded from all metrics, mirroring the existing per-row results behavior. The card is read-only and the page does not write to the database during rendering.

## Results Page Filters

The assignment results page accepts three optional GET filters: `q` (case-insensitive substring search on `student_name`), `status` in `{all, submitted, unsubmitted}`, and — only when the assignment is mixed/open — `open` in `{all, has_open, no_open}` (matched against whether `quiz_text_answers` exist for each submitted attempt). Unknown values fall back to `all`, and a request with no params renders the same default view as before. The attempts table and the analytics summary both reflect the filtered set; the analytics card heading shows a "филтрирано" pill when any filter is active. The header tiles ("Всички опити", "Предадени", "Незавършени", "Среден резултат") remain overall and unchanged. The CSV export ignores the filters and exports all submitted attempts; the button label is "Експортирай CSV (всички предадени)" to make this explicit. Filtering is performed in Python after the existing SQL query and never writes to the database. The auth contract is unchanged — the page remains admin-only.

## Results Page Sorting

The same form also accepts an optional `sort` GET param: `default`, `name_asc`, `name_desc`, `submitted_desc`, `submitted_asc`, `mc_desc`, `mc_asc`, and — only when the assignment is mixed/open — `open_desc`, `open_asc`. Unknown values fall back to `default`, which preserves the existing SQL ordering (unsubmitted last, then `submitted_at DESC, started_at DESC, student_name`). MC and submitted-time sorts place unsubmitted attempts after submitted ones; name sorts ignore submission state. Open sorts use the existing informational open subtotal (awarded points) per attempt; attempts with no recorded text answers fall to the bottom under `open_desc` (treated as 0). Sorting is applied in Python after filtering, on already-loaded data, so the analytics summary still reflects the filtered set in the new visual order. Sorting never writes to the database, never exposes `accepted_answers_json`, and the auth contract is unchanged.

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
   - Display an open-answer subtotal separately on student and teacher/admin review pages.
   - Open-answer subtotals remain informational until a separate scoring design explicitly includes them in final totals.

7. Final score integration plan.
   - MC score remains canonical unless `include_open_answers_in_final_score` is explicitly true on a mixed/open attempt plan.
   - Open-answer subtotal may later be included only for explicitly planned mixed/open attempts.
   - Teacher overrides must take precedence over auto-grading for included open rows.
   - Combined score rendering is display-only; stored `quiz_attempts.score_correct` and `score_total` remain the MC score.

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
