# Email registration, PR 3 — lifecycle, accounting and student activation: status and contract

Design: `docs/reviews/EMAIL_REGISTRATION_DESIGN.md` (sections 2, 3, 4.3, 6.3–6.5, 9).
Tests: `docs/reviews/EMAIL_REGISTRATION_TEST_PLAN.md`, sections D, D-L, D-S, D-A,
D-C, E and N.
Scope: `docs/reviews/EMAIL_REGISTRATION_SCOPE.md`.

**Status:** implemented in draft PR #337, **not approved**. The first
independent review asked for changes; this revision answers its findings
(issuance time, dispatch binding, collision classification, a real commit
failure, test and document corrections). Gabriel then **approved four
contract points** ("Approved decisions" below): one send per generation is
caller sequencing; a stored random issuance identity; code input trimmed at
the ends only; settlement by the same identity. A later independent review of
the revision implementing them returned **APPROVE WITH NON-BLOCKING NOTES**:
all three earlier blockers resolved, no activation or deployment approval.
Two small corrections were made **after** that review and are **not
independently reviewed**: a test no longer forbids the code digits inside a
random issuance identity (the valid identity `111111` + 26 × `a` is now a
regression case), and these documents were reconciled with the approvals. It is the registration service, two
`accounts` additions and migration 022 (one nullable column and one index on
the send ledger).
Nothing calls any of it: no route, template, session slot, gate constant, CLI
command, timer, production start-up change or mail configuration exists, and
no message was sent to anyone. No real database was migrated. Registration
cannot be reached in any environment, issues no session, activates students
only, and production activation stays off. Apart from the approved
points (caller sequencing of dispatch, the stored issuance identity, code
input trimming, settlement by identity and no settlement after `NotSendable`),
the deviations listed below remain **interpretations for the reviewer and for
Gabriel**, not approved changes to the design. Cancellation during a hold
still needs a decision before any route is exposed.

## What PR 3 contains

| Part | File |
| --- | --- |
| Pending lifecycle, codes, ledger, caps, circuit, settlement, shadow rows, student activation, clock policy, cleanup, the hand-over to a mailer | `web/services/registration.py` (new) |
| `hash_new_password`, `password_hash_is_current`, `create_verified_user`, `VERIFIED_REGISTRATION_ROLES` | `web/services/accounts.py` (additions only) |
| Issuance identity of the send ledger: `registration_sends.issuance_id` and its unique index | `web/migrations/022_registration_issuance_identity.sql` (new); `data/canonical/manifest.json` (migration list and level only) |
| Shared test fixture | `tests/registration_support.py` |
| Migration 022: upgrade, fresh-build parity, constraints, old rows, canonical exclusion | `tests/test_registration_issuance_migration_sql.py` (new) |
| Pins of the newest migration | `tests/test_build_database.py`, `tests/test_schema_baseline.py`, `tests/test_registration_migration_sql.py` (one line each) |
| Lifecycle, activation, collisions, clock, cleanup, preservation | `tests/test_registration_service.py` |
| Ledger, caps, total budget, circuit, concurrency | `tests/test_registration_ledger.py` |
| Settlement and `dispatch` | `tests/test_registration_settlement.py` |
| Documents | this file; scope status; design 0.7 and 6.3 (pointer rows only); `docs/TESTING.md`; `docs/ACCOUNTS_DESIGN.md` (status list); `docs/DEPLOYMENT.md` 32 |

Not changed: migration 021 and every earlier migration, every canonical
JSONL file and every table digest in the manifest, the table classification
(`registration_sends` was already runtime), `src/`, `web/app.py`, `web/production.py`, `web/wsgi.py`, every blueprint and
template, `web/services/mail*.py`, `profiles.py`, `registration_forms.py`,
`deploy/`, the vault. `accounts._USER_COLUMNS` and the CLI's
`REQUIRED_USER_COLUMNS` are **not** widened (see "Account readers").

## API (`web.services.registration`)

Every writer takes the caller's open connection and a `clock`: a callable
returning the timezone-aware UTC wall clock. `secret_key` is the application's
`SECRET_KEY` (text or bytes).

| Function | Returns |
| --- | --- |
| `binding_hash(secret_key, token)`, `email_hash(secret_key, email)` | The stored 64-hex keyed hashes (design 2.3, 3.4). |
| `availability(conn, now, *, sender_busy, total_cap=300)` | `Availability`: `AVAILABLE`, `HOLD`, `CLOCK_BEHIND`, `SENDER_BUSY`, `CIRCUIT_OPEN`, `TOTAL_CAP`, decided in that order. Reads only. |
| `address_capped(conn, address_hash, now)` | `bool`. Reads only. |
| `create_pending(conn, *, cleaned, account_type, password_hash, previous_binding, secret_key, clock, sender_busy, total_cap=300, teacher_enabled=False)` | `Result`: `ISSUED` (with `token`, `pending_id`, `send_id`, `kind`, `message`), `UNAVAILABLE` (with `reason`), `ADDRESS_CAPPED`. |
| `resend(conn, binding, *, secret_key, clock, sender_busy, total_cap=300, teacher_enabled=False)` | `ISSUED` (no token), `NO_PENDING`, `NOT_PENDING`, `UNAVAILABLE`, `EXPIRED`, `RESEND_EXHAUSTED`, `TOO_EARLY`, `ADDRESS_CAPPED`. |
| `verify(conn, binding, code, *, secret_key, clock, teacher_enabled=False)` | `NO_PENDING`, `NOT_PENDING`, `UNAVAILABLE`, `EXPIRED`, `CODE_EXPIRED`, `TOO_MANY`, `WRONG_CODE`, `EMAIL_TAKEN`, `ACTIVATED` (with `user_id`). |
| `cancel(conn, binding, *, clock)` | `CANCELLED`, `NO_PENDING`, `NOT_PENDING`, `UNAVAILABLE` (clock behind). |
| `settle_send(conn, send_id, issuance_id, outcome, *, clock)` | `Settlement`: `SETTLED` (this call recorded the outcome), `ALREADY_SETTLED` (nothing changed), `NOT_THIS_ISSUANCE` (no ledger row has this number and identity; nothing changed). `ValueError` for a malformed or missing identity. |
| `cleanup(conn, *, clock)` | `CleanupResult(clock_behind, scrubbed, deleted, deleted_sends)`. |
| `dispatch(conn, mailer, issued, *, secret_key)` | `SendOutcome`. **One `mailer.send` per call, not per generation**; writes nothing. Refuses (`NotSendable`) anything but the committed, unsettled, current issuance with its own identity, exactly as issued. A second call for the same generation before settlement sends again: preventing it is the caller's (approved). |

`Result.token` and `Result.message` (an `OutboundEmail` holding the code) are
left out of `repr` and out of `==`; they must not be logged or serialized.
`Result.issuance_id` (with `ISSUED` only) is the identity of the issuance:
32 lower-case hexadecimal characters, `secrets.token_hex(16)`, new for every
issuance and resend, derived from nothing. `send_id` and `issuance_id`
together name an issuance; the number alone does not. It is left out of
`repr` and of every log line. A refused issuance has neither.
Exceptions: `TransactionRequired`, `NotSendable`, `ValueError` for malformed
input (never holding a value).

`accounts.create_verified_user(conn, *, email, display_name, role,
password_hash, verified_at)` accepts role `student` only, an email already in
normalised form, and a hash of the current method and parameters; it sets
`email_verified_at` and `password_changed_at` to `verified_at`. It returns the
new id, or **`None` when, and only when, the insert met the uniqueness of
`users.email`** and wrote nothing. That is decided by SQLite
(`INSERT ... ON CONFLICT(email) DO NOTHING`, which names that one constraint),
never by the text or class of an exception. Every other failure raises: a
trigger's `RAISE` (extended code 1811, whatever its message), a CHECK, a
foreign key, `NOT NULL`, any other uniqueness, a storage error.
`accounts.hash_new_password` hashes the exact string.

Requirements of that statement: SQLite 3.24 or later (the upsert clause; the
high-water mark already needs it), any supported Python `sqlite3`. On an older
library the statement is a syntax error, `sqlite3.OperationalError`, which
propagates like every other failure: nothing is created and nothing is
reported as taken (fail closed). `users.email` must keep its `UNIQUE`
constraint (migration 012); without it the statement is refused when prepared.
A refused insert may use up a `users` id (AUTOINCREMENT), leaving a gap; no
existing id changes.

## Transactions and caller obligations (PR 4)

1. **Begin every writer's transaction yourself** (`BEGIN IMMEDIATE`). With no
   transaction open a writer raises `TransactionRequired` and writes nothing.
   The service never commits and never rolls back.
2. **Pass a clock, not a time.** The writer takes SQLite's write lock inside
   your transaction (a statement that changes no row) and only then calls
   `clock()`, once. A time read before the lock cannot stamp an issuance.
3. **Commit after every returned outcome**, refusals included (a counted
   attempt, an expiry and a voiding must persist). **Roll back after any
   exception and after a failed commit**: the registration is then exactly as
   before the call.
4. **`ISSUED` and `ACTIVATED` are decisions, not acceptances.** Render the
   confirmation, set the `reg` slot or send anything only after the commit has
   returned. Never answer "address taken" or "confirmed" from an exception.
5. **Order of a send:** `availability` and `address_capped` (no lock) →
   `accounts.hash_new_password` → `BEGIN IMMEDIATE`, `create_pending` or
   `resend`, commit → `dispatch` with **no transaction open** → `BEGIN
   IMMEDIATE`, `settle_send`, commit. `dispatch` raises `NotSendable` while the
   connection has a transaction open, and unless the result is the committed,
   unsettled, current issuance exactly as issued ("Dispatch binding" below).
   It assumes sqlite3's default transaction mode (a `SELECT` opens no
   transaction).
5a. **Call `dispatch` once per committed issuance (approved: caller
   sequencing).** The service guarantees one mailer invocation per call, not
   per generation: until the generation is settled a second or concurrent
   call sends again. Every handler path must make a second call impossible
   (a repeated POST, a retry after an error page, two workers, a redirect that
   re-enters the handler). Pass the `Result` you were given, unchanged, and
   only after your own commit of that issuance returned. Never retry a send.
5b. **Never settle after `NotSendable`.** Nothing was handed to a mailer, so
   there is no outcome to record; the generation, if it exists, stays
   `started` (a safe state: counted in the caps, not in the circuit).
5c. **Settle with both `send_id` and `issuance_id` of the `Result` you
   dispatched.** `NOT_THIS_ISSUANCE` means your result is not the committed
   issuance (rolled back, or older than migration 022): nothing was changed;
   log it by number and do not retry with other values.
6. Read `mail.sender_busy()` and pass it as `sender_busy` to `availability`,
   `create_pending` and `resend`. Pass `MailSettings.hourly_cap` as
   `total_cap` (1 to 300; it can only lower the budget).
7. Never retry a send. Settle whatever `dispatch` returned. A settlement
   transaction that fails is logged by class name and left: `started` is a
   safe state.
8. Pass `teacher_enabled` from the teacher gate. Until PR 7 it changes
   nothing at verification: a teacher registration is voided there whatever
   it says.
9. Drop the `reg` slot when the row left `pending`. Issue no session.
10. Log nothing of a `Result` but its outcome, ids and `reason`.
11. **The service's own log lines are written before your commit.** They are
    named as decisions (`submission_decided`, `resend_decided`,
    `activation_decided`, `email_taken_decided`, `wrong_code_decided`,
    `gate_off_decided`, `settlement_decided`, `cleanup_decided`) and are not
    evidence that anything became durable. Log the durable event yourself,
    after the commit returned.

## Behaviour

**Lifecycle.** `pending` → `activated`, `void` or `expired`; every final state
is kept and scrubbed in the statement that sets it (the nine columns of the
table CHECK). A request that finds a final row writes nothing. A pending
registration is not a `users` row, grants nothing and reserves no address; any
number may exist for one address. One per browser: a new submission voids the
row of `previous_binding`.

Which final state an **overdue** pending row gets depends on what finds it:
`verify`, `resend` and `cleanup` make it `expired`; `cancel`, which checks no
expiry, makes it `void`; a new submission makes the browser's overdue previous
row `expired`, because the inline cleanup runs before the voiding of the
previous row and has already closed it. All are final and scrubbed.

**Codes.** Six digits from `secrets`, stored as a keyed hash over the row id,
valid 10 minutes and never past the registration's 30, five attempts per
generation (wrong, malformed and missing codes count; a code that is not live
does not). **Input (approved):** the submitted value is trimmed of leading
and trailing whitespace only and must then be exactly six of the characters
`0`–`9`. `123 456`, a tab or newline inside, and digits of other scripts
(Arabic-Indic, fullwidth, superscripts) are wrong codes and count. Passwords
are not touched by this: they stay exact. Three resends at least 60 seconds apart; each replaces the code,
resets its attempts and is a new send generation. A correct code clears itself
before anything else is decided.

**When a code was issued** is read from the ledger: `attempted_at` of the
`registration_sends` row named by `code_send_id`. It is never worked back from
`code_expires_at`, because an expiry is cut at the registration's deadline (a
code issued at minute 28 expires at minute 30, not 38). A code is live when
it was issued no more than 300 seconds ahead of the clock and the clock is
before its expiry. The evidence is validated on every verification: the ledger
row exists, belongs to this registration, is of the row's kind (`code`, or
`notice` for a shadow), its stamp is exactly in the layout the service writes,
and the stored expiry equals `min(issued + 10 min, registration expiry)`.
Missing or inconsistent evidence is logged (`code_evidence_invalid`, ids only)
and answered `CODE_EXPIRED` with no attempt counted (fail closed); a resend
replaces it.

**Ledger and caps.** One ledger row per issuance, code or notice, stamped
under the lock, in the transaction that stores the code, before any send. An
address is refused at 5 rows in the trailing 60 minutes or 8 in the trailing
24 h 15 min (both inclusive of the boundary second); all addresses at 300 in
60 minutes. A row dated later than `now` counts in every window. **Nothing is
ever refunded:** no outcome deletes a ledger row, lowers `send_count` or
reopens a cap.

**Settlement.** Named by the generation number **and** the issuance identity,
validated under the write lock before anything is written. A number that does
not exist, an identity that is not the row's, and a row with no identity all
answer `NOT_THIS_ISSUANCE` and change nothing: no outcome, no circuit history,
no pending row. That answer makes no statement about whether anything was
sent. Otherwise conditional on `outcome = 'started'`; the first settlement
wins (`ALREADY_SETTLED` afterwards). Settlement belongs to the ledger row and
does not need the registration to be current: an older committed generation
settles its own row after a resend, a cancellation, an activation or an
expiry. Only `not_sent` touches a pending row, and only the
row whose live code that generation issued: code retired, wait waived. A late
failure of an older generation changes nothing of a newer code, its attempts
or its wait, nor of an activated, cancelled, replaced or expired row.

| Outcome (final, #336) | Row | Budgets | Circuit |
| --- | --- | --- | --- |
| `ACCEPTED` (the relay took it; never a statement about a mailbox) | unchanged | spent | counted as a success |
| `REFUSED` | unchanged | spent | left out of the history |
| `UNKNOWN` (421 before the final `250`, a deadline, an exception from a mailer, anything not established) | unchanged: code and wait stay | spent | counted as a failure |
| `NOT_SENT` | code retired, wait waived | spent | counted as a failure |
| never settled | unchanged, code usable | spent | not counted |

**Circuit.** Read from the ledger: of the rows settled `accepted`, `not_sent`
or `unknown`, the three with the highest generation ids all failed and the
newest was attempted less than 5 minutes ago. **Assumption:** attempts are
admitted and settled one after another (one synchronous worker). With several
handlers as many attempts as handlers are admitted before a failure is
settled, and attempts never settled never close it; the hourly cap is then the
only bound. Both limits are pinned by tests.

**Clock, mark and hold.** In `create_pending`, `resend` and `verify` the hold
and then the clock against the high-water mark are decided before any change;
in `cancel` and `cleanup` the clock only. More than 300 seconds behind the
mark: nothing is written, the mark included. The mark is raised by an
issuance and by a cleanup run whose `now` is later than it, never lowered, and
no hold or clock incident is released by anything in this PR. A row dated more
than 300 seconds in the future is expired only once the mark no longer says
the clock is behind.

**The guessing bound is conditional.** At most 8 codes (40 guesses) of an
address in any 24 hours **only while** the wall clock is not stepped, the
secret key is unchanged and no ledger row of the address is removed other than
by the 48-hour cleanup. The exceptions are pinned as exceptions: two 300-second
steps (13 codes, DC7), a key rotation (D11), forward steps of 24 h 15 min
(unbounded, DC4).

**Existing address (D8).** A shadow row: no password hash, a code hash no six
digits can produce, the notice instead of a code, the same ledger and caps.
No code activates it, including one forced to match. Nothing is overwritten,
linked or signed in. An address that appears only as a Google identity's
`email_at_link` is not a collision; the service never reads `user_identities`.

**Activation.** Under the caller's lock: `users` row (role literal `student`,
`email_verified_at` set), profile, row `activated` and scrubbed. `EMAIL_TAKEN`
in two cases only: the row is a shadow, or `create_verified_user` returned
`None` (the conflict-targeted insert met `users.email` and wrote nothing). No
exception is ever turned into `EMAIL_TAKEN` and no message is read. Every
failure propagates and leaves the transaction to the caller. No session, no
sign-in. A teacher registration is voided and answered `UNAVAILABLE`; it is
never activated and never made a student.

**Dispatch binding.** `dispatch` sends only when all of this holds, read from
the committed rows and recomputed with `secret_key`; no plaintext or log line
was added, and the one column added is the issuance identity of migration 022
(compared when present; a legacy NULL identity is neither sent nor settled):

- the ledger row of `send_id` exists for `pending_id`, is `started`, and its
  kind is `issued.kind`;
- the registration is `pending`, of the matching kind, and `send_id` is its
  current `code_send_id` (an older generation is not sent once replaced);
- the row's address hashes to the ledger row's address key;
- the token, when the result has one (first issuance), hashes to the row's
  binding hash;
- the whole message equals the one the service builds for the row's address,
  and for a code the six digits in it hash to the stored code hash.

So a result kept from a rolled-back transaction whose ids SQLite reused, and a
result whose recipient, subject, body, kind, token, identity or ids were
altered, are refused. The first condition of all, checked before the others:
**the ledger row holds the result's `issuance_id`.** A rolled-back issuance
has another identity than the committed one that got its numbers, whatever
else coincides: the recipient, the six digits, the token and the whole
message included (the earlier "byte-identical resend" limit is closed). A
ledger row without an identity is never sent.

**Not single-send.** One mailer invocation per call. Until the generation is
settled a repeated or concurrent call passes every check and sends again; no
durable send claim exists and no exactly-once claim about SMTP is made.

**Issuance identity and migration 022.** `registration_sends.issuance_id` is
`TEXT`, NULL or exactly 32 bytes of `0-9a-f` (a CHECK), unique among non-NULL
values (a partial unique index). The service writes
`secrets.token_hex(16)` with every issuance and resend, in the transaction
that writes the ledger row, and returns it in the `Result`. It is independent
of the code, the address, the row ids and the key, and it is not a credential:
it is never shown to a person, sent in a message or accepted from a request.
No code and no message body is stored anywhere in clear.

*Rows that predate the migration.* The column is added NULL; the migration
writes no row and invents no identity. Such a row keeps every value it had
and **counts exactly as before** in the per-address caps, the total budget
and the circuit, and the 48-hour cleanup removes it by age. It can be
neither dispatched (`NotSendable`) nor settled (`NOT_THIS_ISSUANCE`), so one
that was `started` stays `started`. A code such a row issued still verifies:
verification reads the ledger row's time and kind, not its identity. No such
row exists in any deployment (the service has had no caller); the rule is
tested with rows inserted at level 021.

*Compatibility.* Additive, not a table rebuild; no existing row, user,
profile, sequence or canonical file changes. A fresh build and an upgraded
database have the same schema. Code from before the migration that inserts a
ledger row without the column still works and produces an identity-free row.
Once applied, every build serving the database must ship
`022_registration_issuance_identity.sql` (`docs/DEPLOYMENT.md`, 32).

## Deviations and interpretations, for the reviewer

Design sections named exactly. Each is the smallest reading I found. Only what is marked
"approved" is; the rest stays an interpretation.

1. **`clock` instead of `now` in the writers (6.3, 3.4).** 6.3 gives every
   function `now`; 3.4 and test DL11 require that a submission which waited for
   the lock is not stamped with the reading it held. A value cannot meet that
   at the service; a callable read after the lock does. `availability` and
   `address_capped` keep `now`. Smallest correction to 6.3: replace `now` by
   `clock` in the six writers.
2. **`secret_key` parameter (6.3).** The signatures of 6.3 carry no key, and
   2.3, 3.1 and 3.4 need one. Added as a keyword, as `login_throttle` takes it.
3. **`sender_busy` and `total_cap` on `create_pending` and `resend` (6.3,
   6.4).** 6.4 decides availability again under the lock; the two functions
   need its inputs.
4. **`teacher_enabled` on `create_pending` and `resend` (6.3, 7.3).** 7.3 has
   `resend` void a teacher row while the gate is off, and 6.3 gives `resend` no
   such parameter. `create_pending` refuses a teacher registration without it
   and writes nothing. With it a row is stored, and is still voided at
   verification (D5).
5. **`dispatch` (4.3, 6.4).** A function that enforces "no transaction open"
   and "the committed, unsettled, current issuance exactly as issued", and
   maps a mailer's exception to `unknown` (test H4). It takes `secret_key`,
   builds no mailer and has no caller. It invokes the mailer once per call
   and **does not enforce once per generation** (approved: caller
   sequencing).
6. **`create_pending` and `resend` return the message**, an `OutboundEmail`
   with the texts of 4.5, where 6.3 says "code or notice marker". The code
   exists in clear nowhere else.
7. **`cancel` and the clock (3.8, 6.3).** 6.3's clock-first list omits
   `cancel`; 3.8 says no row is voided by a request while the clock is behind.
   `cancel` answers `UNAVAILABLE` (`CLOCK_BEHIND`) then and writes nothing.
   A restore hold alone does not stop it: the row is voided and scrubbed and
   the hold and the mark are left exactly as they were. With both, the clock
   decides: nothing is voided. Voiding during a hold is this PR's reading (a
   cancellation only removes data); the design does not say either way.
8. **`settle_send` takes the issuance identity and returns `Settlement`
   (approved), and is not stopped by a hold or the clock (4.3).** It is the
   SQL of 4.3 and records a fact about a send already made.
9. **`EMAIL_TAKEN` is narrower than 6.3.** 6.3: an `IntegrityError` from the
   insert with a `users` row of that address present. Here no error is
   classified at all: only SQLite's own report of a conflict on `users.email`
   (`ON CONFLICT(email) DO NOTHING`, no row inserted) or a shadow row.
   `create_verified_user` therefore returns `None` instead of raising for
   that one case. There is no read of `users` before the insert any more.
10. **`NO_PENDING` and `NOT_PENDING` precede the hold and the clock in
    `resend` too**, as 6.3 orders `verify`. They are reads.
11. **Whitespace in a submitted code (approved).** Leading and trailing
    whitespace is trimmed; inner whitespace is not removed and makes the code
    wrong. 3.1's "stripped of spaces" is read as that.
12. **A future-dated code (3.8)** is one whose ledger row says it was issued
    more than 300 seconds ahead of the clock; it is answered `CODE_EXPIRED`
    and counts nothing, also after the mark was reset.
13. **Final state of an overdue row.** Cancelled: `void`. Replaced by a new
    submission: `expired`, through the inline cleanup. See "Lifecycle".
14. **`password_changed_at` of a registered account is the verification
    time** (the injected clock, not `CURRENT_TIMESTAMP`).
15. **Teacher subjects are checked for shape only** when a pending row is
    stored; the registry check is the profile service's at activation (PR 7).
16. **Logging (3.10).** The service cannot log after a commit it does not
    make, so its lines are named as decisions (obligation 11), not with 3.10's
    event names; `cleanup_decided` carries `sends=<n>` as well;
    `code_evidence_invalid` is new. The durable events of 3.10 are PR 4's to
    log after its commits.
17. **Total-cap boundary.** A row exactly 60 minutes (or 24 h 15 min) old is
    still inside its window; it leaves one second later (tests DL2, DL3).

## Account readers (`_USER_COLUMNS`)

PR 3 **writes** `users.email_verified_at` and no account reader reads it, so
it is not "the first PR that reads the column" (design 6.1, 9). `_USER_COLUMNS`
and `REQUIRED_USER_COLUMNS` are unchanged, the migration-012 fixtures are
untouched, and the obligation stays with the first reader, expected to be
`manage_accounts list` in PR 4. `create_verified_user` needs migration 021 and
is called only at activation.

## Tests

`python3 -B -m unittest tests.test_registration_service
tests.test_registration_ledger tests.test_registration_settlement
tests.test_registration_issuance_migration_sql` (176 tests, about 9
seconds). Temporary databases cloned from the migrated canonical template, an injected clock, `InMemoryMailer` or a fake; no socket,
no sleep, no real mail. Where a test compares the codes of two issuances the
codes are injected (`RegistrationTestCase.codes`), so no result depends on two
random six-digit codes being different.

| Plan | Where |
| --- | --- |
| D1–D11, B8 after activation | `test_registration_service`: `AccountsAdditionsTest`, `PendingTest`, `VerificationTest` |
| K5 (service half), N3 | `TeacherTest` |
| DA1–DA6 | `ActivationErrorTest`, `PendingTest` (transaction ownership). DA5 is a real `SQLITE_BUSY` at `COMMIT` (rollback-journal file, a reader holding its shared lock) |
| Collision classification | `AccountsAdditionsTest`, `ActivationErrorTest`: trigger (1811, with the matching text and the address present), CHECK, foreign key, unrelated uniqueness, forced error text, and the real collision |
| Issuance evidence and skew boundaries | `ClockPolicyTest` |
| Dispatch binding and issuance identity | `DispatchBindingTest`: rolled-back first issuance, resend and notice whose ids, recipient, digits, token and message all equal the committed one's; altered or missing identity; a ledger row without one |
| Settlement identity | `SettlementIdentityTest`: stale result against a reused number, wrong and malformed identities, a row without one, duplicate settlement, late settlement after resend, cancellation and activation, the caller sequence after `NotSendable` |
| Code input policy | `VerificationTest.test_exact_code_input_policy` |
| Migration 022 | `test_registration_issuance_migration_sql`: upgrade from 021 with accounts, profiles, registrations and ledger rows present; fresh-build parity; constraints; old rows fail closed; caps and circuit read as before; canonical exclusion and byte-equal export |
| E1–E4, E6–E10 | `CollisionTest` (threads on separate connections; the CLI's real `create` as a process) |
| DC1–DC8 | `ClockPolicyTest` |
| N1, N2, N4 | `CleanupTest` |
| DL1–DL11, DL9a, C17 (registration half) | `test_registration_ledger` |
| E5, DL11 | `ConcurrencyTest`. DL11 includes the last slot of an address (and of the total budget) taken by the lock holder while another submission, which saw it free, waits; the waiter is refused under the lock and writes nothing |
| DS1–DS8, E11, H3–H5 and H7 (service half) | `test_registration_settlement` |
| J5 and canonical preservation | `PreservationTest` |

Not done here, and why: DL12, N5 and the CLI half of N4 (the commands are
PR 4); DA1's "each subject" (teacher activation, PR 7); what a route answers
and what its session holds after a failed commit (PR 4; here the caller
pattern claims no acceptance and the pending row is restored); DL10's "no
scrypt on a refused submission" is a route
property (PR 4), here pinned as "the pre-checks are reads and the service
never hashes"; sections F, G, H and K at route level (PR 4); D-R (PR 5).

Before the first full run, 37 one-line mutations of the service were each run
against the three modules; 34 failed a test and three were equivalent. For
this revision 27 mutations of the new guards (issuance evidence, dispatch
binding, the conflict-targeted insert) were run the same way: 26 fail a test;
the survivor is equivalent (`p.state = 'pending'` in the dispatch read, which
the table CHECK already implies for a row that still has a code hash). For
the identity revision 14 more (identity checks in `dispatch` and
`settle_send`, the identity format, the code input policy): all 14 fail a
test.

## Approved decisions

Approved by Gabriel after the first review of #337; implemented in this
revision and awaiting review.

1. **One send per generation is caller sequencing.** `dispatch` guarantees
   one mailer invocation per call, not per generation. PR 4 prevents a
   duplicate `dispatch` in its handler paths. No automatic retry, no durable
   send-claim state and no exactly-once claim about SMTP.
2. **A stored random issuance identifier** (migration 022). A result from a
   rolled-back transaction matches no later generation, even when SQLite
   reuses the numeric ids and the code digits coincide.
3. **Verification-code input:** leading and trailing whitespace is trimmed;
   inner whitespace is rejected; the rest must be six ASCII digits.
4. **Settlement requires the same issuance identity.** Callers never settle
   after `NotSendable`. A stale result cannot settle an unrelated generation
   that reused its number.

## Remaining obligations

**PR 4:** the blueprint, templates and the `reg` slot; the two gate constants;
**one `dispatch` per committed issuance in every handler path, with route
tests for a repeated POST, a retry and two workers; no settlement after
`NotSendable`; settlement with `send_id` and `issuance_id`**;
the transactions and order above; the first read of `email_verified_at` with
the `_USER_COLUMNS` and CLI schema-check widening; `registrations-cleanup
[--check]`, `registrations-clock-accept` and `clear-registration-limit`;
production wiring of the mail settings; a handler for
`learnpilot.registration`; the Nginx zone; the route-level tests.

**PR 5:** the cleanup timer; `restore_gate`, `restore_status`, `release_hold`
and their commands; the restore script; the snapshot scrub; the restore-lock
refusal of the cleanup command. No hold can be set or released by this PR's
code.

**PR 7:** `teacher` in `VERIFIED_REGISTRATION_ROLES` and in the role map,
teacher profile and subjects at activation, quotas.

**Activation prerequisites** are unchanged and none is done (design 0.3).

## Not verified

Everything ran on macOS (Darwin 25.5.0, arm64) with CPython 3.12.15 and SQLite
3.53.4. Production's SQLite is older; the service uses upserts
(`ON CONFLICT ... DO UPDATE ... WHERE` for the mark and
`ON CONFLICT(email) DO NOTHING` for the account, SQLite 3.24 or later), reads
`sqlite3.Error.sqlite_errorcode` in tests only (Python 3.11 or later), adds a
column with a CHECK by `ALTER TABLE` (migration 022; accepted because the
default, NULL, satisfies it; exercised here on this SQLite only), and relies on a
row-less `UPDATE` taking the write lock, checked here in rollback-journal and
WAL mode on this build only. No Linux run, no server, no provider, no real
database.
