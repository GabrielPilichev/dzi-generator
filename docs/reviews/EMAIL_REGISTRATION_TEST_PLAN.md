# Verified email/password registration — test plan

Companion of `docs/reviews/EMAIL_REGISTRATION_DESIGN.md` (section numbers
below refer to it). Not approved; it follows the design's proposals and
changes with them. Revision 3, after the independent reviews of revisions
1 and 2.

Test ids (A1, D10, …) are local to this document. Decision ids of the design
are written "decision D7" to keep the two apart.

## Conventions

- Temporary databases built from migrations and canonical data
  (`tests/support.py`); `data/questions.db` is never read.
- Time is injected (`now`); no test sleeps for a limit or reads the real
  clock. The exceptions are the sender-process tests C5–C8 and C17–C19, which measure
  real elapsed time against stand-in children with the deadline, the reaping
  bound and the alarm shortened to fractions of a second.
- Mail is the in-memory backend. No test opens a socket; the SMTP code is
  tested against a fake `smtplib.SMTP` object.
- Concurrency tests use two connections to one database file and threads, as
  `tests/test_quiz_attempt_race_ownership.py` and
  `tests/test_google_oidc_replay.py` do. They must hold with more than one
  handler, and do not rely on the single synchronous worker.
- "Unchanged" tests are the existing suites, which must pass without edits
  other than sweep lists.
- Linux-only units and scripts are tested statically from any platform (as
  the existing template tests are) and exercised on Linux as activation
  evidence (section L). Their logic lives in Python functions tested
  portably.

Proposed files: `test_registration_migration_sql.py`,
`test_registration_forms.py`, `test_profiles_service.py`,
`test_mail_service.py`, `test_mail_sender_deadline.py`,
`test_registration_service.py`, `test_registration_ledger.py`,
`test_registration_settlement.py`, `test_registration_restore_gate.py`,
`test_registration_cleanup.py`, `test_registration_routes.py`,
`test_registration_production.py`, `test_account_profile.py`; additions to
`test_access_policy.py`, `test_session_cookie_samesite.py`,
`test_canonical_content.py`, `test_schema_baseline.py`,
`test_manage_accounts.py`, `test_oidc_accounts_service.py`,
`test_google_oidc_routes.py`.

## A. Schema and classification (PR 1)

| ID | Test |
| --- | --- |
| A1 | Migration applies to a database at the previous head and to an empty build; it is idempotent in the runner; `PRAGMA foreign_key_check` is clean. |
| A2 | No existing table is rebuilt and no existing row changes: `users` rows are byte-identical apart from the new NULL column `email_verified_at`. The same test is repeated in PR 6 for `profile_required_at`, if D11 (b) or (c) adds it: every account existing then has it NULL. |
| A3 | `account_type` rejects `admin` and any other value in both tables. |
| A4 | The student/teacher CHECK of `user_profiles`: student without grade, student with school, teacher with grade, teacher without school are each refused. |
| A5 | Name, grade, school and label bounds are enforced by the tables. |
| A6 | `pending_registrations`: a non-pending row with **any** of email, first name, last name, grade, school, subjects, password hash, code hash or code expiry is refused, one column at a time; duplicate `binding_hash` is refused; two pending rows for one address are accepted. |
| A7 | `registration_sends`: an unknown `kind` or `outcome` is refused; ids are never reused after a delete (AUTOINCREMENT). `registration_control`: a second row and an unknown hold reason are refused. |
| A8 | The five tables are classified runtime; a canonical export contains none of them; a fresh build has them empty. |
| A9 | Code that predates the migration (explicit column lists) still creates and loads users on the new schema. |

## B. Forms (PR 1)

| ID | Test |
| --- | --- |
| B1 | Each field: empty, minimum, maximum, maximum + 1, with the exact Bulgarian error. |
| B2 | Names: Cyrillic, Latin, hyphenated, apostrophe accepted; digits, control and bidirectional-override characters, emoji refused; whitespace stripped and collapsed. |
| B3 | Two 39-character names give a `display_name` that the `users` CHECK accepts. |
| B4 | Email: every shape `normalize_email` accepts but the public rule refuses (non-ASCII, no dot in the domain, doubled dot) is refused; upper-case input is normalised; inner whitespace is refused, never collapsed; a CR or LF anywhere is refused. |
| B5 | Grade: only 8–12 as integers; `8.0`, `08a`, 7, 13, a list, missing are refused. |
| B6 | Subjects: none, four, an unknown id, a duplicate, `other` without text, `other` text out of bounds. |
| B7 | Password: the existing `password_problem` cases, plus mismatch with the confirmation. Passwords are never echoed into the re-rendered form. |
| B8 | **Password exactness.** For each of `"  pupil  passphrase  "`, a password with a leading space, a trailing space, a doubled inner space, a tab, a non-NFKC character (for example a full-width letter) and a 256-character value: the validator returns the identical string; the stored hash verifies the original; after activation `authenticate` accepts the original and refuses the stripped, the collapsed and the NFKC-normalised variants. A 257-character password is refused, not cut. |
| B9 | Confirmation is compared exactly: `"abc  defghij"` against `"abc defghij"` is a mismatch. |
| B10 | `next`: stored unaltered when `safe_redirect_target` accepts it; an external URL, `//host`, a backslash, a control character and an over-long value are dropped, never repaired. |
| B11 | A submitted `role`, `account_type`, `is_admin`, `email_verified_at` or `profile_required_at` field changes nothing. |

## C. Mail and the deadline (PR 2)

| ID | Test |
| --- | --- |
| C1 | The message built for the fake SMTP object has one recipient, the configured sender, a UTF-8 plain-text body and no other part. |
| C2 | A subject or address containing a line break raises before anything is sent. |
| C3 | STARTTLS and implicit TLS verify certificates; `none` is refused for a non-loopback host. |
| C4 | The sender script against the fake SMTP object, by exit status and marker: refused connection, TLS failure, AUTH failure and refused sender → `10`–`19` and no output; refused recipient and refused data → `20` after `R`; a clean exchange → `0` after `R`. **The marker precedes the recipient:** the fake records the order of calls, and the `os.write` of `R` has returned before the recipient command is issued, in every path that issues it. |
| C5 | **Communication deadline.** With a stand-in child and the deadline shortened: (a) a child that writes nothing and blocks, (b) a child that writes `R` and then blocks, (c) a child that ignores `SIGTERM`, (d) a child that floods its output. In each case `send` returns within the deadline plus the reaping bound plus a fixed margin, the child's pid no longer exists and no pipe is left open. **Every one of them is `unknown`, (a) included:** a kill before the marker is not `not_sent`. |
| C6 | **Outcome rule, row by row** (stand-in children): exit `10` with no output → `not_sent`; `Popen` raising (a missing interpreter path) → `not_sent` and no process; exit `0` after `R` → `accepted`; exit `20` after `R` → `refused`; and `unknown` for each of: exit `0` without `R`, exit `20` without `R`, exit `10` after `R`, an unlisted status, death by a signal, output other than `R`, more than one byte. |
| C7 | **A child that dies between the recipient and the marker cannot be misread.** A stand-in that reports "recipient transmitted" through a side channel the test controls and is then killed before it would have written anything further: because the marker is written first, the parent has `R` and the outcome is `unknown`. The reverse order (recipient first) is asserted to be impossible by C4. |
| C8 | **Cleanup on every parent exit path.** An exception injected at each point after `Popen` returns (while writing the input: `BrokenPipeError`; inside `communicate`: `MemoryError`, `KeyboardInterrupt`, `SystemExit`): the child is gone, the pipes are closed, the exception propagates or is mapped to `unknown` as specified, and a later `send` works. |
| C17 | **Reaping bound.** A stand-in that cannot be reaped within the bound (the test replaces `wait`): `sender_unreaped` is logged, the outcome is `unknown`, and the next `send` in that worker creates no process and returns `not_sent` until the child is gone. |
| C18 | **Worker death and replacement.** A parent process that starts a send against a blocking stand-in is killed outright; a second parent then starts its own send. The test asserts what the design states and no more: both senders may be alive at once; the orphan ends within the shortened alarm; the second send completes normally; nothing is recorded for the orphan (its ledger row would stay `started`, DS7). |
| C19 | **The alarm.** The sender script, started with no parent that kills it and a peer that never answers, ends by itself at the alarm. A stand-in for a slow interpreter start shows the documented gap: the alarm counts from the script's first statement, not from process creation. |
| C12 | **Entry point.** The command is `[sys.executable, "-I", <absolute path>]`; the path exists and is absolute; run from a different working directory it starts. The script imports nothing from `web` or `src` (static check of its imports) and opens no file (instrumented `open`). Starting it by module name in isolated mode fails, which pins why it is not done. |
| C13 | **Process contract.** The child's argument list holds only the interpreter, `-I` and the path. Its environment holds only the allowed names: `DZI_SECRET_KEY`, `DZI_TOTP_KEY`, the Google and SMTP variables and the state paths are absent while the parent has them. Its standard error is the null device. A stand-in that lists its open descriptors reports only 0, 1 and 2 while the parent holds an open SQLite connection and a listening socket. |
| C14 | **Bounded input and output.** Input of 16 KiB + 1, truncated JSON and unknown keys end the child with a `10`–`19` status before any network call (the fake records none). The parent never reads more than 16 bytes of output. |
| C15 | Nothing logged at any level contains the recipient, the body or the SMTP password; `smtplib` debug output is never enabled; a traceback forced in the child reaches no log. |
| C9 | `console` and `file` backends are refused by a production start; a missing backend means registration is disabled, not an error. |
| C10 | Settings parsing: each malformed value is reported by name without echoing a secret; an hourly cap above 300 is refused. |
| C11 | `send` never raises for a delivery problem, and never retries: one call creates at most one process. |
| C16 | No test in this section asserts a bound on process creation, on the whole request, or on the number of senders across workers: the design claims none. |

## D. Pending registration and codes (PR 3)

| ID | Test |
| --- | --- |
| D1 | `create_pending` stores a scrypt hash and a keyed code hash; neither the password nor the code appears in any column of any table. |
| D2 | The code is six digits including leading zeros; a thousand generated codes are not constant (generator is wired to `secrets`). |
| D3 | Correct code activates a student registration: one `users` row with the normalised email, role `student`, `email_verified_at` set, `display_name` of the two names and a profile. `authenticate` then accepts the password. A teacher registration is never activated before PR 7 (K5); its role and subjects are tested there, as decision D5 decides. |
| D4 | **Final states are kept.** After activation the row is `activated` and scrubbed; verifying again with the same binding returns `NOT_PENDING`, writes nothing (database hash unchanged) and the row is still `activated`. The same for a `void` row (cancelled, replaced, address taken) and an `expired` row: each keeps its own state. Only a `pending` row past its time becomes `expired`. |
| D5 | Wrong code counts; five wrong codes, then the correct one is refused; a resend makes a correct new code work. |
| D6 | Malformed and empty codes count as attempts. |
| D7 | A code past ten minutes is refused without counting; a registration past thirty minutes is `expired` and scrubbed, whatever its code. |
| D8 | A resend invalidates the previous code, is refused inside sixty seconds and after three, and never sets a code expiry past the registration's. |
| D9 | The code of one registration does not verify another registration of the same address. |
| D10 | `create_verified_user` refuses role `admin`, role `teacher` (until PR 7), a missing email, and a hash that is not of the current method. |
| D11 | With a different `SECRET_KEY` no existing code or binding matches, and the address hash of the ledger differs. **This is an exception to the guessing bound, pinned as such:** an address at its daily cap is admitted eight more attempts after the key changes, and the codes issued before the change no longer verify. |

### D-L. Send ledger and caps (PR 3)

| ID | Test |
| --- | --- |
| DL1 | Every issuance, code or notice, inserts exactly one ledger row with `attempted_at = now`, in the transaction that stores the code, before any send. |
| DL2 | Hourly cap: the sixth attempt inside any 60 minutes is refused, counted across rows and across real and shadow rows, independent of other addresses; the sixth is admitted one second after the first leaves the window. |
| DL3 | Daily cap by issuance time: eight attempts spread so that each row is younger than its sends; the ninth is refused until 24 h 15 min after the first, and admitted one second later. |
| DL4 | **The first review's boundary schedule.** Row A sends at 0, 60, 120, 180 s; row B at 3601, 3661, 3721, 3781 s; four new rows submit at 86 401, 86 402, 86 403, 86 404 s. All four late submissions are refused. At 86 404 s exactly **7** issuances lie in the preceding 24 hours (A's first, at 0 s, has left it) and exactly **8** in the preceding 24 h 15 min, which is the window the cap uses. A companion assertion documents that counting by row creation time would admit all twelve. |
| DL5 | **Guessing bound, steady clock.** With a clock that only advances and is never stepped: a property test with a seeded generator, many random schedules of submissions and resends over several simulated days, each sending whenever admitted and probing one second either side of every cap boundary. For every instant, the number of codes that can be guessed in the preceding 24 hours (issued up to 10 minutes before it) is at most 8. Deterministic boundary cases accompany it: a ninth attempt at exactly `W`, one second before and one second after, for both caps. The test's name and docstring state the three assumptions of 3.3. |
| DL6 | A ledger row dated later than `now` counts inside every window. |
| DL7 | Total budget: the 301st attempt in an hour is refused for every address and logs the event once; the budget is shared by codes and notices. |
| DL8 | **Not refundable, isolated from the circuit.** 400 attempts in one hour, every one settled `refused` (which the circuit does not count): exactly 300 are admitted. For `not_sent` and `unknown` the same count is asserted with the circuit check replaced by a stub that always answers open-for-business, so that the budget alone is measured; DL9 covers the circuit. No outcome lowers a per-address count either: five `not_sent` attempts to one address exhaust its hourly cap. |
| DL9 | Circuit: three consecutive attempts settled `not_sent` or `unknown`, the newest under 5 minutes old → submit and resend answer "unavailable" before any hash, row or send (instrumented). An `accepted` attempt among the last three, or 5 minutes passing, reopens it. Three `refused` attempts never close it. Unsettled `started` rows are ignored. **Order is by generation id:** generations 1–3 fail, generation 4 is accepted, then a late settlement of generation 2 arrives: the circuit stays open for business. |
| DL9a | The circuit's stated limits, pinned: with two handlers, two attempts are admitted before either failure is settled; a sequence of attempts that are never settled (`started`) never closes the circuit, and the hourly cap still stops them at 300. |
| DL11 | **Issuance time is sampled under the write lock.** Two submissions for one address at its cap minus one, the first holding the lock while the second waits with an earlier clock reading in hand: the second is stamped with a time not earlier than the first's and is refused. A caller that passes a `now` read before `BEGIN IMMEDIATE` is caught by the route-level test H3. |
| DL10 | The caps and the circuit are read before hashing: a refused submission performs no scrypt computation. They are enforced again under the lock (E5). |

### D-S. Settlement (PR 3)

| ID | Test |
| --- | --- |
| DS1 | `settle_send` with `not_sent` on the generation that owns the live code: the code and its expiry are cleared, the wait is waived, `send_count`, the ledger row count and every cap are unchanged. |
| DS2 | **Delayed failure after a resend.** Send 1 issued, send 2 issued, then send 1 settled `not_sent`: the code of send 2, its attempt count, its expiry and its wait are unchanged, and the code of send 2 still verifies. |
| DS3 | **Delayed failure after verification.** The row is `activated`; settling its send `not_sent` changes neither the row nor the `users` row. |
| DS4 | **Delayed failure after cancellation, replacement and expiry.** The row stays in its final state, scrubbed. |
| DS5 | **Duplicate settlement.** The same outcome twice, and a second different outcome (`accepted` then `not_sent`, `not_sent` then `accepted`): the first wins, the second changes nothing in either table. |
| DS6 | `refused` and `unknown` settle the ledger row and change nothing in the pending row. |
| DS7 | A settlement that never runs: the ledger row stays `started`, still counts in every cap, and the code still verifies. |
| DS8 | Two settlements of one generation released together on two connections: exactly one reports that it settled. |

### D-A. Activation errors (PR 3)

| ID | Test |
| --- | --- |
| DA1 | An exception injected at each write of activation (the `users` insert, the profile, each subject, the state change) leaves no `users`, profile or subject row; the registration is still `pending` with the same code hash and the same attempt count as before the request. |
| DA2 | **Only a real collision is "address taken".** A profile CHECK failure, a subject foreign-key failure and a `display_name` rule failure, each forced in the test, are **not** reported as `EMAIL_TAKEN`: the exception propagates and DA1 holds. |
| DA3 | An `IntegrityError` from the `users` insert while a `users` row with that address exists (inserted by the test between the read and the insert through a hook) is `EMAIL_TAKEN`: the row is `void` and scrubbed, and the existing account is byte-identical. |
| DA4 | An `IntegrityError` from the `users` insert with no such row (forced) propagates. |
| DA5 | **Commit failure.** The commit of the verify transaction is made to fail: nothing is activated, the answer is the generic failure, never the confirmation, and under decision D7 (B) no session is issued. The registration is still `pending`. |
| DA6 | The service never commits or rolls back: every function leaves the connection in the transaction the caller opened (asserted with `in_transaction` before and after). |

### D-C. Clock policy (PR 3)

| ID | Test |
| --- | --- |
| DC1 | A row or code dated more than 300 seconds in the future is expired; within 300 seconds it works. |
| DC2 | Clock behind the high-water mark by more than 300 seconds: submit, resend and verify answer "unavailable"; no ledger row, no counted attempt, no state change. It ends when `now` passes the mark, and at once after `registrations-clock-accept`. |
| DC3 | A clock set back does not increase attempts per code. |
| DC4 | A clock set forward expires rows and codes early. A companion test **documents the unbounded case**: after a forward step of 24 h 15 min the address is admitted eight more attempts, and again after a second step. The test asserts that behaviour so that nobody later claims a bound for it. |
| DC5 | Forward then back with a cleanup run in between: the cleanup raised the high-water mark, so the return is DC2. |
| DC6 | Every issuance and every cleanup run raises the high-water mark and never lowers it. |
| DC7 | **The second review's small-step schedule, deterministic.** Real and wall time are separate in the test. Five codes at real 0–4 s; wall clock set back 300 s; the five codes still verify at real 899 s (wall 599 s); offset returned to zero at real 900 s and advanced 300 s at real 901 s; three codes admitted at real 3601–3603 s; five admitted at real 87 006–87 010 s. The test asserts that all 13 are admitted and that all 13 could be guessed within one real 24 hours (65 guesses). It exists so that the bound is never again stated without its steady-clock assumption; no step exceeds the 300 seconds of DC1 and DC2, and none is refused. |
| DC8 | Each step of DC7 alone: a backward step of 300 s extends the real time in which a code verifies by 300 s; a forward step of 300 s shortens every window by 300 s of real time. |

### D-R. Restore gate (PR 5; the functions are portable)

DR1–DR9 apply under either option of decision D20. DR10–DR15 apply only
under D20 (b) and are deleted from the plan if (a) is chosen.

| ID | Test |
| --- | --- |
| DR1 | **Cancellation is not undone.** Copy the database; cancel a registration on the original; run `restore_gate` on the copy. The copy has no `pending` row and the original code verifies nowhere. Without the gate (asserted as the defect) the copy's row is live. |
| DR2 | **Spent attempts are not returned.** The same with five wrong codes after the copy was taken. |
| DR3 | After the gate the hold `restore` is set. Submit, resend and recovery answer "unavailable"; verification has nothing to verify. |
| DR4 | **No automatic release.** With the hold set, advancing the clock by 24 h 15 min, by a week, and stepping it forward by a year releases nothing. Only `registrations-release-hold --acknowledge-lost-history` does; without the flag the command refuses and changes nothing. |
| DR5 | **A hold is never cleared by the gate.** A copy that already carries a hold keeps it with its original time; a replaced database that carries a hold makes the result held; a second run of the gate changes nothing (database hash unchanged). |
| DR6 | The gate is one transaction: an error injected at each of its statements leaves the copy byte-identical, and the command exits non-zero. It touches no `users`, profile or subject row and scrubs every column of A6. |
| DR7 | `registrations-restore-status --expect held` fails while a pending registration or recovery credential exists, when the control row is missing, and when the hold is clear; it passes after the gate. It writes nothing. |
| DR8 | `registrations-cleanup` refuses to run while the restore lock exists, and writes nothing. |
| DR9 | An ordinary restart (connections closed and reopened, a new application object) keeps a pending registration usable with its code, and sets no hold. |
| DR10 | (b) Complete evidence: same `ledger_id`, every recent row of the copy present in the replaced database, no hold on either side. Afterwards every ledger row of the replaced database younger than 48 hours is present with its id, none is duplicated on a second run, the id sequence is past the highest id, the high-water mark is the later of the two, and the hold is clear. An address at its daily cap before the restore is still at its cap after it. |
| DR11 | (b) **Settlements are merged.** A generation that is `started` in the copy and `not_sent` in the replaced database is `not_sent` afterwards, with its settlement time; three such failures leave the circuit closed after the restore. A generation settled in the copy is never returned to `started`. |
| DR12 | (b) **A readable database is not complete evidence.** Each of these leaves the ledger unmerged and the hold set, with the reason reported: a different `ledger_id`; a missing `ledger_id`; a recent row of the copy absent from the replaced database; a row with the same id and a different address hash, kind or time; a replaced database whose high-water mark is earlier than the copy's, or ahead of the present by more than 300 seconds; a failing `quick_check`; an unexpected migration record; a missing or unreadable file. |
| DR13 | (b) **Second restore.** Restore without the replaced database (held); restore again, giving the first result as the replaced database. The result is held: the hold was carried, although the file is readable and its ledger matches. |
| DR14 | (b) The replaced database is opened read-only and is byte-identical after the gate. |
| DR15 | (b) `registrations-restore-status --expect merged` passes only for a clear hold with a consistent `ledger_id` and id sequence. |

## E. Collisions and concurrency (PR 3)

| ID | Test |
| --- | --- |
| E1 | Two registrations of one address, both verified: the first creates the account, the second returns "address taken", is voided, and the first account's hash is unchanged. |
| E2 | The same, with the two verifications released at the same moment on two connections: exactly one account, no exception escapes, no orphan profile. |
| E3 | Two simultaneous correct verifications of one registration: one account. |
| E4 | Five wrong codes sent concurrently on one registration count as five; a sixth is refused. |
| E5 | Concurrent submissions for one address cannot exceed a cap; concurrent submissions for different addresses cannot exceed the total budget. |
| E6 | The CLI's `create` for the same address racing an activation: one account. Whichever loses reports its normal error (the CLI its "already exists", the activation `EMAIL_TAKEN`). The test does not assume the CLI opens `BEGIN IMMEDIATE`; it uses the CLI's real code path. |
| E7 | Shadow: an existing address (each role, active and disabled) yields a row with no password hash; expiry, attempt counting, the attempt limit and resend behave as for a real row; no code activates it, including one forced to match in the test; the existing account's row is byte-identical afterwards. |
| E8 | Registration with an address that exists only as `email_at_link` of a Google-created account proceeds and creates a separate account; the Google account and its identity are untouched. |
| E9 | Google `sign_in_or_onboard` with the address of a registered account returns `email_exists`; with the address of a merely pending registration it creates the Google student, and the later activation still succeeds as a separate account. |
| E10 | No function in the registration service reads or writes `user_identities`. |
| E11 | A resend racing a late settlement of the previous send, on two connections, in both orders: the newer code survives (DS2 under concurrency). |

## F. Routes, session and integrity (PR 4)

| ID | Test |
| --- | --- |
| F1 | Every new route declares an access policy and, if unsafe, `same_origin`; the coverage and `url_map` tests pass with them. |
| F2 | A cross-origin POST to each route is 403 and writes nothing. |
| F3 | No GET writes: database hash and `Set-Cookie` are unchanged after every GET, with and without a registration in progress. |
| F4 | Disabled: every `/register...` route is the generic 404, and the sign-in page has no registration link. |
| F5 | The full student journey up to activation: submit → 303 → verify page shows the address → code → account exists. **Then, by decision D7:** (A) the confirmation page links to `/login?next=`; signing in with the password reaches the validated `next`; no account session exists at any point before `/login`. (B) the verifying response redirects to the validated `next` with an account session issued by `issue_account_session`, only after the commit. Only the variant Gabriel approves is implemented; the other is deleted from the plan then. |
| F6 | After verification the cookie has no `reg`. Under (A) it has no `uid`, `sk`, `iat`. Under (B) it has exactly those of the new account, the role is `student` or `teacher`, and no admin second-factor state is ever skipped (an activation cannot produce an admin, I1). |
| F7 | Before verification every protected route answers as for an anonymous visitor; `/login` with the registered credentials fails with the standard message. |
| F8 | Another browser (no cookie) posting the correct code: no row is read by code, nothing is created, the standard "started in another browser" answer. |
| F9 | A second submission in one browser voids the first registration; its code no longer works. |
| F10 | A forged or re-signed `reg` slot with a wrong version, a non-string token or extra keys is ignored. |
| F11 | A signed-in account on each **`/register...`** route: GET redirects to its home; POST creates no row. Its session is unchanged. The same account can open and submit `/account/profile` (J5): the redirect rule does not reach it. |
| F12 | Session transitions, per the existing contract. Sign-in (`issue_account_session`) and the pending second factor (`issue_pending_second_factor`) drop `reg` and **keep** the `quiz_attempt_owner:*` markers. Sign-out (`end_session`) clears the whole session: `reg` and the markers are both gone. |
| F13 | `next`: an external URL, `//host`, a backslash and an over-long value are dropped at render and at use; the return target after activation is always a GET that starts no attempt. |
| F14 | `/register/verify`, `/resend`, `/cancel` responses carry `no-store` and `no-referrer`; the address is in no URL, cookie or log line. |
| F15 | Cancel voids and scrubs the row and removes the slot. |
| F16 | The teacher route is 404 while its gate is off, with the student gate on. Before PR 7 it is 404 in every environment and no test configuration turns it on (see also K5). |
| F17 | A storage failure during verify (DA1 at route level) renders the generic failure with status 500, keeps `reg`, shows neither "address taken" nor the confirmation, and a retry with the same code then activates. |

## G. Enumeration (PR 4)

For an address with an account and one without, submitted with otherwise
identical input. These tests establish the claims of 3.7 and no more; they do
not assert equal delivery or equal duration.

| ID | Test |
| --- | --- |
| G1 | Status, `Location`, response body, header set and cookie structure are identical at submit, verify (wrong code), resend, and at every cap. |
| G2 | The same number of scrypt computations, pending rows, ledger rows and send attempts (counted by instrumented fakes). |
| G3 | **Content-dependent rejection.** The backend is told to refuse notices and accept codes, then the reverse (`refused`): in both, the responses of G1 are still identical, the pending rows differ only in the columns that differ anyway (ids, hashes), the per-address and total counts are equal, and a following resend is admitted or refused alike. The same with `unknown` for one kind only. |
| G4 | `not_sent` for both kinds gives the same visible failure, the same retired code and the same waived wait. A sender killed at the deadline before its marker, for either kind, is `unknown`: no visible failure, the code kept, the wait applied. The visible failure therefore never depends on what was given to the mail server. |
| G5 | **Latency.** The backend is told to take a different stated time for a code and for a notice. The responses and all accounting are identical. The test also records, through an instrumented clock, that the application adds no account-dependent step of its own between receiving the request and calling `send`, and between `send` returning and the response. It asserts nothing about the backend's time, which the application does not equalise (3.7). |
| G6 | The circuit under a one-sided stall (three notices `unknown`): registration closes for both kinds alike. The residual of 3.7 is thereby pinned as known behaviour. |
| G7 | The notice goes to the existing address and contains no name, role or link; the existing account receives no code. |
| G8 | `/login` answers the standard message for an unverified registration's credentials, an unknown address and a wrong password alike. |
| G9 | Only if decision D8 (b) is chosen: the shadow path sends nothing, and `not_sent` is never shown for either kind. |

## H. Delivery at route level (PR 4)

| ID | Test |
| --- | --- |
| H1 | `not_sent` at submit: the row stays pending without a live code, the page offers a resend, a resend at once is admitted, and the address has used two of its attempts, not one. |
| H2 | `unknown` and `refused`: the normal verify page, the code stays valid, the wait before a resend applies. |
| H3 | The mail backend is called after the commit and with no transaction open on the request's connection. The `now` given to the issuing function was read after `BEGIN IMMEDIATE` returned (instrumented clock and connection). |
| H4 | An exception escaping the backend is handled as `unknown` and never shows a traceback or the address. |
| H5 | A failure of the settlement transaction leaves the ledger row `started`, a usable code and a logged class name; the response is the normal verify page. |
| H6 | A request killed between the send and the settlement (simulated by not running it): DS7. |
| H7 | `unknown` is never followed by a second send in the same request, and the person's next resend is a new generation with a new code, admitted only after the wait and counted against every cap. |

## I. Roles and escalation

| ID | Test |
| --- | --- |
| I1 | No request to any registration or profile route, with any body, produces a `users` row with role `admin`. |
| I2 | The student route always yields `student`; a tampered hidden field or a `/register/student` body with teacher fields does not change it. From PR 7 the teacher route always yields what decision D5 decides and nothing a field can change. |
| I3 | (PR 7, under decision D5 (A)) A registered teacher gets exactly the existing teacher access matrix: 403 on `/teacher`, `/dzi`, `/teacher/dzi-training`; 404 on another teacher's and on unowned assignments. |
| I4 | A registered student gets exactly the existing student matrix. |
| I5 | `/account/profile` cannot change `role`, `email`, `email_verified_at`, `is_active`, `profile_required_at` or another account's profile. **Which fields it accepts follows the stored profile type, not the current role:** an account with a student-type profile cannot store school or subjects, and one with a teacher-type profile cannot store a class, each tested with the current role equal to the profile type and, after `set-role`, different from it (a student-type profile on a role-`teacher` account; a teacher-type profile on a role-`student` account). An account with no profile is offered the student form only while its current role is `student`; with role `teacher` or `admin` a POST creates nothing. |
| I6 | Profile data plays no part in any access decision: an account with a teacher profile and role `student` is a student everywhere, and the reverse. |
| I7 | Quotas (PR 7): the approved limits refuse creation past them, per teacher, without affecting another teacher or an admin. |

## J. Existing accounts, Google and profiles (PRs 4 and 6)

| ID | Test |
| --- | --- |
| J1 | The existing account, TOTP, throttle, session, cookie, ownership and Google suites pass unchanged. |
| J2 | A CLI-created **teacher** (no profile, `email_verified_at` NULL) signs in, changes password, links Google and manages assignments as before, and is never redirected to the profile page. |
| J3 | A CLI-created **admin** signs in with password and second factor as before and is never redirected to the profile page. **Google linking stays refused for an admin:** the link start and `confirm_link` refuse it exactly as before this milestone. The same holds for an account that registered by email and was later promoted to admin. |
| J4 | `manage_accounts`: every existing command behaves as before; `list` shows the new columns; `set-role admin` on a registered account still requires the CLI and a password; the new commands touch only the registration tables (and `require-profile` one column of one row). |
| J5 | Existing `quiz_attempts.student_name`, `user_id` and `quiz_assignments.owner_user_id` values are unchanged by registration, activation and profile edits; a profile edit does not rotate `session_key` or remove attempt markers. |
| J6 | **Profile gate off:** `/account/profile` is 404; Google onboarding redirects exactly as before; a profile-less student starts an attempt exactly as before; no `profile_required_at` is written. |
| J7 | **Profile gate on, onboarding:** a student created through Google is marked and sent to `/account/profile` with `next` preserved; completing it stores the profile and updates `display_name`; no email or password is requested or stored. |
| J8 | **New attempt versus resume.** A marked, profile-less student: (a) starting a new attempt is redirected to the profile page and no attempt row is created; (b) with an unfinished attempt at that assignment, the same POST resumes it; (c) with a submitted attempt, it goes to the result; (d) `/quiz/attempt/<id>` GET and POST, submitting and the result page all work; (e) on a closed assignment with no attempt, the closed notice is shown, not the profile page. |
| J9 | After completing the profile the student lands on the start page (a GET) and starts the attempt by pressing the button; an unsafe `next` becomes the role's home. |
| J10 | **Nothing retroactive.** With the gate on, a CLI-created student and a Google student created before the gate was on (mark NULL) start attempts normally and are never redirected; the profile page is available to them and optional. |
| J11 | Only under decision D11 (c): `require-profile --id` marks one student, who is then handled as J8; no command marks every student. |
| J12 | **Role changes.** A marked, profile-less student promoted to teacher or admin is no longer redirected; demoted again, is redirected again. A teacher with a teacher profile demoted to student is not redirected. `set-role` writes neither `user_profiles` nor the mark. |
| J13 | A student with a profile, a teacher and an admin start attempts normally in every gate state. |
| J14 | A registered student or teacher links Google through the existing confirmation flow; nothing links automatically when a Google address equals a registered address. |
| J15 | **The three gates are independent.** For each of the eight combinations of the registration, teacher and profile gates, with Google enabled and disabled: registration routes answer by the registration gate alone; `/account/profile`, the onboarding redirect and the new-attempt check answer by the profile gate alone; Google sign-in behaves as before except for the onboarding redirect and mark, which exist only with the profile gate on. In particular: registration on with profiles off creates accounts with profiles that cannot yet be viewed and blocks no attempt; profiles on with registration off marks and asks Google students only. With all three off the suites of J1 see no difference. |

## K. Production configuration and gates (PR 4, PR 6)

| ID | Test |
| --- | --- |
| K1 | With the registration constant off, production starts with and without mail settings, registration is disabled, and one warning names the settings. |
| K2 | With it on (patched in the test), malformed mail settings are startup errors that name the variable and echo no secret. |
| K3 | No environment variable turns on registration, the teacher form or profiles in production while a constant is off. |
| K4 | The Nginx template has the `lp_register` zone on the submit and resend addresses and `lp_login` on verify (static test, like the existing template tests). |
| K5 | **Teacher gate through activation** (PR 7 for the gate-on half; before it, every teacher registration is refused). A teacher registration is created with the gate on; the gate is then off. `verify` with the correct code creates no account, voids and scrubs the row and answers "unavailable"; so does `resend`, which sends nothing. No student account results either. A student registration pending at the same moment still activates. |
| K6 | **Registration gate turned off with rows pending.** Every route is 404, a correct code posted to `/register/verify` activates nothing, and the next cleanup run scrubs the rows when they expire. |
| K7 | A restore hold, an open circuit, a used total budget and a regressed clock each answer "unavailable" (not 404) on submit and resend, write nothing, and do not depend on the address. |

## L. Manual and activation evidence (not automated, none done)

- A real send through the chosen provider to an address Gabriel owns, and
  delivery checks to abv.bg, Gmail and a school mailbox.
- SPF, DKIM and DMARC of the sender domain; verified TLS to the relay.
- **Forced hang:** the SMTP host pointed at an endpoint that accepts the
  connection and never answers, and at one that answers one line at a time
  forever. Each request ends after the communication deadline, no sender
  process remains (`ps`), and the next request is served.
- **Worker replacement under the real unit:** the worker killed outright
  during a send. The orphaned sender is gone within its lifetime, the
  replacement worker serves, and the registration's code still works if the
  message arrived.
- **Descriptor and environment isolation under the real unit:** the sender's
  open descriptors (`/proc/<pid>/fd`) and environment (`/proc/<pid>/environ`)
  hold nothing of the worker's.
- **Capacity:** measured duration of one send on the server, and thirty
  submissions at once through Nginx, against the criterion of 3.5.
- Durations of a code and of a notice through the chosen provider (3.7).
- A classroom-sized registration through Nginx to tune the zone; a second
  class from the same address inside the hour; trusted client-IP handling;
  the backend unreachable except through Nginx.
- Linux-only: the cleanup timer firing on a server with no traffic and
  scrubbing an expired registration; the full restore script with its gate
  against a staging copy, including a run interrupted after the gate, a run
  started while a cleanup is executing, and a second restore on top of a held
  one; the snapshot step followed by a test restore of that snapshot; the
  sender process under the unit's sandboxing. None can be run from the
  development Mac.

## M. Password recovery (PR 8, only under decision D10 (a))

Planned with its addendum. At minimum the equivalents of D1–D11, DL1–DL10,
DS1–DS8, DC1–DC8, DR1–DR9 (and DR10–DR15 under D20 (b)), E3–E5, F1–F3,
G1–G6, H1–H7 and N1–N5 for the
recovery credentials, plus:

| ID | Test |
| --- | --- |
| M1 | An admin, a password-less account, a disabled account and (unless decided otherwise under D10) an account whose address was never verified receive no code, with the same visible answer as any other address. |
| M2 | **Eligibility is rechecked at redemption.** A code issued to a teacher who is then promoted to admin redeems nothing; the admin's password and second factor are unchanged. The same for an account disabled, or whose role changed, after the code was issued. |
| M3 | A code issued before a password change, an operator sign-out or a TOTP reset (each rotates `session_key`) redeems nothing. |
| M4 | A successful redemption voids every other pending recovery code of that account, including one issued in another browser; rotates `session_key`; issues no session; the old password stops working and other devices are signed out. |
| M5 | A code is sent only to `users.email`. An account whose address appears only as `email_at_link` receives nothing, and that address finds no account. |
| M6 | Recovery sends count against the same total budget as registration sends. |
| M7 | Under decision D10 (b) there is no recovery route at all: the tests assert its absence, and `set-password` keeps working. |

## N. Retention and cleanup (PR 3 for the function, PR 5 for the units)

These tests are about **logical** retention: what a query returns. None
asserts that bytes are gone from a live database file or its WAL; the design
claims no such thing (6.5).

| ID | Test |
| --- | --- |
| N1 | **Quiet site.** A registration is submitted; no further request of any kind is made; `cleanup` is called at 31 minutes as the timer would. The row is `expired` and every column of A6 is NULL. No submission was needed for it. |
| N2 | `cleanup` deletes final rows 24 hours after creation and ledger rows 48 hours after the attempt, and nothing younger; it touches no other table; it raises the high-water mark. |
| N3 | `cleanup` scrubs pending rows whatever the gates are, including teacher rows after the teacher gate was turned off. |
| N4 | **The target's conditions, pinned.** (a) A run that cannot take the write lock (held by another connection past the busy timeout) scrubs nothing, exits non-zero and leaves `--check` failing once the row is an hour old. (b) With the clock set back by 20 minutes after submission, a run at real 31 minutes scrubs nothing; the row is scrubbed when the wall clock passes its expiry. Neither is repaired by the application. |
| N5 | `registrations-cleanup` needs no `DZI_SECRET_KEY`; `--check` exits non-zero when a `pending` row is older than an hour and zero otherwise. |
| N6 | Static: the timer unit runs every 10 minutes, carries the restore-lock condition, and its service runs the cleanup command as the service user. The restore script stops the cleanup timer and requires the cleanup service to be inactive or failed before it copies; calls the gate after its migration step and **before** its final check-and-fingerprint step; calls the status check in that step and again after the replacement; and starts the timer again at the end. No gate command appears after the fingerprint. |
| N7 | Snapshot step, by decision D18. **(a)** nothing to test. **(b)** a snapshot made while a registration is pending has no `pending` row and no value in the columns of A6; the live database is byte-identical before and after; accounts, profiles and the ledger are the same in both. The test asserts nothing about free pages. **(c)** as (b), and the published file is the rebuilt one: it passes `quick_check` and `foreign_key_check`; its schema text, migration record, row count per table and id sequences equal the copy's; it has no free pages; a marker seeded into a free page of the copy is absent from it; the intermediate copy is gone; and any recorded checksum matches the published file. |
| N8 | Under (c), a rebuild whose validation fails (a row count forced to differ) publishes nothing and exits non-zero, as every other snapshot failure does. |
| N9 | The live backup path is unchanged: the snapshot's database is still produced by the online backup API, tested by the existing backup suite without edits. |
