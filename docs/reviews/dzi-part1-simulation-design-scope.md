# C3 DZI Part 1 simulation design scope

Base ffa74c98ee6bb532870827269ef8b69e0416bbed. Branch docs/dzi-part1-simulation-design in a dedicated
worktree. Follow the approved 2026-11-18 launch-first roadmap and its
C3 proposal: a signed-in, timed Part 1 simulation using the existing
DZI blueprint, 15 multiple-choice and 10 short-answer tasks. Keep
the admin DZI archive and teacher-generation tooling separate.

## Deliverable

Write docs/DZI_PART1_SIMULATION_DESIGN.md only. Locate the relevant
tracked schema, blueprint, ownership, generation, time, assessment and
result code at this exact base. Treat the Fable review §5.4 as the
source of release intent rather than approval of unrecorded design
choices. Use a disposable canonical build for any readiness evidence.

- Explain where the Part 1 slot types, count, order, points and pools
  come from. Distinguish official exam identity from owned, reviewed,
  renderable and auto-gradable candidates. Preserve existing source,
  asset and accepted-answer rules. Show thin-pool failure, no borrowed
  question, and what approved content gaps remain.
- Compare viable runtime storage for timed mixed answers with the
  current C2 section/topic-only tables. Recommend additive storage
  or a narrowly justified extension, including runtime classification,
  stable ID references, updater/backup compatibility, migrations and
  rollback. Do not create a system teacher or assignment, write notes
  or give students admin routes.
- Specify account-owned start/resume/submit/results, same-origin,
  server-owned deadlines, idempotence, refresh, old tabs, clock
  failure and deadline races. Clearly identify proposed app timing
  when the tracked evidence does not establish official timing. Do
  not promise an automatic background close without defining it.
- Reuse verified grading and rendering where applicable, including
  persisted verdicts and points and the current-content caveat.
  Keep correct answers away from attempts. Explain short-answer
  grading limits, incomplete and timeout treatment, result score
  maximum, and eligibility for later separate C4 progress.
- Describe testable acceptance and implementation order, default-off
  exposure, existing DZI ownership and visibility and activation
  prerequisites. Leave C4 and the C5 DZI public-content decision
  for their existing separate scopes.

The implementation design records engineering proposals as such.
Do not set official or deployment facts from memory. Keep legal,
privacy, users, timed activation, ops and content additions out of
this doc PR. No application, migrations, templates, canonical rows,
content gate or production changes. No runtime DB access, deployed
services, providers or credentials, generated vault, server/tunnel or
live operation. Review diff and whitespace, and supply dated scratch
evidence and independently reviewed exact SHA.
