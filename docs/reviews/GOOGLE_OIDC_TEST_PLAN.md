# Google OIDC sign-in — test plan

Companion to `docs/reviews/GOOGLE_OIDC_DESIGN.md` (revision 2.3 with the
corrections of its review; design 0.3, last table); section numbers refer to
it. **A plan. No test of parts B and C exists yet, and none of the manual
checks has been performed.** The independent review of revision 2.3
approved the technical specification with seven non-blocking notes. A Codex
review of the corrections made for those notes, here and in the design,
returned "approve with non-blocking notes", for the technical documentation
only. The wording corrections made after it (part D here; design 0.3) have
not been independently reviewed. Gabriel has approved the product decisions
P1–P4, T5 and M1–M3 (design 0.2). Implementation and activation remain
unapproved.

Part A is the prerequisite's tests. They exist: PR #332 is merged on `main`
at `008c236`, and this branch does not contain it yet. Parts B and C belong
to PR #331. Part D is manual and is an activation prerequisite.

**Approved decisions (design 0.2), and what a mark means.** P1–P4, T5 and
M1–M3 were pending choices, and Gabriel has approved them. A test that pins
one of them is marked with its number, so that the decision it rests on can
be found. A mark says nothing else: every marked test is written with the
suite, and none waits for a ruling.

**The answers of the cancel route** (design 1.6, 1.9). Four codes, each with
its exact text on the account page:

| Row of 1.6 | Code | Text |
| --- | --- | --- |
| 4 | `link_cancelled` | „Свързването с Google беше отказано.“ |
| 2 | `account_linked` (M2) | „Профилът ти вече е свързан с Google.“ |
| 5 | `link_not_pending` (M1) | „Няма свързване с Google, което тази заявка да прекрати.“ |
| 1 and 3 | `cancel_unconfirmed` (M3) | „Не успяхме да потвърдим отказа.“ |

A cancel test asserts the code of its row, and that the page it leads to
shows that row's text and none of the other three.

## Method

- Temporary databases built from migrations and canonical data
  (`tests/support.py`). `data/questions.db` is never read or copied.
- **A fake Google with real cryptography** (`tests/google_oidc_support.py`):
  it generates an RSA key pair, publishes its JWKS, and mints ID tokens signed
  with RS256 by joserfc. It is mounted as the innermost transport, under the
  bounded reader, so Authlib's real client, the response rules, the real
  signature verification, the real claim checks and the provider gate all run.
  It records every outbound request.
- **No network.** The test base makes any real socket connection an error.
- **"During the network call" is deterministic.** The fake token endpoint
  takes a hook that runs while the application waits (disable the account,
  rotate the key, change the role, complete a competing callback), and can
  block on an event until the test releases it. No test sleeps to produce a
  timeout; the budget is set to a fraction of a second for those tests.
- **Clock and instance.** Tests replace the one clock function,
  `monotonic_seconds`, and the instance value of `web/oauth_state.py` (2.3).
  The wall clock is patched separately, so that a test can move one without
  the other.
- **Races** at the service level use threads with one connection each.
- **Storage failures are real where SQLite can produce them.** A second
  connection holds the write lock past a shortened busy timeout; the table is
  dropped; the connection is made `query_only`; the temporary database file
  is given mode 0400; a trigger in the temporary database fails the insert
  of one chosen key; a reader on a rollback-journal database holds its lock
  while the writer commits. Only a failing `COMMIT` on a WAL database, a
  `COMMIT` that takes effect and then reports an error, and a failing
  `ROLLBACK` need a stand-in: a `sqlite3.Connection` subclass, supplied
  where the request's connection is opened.
- **Which failure fails where.** Two groups, kept apart (design section 9,
  "Ownership"; E19):
  - *At `BEGIN IMMEDIATE`:* a held write lock, and a `query_only`
    connection. No transaction begins. The expected trace holds `BEGIN
    IMMEDIATE` and no `ROLLBACK`, and the clock is not read.
  - *Inside the transaction:* a missing table, the trigger, and **a
    read-only database file**. `BEGIN IMMEDIATE` returns, the clock is read
    once, the failing statement raises, and the expected trace holds
    exactly one `ROLLBACK`. For the read-only file the failing statement is
    the first write: the cleanup `DELETE` of `consume` at the callback and
    when retiring, the clearing of the throttle counter at the
    confirmation.

  Every storage test states which of the two it is. **Where the read-only
  file fails is a fact about the suite's databases, not about SQLite in
  general.** They are rollback-journal files, and the file is read-only by
  its mode, in a writable directory. A WAL database, on another SQLite
  version, can fail at `BEGIN IMMEDIATE` instead (E19), and no test asserts
  a timing for one. Each read-only-file test first asserts that its
  database's journal mode is `delete`, and is skipped when the suite runs
  as root, which file modes do not bind, as the existing permission test of
  `tests/test_production_deployment.py` is.
- **Whether retirement took the connection** is read from a spy on the
  accessor of the request's connection. The tests do not say "no connection
  was opened" of a whole signed-in request: the account loader opens one for
  every such request, with or without Google sign-in.
- **What a function executed** is read from `Connection.set_trace_callback`
  on the request's connection: the statements, in order, and nothing else.
- The suite needs an interpreter with Authlib, joserfc and requests
  installed; the development machine's default interpreters lack them.
- **Python.** The suite runs on Python 3.12, the supported verification
  workflow of PR #332 and what production runs. Outcomes that rest on
  `AlreadyConsumed` (a replayed callback answering `failed`,
  `already_linked`, a retired object reported `consumed`) need 3.11 or newer
  (design section 9). One test per call site stands in for an older
  interpreter by having `consume` raise a plain `sqlite3.IntegrityError`,
  and expects `unavailable` or `not_recorded`: never an acceptance and never
  a retirement.

## A. Prerequisite: consumption storage (PR #332, merged)

The tests of the prerequisite exist on `main` and are not repeated here:
`tests/test_oidc_consumed_transactions_migration_sql.py` and
`tests/test_oidc_consumption_service.py`, with the migration-level and
classification assertions in `tests/test_build_database.py`,
`tests/test_canonical_content.py` and `tests/test_schema_baseline.py`. They
cover the migration (the byte-length condition and the affinity cases
included), the classification and build, and the service: fixed purposes,
expiry before cleanup, cleanup by expiry only, only the primary-key duplicate
as `AlreadyConsumed`, the cross-instance regression, and the caller-owned
transaction. PR #331 changes none of them except the one named in part C,
and all of them must keep passing: it calls the storage and changes nothing
in it.

## B. PR #331: new test modules

### B1. Transaction and pending link (`tests/test_oauth_state.py`)

- the transaction holds exactly the keys of 2.1, the pending link exactly
  those of 2.2; neither holds a secret, a code or a token;
- state, nonce, verifier and pending-link id differ on every creation and
  have the stated lengths;
- lifetimes per object (600, 300, 180) on the monotonic clock; acceptance at
  `exp - 1`, refusal at `exp`;
- **the wall clock does not matter:** moving it a day forward or back between
  start and callback changes no outcome; moving the monotonic clock does;
- `monotonic_seconds` returns `int(time.monotonic())` and the module has no
  platform branch and names no other clock (a static check);
- **the instance:** a transaction or pending link of another instance is
  refused, its slot removed, and no record written;
- every row of 2.5;
- a second start replaces the first; the first tab's callback changes nothing
  and the second flow still completes;
- the cookie budget: an oversized session is refused and keeps neither a
  transaction nor a pending link; the pending link at every field's bound
  fits;
- the cookie that carries either object is `Lax; HttpOnly; Secure` with no
  expiry, like every other;
- **lock, then clock (section 9, obligation 1).** The replaced clock
  function, each time it is called, tries to take the write lock from a second
  connection without waiting and notes whether it was refused. For every
  reading that reaches `consume`, at the callback, at the confirmation and
  when retiring, it was refused: the caller already held the lock. Among the
  three call sites only `begin_write` calls the clock (a static check). When
  retiring finds a transaction of its caller, or its `BEGIN IMMEDIATE`
  fails, the clock is not read at all. This is asserted on a writable
  database. On a read-only file a `BEGIN IMMEDIATE` that returned holds no
  write lock, the second connection is not refused, and no reading there
  can lead to an acceptance (design section 9, "Ownership"); the
  read-only-file tests below do not make this assertion;
- **the expiry and the key are the object's:** a spy on `consume` sees, at
  each of the three call sites, `expires_at` equal to the `exp` of the session
  object, and the key that `consumption_key` gives for the right purpose and
  that object's `state` or `id`;
- **well-formed means usable by the storage:** an `exp` that is a boolean, a
  float, a string, negative or 2^62 makes the object malformed (2.1), so it
  never reaches `consume` as a `ValueError`;
- **every replacement of a session tries to retire first:**
  `issue_account_session`, `issue_pending_second_factor` and `end_session`
  each call `retire_linking` before they clear the session; none of them
  reads its result (a static check on the three function bodies); and no
  module other than `web/account_session.py` calls `session.clear()` (a
  static check);
- no module of PR #331 contains a statement that deletes from
  `oidc_consumed_transactions` (a static check; section 9, obligation 8).

**Retirement: outcomes (4.4).** Each case is run for a `link` transaction
(linking in flight), for a pending link, and for a session built to hold
both.

- `none`: an empty slot; a `signin` transaction, which leaves with its slot
  and is not recorded;
- `dead` without the database: a malformed object, and one of another
  instance. Retirement does not take the request's connection, and no row
  is written;
- `dead` by `Expired`: the clock at `exp` and after it. No row is written;
  at `exp - 1` the object is `invalidated`;
- **`consumed`, not `dead`, for `AlreadyConsumed`:** the key is already
  recorded. The outcome is `consumed` whichever earlier use wrote the row:
  the callback's `consume`, the confirmation's, or an earlier retirement.
  The stored row is unchanged, and the retirement adds none;
- **`dead` does not prove that an object was unused:** a transaction whose
  key was recorded before its `exp` and that is retired at `exp` or later is
  `dead`, and the trace shows that the storage executed no statement for it;
- `invalidated`: read from a second connection after the call, the table
  holds the key that `consumption_key` gives for the object's purpose and
  value, with the object's own `exp`;
- **`invalidated` proves that the object was unused:** with the key recorded
  at any earlier value of the clock, a retirement before `exp` never returns
  `invalidated` for it;
- the derived values, over every combination of two outcomes: `complete` is
  true exactly when no outcome is `not_recorded`; `all_invalidated` is true
  exactly when at least one outcome is `invalidated` and the other is
  `invalidated` or `none`. An empty session is `complete` and not
  `all_invalidated`; so is a session whose only object is `consumed`, and
  one whose only object is `dead`;
- **both objects in one transaction:** the trace shows one `BEGIN IMMEDIATE`
  and one `COMMIT` around both inserts, the link transaction's before the
  pending link's. Mixed results occur in both orders: `dead` beside
  `invalidated`, and `consumed` beside `invalidated`;
- **the slots go in every case:** after each case of this block and of the
  two blocks below, the session holds neither slot, and every other key it
  held is unchanged. The one exception is the `SystemExit` case, in which
  the request does not return;
- **nothing to do takes no connection:** with no live object, and with the
  provider disabled, retirement does not take the request's connection.
  With the provider disabled the slots are not read and the result is
  `none` for both.

**Retirement: a transaction of the caller is open (4.4 step 3).** The
request's connection holds a pending write of the caller (a throttle failure
recorded and not committed) when `retire_linking` is called with a live
object.

- every live object is `not_recorded`; `complete` is false;
- the trace shows that retirement executed **no statement**: no `BEGIN`, no
  `DELETE`, no `INSERT`, no `COMMIT`, no `ROLLBACK`;
- `conn.in_transaction` is still true, and no consumption row exists;
- **the caller's write is preserved:** committed by the caller afterwards, it
  is in the database; rolled back by the caller afterwards, it is not.
  Neither leaves a consumption row. This is the regression test for
  revision 2.1's rule, under which the same situation rolled the caller's
  write back;
- the log line gives `caller_transaction` as its reason;
- **the call sites keep the precondition:** a spy on `retire_linking` at the
  password sign-in, the admin's password step, the completed second factor,
  the password change, sign-out, the Google sign-in, the cancel route and
  the link start asserts that the request's connection, if it is open, is
  not in a transaction at the call. Each is run with a live pending link in
  the session, so that the path which opens the database is the one taken.

**Retirement: storage failures (4.4 step 7).** Each for a live transaction,
for a live pending link, and for both.

- **`BEGIN IMMEDIATE` fails: the write lock is held elsewhere; the
  connection is `query_only`.** In each, `not_recorded`. Retirement never
  owned a transaction, so the trace holds `BEGIN IMMEDIATE` and **no
  `ROLLBACK`** and no `COMMIT`; the connection is not in a transaction
  afterwards; the clock was not read;
- **the database file is read-only** (mode 0400, rollback-journal; Method):
  `not_recorded`. `BEGIN IMMEDIATE` **returns**, so retirement owns a
  transaction, and the clock was read once. The first write, the cleanup
  `DELETE` of `consume`, raises. The trace holds `BEGIN IMMEDIATE`, that
  `DELETE`, **exactly one `ROLLBACK`** and no `COMMIT`; the connection is
  not in a transaction afterwards; no row was written;
- **the table is missing** (the failure is inside the transaction):
  `not_recorded`; the transaction that retirement began is rolled back, the
  trace holds exactly one `ROLLBACK`, and nothing is left pending on the
  connection;
- **a failure between two records:** a trigger fails the insert of the
  pending link's key after the transaction's `consume` has returned. Both
  are `not_recorded`, the table has no row, and a later callback with a copy
  of the start cookie is accepted: the first record did not survive;
- the same trigger with the transaction's key already recorded: `consumed`
  and `not_recorded`, and the earlier row is still there;
- **a `COMMIT` that fails:** once through the connection subclass, and once
  for real, with a reader holding its lock on a rollback-journal database.
  Every object whose record was pending is `not_recorded`, the table has no
  row, and the connection has no transaction afterwards;
- **a rollback that fails as well:** the result is unchanged, nothing is
  raised, and the second failure is logged by its class name. Retirement
  executes nothing further on the connection, and after the request the
  database holds no row of it: the open transaction went with the
  connection;
- an exception that is not a `sqlite3.Error` (`record_use` made to raise
  `RuntimeError`): `not_recorded`, rolled back, not raised to the caller;
- `SystemExit` raised inside: the transaction is rolled back and the
  `SystemExit` propagates;
- **an older Python:** `consume` made to raise a plain
  `sqlite3.IntegrityError` for a key that is recorded: `not_recorded`, not
  `consumed`;
- **never a false success.** In every case of this block and of the previous
  one: no outcome is `invalidated`; `complete` and `all_invalidated` are
  false; and one line on `learnpilot.oidc` holds `retire_not_recorded`, the
  object names and a reason that is `caller_transaction` or a class name. It
  holds none of the state, the pending-link id, the key and the exception's
  text.

### B2. Provider service (`tests/test_google_oidc_service.py`)

Each token case uses a real signature unless the case is about its absence.

- **accepted:** both issuer spellings; either of two published keys; expiry
  inside the leeway; `azp` absent or equal to the client id;
- **signature refused:** `alg: none`; HS256 keyed with the public key; another
  key under a published `kid`; unknown `kid`; no `kid`; altered payload;
  truncated signature; not a JWT; a payload that is not a JSON object;
- **issuer refused:** trailing slash, upper case, `http`, look-alike host,
  missing, not a string;
- **audience refused:** another client with our `azp`; ours plus another; a
  one-element list of ours; a number; an object; ours with a trailing space;
  another `azp`; `azp` as a list, a number or an empty string;
- **times refused (5.4 step 3):** `exp` or `iat` as `Infinity`, `-Infinity`,
  `NaN`, `1e400`, a float, a boolean, a numeric string, zero, negative or
  2^70; `exp` missing; `iat` missing; `iat` in the future; `iat` older than
  ten minutes; `iat` later than `exp`; expired;
- **nonce refused:** another transaction's; missing; not a string;
- **subject refused:** missing, empty, a number, 256 characters, with a space
  or a control character; and in each case no lookup and no row;
- **metadata never fails a token:** `email` a number, `name` a list,
  `email_verified` a string;
- **provider responses (5.3), one test per row:** 200 without `id_token`; a
  JSON array; a body that is not JSON; a body over 64 KiB; an `id_token` over
  16 KiB; 302 with and without a JSON body, and the `Location` is never
  requested; 429; 503; 400 `invalid_grant`; 401 `invalid_client` (logged at
  ERROR without the description); connection error; TLS error;
- **one request, no retry:** exactly one request reaches the token endpoint
  for each case above, a dropped connection included; the code is never sent
  twice; every request carries `allow_redirects=False` and timeouts no larger
  than the time remaining; the HTTP session ignores proxy variables;
- **keys (5.6):** cached within `max-age`; fetched once for a new `kid`; at
  most one fetch per callback; a stale set used when a fetch fails;
  no set at all ends `unavailable`; oversized, non-JSON, empty and non-RSA key
  sets refused; a key under 2048 bits ignored; a fetched set is stored only
  when the result arrived in time;
- no URL is ever taken from a token or a response.

**The budget and the gate (5.7)**

- a token endpoint that blocks: the call returns `unavailable` at the budget;
  the worker thread is still alive; the slot is still held;
- releasing the blocked endpoint afterwards changes nothing: the key cache,
  the cooldown as left by the overrun, the database and the session are what
  they were (the late result is discarded);
- **what a worker thread touches:** the key cache and the cooldown are
  wrapped so that they record the thread of every access. Over a call that
  arrives in time and one that overruns, no access comes from the worker
  thread; its only writes are its own task's result and the slot count;
- **worker exceptions:** a worker function that raises a `RuntimeError` whose
  text holds a token, a JSON error on a token response, and `SystemExit` each
  give a failure that carries the class name only; nothing is written to
  stderr or to any logger, `threading.excepthook` is not called, the exception
  object is not kept, and the slot is free afterwards;
- **the provider lock:** the slot count, the key cache and the cooldown are
  wrapped to assert that the lock is held at every read and write; the lock
  is not held during the network call (a second call takes a slot while the
  first is blocked);
- 200 calls from 8 threads never take the slot count below zero and leave
  both slots free;
- the worker function runs without an application context: touching
  `flask.session`, `g`, `request` or `current_app` inside it raises; it
  receives no connection (pinned by signature and by a static check that the
  module imports neither Flask nor `sqlite3`);
- two blocked calls hold both slots; the third answers `unavailable` without
  starting a thread and without a request; after the release both slots are
  free and a call succeeds;
- fifty overruns in a row, with no start failing, never raise the process's
  thread count above its starting value plus two;
- **the bound is on workers inside the function (5.7):** the network
  function is wrapped to count the workers that are inside it. The count
  never exceeds two in any test of this module. No test asserts that at
  most two thread objects exist. One test shows the opposite on purpose. A
  first task's start raises after its thread exists, and a hook holds that
  thread before its first action; the request thread gives the slot back.
  Two further tasks then take both slots and block inside the function.
  Three worker threads exist, two of them inside the function. Released,
  the first thread finds its task abandoned, never enters the function, and
  its own release changes nothing;
- the budget covers the key fetch: an exchange that leaves less than two
  seconds starts no fetch;
- a slow name lookup (resolver patched to block) ends `unavailable` at the
  budget;
- a body delivered slowly is abandoned by the reader at the deadline;
- **a start that raises, no thread:** `Thread.start` made to raise without
  creating a thread. The callback answers `unavailable`; no request reached
  the provider; both slots are free afterwards and the next call succeeds;
  one line is logged with the class name. Ten such failures in a row leave
  the slot count at two;
- **an exception between the slot and `start()` (5.7, the guarded
  region):** the construction of the thread object is made to raise after
  the slot was taken. The same result as for a start that raises with no
  thread: `unavailable`, no request, no thread started, both slots free
  afterwards, one line with the class name, and the next call succeeds. A
  `SystemExit` raised there is raised again, after the slot was given back.
  A static check finds no statement between the taking of the slot and the
  `try` that guards the region;
- **a start that raises after the thread exists** (a `Thread` subclass whose
  `start` runs the real one and then raises), in both orders that a hook in
  the worker's first action produces:
  - the worker had not begun: the request thread gives the slot back; the
    worker finds the task abandoned, makes no request, and its own release
    changes nothing;
  - the worker had begun: the request thread does **not** give the slot
    back; the slot is held until the worker's function returns, and a
    further call meanwhile is refused for lack of a slot;

  in both, the callback answers `unavailable`, the worker's result is
  discarded, and once the worker has ended the count of free slots is
  **exactly two, never three**;
- **exactly once:** the release function called twice for one task changes
  the count once. The free count is wrapped to record its highest value; it
  never exceeds two in any test of this module, including 200 calls from 8
  threads with the three kinds of start mixed;
- a `SystemExit` raised by `start` after the thread exists is raised again,
  and the slot count returns to two;
- **the worker's release is in a `finally` from its first statement
  (5.7):** a hook makes the worker's first action raise, once before and
  once after it marked that it had begun, and makes its second action
  raise. In each case the slot is given back once and is free when the
  thread has ended; `threading.excepthook` is not called; no result is
  handed over; and the request answers `unavailable` at its deadline, which
  counts as an overrun. Combined with a start that raised, in both orders
  of the hook, the count of free slots ends at exactly two;
- **the failure count:** before each start-failure case, before the
  exception between the slot and `start()`, and before a refusal for lack
  of a slot, the count is set to 0, 1 and 2 in turn. It is the same
  afterwards: neither increased nor reset to zero.

**The cooldown (5.8)**

- three consecutive failures start it; two failures and a provider answer do
  not; one overrun starts it;
- during it: `start` and `link` create no transaction; a callback is matched
  and recorded as used, makes no request, and answers `unavailable`;
- after 60 seconds calls are made as before. The first one is not special: two
  callbacks may both go out, a failure counts as one, and it takes three
  consecutive failures or one overrun to cool down again;
- **what leaves the count as it was:** a refusal for lack of a slot, and an
  exception between the slot and the start, a start that raised included.
  With two failures counted, either one of them followed by a third failure
  starts the cooldown (the count was not reset), and either one alone does
  not (it was not counted);
- what resets it: a provider answer, a token that then fails validation, a
  success, and the start of a cooldown.

### B3. Accounts service (`tests/test_oidc_accounts_service.py`)

**Sign-in and onboarding**

- a new identity creates exactly one student and one identity; a failure
  between the two inserts leaves neither;
- 8 connections on one new identity: one user, one identity, none orphaned;
- **the identity is resolved first:** an existing identity signs its user in
  whatever email the token carries, including one that collides with another
  account;
- a disabled account's identity is refused and creates no second account; so
  is an admin's, including a teacher promoted after linking;
- **email table of 6.3, every row,** each with `email_verified` true, false,
  absent and a string: a comparable email that equals an existing
  `users.email` creates nothing, links nothing and signs nobody in, verified
  or not; a non-comparable one is omitted, creates the account with
  `email_at_link` `NULL`, and is never written to `users.email`;
- the collision compares normalized forms (case, surrounding whitespace, NFKC);
- `users.email` of a created account is `NULL`; no password hash exists; the
  role is `student` and no code path writes another role;
- names: 81 and 5000 characters, control and bidirectional characters, a
  formula prefix, HTML;
- (P4) a name that is absent, not a string, empty, or shorter than two
  characters after cleaning gives the display name „Ученик“ (6.4 step 4).

**`confirm_link`**

- the happy path stores one identity with `email_at_link` as reported and
  writes the confirmation record in the same commit;
- each refusal of 4.3 steps 1–5, with the outcome it names. For a refusal at
  steps 2–5 no identity is written and the record is committed. At step 1
  neither refusal stores a record or an identity: after `Expired` the
  service has executed no statement, and after `AlreadyConsumed` only the
  storage's cleanup of expired rows is pending for the caller. Whatever the
  caller wrote earlier in the transaction, the cleared throttle counter
  included, is still pending and is committed by the caller;
- **the account, read again (4.2):** `bound_account` refuses a missing row,
  an inactive account, an admin, a rotated session key and a session past
  its absolute lifetime, and passes otherwise. One parametrised set of cases
  runs through the callback after the exchange and through steps 2–4 of
  `confirm_link`, and both give the same answer for every case;
- **identity first, in both places (4.2):** one parametrised set of cases
  runs through `link_decision` at the callback and inside `confirm_link`. An
  identity already on this account answers `already_linked`, and one on
  another account `identity_taken`, whatever the account's password state and
  whatever email the token carries; only then `not_allowed`; only then (P1)
  `email_other_account`. Both places give the same outcome for every case;
- an exception between the record and the identity leaves neither;
- 8 simultaneous confirmations of one pending link: one `linked`, seven
  `already_linked`, one identity, one record;
- two accounts confirming one Google identity at once: one `linked`, one
  `identity_taken`, nothing reassigned;
- a first sign-in against a confirmation of the same new identity: both
  orders are consistent;
- (P1) the email of another account refuses the link; the linking account's
  own email does not; a non-comparable email does not.

### B4. Routes (`tests/test_google_oidc_routes.py`)

- start: same-origin POST only; the authorization URL has exactly the
  parameters of 1.3 and the configured redirect URI; a signed-in account
  starts nothing;
- the redirect URI ignores a forged `Host` and `X-Forwarded-Host`;
- callback outcomes: first sign-in, returning sign-in, cancellation, provider
  error, missing and oversized `code`, wrong `iss` parameter;
- **methods on the callback address (1.2):** `HEAD` and `OPTIONS` answer 405
  with `Allow: GET` from the hook, with and without a matching transaction;
  they set no cookie, open no database connection, make no outbound request
  and do not run the view. A same-origin `POST` answers 405 and a cross-origin
  one 403; in both no view resolved and the hook did nothing;
- **a callback without a matching transaction,** from each kind of visitor:
  no cookie, no row in any table, no outbound request, and the stored
  transaction is unchanged;
- **order of 1.5:** a matched callback with `error=…`, with a malformed
  `code`, and with a wrong `iss` each leave a consumption record and make no
  outbound request;
- after a sign-in the session is exactly `uid`, `sk`, `iat` plus carried
  attempt markers; the absolute lifetime and key rotation behave as for a
  password session;
- return target: stored only when safe; re-validated when used; external,
  protocol-relative, backslash and control-character targets fall back by
  role;
- admin: cannot start a link (403); cannot confirm one (403); an admin
  identity cannot sign in; a pending second factor is not bypassed;
- link start: asks for no password; an account without a password or with a
  Google identity is refused; an existing pending link is retired. With the
  storage failing during that retirement the route still answers `303` to
  Google with a new transaction, the earlier slots are gone, and one line is
  logged;
- **a link callback writes no identity:** after it the database has no new
  row except the consumption record, and the session holds a pending link
  with exactly the keys of 2.2;
- **a link callback reads the account again after the exchange (1.5 V7):**
  with the account disabled, its key rotated or its role changed through the
  hook during the exchange, the answer is `failed`, no pending link is in
  the session, and the database has no new row except the callback's record;
- confirmation page: names both profiles; escapes a hostile Google name and
  email; answers `expired` without a pending link and changes no cookie;
- confirm POST: wrong password keeps the pending link and counts one failure
  on the account's counter; the counter is shared with the sign-in and the
  password forms; a blocked counter answers 429; the right password links,
  clears the counter and removes the pending link; an over-long password
  counts as wrong;
- **confirm POST, rolled back (1.6):** a failure forced inside the
  transaction leaves no record, no identity and the counter as it was. **The
  pending link is still in the session.** The answer is
  `/account?google=unavailable` for a database error and `failed` for
  another one; the account page still offers the confirmation; and a second
  POST with the right password, once the fault is gone, links. A failure of
  `BEGIN IMMEDIATE` itself (a held lock; a `query_only` connection) issues
  neither a `COMMIT` nor a `ROLLBACK`. A read-only database file fails
  later, at the first write, which here is the clearing of the counter: the
  route owns its transaction by then and rolls it back once (Method);
- **confirm POST, a `COMMIT` that fails (1.6; section 9):** through the
  connection subclass, and for real with a reader holding its lock on a
  rollback-journal database. The answer is `/account?google=unavailable`;
  the trace holds one `ROLLBACK` after the failed `COMMIT` and no second
  `COMMIT`; the database has no identity and no record, and the counter is
  as it was; the pending link is still in the session; a second POST links;
- **confirm POST, a rollback that fails as well:** the same answer; nothing
  is raised; the failure is logged by class name; the route executes nothing
  further on the connection; after the request the database has no identity
  and no record;
- **confirm POST, a `COMMIT` that took effect and still reported an error**
  (the stand-in commits, then raises): the response is `unavailable`, never
  `linked`, and it does not say that nothing was linked either. The pending
  link is still in the session. The account page then lists the identity,
  read from the database, and a second POST of the same pending link answers
  `already_linked`. This pins that no response claims what a failed `COMMIT`
  left on the disk;
- **cancel, a clean cancellation (1.6 row 4):** a live pending link, no
  Google identity on the account. The record is written, both slots are
  removed, the answer is `/account?google=link_cancelled`, and a replay of
  the earlier cookie is refused. The same for a live link transaction;
- **cancel never claims more than it established (1.6; 3.3 rows 19–22).**
  In each case below the response is a redirect to `/account?google=<code>`
  with the code named, both slots are gone from the response cookie, the
  code is **not** `link_cancelled`, and the page it leads to shows the text
  of its code and does not contain „Свързването с Google беше отказано.“:
  - *(a) two tabs:* a confirm POST with the right password, then a cancel
    POST from the same client, whose session now holds no slot. Retirement
    takes no connection and returns `none` for both. The identity row is
    unchanged. The answer is `account_linked` (M2);
  - *(b) a stale pending link:* the cookie from before the confirmation is
    restored, so that the cancel request still carries the confirmed pending
    link. The outcome is `consumed`; no row is added to the consumption
    table and the identity row is unchanged. The answer is `account_linked`
    (M2). The same with the two requests sent at once from two clients:
    whichever order the database gives them, the pair never ends as
    "linked, and told cancelled";
  - *(b′)* the same after a confirmation that recorded the use and stored
    nothing (each refusal of 4.3 steps 2–5): `consumed`, and the answer is
    `link_not_pending` (M1);
  - *(c) a copy of the start cookie reached the callback first:* B5 has the
    full sequence. Here: the original session still holds the transaction,
    its key is recorded, and the cancel answers `link_not_pending` (M1);
  - *(d) the account cannot be read:* the identity read is made to raise,
    once for a retirement that invalidated a live pending link and once for
    an empty session. The answer is `cancel_unconfirmed` (M3) in both, and
    one line is logged with the class name;
  - an empty session and no Google identity on the account:
    `link_not_pending` (M1). A second cancel POST after any first one
    answers `link_not_pending` or `account_linked`, never `link_cancelled`;
  - a pending link that is malformed, of another instance, or expired
    (`dead`): `link_not_pending` (M1);
  - another session of the same account has linked meanwhile, and this
    session cancels its own live pending link: the object is `invalidated`,
    its record is written, and the answer is `account_linked` (M2), because
    row 2 comes before row 4;
- **cancel, not recorded (1.6 row 3):** for each storage failure of B1, with
  no Google identity on the account, the answer is a 302 to
  `/account?google=cancel_unconfirmed` (M3). It is **never**
  `link_cancelled`, and it is not `unavailable`. The page it leads to shows
  „Не успяхме да потвърдим отказа.“ It contains neither „Свързването с
  Google беше отказано.“ nor the sign-in text of `unavailable` nor the
  notice of an unfinished link. The response cookie holds neither slot. One
  line is logged. With a Google identity on the account the answer is
  `account_linked` (M2), and the line is logged all the same;
- **the order of the rows of 1.6:** one parametrised test runs every
  combination of the two outcomes with the account read giving "linked",
  "not linked" and an error, and compares the code with the table:
  `link_cancelled` in exactly the combinations of row 4, and
  `cancel_unconfirmed`, `account_linked` or `link_not_pending` in every
  other, by the first row that applies. The account is read after the
  retirement has returned (a spy on both);
- **the exact texts of a cancellation (1.9):**
  `/account?google=link_cancelled` shows „Свързването с Google беше
  отказано.“; (M1) `/account?google=link_not_pending` shows „Няма свързване
  с Google, което тази заявка да прекрати.“; (M2)
  `/account?google=account_linked` shows „Профилът ти вече е свързан с
  Google.“ and the identity below it; (M3)
  `/account?google=cancel_unconfirmed` shows „Не успяхме да потвърдим
  отказа.“ Each page shows its own text and none of the other three;
- **sign-out, recorded:** retires linking in progress; lands on the home
  page as today, for every account (P3). With the provider disabled it
  behaves exactly as today, and retirement does not take the request's
  connection. The request's other database work, the account loader's
  read, is what it is today;
- **sign-out, a new link and a change of account with a `consumed` or
  `dead` object:** each goes on exactly as with an empty session, shows
  nothing and logs nothing about it;
- **sign-out, not recorded:** for each storage failure of B1, the response
  is the same redirect to the home page, with no message and no error page.
  The session holds none of `uid`, `sk`, `iat` and neither slot. Apart from
  one log line, nothing differs from a sign-out without linking;
- **a change of account, not recorded:** a password sign-in as another
  account, an admin's password step, a completed second factor, a password
  change and a Google sign-in, each with linking in progress in the session
  (for the Google sign-in: a pending link left in a session whose account no
  longer loads) and each storage failure of B1. The sign-in or the change
  completes with
  the response it has without the failure; the session is replaced and holds
  neither slot; what the route committed before (the login stamp, the
  cleared counter, the new password and its new session key) is in the
  database;
- with the provider enabled, `GET /login?google=<code>` from a signed-in
  account lands on `/account?google=<code>`; an unknown code is dropped;
- the callback never returns a page: every outcome, a raised exception
  included, is a redirect with `Cache-Control: no-store`;
- disabled provider: no button, no form, 404 on all five routes, 403 for a
  cross-origin POST; a transaction or pending link in the cookie is ignored
  and nothing is retired;
- **disabled provider, no Google message:** `/login?google=unavailable`, the
  address Nginx redirects to, renders the sign-in page byte for byte as
  without the parameter; so does `/account` with any code; a signed-in
  `GET /login?google=unavailable` redirects as today, without the parameter;
- **no secret in any log:** a whole flow, with failures, a timeout, a link,
  a retirement that is not recorded, a cancellation whose account read
  fails, and a confirmation whose `COMMIT` and rollback both fail, is run
  with every logger at DEBUG,
  and the captured output contains none of the code, state, nonce, verifier,
  pending-link id, consumption key, ID token, access token, client secret,
  subject or email;
- a hostile display name is escaped in pages, neutralized in the CSV export
  and slugged in note filenames.

### B5. Replay and concurrency (`tests/test_google_oidc_replay.py`)

One test per row of 3.3, and:

- **restored start cookie, same code:** refused; the fake provider received
  one request in total;
- **restored start cookie, fresh code:** refused; no second user, no second
  session, no second request;
- **simultaneous callbacks** on one start cookie (threads, separate clients):
  one proceeds;
- a restored start cookie after expiry, and after an instance change, is
  refused;
- the record persists when what follows fails: a provider failure, a
  validation failure, an exception in onboarding, each followed by a replay
  that is refused;
- a storage error at consumption (table dropped, connection made
  `query_only`, the database file made read-only, a commit that fails) ends
  `unavailable`, makes no outbound request and issues no session. The
  `query_only` case fails at `BEGIN IMMEDIATE`, and the hook issues no
  rollback for it. The other three fail inside the hook's transaction and
  are rolled back once; for the read-only file the failing statement is the
  cleanup `DELETE` of `consume` (Method). A rollback that fails as well
  changes nothing and is logged by class name;
- a `COMMIT` at the callback that took effect and still reported an error:
  the callback answers `unavailable` and makes no outbound request, and a
  replay of the start cookie is refused as `failed`. The transaction was
  spent, and the first answer had not claimed otherwise;
- a repeated key reported as a plain `sqlite3.IntegrityError`, as an older
  Python reports it, ends `unavailable` at the callback and at the
  confirmation: refused, by another branch;
- an expired start cookie leaves no record and is refused again at every
  later value of the clock;
- **two instances on one database** (two application objects with different
  instance values): a transaction of the first is used; the second completes
  a different sign-in at a later clock value; the first transaction is
  replayed to the first application and is refused, and its record is still
  in the table;
- **two instances and a retirement (3.7): a stated limit, characterised.** A
  pending link issued by the first application is cancelled at the second.
  The outcome is `dead`; no record is written and no line is logged; the
  answer is `link_not_pending` (M1), not `link_cancelled`. The first
  application then still confirms that pending link with the right password
  before its `exp`. The test documents what two instance values on one
  database do. It is not a control, and the deployment test pins
  `preload_app` (B6);
- link: the key rotates during the code exchange (password change, disable,
  role change, `sign-out`, each through the hook) → `failed` and no pending
  link (the second validation of 4.2);
- link: the key rotates between the callback and the confirmation → nothing
  is linked, also from a copy of the cookie;
- link, retirement recorded: sign-out, cancel, or a new link start, then a
  copy of the cookie with the pending link → refused;
- link, retirement recorded: sign-out while the browser is at Google, then a
  copy of the start cookie with the callback → refused, and no pending link
  is created;
- link, retirement recorded: another account signs in on the browser (the
  password form; an admin's password step), then a copy of the earlier
  cookie → refused, with the first account's password too (3.3 row 16);

**A captured cookie after a retirement that was not recorded (3.3 row 18;
4.4, "What may be claimed").** The session's cookie is copied; then a
cancellation, a sign-out, a sign-in as another account, and a new link start
are each run with the storage failing during the retirement and working
again afterwards. The tests pin the residual as the design states it, no
smaller and no larger.

- *The pending link, in the copy:*
  - with the bound account's right password, before `exp`, on the unchanged
    session key: **it is linked.** The Google identity of that pending link
    is stored on the bound account. This is the residual, and the test
    exists so that the documentation cannot understate it;
  - with a wrong password: refused; one failure is counted on the bound
    account's counter; a blocked counter answers 429;
  - after the bound account's session key rotated (a password change, an
    operator `sign-out`, a disable, a role change): refused. At the route the
    copy's session no longer loads; at the service `confirm_link` answers
    `failed` at step 3;
  - with the bound session past its absolute lifetime: refused;
  - at `exp` and after it: `expired`; at `exp - 1`: accepted;
  - on another instance: refused.
- *The link transaction, in a copy of the start cookie:* its callback is
  accepted once and creates a pending link in that response; the database
  has no identity row; the identity is stored only by a confirm POST with
  the right password; a second callback from the same copy is refused by the
  callback's own record.
- *The browser itself:* after each of the four events its own cookie holds
  neither slot, whatever the copy can still do. The test asserts that the
  copy is unaffected by the response that removed the slots: deleting them
  revokes nothing.
- *What the person was told:* the cancellation answered
  `cancel_unconfirmed` (M3), not `link_cancelled`; the other three answered
  exactly as without the failure.
- *The same copies after a retirement that was recorded:* refused in every
  case above, with the right password too.
- *A second cancellation:* after the cancellation that was not recorded, the
  storage works again and the same browser cancels once more. Its session
  holds nothing; the answer is `link_not_pending` (M1), not
  `link_cancelled`; and the copy of the pending link is still confirmed
  with the right password.

**What a cancellation can know: a successor that the session never held
(3.3 rows 19–22; 4.4, "What may be claimed").** Rows 19, 20 and 22 are the
route tests (a), (b) and (d) of B4, run here once more from separate
clients. Row 21 is the sequence below.

- *The sequence (c).* A link is started. The cookie is copied to a second
  client. The second client makes the callback with a Google authentication
  of its own: it is accepted, and its response holds a pending link that the
  first client never sees. The first client still holds the transaction.
- *The original then cancels, signs out, starts another link, or signs in as
  another account.* In each: the transaction's outcome is `consumed`; the
  first client's cookie holds neither slot afterwards; the cancellation
  answers `link_not_pending` (M1) and **never `link_cancelled`**; the other
  three answer exactly as without the copy.
- *The successor is not invalidated.* No consumption row exists for the
  successor's `id` after any of the four events. The retirement of the
  transaction it came from wrote nothing for it and could not have.
- *The successor is still held by every check it had.* From the second
  client, after each of the four events:
  - with the bound account's right password, before the successor's own
    `exp`, on the unchanged session key: **it is linked.** This is the
    stated residual, pinned so that the documentation cannot understate it;
  - with a wrong password: refused; one failure is counted on the bound
    account's counter; a blocked counter answers 429;
  - after the bound account's session key rotated (a password change, an
    operator `sign-out`, a disable, a role change): refused, at the route
    because the copy's session no longer loads, and at the service at step
    3 of `confirm_link`;
  - with the bound session past its absolute lifetime: refused;
  - at the successor's `exp` (180 seconds after the copy's callback) and
    after it: `expired`; at `exp - 1`: accepted;
  - on another instance: refused in the route's first validation, before
    the password is looked at and without a consumption row;
  - with a link decision that refuses (the identity already on another
    account; P1): that outcome, and no identity.
- *After the successor was confirmed,* a cancellation from the first client
  answers `account_linked` (M2).
- *The original's own callback,* arriving after the copy's: refused as
  `failed`; no second pending link is created.
- *The transaction has expired before the original cancels:* its outcome is
  `dead`, the answer is `link_not_pending` (M1), and the successor, whose
  `exp` is later, is still confirmed with the right password.

**Two limits of a cancellation's answer (1.6, "Two limits"): stated limits,
characterised.** Neither test is a control. Each documents an ordering in
which an answer was true of its own response and is overtaken afterwards.

- *A callback of the same browser that answers after the cancellation.* A
  link is started. The callback is made, and while it waits in the code
  exchange, the hook sends a cancel POST from a second client that holds
  the same start cookie. The cancellation finds the transaction `consumed`,
  removes both slots from its own response, and answers `link_not_pending`
  (M1), not `link_cancelled`. The callback then completes, and **its**
  response holds a pending link. That pending link is confirmed only with
  the right password, and a cancel POST that carries it answers
  `link_cancelled`. The test pins that the first answer was not a statement
  about a later response: its text speaks of the request, and it stays
  true;
- *Another session of the account links after the read.* Two clients are
  signed in to one account, and each holds a live pending link of its own.
  The first cancels: its object is `invalidated`, the account has no Google
  identity at the read, and the answer is `link_cancelled`. The second then
  confirms with the right password: `linked`. The first client's account
  page lists the Google identity. Nothing is retracted, nothing is written
  for the first client's cancellation after the fact, and a copy of the
  first client's pending link is still refused. In the other order, with
  the confirmation first, the first client's cancellation does not answer
  `link_cancelled` (B4).

**Other**

- a `signin` transaction never links, whatever the session holds; a `link`
  transaction never signs in;
- a code from another transaction is refused by the nonce even when the fake
  provider does not enforce PKCE;
- the callback's record does not stand in for the confirmation's: with the
  callback's record present and the confirmation's removed by the test from
  its temporary database (nothing in the application deletes a row), the
  confirmation still stores at most one identity (the uniqueness rules), and
  with both present a second confirmation stores no identity and no record.

### B6. Configuration and deployment (`tests/test_google_oidc_production.py`)

- **the activation gate (P2):** with the constant off, production starts
  with neither, one, both and malformed values; the provider is disabled; one
  WARNING names the settings and no value, and none is logged when both are
  unset. With the constant on (patched), both valid values enable the
  provider, and exactly one value or a malformed one refuses to start with a
  message that names the setting and no value. A development application
  never refuses to start;
- the redirect URI comes from `DZI_PUBLIC_HOST`; no base URL → disabled;
- Gunicorn: the access-log format has no `%(r)s` and no `%(q)s`; `loglevel`
  is not `debug`; the logger class installs its filter on `gunicorn.error`;
  `preload_app` is true (availability, and the instance statement of 3.7);
- **Gunicorn's real handler (8.4):** `handle_error` is run for every parser
  exception class it answers as a bad request, each built with a code, a
  state and a cookie value, and for an escaping exception and a worker abort
  with a callback URI: no emitted line contains any of the three. The real
  parser is fed a callback request line with a space in its query, one
  without a version, and a header line without a colon, with the same
  result. An ordinary message without a `?` is unchanged;
- **either quotation mark (8.4):** every case of the previous item is
  repeated with the refused text as `str` and as `bytes`, and with an
  apostrophe after the secret, an apostrophe before it, a double quote, and
  both marks. In particular a text that Python quotes with `"`, because it
  contains an apostrophe, leaves nothing of itself in the line, and the
  reason before the first mark is kept. A filter that cut at `'` alone fails
  these cases;
- **tracebacks are not filtered (8.4): a characterisation.** One test gives
  Gunicorn's `InvalidChunkSize`, built with a form body, to the logger with
  the filter installed and asserts what the design says: the message is cut
  and the traceback is not. It documents a limit. It is not a control. The
  whole-flow log test of B4 shows that no exception text of the OIDC modules
  reaches any log;
- **a failing body read answers 400 and logs nothing (8.4, mechanism 3).**
  For `POST /login`, `POST /account/password` and `POST
  /auth/google/link/confirm`, each with what it needs to reach its read of
  the form: a same-origin `Origin`; for the second a signed-in session; for
  the third a signed-in session with a valid pending link. The WSGI
  environment is the one Gunicorn builds, with `wsgi.input_terminated` set,
  and the input is Gunicorn's real body reader over three bodies: a
  malformed chunk-size line that holds the whole form, with a marker as the
  password; a missing chunk terminator; a truncated chunk. In every case:
  - the response is **400**;
  - with every logger at DEBUG and `wsgi.errors` captured, **no line holds
    the marker, and no traceback is logged**;
  - nothing was done with the request: no throttle failure is counted, no
    session is issued, no password is changed, no consumption record and no
    identity is written, and for the third route the pending link is still
    in the session;
- an interrupted body that came with a `Content-Length` raises nothing and
  logs nothing on the three routes: the route answers as for the shorter
  form;
- **the three assumptions of 8.4, pinned as assumptions about logs:** `bind`
  in `deploy/gunicorn.conf.py` is a loopback address; `MAX_CONTENT_LENGTH`
  is a positive integer in `web.app` and in the application that `web.wsgi`
  builds; `worker_class` is `sync` and `threads` is 1, so that Gunicorn runs
  its sync worker and not the threaded one; and a static check finds no
  read of `wsgi.input` and no assignment to a request's
  `max_content_length` in `web/` and `src/`;
- **why the worker class is an assumption (8.4): a characterisation.**
  Gunicorn's real parser, with no application, is asked for a second
  request on a connection whose first request left a malformed chunked body
  unread and did not ask to close the connection. It raises the body
  reader's exception, an `OSError`, with the marker in its text: the read
  that a worker class with keep-alive performs outside Werkzeug's wrapper.
  With `Connection: close` the parser stops and reads nothing. The test
  documents why the pin matters. It is not a control, and no test runs a
  threaded or asynchronous worker;
- **request buffering is not tested as a logging control.** It stays pinned
  by the existing deployment test as the capacity setting it is. No test
  here runs Nginx: its parsing and re-framing of a request body (8.4,
  mechanism 1) is derived from its source and is checked on the server only
  (part D);
- Nginx template: the zones and locations of 8.3; `nodelay` on each;
  the callback location has the args-free `access_log`, `error_log /dev/null
  emerg`, `proxy_intercept_errors on`, the `error_page` line and the three
  headers; `@oidc_leave` returns a 302 and repeats the logging lines; the
  format `lp_no_args` contains neither `$request` nor `$args` nor
  `$request_uri`;
- the env template has both variables, empty and commented.

### B7. Operator CLI (`tests/test_manage_accounts.py`, extended)

- `disable`, `enable`, `sign-out`, `set-role` by `--id`; each rotates the
  session key as by `--email`;
- `--email` and `--id` together, and neither, are argparse errors; a
  non-numeric, zero or negative id is refused; an unknown id is reported;
- every message names the account id; an account without an email prints
  `no email`, never `None`;
- (T5) `set-role --role admin` is refused for an account without a password
  and changes nothing; `--role teacher` for such an account works;
- `list` shows the `password` and `google` columns and no subject;
- commands that keep `--email` only still refuse `--id`.

## C. Existing suites affected by PR #331

| Suite | Change |
| --- | --- |
| `tests/test_access_policy.py` | Six endpoints join the expected matrix. New: the `OAUTH_STATE` rules of 1.2. The "ordinary unsafe routes use same-origin" test stays as it is. |
| `tests/test_session_cookie_samesite.py` | The GET sweep (`test_no_get_changes_state_or_touches_the_cookie`) gains `account.overview`, `oidc.google_link_confirm` and `oidc.google_callback`; the unsafe sweep gains the four POST routes; the cookie-attribute tests gain the start, the callback and the confirmation. |
| `tests/test_account_login.py` | `test_no_registration_reset_or_oauth_route_exists` forbids "callback" in any path: replaced by a test that names the five routes exactly and still forbids registration, reset and verification paths. The account route table gains `/account`. Sign-out tests gain the cases of B4. Tests that patch or import the session-issuing functions follow them to `web/account_session.py`. |
| `tests/test_identities_service.py` | `test_nothing_in_the_application_uses_the_storage_yet` is replaced, as its own comment says, by a test that lists the modules allowed to use the storage. The standard-library-only and SQL-shape tests stay and must keep passing. |
| `tests/test_oidc_consumption_service.py` (merged with PR #332) | `test_nothing_in_the_application_uses_the_storage_yet` becomes the list of allowed importers: `web/oauth_state.py` and `web/services/oidc_accounts.py`. Nothing else in that module, or in the migration's test module, changes. |
| `tests/test_auth_blueprint.py` | The blueprint list and the controller list gain `oidc`; the import-direction tests then cover it. |
| `tests/test_production_deployment.py` | Nginx zones and locations (the `lp_login` count becomes 3), the Gunicorn format and logger class, the env template, the requirement lines. Its existing pins of `bind` (`127.0.0.1:8000`), of `worker_class` (`sync`) with `threads` (1), and of `MAX_CONTENT_LENGTH` stay as they are; B6 names them as the three assumptions of 8.4. `proxy_request_buffering on` stays pinned as a capacity setting and gains no logging meaning. |
| `tests/test_account_ui_state.py`, `tests/test_xss_escaping.py` | The „Профил“ link; provider-supplied names and emails. |
| `tests/test_manage_accounts.py` | Part B7; existing message assertions change to the id-naming form. |
| Lifecycle restore tests | One assertion: a Google sign-in and a link write nothing to `lifecycle_clock` (3.5). |
| Query-budget tests | No per-request query is added. `/account` reads identities, and so does the cancel route, once (1.6 step 3). Sign-in, the password change and sign-out use the database for retiring only when the session holds a live linking object (4.4 step 2). |

## D. Manual checks: activation prerequisites, not performed

They need the recovery milestone's build, a registered client and the
production host. Design 11.2 is the list; 8.7 step 3 is the log check. They
are not automated and none has been carried out. **The pass that corrected
the notes of the review of revision 2.3 ran none of them** and copied none
into `docs/DEPLOYMENT.md`.

What the server checks verify, and what stays derived from source until
then (design 8.4, 11.2 checks 15 and 16):

- **probe N,** the malformed-chunk request through Nginx, tests **Nginx's
  parser**, which parses the framing with request buffering on and off. Its
  four pass criteria as a set are stated for the deployed configuration,
  buffering on (below);
- **probe D,** sent straight to the loopback port after its control
  request, tests **Werkzeug's wrapper** in the versions that are deployed;
- `ss -ltnp` verifies the loopback bind, and Gunicorn's line `Using worker:
  sync` in the journal verifies the worker class;
- the versions of Nginx, Flask, Werkzeug and Gunicorn on the server are
  recorded. The automated tests run against the versions installed where
  the suite runs, which `requirements.txt` does not hold to those of the
  server.

**The two body probes are run exactly as design 8.7 specifies them** ("The
two body probes of step 3"), and that text binds; this is a summary.

- The exact raw HTTP/1.1 bytes, every line ended by CRLF, with `Connection:
  close`; a client timeout of 5 seconds; one run of each, inside the
  deployment window.
- Synthetic markers only, one for each probe: `LPPROBE_NGINX_BODY` and
  `LPPROBE_DIRECT_BODY`. No real email, password, cookie, `state` or other
  credential is in either request.
- They are **not** among the requests that are repeated with a real `state`
  (design 8.7 step 8; 11.2 check 10).
- Probe N passes, **on a server with `proxy_request_buffering on` as
  deployed**, with a 400, Nginx's error-log entry `client sent invalid
  chunked body`, no entry in Gunicorn's access log, and the marker in no
  log. With buffering off Nginx may turn to the application before the
  malformed body arrives, so the absence of an entry in Gunicorn's access
  log is not guaranteed, and the four criteria are no test for such a
  server. That distinction is derived from Nginx's source and is not
  verified on a server.
- Probe D passes with a control request answered 200; a 400 with its
  `POST /login` entry in Gunicorn's access log; no `Invalid request from
  ip=` and no `Error handling request` entry; no traceback; and the marker
  in no log.
- **Any other result is not a pass.** A 403 for probe D is not a pass and
  the probe is not sent again: its cause is diagnosed and corrected before
  a new bounded verification run is scheduled, and that run includes the
  control `GET`.

## E. Experiments

Design 11.3. They established design facts in a scratch environment; they are
not tests of the implementation and are not part of any PR.
