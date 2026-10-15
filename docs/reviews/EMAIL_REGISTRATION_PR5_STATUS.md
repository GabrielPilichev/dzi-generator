# Email registration, PR 5 — operations: status and contract

Design: `docs/reviews/EMAIL_REGISTRATION_DESIGN.md` (sections 3.8, 3.9, 6.5, 6.6, 9;
decisions D18 (b) and D20 (a)).
Tests: `docs/reviews/EMAIL_REGISTRATION_TEST_PLAN.md`, DR1–DR9, N5–N7, N9.
Earlier PRs: `EMAIL_REGISTRATION_PR1_STATUS.md` to `PR4_STATUS.md` (merged: #335–#338).
Operator guide: `docs/DEPLOYMENT.md`, section 34 (and sections 16, 17, 19).

**Status:** PR #339, branch `feature/registration-operations`. Committed: `36cffbe` (implementation),
`b719728` (fixes after the first independent review), `39157f5` (the timer fix after the second),
`f75ca8d` (ownership, guard and records after the third) and `cc4e2d5` (lifetime exclusion, protected
ownership and swap state after the fourth). The fifth independent review, of
`cc4e2d599f805791bcf320474252c68cc13bc105`, asked for changes: three recovery defects around an
unfinished replacement. Their fixes (below, "Recovery over an unfinished replacement") are **unstaged
and uncommitted** on top of `cc4e2d5`, **awaiting independent review, not approved**. Registration is
**not activated**: both build gates stay off. No migration, no schema change, no deployment, no
provider, no real database, no real message, no server. Nothing here has run under systemd or on Linux.

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

### The timer fix, committed as `39157f5`

Kept as it was reviewed. Points 1 (where the notes are) and 6 (the pre-flight's unlocked look at the
lock) are replaced by "Ownership, guard and records" below; the rest stands, now carried out under
the guard. Protocol, in `deploy/learnpilot-restore.sh` at `39157f5`:

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

### Third independent review, of `39157f5fa8f12f121333b3df6ff2d6c85ccc6b02`: REQUEST CHANGES

Two blockers, both in the timer recovery; the fixes of the first two reviews (schema scrub fails
closed, release-hold looks for the lock again under SQLite's write lock) were confirmed and are
unchanged.

| # | Finding |
| --- | --- |
| 1 | No exclusion between recovery and restore ownership. A recovery run could pass its unlocked "no restore lock" check, pause, then remove the note that another restore had written before stopping its timer; a SIGKILL of that restore left the timer inactive with no intent, and the next restore ended with exit 0 without it. The same race after a restore gave its lock back and before `restart_timers` had finished. |
| 2 | The notes were in the service-writable `$STATE`, and root followed a planted symbolic link when writing them: creation through a dangling link, truncation of an existing target (reproduced). |

Related, to inspect: the takeover's handling of `$LOCK/pid` (read and written by root through a
link) and the window between `mkdir` of the lock and the write of its pid. Non-blocking: no
repository regression for the migration of in-lock markers; the held-count message lost when the
restart needed the retry; manual recovery guidance that was an unlocked "check, then remove"; the
durability of a note's removal not described.

### Ownership, guard and records, committed as `f75ca8d`

Kept as it was reviewed. Its timer-record concurrency and record-path fixes were confirmed by the
fourth review and are unchanged; what it left open (item 9 below, and ownership that went by the
lock directory and by the holder's own process) is what the fourth review's blockers are about.
In `deploy/learnpilot-restore.sh` at `f75ca8d`; operator text in `docs/DEPLOYMENT.md`, sections 2,
17 and 34.

1. **Records outside the service's reach.** `/var/lib/learnpilot-restore` (`LEARNPILOT_RESTORE_RECORDS`;
   refused if relative, not plain — `//`, `.`, `..`, a space, a final `/` — or inside
   `LEARNPILOT_STATE`) holds `restore-owner` (the process id of the run that holds the lock),
   `pending-backup-timer` and `pending-registration-cleanup-timer`. Section 2 makes it root's, 0700;
   the pre-flight makes it (`mkdir -m 0700`, after the directories above it are checked) if it is
   missing.
2. **Checked, and nothing repaired.** Every program that touches it runs as root and starts with the
   same checks (`CHECKED`): every directory from `/` down is a directory and not a link, belongs to
   root or the effective user, and is writable by no one else unless sticky; the directory itself
   belongs to the effective user and has mode exactly 0700 (an SELinux `.` or macOS `@` is allowed,
   an ACL `+` is not); each record is absent or a regular file of that owner with one link. A link
   (dangling or not), a directory, a pipe or a second name of another file is refused. The
   pre-flight gate runs them before anything is stopped; every step under the guard runs them again
   before it uses a record. As only root can change anything on that path, and the script changes
   it only under the guard, what is checked is what is used. The 39157f5 location
   (`$STATE/restore-pending-*`) is never followed or trusted: if anything is there, the pre-flight
   stops, untouched, and points to section 34.
3. **One guard: `sudo flock "$RECORDS" sh -c PROGRAM`.** flock(1) takes an exclusive flock(2) lock on
   the directory and runs the program with the descriptor inherited by it and by everything it
   starts (`systemctl`, `sync`, `rm`, `ps`): the guard is held until the last of them has ended, and
   the kernel releases it when they die. No program in a step starts anything that outlives it
   (`systemctl` asks systemd, whose units do not inherit our descriptors, and `start`/`stop` wait for
   their job). A signal to the script, or a SIGKILL of it, does not cut a step short (its processes
   are not the script's process); a power loss or a kill of the whole process group can, between two
   of its commands. The guard is held only for these short steps, never while the script waits for
   the operator or runs a restore gate.
4. **The steps.** `ACQUIRE`: make the lock, or take over one whose recorded holder is not a running
   `learnpilot-restore` (else exit 3: "another restore is running"), write `restore-owner`, report
   the notes there. `PAUSE` (per timer, only for the holder of the lock): write the note, `sync` it
   and the directory, then stop the timer if it was active. `RESTART` (per timer): if any restore
   lock exists, exit 3 and touch nothing; otherwise start the timer and, only if that succeeded,
   remove the note and `sync` the directory (4: start failed, note kept; 5: started, note not
   removed). `RELEASE`: only for the recorded holder, remove the lock (all, or only if empty) and
   `restore-owner`. Nothing else in the script makes or removes the lock, a note or the holder
   record, or starts or stops a timer.
5. **What follows.** A note is removed only in a step that saw no lock and started its timer; a lock
   is made only in a step under the same guard; a timer is stopped by the restore only in a step of
   the lock's holder, after its note is on disk. So: a recovery paused anywhere in its step keeps
   any restore from taking the lock until it is done, and the restore then finds the timer active
   and notes and stops it; a run that gave its lock back and finds a new restore holding one leaves
   the timer and the note to it, warns (`a restore lock exists again`), and ends not 0; two runs
   that find an abandoned lock cannot both take it over, and none looks at a lock half-made (the
   `mkdir`-to-pid window is gone).
6. **Unchanged from `39157f5`, now under the guard:** a note is never dropped because a timer is
   found inactive; initially inactive timers are never started; both timers are always attempted;
   signal statuses are kept; no exit 0 while a noted timer is not active. In-lock markers of an
   earlier version (`backup-timer-was-active` from the released script, and
   `registration-cleanup-timer-was-active`) become notes on takeover. Diagnostics tell apart a timer
   that could not be started, one whose note could not be removed, one left to a restore that holds
   the lock again, and a step that did not run.
7. **Outcome kept on the retry path.** When the restart needed `finish`'s retry (or a signal came
   after the start), the run still says registration is held and, with `--accept-held N`,
   `RESTORED, NOT RECOVERED: N assignment(s) stay held`; it keeps its non-zero exit, and it never
   says `RESTORE COMPLETE`.
8. **Durability.** A note is written and flushed (file, then directory) before its timer is stopped.
   Its removal is flushed (directory) after the start; a power loss before that flush can bring the
   note back, and the next run then starts that timer once more: nothing changes if it is active,
   and it is started again if an operator had stopped it on purpose in between (the run says so).
9. **Related findings.** `$LOCK/pid` is no longer read or written: the holder is in `restore-owner`.
   A `pid` file, or a link, in a lock an earlier version left is ignored, and nothing is written
   through it (regression below). The `mkdir`-to-pid race is closed by item 4. **Not changed, and
   still open:** root still creates the `swap-started` mark inside the lock with `: >`. The lock sits
   in `$STATE/data`, which the service can write, so a service user who replaces the lock directory
   could plant a link there and have root truncate its target when a replacement begins. This is
   not part of the timer protocol, and fixing it means changing how the lock directory is made and
   trusted, so it is left for a follow-up. The same directory lets the service user rename or remove
   the lock itself, as it always could. Legacy markers are still read with `test -e`, which follows
   a link but only tests existence.
10. **Operator migration.** None from a released version (none kept notes outside the lock). Do not
   deploy this script while an older version's restore is running: it ignores `$LOCK/pid` and would
   take that lock over. Notes of 39157f5 (never merged) in `$STATE/restore-pending-*` stop the
   pre-flight until an operator handles them (section 34). Manual recovery is now one command that
   takes the guard, refuses while a restore lock exists, starts the timer and only then removes the
   note (section 34). After a rollback, bring back any timer noted in `/var/lib/learnpilot-restore`:
   an older script does not read it.

Regressions, in `tests/test_registration_restore_operations.py` unless marked otherwise. Concurrent
runs: two scripts on one server at the same time, each with its own call log, paused where the shims
are told (`ConcurrentRestoreTest`):

- A pre-flight recovery paused before, or just after, its start while another restore takes the
  lock, notes and stops that timer, and is then SIGKILLed. Both timers, the other one active or
  inactive at the start: 8 cases. The restore's note is kept, and a third run takes the lock over
  and brings back exactly what was active.
- A restore paused after it gave its lock back and before its timers are restarted, while a new
  restore recovers, takes the lock, notes, stops and is SIGKILLed: the first ends 1 with the
  warning, starts nothing, keeps the notes and still reports what it restored. Both timers × other
  active or inactive: 4 cases, then takeover and recovery.
- Two runs that find an abandoned lock, one paused in the middle of the takeover: the other is
  refused with "another restore is running (process <first>)", untouched; the first completes.
- A kill of the whole run (`KILLALL`) inside a step under the guard (holding the guard before the
  lock is made, after a note's flush, after a stop, after each start): nothing lost, and the next
  run is not kept waiting by the guard.

Records (`RecordsStorageTest`): a dangling link, a link to a sentinel file, a directory, a named pipe
and a second name of a sentinel, at each of the three record paths (15 cases): refused before
anything is stopped, the sentinel byte-identical, nothing created through the link. The records
directory as a link, a file, modes 0750, 0705 and 0770, root's (`/usr`, run as a non-root user), a
directory above it writable by the group or by anyone, a link above it: refused before anything is
stopped. A sticky directory above it is accepted and a missing directory made 0700. A relative,
non-plain or in-state path: exit 2 before anything runs. In-lock markers of an earlier version become
notes, and only their timers come back (backup, cleanup, both). A note at the 39157f5 location, file or
link, is not followed or acted on. A note that cannot be removed is told apart from a timer that
cannot be started, and recovered by the next run. A restore that needed the retry still says
`RESTORED, NOT RECOVERED: 2 assignment(s)`. In `tests/test_lifecycle_restore_script.py`,
`InterruptedRunTest`: the "another restore is running" refusal reads the holder from `restore-owner`,
and a `pid` link in an abandoned lock that names a running process is neither trusted nor written
through.

Mutation evidence, on disposable copies of the tree outside the repository, running
`ConcurrentRestoreTest`, `RecordsStorageTest`, `InterruptedRestoreTest`, `BackupOverlapTest` and the
two `InterruptedRunTest` cases above (37 tests, with subtests; failing cases counted). The fixed tree
passes. The script of `39157f5`: 79 failing cases (many only because the notes are elsewhere; the
concurrent cases fail on the state they find). Weakening one thing at a time:

| Copy | Failing cases | Which |
| --- | --- | --- |
| No guard (`flock` removed from all 8 steps) | 12 | all 8 paused-recovery cases, the two takeovers; 3 others only by the text or call numbering they pin. The 4 "gave its lock back" cases pass: that race is closed by the lock check in `RESTART` (next row), not by the guard alone. |
| `RESTART` ignores the lock | 8 | all 4 "gave its lock back" cases, and 4 takeover cases of `InterruptedRestoreTest`/`BackupOverlapTest` |
| `ACQUIRE` ignores a running holder | 2 | the two takeovers; "another restore is running" |
| Records not checked for their type | 15 | all 15 record cases |
| Only the link check of a record removed | 6 | the 6 link cases |
| The directory checks removed (itself and above) | 9 | all 9 directory cases |
| Only the directories above not checked | 3 | group- and world-writable above, link above |
| A removal failure reported as a start failure | 2 | both timers |
| Records allowed inside the state directory | 2 | both in-state paths |
| The note removed after a failed start | 28 | across the restart, concurrency and recovery tests |

### Fourth independent review, of `f75ca8d2fab4b347ef633ee55d6868c048ecc957`: REQUEST CHANGES

The timer-record concurrency and the record-path symlink findings of the third review were confirmed
as resolved, and so were the schema-scrub and hold-release fixes. Four P1 blockers:

| # | Finding |
| --- | --- |
| 1 | `swap-started` was still written by root through the lock directory, which the service can replace (privileged truncation through a planted link); and with the mark removed or suppressed, recovery released the lock and restarted the timers over a half-replaced state. |
| 2 | A takeover went by the liveness of the holder's own process. A command it had started could survive it: A's final rename paused, A killed, B took over and finished, A's rename then put A's copy over B's live database, losing a write made after B. |
| 3 | Taking the restore went by whether the lock directory was there. Renamed or removed under a running restore, the next run made a fresh one and wrote itself over the recorded holder without consulting it. |
| 4 | Both legacy timer markers could be lost: a timer-state query that failed before the notes were written, recovery starts that also failed, and the cleanup that removed the legacy lock with the markers in it. |

### Lifetime exclusion, protected ownership and swap state, committed as `cc4e2d5`

In `deploy/learnpilot-restore.sh`; operator text in `docs/DEPLOYMENT.md`, sections 17 and 34.

**Where authority is.** In `/var/lib/learnpilot-restore` (root's, 0700, checked as in `f75ca8d`):
`restore-owner` (the process id of the run that holds the restore), `swap-state` (a replacement of
the live state has begun), `lifetime` (the lifetime lock's file) and the two timer notes. The lock
directory `$STATE/data/restore-in-progress` stays what the application and the units look at, and
nothing else: the script never reads it as evidence of ownership or of a replacement, and never writes
into it (it makes and removes the directory itself; `rm -rf` on a planted link removes the link).

**Two locks, one order.** (1) The guard: `flock` on the records directory, for the short steps that
take or give back the restore, write or remove a record, or stop or start a timer. (2) The lifetime
lock: `flock` on `lifetime`. Only `ACQUIRE` holds both, and it takes the second with `-n`, so it never
waits for it; no process holding the lifetime lock ever asks for the guard. No cycle, so no deadlock.

1. **Lifetime exclusion, inherited by every mutating descendant (blocker 2).** Once a run holds the
   restore, every root or service-user command it starts runs as
   `sudo flock -s lifetime sh -c OWNED OWNER_RECORD PID COMMAND...` (`AS_OWNER`): `OWNED` checks under
   the shared lock that `restore-owner` still names this run, then `exec`s the command. The process
   doing the work therefore holds the lock's open file description, and so does everything it starts,
   until the last of them has ended or died; util-linux `flock` also keeps it in its own process while
   it waits for the command. Service-user commands go through `setpriv --reuid --regid --init-groups
   --reset-env --` rather than `sudo -u`: sudo closes descriptors from 3 up before it runs a command
   (`closefrom`), setpriv keeps them, so a Python gate, `sqlite3` or `tar` holds the lock itself. This
   covers every command after `ACQUIRE`: the stop and start of the service, both checkpoints, the
   copies and `install`s, the extraction, migrations, gate, hold, reconcile, clock, the fingerprints,
   the moves, the second name, the final rename, `rmdir`, and in `finish` the moves back, the removal
   of the copy and the stop. The steps under the guard are serialized by the guard instead, and
   `systemctl show` only reads. A test pins that no other `sudo` follows `AS_OWNER=`.
2. **Takeover (blocker 2, 3).** `ACQUIRE`, under the guard, in this order: the recorded holder is read
   whatever the lock directory looks like (a running `learnpilot-restore` → refused, 3; content that is
   not a process id → refused as inconsistent, 5); then the lifetime lock is taken exclusively with
   `-n` and kept until the step ends (any command of an earlier run still alive → refused, 4, "a command
   an earlier restore started is still running"); then a lock path that is a link or not a directory is
   refused. A command of the earlier run that starts after this step finds the new holder and is not
   carried out. A command stuck for good keeps every later run out: refusing is the conservative
   choice, and the operator ends it (section 17: `fuser -v lifetime`, `kill`, check again; a reboot
   for one in state `D`).
3. **Swap state (blocker 1).** Before the first move, a guarded step (`SWAP begin`, holder only) writes
   `swap-state` and flushes it, file and directory. It goes only (a) with a restore that has finished,
   when the service runs (`RELEASE owner`), or (b) when this run took its own replacement back
   completely (`SWAP clear`; never a record of an earlier run). `RELEASE all`, the way back for a run
   that replaced nothing, refuses while it exists. `ACQUIRE` reports it ("swap:begun") and that alone
   makes the run treat the earlier one as interrupted while replacing: the lock stays on failure, no
   timer is started. Removing, emptying or replacing the lock directory changes none of that.
4. **Holding the restore until the service runs.** At the end the lock directory goes first (`RELEASE
   lock`: the service refuses to start under it), the run still holds the restore through the start and
   the health check, and only then are its records given back (`RELEASE owner`), and the timers
   started. A failed start is stopped again by the holder, which then gives the records back.
5. **Legacy intents migrated before anything can fail (blocker 4).** In `ACQUIRE`, after the
   holder and lifetime checks and before the new holder is written: each legacy mark in the lock
   directory (`backup-timer-was-active`, `registration-cleanup-timer-was-active`, `swap-started`) is
   written as a note or as `swap-state`, and flushed. If any of it fails, the step ends without
   recording a holder: the lock directory and its markers are as they were, the run stops untouched
   (`the restore could not be taken ... as they were`). From then on the intents are in the records,
   so a failure anywhere later (a timer state systemd cannot report, starts that fail) leaves the notes,
   and the next run brings the timers back. The script no longer reads the markers itself.
6. **Recovery of a timer goes by the records.** `RESTART` starts a timer and removes its note only if
   there is no `restore-owner` (and no lock directory either): a lock directory removed under a running
   restore does not let a recovery start that restore's timers.
7. **Unchanged:** the guard and the record checks of `f75ca8d`; a note never dropped because a timer
   is found inactive; inactive timers never started; both timers attempted; signal statuses kept; no
   exit 0 while a noted timer is not active; the schema-scrub and hold-release fixes.

**Not changed, and stated.** An earlier version's restore that is still running is not seen: its holder
is not recorded and its commands do not hold the lifetime lock. Deploying this script while one runs
stays prohibited (section 34); compatibility is not implemented. A rollback is done only with no
restore held or left unfinished (section 34). `ACQUIRE` reads the legacy marks in the lock directory
with `test -e` (existence only; it follows a link, writes nothing through it). Taking the restore when
a holder's process id was reused by another `learnpilot-restore` refuses, conservatively.

Regressions, in `tests/test_registration_restore_operations.py`:

- `SurvivingWriterTest`: A paused inside a command that changes the live state (the final rename, as
  root; the checkpoint of the live database, as the service user through `setpriv`), A's own process
  killed, the lock directory kept, removed or replaced: B is refused ("a command an earlier restore
  started is still running") and reaches no stop, copy, move, timer or new holder; the live database is
  unchanged. Then A's command finishes, or is killed with A's process group; only after every process
  of A has ended does the next run take over and finish, and a write made after it is still there (6
  cases). A command of A that was started but had not yet taken the lifetime lock when A died: B
  takes over and finishes, then the command takes the lock, finds A no longer the holder, and does
  nothing (the final rename never runs; a write made after B stays). A source test pins that every root
  or service-user command after `AS_OWNER=` goes through it.
- `LockDirectoryIsNoAuthorityTest`: A alive at its prompt for the time, the lock directory removed,
  replaced by a directory, or by a link to a sentinel directory: B is refused ("another restore is
  running (process A)"), starts no timer, keeps A's notes, holder and timers as they are; A then
  finishes, and the sentinel is untouched (3 cases). A holder record that is not a process id is
  refused as inconsistent: nothing touched, every record byte-identical.
- `SwapStateTest`: links planted at `swap-started`, `pid` and a timer marker in the lock directory of a
  running restore: nothing is written through them, the sentinel is byte-identical (3 cases). A run
  killed half-way through the replacement, the lock directory kept, removed or emptied: the next run,
  failing before it replaces anything, keeps the lock, the service and the timers stopped and the record
  (3 cases); a finished run ends it. The record goes when a run took its own replacement back, and stays
  when a final check fails after the database was replaced.
- `LegacyIntentMigrationTest`: a lock of an earlier version with both markers and both timers inactive:
  a timer state systemd cannot report, then both starts failing: both notes kept, and a clean run brings
  both back; the migration failing at its first or second flush, or not running at all: the lock and its
  markers as they were, no holder recorded, then clean recovery (3 cases); a takeover killed after the
  migration, then clean recovery. A lock of an earlier version with `swap-started`, from a run killed
  half-way through the replacement: the next run, failing, keeps the lock and writes `swap-state`; with
  the lock directory then removed, the run after it still keeps everything stopped; a finished run ends
  it.
- Adapted from `f75ca8d`: the "gave its lock back" concurrency case now pauses the first run after its
  records are given back (the lock directory alone no longer marks a restore as over); the
  interrupted-run tests look for `swap-state` in the records.

The `sudo` shim now closes descriptors from 3 up before it runs a command, as sudo does; it does not
stay behind as the command's parent, as sudo does. The `setpriv` shim only drops its options.

Mutation evidence, on disposable copies of the tree outside the repository, running
`SurvivingWriterTest`, `LockDirectoryIsNoAuthorityTest`, `SwapStateTest`, `LegacyIntentMigrationTest`,
`ConcurrentRestoreTest`, `InterruptedRestoreTest` and `test_lifecycle_restore_script.InterruptedRunTest`
(38 tests, with subtests; failing cases counted). The fixed tree passes. The script of `f75ca8d`: 31
failing cases, across every one of the four blockers' tests. Weakening one thing at a time:

| Copy | Failing cases | Which |
| --- | --- | --- |
| Taking the restore does not ask for the lifetime lock | 6 | all 6 surviving-command cases |
| Commands not under the lifetime lock (holder check kept) | 7 | all 6 surviving-command cases, and the source test |
| Service-user commands through `sudo -u` (descriptor closed) | 3 | the 2 checkpoint cases (the service user's command), and the source test |
| No holder check in each command | 0, then 1 | survived the first set: added `test_a_command_of_a_killed_restore_that_takes_the_lifetime_lock_after_a_takeover_is_not_carried_out`, which rejects it (and `f75ca8d`) |
| Holder consulted only when the lock directory is there | 3 | the lock directory removed under a live A, and the 2 surviving-command cases with it removed |
| Recovery of a timer by the lock directory only | 3 | the same three |
| Replacement not recorded as begun | 13 | the 3 partial-replacement cases, when the record goes, and 9 interrupted-run cases |
| An earlier version's `swap-started` not carried over | 0, then 1 | survived the first set: added `test_a_replacement_an_earlier_version_had_begun_is_carried_into_the_records_before_anything_else`, which rejects it (and `f75ca8d`) |
| Legacy timer markers not migrated | 5 | the timer-state failure, the 3 migration-failure cases, the takeover and recovery |
| New holder recorded before the migration | 2 | the two failed flushes (no holder must be recorded) |

The two added tests were run afterwards on the fixed tree, on `f75ca8d` and on the mutant each was added
for; each passes on the first and fails on the other two.

### Fifth independent review, of `cc4e2d599f805791bcf320474252c68cc13bc105`: REQUEST CHANGES

Independently confirmed as resolved: the surviving-descendant exclusion (lifetime lock), protected
ownership, the record-path security, the schema-scrub rejection and the hold-release fixes. Three
recovery defects remained, all around a replacement of the live state that had not finished:

| # | Finding |
| --- | --- |
| 1 | `RELEASE empty` removed an empty lock directory over an unfinished replacement. A killed before its second move (live uploads set aside, the copy's not in place); B, taking it over, signalled (SIGTERM) during the guarded `ps` look at A: the step still recorded B, and B's failure handling removed the empty lock although `swap-state` was there and the live uploads were missing. The application could then start. |
| 2 | `RESTART` did not look at `swap-state`. The flush of a migrated `swap-started` failed after both timer notes were written: `swap-state` without `restore-owner`. With the service-writable lock directory removed, the pre-flight of a later run (which then failed) started both timers and removed their notes over the half-replaced state. The documented manual recovery had the same gap. |
| 3 | A retried migration did not repeat a failed flush. The second migrated note's flush failed; the retry found the note there, skipped the flush, recorded its holder with no `sync` at all, then failed (timer state not readable, starts failing) and its failure handling removed the lock with the legacy markers in it. |

### Recovery over an unfinished replacement (unstaged, awaiting independent review)

The invariant, checked against every branch of `RELEASE` and `RESTART`: **while `swap-state` records a
replacement that has not finished, the lock directory is not removed and no timer is started, whoever is
recorded as the holder and whatever is left of the lock directory.** In `deploy/learnpilot-restore.sh`;
operator text in `docs/DEPLOYMENT.md`, section 34. The ownership protocol, the guard, the lifetime lock
and the record checks are unchanged.

1. **`RELEASE empty` (finding 1).** After the holder check, and before `rmdir`: if `swap-state` exists,
   refuse (`a replacement of the live state is recorded as begun (...): the restore lock stays`). The
   lock directory and the records stay; `finish` says `A restore lock is left that this run does not give
   back`, and the next run takes the restore over. A run that is not the recorded holder (taking it
   failed, or no holder is recorded) was already refused by the holder check and still is.
2. **`RESTART` (finding 2).** It now takes `SWAP_RECORD` (arguments: `RECORDS LOCK OWNER_RECORD
   SWAP_RECORD NOTE TIMER noted|flagged`), checks it as a record (`record`: a link, a directory, a pipe
   or a second name is refused, status 1), and returns 3 while it exists, independently of
   `restore-owner` and of the lock directory. 3 keeps the note; in a pre-flight it starts nothing, at the
   end of a run it is reported (`WARNING ... a restore is held, or was left unfinished`) and the run does
   not exit 0. The documented manual recovery (section 34) refuses as well when anything stands at
   `restore-owner`, `swap-state` or the lock path (`-e` or `-L`, so a link, dangling or not, or a
   directory fails closed), and the guide says that an orphaned `swap-state` is ended by running the
   script, never by hand.
3. **Migration flushes (finding 3).** In `ACQUIRE`, the legacy marks are written into the records as
   before (only if missing). Then **every** note and `swap-state` that exists is flushed, file and
   directory (`sync -- RECORD RECORDS`), whether this attempt wrote it, an earlier failed attempt wrote
   it, or it was there for another reason; only then is the holder written. A failed `sync` ends the step
   with status 1 before the holder is recorded: the legacy markers stay in the lock directory (nothing
   can remove them without being the holder) and the run stops untouched (`... as they were`). The cost
   is one more `sync` per existing record when a restore is taken over.
4. **The other branches, reviewed and unchanged.** `RELEASE all` already refuses while `swap-state`
   exists. `RELEASE lock` and `RELEASE owner` run only for the holder, after its own `SWAP begin`
   rewrote `swap-state` and after every gate that follows the final rename (`lock`), or once the service
   runs on the restored state (`owner`); the replacement they end is the one this run finished.
   `SWAP clear` is called only when no earlier run's record was found (`INTERRUPTED_BEFORE=no`), after
   this run moved everything back. `restart_timers` is called in the pre-flight, after `RELEASE all`,
   after `RELEASE owner`, and after the start whether or not `RELEASE owner` succeeded; every one of
   those calls now meets the `swap-state` check in `RESTART` itself.

Regressions, in `tests/test_registration_restore_operations.py` (each deterministic: the signal is sent
while the run is paused under the guard, failures are injected at a numbered call; no lock is tampered
with in the first):

- `SwapStateTest.test_a_takeover_cut_short_while_it_looks_at_the_holder_leaves_a_partial_replacement_locked`:
  A killed at its second move; B paused at `ps#1` inside `ACQUIRE`, sent SIGTERM, SIGINT or SIGHUP,
  released: B ends with the signal's status, is the recorded holder, and `swap-state`, the lock
  directory, the missing live uploads and the old vault are all still there; `web.production` refuses to
  start; no timer or service is started or stopped by B. A finished run then ends it (3 cases).
- `LegacyIntentMigrationTest.test_a_replacement_record_left_without_a_holder_keeps_every_noted_timer_stopped`:
  a legacy lock from a run killed at its second move, with `swap-started` and the markers of the backup
  timer, the cleanup timer or both; the flush of `swap-state` fails (after each note's); the lock
  directory is removed; the next run fails its pre-flight: no timer start, every note and `swap-state`
  kept, both timers inactive, the live uploads still missing. A finished run then brings back exactly
  the noted timers (3 cases).
- `LegacyIntentMigrationTest.test_a_migration_retried_after_a_failed_flush_flushes_every_record_again_before_it_changes_hands`:
  a legacy lock; the first, the second or (with `swap-started`) the third flush fails; the retry has a
  timer state systemd cannot report and both starts failing. Hooks note, just before each `sync`, whether
  a holder is recorded and what is in the lock directory; the `sync` shim now logs what each call that
  was carried out flushed (`flushed.log`). Each note (and `swap-state`) is flushed with the records
  directory while no holder is recorded and every legacy marker is still in the lock; then the holder is
  recorded (`An earlier restore did not finish`), the notes outlive the failure handling, and a clean run
  brings both timers back (3 cases). The shim records the order of the syncs, not that anything reaches
  a disk.
- `DocumentedTimerRecoveryTest`: the command of section 34, extracted from `docs/DEPLOYMENT.md` as it
  stands, paths substituted, run under the shims: it starts a noted timer and removes its note when
  nothing is there, and refuses (no start, note kept) with `swap-state` as a file, a dangling link or a
  directory, with `restore-owner`, and with the lock directory (6 cases).
- Source pins updated for the new `RESTART` arguments (`RestoreScriptOrderTest`,
  `test_lifecycle_restore_script.RestoreScriptSourceTest`); no assertion was dropped.

Mutation evidence, on disposable copies of the tree outside the repository (`rsync` without `.git`),
running the four new tests (15 cases) and the two updated source pins (`RestoreScriptOrderTest`,
`RestoreScriptSourceTest`); failing cases counted. The fixed tree passes. The tree with the script and
`docs/DEPLOYMENT.md` of `cc4e2d5` (tests and harness of this change): 14 failing, every case of the four
new tests that concerns a finding (3 takeover, 3 orphaned record, 3 retried migration, the 3 `swap-state`
cases of the documented command) and both source pins; the 3 other cases of the documented command
(nothing there, `restore-owner`, the lock) pass there as well, as they should. Weakening one thing at a
time:

| Copy | Failing cases | Which |
| --- | --- | --- |
| `RELEASE empty` without the `swap-state` check | 3 | the 3 takeover cases (TERM, INT, HUP) |
| `RESTART` without the `swap-state` check (record check kept) | 3 | the 3 orphaned-record cases |
| The documented command without the `swap-state` lines | 3 | its 3 `swap-state` cases |
| The documented command with `-e swap-state` only | 1 | the dangling link |
| `ACQUIRE` flushing only what this attempt wrote (as `cc4e2d5`) | 3 | the 3 retried-migration cases |
| `ACQUIRE` flushing the notes again, not `swap-state` | 4 | the retried migration with `swap-started`; the 3 orphaned-record cases (their first run no longer fails at the `swap-state` flush, and takes the restore) |
| `ACQUIRE` flushing after the holder is recorded | 6 | the 3 retried-migration cases; the 3 orphaned-record cases (`restore-owner` left behind by the failed step) |

Every copy passes `bash -n`. The test of the third finding first errored instead of failing on
`cc4e2d5` (no `sync` at all, so no hook log) and matched a flush of the first attempt; both were
corrected (an absent log reads as no syncs; the retry runs under its own tag) before the counts above.

## What PR 5 contains

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
  checkpointed and fingerprinted: the cleanup timer is stopped (if it was active; noted first, in
  the same step under the guard, in `/var/lib/learnpilot-restore/pending-registration-cleanup-timer`,
  flushed to disk) and the cleanup service must be `inactive` or `failed`. A first look in the pre-flight avoids a wasted start. Any other state, or a state
  systemd cannot give, ends the restore (`activating`, `active`, `reloading`, `deactivating`:
  "a registration cleanup is running").
- The timer is started again wherever the backup timer is: after a failure that gave the lock back,
  after "restore complete, service not running", after a successful start, and after a run stopped
  or failed once the service was started. A timer that was not active is never started. Its note
  goes only once the start succeeded, and only while no restore is held or left unfinished (no
  `restore-owner`, no `swap-state`, no lock directory); otherwise the run does not exit 0 and the
  next run starts the timer first (none of them there) or at its end (restore taken over and
  finished). Protocol: "The timer fix", "Ownership, guard and records", "Lifetime exclusion, protected
  ownership and swap state" and "Recovery over an unfinished replacement" above. Who holds the restore and whether a replacement has begun are in
  root's records, never in the lock directory; every command after the restore is taken holds the
  lifetime lock.
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
| Restore ownership and the guard: overlapping recovery and restore, a run that gave its lock back, two takeovers, a kill inside a guarded step | `ConcurrentRestoreTest` |
| The restore's records: place, path checks, in-lock markers, the 39157f5 location, removal failure, outcome on the retry path | `RecordsStorageTest`; `test_lifecycle_restore_script.InterruptedRunTest` (holder, `pid` link) |
| Lifetime exclusion: a surviving command of a killed restore; every command after the restore is taken | `SurvivingWriterTest` |
| The lock directory removed or replaced under a running restore | `LockDirectoryIsNoAuthorityTest` |
| Swap state in the records: planted links, an interrupted partial replacement, when the record goes, a takeover cut short over a partial replacement | `SwapStateTest` |
| Legacy timer and swap markers migrated before anything can fail; an orphaned `swap-state` keeps the timers stopped; a retried migration flushes again before the holder changes | `LegacyIntentMigrationTest` |
| The documented manual timer recovery, run as it stands in the guide | `DocumentedTimerRecoveryTest` |
| Staged-copy invalidation, persistent hold, existing hold, no carry-over, accounts and canonical content kept | `RegistrationGateOnTheStagedCopyTest` |
| N7, N9 (order, hash, live untouched, failure publishes nothing) | `SnapshotScrubTest`; `test_backup_sqlite.py`, `test_application_backup.py` unchanged |
| Units against the real parser | `UnitFilesTest` |
| Documented residuals | `SnapshotScrubTest.test_the_documented_residuals…` |

**Simulated, not real:** the restore tests run the real script with shims for `sudo`, `systemctl`,
`sync` and `curl`; they show the script's control flow, not systemd's behaviour. The `sync` shim
only records the call: nothing shows that a note survives a power loss, and a SIGKILL in the
harness (also `KILLALL`, of the whole process group) is killed processes, not a lost page cache.
`sudo` runs as the test user, so "root" in the records checks is the effective user there, and
`/usr` stands for a directory of someone else. The `sudo` shim closes descriptors from 3 up before it
runs a command, as sudo's `closefrom` does, but does not stay behind as the command's parent; the
`setpriv` shim drops its options and runs the command, so it shows that the script asks setpriv to
keep the descriptor, not that util-linux setpriv does (it does not close descriptors; unverified
here).

**The guard, and what is real about it.** macOS has no util-linux `flock(1)`. The `flock` shim uses
it where it is installed (`/usr/bin/flock` or `/bin/flock`, i.e. on Linux) and otherwise runs
`FLOCK_PY` (`tests/test_lifecycle_restore_script.py`): `flock(2)` with `LOCK_EX` on the directory,
the descriptor handed on to the command by `exec`. So the exclusion and its release on death that
the concurrent tests show are the kernel's, on macOS, in this session. They do not show the
behaviour of util-linux `flock(1)` itself (opening a directory, passing the descriptor to the
command, its exit statuses), nor Linux's `flock(2)`; those need a Linux run. The `ps` shim only
logs and pauses before the real `ps`; the `rm` shim fails only for the one path a test names.

## Not verified / remaining activation prerequisites

- Linux/systemd, unrun: `systemd-analyze verify` of both units; the timer firing every ten minutes
  on a quiet server and scrubbing an expired registration; `ConditionPathExists=` refusing a run
  under the lock; the full restore script on Linux with its gate, an interrupted run, a run started
  while a cleanup executes, and a second restore on top of a held one; a snapshot taken by the
  backup unit followed by a test restore; `ProtectSystem=strict` with the unit's `ReadWritePaths`;
  GNU `sync -- FILE DIR` on the production filesystem, and a timer note surviving a real power loss
  or crash; real `systemctl start` failures of a timer and the recovery by the next run;
  util-linux `flock(1)` on `/var/lib/learnpilot-restore` (exclusion between two real runs, release
  on a crash, descriptor inheritance by `sh` and `systemctl`); the records checks run as root on
  Ubuntu (GNU `ls -ldn` output, an SELinux `.`, `/var/lib` and `/var` as root's 0755); the
  descriptor inheritance as Ubuntu really does it: util-linux `flock -s FILE COMMAND` keeping the lock
  in its child and in itself, `setpriv --reset-env` keeping descriptors and resetting the environment
  as `sudo -u` did, sudo with `use_pty` (the command in a pty session of its own) and what becomes of a
  command when the script's process group is killed; `fuser -v` on the lifetime file as the operator
  guide uses it.
- The health-check additions (DEPLOYMENT 19) were not executed anywhere.
- No claim of VPS validation is made.
- Still open from the design: D16 (privacy), D17 configuration, Nginx evidence, sender
  containment evidence, teacher quotas (PR 7), password recovery (PR 8, which must extend the gate,
  status and scrub), and an independent review of "Recovery over an unfinished replacement" above.
- Power loss: the regressions of finding 3 show the order of the `sync` calls (the shim logs them),
  not that a record survives a lost page cache; GNU `sync -- FILE DIR` on the production filesystem is
  still unrun. The signal in the finding 1 regression is delivered by the test while the step is paused;
  bash's deferral of the trap until the command substitution ends was observed with macOS
  `/bin/bash` 3.2.57, not with Ubuntu's bash 5.
