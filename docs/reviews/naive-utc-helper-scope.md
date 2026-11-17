# A6 — replace deprecated naive UTC readers

Gabriel authorized A6 after PR #357 merged. Base:
`affeb6a7d9bbaf454cc6539213fa886bc963fe72`.
Branch: `refactor/naive-utc-helper`.
Goal: remove deprecated datetime UTC readers without changing clock semantics,
stored timestamp bytes or existing aware UTC clocks.

## Verified boundary

An AST scan of all tracked Python files finds three calls and one callable
reference: `web/services/quiz.py:1767`, `tests/support.py:74`,
`tests/test_assignment_accounting.py:521`, and the default `_wall_clock` binding
at `web/services/lifecycle.py:743`. The last is a callable reference, so a search
for calls alone misses it.

Fable's 19 matches reconcile to five `_utcnow` function definitions, thirteen calls
to those already-aware wrappers, and the quiz's one deprecated call. The five
wrappers in `src/manage_accounts.py` and the account, OIDC, recovery and
registration blueprints already use `datetime.now(timezone.utc)`. Leave their
names, aware return values, call sites and injection seams unchanged. Acceptance
means no deprecated `datetime.utcnow` attribute calls or bindings in tracked
Python, rather than removing legitimate `_utcnow` names from text.

## Allowed implementation

After this reviewed scope is committed and a draft PR exists, fresh Sonnet / High
may change only these five files:

- `web/services/lifecycle.py`: add one zero-argument `naive_utc_now()` helper
  before its default-clock binding, using explicit UTC and removing only tzinfo
  (`datetime.now(timezone.utc).replace(tzinfo=None)`). Preserve microseconds and
  sample the clock once per call. Bind the default `_wall_clock` to this helper.
  The module must remain standard-library-only, as its existing static test
  requires; introduce no common-module import or new clock abstraction.
  Its helper, comments and docstring must obey the existing whole-file timezone
  check: no `datetime.now()`, `date.today()`, `.astimezone()`, `time.localtime`,
  `tzlocal` or `fromtimestamp(` anywhere in `lifecycle.py`.
- `web/services/quiz.py`: replace only the reader inside
  `quiz_current_timestamp()` with the shared helper through the existing
  `lifecycle` import. Keep its name/signature and the registered
  `lambda: quiz_current_timestamp()` unchanged, so later patches remain visible.
- `tests/support.py`: replace only the unfrozen branch of
  `LifecycleTestClock.wall()` through its existing `lifecycle_service` import.
  Keep the frozen object, branching, reset/freeze/advance behavior and fixture
  installation unchanged.
- `tests/test_assignment_accounting.py`: replace only the reader in
  `RollbackScenarios.setUp()` through the existing `lifecycle` import. Keep
  `.replace(microsecond=0)` and every existing assertion/fixture unchanged.
- New `tests/test_naive_utc_clock.py`: meaningful deterministic regression tests
  for UTC/naive/precision, clock injection and timestamp compatibility.
  No deprecated `utcnow` attribute reference, even as an oracle or disabled code;
  use fixed substituted instants or independent `time.time()` UTC brackets
  (`datetime.fromtimestamp(t, timezone.utc)`).

Keep other imports and functions unchanged; no new dependencies, configuration,
modules, schema, canonical data, providers, auth/scoring or A7 cleanup. The scope
document is the separate preparation commit, not an implementation edit.

## Preservation and evidence

Keep `configure_wall_clock`, its dynamic lookup, `wall_reading` normalization,
clock-environment selection, writer-lock-before-sampling order, high-water and
boot-anchor checks, refusals and transactions unchanged. Existing serialisers,
parsers, timestamp precision/truncation and stored formats stay unchanged.
Do not convert naive data to aware data or rewrite old rows. Quiz note filename
fallbacks using local `datetime.now()` are outside A6 and remain unchanged.

Frozen `TEST_CLOCK`, `lifecycle_time()` and a patched `quiz_current_timestamp`
bypass the replaced readers: their probes prove downstream preservation only.
For replacement evidence, control the instant beneath each reader in scratch:
at head substitute `lifecycle.datetime` with a subclass whose `now(tz)` returns
the fixed instant converted to `tz`, and local time if `tz` is absent. At base,
substitute the corresponding subclass's `utcnow()` on the symbol each call site
resolves (`quiz._quiz_datetime`, `tests.support.datetime`, accounting's `datetime`).
Keep substitutions only around reader calls, then restore the module globals
before serializing: `format_timestamp` checks against `lifecycle.datetime`.
Feed sampled values through existing clock seams to real scratch writers, notes
and results; compare timestamp bytes without masking differences. Cover zero and
nonzero microseconds, UTC midnight and both Europe/Sofia DST transition instants.
Preserve whole-second truncation at existing callers.

The base default captured the built-in callable at import and cannot use that
substitution. Check base/head defaults against real `time.time()` UTC brackets
in fresh interpreters under at least two `TZ` values, and disclose this limit.
The default is also the production clock for `python -m src.manage_lifecycle`
(called by the restore script): that CLI imports lifecycle without quiz.
Use `sys.executable -B -c` subprocess probes that import only lifecycle and
`src.manage_lifecycle`, assert quiz/app absent from `sys.modules`, and verify
`wall_reading()` is naive, whole-second and inside the UTC bracket. Do not run
operator commands or the real restore script. Never reload lifecycle, quiz or
`tests.support`; after any in-process clock configuration, restore the exact
previous reader object with `addCleanup` or an equivalent guaranteed cleanup.

Test that the helper explicitly samples UTC once, frozen fixture time bypasses
the real clock, configured readers and later quiz-clock patches still govern
lifecycle reads, and the cold production default works before quiz registration.
The new tests must distinguish local-time/aware/truncated returns and a bypassed
clock seam; do not merely assert implementation text. Existing assertions and
fixtures remain unchanged except the two listed reader replacements in support
and accounting.

Run the new tests and affected lifecycle time/clock/static/production, quota,
accounting, quiz service/student/teacher route, deadline/result, fixture-isolation
and AGENTS auth/quiz suites without duplicating module runs. Include
`tests.test_lifecycle_operator` and `tests.test_lifecycle_restore_script`,
reporting any skips and reasons. Use bounded paired base/head probes as specified
above, with disposable databases only; disclose normalization precisely.
AST/source review permits only the helper, default binding and three reader
substitutions; scan all tracked Python for deprecated attribute references.

Because the common unfrozen test clock and lifecycle default change, one final
full unittest discovery must actually pass after focused checks. Preserve logs,
exit status and tested hashes; start a long run as a tracked background task.
On failure reproduce affected cases at the exact base before proposing repairs;
no unrelated fixes or inferred full-suite success. Repeat only for a concrete
correction or unresolved failure. Compile to scratch and check whitespace.
AGENTS DB/audit checks use explicit scratch databases; disclose the known absent
official-PDF audit exit 1. No real runtime DB/sidecars, generated vault,
credentials/providers, gates, servers/tunnels, VPS or deployment operations.

Preparation is read-only source/AST/document and whitespace review; no app
execution, tests or database build. Implementation leaves edits unstaged and
uncommitted with per-file changes, commands/results and limitations. Gabriel
commits/pushes; a fresh Opus / High reviews the exact pushed SHA. After approval,
print complete guarded merge commands directly in chat; Gabriel executes them.
