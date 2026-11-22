# C2 reusable multiple-choice result review scope

Base 0cb22d0f10226a8fd9acc24a9387aac54606c258 (merged #368). Branch refactor/mc-result-review-partial in
its dedicated worktree. Implement the reusable result presentation
specified by docs/STUDENT_PRACTICE_DESIGN.md §5. This slice prepares the
shared markup for practice results while preserving existing quiz results.

## Allowed implementation files

- Edit web/templates/quiz_result.html.
- Add web/templates/_multiple_choice_result_review.html.
- Add tests/test_quiz_result_review_partial.py.
- Preserve all existing Python, tests, route manifests, configuration,
  CSS/JS and other templates. If a concrete dependency requires a change
  outside these paths, report it before editing.

## Behavior

Extract the current multiple-choice result review into a Jinja partial.
Preserve the Bulgarian copy, option order and display letters, current
visuals and difficulty labels, summary of chosen and correct options,
feedback, and original verdict styles. The include should accept the
existing resolved question data. It should be usable from later practice
results without assignment identity, route helpers, profile or score
context. Keep assignment and attempt summaries, open-answer analysis,
combined scores and navigation in quiz_result.html.

Preserve the stale warning and suppression of all question and open-text
reviews when stale_attempt_message is set. Keep the current context and
rendering of mixed attempts with open-text answers. Preserve the output
of the existing result page, including whitespace for deterministic
fixtures. If Jinja include boundaries create a whitespace-only
difference, identify the exact change and attempt a local template fix
before claiming render equality. Keep open-answer blocks outside the
shared partial. Reuse the existing visual macro and Jinja environment.

## Verification

Use deterministic render fixtures and the real app template environment.
Pin the existing result rendering by comparing base and updated outputs
for the same contexts. Cover correct, incorrect, unanswered and visual
questions, original chosen and correct tags, mixed/open-text rendering,
and stale suppression. Keep a focused regression in the new test module
with stable inputs and a clear output comparison. Use existing tests and
fixtures where appropriate. Do not commit generated HTML, screenshots,
snapshots from runtime data, or an extra copy of the old template.

Run the new tests and affected result, render, mixed-quiz and auth suites.
Run the agreed full unittest discover on the final frozen hashes using a
real site-enabled venv and isolated temp paths. The standard documented
command is python -B -m unittest discover -s tests. Use a background
shell without a foreground timeout. Save start/end, exit and hashes.
Diff/whitespace and template compilation should pass. Keep exact author
evidence and independent review separate.

## Exclusions and handoff

No practice route, result reader, context or grading change, config,
migration, content changes, activation, or feature gate edits. No live
DB or sidecar access, live import/migration, providers/credentials,
network research, deploy/VPS, generated vault or servers/tunnels.
Use scratch-only fixtures and the in-process test client if needed.

Leave the three approved paths unstaged/uncommitted. Report exact branch,
HEAD, status, output comparison and test limits, plus stage list, commit
and PR descriptions and stable evidence manifest. Separate Opus / High
review follows the exact pushed implementation SHA.
