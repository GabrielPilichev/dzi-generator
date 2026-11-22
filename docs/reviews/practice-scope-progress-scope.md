# C2 account-owned practice progress scope

Base 8287c05292bbbccc41402ee696e12b3e7daa99e9 (merged #372). Branch feature/practice-scope-progress in
its dedicated worktree. Implement the first-version section and topic
progress in docs/STUDENT_PRACTICE_DESIGN.md §4 using the reviewed
practice storage, default-off routes and browse entry.

## Allowed paths

- web/services/practice.py for read-only submitted-session progress.
- web/blueprints/browsing.py and web/templates/_practice_start_form.html
  for the armed signed-in browse entry display. A necessary small
  template change should stay with the shared form.
- tests/test_practice_routes.py for integration and a focused new
  tests/test_practice_progress.py for the read contract.
- Preserve app composition, gate values, existing routes/permissions,
  scoring and result rendering, schema, canonical data and other files.

## Behavior

Return the active signed-in account's submitted practice session count
and most recent saved correct/total for one real non-DZI scope. Use a
coherent read snapshot and the existing principal and scope validators.
Account ID comes only from the trusted principal. Match the section and
topic exactly, with NULL topic meaning the section-wide practice alone.
Exclude open and abandoned sessions, count each repeat independently,
and take latest by persisted UTC submission time with a stable ID
tie-break. Do not re-grade saved results or include assignment attempts.
Use the existing scoped SQLite index and reuse the current read-snapshot
helper. Decide scope availability and progress together so the armed
page does not validate the scope twice or show another account's data.
The start operation still makes its own authoritative checks. No writes.

Show the count and last result next to the practice entry for the same
scope when the existing route gate is armed and the user is signed in.
Use short Bulgarian labels and the existing layout. No public or
shared-cache account data, cross-owner view, topic roll-up, dashboard,
retention, export, new URL or gate. Section/topic/legacy pages stay as
before without arming, including response output and query path, and
anonymous browsing remains public. Preserve the existing start form,
sign-in confirmation, scope restrictions and authoritative start pool
decision. An old saved result remains counted after content changes.

## Verification and handoff

Use real disposable migration/canonical fixtures. Cover zero, open,
abandoned and submitted sessions, repeats, separate users and roles,
scope separation, timestamp ties and saved-score preservation through
content changes. Exercise read-only/query-only and caller transaction
behavior. Verify exact wrong-owner absence and no account data in
shared browse state. Verify default-off and signed-out HTML and SQL
parity, and actual signed-in progress with fixture-armed real app test
client. Existing practice and legacy browse/quiz behavior stays green.

Run affected practice, browsing, authentication and quiz route suites;
compile to scratch and verify a scratch build, FK and integrity. Use
the real venv with -B and no outer -S. Isolate temp/pycache and record
exact file hashes, UTC times and exits. Repeat full discovery only if
findings point to an impact beyond those affected suites.
Report nested armed counts separately from the ordinary test wrapper.
Leave intended files unstaged/uncommitted. Handoff report, review notes,
stage list, commit/PR text and stable manifest. Fresh Opus / High will
review the exact pushed implementation.

## Exclusions

No production activation or change to build gate, no real runtime DB
including list/stat/glob or sidecars, live import/migration, credentials
or providers, deployment/VPS, generated vault, server/tunnel or network
research. No DZI simulation, assignment summaries, cross-topic roll-up,
or larger UI/navigation work.
