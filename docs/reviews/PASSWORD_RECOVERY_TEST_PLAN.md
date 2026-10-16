# Password recovery by emailed code — test plan (registration PR 8)

Companion of `docs/reviews/PASSWORD_RECOVERY_DESIGN.md` (section numbers below
refer to it; "registration design" and "registration test plan" are
`EMAIL_REGISTRATION_DESIGN.md` and `EMAIL_REGISTRATION_TEST_PLAN.md`). A
proposal that follows the proposed design and changes with it; it authorizes
nothing. **No test in this document has been written or run.** Where a test
depends on an open decision it names it (RD2 (a) or (b), RD4 digits, RD8);
the implementation writes the variant Gabriel selects.

Test ids (RA1, RV3, …) are local to this document.

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
identity), built through the real account functions, with `email_verified_at`
set the way registration sets it.

Proposed files: `test_recovery_migration_sql.py`, `test_recovery_service.py`,
`test_recovery_eligibility.py`, `test_recovery_redeem.py`,
`test_recovery_ledger.py`, `test_recovery_settlement.py`,
`test_recovery_concurrency.py`, `test_recovery_operations.py`,
`test_recovery_routes.py`, `test_recovery_enumeration.py`,
`test_recovery_production.py`; additions to `test_access_policy.py`,
`test_session_cookie_samesite.py`, `test_canonical_content.py`,
`test_schema_baseline.py`, `test_registration_ledger.py`,
`test_registration_settlement.py`, `test_registration_restore_operations.py`,
`test_backup_sqlite.py`, `test_manage_accounts.py`.

## Evidence classes

| Class | What it can show | Where |
| --- | --- | --- |
| **Portable automated** | Every logic, ordering, accounting, concurrency, schema, scrub and enumeration-seam test below | Any platform, in the suite |
| **Browser (manual)** | Autocomplete hints, a phone reading the code and the laptop entering it, back-button and second-tab behaviour, the visual states | A real browser against a development server with the console backend |
| **Provider** | Delivery, durations of a code and a notice, content-dependent relay behaviour | The chosen relay, addresses Gabriel owns |
| **Capacity** | `set_password` lock time, send durations under the shared budget, a class mixing registration and recovery | The server, through Nginx |
| **Linux / systemd** | Cleanup timer, restore script, snapshot step and sender process under the real units | A Linux staging host |

Only the first class is automated. None of the others has been produced, and
none can be produced from the development Mac.

## RA. Schema and classification (8.1)

| ID | Test |
| --- | --- |
| RA1 | The migration applies to a database at the previous head and to an empty build, is idempotent in the runner, and `PRAGMA foreign_key_check` is clean. No existing row changes; `registration_sends` rows keep their values with `recovery_id` NULL. |
| RA2 | `password_recoveries`: a non-pending row with any one of `email`, `user_id`, `session_key_digest`, `code_hash`, `code_expires_at` set is refused, one column at a time; a pending row without `email` is refused; a pending real row without `user_id` or digest, and a pending shadow with either, are refused; an unknown `state` and an `is_shadow` outside 0/1 are refused; duplicate `binding_hash` is refused; malformed hex in every hash column is refused. |
| RA3 | `registration_sends.recovery_id`: a row with both `pending_id` and `recovery_id`, a zero, negative or text `recovery_id` are refused; the existing kinds and outcomes are unchanged; a row inserted with the pre-migration column list is accepted. Run on the SQLite version the server uses as well (activation evidence if it differs from the development one). |
| RA4 | Classification: `password_recoveries` is runtime; a canonical export contains it in no form; a fresh build has it empty. |
| RA5 | Code from before the migration (registration's service and CLI, explicit column lists) creates, issues, settles and cleans up on the new schema exactly as before. |

## RB. Input rules (8.1, 8.3)

| ID | Test |
| --- | --- |
| RB1 | Email: the registration public rule (registration test plan B4) applies; a malformed address is a field error, independent of any account, and writes nothing. |
| RB2 | Code: trimmed at the ends only, then exactly 8 ASCII digits (RD4; 6 if not approved); inner whitespace, other Unicode digits, 7 or 9 digits, a list, missing → wrong code, counted as an attempt when the code is live. Leading zeros are kept and generated (`randbelow(10**8)`, zero-padded). |
| RB3 | **Password exactness** (registration test plan B8, B9) through redemption: leading, trailing and doubled spaces, a tab, a non-NFKC character and a 256-character value are stored as typed; after the reset `authenticate` accepts the original and refuses stripped, collapsed and NFKC variants; 257 characters are refused, not cut; confirmation compared exactly. |
| RB4 | A password error (too short, equal to the email, mismatch) re-renders the page with the field error, counts no attempt, reads no code, writes nothing; the password and the code are not echoed. |
| RB5 | `next`: accepted unchanged only by `accepted_next`; dropped, never repaired or echoed, otherwise; used through `safe_redirect_target`; success always goes to `/login`, never straight to `next`. |

## RC. Request and resend (8.1)

| ID | Test |
| --- | --- |
| RC1 | A request for an eligible account creates one pending real row (`user_id`, digest of the current key, `email`), one ledger row (`recovery_id` set, `pending_id` NULL, kind `code`, `attempted_at` = the lock-time clock, a fresh 32-hex identity), a keyed code hash, expiry `min(now+10 min, expires_at)`, `resend_after = now+60 s`, and raises the mark. The code appears in no column of any table. |
| RC2 | A request for each non-eligible fixture creates a shadow row (`user_id` and digest NULL) and one ledger row of kind `notice`; under RD2 (a) the notice message of design 2.8 is returned for dispatch, under RD2 (b) no message is returned and nothing is dispatched. The existing account's row is byte-identical afterwards. |
| RC3 | A second request in the same browser voids and scrubs the previous row; its code no longer verifies. |
| RC4 | Resend: refused inside 60 s and after 3 resends; each replaces the code, resets its attempts and binds afresh: a password change between request and resend leaves the new code redeemable; a promotion to admin or a disable between them turns the row into a shadow; an account created (by registration) between them turns a shadow into a real row (RD11). |
| RC5 | Request and resend never compute scrypt (instrumented), for either kind of row. |
| RC6 | Request ordering under the lock: with the hold set, the clock behind the mark, an unreaped sender, the circuit open, the total budget used or the address capped, nothing is written: no row, no ledger row, no inline cleanup, no voided previous row, no mark change (content hash of the tables unchanged). |
| RC7 | The code of one recovery does not verify another recovery of the same address (the row id is in the hash). |

## RE. Eligibility, at issuance and at redemption (8.1)

Every case: a code issued while eligible, a change committed, then the correct
code. Expected: `INVALIDATED`, the row `void` and scrubbed, **the password,
the role, the key, TOTP state and every other column of the account
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

## RK. Session-key rotation (8.1)

| ID | Test |
| --- | --- |
| RK1 | Each rotation source invalidates a live code: CLI `set-password`, `/account/password`, `set-role`, `disable`/`enable`, `sign-out`, `totp-enroll`, `totp-confirm`, `totp-reset` (on an account that has TOTP state), and a change of `DZI_SECRET_KEY` (no binding, code or digest matches) (registration test plan M3). |
| RK2 | What does not rotate does not invalidate: a successful sign-in, a sign-in that upgrades a pbkdf2 hash, `set_display_name`, `mark_profile_required`, a profile edit. The code still redeems afterwards. |
| RK3 | The digest is keyed and labelled: `oidc_accounts.session_key_digest` of the same key does not equal it; the stored digest never equals the key. |

## RV. Redemption (8.1, 8.3)

| ID | Test |
| --- | --- |
| RV1 | The steps of design 2.4 in order, each in isolation: no row; final row (writes nothing, database hash unchanged); hold; clock behind; expired (row expired and scrubbed); no live code (no attempt counted); attempts used up; wrong code counted; correct code. |
| RV2 | Success: `set_password` is called (instrumented) with the exact string; the new hash verifies; `password_changed_at` and `updated_at` are written; the key differs; other sessions of the account are signed out at their next request; the redeeming browser has no `uid`, `sk`, `iat` or `mfa_*` key and no `rec`; the row is `redeemed` and scrubbed. |
| RV3 | Every other pending row of the account (other browsers, real and shadow, by `user_id` and by `email`) is voided and scrubbed; rows of other accounts are byte-identical (registration test plan M4). |
| RV4 | RD8: the address's `login_throttle` row is deleted in the same transaction; a throttle row of another address is untouched. (If RD8 is not approved: the row is untouched and sign-in stays blocked until it ends.) |
| RV5 | Single use: a repeat POST with the same code after success writes nothing and redirects to `/login`. |
| RV6 | Steps 1–9 are the same sequence of statements for a real and a shadow row (instrumented connection). |
| RV7 | No scrypt for any outcome but a correct code with a passed recheck (instrumented). |
| RV8 | Admin TOTP unchanged: an admin's sign-in still needs TOTP or a recovery code; no recovery function touches a TOTP table (static and instrumented). |

## RX. Concurrency (8.1)

| ID | Test |
| --- | --- |
| RX1 | Two correct redemptions of one row released together: exactly one `REDEEMED`; the other `NOT_PENDING`; the stored hash is the winner's password; no exception escapes. |
| RX2 | Two rows of one account, both correct, released together: one redeems; the other ends `void` (or `INVALIDATED` if the voiding is disabled in a companion test, pinning the digest as the second line). |
| RX3 | Redemption racing CLI `set-password`, `set-role admin`, `disable`, `sign-out`, `totp-reset`, in both orders, through the CLI's real code path: the final state is the later commit's, and a redemption that loses is `INVALIDATED`. |
| RX4 | Five wrong codes at once on one row count five; a sixth is refused. |
| RX5 | Requests for one address at its cap minus one, and for many addresses at the total budget minus one, mixing registration and recovery: never more than the cap. The second is stamped no earlier than the first (issuance time read under the lock). |
| RX6 | A resend racing a late settlement of the previous generation, in both orders: the newer code survives with its attempts and wait. |
| RX7 | A recovery redemption racing a registration activation of the same address (a real pending registration begun before the account): the account is not changed by the activation (`EMAIL_TAKEN`), and the redemption's outcome depends only on its own recheck. |

## RF. Failures and commit failures (8.1, 8.3)

| ID | Test |
| --- | --- |
| RF1 | An exception injected at each statement of `redeem` (the attempt count, the code clear, the account read, `set_password`, the throttle clear, the voiding, the close): the account, the key, the throttle, the other rows and this row (code, attempts) are exactly as before; the route answers the generic failure with 500 and keeps the slot; a retry with the same code then redeems. |
| RF2 | The commit of the redemption is made to fail: the same as RF1; no success page, no slot dropped. |
| RF3 | An exception at each statement of `request` and `resend`, and a failed commit: no row, no ledger row, no slot, nothing dispatched. |
| RF4 | `set_password` raising `ValueError` or `LookupError` (forced) is a rollback and the generic failure, never a success. |
| RF5 | Every writer raises `TransactionRequired` and writes nothing with no transaction open, and never commits or rolls back itself (`in_transaction` before and after). |

## RI. Issuance identity, stale results and payload substitution (8.1)

| ID | Test |
| --- | --- |
| RI1 | **Rollback reuse.** A request whose transaction is rolled back after its issuance, then a committed request that reuses the same row id and ledger id (and, forced, the same digits): dispatching or settling the first `Result` is refused (`NotSendable`, `NOT_THIS_ISSUANCE`), nothing is sent, nothing settled. |
| RI2 | `dispatch` refuses, sending nothing, for each: a transaction open; another identity; a ledger row of a registration (`pending_id` set); a registration `Result`; an older generation after a resend; a final recovery row; a settled generation; another recipient; another subject or body; other digits; a token that does not hash to the binding; a real row whose account's `users.email` no longer equals the row's address. |
| RI3 | `dispatch` invokes the mailer once per call, catches the mailer's exception as `unknown`, and never retries (one process per call at most). |
| RI4 | A ledger row without identity (pre-022 shape) is neither sent nor settled. |

## RS. Settlement (8.1)

| ID | Test |
| --- | --- |
| RS1 | Each outcome settles the ledger row once, with `settled_at`, and **changes no recovery row** (RD7): the code, its expiry, the wait and the attempts stay. |
| RS2 | First settlement wins; a second or different outcome changes nothing. Two settlements on two connections at once: exactly one `SETTLED`. |
| RS3 | Late settlement after a resend, a redemption, a cancellation and an expiry: only the old ledger row changes. |
| RS4 | Cross-purpose: recovery `settle_send` refuses a registration generation; registration `settle_send` (with the companion guard) refuses a recovery generation; neither changes any row. |
| RS5 | A settlement that never runs: the ledger row stays `started`, counts in every cap, is ignored by the circuit, and the code still redeems. |

## RL. Shared budgets and circuit (8.1)

| ID | Test |
| --- | --- |
| RL1 | **Per address, shared (RD5).** Any mix of registration codes, registration notices, recovery codes and recovery notices for one address: the sixth in 60 minutes and the ninth in 24 h 15 min are refused, whichever purpose asks; admitted one second after the oldest leaves the window. |
| RL2 | **Total, shared.** 300 issuances in an hour across both purposes; the 301st is refused for both, logged once as `total_cap_reached`; `DZI_MAIL_HOURLY_CAP` lower than 300 applies to both. |
| RL3 | **Contention.** Registration traffic using up the total budget makes recovery answer "unavailable", and the reverse; neither gets a second budget. |
| RL4 | **Not refundable.** 400 recovery requests in an hour, every one settled `refused`: exactly 300 admitted. Five `not_sent` recovery sends exhaust an address's hourly cap. |
| RL5 | **Shared circuit (RD6).** Three consecutive failures across purposes (registration, recovery, registration) close both; an `accepted` of either purpose reopens both; `refused` and `started` of either are left out; ordering is by generation id across purposes, so a late settlement of an older recovery generation cannot reorder it. |
| RL6 | **Guessing bound, steady clock** (registration test plan DL5, extended): random schedules of both purposes over several simulated days; at every instant at most 8 codes of an address can be guessed in the preceding 24 hours, and for an address with an eligible account they are all recovery codes. The test's docstring states the three assumptions. |
| RL7 | **Exceptions pinned**: two 300-second clock steps admit 13 codes in a real day (DC7 with recovery codes); a key rotation, a released hold and `clear-registration-limit` each admit 8 more in the same day, now for recovery too. |
| RL8 | `clear-registration-limit --email` deletes the address's recovery ledger rows too; a live recovery code of that address is then not live (fail closed); the printed note names recovery. |
| RL9 | Redemption is not stopped by the circuit, the total budget or an unreaped sender; only by the hold and the clock. |

## RT. Attempt and send limits (8.1)

| ID | Test |
| --- | --- |
| RT1 | Five wrong codes, then the correct one: refused; a resend makes a new correct code work. Malformed and empty codes count. |
| RT2 | A code past 10 minutes is refused without counting; a recovery past 30 minutes is expired and scrubbed whatever its code. |
| RT3 | Four sends at most, 60 seconds apart; the code expiry never passes the recovery's. |
| RT4 | Recovery attempts never read or write `login_throttle` (RD10): 100 wrong recovery codes leave sign-in for the address unblocked. |

## RCl. Clock (8.1)

| ID | Test |
| --- | --- |
| RCl1 | Clock behind the shared mark by more than 300 s: request, resend, redeem and cleanup write nothing at all, recovery and registration tables unchanged (content hash), mark not lowered; cancel refuses too. Order: with a row that would otherwise be expired, nothing is expired or scrubbed first. |
| RCl2 | A row or code dated more than 300 s in the future is expired only once the clock is no longer behind the mark (after `registrations-clock-accept`). |
| RCl3 | Every recovery issuance and cleanup raises the shared mark; a recovery issuance that raised it makes a later regressed registration request unavailable, and the reverse. |
| RCl4 | A forward step expires rows and codes early; the unbounded-budget companion of registration test plan DC4 holds for recovery too. |
| RCl5 | A clock set back does not raise the attempts a code allows. |

## RH. Hold (8.1, 8.2)

| ID | Test |
| --- | --- |
| RH1 | With the `restore` hold set: request and resend answer "unavailable" before anything is written; redemption answers "unavailable" with nothing written, the code and attempts kept. |
| RH2 | Cancel during a hold voids and scrubs the row and leaves the hold and the mark exactly as they were (RD12). |
| RH3 | No automatic release: advancing the clock by a day, a week or a year releases nothing; only `registrations-release-hold --acknowledge-lost-history` does, for registration and recovery together; its printed consequence names recovery. |

## RR. Restore and resurrection (8.2)

| ID | Test |
| --- | --- |
| RR1 | **Cancelled code not revived.** Copy the database; cancel a recovery on the original; run `registrations-restore-gate` on the copy: no pending recovery remains; the code redeems nowhere. Without the recovery part of the gate (asserted as the defect) the copy's code is live. |
| RR2 | **Spent attempts not returned**: the same with five wrong codes after the copy. |
| RR3 | **Redeemed recovery not replayed**: redeem on the original after the copy; on the gated copy the row is void and the code redeems nothing; the copy's password and key are the snapshot's (documented consequence of design 8.2). |
| RR4 | The gate is one transaction: an error injected at each of its statements leaves the copy byte-identical and exits non-zero; repeatable (second run changes nothing); never clears a hold; touches no account. |
| RR5 | `registrations-restore-status --expect held` fails while a pending recovery exists, reports the count, passes after the gate, and writes nothing. |
| RR6 | Static: `deploy/learnpilot-restore.sh` is byte-identical to its pre-PR 8 content, or, if edited for wording only, still calls `registrations-restore-gate` after the copy's migration and before its final check-and-fingerprint step, and `registrations-restore-status --expect held` in that step and after the replacement; no recovery-specific command is added after the fingerprint. The existing restore-script tests pass unchanged. |
| RR7 | A pre-migration snapshot restored through the script's migration step: the gate finds the empty table and passes. |

## RN. Cleanup, scrub and snapshots (8.1, 8.2)

| ID | Test |
| --- | --- |
| RN1 | **Quiet site.** A recovery is requested; no further request; `registrations-cleanup` at 31 minutes: the row is expired and every scrubbed column is NULL. |
| RN2 | Cleanup deletes final recovery rows 24 hours after creation, ledger rows (both purposes) 48 hours after the attempt, and nothing younger; it runs in the command's one transaction; it does nothing at all when registration's cleanup reports the clock behind; it refuses while the restore lock exists. |
| RN3 | `registrations-cleanup --check` fails when a pending recovery (or registration) is older than an hour. |
| RN4 | Snapshot: a backup made with a pending recovery has no pending recovery and no value in the scrubbed columns; the live database is byte-identical before and after; the scrub happens before `quick_check`, fsync and link, and the returned hash is the published file's; `recoveries_scrubbed` is reported. Nothing is asserted about free pages, unused page space or the WAL. |
| RN5 | **Schema checks (RD13)**, for the backup, the gate, the status and the cleanup: table absent and migration not recorded → nothing to scrub, success; table absent and migration recorded → refused, nothing published or replaced; a column missing → refused; an extra column → refused; the error names the table and no value. |
| RN6 | The existing backup suite passes unchanged (live backup path still the online backup API). |

## RG. Enumeration seams (8.3)

For an eligible address and for each non-eligible fixture plus an address
with no account, with otherwise identical input. These tests establish design
1.4 and no more; none asserts equal delivery or duration.

| ID | Test |
| --- | --- |
| RG1 | Status, `Location`, body, header set and cookie structure identical at request, verify with a wrong code, resend, cancel, at every cap and in every unavailable state. |
| RG2 | The same number of rows, ledger rows, attempts counted and scrypt computations (zero) at request and resend. Under RD2 (a) the same number of sender processes; under RD2 (b) the difference is exactly one process and is asserted as the documented oracle. |
| RG3 | `not_sent`, `refused` and `unknown` for a real row's code give the same page, the same wait before a resend and the same attempt behaviour as a shadow (RD7); a `not_sent` real code still counts attempts up to `TOO_MANY`. |
| RG4 | Under RD2 (a): the backend told to refuse notices and accept codes, and the reverse: responses identical, counts identical, a following resend admitted or refused alike (registration test plan G3). |
| RG5 | Step-sequence comparison at the instrumented seams (registration test plan G5): no account-dependent step of the application's own between receiving the request and calling `send`, and between `send` returning and the response. Not a timing measurement. |
| RG6 | The circuit under a one-sided stall closes recovery and registration for everyone alike; pinned as the documented residual. |
| RG7 | The verify page's sentence and the `/login` link are the same for every address; `/login` itself answers the standard message for every failed sign-in, unchanged. |

## RLog. Logs (8.1, 8.3)

| ID | Test |
| --- | --- |
| RLog1 | Every log line of a full journey (request, wrong code, resend, redemption, cancellation, failures) at every level contains no address, code, token, password, hash, session key, digest, issuance identity or message body (captured handler, searched for each value). |
| RLog2 | No log line distinguishes a shadow from a real row before redemption. `user=` appears on `redeemed` only. |
| RLog3 | `repr` of every recovery result and view omits the token, the message, the address and the identity. |
| RLog4 | No recovery value appears in any URL, `Location` header or `Set-Cookie` value; the `rec` slot holds only `v`, `t` and `next`. |

## RW. Routes, session and integrity (8.3)

| ID | Test |
| --- | --- |
| RW1 | Every route declares an access and integrity policy; the coverage and `url_map` sweeps include them; cross-origin POSTs are 403 and write nothing. |
| RW2 | No GET writes: database hash and `Set-Cookie` unchanged after every GET, with and without a recovery in progress. |
| RW3 | Disabled: every `/recover...` route is the generic 404 after the same-origin check; `/login` has no link. |
| RW4 | Signed in: GETs redirect home, POSTs start nothing; the session is unchanged. |
| RW5 | Another browser (no slot) posting the correct code: no row is read by code or address; the "no usable recovery" page. A forged slot (other keys, version, token shape, `next`) is ignored. |
| RW6 | Session transitions: sign-in and the pending second factor drop `rec` and keep the attempt markers; sign-out clears everything; `reg` and `rec` coexist independently. |
| RW7 | `Cache-Control: no-store` and `Referrer-Policy: no-referrer` on every recovery response; the code and password inputs carry the autocomplete hints of design 2.6. |
| RW8 | The full journey: request → 303 → verify page → code and password → `/login?recovery=done` shows the notice → sign-in with the new password reaches the validated `next`; no code is sent or asked at sign-in (D7). |
| RW9 | `/login?recovery=done` while recovery is disabled shows nothing; while enabled it shows the fixed text by lookup and echoes nothing (RD16). |

## RP. Production and gates (8.3)

| ID | Test |
| --- | --- |
| RP1 | With `recovery.PRODUCTION_ACTIVATION` off, production starts with and without mail settings and recovery is disabled; no environment variable enables it. |
| RP2 | With it on (patched), malformed mail settings are start-up errors naming the variable without its value; `console` and `file` are refused. |
| RP3 | The four gates are independent: for each combination of the registration, teacher, profile and recovery gates, the recovery routes answer by the recovery gate alone and the others are unchanged. |
| RP4 | Recovery gate turned off with rows pending: every route 404, a correct code redeems nothing, the next cleanup scrubs them after expiry. |
| RP5 | The Nginx template puts `/recover` and `/recover/resend` in `lp_register` and `/recover/verify` in `lp_login` (static, as the existing template tests). |

## RQ. CLI (8.2)

| ID | Test |
| --- | --- |
| RQ1 | Every existing `manage_accounts` command keeps its behaviour; `set-password` remains the operator's recovery and invalidates a pending recovery code (RK1). |
| RQ2 | The changed commands of design 6.5 report recovery counts and refuse a database whose recovery storage is incomplete with the "apply migrations first" error. |
| RQ3 | `registrations-cleanup` needs no `DZI_SECRET_KEY`; `clear-registration-limit` still does. |

## Mapping to registration test plan section M

| Registration test | Here |
| --- | --- |
| M1 (admin, password-less, disabled, never-verified get no code; same answer) | RE8, RG1, RC2 |
| M2 (eligibility rechecked at redemption) | RE1–RE7 |
| M3 (session-key changes void a code) | RK1, RE2, RE3 |
| M4 (success voids every other code, rotates the key, no session) | RV2, RV3, RX2 |
| M5 (only `users.email`; `email_at_link` never) | RE9, RI2 |
| M6 (shared total budget) | RL2, RL3 |
| Equivalents of D1–D11, DL1–DL10, DS1–DS8, DC1–DC8, DR1–DR9, E3–E5, F1–F3, G1–G6, H1–H7, N1–N5 | RC, RT, RL, RS, RCl, RR, RX, RW, RG, RF, RN |

## Manual and activation evidence (not automated; none done)

- **Browser**: the journey on a phone (reading the code) and a laptop
  (entering it); a second tab submitting after the first succeeded; the back
  button after success; password managers offering to save the new password
  and not the code.
- **Provider**: delivery of a recovery code and a notice to abv.bg, Gmail and
  a school mailbox; their measured durations through the relay (RD2 (a)), or
  the recorded acceptance of the timing oracle (RD2 (b)).
- **Capacity**: `set_password` under the writer lock on the server; send
  durations with registration and recovery sharing the budget; a class-sized
  run mixing both through Nginx.
- **Linux / systemd**: the cleanup timer scrubbing an expired recovery on a
  quiet server; the full restore script against a staging copy with a pending
  recovery, including an interrupted run and a run during a cleanup; the
  snapshot step on a copy with a pending recovery, then a test restore; the
  sender under the real unit (registration test plan L, unchanged).
