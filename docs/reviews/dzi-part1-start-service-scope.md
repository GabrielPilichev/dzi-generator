# DZI Part 1 simulation pools, start, and clock recovery scope

Implement C3.2 on the merged C3.1 storage in the dedicated
`feature/dzi-part1-start-service` worktree at base `9c03dcf4cc4599ebe16cf6268887ebcb18940f03`.
The accepted contract is [DZI Part 1 simulation design](../DZI_PART1_SIMULATION_DESIGN.md),
particularly sections 3–7, the C3.2 acceptance in section 9 and
implementation order in section 10. Migration 027 already ships both
runtime tables. This slice establishes the first writer and changes
the existing clock-recovery command in the same PR.

## Change boundary

- New `web/services/dzi_part1.py` for the dormant service. Reuse
  existing question, visual, ownership, clock and connection
  primitives without modifying their callers or shared policy.
- `src/manage_lifecycle.py` for the existing clock-inspect and
  clock-recover outputs and the guarded recovery update.
- New database-backed service and recovery regressions in
  `tests/test_dzi_part1_start_service.py` and
  `tests/test_dzi_part1_clock_recovery.py`. Existing
  `tests/test_lifecycle_operator.py` may be adjusted only if its
  fixtures or exact output assertions need the added count.

Keep migration 027, canonical data, statement shapes of existing
assignment operations, C2 practice, DZI admin, teacher generation,
assignment routes, quota and token behavior intact. There is no
route registration, UI, grading, submit/result loader, exposure flag
change, or activation in C3.2. Keep scope in these paths. Do not put
scratch probes, transient databases or reviewer inputs in the repo.

## Blueprint and eligible pools

Only a new attempt needs the active IT DZI blueprint for the
repository format. Require the 25 contiguous Part 1 slots in their
supported kind and points pattern (15 multiple choice at 1, 10 short
answer at 3), integer and bounded positive Part 1 minutes, and no
topic, section, topic-area or required-asset constraint. Derive slot
counts, slot format, points and duration from the validated rows. Do
not use the entire paper's total or the Part 2 configuration. Pin
creation with a tampered blueprint and record the duration bound and
its unit. The current initial duration is 90 minutes, with a 60-second
stored submit cutoff extension. Both are app timing.

Compose unique candidates by kind from primary links to tasks 1–25 of
an IT DZI exam of the same format, with IT question ownership, DZI
level and matching canonical exam/year. Task number does not dictate
the candidate kind or its destination slot. Apply the existing
approval and renderability predicates. No AI-generated candidates,
secondary or legacy unlinked candidates, Part 2, cross-subject
borrowings, or repeated ids. Use the existing visual loader and
allowed image types and roots. For short answer apply the existing
generation-readiness check and require positive, valid part weights
whose sum is the slot value and equal weights in order-independent
mode. Missing stimulus ids 67, 131 and 202 are a service-local
exclusion at pool composition. Do not rewrite shared readiness
patterns or public rendering rules. Check current pool counts of 105
multiple choice and 53 short answer, with per-exam counts for the
latter 6, 7, 9, 7, 9, 7, 8, before relying on synthetic cases. These
counts are test evidence, not a service condition.

## Owner and transaction contract

1. Accept an account principal only and recheck active state under the
   lifecycle writer lock. Limit decisions and writes to that account.
   Keep existing connection ownership and transaction checks.
2. Read the open row before any blueprint or pool query. Validate the
   stored flat set of 25 distinct positive question ids and its
   accompanying 25 kind/points objects, with their saved maximum and
   relevant time invariants. Reject malformed or inconsistent pairs
   atomically. Make state a pure decision from stored timestamps and
   the one lifecycle time. Neither the active blueprint nor the grace
   constant may change the state or saved weights of a stored row.
3. Resume an open attempt with any still-presentable item. Judge
   existing items against their stored kind and points and current
   per-item rendering and readiness, without rebuilding the pools or
   applying a later change to the stimulus exclusion list. Return the
   same attempt throughout grace, before any stale-item decision. At
   or after the stored cutoff, mark expired. An inconsistent row
   stamped after lifecycle time, or a pre-deadline row presenting
   nothing, is abandoned. Guard each write on id, owner and open
   state. Only marking or inserting invokes `admit()`. An unrelated
   row timestamp never invokes `check_factual()`. Read-only exits
   discard the transaction, even during grace, without a new
   high-water mark. Ordinary lifecycle refusal can still store the
   pre-existing clock-fault record.
4. Only after handling the open row, validate the active blueprint
   and compose both pools for creation. Return
   `blueprint_unavailable` or `pool_insufficient` as appropriate.
   Keep a legitimate terminal marking on either outcome; roll the
   transaction back on unexpected failure. Pick one cryptographic
   seed; use one seeded draw over both sorted pools with distinct ids
   and fill matching slots in order. Store flat question ids and
   separate slot format, planned maximum and duration. Set started,
   deadline and cutoff from one admitted `now`. No other runtime
   row is created except the normal clock update.

## Clock operations and evidence

Use the existing `clock-inspect` read-only path to add only an open
C3 row count. Add the number abandoned to the existing recover
output and audit entry. In the same established writer transaction
and after the verified-clock checks, recover stamps every still-open
C3 row with the verified time, irrespective of suspect interval or
whether that time precedes its start. Terminal C3 rows, answers,
existing assignment hold/floor logic, schema refusal, audit and
rollback behavior stay intact. Do not duplicate clock logic or add
a partial recovery mode. This necessarily abandons honest open
attempts during a recovery too.

Use the real fixture clock helper and public CLI entrypoint on
disposable databases. Exercise real writer races, failed second
writes, distinct owners, source/pool exclusions and no borrowing,
stored cutoff at all edges, stale and malformed rows, and persistence
after changes to blueprint or grace. Test the count and transactional
rollback with and without a suspect interval, terminal row and
assignment preservation, no-op recovery, and refusal with 027
pending. In the two reviewed recovery sequences (+6 h, recovered
before the stamp; +1 h, recovered after it), verify the stored
abandonment persists as time moves past the stamp and the next start
can create a new row. C3.3 will verify submit refusal against it.

Characterize same-boot regression/step and latch, unsynchronised
clock and reboot/boot-time input. Reproduce the final #375 review
case where a boot anchors 60 seconds ahead, a later start is 120
seconds ahead, and correction plus a request at five seconds behind
the mark is admitted. Carry that limitation to activation. Preserve
the reviewed undetected-reboot case: a forward-stamped attempt can remain open
after correction without a lifecycle transaction before reboot and
with the wall clock back within high-water tolerance. At this slice
test the remaining stored time and persisted clock state only.
Actual deployed boot inputs and the owner decision on the undetected
case remain activation prerequisites.

Run focused migration, clock, operator, C2 and DZI assignment
regressions using the real site-enabled interpreter. Use compile and
whitespace checks and a scratch integrity/foreign-key check. Report
observed counts, all exclusions or limitations, variant probes, the
exact allowed path diff and no remaining jobs. Preserve the live
runtime database and sidecars, vault output, server, tunnel,
providers, production clock, VPS and gates. All lifecycle operator
commands in this slice run against disposable paths.
