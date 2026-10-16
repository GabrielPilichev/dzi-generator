# Password recovery by emailed code — test plan (registration PR 8)

Companion of `docs/reviews/PASSWORD_RECOVERY_DESIGN.md` (section numbers below
refer to it; "registration design" and "registration test plan" are
`EMAIL_REGISTRATION_DESIGN.md` and `EMAIL_REGISTRATION_TEST_PLAN.md`). A
proposal, revision 2, that follows the proposed design and changes with it;
it authorizes nothing. **No test in this document has been written or run.**
Where a test depends on an open decision it names it (RD2 (a) or (b), RD4
digits, RD8 (a) or (b), RD17, RD18); the implementation writes the variant
Gabriel selects. Where a decision would amend an approved requirement (RD4,
RD7, RD10) and is not selected, the test of the approved behaviour is
written instead.

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
| RA3 | `registration_sends.recovery_id`: a row with both `pending_id` and `recovery_id`, a zero, negative or text `recovery_id` are refused; the existing kinds and outcomes are unchanged; a row inserted with the pre-migration column list, `pending_id` NULL included, is accepted (design 3.1: exactly-one is deliberately not a CHECK). From 8.2: every ledger row the registration or recovery service inserts has exactly one of the two ids (asserted over the whole suite's service-made rows, and by instrumenting both `_issue` paths). Run on the SQLite version the server uses as well (activation evidence if it differs from the development one). |
| RA4 | Classification: `password_recoveries` is runtime; a canonical export contains it in no form; a fresh build has it empty. |
| RA5 | Code from before the migration (registration's service and CLI, explicit column lists) creates, issues, settles and cleans up on the new schema exactly as before. |
| RA6 | Purpose-less ledger rows (both ids NULL) count in every cap and in the circuit as today, are never a live code for either module, are never dispatched by either, are refused by recovery's `settle_send` and settled by registration's only as today. |

## RB. Input rules (8.2, 8.3)

| ID | Test |
| --- | --- |
| RB1 | Email: the registration public rule (registration test plan B4) applies; a malformed address is a field error, independent of any account, and writes nothing. |
| RB2 | Code: trimmed at the ends only, then exactly 8 ASCII digits if RD4 (an amendment) is selected, else 6 as approved; inner whitespace, other Unicode digits, one digit too few or too many, a list, missing → wrong code, counted as an attempt when the code is live. Leading zeros are kept and generated (`randbelow(10**8)` or `10**6`, zero-padded). Registration codes stay six digits either way. |
| RB3 | **Password exactness** (registration test plan B8, B9) through redemption: leading, trailing and doubled spaces, a tab, a non-NFKC character and a 256-character value are stored as typed; after the reset `authenticate` accepts the original and refuses stripped, collapsed and NFKC variants; 257 characters are refused, not cut; confirmation compared exactly. |
| RB4 | A password error (too short, equal to the email, mismatch) re-renders the page with the field error, counts no attempt, reads no code, writes nothing; the password and the code are not echoed. |
| RB5 | `next`: accepted unchanged only by `accepted_next`; dropped, never repaired or echoed, otherwise; used through `safe_redirect_target`; success always goes to `/login`, never straight to `next`. |

## RC. Request and resend (8.2)

| ID | Test |
| --- | --- |
| RC1 | A request for an eligible account creates one pending real row (`user_id`, digest of the current key, `email`), one ledger row (`recovery_id` set, `pending_id` NULL, kind `code`, `attempted_at` = the lock-time clock, a fresh 32-hex identity), a keyed code hash, expiry `min(now+10 min, expires_at)`, `resend_after = now+60 s`, and raises the mark. The code appears in no column of any table. |
| RC2 | A request for each non-eligible fixture creates a shadow row (`user_id` and digest NULL) and one ledger row of kind `notice`; under RD2 (a) the notice message of design 2.8 is returned for dispatch, under RD2 (b) no message is returned and nothing is dispatched. The existing account's row is byte-identical afterwards. |
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
| RE6 | `email_verified_at` set to NULL by direct SQL (RD1). |
| RE7 | The account's row replaced by another account with the same address (direct SQL in a test with foreign keys off): another `user_id` → invalidated. |
| RE8 | Issuance-time refusals: each non-eligible fixture of design 1.3 gets a shadow; no code it receives (none under RD2 (b), a notice under (a)) and no forced match (a test that sets the shadow's hash to a known code) redeems anything. |
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
| RV3 | **Other rows after a success (RD17).** Every other pending row of the account (other browsers, real and shadow, by `user_id` and by `email`) is **byte-identical** after the redemption (state, counters, code hash, digest, times); each real one's correct code then ends `INVALIDATED` at step 10 and only then is voided and scrubbed; each shadow's never matches. Rows of other accounts are byte-identical (registration test plan M4). Under RD17's alternative (if selected instead): every other row is `void` and scrubbed in the same transaction. |
| RV4 | **Throttle (RD8).** (a) The address's `login_throttle` row, set to blocked before the redemption, is byte-identical after it and `/login` still answers 429 until its window ends; no recovery function reads or writes `login_throttle` (instrumented). (b) If selected instead: the row is deleted in the same transaction; a throttle row of another address is untouched. |
| RV5 | Single use: a repeat POST with the same code after success writes nothing and redirects to `/login`. |
| RV6 | Steps 1–9 are the same sequence of statements for a real and a shadow row, and the same before and after another browser's reset of the account (instrumented connection). |
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
| RPoll1 | `GET /recover/verify` from A, before and after O's reset: identical; A's row unchanged; `pending_view` reads no `users` row (instrumented). |
| RPoll2 | A posts a wrong code before and after O's reset: `WRONG_CODE` both times, one attempt counted each time; the sixth gives `TOO_MANY` after the reset exactly as it would before. |
| RPoll3 | A resends after O's reset: the same page and status as a resend before; a new ledger row counted in the address's caps; the new code is bound to the rotated key (its digest equals the digest of the account's current key), so a later correct code from the mailbox redeems. A resend committed before O's reset is bound to the old key and its correct code, presented after, ends `INVALIDATED`. |
| RPoll4 | A cancels after O's reset: the same `303` as before; A's row `void` and scrubbed. |
| RPoll5 | A's correct code after O's reset (A holds the mailbox): `INVALIDATED`, A's row voided then, the account untouched by A's request. |
| RPoll6 | A's row expires on its own schedule (30 minutes from its creation) whether or not O reset, and is scrubbed by the next cleanup; no earlier scrub. |
| RPoll7 | The same comparisons with A's row a shadow (an address without an eligible account, where O's "reset" is replaced by a reset of an unrelated account): identical, so the comparison cannot separate "has an account" from "has none". |
| RPoll8 | RD8 (a): an observer who locked the address's sign-in (ten failed sign-ins) keeps getting 429 at `/login` after O's reset until the window ends, as for an address without an account. (Under RD8 (b), the test pins the documented disclosure: the next failed sign-in after the reset answers „Грешен имейл или парола.“.) |

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
| RF1 | An exception injected at each statement of `redeem` (the attempt count, the code clear, the account read, `set_password`, the throttle clear under RD8 (b), the close), followed by a rollback that returns: the account, the key, the throttle, the other rows and this row (code, attempts) are exactly as before; the route answers „…паролата не беше сменена…“ with 500 and keeps the slot; a retry with the same code then redeems. |
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
| RS1 | Each outcome settles the ledger row once, with `settled_at`, and **changes no recovery row** if RD7 (an amendment) is selected: the code, its expiry, the wait and the attempts stay. If RD7 is not selected, the approved behaviour of registration design 4.3 is tested instead (`not_sent` retires the code and waives the wait, and the page says so). |
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
| RT4 | Recovery attempts never write `login_throttle` (RD10, an amendment; if not selected, the approved counting of registration design 3.4 is tested instead): 100 wrong recovery codes leave sign-in for the address unblocked. Under RD8 (a) no recovery function reads or writes it at all. |

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
| RR3 | **Redeemed recovery not replayed**: redeem on the original after the copy; on the gated copy the row is void and the code redeems nothing; the copy's password hash and key are the snapshot's (the documented consequence of design 6.6, which RR8–RR15 handle). |
| RR4 | The gate is one transaction: an error injected at each of its statements leaves the copy byte-identical and exits non-zero; repeatable (second run changes nothing); never clears a hold; touches no account. |
| RR5 | `registrations-restore-status --expect held` fails while a pending recovery exists, reports the count, passes after the gate, and writes nothing. |
| RR6 | Static: `deploy/learnpilot-restore.sh` is byte-identical to its pre-PR 8 content, or, if edited for wording only, still calls `registrations-restore-gate` after the copy's migration and before its final check-and-fingerprint step, and `registrations-restore-status --expect held` in that step and after the replacement; no recovery-specific command is added after the fingerprint; it still keeps the replaced database at `$STATE/replaced-<stamp>/questions.db` and still ends with its one start. The existing restore-script tests pass unchanged. |
| RR7 | A pre-migration snapshot restored through the script's migration step: the gate finds the empty table and passes. |

**Restore and credentials (RD18, design 6.6).** RR8–RR14 test
`restore-credential-diff` under RD18 (a); under (b) the same cases are run
against the SQL of the runbook in `docs/DEPLOYMENT.md`, extracted from the
document by the test. Each builds a "snapshot" database and a "replaced"
database from one migrated build, then changes the replaced one.

| ID | Test |
| --- | --- |
| RR8 | **Read-only.** Both files byte-identical (hash) after the command; no `-wal`, `-shm` or `-journal` file created next to either; the command opens both with `mode=ro&immutable=1` (asserted on the URI) and works on read-only files in a read-only directory; it imports no application module that writes. |
| RR9 | **Classes.** One account per row of the table of design 6.6: password changed by `set_password` after the snapshot → *password reverted*; `sign-out` only → *sessions revived*; `disable` or `set-role` → *state reverted*; TOTP reset or a used recovery code → *second factor reverted*; account created after the snapshot → *created after the snapshot*; no change → *unchanged (as far as compared)*. The exit status is non-zero while any *password reverted*, *second factor reverted* or *uncertain* account exists. |
| RR10 | **NULL handling.** `password_changed_at` NULL on one side only → *password reverted*; NULL on both with equal hashes → *unchanged (as far as compared)*; a password-less account on both sides → *unchanged*; a password added on one side → *password reverted*. |
| RR11 | **Equal or coarse timestamps.** Two `set_password` calls inside one second, and a change whose `password_changed_at` was written back to the snapshot's value by direct SQL: hashes differ, timestamps equal → *uncertain*, never *unchanged*. A pbkdf2 hash upgraded at sign-in (`authenticate`) → *uncertain*. The output never says "safe" or "no change". |
| RR12 | **Missing accounts.** An id only in the snapshot, and an id present in both with another email → *uncertain*; an id only in the replaced database → *created after the snapshot*. |
| RR13 | **Unavailable evidence.** Replaced file missing, unreadable, failing `quick_check`, or with an unknown `users` column set: the command says the evidence is unavailable, classes every account with a password *uncertain*, prints no comparison as if made, and exits non-zero. |
| RR14 | **No secret printed.** For every class the output contains no password hash, session key, TOTP secret or recovery-code hash (searched for each value of both databases); it prints ids, addresses and class names only. |
| RR15 | **Runbook text and script unchanged** (static, documentation): `docs/DEPLOYMENT.md`'s restore section names the comparison, requires a reviewed password replacement or a disable (not a sign-out alone) for *password reverted*, *second factor reverted* and *uncertain* accounts before the service is started again, and separates credential repair from `registrations-release-hold`; RR6 still holds. |

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
| RG2 | The same number of rows, ledger rows, attempts counted and scrypt computations (zero) at request and resend. Under RD2 (a) the same number of sender processes; under RD2 (b) the difference is exactly one process and is asserted as the documented difference (design 1.4: with current synchronous dispatch, likely distinguishable; not measured). No test measures or asserts a duration. |
| RG3 | `not_sent`, `refused` and `unknown` for a real row's code give the same page, the same wait before a resend and the same attempt behaviour as a shadow (RD7, if selected); a `not_sent` real code still counts attempts up to `TOO_MANY`. |
| RG4 | Under RD2 (a): the backend told to refuse notices and accept codes, and the reverse: responses identical, counts identical, a following resend admitted or refused alike (registration test plan G3). |
| RG5 | Step-sequence comparison at the instrumented seams (registration test plan G5): no account-dependent step of the application's own between receiving the request and calling `send`, and between `send` returning and the response. Not a timing measurement. |
| RG6 | The circuit under a one-sided stall closes recovery and registration for everyone alike; pinned as the documented residual. |
| RG7 | The verify page's sentence and the `/login` link are the same for every address; `/login` itself answers the standard message for every failed sign-in, unchanged. |

## RLog. Logs (8.2, 8.3)

| ID | Test |
| --- | --- |
| RLog1 | Every log line of a full journey (request, wrong code, resend, redemption, cancellation, failures) at every level contains no address, code, token, password, hash, session key, digest, issuance identity or message body (captured handler, searched for each value). |
| RLog2 | **By option (design 8.1).** In every option: no line **names** the shadow state, and every line written before the dispatch decision (`requested`, `unavailable`, `wrong_code`, …) is the same for a real and a shadow row. **Under RD2 (a)**: the full sequence of event names across the `learnpilot.recovery`, `learnpilot.registration` and mail loggers is the same for a real and a shadow request and resend, outcomes aside (a relay outcome differing by content is the documented residual). **Under RD2 (b)**: the test asserts the documented difference and no more: `send_started` and `send_settled` (and, with a failing backend, `dispatch_failed`, `settlement_failed`, `sender_error`) appear for real rows only. `user=` appears on `redeemed` only. |
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
| RW9 | `/login?recovery=done` while recovery is disabled shows nothing; while enabled it shows the fixed text by lookup and echoes nothing (RD16). |
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
| M1 (admin, password-less, disabled, never-verified get no code; same answer) | RE8, RE11, RG1, RC2 |
| M2 (eligibility rechecked at redemption) | RE1–RE7 |
| M3 (session-key changes void a code) | RK1, RE2, RE3 |
| M4 (success voids every other code, rotates the key, no session) | RV2, RV3, RX2, RPoll3, RPoll5 (by the digest, RD17, an interpretation for Gabriel to confirm) |
| M5 (only `users.email`; `email_at_link` never) | RE9, RI2 |
| M6 (shared total budget) | RL2, RL3 |
| Equivalents of D1–D11, DL1–DL10, DS1–DS8, DC1–DC8, DR1–DR9, E3–E5, F1–F3, G1–G6, H1–H7, N1–N5 | RC, RT, RL, RS, RCl, RR, RX, RW, RG, RF, RN, RPoll |

## Manual and activation evidence (not automated; none done)

- **Browser**: the journey on a phone (reading the code) and a laptop
  (entering it); a second tab submitting after the first succeeded; the back
  button after success; password managers offering to save the new password
  and not the code; a second browser left on the verify page while the first
  resets (it looks the same until its own code is used).
- **Provider**: delivery of a recovery code and a notice to abv.bg, Gmail and
  a school mailbox; under RD2 (a) their measured durations through the relay,
  under RD2 (b) the recorded acceptance that eligibility is likely
  distinguishable by timing. No equal timing is claimed from either.
- **Capacity**: `set_password` under the writer lock on the server; send
  durations with registration and recovery sharing the budget; a class-sized
  run mixing both through Nginx.
- **Linux / systemd / Nginx**: the cleanup timer scrubbing an expired recovery
  on a quiet server; the full restore script against a staging copy with a
  pending recovery, including an interrupted run and a run during a cleanup;
  the restore credential runbook of design 6.6 rehearsed after that restore
  (service stopped, comparison, repair, start), and once with the replaced
  evidence unavailable; the snapshot step on a copy with a pending recovery,
  then a test restore; the sender under the real unit (registration test plan
  L, unchanged); `nginx -t` on the template and one `Referrer-Policy:
  no-referrer` observed on each `/recover` address, including a 404 and a 429.
