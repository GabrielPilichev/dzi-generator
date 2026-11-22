# C2 practice browse entry and safe sign-in return scope

Base bc2ea41d0e2b2898cefdd3fc37ab8f270e4606b7 (merged #371). Branch feature/practice-browse-entry in its
dedicated worktree. Follow docs/STUDENT_PRACTICE_DESIGN.md §§3–6,
the reviewed default-off practice routes and the launch-first roadmap.

## Allowed paths

- web/blueprints/browsing.py, web/blueprints/practice.py and
  web/services/practice.py.
- web/templates/subject_section.html, web/templates/subject_topic.html,
  web/templates/section.html, and new web/templates/_practice_start_form.html
  and web/templates/practice_start.html as needed. Update
  web/templates/practice_result.html only for the current-content note.
- tests/test_practice_routes.py and docs/AUTH_ROUTE_CONTRACT.md.
- Preserve app composition, existing gate values, all other routes,
  access policy, CSS/JS, launch config, schema and canonical data.

## Browse entry

Add a Bulgarian “Упражнявай се” POST entry to the existing real
section and topic pages when the practice routes are armed and the
current principal is signed in. Keep public browsing and existing
teacher actions intact. Use resolved section and topic IDs from
the existing browse contexts. Reuse the practice scope validation
to exclude DZI-training sections, placeholders and invalid topics.
Do not use browsed question counts as a substitute for the start
service's eligibility or minimum. An existing attempt can be
resumed with fewer questions, and start remains authoritative for
thin pools. Preserve legacy IT section pages and the newer subject
section and topic routes. In the disabled build the ordinary browse
output and DB query path stay unchanged, with no new URL lookup.

## Recover the sign-in return

The start endpoints are currently POST-only. After a signed-out
same-origin POST, the existing sign-in flow returns with a GET and
receives 405. Give those same two endpoints safe signed-in GET/HEAD
confirmation for the supplied section or topic scope. Persist nothing on
GET/HEAD, including for an already open, stale or thin attempt. The
confirmation offers an explicit Bulgarian POST to the exact same
scope. There is no automatic replay of the original POST, no change to
login, and no new redirect target or trust in form-supplied identity.
Validate scope and IDs read-only with existing rules. POST continues
through the tested start/resume writer and same-origin hook. Respect
the documented default-off registration, with no environment override.

## Review carry-forward

Correct the route contract's retired-scope statuses: start 404,
attempt/submit 409 and owned submitted result 200 with the saved
score. Update the old POST-only and link statements. A malformed
stored plan still fails closed with the safe 409. Log the endpoint
and exception class only, without IDs, labels or plan content. Record
the separate operator repair decision: restore a verified original
plan if available, else use explicitly authorized maintenance to
abandon the open row. This slice does not implement or run that repair.

On results with questions, include a short Bulgarian note that the
saved score is from submission and the visible review uses current
question content. Keep the saved verdict and score, current options
and shared review markup. Do not otherwise alter result behavior.

## Verification

Use the actual app with disposable fixture DBs and independent test
processes when arming the existing constant. Verify the ordinary
disabled route map and deterministic browse output against base.
Verify new forms on real section/topic scopes with mapped exact URLs,
including legacy sections, and absence for disabled, signed-out,
placeholder and DZI cases. Exercise signed-out POST → fixture login →
GET confirmation → explicit same-origin POST. Include HEAD and
rejected origin cases, no GET/HEAD writes or replay even for stale
or thin sessions, unavailable scope, resumed attempts, and unchanged
owner result/repeat and legacy route behavior. Verify safe malformed
plan logging and the changed-content note. Keep the relevant old
test assertions except for deliberate changes to practice entry and
start method expectations. Document all declared policies.

Run relevant route, access, quiz, browse and practice suites, compile
changed Python/templates to scratch, and scratch DB FK/integrity.
Use one full frozen standard site-enabled unittest discover with
a background shell (no outer -S). Record hashes, run timings, exit
and proven counts. Leave allowed edits unstaged/uncommitted, with
report, test evidence, stage list, commit and PR text and stable
manifest. Separate Opus / High reviews the exact pushed final commit.

## Exclusions

No production activation, server or tunnel, deployed config, VPS,
providers or credentials, real runtime DB or sidecar access including
list/stat/glob, live import/migration, generated vault, schema or
canonical data, existing auth policy changes or query-state mutation
from GET, DZI simulation, progress or other navigation edits.
