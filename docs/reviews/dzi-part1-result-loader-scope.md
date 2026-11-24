# DZI Part 1 simulation result loader scope

Implement C3.4 in `feature/dzi-part1-result-loader` at base
`2a9560d69b49162231e943591a7ab3358e701cc1` after C3.3 is merged. Follow
[the reviewed design](../DZI_PART1_SIMULATION_DESIGN.md) sections 5,
6.6–6.7 and 8–10. Submission already stores per-part verdicts and an
effective score. Load those persisted values for the owning account.

## Change boundary

- `web/services/dzi_part1.py` for a dormant result loader.
- `web/templates/_multiple_choice_result_review.html` for optional
  saved slot numbering, with no change to existing callers.
- New `tests/test_dzi_part1_result_loader.py` and
  `tests/test_dzi_part1_result_partial.py` for direct-fixture result
  and legacy render-equality regressions.

Leave schema, canonical content, shared grading, submit, recovery,
routes, access policies and feature gates in their existing state.
No result route, complete result page, attempt view, client timer,
draft behavior or activation in this slice. Use only temporary
databases and in-process rendering. No real database or sidecar,
server, tunnel, providers, clock operation, deployment, VPS or vault
output. Leave author edits unstaged and uncommitted.

## Owner, snapshot, and persistent results

Take the trusted account principal and attempt id. Read the active
account, attempt and its current-content review rows in a single
read snapshot. Missing, foreign, open, expired and abandoned rows
share the not-found result. Do not recheck the current blueprint or
clock for a successfully submitted, readable result. This read
neither admits nor stamps. Preserve caller connection ownership and
the established read-transaction constraints, with no remaining
transaction or state change from a read. Prove the reader sees one
consistent snapshot when content can change from a separate
connection.

Load the persisted effective score and kind subtotals from the saved
answer rows. Confirm internal consistency against the saved flat plan,
slot format, part weights and score, with fail-closed behavior for
corrupt stored state. Do not repair, regrade, read a current key to
derive a verdict, replace a saved maximum with the planned maximum,
or invent answer rows for skipped items. Report the original score
even if there are no current review items. Keep original slot order,
including gaps.

For still-presentable saved items, use current prompts, choices,
accepted answers, visuals and explanations through existing read
helpers. The saved verdict, awarded and possible points, and raw
answer or chosen key remain the authoritative submission. Current
content can differ, which later copy will disclose. A planned item
unavailable at grading stays omitted. Count saved items that can no
longer be rendered or no longer meet their compatible current
per-item check. Keep the result's stored total even after content or
blueprint edits. Do not recompose a pool or consult the stimulus
list for an existing attempt. Carry the inherited order-independent
matcher and clock limits to activation unchanged.

## Shared partial

Provide an explicit opt-in for the new loader's saved slot number.
Keep legacy assignment and C2 practice behavior by default. Render
their existing result contexts byte-identically to the base,
including skipped/unanswered and stale cases. A DZI result with a
gap must show the original saved slot number. Do not change any
shared grading or source-disclosure behavior or introduce a public
consumer here.

## Verification

Exercise owners and missing/terminal states, no-write snapshots,
canonical 45-point and effective-score examples, distinct saved
subtotals, changed current keys and accepted texts, removed structures
and empty review, corrupted stored result rows, and actual saved slot
numbering. Use temporary fixture databases, real helpers and Jinja
rendering inside the test process. Run focused direct-fixture
regressions, compile and whitespace checks, and scratch integrity
and foreign-key checks. Use the previous review's scratch path guard
for incidental default runtime pathname resolution before a syscall.
Do not run status --ignored or modify tracked test support.
