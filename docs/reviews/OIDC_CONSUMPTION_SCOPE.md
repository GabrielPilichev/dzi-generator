# OIDC consumption storage

Status: **proposed scope, awaiting independent review. Nothing is implemented:
no branch, pull request, migration or service exists, and no number is
assigned to any of them.**

This file is the complete specification of the prerequisite of Google OIDC
sign-in. `GOOGLE_OIDC_DESIGN.md` and `GOOGLE_OIDC_TEST_PLAN.md` refer to it
and do not repeat it.

## Purpose

An OAuth transaction lives in the signed session cookie, and the server
cannot revoke a cookie. Removing the transaction from a response does not stop
an earlier copy of the cookie from presenting it again
(`docs/ACCOUNTS_DESIGN.md` §13, amendment of 2026-10-11). This release adds
the one piece of server-side state that makes "used once" enforceable: a
table that records that a key was used, and a service that records it
atomically.

Storage only, like the identity-storage release before it (migration 019): a
migration, a classification entry and a service that nothing calls yet. No
route, dependency, configuration, page or behaviour changes.

## Prerequisites and approval

- Merged PR #330: external identity storage, migration 019.
- **Approved** (Gabriel, 2026-10-11, decision 1 of `GOOGLE_OIDC_SCOPE.md`): a
  dedicated consumption table, hash-only, delivered by a separate prerequisite
  PR; `login_throttle` is not used for it; duplicates and storage errors fail
  closed; retention covers the whole acceptance lifetime plus an allowance;
  cleanup cannot make an accepted transaction reusable.
- **Proposed, not approved:** everything below that gives those decisions a
  concrete form (names, the schema text, the API, the obligations, the tests).

## 1. Schema

`web/migrations/<next>_oidc_consumed_transactions.sql`, where `<next>` is the
next free number when the PR is prepared (020 if nothing else is merged
first). Additive: one table, no index, no row; no other table is touched.

```sql
CREATE TABLE IF NOT EXISTS oidc_consumed_transactions (
    tx_hash TEXT PRIMARY KEY
        CHECK (typeof(tx_hash) = 'text' AND length(tx_hash) = 64
               AND tx_hash NOT GLOB '*[^0-9a-f]*'),
    expires_at INTEGER NOT NULL
        CHECK (typeof(expires_at) = 'integer' AND expires_at >= 0)
) WITHOUT ROWID;
```

The file begins with `PRAGMA foreign_keys = ON;` like migration 019.

- `tx_hash` is the key of section 3: 64 lower-case hexadecimal characters.
  The `CHECK` makes any other shape impossible to store; a raw state value
  is 43 characters of another alphabet.
- `expires_at` is the expiry of the object the key was derived from, in whole
  seconds of the callers' monotonic clock (section 4). It is not a wall-clock
  time.
- **Nothing else is stored.** No state, nonce, verifier, code or token; no
  user id, subject, email, purpose or address; no wall-clock time.
- **No `instance` column.** Which service run wrote a row is not recorded and
  decides nothing in the storage (section 5).
- `WITHOUT ROWID` and no secondary index: the table holds the callbacks of a
  few minutes.
- **Column affinity.** `expires_at` has INTEGER affinity, so SQLite converts a
  value that reads as a whole number before the `CHECK` sees it: the texts
  `'600'`, `' 600 '` and `'6e2'` and the real `600.0` are each stored as the
  integer 600. That is accepted, not a gap: what is stored is always an
  integer of at least 0. Refused: text that is not a number, a real with a
  fraction, a negative number, `NULL`, a BLOB. `tx_hash` has TEXT affinity: an
  integer becomes text and fails the length check; a BLOB stays a BLOB and
  fails `typeof`. (Measured on SQLite 3.53.4; "Evidence" below.)

## 2. Classification and manifest

- `src/canonical_tables.py`: `"oidc_consumed_transactions": (OPERATIONAL,
  "single-use records of OIDC transactions (keyed hashes only)")`, beside
  `login_throttle`. It is bookkeeping with a lifetime of minutes and holds no
  account state, which is what `OPERATIONAL` means there.
- `data/canonical/manifest.json`: the migration is appended to
  `schema.migrations` and becomes `schema.migration_level`; the table joins
  `classification.operational` and `operational_exclusions`. Canonical
  tables, files, row counts and hashes do not change.
- The table is never exported to `data/canonical`. A database built by
  `src.build_database` has it empty.

## 3. Service API

`web/services/oidc_consumption.py`. Standard library only: no Flask, no
blueprint, no logging, no clock. Like the other services it takes an open
`sqlite3` connection and **never commits or rolls back: the caller owns the
transaction and the clock.**

**Constants**

- `PURPOSE_CALLBACK = "callback"`, `PURPOSE_LINK_CONFIRM = "link-confirm"`,
  `PURPOSES = frozenset({PURPOSE_CALLBACK, PURPOSE_LINK_CONFIRM})`. The set is
  fixed. Adding a purpose is a change to this module with its own review.
- `RETENTION_ALLOWANCE = 60` seconds.

**Exceptions**

- `ConsumptionRefused(Exception)`, with two subclasses: `AlreadyConsumed` and
  `Expired`. No exception message contains a key or a value.

**`consumption_key(secret_key, purpose, value) -> str`**

- Returns `HMAC-SHA256(secret_key, b"oidc-consume\0" + purpose + b"\0" +
  value)` as 64 lower-case hexadecimal characters.
- `secret_key`: non-empty `bytes`, or a `str` that is encoded as UTF-8, as
  `login_throttle.identifier_hash` does
  (`web/services/login_throttle.py:24`).
- `purpose`: a member of `PURPOSES`. Anything else is a `ValueError`.
- `value`: a `str` of 1–128 characters from `A–Z`, `a–z`, `0–9`, `-` and `_`
  (the alphabet of `secrets.token_urlsafe`). Anything else is a `ValueError`.
- Neither part can contain the separator, so two different pairs of purpose
  and value never produce the same message, and the two purposes never
  produce the same key for one value.

**`consume(conn, key, *, now, expires_at) -> None`**, in this order:

1. Arguments. `key` is a `str` of 64 lower-case hexadecimal characters; `now`
   and `expires_at` each satisfy `type(x) is int` and `0 <= x < 2**62`.
   Otherwise `ValueError`. Booleans, floats and strings are refused here,
   whatever the column would have accepted.
2. `conn.in_transaction` is false: `RuntimeError`. The service works only
   inside a transaction the caller has begun.
3. `now >= expires_at`: `Expired`. Nothing is read, written or deleted.
4. `DELETE FROM oidc_consumed_transactions WHERE expires_at < :now -
   RETENTION_ALLOWANCE`. This is the only statement anywhere that deletes
   rows, and expiry is its only condition.
5. `INSERT INTO oidc_consumed_transactions (tx_hash, expires_at) VALUES (:key,
   :expires_at)`. `sqlite3.IntegrityError` becomes `AlreadyConsumed`.

It returns `None`. Any other `sqlite3.Error` propagates unchanged.

## 4. Caller obligations

The service cannot check these. Every caller meets all of them, and the PR
that adds a caller pins them with tests at its call sites.

1. **Lock, then clock.** The caller executes `BEGIN IMMEDIATE` first, reads
   the clock once after that statement has returned, and passes that reading
   as `now`. It never passes a reading taken before it held the write lock.
2. **One clock.** `now` and every `expires_at` are whole seconds of one
   monotonic clock that all processes writing to the database share:
   `int(time.monotonic())`. Never the wall clock.
3. **A fixed, authenticated expiry.** `expires_at` is the expiry stored in the
   object the key was derived from, after the caller has verified that
   object's integrity, and it is passed unchanged. It is never computed at the
   time of consumption and never taken from a request. One key therefore has
   one `expires_at` in every call that ever names it.
4. **Only the run that issued it.** Before calling `consume`, the caller
   refuses every object that the current run of the service did not issue. A
   restart, a reboot and a restore each begin a new run.
5. **Acceptance means: `consume` returned and the caller's `COMMIT`
   succeeded.** Everything else means "do not proceed": both refusals,
   `ValueError`, `RuntimeError`, any `sqlite3.Error`, and a commit that
   fails. No error is read as "not used yet".
6. **The caller finishes the transaction** after every outcome, by committing
   or rolling back. After `AlreadyConsumed` the transaction is still usable:
   only the failed `INSERT` is undone; the cleanup of step 4 and the caller's
   own earlier writes are still pending.
7. **Keys come only from `consumption_key`,** with one of the listed
   purposes.
8. **Nothing else deletes rows** while the service runs: no other code, and no
   operator procedure.

Why obligation 1 is stated first: with the clock read before the lock, a
caller that waits for the lock holds a stale `now`. If another caller's
cleanup removes the row in the meantime, the stale `now` passes step 3 and the
`INSERT` succeeds a second time. `RETENTION_ALLOWANCE` covers a reading that
is up to 60 seconds stale, and a request's connection waits at most 5 seconds
for the lock (`web/db.py:29`). The allowance is a margin for a caller that
gets this wrong; the guarantee below assumes the obligation is met.

## 5. What is guaranteed, and what is not

**A key is accepted at most once.** Let every caller meet obligations 1–3.
SQLite's write lock puts the calls in an order. Each caller reads the clock
while it holds the lock, and the clock never goes back, so the `now` values
never decrease in that order. Take a key with expiry `E` whose `consume`
returned at `now = t` and was committed, so `t < E`. For any later call with
that key, at `now = u`:

- the row is still there: the `INSERT` fails, `AlreadyConsumed`;
- the row is gone: some call in between deleted it at a `now = d` with
  `E < d - 60`. Then `u >= d > E`, and step 3 answers `Expired` before
  anything else happens.

Either way the key is refused. An expired key is refused before the cleanup
runs, so cleanup never opens a way back in.

**What this does not rely on.** The argument names no process, worker or
instance. It holds for one worker or many, with or without `preload_app`,
whenever each module happened to be imported, and for an old and a new
process that overlap during a restart. A row is never deleted because some
other run wrote it.

**A consume that is rolled back is not an acceptance.** The key is unused
again. A caller that needs the record to survive a later failure commits it
by itself first; a caller that needs the record and another write to stand or
fall together commits them together.

**Restart of the service (same boot).** The clock continues. Rows of the
ended run are ordinary rows: each is deleted by the first consumption after
the clock has passed its expiry plus the allowance.

**Reboot.** `time.monotonic()` starts again near zero, so the rows of the
earlier boot carry values of a clock that no longer exists.

- They are **inert**. Their keys can be derived only from objects of an
  earlier run, and obligation 4 refuses those before `consume` is called.
  A fresh object has a fresh random value and cannot meet an old key.
- They are **removed by the same rule**, when the new clock passes their
  expiry plus the allowance. That can take as long as the earlier boot's
  uptime, and if the host never stays up that long they remain.
- They are **few**: at most the rows that were retained at the last
  consumption before the reboot, that is, the matched callbacks of about
  eleven minutes. A row is one 64-character key and one integer.
- Nothing needs to remove them, and nothing does.

**Why obligation 4 is needed.** Expiry values mean something only within one
run of the clock. After a reboot the storage alone would accept an *unused*
object of the earlier boot for as long as its old `expires_at` exceeds the
new clock. The caller's check, not the table, is what refuses it.

**Suspend.** On Linux the monotonic clock does not count time during which
the system is suspended. Lifetimes and retention stretch by the same amount,
so nothing is accepted twice. A production host is not suspended.

**Restore.** A snapshot holds the table as it was when the snapshot was
taken; rows written afterwards are lost. The supported restore
(`deploy/learnpilot-restore.sh`, `docs/DEPLOYMENT.md` §17) stops the service
in its step 2 and starts it in its step 11, which begins a new run, so
obligation 4 refuses every earlier object and a lost row cannot matter. The
snapshot's own rows are inert in the sense above. The storage does not make a
lost row harmless by itself.

**Limits**

1. A database rolled back or replaced **under a running service** loses rows
   without a new run beginning. A key used in that interval could be accepted
   once more within its remaining lifetime. The restore procedure forbids
   doing its steps by hand.
2. Rows deleted by anything other than step 4 (obligation 8) void the
   guarantee for those keys until their expiry.
3. Obligation 2 holds for all processes of one host within one boot. It does
   not hold across two hosts, or between processes in different Linux time
   namespaces, which have separate monotonic clocks. The deployment has one
   host and uses no time namespace; the deployment guide states the
   requirement.

## 6. Deployment integration

- **Order.** The standard procedure of `docs/DEPLOYMENT.md` §20, with nothing
  added. On a database at migration 019 the dry run lists exactly this
  migration. From this release on the application refuses to start on a
  schema without it (§9 there), as for every migration.
- **Effect.** One empty table. Accounts, sessions, sign-in and every page are
  unchanged, and nobody is signed out.
- **Backup and restore.** The table is part of the live database, so the
  snapshot of §16 and the restore of §17 cover it with no change to either.
  A snapshot from before this release is migrated by the restore script,
  which adds the empty table. `deploy/learnpilot-restore.sh` is not changed.
- **Rollback.** Migrations are forward-only (§21). Once this one is applied,
  every build that serves the database ships it together with its
  classification and manifest entries, a recovery build included. The table
  stays, empty; it is not dropped.
- **`docs/DEPLOYMENT.md`** gains one section for the migration, written like
  §27: deployment order, backup and restore, rollback, and the one-clock
  requirement of limit 3.
- No environment variable, Nginx, Gunicorn or systemd change.

## 7. Acceptance tests

Temporary databases built from migrations (`tests/support.py`);
`data/questions.db` is never read. No test sleeps: the clock is an argument.

**Migration (`tests/test_oidc_consumed_transactions_migration_sql.py`)**

- applied to a database at the previous level it adds one table and nothing
  else: no index, no row, no change to any other table, row or sequence;
  applying it twice changes nothing;
- the table is `WITHOUT ROWID` and has exactly the columns `tx_hash` and
  `expires_at`; there is no `instance` column;
- `tx_hash` refused: 63 and 65 characters; upper-case hexadecimal; a
  non-hexadecimal character; a 43-character URL-safe value; `NULL`; a 64-byte
  BLOB of hexadecimal characters; the integer 123 (stored as text, too
  short);
- `expires_at` refused: the text `'soon'`; the real `600.5`; `-1`; `NULL`; a
  BLOB;
- `expires_at` **accepted and stored as the integer 600**: the text `'600'`
  and the real `600.0`. The test asserts `typeof(expires_at) = 'integer'` for
  both, so that the affinity rule is pinned and not assumed;
- a duplicate key is refused by the primary key;
- the file is the SQL block of `docs/ACCOUNTS_DESIGN.md` §5 and nothing else,
  is numbered without a collision, and contains no destructive statement (as
  the test of migration 019 checks its file).

**Classification and build**

- `TABLE_CLASSIFICATION` lists the table as `OPERATIONAL`, and not as
  canonical or runtime; the manifest's migration list, level, operational
  classification and operational exclusions match;
- a database built by `src.build_database` has the table and it is empty;
- the assertions on the migration level and the operational table list in
  `tests/test_build_database.py`, `tests/test_canonical_content.py` and
  `tests/test_schema_baseline.py` are updated.

**Service (`tests/test_oidc_consumption_service.py`)**

- `consumption_key`: 64 lower-case hexadecimal characters; deterministic;
  changes with the secret, the purpose and the value; a `bytes` and a `str`
  secret give the same key; the two purposes give different keys for one
  value;
- **purposes are fixed:** each member of `PURPOSES` is accepted; an unknown
  label, an empty string, `None` and an upper-case spelling are `ValueError`;
  `PURPOSES` is exactly the two labels;
- values: an empty string, 129 characters, a space, a NUL and a non-`str` are
  `ValueError`; an empty secret is `ValueError`;
- a fresh key: `consume` returns `None` and leaves exactly one row holding
  the key and the `expires_at` that was passed;
- a second `consume` of the key raises `AlreadyConsumed` and changes no row,
  with the same and with a later `now`;
- **expiry before cleanup:** `now == expires_at` and `now > expires_at` raise
  `Expired`; `now == expires_at - 1` is accepted; an `Expired` call writes
  nothing and deletes nothing, including a row that the cleanup would have
  removed at that `now`;
- malformed arguments raise `ValueError` and write nothing: each key shape of
  the migration tests; a boolean, float, string, negative or `None` time; a
  time of `2**62`;
- outside a transaction `consume` raises `RuntimeError` and writes nothing;
- **caller-owned transaction:** `consume` never commits and never rolls back,
  after success and after each refusal; the caller's rollback removes the
  record and the key is then accepted again; the caller's commit keeps it;
  the caller's earlier writes in the same transaction survive
  `AlreadyConsumed` and can be committed;
- **cleanup by expiry only:** a row with `expires_at == now - 60` stays; one
  with `expires_at == now - 61` goes; no other property of a row or of the
  caller makes a row go;
- **cross-instance regression:** two connections stand for two service
  instances on one database. A consumes key 1; B consumes key 2 at a later
  `now`; A's replay of key 1 raises `AlreadyConsumed`, and key 1's row is
  still present. The same holds for every `now` of B up to key 1's expiry
  plus the allowance;
- **cleanup cannot free a key:** for a key recorded with expiry `E`, every
  later `consume` of it with a `now` that is not smaller than the recording
  one raises: `AlreadyConsumed` while the row exists, `Expired` afterwards;
- **randomised model:** several connections take turns under `BEGIN
  IMMEDIATE` with a clock that never decreases; no key is accepted twice;
- **concurrency:** twelve threads, one connection each, on one key under
  `BEGIN IMMEDIATE`: exactly one returns, eleven raise `AlreadyConsumed`,
  none errors;
- **rows of an earlier boot:** with rows whose `expires_at` lies far above a
  small `now`, a fresh key is accepted, the old rows stay, and they go once
  `now` passes their expiry plus the allowance;
- **storage failures propagate** as `sqlite3.Error` and nothing is recorded:
  a missing table, a `query_only` connection, a writer lock held past the
  busy timeout;
- the module imports only the standard library, and neither `time` nor
  `datetime`; it logs nothing; no key or value appears in an exception
  message;
- **nothing uses it yet:** no module under `web/` or `src/` imports
  `oidc_consumption`. The PR that adds callers replaces this test by the list
  of modules allowed to import it.

## 8. Files

| File | Change |
| --- | --- |
| `web/migrations/<next>_oidc_consumed_transactions.sql` | New. |
| `web/services/oidc_consumption.py` | New. |
| `src/canonical_tables.py`, `data/canonical/manifest.json` | Section 2. |
| `tests/test_oidc_consumed_transactions_migration_sql.py`, `tests/test_oidc_consumption_service.py` | New. |
| `tests/test_build_database.py`, `tests/test_canonical_content.py`, `tests/test_schema_baseline.py` | The migration level and the operational table list. |
| `docs/DEPLOYMENT.md` | The new section of section 6. |
| `docs/ACCOUNTS_DESIGN.md` | §5: the schema of section 1 as an SQL block, which the migration test compares with the migration file as the 019 test does (`tests/test_user_identities_migration_sql.py:388`). §13 and the status list: the table and the service exist; nothing uses them yet. |

The schema enters `docs/ACCOUNTS_DESIGN.md` only with that PR, once this
scope is approved. Until then §13 records the amendment as planned and
contains no schema.

## Exclusions

No caller of the service, route, hook, session change, provider code,
dependency, configuration, Nginx or Gunicorn change, page, account or CLI
change. No deletion rule other than expiry and no scheduled cleanup. No
server-side OAuth transaction store: the table holds no transaction. No real
database migration and no deployment.

## Evidence

A scratch experiment on temporary databases, outside the repository and not
part of any PR (E11 in `GOOGLE_OIDC_DESIGN.md` 11.3; Python 3.12.15, SQLite
3.53.4, macOS). A prototype of sections 1 and 3 gave:

- the affinity results of section 1;
- two instances, A then B then A again: with a rule that deletes rows of
  other instances, A's replay was **accepted**; with the rule of section 3 it
  raised `AlreadyConsumed`;
- a randomised model with three instances and one clock read under the lock,
  200 runs: 17337 acceptances, no key accepted twice. The same model under
  the instance-deleting rule accepted 3737 keys twice;
- a clock reading taken before the lock: with no allowance, a reading 2
  seconds stale was accepted a second time; with the 60-second allowance, 2
  and 59 seconds were refused and 62 was accepted. Read under the lock, every
  case was refused as `Expired`;
- 40 keys with 12 simultaneous callers each: 40 accepted, 440 refused, no
  error;
- after a simulated reboot, 25 rows of the earlier boot were still present
  after seven days of the new clock and were deleted once the clock passed
  their expiry plus the allowance; an unused key of the earlier boot was
  accepted by the storage alone, which is the case obligation 4 exists for.

Not exercised: `time.monotonic()` on Linux. Its properties are taken from
Python's documentation ("cannot go backwards", "the same for all processes")
and the Linux manual page for `CLOCK_MONOTONIC` (seconds since boot, not
counting suspended time); the experiments passed plain integers as the clock.

## Execution

Claude Local Opus 5.5, Extra High reasoning.
Independent review in a separate Claude session.
Claude must not stage, commit, push, create PRs or merge. The branch and the
pull request are created by Gabriel.
Tests use isolated temporary databases only.

## Independent review refinements

These requirements supersede conflicting wording above:
- Enforce the hash's 64-byte length using
  length(CAST(tx_hash AS BLOB)) = 64 alongside its hexadecimal constraint.
  Test embedded/trailing NULs and trailing newlines.
- Map only a primary-key duplicate to AlreadyConsumed.
  Other integrity failures must propagate as storage failures.
- Cleanup remains expiry-only, independent of service instance.
- Callers obtain the write lock before reading fresh monotonic time,
  preserve the authenticated expiry for each key, and reject expired objects.
- This PR implements storage only. Google routes, session retirement,
  provider integration, activation policy and deployment are excluded.

The independent review found no blocker in this prerequisite.
PR #331 remains dependent on it and has a separate unresolved retirement-
failure specification. This scope does not approve the complete Google design.
