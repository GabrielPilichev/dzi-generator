# C2 practice confirmation and retired-scope result scope

Base f0f62a6e6f9a3364bd67895428202938eef77372 (merged #373). Branch feature/practice-confirmation-restart
in its dedicated worktree. Close the four reproduced pre-activation
findings from the independent exact-commit review of #372
(f30144accd4e12775b2f6f1b14ce331b43efbd6f).

## Allowed changes

- web/templates/practice_start.html and web/templates/practice_result.html.
- web/services/practice.py and web/blueprints/practice.py for one
  result-snapshot scope fact and the existing new-attempt form.
- docs/AUTH_ROUTE_CONTRACT.md for the current armed behavior.
- tests/test_practice_routes.py and tests/test_practice_result_loader.py
  for the affected regressions.

## Behavior

Use accurate short Bulgarian confirmation copy. The read can promise one
explicit submit action, with the start subsequently deciding whether to
resume, replace, create or refuse. It makes no attempt-state lookup.

Preserve the signed-in owner's saved result and score for a retired
section or topic, with the existing unavailable review text. Suppress
the "Нов опит" form only when the scope is retired according to the
current result's scope resolution. Use that decision in the same read
snapshot. No extra read, attempt-state change or regrading. A real scope
with all questions now unavailable retains the explicit POST and its
existing authoritative start behavior. The return to the home page
remains available. A legacy or stale tab POST keeps its existing status.

Document that a gated signed-in request to a DZI browse surface can
perform a read-only scope exclusion check, and that the attempt page
currently omits a scope title. Reflect the merged progress read helper
and display in the browse paragraph. Record migration 026 verification as
a prerequisite before arming. Keep access
policy, route map, score, IDs, start/submit flow and gate unchanged.

## Verification

Disposable fixture-armed real app and canonical/migration fixtures.
Verify both sign-in confirmations still perform no session read or
write, and no pre-existing session outcomes change on POST. Cover a
valid scope with renderable questions, a real scope with every previous
question unavailable, and retired section and topic scopes. Confirm the
saved score, no restart form in the retired result, legacy restart POST
404, live scope same-origin form, foreign owner 404, query/transaction
preservation and gate-off URL and browse parity. Relevant practice and
route suites, scratch compile/FK/integrity and whitespace check only
unless a concrete broader impact warrants a wider suite.

Leave exact intended paths unstaged. Provide timestamped logs, frozen
hashes, report, review handoff, stage list, commit text, PR body and a
stable evidence manifest. Separate Opus / High exact-SHA review.

No runtime DB access, deployed services, credentials, provider contact,
generated vault, network research, imports, DZI simulation or activation.
