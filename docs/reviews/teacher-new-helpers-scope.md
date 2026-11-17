# A4 — extract teacher_new helpers

Gabriel authorized A4 after PR #355 merged. Base:
`116a543c4f1f3c55b6019f5816a057b90c17abc3`.
Branch: `refactor/teacher-new-helpers`.
Goal: separate POST parsing/validation and server-side assignment planning
from `teacher_new`, preserving its behavior and the A3 connection boundary.

## Implementation boundary

After this scope is committed and a draft PR exists, a fresh Sonnet / High
session may change only `web/blueprints/teacher.py`: `teacher_new`, new private
helpers beside it, small private input/result records if needed, and imports
needed by those additions. Keep the existing tests, templates, service modules,
other functions and module-level behavior unchanged. No general cleanup,
query optimization, new feature, migration or content change.

- Extract POST form parsing and validation into a helper accepting the form
  and any explicit role input it needs. This covers forged-key screening,
  numeric/default/range handling, form redisplay values and deadline parsing;
  it opens no connection and samples no clock. Subject resolution stays first
  inside the existing connection scope; parsing runs only on POST, there at
  its current position. An eager cached-principal role lookup on POST is allowed.
- Extract section/scope checks, eligibility counting, MC/mixed plan construction
  and plan validation into a helper accepting the route's open connection and
  parsed input. Return explicit outcomes and server-built creation fields;
  distinguish refused input, empty-pool redirect and valid creation.
  Every outcome retains the current raw/validated `form_values`, error and
  preselection. A valid outcome also retains section id/title and any mixed
  summary for redisplay if persistence is refused. Return the empty-pool
  redirect from the route inside the connection scope.
- Reuse `_create_assignment_with_deadline` unchanged for persistence. It already
  owns the lifecycle writer transaction, quota admission and deadline resolution;
  do not add a wrapper that merely forwards its arguments or duplicate its rules.
  The route coordinates the helper outcomes, persistence, rendering and redirect.
- Keep the existing non-success form/page-loading block and final rendering in
  the route. Do not extract or redesign the batched page loader in this PR.
  Prefer simple explicit results over a framework or generic validation layer.

## Contracts to preserve

- Keep the URL, endpoint and policy decorators, subject selection/defaults,
  invalid-subject GET 404 / POST 400, all error codes, precedence, status codes,
  redirects, Bulgarian text and complete template context unchanged.
- Retain the exact prefix rejection for `question_ids`, `open_question_ids` and
  `question_plan` form keys. Preserve MultiDict/get semantics, whitespace,
  truthiness/defaults, numeric limits, raw versus validated redisplay values,
  admin-only open-answer scoring, deadline fields and preselection behavior.
  Extraction must not fix or reinterpret existing edge cases.
- Numeric errors overwrite a preceding forged-key error; the forged error
  survives only with valid numbers. Never short-circuit numeric validation on
  forged keys. Parse errors skip deadline parsing and POST planning/persistence,
  but still run the failure page loader. Keep numeric, deadline, scope and plan
  exception regions separate and as narrow as today: `DeadlineError` subclasses
  `ValueError`; do not swallow pool, planning, persistence or lifecycle failures.
- Question ids and plans come only from server-loaded subject/section scopes.
  Preserve MC count clamping, mixed counts/order/shortfalls, source restrictions,
  `AssessmentScope.validate_plan`, quota refusals and lifecycle/clock responses.
- Form values become validated only after scope checks pass. Keep the mixed
  summary after plan/persistence refusal. Preselect the section on the scope's
  open-question refusal, either shortfall, invalid plan or persistence refusal;
  not on numeric/deadline-parse errors, invalid scope or subject mismatch.
  Preserve the later eligible `?section=` override, including on POST.
- Keep SQL literal bytes, bound parameters, statement order/count and lazy
  evaluation unchanged. No extra scope/pool lookups or successful-POST form
  queries. Preserve existing module-level dependency lookups so all current
  test patches still observe real calls; bind no dependencies as default args.
  A common persistence call may use server-built kwargs; preserve the MC
  omission versus mixed inclusion of `question_plan_json`.
- The route retains one `with quiz_connection()` around the same work. New
  helpers use the passed connection and neither open nor close/commit it.
  `_create_assignment_with_deadline` retains its existing transaction authority.
  A created id uses `is not None`; note writing and redirect stay after close.
  Failure-only form loading still runs only when no assignment was created.

## Verification and handoff

Scope preparation is document/source/diff/whitespace review only, with no tests
or database build. Before implementation, independently review this scope.
Implementation must pass existing `/teacher/new` tests **unchanged**: teacher
blueprint, query batching, hardened inputs, mixed planning, assignment plans/quota,
subject assessment, lifecycle teacher routes, A3 lifetimes and auth/quiz smoke.
Also run unchanged source pins in `test_assignment_qr_sharing`,
`test_auth_blueprint`, `test_teacher_blueprint`, `test_lifecycle_static`,
`test_assignment_public_tokens`, `test_manage_assignment_tokens`,
`test_subject_generic_flow`, `test_browsing_blueprint`, `test_practical_blueprint`
and `test_access_policy`, plus quota's static pins. Preserve exactly two
`quiz_create_assignment(` text occurrences and the forbidden-substring checks
(including `requests.`/`cdn.`); helper comments/docstrings count too.
Use scratch base/head probes for response/context, SQL trace, stored assignment
and note parity on GET, malformed/forged/combined-invalid POSTs, MC/mixed success,
empty/shortfall, deadline/clock/quota refusal and role/subject cases.
Include forged keys combined with non-numeric input, time `0`, open>total and
valid numbers/missing deadline (forged error wins there); mixed persistence
refused by deadline-too-far or quota must retain its summary and validated form.
Preserve preselection and any POST query-string override. Control
timestamps/tokens/randomness only in fixtures; compare note bytes and disclose
scratch-path normalization. Preserve existing query ceilings and close-before-note.
Compile to scratch and check whitespace. A full suite is not automatic: run it
only if a concrete unresolved concern justifies it, and disclose any failure.

Only temporary databases/vaults via existing test support are authorized. No real
`data/questions.db` or sidecars, generated repository vault, real providers or
credentials, gates, VPS, deployment, server or tunnel operations.

Leave implementation edits unstaged/uncommitted. Report exact branch/HEAD,
changed files, helper contracts, preservation evidence, tests and limits.
Gabriel commits/pushes; fresh Opus / High reviews the exact pushed SHA. After
verified approval, print the complete guarded merge commands directly in chat;
Gabriel decides whether to execute them.
