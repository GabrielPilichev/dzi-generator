# Tester Feedback Implementation Plan

Docs-only planning checkpoint for the latest LearnPilot tester feedback. This
does not change app code, templates, tests, CSS/JS, data imports, migrations,
assets, `data/questions.db`, practical scoring, or generated quiz notes.

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
