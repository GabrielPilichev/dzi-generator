# C2 practice start service scope

Base 1979e389d4813942919f3a60a262c23e72f049ad (merged #366). Branch
feature/practice-start-service in its dedicated worktree. The approved
student practice design in docs/STUDENT_PRACTICE_DESIGN.md governs this
slice. Migration 026 and the shared expression-index comparison are
already merged. This PR adds one service for starting, resuming and
replacing a section or topic practice session.

## Allowed implementation files

- Add web/services/practice.py.
- Add tests/test_practice_start_service.py.
- Preserve this scope, design, migrations, content, offline tools and all
  existing app and test files. Report any concrete dependency outside the
  list before editing it.

## Service contract

Take a caller-owned configured SQLite connection and the trusted
authenticated principal, plus positive integer section and optional topic
IDs. The future route takes the principal from the request, never from
form values. Only active signed-in accounts can practice. Teacher and admin
accounts can start only their own sessions. Recheck account activity and
scope inside the writer transaction. Reject invalid IDs and ordinarily
return an explicit, safe outcome for unavailable scope or pool. Define the
small service result shape in code and document it in the handoff.

Use the existing subject and assessment helpers and approved multiple-choice
selection, including option and visual eligibility. Validate membership of
a requested topic via topic_section_assignments and restrict the pool by
questions.topic_id. Exclude DZI training sections and official DZI rows
through the existing DZI predicate. No borrowing across subjects or scopes.
An unknown subject or unavailable scope must never create a session. Do
not edit the existing picker or introduce another eligibility rule set.
For a new session require at least five distinct eligible IDs and sample
min(10, eligible) without replacement. Persist order and seed for repeatable
rendering. A repeat after submission gets a fresh session and sample.

Start or resume takes BEGIN IMMEDIATE before deciding. Commit or roll back
only the transaction the service owns. Take the UTC timestamp while holding
the lock, reusing the pure UTC helper. Leave assignment clocks, quotas and
profiles alone. Resume a still-renderable open session with its existing
ID, plan, seed and start time, even if the pool for a new session is thin.
Use the existing renderability checks for stored IDs. Keep current scope
and DZI exclusions so changed content cannot introduce out-of-scope items.
Fail closed on malformed stored plans without writing or treating them
as an ordinary empty pool.

Mark an open session with zero renderable items abandoned under the same
lock. A replacement can only be written if the current eligible pool meets
the same minimum. Below five, commit the abandonment alone and return the
empty-pool outcome. Never score or create answer rows in this slice.
Preserve the original rows on failed writes. Use the open-state predicate
defensively and verify rows affected. Do not adopt a different owner's row
or swallow unexpected database errors.

## Verification

Use disposable DBs from the repository migrations and fixtures. Cover
account/sign-in rules, positive IDs, scope and topic membership, DZI/subject/
approval/options/visual exclusions, pools with 0/4/5/10/over-10 usable
items, resumption and repeat, stale replacement and insufficient replacement,
malformed plans, rollback and unchanged legacy runtime rows. Exercise two
concurrent starts via separate SQLite connections for both NULL-topic and
concrete-topic scopes, with no timing-dependent sleeps as assertions. Verify
that both callers get one open session and that other accounts and scopes
remain independent. Check migration and scratch build/integrity preservation.

Run the new module plus practice migration, auth guard, quiz attempt
render and affected subject/DZI tests. Compile changed Python to scratch.
Run one full unittest discover on final frozen code and tests as the
roadmap requires. Preserve exact start/finish/exit markers and hashes.
Report author results and limitations, exact branch/HEAD, changed paths,
and provide staging list, meaningful commit/PR text and stable manifest.
Leave all edits unstaged/uncommitted. Coordinator commits/pushes in a
dedicated Terminal and launches fresh Opus/High exact-SHA review.

## Exclusions

This slice has no routes, templates, submit writer, result view, progress
queries or activation. No configuration, migration, canonical data,
auth/scoring/teacher lifecycle or retention changes. Only temporary
databases. No real runtime database or sidecar reads/copies/hashes, live
migration/import, providers/credentials, network research, deployment/VPS,
generated vault, servers/tunnels or gates. The C2 submit and HTTP/UI
slices and C3–C5 remain separate.
