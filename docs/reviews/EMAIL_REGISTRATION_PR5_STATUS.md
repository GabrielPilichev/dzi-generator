# Email registration, PR 5 — operations: status and contract

Design: `docs/reviews/EMAIL_REGISTRATION_DESIGN.md` (sections 3.8, 3.9, 6.5, 6.6, 9;
decisions D18 (b) and D20 (a)).
Tests: `docs/reviews/EMAIL_REGISTRATION_TEST_PLAN.md`, DR1–DR9, N5–N7, N9.
Earlier PRs: `EMAIL_REGISTRATION_PR1_STATUS.md` to `PR4_STATUS.md` (merged: #335–#338).
Operator guide: `docs/DEPLOYMENT.md`, section 34 (and sections 16, 17, 19).

**Status:** PR #339, branch `feature/registration-operations`. Committed: `36cffbe` (implementation)
and `b719728` (fixes after the first independent review). The second independent review, of
`b719728`, asked for changes to the timer recovery; that fix (below) is **unstaged and uncommitted**
on top of `b719728`, **awaiting independent review, not approved**. Registration is **not
activated**: both build gates stay off. No migration, no schema change, no deployment, no
provider, no real database, no real message, no server. Nothing here has run under systemd or on
Linux.

## Review history

### First independent review, of `36cffbe`: REQUEST CHANGES

Three blockers; `b719728` addressed them. The second review found that the timer fix (1) did not
resolve the finding; it resolved findings 2 and 3 independently.

| # | Finding | Fix in `b719728` | Second review |
| --- | --- | --- | --- |
| 1 | `deploy/learnpilot-restore.sh`: SIGTERM after the first timer restart (`timer-start#1=TERM`) left exit 143 with the cleanup timer off; the lock and its marker were already gone, so a later restore saw an inactive timer and never restored it. | `restart_timers`, called again from `finish` after the start. | **Not resolved**: see below. |
| 2 | `registration_scrub.has_pending_storage` treated an existing table lacking a column (`closed_at` renamed) as absent legacy storage: unscrubbed pending data was published with `quick_check=ok` and a valid hash. | Only a missing table returns "nothing to scrub". An existing table without a required column raises `UnsupportedRegistrationSchema`; `scrub_pending_registrations` turns it into `BackupError`, so nothing is published and the partial file is removed. Regression: `SnapshotScrubTest.test_an_existing_registration_table_that_cannot_be_scrubbed_fails_the_backup_closed`. | Resolved. Unchanged since. |
| 3 | `registrations-release-hold` looked for the restore lock only before `BEGIN IMMEDIATE`. | Looks again with the write lock held; refuses, the dispatcher rolls back, the hold, its time, the mark and the ledger stay. Regression: `OperatorCommandsTest.test_a_restore_lock_made_while_the_release_waits_for_the_database_stops_it`. | Resolved. Unchanged since. |

`b719728` also corrected the gate's documentation: the gate **does take the current time** for the
timestamps it writes (`closed_at` of voided registrations, the time of a new hold). It does not
accept or reset the clock and never sets or changes the high-water mark.

### Second independent review, of `b7197288b0eab9d1f80e8ab3ce8309ff07dc28f8`: REQUEST CHANGES

Remaining blocker: `restart_timers` cleared a timer's flag even when `systemctl start` failed. For
either timer: one failed restart ended with exit 0, the timer inactive and the intent gone; a later
failure-free restore ended with exit 0 and the timer still inactive; a start and its retry failing
around SIGTERM ended with 143 and the intent lost; a SIGKILL before or between the restarts lost
the intent too (it lived in the lock, which is removed before the start, and in shell variables).
Advice to check the timers by hand does not meet design 3.9 ("the timer comes back on every path
the backup timer does").

### The timer fix (unstaged, awaiting independent review)

Protocol, in `deploy/learnpilot-restore.sh`:

1. **Note, outside the lock.** One file per timer in the state root, root-owned like the lock and
   written the same way (`sudo sh -c 'printf ... > file'`), holding the unit name:
   `$STATE/restore-pending-backup-timer`, `$STATE/restore-pending-registration-cleanup-timer`.
   They block nothing (the application and both units look only at the lock).
2. **On disk before the stop.** For a timer found `active`/`activating`, or already noted: the note
   is written, then `sync -- NOTE $STATE` (file, then directory), each a gate, and only then is the
   timer stopped. A run that cannot write or flush the note stops before it stops the timer.
3. **Never dropped by an observation.** A note found when the lock is taken (fresh or taken over)
   makes the timer "to be started again", whatever state the timer is in now. Markers an earlier
   version of the script left in a taken-over lock (`backup-timer-was-active`,
   `registration-cleanup-timer-was-active`) are taken over as notes. A timer that was inactive and
   not noted is never started.
4. **Removed only after a successful start.** `restart_timers` runs
   `systemctl start TIMER && rm -f -- NOTE` for each noted timer, clears the flag only if both
   succeeded, always goes on to the other timer, and returns 1 if any timer failed.
5. **Failures propagate.** At the end of a successful run, a failed restart is `die 1` (no
   `RESTORE COMPLETE`). `finish`, for a run stopped or failed after the start, retries
   `restart_timers` and exits with the status the run ended with (143/130/129 for a signal, 1 for a
   failure), even if the retry succeeded. After a failure that gave the lock back, and after
   "restore complete, service not running", `finish` restarts the noted timers the same way.
   Whenever a timer is still noted at the end, `NOT ACTIVE AGAIN YET` is printed with the paths.
6. **Recovery by the next run.** Right after `sudo true` in the pre-flight, if no restore lock
   exists, each noted timer is started (and its note removed) before anything else. If that fails,
   the run says so and goes on; the note stays, the run retries at its end and cannot exit 0 while
   the timer is not active. With a lock present nothing is started in the pre-flight: a restore is
   running or unfinished, and the run that takes the lock over starts the timer when it ends.

What this guarantees, from the script's order (shim evidence only): from the moment a timer is
stopped until it has been started again, a note naming it is on disk outside the lock, and no exit
status 0 is possible while a noted timer is not active. A SIGKILL or power loss anywhere leaves
either the timer active or its note; between a successful start and the removal of the note, both
(the next run starts it again, harmlessly). What it does **not** prove: that GNU `sync FILE DIR`
makes the note survive a power loss on the production filesystem (an assumption about coreutils
and the filesystem; unverified), that real systemd behaves like the shim, or that a timer that
systemd refuses to start can ever be started; such a timer stays noted and every run fails until
it is fixed. If two restores are started at the same instant, the pre-flight recovery of one can
start a timer the other has just stopped under its lock; the units' `ConditionPathExists=!` and
the cleanup command's own refusal keep that timer from doing anything until the lock is gone.

Regressions (in `tests/test_registration_restore_operations.py`, `InterruptedRestoreTest`, each
timer separately, and `tests/test_lifecycle_restore_script.py`, `BackupOverlapTest`): first restart
fails and the retry succeeds (exit 1, timer active, note gone); restart and retry both fail (exit 1,
note kept, other timer started) and a clean run recovers it before its pre-flight; repeated failure
across runs keeps the note and fails each time although the timer is found inactive; TERM, INT and
HUP right after a restart (exit status kept, both active); a restart and its retry failing around
TERM (143, note kept) and recovery; SIGKILL before the first restart, between the restarts, and after
a start before its note was removed, each followed by recovery; SIGKILL after a note was flushed and
before the stop (taken over under the lock, started at the end); an unresolved note surviving a run
that leaves the lock; a pre-flight failure after recovery and with recovery failing; initially
inactive timers under a signal, a failed restart of the other timer, a SIGKILL and a clean run;
the order note → flush → stop and the notes' place and mode. Mutation evidence, on disposable copies
of the tree outside the repository, running `InterruptedRestoreTest` and `BackupOverlapTest`
(23 tests, with subtests): the fixed tree passes; the script of `b719728` fails 24 cases; reverting
one part of the fix at a time fails 13 (a failed start clears the note), 8 (no pre-flight
recovery), 1 (note written after the stop), 10 (no retry in `finish`), 3 (an inactive timer drops
its note) and 7 (a failed restart only warns, exit 0).

## What PR 5 contains## What PR 5 contains

| Piece | Where |
| --- | --- |
| Cleanup unit and timer (every ten minutes) | `deploy/systemd/learnpilot-registration-cleanup.service`, `.timer` |
| Cleanup coordination and restore gate in the restore script | `deploy/learnpilot-restore.sh` |
| `restore_gate`, `restore_status`, `release_hold`, `NO_MARK` | `web/services/registration.py` |
| One definition of the scrub (columns, `void_pending`) | `web/services/registration_scrub.py` (stdlib only; `registration.py` uses it for `_SCRUB`) |
| Commands `registrations-restore-gate`, `registrations-restore-status [--expect held\|open]`, `registrations-release-hold --acknowledge-lost-history` | `src/manage_accounts.py` |
| Cleanup command re-checks the restore lock once the write lock is held | `src/manage_accounts.py` |
| Logical snapshot scrub (D18 (b)) | `src/backup_sqlite.py` (`scrub_pending_registrations`) |
| Tests | `tests/test_registration_operations.py`, `tests/test_registration_restore_operations.py`; adapted `tests/test_lifecycle_restore_script.py` |

## The contract

### Cleanup timer and service

- `OnCalendar=*:0/10`, `Persistent=true`; the service is a `oneshot` running
  `python -m src.manage_accounts --db ${DZI_DB} registrations-cleanup` as `learnpilot`, with the
  backup unit's settings file (state paths only, no secret), the sandbox of the other units, and
  `ReadWritePaths=/var/lib/learnpilot`. `ConditionPathExists=!` the restore lock, as the backup
  unit.
- Overlap: a oneshot service is not started again while it runs. A run that cannot take SQLite's
  write lock exits non-zero (unit `failed`, nothing changed); the next tick retries. No `Restart=`.
- Clock first (unchanged service behaviour): a clock behind the high-water mark → exit 1, nothing
  written. The hold does not stop the cleanup (design N3).
- The command refuses while the restore lock exists, and looks again after taking the write lock
  (a restore that locks while a run waits for the database stops the run before it writes). A lock
  made after that look is caught by the restore's fingerprint of the live database, as a failed
  restore (design 3.9).

### Restore script

- After the lock and the backup-timer/service handling, and before the live database is
  checkpointed and fingerprinted: the cleanup timer is stopped (if it was active; noted first,
  outside the lock, in `$STATE/restore-pending-registration-cleanup-timer`, flushed to disk) and
  the cleanup service must be `inactive` or `failed`. A first look in the pre-flight avoids a wasted start. Any other state, or a state
  systemd cannot give, ends the restore (`activating`, `active`, `reloading`, `deactivating`:
  "a registration cleanup is running").
- The timer is started again wherever the backup timer is: after a failure that gave the lock back,
  after "restore complete, service not running", after a successful start, and after a run stopped
  or failed once the service was started. A timer that was not active is never started. Its note
  goes only once the start succeeded; otherwise the run does not exit 0 and the next run starts the
  timer first (no lock) or at its end (lock taken over). Protocol: "The timer fix" above.
- Step 4b, after the migration of the copy and before the final fingerprint:
  `registrations-restore-gate` on the staged copy. Step 8 and step 10: `registrations-restore-status
  --expect held` (staged copy, then the database in place). Nothing of the gate runs after the
  fingerprint.
- The script never runs `registrations-release-hold`.

### The gate (`restore_gate`, `registrations-restore-gate`)

One caller-owned `BEGIN IMMEDIATE`: every `pending` registration becomes `void` and loses all nine
columns of the CHECK; the hold `restore` is set. A hold the copy already carries is kept with its
original time. Repeatable; atomic (an exception rolls the caller back; tested by failing each
statement). It reads no other database, touches no account, profile, subject, ledger or canonical
row, and neither raises nor lowers the high-water mark. It does take the current time for the
timestamps it writes (`closed_at` of each voided registration, the time of a new hold); that time
is compared with nothing, does not accept or reset the clock and never sets the mark. An absent control row is created with the
mark `1970-01-01 00:00:00` (`NO_MARK`, "no mark yet", what an absent row means).

### Status and release

- `registrations-restore-status [--expect held|open]` never writes (`PRAGMA query_only`). With
  `--expect` it fails unless no registration is pending, the control row exists and the hold is in
  the expected state. (Recovery credentials: see "Deviations".)
- `registrations-release-hold --acknowledge-lost-history` prints the consequence first; without the
  flag it exits 1 and changes nothing. It clears the hold and nothing else: not the mark, not the
  ledger, not a cap. If the clock is behind the mark it says registration still answers
  "unavailable" until the clock passes the mark or the operator runs `registrations-clock-accept`.
  It refuses while a restore lock exists. No automatic release exists anywhere.

### Snapshot scrub (D18 (b))

In `backup_database`, after the online-backup API wrote the isolated partial file and the journal
mode was switched, `scrub_pending_registrations(dst)` voids and scrubs pending rows in that file
only, in its own transaction. It runs before `quick_check`, the fsync, the publishing link and the
`sha256`, so every check and the returned hash describe the published file. It leaves accounts,
profiles, canonical content, the ledger, the control row (hold included) and final rows as they are,
and does not set a hold. A database without the `pending_registrations` table (before migration 021) is backed up unchanged; a table that exists without a required column fails the backup. A
failure raises and publishes nothing. The summary gains `registrations_scrubbed`.

**Logical, not physical.** No claim is made about free pages, unused space inside pages, the WAL of
the live database, earlier snapshots, off-server copies or storage below the application; no
`VACUUM`, `secure_delete` or checkpoint setting is added. The unselected rebuild option is not
implemented.

## Caller and operator obligations

- Install the units only after this build is deployed and migrations 021/022 are applied; run
  `systemd-analyze verify` first (DEPLOYMENT 34).
- After every restore: check `registrations-restore-status`, then release deliberately, accepting
  the lost issuance history. Do not release to "fix" a clock: check the time and use
  `registrations-clock-accept` separately.
- Do not replace a database by hand, outside the script: it gets no gate.
- Do not run `registrations-restore-gate` on the live database except as a deliberate act: it voids
  pending registrations and closes registration.
- Add the timer to the health checks (DEPLOYMENT 19) and watch `registrations-cleanup --check`.

## Deviations and interpretations, for the reviewer

1. **Recovery credentials.** The design's gate and status also cover "recovery credentials of PR 8".
   They do not exist; nothing was invented. The gate, the status check and the snapshot scrub must
   gain them with PR 8.
2. **`--expect` states are `held` and `open`.** The design names only "the expected state"; `open`
   is the opposite, used for the release check.
3. **`NO_MARK` for a created control row.** The design says the gate reads no other database. It
   takes the current time for its timestamps, but it must not accept a clock: the high-water mark
   column is NOT NULL, so an absent row is created with an epoch placeholder instead of the current
   time, which keeps "no hold or mark" faithful and avoids accepting any clock. The first issuance or cleanup then raises it.
4. **A shared module `registration_scrub.py`.** It makes `src.backup_sqlite` independent of the
   application's imports while keeping a single list of scrubbed columns. `registration.py` changed
   in one line (`_SCRUB`).
5. **Release refuses under the restore lock**, and **the cleanup command checks the lock twice**
   (before and after the write lock). Neither is in the design; both only add a refusal.
6. **Cleanup unit reads `/etc/learnpilot/backup.env`.** It holds state paths only, matches
   `DZI_DB`, and avoids giving a cleanup the application's secret. A separate settings file would
   be the alternative.
7. **Early look at the cleanup service in the pre-flight** is an addition to the design's single
   look after the lock; the one that counts is still the second.
8. **Existing restore tests adapted** (`tests/test_lifecycle_restore_script.py`): the systemctl
   shim knows the cleanup unit, `calls()` leaves the `cleanup-*` calls out (`all_calls()` has
   them), and the pinned call lists, gate count, lock markers and timer-start counts were updated
   for the new steps. No test was weakened beyond that; each now also pins the new steps.
9. **Snapshot summary has a new key.** `src.backup_sqlite` prints `registrations_scrubbed: N`.
10. **Persistent=true** on the timer: a missed tick (downtime) runs once at start. Not in the design.

## Tests

Run with Python 3.12.15 in a scratch virtualenv outside the repository, built from
`requirements.txt` plus `python-docx` (Flask 3.1.3, Werkzeug 3.1.9, segno 1.6.6, PyOTP 2.10.0,
cryptography 50.0.2). Results are in the final report of the implementation session.

| Case | Test |
| --- | --- |
| DR1–DR3, DR5–DR7, DR9 | `GateTest`, `StatusAndReleaseTest` |
| DR4 (no automatic release, explicit flag) | `GateTest.test_dr4…`, `OperatorCommandsTest.test_dr4…` |
| Cleanup: normal, held, regressed clock, restore lock, race, busy database | `OperatorCommandsTest`; `test_registration_cli.py` |
| Release and the clock independently | `StatusAndReleaseTest`, `OperatorCommandsTest.test_release_reports…` |
| N6 (script order and timer symmetry) | `RestoreScriptOrderTest`, `CleanupCoordinationTest`, `InterruptedRestoreTest` |
| Timer active/inactive; cleanup running; interrupted and each finish path | `CleanupCoordinationTest`, `InterruptedRestoreTest` |
| Staged-copy invalidation, persistent hold, existing hold, no carry-over, accounts and canonical content kept | `RegistrationGateOnTheStagedCopyTest` |
| N7, N9 (order, hash, live untouched, failure publishes nothing) | `SnapshotScrubTest`; `test_backup_sqlite.py`, `test_application_backup.py` unchanged |
| Units against the real parser | `UnitFilesTest` |
| Documented residuals | `SnapshotScrubTest.test_the_documented_residuals…` |

**Simulated, not real:** the restore tests run the real script with shims for `sudo`, `systemctl`,
`sync` and `curl`; they show the script's control flow, not systemd's behaviour. The `sync` shim
only records the call: nothing shows that a note survives a power loss, and a SIGKILL in the
harness is a killed process, not a lost page cache.

## Not verified / remaining activation prerequisites

- Linux/systemd, unrun: `systemd-analyze verify` of both units; the timer firing every ten minutes
  on a quiet server and scrubbing an expired registration; `ConditionPathExists=` refusing a run
  under the lock; the full restore script on Linux with its gate, an interrupted run, a run started
  while a cleanup executes, and a second restore on top of a held one; a snapshot taken by the
  backup unit followed by a test restore; `ProtectSystem=strict` with the unit's `ReadWritePaths`;
  GNU `sync -- FILE DIR` on the production filesystem, and a timer note surviving a real power loss
  or crash; real `systemctl start` failures of a timer and the recovery by the next run.
- The health-check additions (DEPLOYMENT 19) were not executed anywhere.
- No claim of VPS validation is made.
- Still open from the design: D16 (privacy), D17 configuration, Nginx evidence, sender
  containment evidence, teacher quotas (PR 7), password recovery (PR 8, which must extend the gate,
  status and scrub), and an independent review of the timer fix described above.
