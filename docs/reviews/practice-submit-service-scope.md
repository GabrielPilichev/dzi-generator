# C2 practice submit service scope

Base c1e1e63814c46e3d2368fb1d95f4a36388523438 (merged #367). Branch feature/practice-submit-service in its
dedicated worktree. The approved design in docs/STUDENT_PRACTICE_DESIGN.md,
migration 026 and the reviewed start service govern this slice.

## Allowed files

- Extend web/services/practice.py with the practice submission entry point.
- Add tests/test_practice_submit_service.py.
- Preserve the start-service outcome contract and its tests. Share only the
  existing service logic necessary to keep transaction, ownership, scope and
  plan checks consistent. Preserve all other app, test and content files.

## Submission contract

Take a configured caller-owned connection, the trusted account principal,
a positive session ID and chosen option keys keyed by question ID. The
future route converts form fields into those inputs. The client never
supplies ownership, question plans, timestamps, correctness or scores.
Use an explicit outcome for a missing/inaccessible session and keep missing
and foreign-owned sessions indistinguishable, including for teachers and
admins. Check current account activity and the session row under BEGIN
IMMEDIATE. Never finish a transaction owned by the caller. Explicit SQL
transaction completion must work for Python autocommit=True as in start.

For an owned submitted session return idempotently without inspecting
changed content or writing. For an owned abandoned session refuse without
writing. For an open one, derive the current presentable IDs from the
stored plan and scope under the lock using the start service's rules. Keep
DZI, scope, approval, options and visual eligibility checks. Malformed
plans fail closed as in start. A stale plan or unavailable scope writes
nothing. Start retains responsibility for abandoning/replacing it.

Grade only presentable planned questions. Match chosen keys to the current
stored option keys, independent of shuffled display letters. Ignore fields
outside the plan. A missing or no-longer-valid choice is unanswered; require
at least one usable choice before submission. For a nonempty submission,
store one practice_answers row per presentable question, including NULL
and an incorrect verdict for unanswered questions. Use the existing correct
option lookup. The score denominator is the current presentable count;
unrenderable questions do not count. Keep the original plan, seed and start
time. Do not require five presentable items on submit.

Insert answers and finalize score and UTC submitted_at in one transaction.
Use the pure UTC helper under the lock, guard finalization by owner and
open state, and verify the updated row count. Refuse inconsistent stored
state and propagate unexpected failures with the original rows preserved.
A duplicate submit cannot alter answers, time or score. A stale tab for an
abandoned session cannot affect its replacement. No timer, assignments,
quotas, notes, scoring changes to existing quizzes or progress query.

## Verification

Use disposable migration/canonical DBs. Exercise active account and owner
checks, generic foreign/missing outcomes, submitted retries, abandoned old
tabs, stale and malformed plans, changed content, out-of-scope choices,
partial and empty submission, exact per-question verdicts and stable scores,
non-A–D option keys, rollback after write failures, legacy-state preservation,
two separate connections and real duplicate-submit and replacement races.
Cover configured autocommit=True and caller transaction ownership. Check
compilation, fresh build, foreign keys and integrity. Run existing start tests
with the new tests and relevant auth, quiz, subject and migration suites.
Run one frozen full unittest discover in a Claude background shell, with no
foreground timeout, and preserve exact hashes and start/end/exit evidence.
Report actual tests, limitations, branch, HEAD, paths and commit/PR handoff;
leave implementation edits unstaged and uncommitted for the coordinator.
Separate Opus / High review follows the exact pushed SHA.

## Exclusions

No routes, forms, templates, result view, progress, new activation or feature
gate. No auth policy, migration, canonical content, assignment lifecycle,
retention or dependency edits. No real database or sidecars, live import or
migration, network research, credentials or providers, deployment or VPS,
generated vault, servers or tunnels. C2 HTTP/UI and later C3–C5 follow
separately.
