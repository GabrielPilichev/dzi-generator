# C2 gated non-DZI practice routes scope

Base f83f6c5757918eac993ed8f15cd3ec574d2caffb (merged #370). Branch feature/practice-session-routes in
its dedicated worktree. Follow docs/STUDENT_PRACTICE_DESIGN.md §§3–6,
the approved launch-first roadmap, the merged practice services and
shared multiple-choice result template.

## Allowed implementation files

- web/app.py for the conditional blueprint registration only.
- web/services/practice.py for the default-off build constant and an
  owner-checked read-only attempt view helper. Preserve the current
  start, submit and result loader behavior and their tests.
- New web/blueprints/practice.py, web/templates/practice_attempt.html,
  web/templates/practice_result.html, and optional
  web/templates/practice_unavailable.html for shared error states.
- New tests/test_practice_routes.py covering disabled and fixture-armed
  behavior. Document the declared policies for these routes in
  docs/AUTH_ROUTE_CONTRACT.md.
- Preserve every other application file, existing route, gate, fixture,
  template, config, content and migration. Report a concrete scope
  dependency before editing any additional file.

## Default-off registration

Follow the existing build-activation convention. The new practice
registration constant is False and has no environment override. Register
the blueprint only when the constant is armed. Ordinary app startup
and production expose exactly their existing routes and behaviors.
Arm practice only in isolated tests, by patching the constant before
the separate test interpreter imports the genuine web.app. Use actual
app access and request-integrity hooks. Leave activation for a separate
approved launch change. No browse link or nav edits in this slice.

## Route flow when fixture-armed

Add POST start by section, additionally by section and topic. Choose
consistent /practice paths independent of /quiz and record the exact
endpoints in the route contract. Start consumes path IDs only. Add
the session attempt GET and POST entry and the submitted result GET.
Each practice view declares signed_in. Unsafe views also declare
same_origin and pass the existing app hook before any write. Never use
submitted roles, account IDs, scores, timestamps, labels, plan or seed.

Read open session state through an owner-scoped coherent snapshot with
the same eligibility rules as start, including the stored order and
seed, and no writes on GET or HEAD. Do not render correct keys or
verdicts on the attempt page. Ownership comes from the active principal;
another account, teacher or admin sees the same response as a missing
session. A submitted owned attempt leads to its own result and old or
abandoned attempts cannot be graded or adopted. Follow the saved
result loader for the read-only submitted view.

Use start/submit services for writes and outcomes. The minimum pool
rule belongs to start. Translate input fields into original option
keys by question ID. An empty submit leaves the form available with
a Bulgarian message, and a duplicate leads to the stored result. A
stale, unavailable or malformed plan is handled with a safe Bulgarian
response, without debug or state disclosure. Treat unexpected service
data errors as server errors. Keep the generic 404 for
foreign and missing rows including results. Keep the existing browsing,
legacy routes, auth/profile/DZI policies, quiz scoring and assignment
clock, quota and vault behavior.

## Templates

Use the project's existing Bulgarian language and assessment styles.
Keep the start/attempt/submit/result flow focused, with question order,
radio choices and a submit action. Render the saved score plus the
shared multiple-choice review, skipped count when relevant, and
"Нов опит" POST to the existing scope. No timer, Javascript framework,
new assets, grading logic, public login or enrollment changes.

## Verification and handoff

Test the actual disabled app route map and gate status. In isolated
fixture-armed app runs, cover logged-out and pending authentication
as appropriate, student/teacher/admin as owner and foreign, same-origin
and wrong-origin POSTs, GET/HEAD write-free, start/resume and topic
ownership, thin and stale scope, option ordering, empty/duplicate
submit and abandoned old tabs, owner result and new attempt action.
Legacy numeric quiz entry remains a generic 404. Use disposable test
DBs and actual app test client with no environment-based production
gate. Do not use real credentials or providers.

Run relevant new and existing access/integrity, quiz and practice
suites, compile changed Python/templates to scratch and scratch build,
FK/integrity. Run one final frozen full unittest discover with the
real site-enabled venv using -B and no outer -S. Record UTC start/end,
exit and file hashes; use a background shell without timeout.
Leave intended edits unstaged, document state and verification, and
provide stage list and commit/PR texts with the actual number and a
stable manifest. Separate Opus / High exact pushed SHA review.

## Exclusions

No production activation, deployed config, progress, DZI simulation,
browse links, schema/content changes, real database or sidecar access
(including listing or stat), live migrations/imports, providers or
credentials, external research, deployment/VPS, generated vault or
server/tunnel operations. Use fixture-only local work.
