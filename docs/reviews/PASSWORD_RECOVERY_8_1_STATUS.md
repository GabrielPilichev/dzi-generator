# Password recovery 8.1 — implementation and evidence

Gabriel authorized **scope 8.1 only** and chose Codex for its initial
implementation (confirmed session history); later work on it, including the
post-review cleanup below, uses Claude Opus / High. This is an implementation
report, **not approval**. Scopes 8.2/8.3, deployment, activation and
provider/host operations remain unauthorized. Existing production gates remain
OFF. The recovery service/gate itself belongs to later scopes and is absent. No
review requirement is waived.

## Provenance and boundaries

*Later provenance (recorded with scope 8.2, outside this report's evidence):* the
post-review cleanup below was merged with scope 8.1 as `09fdb68` (PR #343). That
same-file hardening followed the independent review of `ba4b9cd` and passed
focused tests; no repeat whole-suite audit was run for it. Scope 8.2 is now
authorized; see `PASSWORD_RECOVERY_8_2_STATUS.md`.

- Worktree: `/Users/gabrielpilichev/dzi-generator-recovery-operations`.
- Branch: `feature/recovery-storage-operations`.
- Implementation started from `755058fa1a4fbd42a14acecaf750a48b676b99c8` (an
  empty preparation commit on `cf5da98`) with a clean status, and was committed
  as `ba4b9cd7e293ec2427904451cd0221d3f1a5ccc8` (PR #343). The implementation
  evidence below was produced on the working tree **before** that commit.
- Migration: `025_password_recoveries.sql`, next available after 024.
- Independent review of exactly `ba4b9cd`: **APPROVE WITH NON-BLOCKING NOTES**
  (see *Independent review and post-review cleanup*). The cleanup after it is
  uncommitted, for Gabriel; no GitHub posts, pushes or merges were made.
- No real runtime database, credentials, vault content, provider, running service,
  tunnel, firewall, Nginx, deployment or activation was used/changed. All database
  work was on temporary migration/canonical builds or synthetic historical fixtures.
- Restore script and Nginx template remain byte-identical to the starting HEAD;
  static tests pin the gate/status positions and single service start.

## Contract and compatibility

The additive migration introduces the approved recovery table, hash/identity/final
state invariants, the nullable ledger purpose and two explicit indexes. It preserves
existing rows and old explicit-column inserts, including purpose-less ledger rows.
Integer affinity deliberately accepts convertible numeric text; invalid, fractional,
nonpositive and simultaneous-purpose ids fail. Runtime classification and manifest
schema/exclusions advance together; canonical JSONL bytes are unchanged.

`recovery_scrub` is standard-library-only. Its five scrubbed columns are email,
user_id, session_key_digest, code_hash and code_expires_at. Known columns are an
exact allow-list, including generated/hidden columns via table_xinfo. Missing
recorded storage or any missing/extra column fails closed; genuine historical
absence is accepted. It never creates a recovery row. Finalization and NULLs share
one statement. Cleanup/gate/status use the existing CLI; cleanup shares the writer
transaction, clock and high-water policy with registration. Existing restore-lock
checks, holds and marks are preserved. Restore gate intentionally retains its
existing clock policy: it voids credentials without accepting/lowering the mark.

Backup uses the existing online API, scrubs only the isolated destination before
validation/fsync/publication/hash, reports recoveries_scrubbed and publishes nothing
after a scrub failure. Registration settlement refuses recovery-purpose rows
before mutation and in its conditional UPDATE; legacy registration settlement is
preserved on the migrated schema. The current settlement service requires
migration 025; historical-schema support is for scrub/inspection and the offline
comparison, not for running the current application without its migrations.
No account/TOTP APIs, production writers, routes, templates or senders
were added. Public registration wrappers and recovery_candidate remain 8.2 work.

The comparison is isolated in `src/restore_credential_diff.py` behind a separate
manage_accounts dispatch **before open_database**, outside COMMANDS and
READ_ONLY_COMMANDS. Live --db is stat'ed only. Prepared offline files require a
complete rollback-mode header, no sidecars, no symlink/live identity, quick_check,
supported historical columns, and stable identity/metadata/hash brackets.
Supported absence for credential comparison additionally requires migration
bookkeeping (unlike recovery scrub's explicitly allowed pre-bookkeeping absence).
It reads immutable connections and performs no repairs. Complete ordered classes
map to P/N/K/none; state and second-factor findings are independent. It never
prints credential values. Unavailable replaced evidence gives uncertainty for all
snapshot accounts; unavailable snapshot evidence produces no classes and requires
operator treatment of every live account as uncertain.

## Caller and operator obligations

- Scrub writers require an open transaction and never commit. Cleanup's caller
  must acquire the writer lock, read the clock there, run registration.cleanup
  with that same time and refuse clock_behind before recovery cleanup. CLI does
  this and rolls back all writes on failure. Standalone scrub functions are not
  a new issuance or clock-admission API.
- Future recovery columns must update the allow-list and scrub together. A build
  serving migration 025 must retain all cleanup/restore/backup integration.
- Logical retention only: NULL/deletion says nothing about free pages, unused
  page space, WAL, earlier snapshots or copies below the application. Published
  backups are never rewritten; no physical-remnant bound is claimed.
- Offline-input stability depends on verified quiescence, protected copies and
  preparation, not equal before/after hashes alone. Changes made and undone
  between checks are not excluded by the checks.
- Deployment section 37 requires verified maintenance isolation before restore
  through repair/validation. No exposed-window fallback. Failed prerequisites
  postpone restore; failed isolation after service start postpones reopening and
  requires repeated repair/validation. The direct loopback health check remains.
- Repair uses the snapshot's password presence. P requires replacement/disable;
  N sign-out/disable without granting a password; K sign-out. Findings add state
  repair/TOTP reset. Validate commands and pre/post snapshots against a reference
  time captured after activity stopped. Hold release repairs accounting only.

## Approved-case mapping

| Cases | Evidence |
| --- | --- |
| RA1–4 | test_recovery_migration_sql: upgrade/fresh parity, runner idempotence, old-row preservation, constraints/affinity/indexes, FK check, runtime classification; canonical suites pin exclusion and unchanged JSONL. |
| RA5–6, RS4 | Existing registration service/ledger/settlement/CLI suites unchanged in behavior; recovery_operations tests guard all settlement outcomes, purpose-less settlement and shared deletion. Recovery-side dispatch/settlement is deferred to 8.2. |
| RN1–3, RCl1–3, RH3, RQ2–3 | recovery_operations: quiet expiry, final/ledger retention, stale checks, clock refusal/acceptance, future rows, holds, restore-lock checks and reporting. Existing tests pin the registration side and CLI secrets. |
| RN4–6 | recovery_scrub: source preservation, every scrubbed column NULL, ordering before final check/fsync/link, published hash, unsupported schema and partial-file cleanup; original backup suite. |
| RR1–7 | recovery_operations fixture histories, per-write rollback injection, status/gate idempotency, historical schema; no_caller pins unchanged script/Nginx and call order; existing restore-script harness. These are SQL fixture/code-path evidence, not a real recovery redemption or Linux restore. |
| RR8, RR13, RR16–18, RR21 | recovery_restore_credentials: independent dispatch, no live open, read-only files/directories, env fallback, missing db, headers, sidecars, WAL-only commit hazard and prepared backup, symlink/hardlink/live identity, same-file inputs, replaced-side refusals as unavailable evidence, expected hashes, changing files, unsupported columns/storage and unavailable evidence. |
| RR9–12, RR14, RR19–20, RR22 | Real account/TOTP changes, feasible CLI repairs, passwordless accounts, used recovery codes, sign-in hash upgrade, historical levels 011/012/015/016/021/023, pre-016 admin rotation; exhaustive 5,760 ordered classification/category/finding combinations; secret-output assertions. |
| RR15 | no_caller runbook assertions plus DEPLOYMENT §37: required isolation, all ingress/connection/tunnel/SSH/NAT checks, argument-free logs, preparation, repair, validation and postponement. Static only. |
| RQ1 | Existing manage_accounts/account/TOTP/cutover suites; set-password and TOTP APIs unchanged. Pending-code invalidation is 8.2 evidence. |
| RE10, RZ1 | AST/string scans over every web/src Python module, imports/call sites/deploy scans, fresh-interpreter application/WSGI/blueprint/operational-command import checks; whole-suite recovery_audit denies nonfixture inserts and checks nonfixture storage at close/collection across the dedicated interpreter's subprocesses. |

## Deviations and evidence limits

1. *Resolved by clarification after the review.* RR16–RR18 said a refused input
   "classifies nothing" / "prints no class", and design §6.6 itself said "nothing
   classified", while the same §6.6 (row 1) and RR13 class every snapshot account
   uncertain when replaced evidence is unavailable. The documents now state the
   selected policy in one form: nothing is classified from the refused input; a
   refused snapshot classifies nothing; a refused replaced input is unavailable
   evidence, RR13 / row 1 applies, every snapshot account is uncertain (no
   evidence), and no comparison class is emitted. The implementation already
   behaved this way; no class or repair changed.
2. CLI rejects partial recovery storage (table without ledger column or ledger
   column without table) in addition to the table allow-list. Snapshot scrub
   validates its own sensitive table. Unknown schema identifiers and SQLite
   error text are not echoed, to avoid leaking data from malformed schemas.
3. The standalone comparison module is an internal implementation split; only
   manage_accounts exposes its command. No unrelated public APIs were widened.
4. Runtime import probes cover app/WSGI/production, all blueprints and operational
   command modules. Every web/src module has static import/write checks. Legacy
   content scripts with optional tooling dependencies are not all dynamically
   imported; no claim is made that executing those scripts was tested.
5. No Linux/systemd/Nginx rehearsal, deployed SQLite-version check, provider,
   browser, timing/capacity or privacy/wording evidence was produced. These remain
   activation prerequisites, along with independent security review. Static
   checks and process shims are not host isolation evidence.

## Execution evidence

- Python 3.12.15; Python SQLite 3.53.4; local SQLite shell 3.51.0. Deployed SQLite
  version was not examined.
- Repository requirements verified with pip --no-index against real installed
  dependencies: Flask 3.1.3, Werkzeug 3.1.9, Gunicorn 23.0.0, PyOTP 2.10.0,
  cryptography 50.0.2, segno 1.6.6, Authlib 1.8.0, joserfc 1.7.5,
  requests 2.34.2, urllib3 2.8.0. Jinja2 3.1.6; python-docx 1.2.0/lxml 6.1.3
  were available for existing document tests.
- PyPI installation failed DNS resolution both inside and outside the sandbox.
  Initially the dedicated scratch venv referenced another verified scratch
  installation. After its package files disappeared between sessions, intact
  packages at the same versions were copied from a second scratch installation
  into the dedicated venv. Imports, pip check and requirements were reverified;
  no dependency stubs or fake crypto were used.
Every count in this section is **pre-commit worktree verification** by the
implementer, reported from its local logs. Nothing binds these runs
cryptographically to `ba4b9cd`: the commit was made about four minutes after the
final full run, from the state that run reported. Runs overlap (the focused
runs are subsets of the full suite) and their counts must not be added together.

- Initial focused run: **344 tests passed**, including **38 recovery tests** and
  the 5,760-combination subtest matrix. Earlier runs caught missing manifest
  exclusion metadata, outdated static sweeps and test-fixture errors; corrected
  before this run. Existing backup tests passed without modification.
- The first full run executed **4,396 tests in 941.118 seconds**, with 58 failures
  and one error: 54 failures came from audit inspection reads entering existing
  SQL traces, four from outdated schema/metadata expectations, and the error
  from a pre-025 fixture invoking the current settlement service. The audit now
  excludes only its own inspection reads from the caller's trace. Existing
  query budgets/transaction assertions remain unchanged. Schema expectations
  and the service fixture were updated; migration-022-only tests still isolate
  that migration. A later focused attempt could not import the missing scratch
  dependencies described above; it is not counted as a passing test run.
- Corrected focused run: **540 tests passed in 57.264 seconds**, covering every
  affected suite and all 38 recovery tests, with the corrected audit enabled.
- All **20 changed/new Python files** compile. An additional fresh temporary
  canonical build reached migration 025 with no foreign-key violations and zero
  recovery rows. A direct SQL probe with a valid real-account fixture accepted a
  valid session digest, rejected six malformed digests, and rejected either
  missing identity field independently (the suite also checks shadow rows).
- Final full suite: **4,396 tests passed in 966.697 seconds**, exit status 0,
  on the corrected implementation/test state. No tests were skipped. Command:
  `PYTHONDONTWRITEBYTECODE=1 /private/tmp/recovery-8-1-venv/bin/python -B -m unittest discover -s tests`.
  The process ran outside the sandbox for the existing temporary process-signal
  and restore-lock harnesses; these use test shims, not host services. Tool
  session yields were tracked until process exit and the audit flush completed.
- Final RZ1 audit (during the 4,396-test full run above, not a separate run):
  **7,397 process reports**, **38,914 successful connections**,
  **35,438 empty recovery-storage checks**, **zero violations**. Separately:
  195 fixture-storage observations, 3,262 absent-storage observations and 22
  unreadable/already-closed observations. These are connection/inspection
  counters, not distinct database counts; destructor inspection can also see a
  partially constructed connection that never returned successfully. Fixture
  and uninspectable storage are not claimed to be empty. The main process is
  included in these totals; every supported nonfixture recovery-storage check
  was empty, with no production insertion observed. **Limits:** the counts are
  checks at connection close/collection, not distinct databases, and the audit
  is evidence, not complete proof. It sees only connections made through
  `sqlite3.connect` in interpreters that load its hook: Python started with
  `-S` and the `sqlite3` shell are not instrumented (rows they leave are seen
  only when a hooked connection later closes on that database). A connection
  opened while a recovery test is anywhere on the stack is exempt from the
  emptiness check (its inserts are still judged by their immediate caller).
  The static scan does not match schema-qualified writes such as
  `INSERT INTO main.password_recoveries`. Denied inserts are caught when the
  statement is prepared, so a rolled-back insert is still a violation.
- Local evidence logs (temporary, not committed):
  `/private/tmp/recovery-final-focused.log` (344),
  `/private/tmp/recovery-8-1-corrections-focused.log` (540),
  `/private/tmp/recovery-8-1-initial-full-suite.log` (earlier failed full run),
  `/private/tmp/recovery-8-1-full-suite.log` (passing final full run),
  `/private/tmp/recovery-8-1-final-full-audit.jsonl` (final audit).
- Pre-commit Git checks: `git diff --check` passed; the staged diff was empty.
  The implementation then held **20 tracked modifications and 11 new files**
  (tracked diff stat **475 insertions, 64 deletions**, excluding the new files),
  all committed in `ba4b9cd`. Canonical JSONL, the restore script and the Nginx
  template have no diff.

## Independent review and post-review cleanup

Independent review (Claude Opus / High) of exactly `ba4b9cd`: **APPROVE WITH
NON-BLOCKING NOTES**. Its own evidence, separate from the implementer's and not
to be added to it: the 38 recovery tests passed; 802 tests in 27 adjacent
modules passed (a set that includes those 38); an audit-instrumented run of 480
tests in 17 modules found 0 violations (642 empty checks, a subset run with the
hook loaded through `PYTHONPATH`, so sanitized-environment subprocesses were
not instrumented); 29/29 temporary-database storage/backup/cleanup probes
passed; an independent model of the design 6.6 table agreed with `classify` in
58,320 cases. Python 3.12.15, SQLite 3.53.4, real dependencies.

Notes acted on in the uncommitted cleanup after `ba4b9cd`:

- **Same-file guard.** A replaced input with the snapshot's device and inode
  (the same path or a hard link) was compared with itself and reported every
  account unchanged, exit 0. It is now unavailable replaced evidence: every
  snapshot account uncertain (no evidence), exit 1. Regressions cover the same
  path and a hard link; they fail against `ba4b9cd`'s module. The live `--db`
  alias is still refused first.
- **Unavailable-evidence wording** aligned in design §6.6, RR16–RR18, this report
  and DEPLOYMENT §37 (deviation 1). New replaced-side tests: live alias, live
  path, symbolic link, change during the read and expected-hash mismatch, each
  asserting exit 1, the uncertainty guidance, no comparison class, no secret in
  the output and unmodified inputs.
- **Runbook order.** DEPLOYMENT §37 now stops account activity and rechecks
  isolation before preparing/comparing evidence and repairing (design 6.6 steps
  5–7).
- **Provenance** above corrected.

Deferred observations (tampered databases only; no change in 8.1): the scrub's
schema check is a column allow-list and does not refuse a trigger on
`password_recoveries` (one could copy scrubbed values elsewhere during a scrub);
with 025 unrecorded, a case-variant table name such as `PASSWORD_RECOVERIES` is
treated as absent. The registration scrub has the same pattern.

## Changed-file inventory (committed in `ba4b9cd`)

Tracked modifications:

- data/canonical/manifest.json
- src/backup_sqlite.py, src/canonical_tables.py, src/manage_accounts.py
- web/services/registration.py
- tests/test_canonical_content.py, tests/test_manage_accounts.py, tests/test_schema_baseline.py
- tests/test_build_database.py, tests/test_database_test_support.py,
  tests/test_production_deployment.py, tests/test_registration_issuance_migration_sql.py
- docs/ACCOUNTS_DESIGN.md, docs/DEPLOYMENT.md, docs/TESTING.md
- docs/reviews/EMAIL_REGISTRATION_DESIGN.md, docs/reviews/EMAIL_REGISTRATION_SCOPE.md
- docs/reviews/PASSWORD_RECOVERY_DESIGN.md, docs/reviews/PASSWORD_RECOVERY_SCOPE.md,
  docs/reviews/PASSWORD_RECOVERY_TEST_PLAN.md

New files:

- web/migrations/025_password_recoveries.sql
- web/services/recovery_scrub.py
- src/restore_credential_diff.py
- tests/recovery_support.py, tests/recovery_audit.py
- tests/test_recovery_migration_sql.py, tests/test_recovery_scrub.py,
  tests/test_recovery_operations.py, tests/test_recovery_restore_credentials.py,
  tests/test_recovery_no_caller.py
- docs/reviews/PASSWORD_RECOVERY_8_1_STATUS.md
