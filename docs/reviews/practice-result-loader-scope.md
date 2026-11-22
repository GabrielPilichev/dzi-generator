# C2 practice result loader scope

Base 6f123f252e0c5e252421d295d773980faca3877e (merged #369). Branch feature/practice-result-loader in
its dedicated worktree. The approved student practice design
(docs/STUDENT_PRACTICE_DESIGN.md §§2–5), migration 026, merged start and
submit services, and reusable multiple-choice review guide this slice.

## Allowed implementation files

- Extend web/services/practice.py with a read-only result loader.
- Add tests/test_practice_result_loader.py.
- Preserve start and submit semantics, existing tests, quiz result context,
  reusable template, app and route code, schema and canonical content.

## Result contract

Take a configured caller-owned SQLite connection, the trusted account
principal and a positive session ID. Read only the active account's own
submitted session. Use the same safe unavailable outcome for a missing,
foreign, open or abandoned session, including when the requester is a
teacher or admin. Preserve the account principal validation contract and
verify current activity under the same read snapshot as the lookup. No
role bypass, profile requirement, timer or write.

Use one coherent SQLite read snapshot for account, session, answers and
current content. Reuse an existing caller transaction if present and
never end it. If the loader starts the read transaction, end only its
own transaction with explicit SQL, including under autocommit=True.
Support a configured query-only connection. Propagate unexpected
database errors and preserve connection ownership.

For an owned submitted session return the persisted ID, scope, time and
score, with review rows and skipped count for the later result page.
Use the stored seeded order and original option keys. Load only IDs with
saved answer verdicts and keep their plan order. A question that was
skipped at submission must not later appear as graded. Validate plan and
answer consistency without attempting repair, recalculating a verdict,
or rewriting the score. Use the existing practice errors for invalid
stored state. Return the saved score even when no current question may
be shown.

Use the start and submit services' current eligibility rules. Questions
that are now unavailable, out of scope, unapproved, officially DZI, or
unrenderable should be omitted. An unavailable scope yields no review
questions. Compute how many planned questions are omitted and identify
when only the saved score survives. Reuse quiz_load_questions with the
stored seed, the two current explanation fetchers and the existing
review conventions. The rendered prompts, options, correct keys and
explanations reflect current content. The verdict on each review row
is the stored one. Output is compatible with the shared partial without
assignment identity, profile data, token or teacher context. No
changes to legacy quiz result, score or load functions.

## Verification

Use disposable migration and canonical fixtures. Cover active and
inactive accounts, foreign and missing IDs, open and abandoned sessions,
type validation, malformed or mismatched
plan/answer state, stable scores and verdicts through content changes,
current eligibility and stale result, formerly ungraded items, stored
order/seed and option keys, and the shared markup data shape. Exercise
start→submit→read and deliberate content changes. Prove read-only
behavior on a configured query-only connection and explicit autocommit,
with caller transaction ending rules and coherent snapshot. Assert
unchanged runtime and legacy state. Run the relevant practice, quiz
result, auth and migration suites, compile to scratch and verify a fresh
build/FK/integrity. Full site-enabled unittest discover once on frozen
final files, in a background shell. Record hashes, UTC start/end and exit.

## Exclusions and handoff

No practice routes, templates, progress queries, config or activation.
No real database or sidecars, live import or migration, provider or
credential use, network research, deploy/VPS, generated vault, server
or tunnel operations. Report the result shape, limitations and test
evidence, with exact status and hashes. Leave only the two paths
unstaged/uncommitted. Prepare commit and PR text and stable manifest.
A separate Opus / High review follows the exact pushed implementation.
