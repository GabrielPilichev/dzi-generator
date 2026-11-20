# C1 student practice design: scope

Status: documentation-only task boundaries. This scope selects no storage or
access option; the design note proposes the engineering choices and an
independent Opus/High review assesses them before merge.

## Baseline and authority

- Base: `b0741e7e0f2e5434e880e5f5fc50356d179a6b93` (merged PR #363).
- Base tree: `906496bf6ffb83b489f9ca693b5557fea7614750`.
- Branch: `docs/student-practice-design`, in a dedicated worktree.
- Authority: the approved 2026-11-18 update in
  [the roadmap](../REFACTOR_ACCOUNTS_ROADMAP.md#current-roadmap-update--2026-11-18),
  and C1 in section 5.4 of the review identified there by hash. Practice design
  follows the content-update tool, one PR at a time; this task activates nothing.

## Deliverable and exact files

Add only `docs/STUDENT_PRACTICE_DESIGN.md` after this scope commit. Keep the
note to a two-page brief: at most 1,000 words and 110 lines, including tables
and citations. No executable examples, SQL migration, appendix or second design
document. This scope remains unchanged during authoring.

The note must settle, with source-grounded alternatives and one recommendation
per choice:

1. **Storage:** system-owned assignments per practice session versus dedicated
   practice runtime tables. Explain the chosen representation, ownership,
   repeat/resume and idempotent submission. Include a future migration plan if
   needed; no migration is implemented or run here.
2. **Access:** DZI practice uses `signed_in`; explicitly choose whether non-DZI
   practice is public. Account-owned progress and object access must be clear.
   Do not open the admin DZI tools or weaken any existing route's policy.
3. **First-version progress:** define exactly which submitted results count,
   how repeated and incomplete attempts are treated, what remains after content
   changes, and what is deferred. No mastery, difficulty-calibration, ranking,
   legal-retention or durable-history claim without a contract supporting it.

Include a small C2 section/topic flow, result-presentation reuse boundary,
empty/thin-pool behavior and a short future acceptance list. These are design
contracts, not implemented or tested behavior. DZI simulation and topic-result
aggregation remain C3/C4 work; the DZI content gate remains C5.

## Source facts to verify, not bypass

- `web/migrations/001_quiz_tables.sql` and `015_quiz_attempts_user.sql`:
  `quiz_attempts.assignment_id` is NOT NULL; account-owned attempts are unique
  by `(assignment_id, user_id)`. Existing NULL-owner history is browser-bound.
- `web/services/quiz.py`, `quiz_create_assignment`: creation requires teacher
  capability and uses lifecycle/quota accounting. A student-practice design
  must not forge a teacher/admin principal or silently bypass that contract.
- `web/blueprints/quiz.py`, `quiz_start_public`: the token landing is public,
  but starting a new attempt already requires an account. Preserve this flow,
  historical attempts, owner-only results and the retired numeric-entry 404.
- `web/access.py`, `web/attempt_access.py` and `docs/AUTH_ROUTE_CONTRACT.md`:
  assignment management never grants access to another account's attempt.
- The quiz eligibility, subject ownership and assessment helpers determine
  usable questions. Browseable does not imply quiz-eligible. Do not invent bank
  counts or silently borrow questions from another subject/topic.
- `quiz_result_context`, `quiz_result` and `web/templates/quiz_result.html`:
  explain what presentation can be reused without changing scoring or access.
- `src/canonical_tables.py`, the builder and migration workflow: any proposed
  new runtime tables need classification/build/preservation integration in the
  later implementation plan. Never classify practice history as canonical.
- The lifecycle/history design contains future work. Distinguish implemented
  helpers from deferred PR #327 and do not claim that its witness exists.

## Preserved and excluded

All existing code, tests, migrations, canonical data, inventory, configuration,
gates, auth/scoring and deployment documents stay byte-identical. No UI, route,
schema, content, quota, retention, cleanup, entitlement or billing implementation.
Owner legal/controller/contact/retention inputs and activation evidence remain
unknown or pending as recorded; no legal text or dates are invented here.

No database is opened or built, including a scratch database. No import,
migration, provider/credential, network research, VPS/deploy, backup/restore,
generated vault, local server/tunnel or gate operation. Inspect source as text.

## Verification and handoff

Documentation only: verify citations against the exact checkout, review the
diff and word/line bounds, and run `git diff --check`. No unittest, compile,
database build, broad audit or full-suite run is needed.

Leave the design unstaged and uncommitted. Report the exact branch/HEAD,
changed path, scope hash, checks and limits. The coordinator executes the
authorized commit/push in a dedicated Terminal. A separate fresh Opus/High
review assesses the choices and exact pushed final SHA before merge.
