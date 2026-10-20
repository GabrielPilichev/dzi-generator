# Password recovery by emailed code — test plan (registration PR 8)

Companion of `docs/reviews/PASSWORD_RECOVERY_DESIGN.md` (section numbers below
refer to it; "registration design" and "registration test plan" are
`EMAIL_REGISTRATION_DESIGN.md` and `EMAIL_REGISTRATION_TEST_PLAN.md`).
Revision 3 with post-review corrections and Gabriel's decisions recorded; it
follows the design and changes with it, and it authorizes nothing. Revision 2 received a
second independent review of the cumulative proposal (narrow REQUEST CHANGES,
design 0.5). Revision 3, committed as `48ee5bf`, received an independent
review of exactly that SHA: APPROVE WITH NON-BLOCKING NOTES (R1–R8), with
RD1–RD18 left proposed; the corrections for R1–R8 (design 0.6) came after
that SHA and are outside that review, and so is the recording of Gabriel's
selections (design 0.3, 0.7), after which the tests of unselected variants
were removed. No review approved a decision. **Scope 8.1 is now authorized;
its implemented tests and evidence are mapped in `PASSWORD_RECOVERY_8_1_STATUS.md`.
8.2/8.3 tests remain deferred.** Every test pins the selected behaviour: RD2 (a), RD4
(eight digits), RD7 and RD10 as amended for recovery (design 0.1.1), RD8 (a),
RD17 and RD18 (a), with required maintenance isolation. The approved
recovery-specific amendments are tested as the requirement; registration's
own six-digit codes, `not_sent` settlement and throttle use stay pinned by
the registration suites, unchanged. Only the exact wording (RD15) is open:
tests that compare texts use the proposed wording of design 2.7 and 2.8 and
change with it.

Test ids (RA1, RV3, …) are local to this document. Each section names the
implementation scope of design 10 that writes it: **8.1** storage and
operations, **8.2** service, **8.3** routes and gate.

## Conventions

As the registration test plan, unchanged:

- temporary databases built from migrations and canonical data
  (`tests/support.py`); `data/questions.db` is never read;
- time injected through `clock`; no test sleeps for a limit or reads the real
  clock;
- mail is the in-memory backend; no socket is opened;
- concurrency uses two connections to one database file and threads, and must
  hold with more than one handler;
- "unchanged" tests are the existing suites, passing without edits beyond
  sweep lists.

Fixtures: one account of each kind of the eligibility table of design 1.3
(verified student, verified teacher, CLI student, CLI teacher, admin, disabled,
email without password, Google-created, verified account with a linked Google
identity, a demoted admin with TOTP rows, verified and not), built through
the real account functions, with `email_verified_at` set the way registration
sets it. In scope 8.1, which has no code that creates a recovery row,
recovery rows and recovery ledger rows are inserted by SQL fixtures that
satisfy the migration's CHECKs; scope 8.2 repeats the key operations tests on
rows the service creates.

Proposed files: `test_recovery_migration_sql.py`, `test_recovery_scrub.py`,
`test_recovery_operations.py`, `test_recovery_restore_credentials.py`,
`test_recovery_no_caller.py` (8.1); `test_recovery_service.py`,
`test_recovery_eligibility.py`, `test_recovery_redeem.py`,
`test_recovery_ledger.py`, `test_recovery_settlement.py`,
`test_recovery_concurrency.py`, `test_recovery_polling.py` (8.2);
`test_recovery_routes.py`, `test_recovery_enumeration.py`,
`test_recovery_production.py` (8.3); additions to `test_access_policy.py`,
`test_session_cookie_samesite.py`, `test_canonical_content.py`,
`test_schema_baseline.py`, `test_registration_ledger.py`,
`test_registration_settlement.py`, `test_registration_restore_operations.py`,
`test_registration_production.py` (Nginx), `test_backup_sqlite.py`,
`test_manage_accounts.py`, `test_lifecycle_restore_script.py`.

## Evidence classes

| Class | What it can show | Where |
| --- | --- | --- |
| **Portable automated** | Every logic, ordering, accounting, concurrency, schema, scrub, enumeration-seam, polling, comparison and static-template test below | Any platform, in the suite |
| **Browser (manual)** | Autocomplete hints, a phone reading the code and the laptop entering it, back-button and second-tab behaviour, the visual states | A real browser against a development server with the console backend |
| **Provider** | Delivery, durations of a code and a notice, content-dependent relay behaviour | The chosen relay, addresses Gabriel owns |
| **Capacity** | `set_password` lock time, send durations under the shared budget, a class mixing registration and recovery | The server, through Nginx |
| **Linux / systemd / Nginx** | Cleanup timer, restore script, the restore credential runbook, snapshot step and sender process under the real units; the real `nginx -t` and the headers it sends | A Linux staging host |

Only the first class is automated. None of the others has been produced, and
none can be produced from the development Mac. A static test of the Nginx
template is not evidence that a real Nginx accepts it.

## RA. Schema and classification (8.1)

| ID | Test |
| --- | --- |
| RA1 | The migration applies to a database at the previous head and to an empty build, is idempotent in the runner, and `PRAGMA foreign_key_check` is clean. No existing row changes; `registration_sends` rows keep their values with `recovery_id` NULL, **including rows whose `pending_id` is NULL** (the at-most-one CHECK accepts them). |
| RA2 | `password_recoveries`: a non-pending row with any one of `email`, `user_id`, `session_key_digest`, `code_hash`, `code_expires_at` set is refused, one column at a time; a pending row without `email` is refused; a pending real row without `user_id` or digest, and a pending shadow with either, are refused; an unknown `state` and an `is_shadow` outside 0/1 are refused; duplicate `binding_hash` is refused; malformed hex in every hash column is refused. |
| RA3 | `registration_sends.recovery_id`: a row with both `pending_id` and `recovery_id` is refused, and so are a zero or negative value (also as text, `'-3'`), **non-numeric text** (`'abc'`, `''`, `'0x10'`), a value with a fraction (`5.5`, `'5.5'`) and a blob. **Numeric text that INTEGER affinity converts** (`'5'`, `'5.0'`, `' 7 '`, `'1e2'`) and the real `5.0` are stored as the integer they convert to and pass the CHECK, which sees the converted value: the test pins that conversion (stored value and `typeof` integer) rather than claiming that every text value is refused (modelled in scratch, design 11); the existing kinds and outcomes are unchanged; a row inserted with the pre-migration column list, `pending_id` NULL included, is accepted (design 3.1: exactly-one is deliberately not a CHECK). From 8.2: every ledger row the registration or recovery service inserts has exactly one of the two ids (asserted over the whole suite's service-made rows, and by instrumenting both `_issue` paths). Run on the SQLite version the server uses as well (activation evidence if it differs from the development one). |
| RA4 | Classification: `password_recoveries` is runtime; a canonical export contains it in no form; a fresh build has it empty. |
| RA5 | Code from before the migration (registration's service and CLI, explicit column lists) creates, issues, settles and cleans up on the new schema exactly as before. |
| RA6 | Purpose-less ledger rows (both ids NULL) count in every cap and in the circuit as today, are never a live code for either module, are never dispatched by either, are refused by recovery's `settle_send` and settled by registration's only as today. |

## RB. Input rules (8.2, 8.3)

| ID | Test |
| --- | --- |
| RB1 | Email: the registration public rule (registration test plan B4) applies; a malformed address is a field error, independent of any account, and writes nothing. |
| RB2 | Code: trimmed at the ends only, then exactly 8 ASCII digits (RD4, amendment AM1); inner whitespace, other Unicode digits, seven or nine digits, a six-digit value, a list, missing → wrong code, counted as an attempt when the code is live. Leading zeros are kept and generated (`randbelow(10**8)`, zero-padded to 8). Registration codes stay six digits (the registration suite, unchanged). |
| RB3 | **Password exactness** (registration test plan B8, B9) through redemption: leading, trailing and doubled spaces, a tab, a non-NFKC character and a 256-character value are stored as typed; after the reset `authenticate` accepts the original and refuses stripped, collapsed and NFKC variants; 257 characters are refused, not cut; confirmation compared exactly. |
| RB4 | A password error (too short, equal to the email, mismatch) re-renders the page with the field error, counts no attempt, reads no code, writes nothing; the password and the code are not echoed. |
| RB5 | `next`: accepted unchanged only by `accepted_next`; dropped, never repaired or echoed, otherwise; used through `safe_redirect_target`; success always goes to `/login`, never straight to `next`. |

## RC. Request and resend (8.2)

| ID | Test |
| --- | --- |
| RC1 | A request for an eligible account creates one pending real row (`user_id`, digest of the current key, `email`), one ledger row (`recovery_id` set, `pending_id` NULL, kind `code`, `attempted_at` = the lock-time clock, a fresh 32-hex identity), a keyed code hash, expiry `min(now+10 min, expires_at)`, `resend_after = now+60 s`, and raises the mark. The code appears in no column of any table. |
| RC2 | A request for each non-eligible fixture creates a shadow row (`user_id` and digest NULL) and one ledger row of kind `notice`; the neutral notice of design 2.8 is returned for dispatch to the **typed** address (RD2 (a), interpretation IN1), carries no digits and no link, and is dispatched once. The existing account's row is byte-identical afterwards. A code is never addressed to anything but an eligible account's `users.email`. |
| RC3 | A second request in the same browser voids and scrubs the previous row; its code no longer verifies. |
| RC4 | Resend: refused inside 60 s and after 3 resends; each replaces the code, resets its attempts and binds afresh: a password change between request and resend leaves the new code redeemable; a promotion to admin or a disable between them turns the row into a shadow; an account created (by registration) between them turns a shadow into a real row (RD11). |
| RC5 | Request and resend never compute scrypt (instrumented), for either kind of row. |
| RC6 | Request ordering under the lock: with the hold set, the clock behind the mark, an unreaped sender, the circuit open, the total budget used or the address capped, nothing is written: no row, no ledger row, no inline cleanup, no voided previous row, no mark change (content hash of the tables unchanged). |
| RC7 | The code of one recovery does not verify another recovery of the same address (the row id is in the hash). |
| RC8 | **One order** (design 3.5) for resend, redeem and cancel: with no row or a final row the answer is given and nothing is written **even while the hold is set or the clock is behind** (the binding read comes first and writes nothing); with a pending row and the hold set or the clock behind, nothing is expired, scrubbed, voided or counted (a row that would otherwise be expired stays `pending`, content hash unchanged). The statement sequence is captured and asserted: no write precedes the clock check. |

## RE. Eligibility, at issuance and at redemption (8.2)

Every case RE1–RE7: a code issued while eligible, a change committed, then
the correct code. Expected: `INVALIDATED`, the row `void` and scrubbed, **the
password, the role, the key, TOTP state and every other column of the account
unchanged** by the redemption, and no session.

| ID | Change between issuance and redemption |
| --- | --- |
| RE1 | `set-role admin` (CLI): the admin's password and second factor are unchanged (registration test plan M2). |
| RE2 | `set-role` teacher → student and student → teacher (the key rotates: invalidated even though both roles are eligible). |
| RE3 | `disable`; and `disable` then `enable` (the key differs). |
| RE4 | `users.email` changed by direct SQL (no API exists; the test pins the recheck, not a supported path). |
| RE5 | `password_hash` set to NULL by direct SQL. |
| RE6 | `email_verified_at` set to NULL by direct SQL (RD1: only verified-email accounts are eligible). |
| RE7 | The account's row replaced by another account with the same address (direct SQL in a test with foreign keys off): another `user_id` → invalidated. |
| RE8 | Issuance-time refusals: each non-eligible fixture of design 1.3 gets a shadow and a notice (RD2 (a)); nothing in the notice and no forced match (a test that sets the shadow's hash to a known code) redeems anything. |
| RE9 | A Google `email_at_link` equal to the typed address finds no account and receives no code; `user_identities` is neither read nor written by any recovery function (static and instrumented, registration test plan M5). |
| RE10 | **Verification-address invariant** (design 1.3, from 8.1): static, no production module (`web`, `src`) contains a statement that writes `users.email` (`UPDATE users SET ... email`, an `INSERT OR REPLACE` into `users` outside account creation), so a later email-change feature fails this test and is pointed at the invariant. From 8.2: after a direct SQL change of `users.email` that leaves `email_verified_at` set, the test documents (asserts, with a comment naming it an unsupported path) that the new address is eligible at its next request, which is why an operator must clear `email_verified_at` in the same edit; with `email_verified_at` cleared in the same statement, the new address gets a shadow. |
| RE11 | **Demoted admin with TOTP rows.** (a) A registered (verified) student promoted to admin, TOTP enrolled and confirmed, recovery codes generated, then demoted to teacher: a request issues a real code; redemption succeeds; `totp_credentials` and `totp_recovery_codes` rows are byte-identical before and after; sign-in as a teacher by password asks no second factor (unchanged); after a later `set-role admin` sign-in asks for the old second factor (unchanged). (b) A CLI-created admin demoted with its TOTP rows: never eligible (RD1, no `email_verified_at`), shadow. (c) A code issued while demoted, promotion before redemption: `INVALIDATED` (as RE1), TOTP rows unchanged. No recovery function reads or writes a TOTP table (instrumented). |

## RK. Session-key rotation (8.2)

| ID | Test |
| --- | --- |
| RK1 | Each rotation source invalidates a live code: CLI `set-password`, `/account/password`, `set-role`, `disable`/`enable`, `sign-out`, `totp-enroll`, `totp-confirm`, `totp-reset` (on an account that has TOTP state), and a change of `DZI_SECRET_KEY` (no binding, code or digest matches) (registration test plan M3). |
| RK2 | What does not rotate does not invalidate: a successful sign-in, a sign-in that upgrades a pbkdf2 hash, `set_display_name`, `mark_profile_required`, a profile edit. The code still redeems afterwards. |
| RK3 | The digest is keyed and labelled: `oidc_accounts.session_key_digest` of the same key does not equal it; the stored digest never equals the key. |

## RV. Redemption (8.2, 8.3)

| ID | Test |
| --- | --- |
| RV1 | The steps of design 2.4 in order, each in isolation: no row; final row (writes nothing, database hash unchanged); hold; clock behind; expired (row expired and scrubbed); no live code (no attempt counted); attempts used up; wrong code counted; correct code. |
| RV2 | Success: `set_password` is called (instrumented) with the exact string; the new hash verifies; `password_changed_at` and `updated_at` are written; the key differs; other sessions of the account are signed out at their next request; the redeeming browser has no `uid`, `sk`, `iat` or `mfa_*` key and no `rec`; the row is `redeemed` and scrubbed. |
| RV3 | **Other rows after a success (RD17).** Every other pending row of the account (other browsers, real and shadow, by `user_id` and by `email`) is **byte-identical** after the redemption (state, counters, code hash, digest, times); each real one's correct code then ends `INVALIDATED` at step 10 and only then is voided and scrubbed; each shadow's never matches. Rows of other accounts are byte-identical (registration test plan M4, as interpreted by IN2). |
| RV4 | **Throttle (RD8 (a)).** The address's `login_throttle` row, set to blocked before the redemption, is byte-identical after it and `/login` still answers 429 until its window ends; no recovery function reads or writes `login_throttle` (instrumented); a successful redemption issues no `login_throttle` statement. |
| RV5 | Single use: a repeat POST with the same code after success writes nothing and redirects to `/login`. |
| RV6 | Steps 1–9 are the same sequence of statements, with the same results, for a real and a shadow row, and the same before and after another browser's reset of the account (instrumented connection); none of them reads `users`. The read-set claim is made for steps 1–9, `pending_view` and `cancel` only: `resend` reads the account by design and is tested by its decisions and results instead (RPoll3). |
| RV7 | No scrypt for any outcome but a correct code with a passed recheck (instrumented). |
| RV8 | Admin TOTP unchanged: an admin's sign-in still needs TOTP or a recovery code; no recovery function touches a TOTP table (static and instrumented). |

## RPoll. Polling another browser's reset (8.2 service level, 8.3 HTTP level)

Two browsers (two clients with separate cookie jars in 8.3; two bindings in
8.2): A requests recovery for an eligible address; O requests for the same
address and redeems. Every test compares A's observations **before** O's
redemption with the same observations **after** it; "identical" means status,
`Location`, body bytes, header set and cookie structure in 8.3, and the
`Result` and the database content of A's row in 8.2.

| ID | Test |
| --- | --- |
| RPoll1 | `GET /recover/verify` from A, before and after O's reset: identical; A's row unchanged; `pending_view` reads no `users` row (instrumented). The same instrumentation on `cancel` (RPoll4) and on redemption steps 1–9 (RPoll2, RV6). It is not applied to `resend`, which reads the account (RPoll3). |
| RPoll2 | A posts a wrong code before and after O's reset: `WRONG_CODE` both times, one attempt counted each time; the sixth gives `TOO_MANY` after the reset exactly as it would before. |
| RPoll3 | A resends after O's reset, compared with a resend before it. **Instrumented, and asserted as reads that happen**: both resends read the account (the eligibility decision of RD11) and, for the real row, the dispatch reads `users.email`. **Asserted equal** before and after: the eligibility branch (real), the ledger row's kind and accounting (counted in the address's caps), the response (page, status, headers, cookie structure in 8.3; the `Result` kind in 8.2), A's row state, `send_count`, attempts reset to 0, the wait and the expiry relative to the resend's time, and the log events. **Asserted different only where every resend differs**: the code, its hash, the generation and identity, and the digest, which is that of the rotated key, so a later correct code from the mailbox redeems. A resend committed before O's reset is bound to the old key and its correct code, presented after, ends `INVALIDATED`. |
| RPoll4 | A cancels after O's reset: the same `303` as before; A's row `void` and scrubbed. |
| RPoll5 | A's correct code after O's reset (A holds the mailbox): `INVALIDATED`, A's row voided then, the account untouched by A's request. |
| RPoll6 | A's row expires on its own schedule (30 minutes from its creation) whether or not O reset, and is scrubbed by the next cleanup; no earlier scrub. |
| RPoll7 | The same comparisons with A's row a shadow (an address without an eligible account, where O's "reset" is replaced by a reset of an unrelated account): identical, so the comparison cannot separate "has an account" from "has none". |
| RPoll8 | RD8 (a): an observer who locked the address's sign-in (ten failed sign-ins) keeps getting 429 at `/login` after O's reset until the block ends, as for an address without an account; the reset itself changes no throttle row. Pinned as well, as existing behaviour that RD8 (a) does not remove: in a later window, with the observer part-way through a count (say four failures), O's successful sign-in with the new password clears the row, so the observer needs ten further failures, not six, before the next 429 (the documented later, non-reset-specific observation, design 4.4); and an observer who fails ten more times relocks the address, after the reset as before it, so the owner is denied sign-in, after the reset as before it (the documented lockout trade-off). |

## RX. Concurrency (8.2)

| ID | Test |
| --- | --- |
| RX1 | Two correct redemptions of one row released together: exactly one `REDEEMED`; the other `NOT_PENDING`; the stored hash is the winner's password; no exception escapes. |
| RX2 | Two rows of one account, both correct, released together: one redeems; the other ends `INVALIDATED` at step 10 (digest), its row then voided; the stored hash is the winner's password (RD17). |
| RX3 | Redemption racing CLI `set-password`, `set-role admin`, `disable`, `sign-out`, `totp-reset`, in both orders, through the CLI's real code path: the final state is the later commit's, and a redemption that loses is `INVALIDATED`. |
| RX4 | Five wrong codes at once on one row count five; a sixth is refused. |
| RX5 | Requests for one address at its cap minus one, and for many addresses at the total budget minus one, mixing registration and recovery: never more than the cap. The second is stamped no earlier than the first (issuance time read under the lock). |
| RX6 | A resend racing a late settlement of the previous generation, in both orders: the newer code survives with its attempts and wait. |
| RX7 | A recovery redemption racing a registration activation of the same address (a real pending registration begun before the account): the account is not changed by the activation (`EMAIL_TAKEN`), and the redemption's outcome depends only on its own recheck. |
| RX8 | A's poll, wrong code and resend racing O's redemption, in both orders: A's answers are those of RPoll whichever commits first; no exception escapes. |

## RF. Failures and commit failures (8.2, 8.3)

| ID | Test |
| --- | --- |
| RF1 | An exception injected at each statement of `redeem` (the attempt count, the code clear, the account read, `set_password`, the close), followed by a rollback that returns: the account, the key, the throttle (never touched), the other rows and this row (code, attempts) are exactly as before; the route answers „…паролата не беше сменена…“ with 500 and keeps the slot; a retry with the same code then redeems. |
| RF2 | The commit of the redemption is made to raise, in two variants: (i) the failure injected before anything is written (the database is then as before) and (ii) the commit made to take effect and the exception raised after it (a wrapper that commits, then raises). In both the route answers the **uncertain** text „Не успяхме да потвърдим дали паролата е сменена…“ with 500, never the success page and never „не беше сменена“; the slot is kept; a retry with the same code then redeems in (i) and is `NOT_PENDING (redeemed)` → `/login`, writing nothing, in (ii). A rollback made to raise gives the uncertain text too. |
| RF3 | An exception at each statement of `request` and `resend`, and a failed commit: no slot, nothing dispatched; when the commit took effect anyway (variant (ii) of RF2) the row and its ledger row exist, the ledger row stays `started` and counts, and nothing is sent. |
| RF4 | `set_password` raising `ValueError` or `LookupError` (forced) is a rollback and the first failure text, never a success. |
| RF5 | Every writer raises `TransactionRequired` and writes nothing with no transaction open, and never commits or rolls back itself (`in_transaction` before and after). |

## RI. Issuance identity, stale results and payload substitution (8.2)

| ID | Test |
| --- | --- |
| RI1 | **Rollback reuse.** A request whose transaction is rolled back after its issuance, then a committed request that reuses the same row id and ledger id (and, forced, the same digits): dispatching or settling the first `Result` is refused (`NotSendable`, `NOT_THIS_ISSUANCE`), nothing is sent, nothing settled. |
| RI2 | `dispatch` refuses, sending nothing, for each: a transaction open; another identity; a ledger row of a registration (`pending_id` set); a purpose-less ledger row; a registration `Result`; an older generation after a resend; a final recovery row; a settled generation; another recipient; another subject or body; other digits; a token that does not hash to the binding; a real row whose account's `users.email` no longer equals the row's address. |
| RI3 | `dispatch` invokes the mailer once per call, catches the mailer's exception as `unknown`, and never retries (one process per call at most). |
| RI4 | A ledger row without identity (pre-022 shape) is neither sent nor settled. |

## RS. Settlement (8.1 guard, 8.2)

| ID | Test |
| --- | --- |
| RS1 | Each outcome settles the ledger row once, with `settled_at`, and **changes no recovery row** (RD7, amendment AM2): after `not_sent` the code is not retired, its expiry, the 60-second wait and the attempts stay, and the page is the same as after `accepted`. Registration's own `not_sent` settlement (code retired, wait waived) stays pinned by the registration settlement suite, unchanged. |
| RS2 | First settlement wins; a second or different outcome changes nothing. Two settlements on two connections at once: exactly one `SETTLED`. |
| RS3 | Late settlement after a resend, a redemption, a cancellation and an expiry: only the old ledger row changes. |
| RS4 | Cross-purpose: recovery `settle_send` refuses a registration generation; registration `settle_send` (with the companion guard, from 8.1) refuses a recovery generation; neither changes any row. |
| RS5 | A settlement that never runs: the ledger row stays `started`, counts in every cap, is ignored by the circuit, and the code still redeems. |

## RL. Shared budgets and circuit (8.2)

| ID | Test |
| --- | --- |
| RL1 | **Per address, shared (RD5).** Any mix of registration codes, registration notices, recovery codes and recovery notices for one address: the sixth in 60 minutes and the ninth in 24 h 15 min are refused, whichever purpose asks; admitted one second after the oldest leaves the window. |
| RL2 | **Total, shared.** 300 issuances in an hour across both purposes; the 301st is refused for both, logged once as `total_cap_reached`; `DZI_MAIL_HOURLY_CAP` lower than 300 applies to both. |
| RL3 | **Contention.** Registration traffic using up the total budget makes recovery answer "unavailable", and the reverse; neither gets a second budget. |
| RL4 | **Not refundable.** 400 recovery requests in an hour, every one settled `refused`: exactly 300 admitted. Five `not_sent` recovery sends exhaust an address's hourly cap. |
| RL5 | **Shared circuit (RD6).** Three consecutive failures across purposes (registration, recovery, registration) close both; an `accepted` of either purpose reopens both; `refused` and `started` of either are left out; ordering is by generation id across purposes, so a late settlement of an older recovery generation cannot reorder it. |
| RL6 | **Guessing bound, steady clock, mixed purposes** (registration test plan DL5, extended): random schedules of both purposes over several simulated days; at every instant **at most eight codes of the address can be guessed in the preceding 24 hours, of which at most eight are recovery codes**. Includes the schedule revision 1 missed: a real pending registration begun before the account existed, then the account created, then that registration's resends issuing registration codes alongside recovery codes; asserted: the union stays at most eight, those registration codes are live and guessable, and a correct one ends `EMAIL_TAKEN` without changing the account. The test's docstring states the three assumptions. |
| RL7 | **Exceptions pinned, and accumulating**: two 300-second clock steps admit 13 codes in a real day (DC7 with recovery codes); a key rotation, a released hold and `clear-registration-limit` each admit 8 more in the same day, now for recovery too; and they add up: a released hold followed by a `clear-registration-limit` in one day admits 8 + 8 + 8, two key rotations likewise. No test asserts a bound across repeated events. |
| RL8 | `clear-registration-limit --email` deletes the address's recovery ledger rows too; a live recovery code of that address is then not live (fail closed); the printed note names recovery. |
| RL9 | Redemption is not stopped by the circuit, the total budget or an unreaped sender; only by the hold and the clock. |

## RT. Attempt and send limits (8.2)

| ID | Test |
| --- | --- |
| RT1 | Five wrong codes, then the correct one: refused; a resend makes a new correct code work. Malformed and empty codes count. |
| RT2 | A code past 10 minutes is refused without counting; a recovery past 30 minutes is expired and scrubbed whatever its code. |
| RT3 | Four sends at most, 60 seconds apart; the code expiry never passes the recovery's. |
| RT4 | Wrong recovery codes never count toward the sign-in throttle (RD10, amendment AM3): 100 wrong recovery codes leave sign-in for the address unblocked and its `login_throttle` row absent or unchanged. With RD8 (a), no recovery function reads or writes `login_throttle` at all. |

## RCl. Clock (8.1 for cleanup, 8.2)

| ID | Test |
| --- | --- |
| RCl1 | Clock behind the shared mark by more than 300 s: request, resend, redeem and cleanup write nothing at all, recovery and registration tables unchanged (content hash), mark not lowered; cancel refuses too. Order: with a row that would otherwise be expired, nothing is expired or scrubbed first. |
| RCl2 | A row or code dated more than 300 s in the future is expired only once the clock is no longer behind the mark (after `registrations-clock-accept`). |
| RCl3 | Every recovery issuance and cleanup raises the shared mark; a recovery issuance that raised it makes a later regressed registration request unavailable, and the reverse. |
| RCl4 | A forward step expires rows and codes early; the unbounded-budget companion of registration test plan DC4 holds for recovery too. |
| RCl5 | A clock set back does not raise the attempts a code allows. |

## RH. Hold (8.2; RH3 8.1)

| ID | Test |
| --- | --- |
| RH1 | With the `restore` hold set: request and resend answer "unavailable" before anything is written; redemption answers "unavailable" with nothing written, the code and attempts kept. |
| RH2 | Cancel during a hold voids and scrubs the row and leaves the hold and the mark exactly as they were (RD12). |
| RH3 | No automatic release: advancing the clock by a day, a week or a year releases nothing; only `registrations-release-hold --acknowledge-lost-history` does, for registration and recovery together; its printed consequence names recovery codes and says that it repairs no account (design 6.2, 6.6). |

## RR. Restore and resurrection (8.1 with fixture rows; RR1–RR3 again in 8.2 with service rows)

| ID | Test |
| --- | --- |
| RR1 | **Cancelled code not revived.** Copy the database; cancel a recovery on the original; run `registrations-restore-gate` on the copy: no pending recovery remains; the code redeems nowhere. Without the recovery part of the gate (asserted as the defect) the copy's code is live. |
| RR2 | **Spent attempts not returned**: the same with five wrong codes after the copy. |
| RR3 | **Redeemed recovery not replayed**: redeem on the original after the copy; on the gated copy the row is void and the code redeems nothing; the copy's password hash and key are the snapshot's (the documented consequence of design 6.6, which RR8–RR22 handle). |
| RR4 | The gate is one transaction: an error injected at each of its statements leaves the copy byte-identical and exits non-zero; repeatable (second run changes nothing); never clears a hold; touches no account. |
| RR5 | `registrations-restore-status --expect held` fails while a pending recovery exists, reports the count, passes after the gate, and writes nothing. |
| RR6 | Static: `deploy/learnpilot-restore.sh` is byte-identical to its pre-PR 8 content, or, if edited for wording only, still calls `registrations-restore-gate` after the copy's migration and before its final check-and-fingerprint step, and `registrations-restore-status --expect held` in that step and after the replacement; no recovery-specific command is added after the fingerprint; it still keeps the replaced database at `$STATE/replaced-<stamp>/questions.db` and still ends with its one start. The existing restore-script tests pass unchanged. |
| RR7 | A pre-migration snapshot restored through the script's migration step: the gate finds the empty table and passes. |

**Restore and credentials (RD18, design 6.6).** RR8–RR14 and RR16–RR22 test
`restore-credential-diff --snapshot --replaced` (RD18 (a), selected).
Unless a test says otherwise, each builds a "snapshot" and a "replaced"
database from one migrated build, changes the replaced one through the real
account functions, and passes both as prepared offline inputs: rollback-mode
files (`journal_mode=DELETE`, as `src.backup_sqlite` writes them) in a
directory only the test can write. No test opens a live or a developer's
database.

| ID | Test |
| --- | --- |
| RR8 | **Read-only, offline.** Both inputs byte-identical (hash) after the command; no `-wal`, `-shm` or `-journal` created next to either; both opened with `mode=ro&immutable=1` (asserted on the URI) only after the pre-checks of design 6.6 passed (asserted on the order: header, sidecars, identity and hash are read before any `sqlite3.connect`); the command works on read-only files in a read-only directory; it imports no application module that writes. **Separate dispatch (design 6.6):** with `--db` naming a WAL-mode live-like database (and, separately, a path whose open would fail), the command runs: `open_database` is never called (instrumented), the command is in neither `COMMANDS` nor `READ_ONLY_COMMANDS`, no `sqlite3.connect` and no `open` receives the `--db` path (instrumented `sqlite3.connect`, `builtins.open` and `os.open`), only `os.stat` does; the `--db` file and its sidecars are byte-identical afterwards and no sidecar is created; its exit status is its own (no `commit`). `--db` and `DZI_DB` both absent → refused before any input is read. The existing commands, `cutover-check` included, keep their dispatch (their tests unchanged). |
| RR9 | **Classes, one per account, in order** (design 6.6): one account per row of the classification table; password changed by `set_password` after the snapshot with a later timestamp → *password reverted*; `sign-out` only → *sessions revived*; created after the snapshot → *created after the snapshot*; no change → *unchanged (as far as compared)*. Separately reported: `disable` or `set-role` → *state differs*; TOTP reset or a used recovery code → *second factor differs*; both on an account whose password also changed → one class (*password reverted*) **and** both findings, with every repair listed. Each repair is printed with its category (P, N, K) and a command whose selector exists: `set-password --email` with the snapshot's address for P, `sign-out --id` or `disable --id` for N and K, `disable`/`enable`/`set-role --id` for a state finding, `totp-reset --email` for a second-factor finding; each printed command is run against the restored test database and succeeds. The exit status is non-zero whenever any account needs a repair (classes 1, 2 and 4–7, or a finding). |
| RR10 | **NULL handling.** `password_changed_at` NULL on one side only with equal hashes → *uncertain (password evidence)* (row 4); NULL on both with equal hashes and equal keys → *unchanged (as far as compared)*; a password-less account on both sides → *unchanged*; a password added after the snapshot (hash and timestamp NULL in the snapshot) → *uncertain* (row 5), never *password reverted* or *unchanged*; a hash NULL on one side **with reliable timestamps** (a password removed, or added, by direct SQL that also wrote a later `password_changed_at`) → *uncertain* (row 5), never left without a class. **Repairs of password-less restored accounts (category N):** for a row-4 account with both hashes NULL and timestamps differing, and for a row-5 account whose snapshot hash is NULL (a password added after the snapshot), the printed repair is `sign-out --id` or `disable --id`, **never** `set-password`; the same for rows 1 and 2 with a NULL snapshot hash, including a Google-created account without an address. A row-5 account whose **replaced** hash is NULL and snapshot hash is not is category P (its old password is back), with the disable named as closest to the replaced state. |
| RR11 | **Equal, coarse or unreliable timestamps.** Hashes differ, timestamps equal: two `set_password` calls inside one second, and a change whose `password_changed_at` was written back to the snapshot's value by direct SQL → *uncertain*, never *password reverted* or *unchanged*. Hashes differ, timestamp unreliable: the replaced timestamp earlier than the snapshot's (a stepped clock), or not in SQLite's layout → *uncertain*. **Hashes equal, timestamps differ** (a timestamp rewritten by direct SQL) → *uncertain*. A pbkdf2 hash upgraded at sign-in (`authenticate`; hash differs, timestamp equal) → *uncertain*. The output never says "safe" or "no change". |
| RR12 | **Identity.** An id only in the snapshot, and an id present in both with another email → *uncertain (identity)*, even when its credentials are equal; an id only in the replaced database → *created after the snapshot*. |
| RR13 | **Unavailable evidence.** Replaced input missing, unreadable, failing `quick_check`, refused by a pre-check (RR16–RR18) or with an unsupported schema (RR21): the command says the evidence is unavailable, classes every account of the snapshot *uncertain (no evidence)* with the repair of design 6.6 row 1, prints no comparison as if made, and exits non-zero. A refused or unsupported **snapshot**: nothing classified, non-zero, the message says every account of the live database is to be treated as uncertain. |
| RR14 | **No secret printed.** For every class and finding the output contains no password hash, session key, TOTP secret ciphertext or recovery-code hash (searched for each value of both databases); it prints ids, addresses, class and finding names only. |
| RR15 | **Runbook text and script unchanged** (static, documentation; from 8.1). `docs/DEPLOYMENT.md`'s restore section: (i) says that the restore script starts the service before it returns, and distinguishes that start from reopening to users; (ii) makes **verified maintenance isolation required** (RD18 (exposure)): the maintenance block established and verified **before** the restore and removed only after the repair has been validated, in one of the two forms of design 6.6 (server-level `return 503` with renewal unavailable and the certificate checked first, or location-level with an ACME exception and no other location), never a `return` added to the normal site; the server-level `access_log … lp_no_args` with `log_format lp_no_args` defined in the maintenance file, and no format logging `$request`, `$request_uri`, `$args` or `$query_string`; and its conditions as read-only checks: every listener, no old worker, listeners (`ss -ltnp`) stated as necessary and not sufficient, the backend's established connections, tunnel and forwarding processes, reverse SSH and SSH forwarding, NAT rules and `route_localnet`, `LEARNPILOT_HEALTH_URL` on loopback (the script's health check stays direct); **no fallback**: it says that a restore whose conditions cannot be established is postponed (the script is not run), that a condition found broken or an unaccounted access-log line after the service start postpones reopening and repeats the repair and validation (design 6.6, *Isolation failure*), and it offers no accepted-window option, no step that reopens before validation and no `systemctl stop` of the application as an alternative to the block; no step that starts, stops or reconfigures a tunnel, forward, firewall or service other than the Nginx block; (iii) requires reading Gunicorn's access log (`journalctl -u learnpilot --since` the service start) before reopening, and says that it detects and does not prevent access and that an absence of lines is not proof of isolation; (iv) prepares the replaced input as design 6.6 does (quiescence, byte copy, re-check, rollback-mode copy) and passes the original published snapshot, never the live database; (v) requires the repair of design 6.6's categories: a reviewed password replacement or a disable (not a sign-out alone) for P, a sign-out or a disable and never `set-password` for N, with the selectors given there; and does not promise recovery by email to accounts that are not eligible or before the hold is released; (vi) separates credential repair from `registrations-release-hold`; (vii) validates the repair as design 6.6 step 8 does: a pre-repair snapshot, a reference time captured with `date -u '+%Y-%m-%d %H:%M:%S'` after activity stopped and immediately before the first repair (not the service start), a post-repair snapshot, both from `src.backup_sqlite`, and the `sqlite3 -readonly … immutable=1` query comparing them with `>=` on `password_changed_at` (no strict `>`), `hash_changed` and `key_rotated`; it does not present a timestamp as proof that the intended password landed, and names the recipient's sign-in for a handed-over password; it does not use `manage_accounts list` for the timestamp (which it does not print) nor a direct read of the live WAL database. RR6 still holds: the script and the repository's Nginx template are byte-identical to their pre-PR 8 content. |
| RR16 | **Committed WAL-only changes.** A replaced database in WAL mode with a password change committed only in its WAL (autocheckpoint off, the connection held open, or the main file and WAL copied together): the command refuses it before any SQLite open (WAL-mode header, and the non-empty `-wal`), classifies nothing and exits non-zero. Companion assertion pinning the hazard: an `immutable=1` read of that main file returns the **old** hash while a normal read returns the new one. The same database prepared as design 6.6 step 4 prescribes (read through SQLite, `journal_mode=DELETE`) is accepted and classes the account *password reverted*: the committed change is included, never ignored. |
| RR17 | **Rollback journals, WAL-mode headers and other sidecars.** Refused before any SQLite open, each on its own and for each input: a `-journal` beside the input, of any size including zero; a `-wal` of non-zero size beside a rollback-mode input; an empty `-wal`, and a `-shm`, beside a rollback-mode input (the rule of the existing `require_inspectable_snapshot`, design 6.6 check 3); a WAL-mode header (bytes 18 and 19 equal to 2) with no `-wal` at all, such as the raw `$STATE/replaced-<stamp>/questions.db` after the script's checkpoint; a file without SQLite's magic; **an empty (zero-byte) file**, and a file shorter than 100 bytes that starts with the magic and has bytes 18 and 19 equal to 1. The empty and short files are refused by the command's own header check (design 6.6 check 2: at least the 100-byte header, the magic, bytes 18 and 19 both 1), before any SQLite open; a companion assertion pins why that check is needed: `require_inspectable_snapshot` alone returns without error for the empty file, and SQLite opens it as an empty database whose `quick_check` is `ok`. Every refusal names the file and the reason, prints no class and exits non-zero. The command reuses `require_inspectable_snapshot` for the sidecar rule, and the existing `cutover-check` tests keep passing unchanged. |
| RR18 | **Live or changing inputs.** Refused: an input that is the file at `--db` (the same inode, also through a hard link or another path), and a symbolic link to any file. Changing during the read, through a test hook between the pre-check and the post-check: an appended byte, a rewritten page with the size kept, a file replaced by rename (another inode), a `-journal` or a non-empty `-wal` appearing, a modification time changed with the content kept → each refused after the read, with nothing classified. An expected hash given and different → refused before the read. Stated in the test: a change made and undone between the two checks is not detected, and the test does not claim it is. |
| RR19 | **Different migration levels.** Snapshot at the level just after 012, after 015, after 016, after 021 and after 023, each against a replaced database at the current head (and one pair with the replaced older than the snapshot): only the required columns of design 6.6 are compared, extra columns are ignored, and the classes are as expected for the same account changes. A snapshot taken before 016 with an admin account: the admin's key differs because migration 016 rotated it on the restore's copy, and the command classes it *sessions revived* (the documented safe false positive) and never *unchanged*. |
| RR20 | **Absent historical TOTP storage.** Snapshot without `totp_credentials` and `totp_recovery_codes`, `016_admin_totp.sql` not recorded: read as no TOTP rows; a replaced database with TOTP rows for an account → *second factor differs* for it; with none on either side → no finding. A snapshot without `users` and `012_users.sql` not recorded → no accounts; every replaced account → *created after the snapshot*. |
| RR21 | **Genuinely missing required evidence.** On either side, each on its own: `users` absent with `012_users.sql` recorded; TOTP tables absent with `016_admin_totp.sql` recorded; a credential table absent with no `schema_migrations`; `users` lacking `session_key`, `password_changed_at` or `password_hash`; `totp_credentials` lacking `secret_ciphertext`; TOTP tables without `users`. Each → unsupported, the side's evidence unavailable (RR13), never silently read as "no rows". An extra column on either side → accepted. |
| RR22 | **Precedence and repair, exhaustively.** For every combination of: id in both, only in the snapshot, only in the replaced, other email; hash equal or different, NULL on neither, one or both sides; timestamp equal, different and later, different and earlier, malformed, NULL on one or both sides; key equal or different; state equal or different; TOTP equal or different: exactly one class is assigned, it is the first row of design 6.6's table that matches, and exactly one repair category (P, N, K or none) is assigned, by the class and the snapshot's hash as design 6.6's category table says; the repair printed for every class other than *created after* and *unchanged*, or for any finding, is the one those tables name. No combination receives `set-password` when the snapshot hash is NULL, and none receives only a sign-out when the snapshot hash is non-NULL and the class is 1, 2, 4, 5 or 6. No combination is left without a class (in particular a hash NULL on one side with reliable timestamps is row 5); no combination whose hashes or timestamps differ is classed *unchanged* or *sessions revived*; no combination classed *password reverted* has a NULL hash or equal or unreliable timestamps. |

## RN. Cleanup, scrub and snapshots (8.1)

| ID | Test |
| --- | --- |
| RN1 | **Quiet site.** A pending recovery row (fixture in 8.1, a real request in 8.2); no further request; `registrations-cleanup` at 31 minutes: the row is expired and every scrubbed column is NULL. |
| RN2 | Cleanup deletes final recovery rows 24 hours after creation, ledger rows (both purposes) 48 hours after the attempt, and nothing younger; it runs in the command's one transaction; it does nothing at all when registration's cleanup reports the clock behind; it refuses while the restore lock exists. |
| RN3 | `registrations-cleanup --check` fails when a pending recovery (or registration) is older than an hour. |
| RN4 | Snapshot: a backup made with a pending recovery has no pending recovery and no value in the scrubbed columns; the live database is byte-identical before and after; the scrub happens before `quick_check`, fsync and link, and the returned hash is the published file's; `recoveries_scrubbed` is reported. Nothing is asserted about free pages, unused page space or the WAL. |
| RN5 | **Schema checks (RD13)**, for the backup, the gate, the status and the cleanup: table absent and migration not recorded → nothing to scrub, success; table absent and migration recorded → refused, nothing published or replaced; a column missing → refused; an extra column → refused; the error names the table and no value. |
| RN6 | The existing backup suite passes unchanged (live backup path still the online backup API). |

## RZ. No caller (8.1, 8.2)

Static and import-level checks. They pin the absence of callers in this
repository; they are not the argument that the build is safe (design 7.3).

| ID | Test |
| --- | --- |
| RZ1 | **8.1: nothing creates a recovery row, from any entry point.** (i) No module under `web/` or `src/` (the migration excepted) contains a statement inserting into `password_recoveries` or writing `registration_sends.recovery_id` (parsed SQL strings and a scan for the table and column names in write statements); `recovery_scrub` contains only `UPDATE ... SET state = 'void'`/`'expired'` with the scrubbed columns NULL and `DELETE` of final rows. (ii) The entry points: importing `web.app`, `web.wsgi`, `web.production`, every blueprint and every `src` command module imports no recovery module other than `web.services.recovery_scrub` (and that only from the CLI and the backup module); `app.url_map` has no rule whose path or endpoint contains `recover`; no `manage_accounts` subcommand other than the operations of design 6.5 references recovery storage, and those only through `recovery_scrub`; no `before_request`, `before_app_request` or startup hook, no Gunicorn hook (`deploy/gunicorn.conf.py`), no systemd unit `ExecStart` and no script under `deploy/` refers to recovery storage except through those commands. (iii) A whole-suite run leaves `password_recoveries` empty in every database that no test fixture wrote. |
| RZ2 | **8.2: the service has no caller.** In a fresh interpreter, importing `web.app`, `web.wsgi`, `web.production`, every module in `web/blueprints/` and every `src` command module (`manage_accounts`, `manage_lifecycle`, `backup_sqlite`, `build_database`, …) leaves `web.services.recovery` out of `sys.modules`; no rule in `app.url_map` has an endpoint or path containing `recover`; no `manage_accounts` subcommand's handler references the module; no `before_request`, `before_app_request`, `teardown` or startup hook does; `deploy/gunicorn.conf.py`, every `deploy/systemd/*` unit's `ExecStart`, `deploy/learnpilot-restore.sh` and every other file under `deploy/` contain no reference to it. Retired in 8.3, replaced by RW3 and RP1. |

## RG. Enumeration seams (8.3)

For an eligible address and for each non-eligible fixture plus an address
with no account, with otherwise identical input. These tests establish design
1.4 and no more; none asserts equal delivery or duration.

| ID | Test |
| --- | --- |
| RG1 | Status, `Location`, body, header set and cookie structure identical at request, verify with a wrong code, resend, cancel, at every cap and in every unavailable state. |
| RG2 | The same number of rows, ledger rows, attempts counted, scrypt computations (zero) and sender processes (one) at request and resend (RD2 (a)). No test measures or asserts a duration. |
| RG3 | `not_sent`, `refused` and `unknown` for a real row's code give the same page, the same wait before a resend and the same attempt behaviour as the same outcome for a shadow's notice (RD7); a `not_sent` real code still counts attempts up to `TOO_MANY`. |
| RG4 | The backend told to refuse notices and accept codes, and the reverse: responses identical, counts identical, a following resend admitted or refused alike (registration test plan G3). |
| RG5 | Step-sequence comparison at the instrumented seams (registration test plan G5): no account-dependent step of the application's own between receiving the request and calling `send`, and between `send` returning and the response. Not a timing measurement. |
| RG6 | The circuit under a one-sided stall closes recovery and registration for everyone alike; pinned as the documented residual. |
| RG7 | The verify page's sentence and the `/login` link are the same for every address; `/login` itself answers the standard message for every failed sign-in, unchanged. |

## RLog. Logs (8.2, 8.3)

| ID | Test |
| --- | --- |
| RLog1 | Every log line of a full journey (request, wrong code, resend, redemption, cancellation, failures) at every level contains no address, code, token, password, hash, session key, digest, issuance identity or message body (captured handler, searched for each value). |
| RLog2 | **Design 8.1.** No line **names** the shadow state; every line written before the dispatch decision (`requested`, `unavailable`, `wrong_code`, …) is the same for a real and a shadow row; and the full sequence of event names across the `learnpilot.recovery`, `learnpilot.registration` and mail loggers is the same for a real and a shadow request and resend (RD2 (a)), outcomes aside (a relay outcome differing by content is the documented residual). `user=` appears on `redeemed` only. |
| RLog3 | `repr` of every recovery result and view omits the token, the message, the address and the identity. |
| RLog4 | No recovery value appears in any URL, `Location` header or `Set-Cookie` value; the `rec` slot holds only `v`, `t` and `next`. |
| RLog5 | Polling (RPoll1–RPoll4) writes no log line that differs before and after another browser's reset. |

## RW. Routes, session and integrity (8.3)

| ID | Test |
| --- | --- |
| RW1 | Every route declares an access and integrity policy; the coverage and `url_map` sweeps include them; cross-origin POSTs are 403 and write nothing. |
| RW2 | No GET writes: database hash and `Set-Cookie` unchanged after every GET, with and without a recovery in progress. |
| RW3 | Disabled: every `/recover...` route is the generic 404 after the same-origin check; `/login` has no link. |
| RW4 | Signed in: GETs redirect home, POSTs start nothing; the session is unchanged. |
| RW5 | Another browser (no slot) posting the correct code: no row is read by code or address; the "no usable recovery" page. A forged slot (other keys, version, token shape, `next`) is ignored. |
| RW6 | Session transitions: sign-in and the pending second factor drop `rec` and keep the attempt markers; sign-out clears everything; `reg` and `rec` coexist independently. |
| RW7 | The application sets `Cache-Control: no-store` and `Referrer-Policy: no-referrer` on every recovery response (development and tests; Nginx replaces the latter in production, RP5); the code and password inputs carry the autocomplete hints of design 2.6. |
| RW8 | The full journey: request → 303 → verify page → code and password → `/login?recovery=done` shows the notice → sign-in with the new password reaches the validated `next`; no code is sent or asked at sign-in (D7). |
| RW9 | `/login?recovery=done` while recovery is disabled shows nothing; while enabled it shows the fixed text by lookup and echoes nothing (RD16). A hand-built `/login?recovery=done` with no recovery in progress shows the same notice and changes nothing: the database hash and the session are unchanged, and nothing reads the parameter except the template's notice lookup (the notice is no proof of a reset). |
| RW10 | Cancel answers the same `303` for a pending, a final and an absent row. |

## RP. Production, gates and Nginx (8.3)

| ID | Test |
| --- | --- |
| RP1 | With `recovery.PRODUCTION_ACTIVATION` off, production starts with and without mail settings and recovery is disabled; no environment variable enables it. |
| RP2 | With it on (patched), malformed mail settings are start-up errors naming the variable without its value; `console` and `file` are refused. |
| RP3 | The four gates are independent: for each combination of the registration, teacher, profile and recovery gates, the recovery routes answer by the recovery gate alone and the others are unchanged. |
| RP4 | Recovery gate turned off with rows pending: every route 404, a correct code redeems nothing, the next cleanup scrubs them after expiry. |
| RP5 | **Nginx template, effective locations, headers and limits** (static, extending `NginxTemplateTest` and its resolver, which selects locations as Nginx does: exact, then longest prefix unless `^~`, then the first matching regular expression): for each of `/recover`, `/recover/verify`, `/recover/resend`, `/recover/cancel`, `/recover/` and `/recover/unknown`, the resolved location is a recovery location (`= /recover`, `= /recover/verify`, `= /recover/resend` or the prefix `/recover/`); its **effective** `add_header` set (its own, since it has some) contains exactly one `Referrer-Policy`, `no-referrer`, plus `X-Content-Type-Options: nosniff` and `X-Frame-Options: DENY`; it contains `proxy_hide_header Referrer-Policy;` and `proxy_pass http://127.0.0.1:8000;` and no `proxy_set_header` or `proxy_pass_request`. Limits: `/recover` and `/recover/resend` resolve to `limit_req zone=lp_register burst=90 nodelay;`, `/recover/verify` to `limit_req zone=lp_login burst=40 nodelay;`, `/recover/cancel` and `/recover/unknown` to no `limit_req`. `/recovery` and `/recover-x` resolve to the general location, not a recovery one. The recovery locations' headers equal the server level's apart from the `Referrer-Policy` (as the registration test does). |
| RP6 | Every recovery route sets its own `Referrer-Policy` in the application (RW7), and the template hides it (RP5), so a response through the template carries one value: asserted by combining the two static facts, and stated as not being evidence of a real Nginx (activation evidence, design 9). |

## RQ. CLI (8.1)

| ID | Test |
| --- | --- |
| RQ1 | Every existing `manage_accounts` command keeps its behaviour; `set-password` remains the operator's recovery and invalidates a pending recovery code (RK1, from 8.2). |
| RQ2 | The changed commands of design 6.5 report recovery counts and refuse a database whose recovery storage is incomplete with the "apply migrations first" error. |
| RQ3 | `registrations-cleanup` needs no `DZI_SECRET_KEY`; `clear-registration-limit` still does; `restore-credential-diff` (RD18 (a)) needs none. |

## Mapping to registration test plan section M

| Registration test | Here |
| --- | --- |
| M1 (admin, password-less, disabled, never-verified get no code; same answer; never-verified decided by RD1) | RE6, RE8, RE11, RG1, RC2 |
| M2 (eligibility rechecked at redemption) | RE1–RE7 |
| M3 (session-key changes void a code) | RK1, RE2, RE3 |
| M4 (success voids every other code, rotates the key, no session) | RV2, RV3, RX2, RPoll3, RPoll5 (by the digest mismatch: RD17, Gabriel's approved interpretation IN2) |
| M5 (only `users.email`; `email_at_link` never; for codes, interpretation IN1) | RE9, RI2, RC2 |
| M6 (shared total budget) | RL2, RL3 |
| Equivalents of D1–D11, DL1–DL10, DS1–DS8, DC1–DC8, DR1–DR9, E3–E5, F1–F3, G1–G6, H1–H7, N1–N5 | RC, RT, RL, RS, RCl, RR, RX, RW, RG, RF, RN, RPoll |

## Manual and activation evidence (not automated; none done)

- **Browser**: the journey on a phone (reading the code) and a laptop
  (entering it); a second tab submitting after the first succeeded; the back
  button after success; password managers offering to save the new password
  and not the code; a second browser left on the verify page while the first
  resets (it looks the same until its own code is used).
- **Provider**: delivery of a recovery code and a notice to abv.bg, Gmail and
  a school mailbox, and their measured durations through the relay (RD2 (a)).
  No equal timing is claimed from them.
- **Capacity**: `set_password` under the writer lock on the server; send
  durations with registration and recovery sharing the budget; a class-sized
  run mixing both through Nginx.
- **Linux / systemd / Nginx**: the cleanup timer scrubbing an expired recovery
  on a quiet server; the full restore script against a staging copy with a
  pending recovery, including an interrupted run and a run during a cleanup;
  the restore credential runbook of design 6.6 rehearsed with that restore,
  as design 9 lists it, with the required maintenance isolation (RR15):
  the replaced input prepared (quiescence, byte copy, re-check, rollback-mode
  copy), comparison, repair by category (a password-less account in an
  uncertain class signed out or disabled, never given a password),
  validation against a reference time captured after activity stopped
  (pre- and post-repair snapshots, the `sqlite3 -readonly` query, `>=` on
  the whole-second timestamp, the recipient's sign-in with a handed-over
  password), reopening; once with the replaced evidence unavailable and once
  with a snapshot from before migration 016; the maintenance block set and
  verified before the restore (every listener, no old worker, `ss -ltnp` showing the backend on
  loopback only, the backend's established connections, tunnel, forwarding
  and SSH processes and NAT rules read and accounted for without operating
  any of them, the script's loopback health check passing), outside
  requests (the OAuth callback with dummy `code` and `state` among them)
  answered 503 and logged without query string, nothing reaching the
  application until the block is removed after validation, ACME renewal as
  the chosen form provides, Gunicorn's access log since the service start
  read before reopening, and the postponement rule walked through (a
  condition left unmet stops the runbook before the restore script runs);
  the snapshot step on a copy with a pending recovery,
  then a test restore; the sender under the real unit (registration test plan
  L, unchanged); `nginx -t` on the template and one `Referrer-Policy:
  no-referrer` observed on each `/recover` address, including a 404 and a 429.
