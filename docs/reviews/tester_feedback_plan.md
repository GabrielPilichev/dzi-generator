# Tester Feedback Implementation Plan

Docs-only planning checkpoint for the latest LearnPilot tester feedback. This
does not change app code, templates, tests, CSS/JS, data imports, migrations,
assets, `data/questions.db`, practical scoring, or generated quiz notes.

## Checkpoint - 2026-05-15

Automated smoke coverage checkpoint:
`docs/reviews/tester_smoke_coverage_checkpoint.md`.

### Fixed / merged

#### Critical quiz flow

- [x] Timer remaining-time calculation fixed for normal and long durations.
- [x] Accidental early auto-submit fixed; submit-on-expiry remains tied to
  countdown reaching zero.
- [x] Student submission validation added for blank/one-character names and
  empty submissions.
- [x] Quiz question randomization added.
- [x] Browser-side quiz draft autosave added.
- [x] Quiz progress indicator added.
- [x] Low-time timer warning added.

#### Login/download 500s

- [x] Bulgarian keyboard/password input now follows the normal login rejection
  path instead of HTTP 500.
- [x] Practical file download crash fixed.

#### Review answers/explanations

- [x] Open/fill-in review answer visibility fixed.
- [x] Question 23 answer visibility fixed.
- [x] Wrong multiple-choice/open-answer feedback added.
- [x] XSS/escaping coverage added for answer displays.

#### Practical uploads/security

- [x] Practical upload hardening added.
- [x] Practical task uploads/review flow preserved.

#### Mobile/navigation UX

- [x] Homepage topic search added.
- [x] Mobile DZI review scroll/filter UX improved.
- [x] Mobile profile/login entry added for tester/admin access.
- [x] DZI preparation separated visually from normal class sections.
- [x] Recent tests grouped/labeled by available context.
- [x] Content/test cards made clickable beyond only the title/name.

#### Results/analytics

- [x] Attempt duration display added to result/review surfaces.
- [x] Success-rate display added.
- [x] Difficulty breakdown / heatmap-lite display added.

#### Review page polish

- [x] Review show-all/hide-all answer controls added.
- [x] Review copy buttons added.

### Needs manual smoke test

- Timer durations: 30, 60, 400, and 600 minutes.
- Mobile class page opens at the top instead of retaining an awkward scroll
  position.
- DZI review button and filter controls on a phone viewport.
- Practical file download.
- ZIP upload for practical work.
- Teacher review/download/score/note flow.
- Wrong-answer explanations for multiple-choice and open/fill-in answers.
- Homepage topic search.

### Remaining / not yet done

- Bigger auth redesign with username + password, if still desired.
- More advanced analytics/heatmaps, if desired after the current
  heatmap-lite display is tested.
- Server-side autosave, if browser local autosave is not enough.
- Importing practical tasks/resources for sources beyond `may_2025_v2`, if
  still needed.
- Broader teacher dashboard improvements, if still needed.

### Next recommendation

Run a localhost or tunnel smoke test with testers, collect screenshots and
exact routes for any remaining bugs, then fix only confirmed remaining issues.

## Latest feedback

### P0 - Timer / auto-submit bugs

- When an assignment duration is set to 30 or 60 minutes, the timer does not
  work and the test finishes immediately.
- When an assignment duration is set to 400 minutes, the test starts from
  220 minutes instead of 400.

### P0 - Authentication crash

- If the password is typed while the keyboard layout is Bulgarian, the site
  returns HTTP 500 Internal Server Error instead of rejecting or accepting the
  submitted password normally.

### P0/P1 - Practical file download crash

- Pressing practical files returns HTTP 500 Internal Server Error.
- Treat as P0 if this blocks all practical-task access for active testers.
  Treat as P1 if it is limited to one source/file edge case.

### P1 - Missing answers and explanations

- For a wrong closed-answer result, show an explanation of why the selected
  answer is not correct.
- For a wrong open-answer result, show an explanation plus the correct or
  accepted answer.
- When a topic is opened and open questions are selected for review, answers
  do not appear.
- In preparation tasks, question 23 does not show which answer is correct.

### P2 - Homepage topic search

- The homepage would be more useful with a topic search field.

## Suggested PR sequence

### PR A - Fix timer duration and accidental auto-submit

- Reproduce 30-minute, 60-minute, and 400-minute assignments.
- Verify whether the stored duration, rendered duration, client countdown, and
  server submit guard use the same units and upper bounds.
- Fix accidental immediate completion before adding unrelated timer features.
- Add focused regression coverage for 30, 60, 220, and 400 minute durations.

### PR B - Fix login/password 500 and practical file download 500

- Normalize the login failure path so Bulgarian keyboard input is handled as
  ordinary Unicode form data and never reaches a 500.
- Make practical file download failures return a controlled 404/403 or a clear
  validation error instead of HTTP 500.
- Keep these as bug fixes only; do not change practical-task scoring or upload
  semantics.

### PR C - Show missing answers in open-question review and question 23

- Fix the topic/open-question review path so accepted answers render in review
  mode where answers are intentionally revealed.
- Fix the preparation-task result/review display for question 23 so the correct
  answer is visible.
- Confirm answer visibility still respects existing tester/student/admin rules.

### PR D - Add explanations for wrong MC/open answers

- For wrong multiple-choice answers, show the explanation for the correct
  answer and, when data supports it, why the selected option is wrong.
- For wrong open answers, show the explanation plus the correct/accepted answer
  set.
- Fall back gracefully when an explanation is missing; do not invent answer
  rationale in code.

### PR E - Homepage topic search

- Add a small homepage search over existing topics.
- Keep the first version read-only and topic-scoped.
- Do not broaden this into global search or content indexing unless planned in
  a separate PR.

## Non-goals

- Do not change DB imports.
- Do not change practical scoring.
- Do not mix practical score into the final score.
- Do not do broad refactors.
- Do not modify `data/questions.db`.
- Do not run migrations as part of this planning checkpoint.
- Do not import questions or assets.
- Do not touch `vault/Generated/Quizzes/`.
