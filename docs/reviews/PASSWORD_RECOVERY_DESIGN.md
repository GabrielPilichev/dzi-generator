# Password recovery by emailed code — design addendum (registration PR 8)

Scope and status: `docs/reviews/PASSWORD_RECOVERY_SCOPE.md`. Tests:
`docs/reviews/PASSWORD_RECOVERY_TEST_PLAN.md`. Parent design:
`docs/reviews/EMAIL_REGISTRATION_DESIGN.md` (cited below as "registration
design", with its section numbers), whose section 5.2 requires this addendum
and whose decision D10 approved self-service recovery by emailed code as PR 8.

**Proposal, revision 2. Not approved, not authorized.** Revision 1 (`745a952`)
was independently reviewed and returned with REQUEST CHANGES; section 0.4 maps
each finding to this revision. Section 0.1 restates the requirements Gabriel
approved before this addendum was written, quoting the approved documents
where they are the source. Everything else is a proposal: section 0.3 lists
the open decisions with a recommendation each, marks the three that would
**amend** an approved requirement (RD4, RD7, RD10) and the two that rest on an
**interpretation** of approved wording (RD2 (a), RD17), and nothing in this
document is labelled selected or approved unless section 0.1 says so. No
migration number is reserved. Nothing here authorizes an implementation, a
deployment or an activation; every production gate stays off.

Existing behaviour is referenced, not restated. "Unchanged" means the
registration design, `docs/ACCOUNTS_DESIGN.md`, `docs/AUTH_ROUTE_CONTRACT.md`
and the code at `513b36c` (branch `docs/password-recovery-design`, whose code
equals `3d4306a`, PR #341 merged).

## 0. Summary

### 0.1 Requirements approved by Gabriel (input to this addendum)

These come from D6, D7, D10, D20 and registration design 3.4 and 5.2, and
from the brief of this addendum. They are restated so that the proposals can
be checked against them; this document does not change them. Where the source
is an approved document the requirement is **quoted** from it (registration
design at `3d4306a`, the base of this branch); rows marked "brief" restate the
brief of the addendum, which is not a repository document.

| # | Source | Approved requirement |
| --- | --- | --- |
| A1 | D7, 5.2 | Quoted from 5.2: "It is a separate flow: its codes are not registration verification repeated, and sign-in never asks for one." |
| A2 | D7 | Quoted: "Later sign-in is email and password only; no code is sent or asked at login. Admin TOTP is unchanged." |
| A3 | 5.2 | Quoted: "request by email, the same visible answer for every address, known or not, with the limits of 3.7 on what \"same\" covers;" |
| A4 | 5.2, brief | Quoted from 5.2: "a code to the account's `users.email`, never to `email_at_link`". The brief words it as "recovery sends only to `users.email`". RD2 (a) needs Gabriel to read the requirement as governing the **code** (the credential), which is how 5.2 words it, and to allow a credential-free notice to the address the person typed; RD2 (b) keeps the brief's wording literally. |
| A5 | 5.2 | Quoted: "**eligibility is decided again at redemption**, under the write lock, from the account as it is then: it exists, is active, has role `student` or `teacher`, has a password, and its `users.email` is still the address the code was sent to. A code issued before a promotion to admin, a disable or a role change redeems nothing." |
| A6 | 5.2 | Quoted: "An admin never recovers by email, whatever the account was when the code was issued: the CLI and TOTP reset stay the only way;" |
| A7 | 5.2 | Quoted: "a recovery code is void once the account's `session_key` differs from the one recorded when it was issued (a password change, a disable, a role change, an operator sign-out and a TOTP reset each rotate it);" |
| A8 | 5.2 | Quoted: "success calls `accounts.set_password`, which rotates `session_key`, voids **every** other pending recovery code of that account, and issues no session;" How this addendum proposes to meet "voids" is an interpretation for Gabriel to confirm (RD17). |
| A9 | 5.2, brief | Operator recovery through the CLI (`manage_accounts set-password`) and admin TOTP handling are preserved. 5.2: "Operator-assisted recovery remains available through the CLI in the meantime". |
| A10 | 5.2 | Quoted: "a code to the account's `users.email`, never to `email_at_link`, with the generation, storage, limits, send ledger, settlement, clock policy, restore gate, cleanup and logging of sections 3, 4 and 6. Recovery sends count against the same total budget;" Registration's generation is six digits (D6, 3.1) and its settlement retires a `not_sent` code and waives the wait (4.3); RD4 and RD7 would amend those. |
| A11 | D20, 3.9, brief | Recovery is integrated with cleanup, the clock policy and holds, the restore gate and status, and the isolated snapshot scrub. D20 quoted: "every restore voids pending credentials and holds registration (and recovery) until the operator releases it, acknowledging the lost history." |
| A12 | brief | Every production gate stays off. |
| A13 | 5.2 | Quoted: "whether an address that was never verified is eligible is not covered by the approval of D10. Until Gabriel decides it in the addendum such accounts get no code and the same visible answer." |
| A14 | D15, brief | Out of scope unless Gabriel says otherwise: email change, adding a password to a Google-only account, identity linking or merging, admin recovery, TOTP reset. |
| A15 | D6 | Quoted: "Six-digit code valid 10 minutes, five guesses per generation; three resends at least 60 seconds apart; pending registration lifetime 30 minutes; 5 sends per address per hour, 8 per 24 h 15 min, and the reviewed global cap of 300 per hour." Through A10 these are the limits recovery inherits; RD4 would amend the code length for recovery. |
| A16 | 3.4 | Quoted: "`login_throttle` is reused where its policy fits (sign-in and, later, recovery attempts). It is not used for these caps: its window and its cleanup are fixed at fifteen minutes for every row." RD10 would amend the words "and, later, recovery attempts". |

### 0.2 The proposal in brief

1. `POST /recover` takes one field, the email. Under the writer lock the server
   creates a **recovery row** in a new table `password_recoveries` and decides
   eligibility (2.1). An eligible account gets a code, bound to its user id and
   to a keyed digest of its current `session_key`. Any other address gets a
   **shadow** row whose stored code hash no digits can match, exactly as
   registration does for an existing address (registration design 2.5).
2. The browser holds a random token in a new session slot `rec`; the row holds
   its keyed hash. Only that browser can redeem, and only with the code from
   the mailbox.
3. Every issuance is a row of the **existing** send ledger
   `registration_sends`, extended by one nullable column `recovery_id`. The
   per-address caps, the 300-per-hour total budget, the circuit and the
   48-hour ledger cleanup therefore count registration and recovery mail
   together, through the code that counts them today. No parallel counter
   exists.
4. `/recover/verify` takes the code **and** the new password (twice) in one
   POST. One `BEGIN IMMEDIATE` transaction checks the code, rechecks
   eligibility and the session-key digest, calls the unchanged
   `accounts.set_password`, clears the address's sign-in throttle only if RD8
   (b) is selected, and closes the row. No session is issued; the person is
   sent to `/login`. Every other pending recovery of the account is left
   `pending` but can no longer reset anything: its session-key digest no
   longer matches the rotated key, so a correct code for it ends
   `INVALIDATED` (RD17, an interpretation of A8). Nothing another browser can
   read or try before a correct code changes (1.4, 2.4).
5. The send runs through the existing sender process with its 8-second
   deadline. A recovery send's outcome is recorded in the ledger only; it never
   changes a recovery row and never changes what the browser sees (RD7, an
   amendment of A10).
6. The restore gate, restore status, snapshot scrub and cleanup timer reach the
   new table through the CLI commands and the backup module they already use;
   the restore script is not edited. A restore also brings back old password
   hashes and session keys; a read-only comparison and a reviewed repair
   precede reopening (6.6, RD18).
7. A separate build gate `recovery.PRODUCTION_ACTIVATION`, `False`.
8. Built in three scopes (10): storage and operations first, with nothing that
   creates a recovery row; then the service, with no caller; then the routes
   and the gate. No build that can create a recovery row lacks the cleanup,
   restore and snapshot integration.

### 0.3 Open decisions (proposals with recommendations; none approved)

"Kind" says what selecting the recommendation would do to the approved
requirements of 0.1: **Proposal** (fills a gap they leave open),
**Amendment** (changes an approved requirement; selecting it is Gabriel's
amendment of that requirement, and until then the approved text stands), or
**Interpretation** (reads approved wording in a way Gabriel must confirm).
"Blocks" says what a decision holds up: an implementation scope of section 10
(8.1 storage and operations, 8.2 service, 8.3 routes), or only activation.

| # | Kind | Question | Recommendation | Main alternative | Blocks |
| --- | --- | --- | --- | --- | --- |
| RD1 | Proposal (A13 is the default) | Are never-verified addresses (`email_verified_at` NULL) eligible? | **No.** They get the shadow treatment of RD2. Legacy accounts keep operator recovery (1.3). | Eligible; or eligible after an operator attestation command (not designed). | 8.2 only if the alternative is chosen; the approved default (A13) is what is built. Activation: Gabriel's confirmation. |
| RD2 | (a) Interpretation of A4; (b) Proposal | What does an address that may not recover receive? | **(a) One neutral notice** to the typed address, issued, counted, sent and settled exactly like a code. This needs Gabriel to confirm that A4 governs **codes** (the credential) and does not forbid a credential-free notice to the address the person typed. | **(b) Nothing**, with identical accounting (the ledger row is written and never sent). Keeps the literal wording of the brief; with current synchronous dispatch eligibility is likely distinguishable by response time, not measured (1.4); send-related log lines exist for real rows only (section 8.1). | 8.2 (the shadow dispatch path) |
| RD3 | Proposal | One step (code and new password together) or a separate reset credential? | **One step**, one transaction (2.4). | Two steps with a short-lived reset token (rejected in 2.4). | 8.1 (the table's states and columns) |
| RD4 | **Amendment** of A10 and A15 (D6: six-digit code) | Limits | **D6's values unchanged** (30 min recovery, 10 min code, 5 attempts per code, 4 sends 60 s apart, 5 per address per hour, 8 per 24 h 15 min, the shared 300 per hour), **except the code length: 8 digits for recovery codes** (5.4). Registration codes stay six digits. | Six digits as approved: 4×10⁻⁵ per day per targeted account, about 1.5 % over a year of daily attack (5.4). | 8.2 |
| RD5 | Proposal | Per-address budget shared with registration or separate? | **Shared**: the same ledger rows under the same keyed address hash (5.2). | Separate budgets: doubles the unwanted mail an address can receive, gains nothing (5.2). | 8.2 |
| RD6 | Proposal | Circuit history shared or separate? | **Shared**: one relay, one history (5.3). | A recovery circuit of its own: doubles the failing attempts admitted. | 8.2 |
| RD7 | **Amendment** of A10 (the settlement of registration design 4.3) | What does a `not_sent` recovery send change? | **The ledger only.** No visible message, no waived wait, the code kept (4.5). | The approved settlement of 4.3 (message, retired code, waived wait): an eligibility oracle under RD2 (b), and under RD2 (a) a second code path for no gain. | 8.2 |
| RD8 | Proposal (**recommendation changed in revision 2**) | Clear the address's sign-in throttle on a successful reset? | **(a) No.** A reset neither reads nor writes `login_throttle`. Clearing it is observable without a code by anyone who keeps the address's sign-in locked (4.4), which is the kind of disclosure RD17 removes from recovery's own pages. | **(b) Yes**, in the same transaction, as `/account/password` does today: a person who locked themselves out does not wait up to 15 minutes after the reset; the reset becomes observable at `/login` as described in 4.4. | 8.2 (small) |
| RD9 | Proposal | Send a "your password was changed" notice after a reset? | **Not in PR 8** (section 8.2). | Yes: a second send path after the commit with its own accounting. | A later scope only if yes |
| RD10 | **Amendment** of A16 (registration design 3.4) | Count wrong recovery codes in `login_throttle`? | **No** (5.6). Selecting it amends the approved sentence of registration design 3.4 ("and, later, recovery attempts"); the proposed amendment is appended there, labelled, and the approved sentence stands until Gabriel selects it. | Yes, as 3.4 anticipated: lets anyone lock a victim out of sign-in by typing wrong recovery codes. | 8.2 |
| RD11 | Proposal | Does a resend decide eligibility again? | **Yes**: every issuance decides it and binds afresh; a row may change between real and shadow (2.3). | Freeze the decision at the first issuance. | 8.2 |
| RD12 | Proposal | Cancellation while a restore hold is set | **Allowed**, mirroring the approved registration behaviour (registration design 0.7). | Refuse during a hold. | 8.2 |
| RD13 | Proposal | Snapshot and restore schema checks | **An allow-list** of the table's columns; an absent table is accepted only if its migration is not recorded (6.4). | Registration's rule (required columns only). | 8.1 (the same scope as the scrub and the checks it governs) |
| RD14 | Proposal | Routes, Nginx locations, login link | `/recover`, `/recover/verify`, `/recover/resend`, `/recover/cancel`; Nginx locations mirroring registration's (7.5); „Забравена парола?“ on `/login` while enabled (2.6). | Other paths or a zone of its own. | 8.3; zone tuning is activation evidence |
| RD15 | Proposal | Bulgarian texts (2.7, 2.8) | Proposed wording. | — | Activation only |
| RD16 | Proposal | The `/login?recovery=done` notice is URL-selected, as registration's activation notice is (approved there, PR 4). | Accept the same display limitation. | A one-shot session flag. | 8.3 (small) |
| RD17 | **Interpretation** of A8 ("voids **every** other pending recovery code") | How does a successful reset void the other pending codes of the account? | **By the session-key digest, without writing their rows.** The reset rotates the key; every code issued before it is bound to the old digest and can no longer reset anything (a correct code ends `INVALIDATED`). The rows stay `pending` until they expire, so another browser's reads, wrong codes, resends and cancellation behave exactly as before the reset (1.4, 2.4). | Write every other row to `void` in the redemption (revision 1): lets anyone who requested recovery for the address poll `GET /recover/verify` and learn, without a code, that the address has an account and that its owner has just reset the password (1.4). | 8.2 |
| RD18 | Proposal (restore handling; the restore script and its ownership protocol unchanged) | What happens to old passwords and sessions a restore brings back? | **(a)** After every restore, before the service is reopened to users: a read-only comparison of the restored snapshot with the preserved replaced database (`manage_accounts restore-credential-diff`, new, read-only), then for every affected or uncertain account a reviewed password replacement or a disable, not a sign-out alone; credential repair is separate from the registration hold release (6.6). Where the replaced database is unavailable as evidence, every account with a password is uncertain. | **(b)** The same runbook with hand-run read-only SQL instead of a command (6.6). **(c)** A sign-out note only, as revision 1 had: leaves restored old passwords valid (not recommended). | 8.1 (the command, under (a)); activation (the runbook, rehearsed) |

Not decided here and not decided by any of these: D16 (privacy, minors,
retention), which must now also cover recovery data and, under RD2 (a),
notices to addresses without an account; the configuration of D17.

### 0.4 What revision 2 changed (independent review of `745a952`: REQUEST CHANGES)

| Finding | Change | Where |
| --- | --- | --- |
| F1 Cross-browser reset polling leak | Another browser's rows are no longer voided by a reset; the digest makes them unusable (RD17, an interpretation of A8 for Gabriel to confirm). Polling model, enumeration claims, lifecycle and tests updated. RD8's recommendation changed because clearing the throttle is the same kind of disclosure at `/login`. | 0.3 RD8, RD17; 1.2; 1.4; 2.1; 2.3; 2.4; 2.5; 3.6; 4.4; 6.1; test plan RPoll |
| F2 Safe implementation sequencing | Scopes reordered: 8.1 storage and operations (migration with its whole integration, nothing that creates a row), 8.2 service with no caller, 8.3 routes. A no-caller test from 8.1 on, extended in 8.2. No claim that a build is safe because routes are absent. | 7.3; 10; scope; test plan RZ |
| F3, F4 Amendments of approved contracts | 0.1 quotes registration 5.2, D6, D20 and 3.4. RD4, RD7, RD10 marked as amendments. Registration design 3.4's approved sentence restored; the RD10 change appended there as a labelled proposal. | 0.1; 0.3; 4.5; 5.4; 5.6; registration design 3.4 |
| F5 Restore revives passwords | New 6.6: what a restore brings back, a read-only comparison runbook, the evidence cases, reviewed replacement or disable before reopening, separate from the hold release (RD18). | 6.2; 6.5; 6.6; 8.2; 9; test plan RR8–RR15 |
| F6 Timing | RD2 (b)'s difference stated as "with current synchronous dispatch, likely distinguishable; not measured". The 0.11 s figure removed. Responding before the send and a fixed minimum response time assessed and not proposed. No equal timing claimed. | 1.2; 1.4; 8.2; 9 |
| F7 Logs under RD2 (b) | Send-related events exist for real rows only under (b); stated, and RLog2 split by option. | 8.1; test plan RLog2 |
| F8 Mixed-purpose bound | At most eight guessable codes of an address, at most eight of them recovery codes; registration codes can still be issued after an account exists. | 5.4; test plan RL6 |
| F9 Nginx headers | Recovery locations mirror registration's: exact locations, the `/recover/` prefix, one effective `no-referrer`, the application's copy hidden. RP5 tests effective locations, headers and limits. | 2.6; 7.5; test plan RP5 |
| F10 Verification-address invariant | Email-change work must clear `email_verified_at` or verify the new address; a direct database edit does not keep verified ownership. | 1.3; test plan RE10 |
| F11 Allow-list scope | The scrub, the allow-list and RD13 are in scope 8.1 together, in every document. | 0.3; 6.4; 10; scope |
| Other | One read, gate and clock order (3.5); exactly-one ledger purpose assessed (3.1); uncertain wording for a commit whose outcome is unknown (2.4, 2.7); exceptions accumulate (5.4); demoted admins with TOTP rows tested (RE11). | as listed |

## 1. Threat model and eligibility

### 1.1 Assets and attackers

| Asset | Why it matters |
| --- | --- |
| A student or teacher account | Its attempts, results and, for a teacher, owned assignments, results of other students and CSV exports. A takeover reads and changes them. |
| An admin account | Everything. Never recoverable by email (A6). |
| The existence of an account for an address | Enumeration; the application otherwise reveals it nowhere (registration design 3.7). |
| The mail budget and the worker | Shared by registration and recovery; exhausting them closes both. |
| The mailbox owner's attention | Unwanted mail. |

| Attacker | Capability assumed |
| --- | --- |
| Anonymous web client | Any number of requests from many addresses, bounded only by Nginx and the application's caps. Can time responses. |
| Holder of a victim's mailbox | Reads codes sent to it (a compromised or shared mailbox, a recycled school address). |
| Holder of a copied session cookie | Has the `rec` slot of a victim's browser. |
| Operator (trusted) | CLI, database, restore. Concurrent operator actions are races, not attacks. |

### 1.2 Threats and answers

| Threat | Answer | Section |
| --- | --- | --- |
| Enumeration through the response | The same status, redirect, page, cookie shape, counters and texts for every address; the ledger counts every request alike. | 1.4 |
| Enumeration through timing | Under RD2 (a) both kinds of row send one message, so the difference is that of a code and a notice through the relay, as in registration. Under RD2 (b) only real rows send: with current synchronous dispatch, likely distinguishable; not measured. Stated, not hidden. | 1.4 |
| Observing another browser's reset (polling) | Someone who requested recovery for the address polls `GET /recover/verify`, tries wrong codes or resends while the owner resets in another browser. A reset writes no other row (RD17), so none of those answers changes before a correct code. With RD8 (b), `/login` would show it to someone who keeps the address locked. | 1.4, 2.4, 4.4 |
| Guessing a code | Keyed hash, 5 attempts per code, at most 8 codes per address per 24 h 15 min shared with registration, browser binding needed. 8 digits recommended (RD4). | 5.4 |
| Takeover through the mailbox | Inherent to email recovery: whoever reads the mailbox can reset. Verified-only eligibility (RD1) narrows it to addresses that once proved control; nothing re-verifies an old address (registration design 5.3). | 8.2 |
| Phishing the code | The binding means the code works only in the requester's browser; an attacker who requests and then persuades the owner to hand over the code succeeds. The message says never to share it. Same residual as registration. | 2.8, 8.2 |
| Stolen cookie | The slot alone redeems nothing without the code; redemption is single-use under the lock. | 2.2 |
| A code that outlives a change of the account | Eligibility and the session-key digest are rechecked at redemption under the lock (A5, A7). | 2.4 |
| Admin takeover | Admins are never eligible at issuance and are rechecked at redemption: a code issued before a promotion redeems nothing. | 1.3 |
| Budget exhaustion | Shared, non-refundable hourly budget and per-address caps; exhausting them closes registration and recovery for up to an hour, or one address for about a day, and nothing else. | 5.5 |
| Concurrent operator change | Serialized by SQLite's single writer; the recheck at redemption sees whichever committed first. | 3.6 |
| Resurrection by restore | The restore gate voids pending recovery rows; spent attempts and cancelled codes do not return. Old password hashes and session keys do return with the snapshot; a read-only comparison and a reviewed repair precede reopening (RD18). | 6.2, 6.6 |
| Disclosure through snapshots | Logical scrub of the isolated copy before publication, with a fail-closed schema check. | 6.3, 6.4 |

### 1.3 Eligibility

An account **may recover** at a moment `t`, under the writer lock, when all of
these hold for the `users` row whose `email` equals the normalised address:

1. it exists, and `is_active = 1`;
2. `role IN ('student', 'teacher')`;
3. `password_hash IS NOT NULL`;
4. `email_verified_at IS NOT NULL` (RD1; the approved default A13);
5. at redemption only: its `id` is the row's `user_id`, its `email` equals the
   row's `email` (exact string comparison, both normalised), and
   `HMAC-SHA256(SECRET_KEY, "recovery-session-key\0" + users.session_key)`
   equals the row's `session_key_digest` (compared with `hmac.compare_digest`).

Who that leaves out, and what each case gets:

| Account | Eligible | Notes |
| --- | --- | --- |
| Registered student, verified, active | yes | The only population that exists while the teacher gate is off. |
| Registered teacher, verified, active | yes | Exists only once the teacher gate has been on. |
| CLI-created student or teacher | no (RD1) | `email_verified_at` is NULL: `create_user` never sets it. Operator recovery, unchanged. |
| Any admin | never (A6) | Promotion after issuance is caught at redemption (role and digest). |
| Disabled account | no | `set_active` also rotates the key. |
| Account with an email and no password | no | `create_user` allows it; such an account cannot sign in by password. Adding a password is out of scope (A14). |
| Google-created account | no | `users.email` is NULL, so no address finds it. A Google `email_at_link` is never read by recovery (A4). |
| Password account that later linked Google | yes, if verified | The link is not touched. |
| An address with no account | — | Shadow row (RD2). |

**Legacy consequence of RD1.** Every account that exists today was created by
the CLI or by Google onboarding (which is off). None has `email_verified_at`.
With RD1 as recommended, recovery helps **no existing account**: pilot
teachers, any CLI-created student and every admin keep operator recovery
(`set-password`, which rotates the key; `docs/ACCOUNTS_DESIGN.md` §12). Recovery
serves accounts created by registration, which is what D10 requires before
public opening. The reasons not to trust a CLI-created address: the operator
typed it, nothing proved the person controls it, and a mistyped or recycled
address would hand the account to whoever reads that mailbox. If Gabriel wants
legacy accounts to recover, the narrower alternative is an operator command
that records an attestation in `email_verified_at` for one account after
checking it out of band. That command is not designed here (it would set a
column whose meaning is "proved by a code"; the attestation would need its own
column or its own decision).

**What `email_verified_at` must keep meaning (an invariant for later work).**
Item 4 trusts `email_verified_at` to say that the **current** `users.email`
was proved by a code. Nothing today changes `users.email` (no API exists,
A14), so the invariant holds by construction. It binds later work:

- Any future email-change feature must, in the transaction that changes
  `users.email`, either set `email_verified_at` to NULL or establish
  verification of the replacement address itself (a code sent to the new
  address and redeemed), and must void or invalidate recovery rows of the
  old address (the redemption recheck of item 5 already refuses them by
  `email`).
- A **direct database edit** of `users.email` (an unsupported operator path)
  does not preserve verified ownership: it leaves `email_verified_at` set for
  an address nobody proved, and that address becomes eligible at its next
  request. An operator who edits an address by hand must clear
  `email_verified_at` in the same statement; the design does not treat such an
  edit as keeping the account verified. A static test pins that no
  production code path writes `users.email` (test plan RE10), so a later
  feature that adds one fails that test and is pointed at this paragraph.

**Teacher accounts with leftover TOTP state.** Sign-in asks a second factor of
`role == 'admin'` only (`web/blueprints/account.py`, `login`). A demoted admin
that still has a TOTP row signs in as a teacher by password, so recovery of
such an account weakens nothing that sign-in enforces. Recovery never reads or
writes TOTP tables (A9). Such an account is eligible only if it also meets
item 4: a CLI-created admin has no `email_verified_at` and stays ineligible
under RD1. If it is eligible and recovers, its TOTP rows are left exactly as
they were; a later promotion back to admin asks for that old second factor at
sign-in, unchanged, and the existing advice to follow a promotion with
`set-password` when the password is shorter than 14 characters
(`docs/DEPLOYMENT.md` §22) applies to a recovered password too. Proposed for
the operations documentation of 8.1, not a behaviour change: before promoting
an account that recovered by email, consider `totp-reset` and a new
enrolment, since the old authenticator was never re-checked. Tests: RE11.

### 1.4 Enumeration

**Established, for every well-formed address, whatever account it has or
lacks:**

- the same status, redirect, page, cookie shape and texts at request, verify
  (any code), resend and cancel, at every cap and in every unavailable state;
- the same accounting: one ledger row per request or resend, counted alike in
  the per-address caps and the total budget, never refunded;
- the same row steps up to a correct code: a row, a binding, a code hash, five
  counted attempts, an expiry, a resend wait (a shadow's code hash matches no
  digits);
- no visible `not_sent` behaviour (RD7), so a failure to send cannot mark a
  row as real;
- no scrypt in either branch (neither has a password before redemption);
- **nothing another browser does short of a correct code changes what this
  browser sees** (RD17, below).

**Polling while the owner resets (F1 of the review, modelled).** Browser A
(an attacker, or anyone) requests recovery for an address and keeps its row
`rA`. The owner, in browser O, requests their own code and resets. A then, at
any moment before or after the reset, without the code:

| A does | Before O's reset | After O's reset, revision 1 (other rows voided) | After O's reset, RD17 (recommended) |
| --- | --- | --- | --- |
| `GET /recover/verify` | the verify page | "no usable recovery": **reveals an account and its reset** | the verify page, the same bytes |
| `POST /recover/verify` with a wrong code | `WRONG_CODE`, attempt counted | `NOT_PENDING (void)`, a different page: **reveals it** | `WRONG_CODE`, attempt counted |
| sixth wrong code | `TOO_MANY` | the different page | `TOO_MANY` |
| `POST /recover/resend` | a new issuance (RD11: eligibility decided again, the code bound to the key **now**) | "no usable recovery": **reveals it** | a new issuance bound to the rotated key, the same page; the owner's mailbox gets one more message, counted in the address's caps like any resend |
| `POST /recover/cancel` | voided, `303` to `/recover` | `303` to `/recover` (a final row is left as it is) | voided, `303` to `/recover` |
| correct code of `rA` (only a mailbox holder has it) | `REDEEMED` | `NOT_PENDING (void)` | `INVALIDATED` at step 10 (digest) |
| waits | `rA` expires 30 minutes after its request | — | the same: `rA` expires on its own schedule |

Under RD17 the redemption writes only the redeemed row, the account (and,
under RD8 (b), the throttle). `pending_view`, steps 1–9 of the redemption,
`resend` and `cancel` read the recovery row, the ledger and the control row,
never the account, before a correct code, and the reset changes none of those
for `rA`. The same holds when `rA` is a shadow (an address with no
recoverable account never has a reset to reveal) and when A and O are two
browsers of the owner. What remains observable without a code is outside
recovery's own pages: with RD8 (b), the sign-in throttle (4.4); the owner's
mailbox (one more message per resend), which informs the owner. Tests:
test plan RPoll.

**Under RD2 (a)** both kinds of row send one message and settle one outcome,
so the application's own steps are the same: one lookup, one row, one
sender process, one exchange. What differs is outside it: the relay may take
longer, accept or refuse differently for a code and for a notice (registration
design 3.7, unchanged). Measured durations through the chosen provider are
activation evidence (9).

**Under RD2 (b)** an eligible address costs a sender process and an exchange
with the relay, bounded by the 8-second deadline; every other address costs
no send. **With current synchronous dispatch, likely distinguishable; not
measured.** No duration has been measured for recovery, on any machine, and
none is claimed here. A3 allows the limitation to be stated rather than
closed, and this is the statement. Two ways to narrow it were assessed and
are not proposed:

- **Respond before the send** (dispatch after the response has been written,
  in the same worker). The request's own duration no longer contains the
  send, but the single synchronous worker is still occupied by it, so a
  second request sent right behind the first (a concurrent observer) waits
  measurably longer after a real row than after a shadow. It also reverses
  the approved order of registration design 4.3 (commit, send, settle, then
  answer), moves dispatch out of the handler's error handling, and gives the
  browser its page before anything was tried. It narrows, does not close.
- **A fixed minimum response time** (every request and resend answered no
  earlier than a fixed `T` after it arrived). To cover a real send `T` must
  exceed the 8-second deadline plus process creation and reaping, which no
  measurement bounds; a send that exceeds `T` still shows. With one
  synchronous worker each request then occupies it for at least `T`: a class
  of 30 submitting together takes at least 30 × `T`, beyond Nginx's 35-second
  upstream timeout for all but the first few, and the total throughput falls
  to 3600 / `T` requests an hour for registration and recovery together. A
  concurrent observer still sees any send longer than `T`, and the sleeping
  worker serves nobody else. The capacity cost is certain and the hiding is
  partial.

RD2 (a) removes the application's own difference instead, which is why it is
recommended; it does not make the two durations equal either.

**Not established in either case:** equal duration, equal delivery and the
circuit's independence of content (registration design 3.7). No option above
claims equal timing. The owner of the mailbox learns that someone asked; that
informs the owner, not the requester.

The only responses that depend on the account by design come after a correct
code (`REDEEMED`, `INVALIDATED`), which only the mailbox holder can send.
Sign-in stays uniform and unchanged under RD8 (a), the recommendation; under
RD8 (b) a reset clears the address's sign-in lockout, which someone who keeps
that address locked can observe (4.4).

## 2. Lifecycle and UX

### 2.1 States

```
POST /recover ──► pending ──► redeemed   (password set, session_key rotated)
                     │
                     ├──► void      (replaced by this browser's next request, cancelled,
                     │              invalidated at its own redemption, restore gate)
                     └──► expired   (30 minutes after creation)
```

- Another browser's successful reset is **not** a transition of this row
  (RD17): the row stays `pending`, its code stops being able to reset
  anything (the digest of 1.3 item 5 no longer matches), and it leaves
  `pending` only by one of the transitions above. Under RD17's alternative a
  fourth cause of `void`, "voided by another redemption", would return, with
  the disclosure of 1.4.
- Every state but `pending` is final and never rewritten. A request that finds
  a final row writes nothing and is answered "no usable recovery" (2.4).
- Leaving `pending` scrubs `email`, `user_id`, `session_key_digest`,
  `code_hash` and `code_expires_at` in the same statement. What remains: the
  state, the binding hash, `is_shadow`, counters and times.
- **Validity** is 30 minutes; **retention** is the logical target of 6.1.

`is_shadow`, `user_id` and `session_key_digest` describe the **live code**,
not the row's history: each issuance sets them (2.3).

### 2.2 Browser binding and session contents

**Why bind.** Without a binding the code alone would redeem from any browser:
a person who requests a reset for someone else's address could redeem the
code the owner reads aloud or forwards, and a code shown on a shared screen
would be enough by itself. With it, redemption needs both what the requester's
browser holds and what the mailbox holds. The cost is registration's: the code
must be typed into the browser that asked (reading it on a phone works; a code
is used rather than a link for that reason). Proposed as registration does
it; it is not a separate decision.

As registration design 2.3, with a slot of its own:

- On a committed request: `token = secrets.token_urlsafe(32)`, stored as
  `HMAC-SHA256(SECRET_KEY, "recovery-binding\0" + token)`, and
  `session["rec"] = {"v": 1, "t": token, "next": <validated path or "">}`.
  Nothing else: no address, no user id, no code, no digest.
- Verify, resend and cancel find the row by the hash of the slot's token only.
  No row id or address is in a URL or a form.
- A slot of any other shape (other keys, wrong version, a token that is not 43
  URL-safe characters, a `next` that `registration_forms.accepted_next` does
  not return unchanged) is ignored, exactly as the `reg` slot is.
- One recovery per browser: a new request voids and scrubs the row of the token
  the slot held.
- `reg` and `rec` are independent; a browser may hold both.
- The existing session functions are unchanged and already right:
  `issue_account_session` and `issue_pending_second_factor` drop `rec` with
  everything but the `quiz_attempt_owner:*` markers; `end_session` drops it.
- A copy of the cookie is the same browser to the server. It gains nothing
  without the code; redemption is single-use because the row's state changes
  under the lock.

### 2.3 Request and resend

**Request** (`POST /recover`, one field `email`):

1. Gate, then the signed-in redirect (2.6), then the address rule of
   registration design 1.3 (`registration_forms.public_email`). A malformed
   address re-renders the form with „Невалиден имейл.“: the answer depends on
   the input only.
2. Without a lock: `registration.availability` (hold, clock, unreaped sender,
   circuit, total budget) and `registration.address_capped`. A refusal costs
   no row, no ledger row and no process.
3. `BEGIN IMMEDIATE`, then `recovery.request(...)` decides, in this order,
   with the clock read after the lock is held: hold and clock; sender,
   circuit and total budget; the address caps; and only then the inline
   recovery cleanup, the voiding of this browser's previous row, the
   eligibility lookup, the row and its first issuance (3.3).
4. Commit; set `rec`; dispatch once with no transaction open; settle in a
   transaction of its own; `303` to `/recover/verify`.

**Resend** (`POST /recover/resend`), in the order of 3.5: the row of the
binding is read (none or final: "no usable recovery", nothing written); then
hold and clock; then the row must be unexpired (else it is expired and
scrubbed); sender, circuit and total budget; at most 4 sends; 60 seconds
after the previous issuance; the address caps; then **eligibility is decided
again** (RD11) and the issuance replaces the code, its attempts and its
binding: an eligible account gets a code bound to its current id and
session-key digest (`is_shadow = 0`), anything else the shadow treatment
(`is_shadow = 1`, `user_id` and digest NULL). A resend therefore behaves as a
fresh request on the same row, and a password change between request and
resend does not strand an eligible person. After another browser's reset a
resend is answered exactly as before it, and its new code is bound to the
rotated key (1.4).

Repeated requests from one browser each void the previous row. Requests from
several browsers make several rows, each with its own code; all are bounded by
the shared address caps (5.2), and a successful redemption leaves every one
of them unable to reset anything without writing them (RD17).

### 2.4 Verification and reset: one step (RD3)

**Recommendation: one POST carries the code, the new password and its
confirmation, and one transaction decides everything.** A separate reset
credential (verify the code, then set a password on a second page) was
considered and is not recommended:

| | One step | Two steps |
| --- | --- | --- |
| Credentials in play | The code only | The code, then a reset token with its own lifetime, storage, binding, scrub and restore handling |
| States | `pending` → final | An extra `verified` state whose token is a bearer credential |
| Eligibility recheck | Once, at the moment the password changes | Twice, or a window between check and change |
| Failure between steps | None | Code consumed, password not set; the person starts again |
| Scrypt work | Only after a correct code, once | The same |
| UX cost | A wrong code means typing the password again | None |

The UX cost is accepted. The password fields are never echoed, and the code
is not echoed either.

**Blueprint, before the lock** (no write, no attempt counted):

1. Gate, signed-in redirect, slot. No usable slot → „Заявката за нова парола
   е изтекла…“ without reading any row by code or address.
2. Read the row of the binding (`recovery.pending_view`, reads only). None,
   final, or past its time by the read → the same "no usable recovery" page.
3. `accounts.password_problem(password, view.email)` on the exact submitted
   value, and the exact comparison with `confirm_password`. A problem
   re-renders the page with the field error; the code is not looked at.

**`recovery.redeem(conn, binding, code, password, *, secret_key, clock,
clear_throttle)`**, inside the caller's `BEGIN IMMEDIATE`, clock read under
the lock, in this order (the order of 3.5: binding reads, then the gate, then
any write):

| Step | Check or action | Result |
| --- | --- | --- |
| 1 | No row for the binding | `NO_PENDING`, nothing written |
| 2 | Row final | `NOT_PENDING` (with `final_state`), nothing written |
| 3 | Hold, or clock behind the high-water mark | `UNAVAILABLE`, nothing written |
| 4 | Past `expires_at`, or created beyond the skew in the future | set `expired`, scrub → `EXPIRED` |
| 5 | No live code (3.4) | `CODE_EXPIRED`, no attempt counted |
| 6 | `code_attempts` at 5 | `TOO_MANY` |
| 7 | Count the attempt | — |
| 8 | Compare (trim at the ends, then exactly 8 ASCII digits under RD4, else 6; `compare_digest`). A shadow never matches. | `WRONG_CODE` |
| 9 | Clear the code (single use) | — |
| 10 | Eligibility and binding of 1.3, items 1–5, read now | any failure: set `void`, scrub → `INVALIDATED` |
| 11 | `accounts.set_password(conn, user_id, password)`, unchanged | new hash, `password_changed_at`, rotated key (4.1) |
| 12 | Only under RD8 (b): `login_throttle.clear` of the address's throttle key | — |
| 13 | Set this row `redeemed`, scrub | `REDEEMED` (with `user_id`) |

No other recovery row is read or written (RD17). Every other pending row of
the account was issued under the key step 11 has just replaced, so its stored
digest no longer matches and its correct code ends at step 10 as
`INVALIDATED`; that is how this addendum proposes to meet "voids **every**
other pending recovery code" (A8), subject to Gabriel's confirmation. Rows
issued later, by a new request or a resend after this commit, are bound to the
new key, as any new code is.

Steps 1–9 are identical for a real and a shadow row, and identical before and
after another browser's reset, so nothing before a correct code depends on
the account or on what happened to it. Steps 10–13 are reachable only with a
correct code, which only the mailbox holder has.

`set_password` hashes with scrypt under the writer lock. That is deliberate:
it happens once per successful reset and never for a wrong code, so the lock
is held for one scrypt computation a handful of times a day, and no
pre-computed hash has to be trusted. The duration was not measured here (the
development interpreter has no `hashlib.scrypt`); it belongs to the capacity
evidence of 9.

**Failures.** Two cases are kept apart, because only one of them is certain:

- **An exception inside `redeem`, before the commit, followed by a rollback
  that returned.** Nothing of the transaction took effect: the password, the
  key, the attempt count, the code and the throttle are as before. The answer
  is the failure page „Нещо се обърка и паролата не беше сменена. Опитай
  отново.“ with status 500; the slot stays, so the same code can be tried
  again.
- **The commit raised, or the rollback after an exception raised.** Whether
  the change was made is not established by the exception alone (an I/O
  error reported during a commit does not by itself prove that nothing
  reached the database file or its WAL), and a connection whose rollback
  failed is in an unknown state. Not investigated further here; the page must
  not say the password was not changed. The answer is „Не успяхме да потвърдим дали паролата е сменена.
  Опитай да влезеш с новата парола; ако не става, започни отначало.“ with
  status 500, and the slot stays. Trying the same code again is safe either
  way: if the commit took effect the row is `redeemed` and the retry is
  `NOT_PENDING (redeemed)`, a redirect to `/login` that writes nothing; if it
  did not, the retry redeems.

The success page is reached only after the commit has returned.
`set_password` raising `ValueError` (a password the pre-check accepted but the
account's current address refuses) cannot happen once step 10 has established
that the addresses are equal; if it does, it is the first case, never a
reported success.

**Outcomes as the browser sees them.**

| Outcome | Response | Slot |
| --- | --- | --- |
| `REDEEMED` | `303` to `/login?next=<next>&recovery=done` | dropped |
| `WRONG_CODE`, `CODE_EXPIRED` | the page with „Грешен или изтекъл код.“ | kept |
| `TOO_MANY` | the page with „Твърде много грешни опити. Поискай нов код.“ | kept |
| `INVALIDATED` | „Не успяхме да зададем нова парола. Започни отново.“ | dropped |
| `NOT_PENDING` with `redeemed` | `303` to `/login?next=<next>` (a repeat of a success; nothing written) | dropped |
| `EXPIRED`, `NO_PENDING`, other `NOT_PENDING` | „Заявката за нова парола е изтекла или е започната от друг браузър. Започни отново.“ | dropped |
| `UNAVAILABLE` | „Възстановяването на парола временно не е достъпно. Опитай по-късно.“, status 503 | kept |
| exception before the commit, rollback returned | „Нещо се обърка и паролата не беше сменена. Опитай отново.“, status 500 | kept |
| commit or rollback raised (outcome may be unknown) | „Не успяхме да потвърдим дали паролата е сменена. Опитай да влезеш с новата парола; ако не става, започни отначало.“, status 500 | kept |

`INVALIDATED` tells the mailbox holder that the account changed (including
a reset from another browser, RD17). That is not enumeration: the correct
code already proves control of the mailbox, as registration's `EMAIL_TAKEN`
does.

### 2.5 Cancellation, expiry, restart

- **Cancel** (`POST /recover/cancel`, „Грешен имейл? Започни отново“): void and
  scrub this browser's pending row, drop the slot, `303` to
  `/recover?next=<next>`. A final row is left as it is, and the response is
  the same `303` whether the row was pending, final or absent. A clock behind
  the mark refuses (nothing voided). A restore hold does **not** refuse
  (RD12): the hold and the mark are left exactly as they were.
- **Expiry**: evaluated on read by GETs, written by POSTs and by the cleanup.
  A GET never writes.
- **Restart, deploy**: nothing is lost; the row and the signed session survive.
- **Secret-key rotation**: every binding, code hash, session-key digest and
  ledger address hash stops matching; every recovery in progress ends (and
  every session, as today).

### 2.6 Routes

All in a new blueprint `recovery` (`web/blueprints/recovery.py`).

| Route | Methods | Access | Integrity | Notes |
| --- | --- | --- | --- | --- |
| `/recover` | GET, POST | `public` | `same_origin` | Form and request. Reads `next`. |
| `/recover/verify` | GET, POST | `public` | `same_origin` | Code, new password, confirmation |
| `/recover/resend` | POST | `public` | `same_origin` | |
| `/recover/cancel` | POST | `public` | `same_origin` | |

Rules, each the registration convention (registration design 6.2):

- **Disabled**: every `/recover...` address answers the generic 404 after the
  same-origin check, and `/login` has no link to it.
- **Signed in**: a GET redirects to the role's home; a POST starts nothing and
  redirects there too. Recovery is for a signed-out browser; a signed-in
  person changes the password at `/account/password`.
- **No GET writes**; a GET without a usable recovery sets no cookie.
- **`next`** passes `accepted_next` when stored and `safe_redirect_target`
  when used. Success goes to `/login`, never to `next` directly, and no
  redirect starts an attempt.
- **Responses** of every `/recover...` route carry `Cache-Control: no-store`
  and `Referrer-Policy: no-referrer` from the application (for development
  and tests). In production Nginx hides the application's
  `Referrer-Policy` and sets exactly one `no-referrer` itself on every address
  under `/recover`, as it does under `/register` (7.5); `Cache-Control` from
  the application passes through.
- The code input has `autocomplete="one-time-code"` and `inputmode="numeric"`;
  the password inputs `autocomplete="new-password"`. Nothing is put in a URL:
  the forms are POST bodies and every redirect target is built from the slot
  or from a fixed value.
- `/login` shows „Забравена парола?“ linking to `/recover?next=<next>` only
  while recovery is enabled, and `/login?recovery=done` shows the success
  notice by lookup, never by echo (RD16: a hand-built URL can show it without
  a reset, the display limitation registration accepted for its activation
  notice).
- The sweeps of `tests/test_access_policy.py` and
  `tests/test_session_cookie_samesite.py` cover the new routes.

### 2.7 Texts (proposals, RD15)

| Case | Text |
| --- | --- |
| Link on `/login` | „Забравена парола?“ |
| Request page | Heading „Нова парола“; label „Имейл“; button „Изпрати код“ |
| Verify page | „Ако за {email} може да се зададе нова парола с код, изпратихме на този адрес 8-цифрен код. Въведи го заедно с новата парола.“ („6-цифрен“ if RD4 keeps 6 digits) |
| Fields | „Код“, „Нова парола“, „Повтори новата парола“; button „Запази новата парола“ |
| Wrong or expired code | „Грешен или изтекъл код.“ |
| Attempts used up | „Твърде много грешни опити. Поискай нов код.“ |
| Resent | „Изпратихме нов код.“ (shown for every row; under RD2 (a) a shadow's notice is also "a message") |
| Too early / used up | „Можеш да поискаш нов код след малко.“ / „Не можеш да поискаш повече кодове. Започни отначало.“ |
| Per-address cap | „Твърде много имейли към този адрес. Опитай отново по-късно.“ |
| Password errors | the existing texts of `password_problem`; „Паролите не съвпадат.“ |
| No usable recovery | „Заявката за нова парола е изтекла или е започната от друг браузър. Започни отново.“ |
| Invalidated | „Не успяхме да зададем нова парола. Започни отново.“ |
| Unavailable | „Възстановяването на парола временно не е достъпно. Опитай по-късно.“ |
| Failure, rolled back (2.4) | „Нещо се обърка и паролата не беше сменена. Опитай отново.“ |
| Failure, outcome not established (2.4) | „Не успяхме да потвърдим дали паролата е сменена. Опитай да влезеш с новата парола; ако не става, започни отначало.“ |
| Success on `/login` | „Паролата е сменена. Влез с новата парола.“ Under RD8 (a) the notice adds, for everyone: „Ако видиш съобщението ‚Твърде много опити‘, изчакай няколко минути.“ (the throttle is not cleared by a reset). |

The verify page's sentence is true for every row, which is why it is
conditional. The address shown is the one this browser typed, read from its
own row; it is never in a URL or the cookie.

### 2.8 Messages (plain text, proposals)

Code. Subject: „Код за нова парола в LearnPilot“.

> Кодът ти за нова парола е: 12345678
> Валиден е 10 минути. Въведи го в страницата, от която поиска нова парола.
> Ако не си поискал(а) нова парола, не прави нищо: паролата ти остава същата.
> Не споделяй този код с никого.

Notice (RD2 (a) only). Subject: „Заявка за нова парола в LearnPilot“.

> Някой поиска нова парола в LearnPilot за този имейл. За този имейл не може да
> се зададе нова парола с код. Ако имаш профил в LearnPilot, обърни се към
> администратора на сайта. Ако не си поискал(а) това, не прави нищо.

One notice text for every non-eligible case (no account, admin, disabled,
never verified, no password): it is true in each, it tells the mailbox reader
nothing about which case applies, and it gives a legacy account the way that
exists (the operator). No link, no name, no tracking. Headers as registration
design 4.1: the only request-derived header is `To`.

Under RD2 (a) the typed address may happen to be the `email_at_link` of some
Google identity, or no one's. The notice goes there because it was typed;
recovery never looks an address up in `user_identities`, the notice carries no
credential, and it selects no account. This is exactly the reading of A4 that
RD2 (a) asks Gabriel to confirm.

## 3. Storage and transaction boundaries

### 3.1 Schema (sketch; additive; no migration created or numbered)

The migration is the next free number at implementation time.

```sql
CREATE TABLE password_recoveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    binding_hash TEXT NOT NULL UNIQUE,        -- 64 lower-case hex, keyed (2.2)
    state TEXT NOT NULL CHECK (state IN ('pending', 'redeemed', 'void', 'expired')),
    is_shadow INTEGER NOT NULL CHECK (is_shadow IN (0, 1)),
    email TEXT,                               -- the normalised address typed; NULL once final
    user_id INTEGER REFERENCES users(id),     -- the account the live code is bound to
    session_key_digest TEXT,                  -- 64 hex, keyed; of users.session_key at issuance
    code_hash TEXT,                           -- 64 hex, keyed (3.2)
    code_expires_at TEXT,
    code_send_id INTEGER,                     -- registration_sends.id of the live code's issuance
    code_attempts INTEGER NOT NULL DEFAULT 0 CHECK (typeof(code_attempts) = 'integer' AND code_attempts >= 0),
    send_count INTEGER NOT NULL DEFAULT 0 CHECK (typeof(send_count) = 'integer' AND send_count >= 0),
    resend_after TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    closed_at TEXT,
    CHECK (state = 'pending' OR (email IS NULL AND user_id IS NULL AND session_key_digest IS NULL
                                 AND code_hash IS NULL AND code_expires_at IS NULL)),
    CHECK (state != 'pending' OR email IS NOT NULL),
    CHECK (state != 'pending'
           OR (is_shadow = 1 AND user_id IS NULL AND session_key_digest IS NULL)
           OR (is_shadow = 0 AND user_id IS NOT NULL AND session_key_digest IS NOT NULL))
);
CREATE INDEX idx_password_recoveries_state ON password_recoveries(state, expires_at);

ALTER TABLE registration_sends ADD COLUMN recovery_id INTEGER
    CHECK (recovery_id IS NULL
           OR (typeof(recovery_id) = 'integer' AND recovery_id > 0 AND pending_id IS NULL));
CREATE INDEX idx_registration_sends_recovery
    ON registration_sends(recovery_id) WHERE recovery_id IS NOT NULL;
```

- The hash columns carry the 64-byte lower-case hex checks of migration 021
  (`typeof`, `length`, `length(CAST(... AS BLOB))`, `NOT GLOB '*[^0-9a-f]*'`),
  omitted above for brevity.
- `user_id` has a foreign key: accounts are never deleted today, and the
  column is NULL on every final row, so it never blocks a future erasure of a
  closed recovery. A future erasure tool must void pending rows first.
- No address hash in the recovery row: the ledger keeps it.
- `registration_sends.kind` keeps its CHECK (`code`, `notice`): a recovery
  code is `code`, a recovery notice is `notice`. What a row is for is
  `recovery_id IS NOT NULL`. The CHECK on the new column refuses a row that
  names both a registration and a recovery. **Checked in scratch** on SQLite
  3.51.0: `ADD COLUMN` accepted the cross-column CHECK, an existing row stayed
  valid, a row with both ids and a zero id were refused, and an insert that
  names only the old columns still worked (11). The migration test must run on
  the deployed SQLite version.
- **Exactly one purpose: assessed, not recommended as a CHECK.** The CHECK
  above enforces *at most* one purpose (never both ids). A CHECK enforcing
  *exactly* one, `(pending_id IS NULL) != (recovery_id IS NULL)`, would
  refuse rows that exist legitimately today:
  - migration 021 declares `pending_id` nullable with no CHECK, and SQLite
    validates a CHECK added by `ADD COLUMN` against every existing row: in a
    scratch model (SQLite 3.51.0) the exactly-one column was **refused** by
    `ADD COLUMN` as soon as one existing row had `pending_id` NULL, and
    accepted on a table whose rows all had it (11). A database holding such a
    row would fail to migrate;
  - the registration service always writes `pending_id`, but the existing
    suite inserts ledger rows without it on purpose: the cap and circuit
    fixtures (`tests/registration_support.py` `add_send`,
    `tests/test_registration_ledger.py`), the restore operations test, the
    migration tests, and the fail-closed test of
    `tests/test_registration_service.py` that sets `pending_id` NULL. With
    the CHECK, every one of them would need rewriting, and the last could no
    longer be expressed;
  - enforcing it for new rows only needs a table rebuild or a trigger, and
    a rebuild is not additive.

  Recommended instead: the at-most-one CHECK; both services always write
  their own id (tested: every ledger row either service inserts has exactly
  one id, RA3); and a purpose-less row (both NULL) keeps today's meaning: it
  counts in every cap and in the circuit, is never a live code for either
  module (each requires its own id to match), is never dispatched by either,
  and only registration's `settle_send` can settle it, as today.
- **Indexes.** Only the state index is needed. Revision 1's partial indexes on
  `user_id` and `email` served the voiding of other rows at redemption, which
  RD17 removes; they return only if RD17's alternative is selected.
- Classification (`src/canonical_tables.py`): `password_recoveries` is
  **runtime** ("password recovery in progress: an address, an account link and
  a credential hash; never canonical content").
- No existing row is written and no table is rebuilt.

### 3.2 Keyed values

| Value | Stored as | Label |
| --- | --- | --- |
| Binding token | `HMAC-SHA256(SECRET_KEY, label + "\0" + token)` | `recovery-binding` |
| Code | `HMAC-SHA256(SECRET_KEY, label + "\0" + <row id> + "\0" + code)` | `recovery-code` |
| Session key | `HMAC-SHA256(SECRET_KEY, label + "\0" + session_key)` | `recovery-session-key` |
| Address (ledger) | `registration.email_hash`: the **registration** label, deliberately (5.2) | `registration-email` |

Labels differ from registration's (`registration-binding`, `-code`) and from
Google's (`oidc-link`), so no value of one purpose verifies another.
`oidc_accounts.session_key_digest` is not reused. Generation:
`f"{secrets.randbelow(10**8):08d}"` (RD4); with 6 digits, `10**6` and `:06d`.

### 3.3 One issuance

Inside the caller's `BEGIN IMMEDIATE`, after the lock is held and the clock
read, for row `r` and address `e`:

1. Decide eligibility (1.3, items 1–4) by `users.email = e`.
2. Insert the ledger row: `pending_id NULL`, `recovery_id = r`,
   `email_hash = registration.email_hash(SECRET_KEY, e)`, `kind` (`code` if
   eligible, else `notice`), `attempted_at = now`, `outcome 'started'`,
   `issuance_id = secrets.token_hex(16)`. Its id is the **send generation**.
3. Update `r` in one statement: `is_shadow`, `user_id`, `session_key_digest`,
   `code_hash` (of the new code, or of 32 random bytes for a shadow),
   `code_expires_at = min(now + 10 min, expires_at)`, `code_send_id`,
   `code_attempts = 0`, `send_count + 1`, `resend_after = now + 60 s`;
   `WHERE id = r AND state = 'pending'`, exactly one row or raise.
4. Raise the shared high-water mark.
5. Return `ISSUED` with the row id, the generation, the issuance identity, the
   kind and the message (`repr=False`, never logged) and, for a first
   issuance, the token.

Under RD2 (b) step 5 returns no message for a shadow and the caller dispatches
nothing; the ledger row stays `started` for ever (counted in every cap, not in
the circuit, deleted after 48 hours).

### 3.4 Live code

As registration's `_code_is_live`, against the recovery row: the ledger row of
`code_send_id` exists, has `recovery_id = r.id` and `pending_id IS NULL`, is
of the kind `is_shadow` implies, carries a stamp in the module's layout, and
`code_expires_at` is the value that stamp gives. Then: issued no more than the
skew ahead of `now`, and `now < code_expires_at`. Anything else is not live
and is logged (fail closed). `clear-registration-limit` deleting the ledger
row therefore kills the code, as for registration.

### 3.5 Transactions (owned by the blueprint)

| Request | Transactions |
| --- | --- |
| Request | Reads: availability and the address cap (no lock) → `BEGIN IMMEDIATE`: `recovery.request` (hold and clock, admission, caps, inline cleanup, void previous, eligibility, row, issuance, mark), commit → set `rec` → dispatch with no transaction open → `BEGIN IMMEDIATE`: `recovery.settle_send`, commit. |
| Resend | `BEGIN IMMEDIATE`: `recovery.resend`, commit → dispatch → `BEGIN IMMEDIATE`: settle, commit. |
| Verify page (GET) | Reads only (`pending_view`). |
| Redeem | Pre-checks (reads, 2.4) → `BEGIN IMMEDIATE`: `recovery.redeem`, commit → drop `rec` on a final outcome. No session. |
| Cancel | `BEGIN IMMEDIATE`: `recovery.cancel`, commit → drop `rec`. |
| Cleanup (timer, CLI) | Inside the existing `registrations-cleanup` transaction (6.1). |

Every writer takes the lock itself with a statement that changes no row,
reads its clock afterwards (registration design 3.4), never commits, and
raises `TransactionRequired` with no transaction open. Log lines written by a
writer name a decision (`..._decided`); the blueprint logs the durable event
after the commit.

**One order for every writer** (registration's, as its code does it:
`registration._usable_row`, `create_pending`, `cancel`, `cleanup`):

1. take the writer lock (a statement that changes no row);
2. read the clock;
3. reads that write nothing and need no gate: the row of this browser's
   binding (none → `NO_PENDING`; final → `NOT_PENDING`). A request has no row
   yet and goes straight to 4;
4. the **gate**: the restore hold (not for `cancel` and `cleanup`, RD12, 6.1),
   then the clock against the high-water mark;
5. only then anything that writes: expiry and scrub, voiding, attempt
   counting, issuance, the mark, deletion. A request decides admission and
   the address caps between 4 and 5, before its inline cleanup.

So no mutation precedes the clock check (clock-first mutation), and the
answers of step 3 depend only on this browser's own row. `cleanup` has no
binding: its step 4 is the clock only, and inside `registrations-cleanup` it
runs only when registration's cleanup did not report the clock behind (6.1).
Sections 2.3, 2.4, 2.5, 5.5 and 5.7 follow this order.

### 3.6 Identity, settlement and concurrency

**Independent issuance identity.** `dispatch` and `settle_send` take the
generation number **and** its random identity. SQLite gives the numbers of a
rolled-back transaction to the next one, `AUTOINCREMENT` included (reproduced
in scratch, 11): a `Result` kept from a rolled-back request must match nothing,
and the identity is what makes it so, even when the row id, the recipient and
the digits coincide.

**`recovery.dispatch(conn, mailer, issued, *, secret_key)`** refuses
(`NotSendable`, nothing sent, nothing settled) while a transaction is open and
unless all of this holds, read from the database:

- the ledger row of `send_id` holds `issuance_id`, has `recovery_id =
  issued.recovery_id` and `pending_id IS NULL`, is `started`, of
  `issued.kind`;
- the recovery row is `pending` and `code_send_id` is this generation (an
  older generation is never sent after a resend);
- the ledger's address key equals `email_hash(row.email)`;
- for a real row (`kind = code`): the account `row.user_id` still has
  `users.email = row.email` (A4 at send time; full eligibility is redemption's);
- the token, when the result carries one, hashes to the row's binding;
- the whole message equals what the module builds for that address, and the
  digits in it hash to the stored `code_hash`.

One mailer call per `dispatch` call; the blueprint calls it once per committed
issuance and never retries. No durable send claim; no exactly-once claim about
SMTP.

**`recovery.settle_send(conn, send_id, issuance_id, outcome, *, clock)`**:
`NOT_THIS_ISSUANCE` (nothing changed) unless the ledger row has that identity
**and** `recovery_id IS NOT NULL`; `ALREADY_SETTLED` if it is no longer
`started` (first settlement wins); otherwise
`UPDATE registration_sends SET outcome, settled_at WHERE id = ? AND
issuance_id = ? AND outcome = 'started' AND recovery_id IS NOT NULL`. **It
never changes a recovery row** (RD7, 4.5). A late settlement of an old
generation, after a resend, a redemption, a cancellation or an expiry, settles
its own ledger row and nothing else. A settlement whose transaction fails is
logged by class name and leaves the row `started`, which is safe.

Recommended companion change to reviewed code: registration's `settle_send`
gains `AND recovery_id IS NULL` (and the identity read refuses a recovery
row), so neither module can settle the other's issuance. Without it a
mis-wired call would settle a recovery ledger row and touch no recovery row,
which is harmless but untested.

**Concurrency, each serialized by SQLite's one writer:**

| Race | Result |
| --- | --- |
| Two tabs of one browser post the correct code together | The first redeems and commits; the second finds a final row → `NOT_PENDING (redeemed)`, writes nothing, and its password is not applied. First winner. |
| Two browsers, two rows of one account, both correct | The first redeems and rotates the key; the other row is not written (RD17), so the second, with its correct code, is refused by the digest at step 10 → `INVALIDATED`, its row voided and scrubbed then. Before its correct code, the second browser saw nothing change (1.4). |
| One browser polls, wrong-codes or resends while another redeems | Serialized. Neither the poll nor the wrong code reads the account; the redemption writes only its own row, the account (and the throttle under RD8 (b)). A resend committed after the redemption binds its new code to the rotated key; one committed before it is bound to the old key and is refused at step 10 if ever presented. |
| Redemption and CLI `set-password` | CLI first: key rotated → `INVALIDATED`. Redemption first: the CLI then sets its own password; the operator's later action stands. |
| Redemption and `set-role admin`, `disable`, `sign-out`, `totp-reset`, `/account/password` | Whichever commits first. Afterwards the recheck refuses (role or active state, and the rotated key), or the operator's change follows the reset. |
| Redemption and a resend of the same row | Serialized; after a resend the old code is gone and the new one counts its own attempts. |
| Two requests for one address at its cap minus one | Issuance time is read under the lock; the second sees the first and is refused. |
| Five wrong codes at once | Five attempts counted; a sixth is refused. |
| Registration activation creating the account while a shadow recovery is pending | The recovery row stays a shadow; a resend decides again and may now issue a code (RD11). |

**Failed commits** anywhere: the request is answered with a failure and
nothing it decided is acted on (no slot written, nothing dispatched, no
success page). Whether a commit that raised took effect is not assumed either
way (2.4); every next step is safe in both cases: an unsent issuance is a
counted ledger row that stays `started`, and a retried redemption is
`NOT_PENDING (redeemed)` or redeems.

## 4. Existing account changes

### 4.1 What the account API provides today

Read in `web/services/accounts.py` at `513b36c`:

- `set_password(conn, user_id, password) -> str`: reads `users.email`
  (`LookupError` for no account, `ValueError` "Парола изисква имейл." for no
  email), hashes the exact string with `_hash_password` (which applies
  `password_problem(password, email)` with **no role**, so the admin minimum of
  14 is not applied; recovery never reaches an admin), then `_update_user`
  sets `password_hash`, `password_changed_at = CURRENT_TIMESTAMP`, a new
  `session_key` and `updated_at`, and returns the new key. Never commits.
- `rotate_session_key`, `set_role`, `set_active` rotate the key; `totp.enroll`,
  `confirm` and `reset` call `rotate_session_key`. `authenticate` (including
  its hash upgrade), `record_login`, `set_display_name` and
  `mark_profile_required` do **not** rotate it.
- `_USER_COLUMNS` excludes `email_verified_at` and `password_hash` (only
  `has_password`); `get_user_by_email` therefore cannot decide RD1.
- `/account/password` calls `set_password`, then `login_throttle.clear` of the
  account's throttle key, in one transaction (`web/blueprints/account.py`).

### 4.2 Changes needed

| Change | Why |
| --- | --- |
| **None to `set_password`.** Redemption calls it as it is (A8). | It is the existing contract: exact string, `password_problem` against the account's address, scrypt, timestamp, rotation. |
| New `accounts.recovery_candidate(conn, *, email=None, user_id=None)`, a dict or `None`: `id`, `email`, `role`, `is_active`, `has_password`, `email_verified_at`, `session_key`, read from the row under the caller's lock. Never the hash. | Eligibility (1.3) needs `email_verified_at`; `_USER_COLUMNS` is not widened, the precedent of `profile_requirement` (PR 6). A database below migration 021 has no column: the function raises, and recovery is unavailable (the production start refuses such a database anyway). |
| New public helpers in `registration`: `control_blocked(conn, now, *, hold=True)`, `raise_mark(conn, now)`, `writer_now(conn, clock)`; the existing `availability`, `address_capped` and `email_hash` are used as they are. | One hold, one mark, one admission rule; no copy that could drift. Thin wrappers over `_blocked`, `_raise_mark`, `_writer_now`, no behaviour change. |
| Registration `settle_send`: `AND recovery_id IS NULL` (3.6). | Cross-purpose settlement refused in both directions. |
| `registration_scrub` unchanged; a new standard-library module `recovery_scrub` (6.3). | The backup imports it without the application. |

### 4.3 Password timestamp and hash policy

- The new hash is made by `set_password` with the current method; the
  password is the exact submitted string: not stripped, collapsed,
  normalised or cut (registration design 1.1). 10–256 characters, not the
  email.
- `password_changed_at` and `updated_at` are SQLite's `CURRENT_TIMESTAMP`, as
  for every password change. No code reads `password_changed_at` for a
  decision (checked: only `manage_accounts` lists it). Its clock is the same
  system clock as the application's; a regressed clock is recorded as it is
  and decides nothing.
- The old hash is replaced, not kept.

### 4.4 Sessions, throttle and other rows

- **Session invalidation.** The rotation in `set_password` signs every device
  out at its next request (`load_session_user` compares the key). The
  redeeming browser had no session (signed-in browsers are redirected away),
  and none is issued (A8, D7).
- **Throttle (RD8).** `/login` answers a throttled address with
  „Твърде много опити…“ and status 429, and any other failure with
  „Грешен имейл или парола.“ (`web/blueprints/account.py`). The throttle is
  keyed by the address whether or not an account exists, so anyone can keep
  an address locked by failing sign-in ten times in fifteen minutes and
  then watch that answer.
  - **(a) No (recommended in revision 2).** A reset neither reads nor writes
    `login_throttle`. The 429 continues until the window ends, exactly as for
    an address without an account, so the lock-and-watch observer learns
    nothing. Cost: a person who locked themselves out before recovering waits
    up to 15 minutes after the reset; the success notice says so to everyone
    (2.7).
  - **(b) Yes** (revision 1's recommendation). Clear the address's row in
    the redemption transaction, with the key `/login` uses
    (`login_throttle.identifier_hash(SECRET_KEY, users.email)`, which is
    `throttle_identifier` of a valid address). The observer's next failed
    sign-in is then answered „Грешен имейл или парола.“ before the window
    ends, which tells them, without a code, that the address has an account
    and that a reset (or another successful clearing) has just happened.
    `/account/password` already clears the throttle the same way today; it
    is reached only by a signed-in owner, and that existing property is not
    changed here.
- **Account-wide invalidation (RD17, an interpretation of A8).** The
  redemption writes no other recovery row. Every other row of the account
  pending at that moment was issued under the old `session_key`; the
  rotation in step 11 makes its digest stale, so its correct code is refused
  at step 10 (`INVALIDATED`) and it can never reset the password. Rows of the
  address that are shadows can never reset anything in any case. Those rows
  keep their address, link, digest and code hash until they expire and the
  cleanup scrubs them (at most 30 minutes plus the cleanup interval, 6.1),
  instead of being scrubbed at once: the retention cost of RD17. Rows of other
  accounts are not touched.
- **Other key changes** (operator commands, `/account/password`, TOTP) do not
  void recovery rows explicitly either; the digest refuses their codes at
  redemption, and the rows expire and are scrubbed on schedule. No existing
  writer is changed for recovery. A reset by recovery is therefore handled
  exactly like every other key change.
- **A plain sign-in does not end a recovery in progress** (it changes no key).
  Proposed as is; sign-in stays unchanged (A2).
- **Pending registrations.** For an address with an account they are shadows
  and are not touched. A real pending registration of the address (begun
  before the account existed) ends as `EMAIL_TAKEN` at its own verification.
  Recovery reads and writes no registration row.
- **Google identities, profiles, assignments, attempts and their markers**:
  neither read nor written.

### 4.5 `not_sent` in recovery (RD7, an amendment)

Registration retires a `not_sent` code, waives the wait and says so
(registration design 4.3). The approved requirement A10 gives recovery "the
... settlement ... of sections 3, 4 and 6", which includes that behaviour.
**This section proposes to amend it for recovery**; until Gabriel selects
RD7, the approved settlement of 4.3 is the requirement. Proposed: recovery
does none of this: the outcome goes to the ledger (and so to the circuit), the
code stays, the wait applies, the page is the same. Reasons:

- under RD2 (b) a shadow is never sent, so it can never be `not_sent`; any
  visible `not_sent` behaviour (the message, an immediate resend, a code that
  stops counting attempts) would mark real rows;
- under RD2 (a) both kinds can be `not_sent`, but one rule for both options
  keeps a single code path and a single test matrix;
- keeping a code that never left costs nothing: nobody has it, and it is
  counted in the guessing bound like any code.

Cost: when the relay was unreachable, the person waits a minute for a
message that never comes and then resends.

## 5. Budgets, clocks and holds

### 5.1 One ledger, one budget

`registration_sends` holds every attempt to send, of both purposes. No new
counter, table or setting counts mail:

| Counter | Counted by | Includes recovery |
| --- | --- | --- |
| Total budget, 300 per hour (or `DZI_MAIL_HOURLY_CAP` downwards) | `COUNT(*)` of the ledger in the hour | yes, automatically |
| Per address, 5 per hour and 8 per 24 h 15 min | the ledger by `email_hash` | yes, with the shared label (5.2) |
| Circuit | the ledger's settled rows by id | yes (5.3) |
| Per recovery row, 4 sends | `password_recoveries.send_count` | its own |
| Per code, 5 attempts | `code_attempts` | its own |

Nothing is refunded, by any outcome or by any path. Every request to a
well-formed address costs one ledger row, whatever the account, so no cap
can be read as an answer.

**Pending limits.** No separate cap on pending rows is proposed: every row
begins with an issuance, so the issuance caps bound them. An address can have
at most 5 rows created in any 30 minutes (its hourly cap), hence at most 5
pending recoveries at once, each with one live code; the site at most 300
(the total budget) at once. A per-account limit would add nothing, since an
account has one `users.email`. A per-browser limit exists by construction:
one slot, and a new request voids the previous row.

### 5.2 Shared per-address budget (RD5)

The recovery ledger row is keyed with `registration.email_hash`, so an
address's 5 per hour and 8 per `W` (24 h 15 min) are one budget for
registration codes, registration notices, recovery codes and recovery
notices together.

- **For:** an address receives at most 8 unwanted messages in any `W` from
  this application, not 16; the guessing bound of registration design 3.3
  applies unchanged to the union (5.4); one operator command clears one
  budget.
- **Against:** someone who submits registrations for an address uses up its
  recovery too. That costs the attacker nothing they could not do through
  recovery directly; separate budgets would only double the mail.
- `clear-registration-limit --email` therefore also releases the recovery
  budget; its printed consequence must say so (6.5).

### 5.3 Shared circuit (RD6)

Registration's `_circuit_open` reads the three most recent settled rows by
generation id (`refused` and `started` left out). With recovery rows in the
same table it sees both purposes, ordered by the one `AUTOINCREMENT` sequence,
so the argument of registration design 3.5 (late settlements cannot reorder)
holds unchanged. One relay has one health; a separate circuit would admit
three more failing attempts per five minutes. The upper bound of registration
design 3.5 (36 failing attempts per hour, one synchronous worker) covers both
purposes together.

The circuit decides requests and resends. It does not decide redemption,
which sends nothing; redemption is decided by the hold and the clock only.

### 5.4 Guessing bound (quantitative; RD4, an amendment)

Under the three assumptions of registration design 3.3 (clock not stepped,
key unchanged, ledger intact: no restore, released hold or limit clearing),
**at most eight guessable codes of one address in any 24 hours, of either
purpose together, and therefore at most eight of them recovery codes**,
because each code lives at most 10 minutes and at most 8 issuances of the
address lie in any `W`. The code lifetime must stay 10 minutes for `W` to
remain valid.

Revision 1 said that for an address with an eligible account every
guessable code is a recovery code. That is wrong: a **real** pending
registration of the address begun before the account existed keeps
`is_shadow = 0` (registration decides it at submission only) and can still
resend registration codes after the account exists, until it expires. Those
codes share the eight. They cannot take over the account (a correct one ends
`EMAIL_TAKEN`, registration design 2.4), so the takeover bound is on recovery
codes alone: at most eight recovery codes, at most **40 recovery guesses per
targeted account per day**, fewer when registration codes or notices use part
of the eight.

| | 6 digits | 8 digits |
| --- | --- | --- |
| Per targeted account, one day | 4×10⁻⁵ | 4×10⁻⁷ |
| Same, every day for a year (≈ 365 × the daily figure) | ≈ 1.5 % | ≈ 0.015 % |
| Whole site at the 300-per-hour budget for a year (300 × 24 × 365 codes × 5 guesses; needs about 900 targeted eligible accounts a day, since each takes at most 8) | ≈ 13 expected takeovers | ≈ 0.13 |

Arithmetic, not measurement. A daily attack sends the victim 8 unwanted
messages a day; a site-wide one keeps registration and recovery closed for
everyone (budget used) and is logged as `total_cap_reached`. Registration
accepted 6 digits for creating an account under someone's address; recovery
grants an existing account with its data, so **8 digits are recommended** for
recovery codes. **This is an amendment:** D6 approved a six-digit code and
A10 gives recovery registration's generation; until Gabriel selects RD4, six
digits are the requirement and the 6-digit column above is the bound. The
input rule stays registration's: trim at the ends, then exactly 8 ASCII
digits; inner whitespace is wrong. The other D6 values are reused unchanged.

**Exceptions, stated as in registration design 3.3 and 3.8:** clock steps
(two steps of 300 s already admit 13 codes in a real day); secret-key rotation
(a fresh budget, up to 16 codes around it); a released restore hold (up to 8
more codes in a day that saw 8); `clear-registration-limit` (the same);
repeated forward steps of the clock (no bound). Each now applies to recovery
codes too. **They accumulate:** each occurrence admits its own further
budget, and occurrences of different kinds add up. Two key rotations, or a
released hold followed by a `clear-registration-limit` for the same address
inside one day, each admit up to eight more codes again; nothing in the
design caps the number of such events, so across repeated events no daily
bound is claimed, only the per-event figures above.

### 5.5 Unavailable before expensive work

| Request | Checked before any write or process | Checked again under the lock |
| --- | --- | --- |
| Request | hold, clock, unreaped sender, circuit, total budget, address caps | the same, before the inline cleanup and any row |
| Resend | — | the binding's row read (none or final: answered, nothing written), then hold and clock, then expiry, sender, circuit, budget, row limits, address caps (3.5) |
| Redeem | slot, row state and password rules (reads) | the binding's row read, then hold and clock, then expiry and the code; scrypt only after a correct code and a passed recheck (3.5) |

No scrypt is computed for a request or a resend (neither has a password,
for either kind of row), and none for a wrong code. The only expensive work an
unauthenticated client can cause is a send, which every cap above precedes.

### 5.6 Recovery attempts and the sign-in throttle (RD10, an amendment)

The approved registration design 3.4 says `login_throttle` "is reused where
its policy fits (sign-in and, later, recovery attempts)" (A16). This addendum
recommends **not** counting recovery attempts in it, which **amends** that
sentence: the throttle is keyed by the address, so anyone typing wrong
recovery codes from their own browser would lock the victim out of sign-in
for 15 minutes at a time. The guessing bound already rests on the per-code
and per-address counters, and Nginx's `lp_login` zone limits the rate per
client. Until Gabriel selects RD10 the approved sentence stands; the proposed
change is appended to registration design 3.4 as a labelled proposal, not
written over it. With RD10 and RD8 (a) recovery neither reads nor writes the
throttle at all; with RD8 (b) it only clears it on success.

### 5.7 Clock and hold

The shared `registration_control` row governs recovery too:

- every recovery writer but `settle_send`, in the order of 3.5, reads its own
  row by binding (writing nothing), then checks the hold (not `cancel` and
  `cleanup`), then the clock against the high-water mark, **before** any
  expiry, scrub, voiding, deletion or other recovery change, and writes nothing
  at all when either fails;
- every issuance and every cleanup run raises the mark and never lowers it;
  `registrations-clock-accept` remains the only way down;
- the `restore` hold set by the restore gate closes recovery until
  `registrations-release-hold --acknowledge-lost-history`; nothing releases it
  automatically;
- an account's lifecycle clock-health record is not consulted.

## 6. Operations

### 6.1 Cleanup and logical retention

| Data | Valid for | Logical-retention target |
| --- | --- | --- |
| A pending row's address, account link, session-key digest, code hash | 30 minutes | Scrubbed by the first cleanup after expiry: about 45 minutes after the request. At once on the row's own redemption, cancellation, replacement or invalidation. Not at once when **another** row of the account is redeemed (RD17): such rows keep these values until their own expiry and cleanup. |
| A final row (state, binding hash, `is_shadow`, counters, times) | — | Deleted 24 hours after creation. |
| A ledger row | the cap windows | Deleted 48 hours after the attempt, by registration's cleanup, unchanged. |

`recovery_scrub.cleanup(conn, now)` (6.4) expires and scrubs pending rows past their time
(or dated beyond the skew in the future, once the clock is not behind the
mark) and deletes final rows older than 24 hours. It needs no secret and
ignores gates and holds.

**Integration point:** `manage_accounts registrations-cleanup` runs, in its
existing single `BEGIN IMMEDIATE` and after its existing restore-lock checks,
`registration.cleanup` and then, only if that did not report `clock_behind`,
`recovery_scrub.cleanup` with the same clock. `--check` fails when a pending
registration **or** a pending recovery is older than an hour. The systemd
units, the timer interval and the restore script's handling of the cleanup
job are unchanged. A request's inline cleanup covers recovery rows only.

### 6.2 Restore gate, status and hold

The restore script calls `manage_accounts registrations-restore-gate` on the
staged copy (its step 4b) and `registrations-restore-status --expect held` in
its final check and after the replacement. **The script is not edited**; the
commands gain recovery:

- `registrations-restore-gate`: in its one transaction,
  `recovery_scrub.void_pending(conn, now)` beside `registration.restore_gate`
  (every pending recovery becomes `void` and is scrubbed); the hold is set by
  the registration gate as today and covers recovery. Repeatable, atomic,
  never clears a hold.
- `registrations-restore-status --expect held`: fails while any pending
  recovery exists, as well as for the existing conditions; reports the count.
- `registrations-release-hold`: its printed consequence also names recovery
  codes ("an address that already received 8 codes in a day may receive up to
  8 further codes, registration or recovery"), and says that it repairs no
  account: restored old passwords and session keys are the credential repair
  of 6.6, a separate step.
- The restore lock and lifetime-lock protocol, the timer notes, the cleanup
  coordination and the fingerprint steps of `deploy/learnpilot-restore.sh` are
  not touched. A static test pins that the script still calls the two commands
  at the same positions (before the final fingerprint, and after the
  replacement).

Effect on a restored database: a code that was cancelled, spent or replaced
after the snapshot does not return. A reset made after the snapshot is undone
with everything else: the **old password hash** returns, so the old password
signs in again, and the **old session key** returns, so cookies the reset had
ended become valid again. The gate does not touch accounts and cannot repair
that; 6.6 proposes what the operator does about it.

### 6.3 Snapshot scrub

`src.backup_sqlite.backup_database` calls a scrub of the isolated copy before
its `quick_check`, fsync, link and hash. It gains
`scrub_pending_recoveries(dst)` beside `scrub_pending_registrations(dst)`,
in the same place; a failure publishes nothing and exits non-zero. The result
gains `recoveries_scrubbed`. Logical only: free pages, unused page space, the
WAL, earlier snapshots and copies below the application can keep old bytes
(registration design 6.5 applies unchanged). Published snapshots are never
rewritten.

### 6.4 Schema checks that do not skip sensitive storage (RD13)

Built in scope 8.1, together with the migration and the scrub that depend on
it, and blocked there by RD13 (10). No scope ships the table, or a scrub of
it, without this check.

`web/services/recovery_scrub.py` (standard library only):

- `SCRUBBED_COLUMNS = ("email", "user_id", "session_key_digest", "code_hash",
  "code_expires_at")` and `KNOWN_COLUMNS`, the full column list of the
  migration.
- `has_recovery_storage(conn)`:
  - table absent and the recovery migration **not** recorded in
    `schema_migrations` (or no such table): `False`, nothing to scrub (a
    database from before the migration);
  - table absent but the migration recorded: raises
    `UnsupportedRecoverySchema`;
  - table present with a column set other than `KNOWN_COLUMNS`, missing or
    extra: raises. An unknown column could hold data this module does not
    scrub; a later migration that adds one must update this list in the same
    PR.
- `void_pending(conn, now)`: one `UPDATE` to `void` with every scrubbed column
  NULL.
- `cleanup(conn, now)` (6.1) lives here too, not in the service: it needs no
  secret, and scope 8.1 needs it before the service exists.

Callers: the backup scrub (raises → `BackupError`, nothing published), the
restore gate and status commands (non-zero exit, the script stops before
anything is replaced), the cleanup command. Registration's own check
(required columns only) is not changed.

**Older snapshots.** A snapshot from before the migration has no table; the
restore script migrates the copy before the gate, so the gate finds the empty
table. A snapshot made while a build without the recovery integration ran on
a migrated database could hold unscrubbed pending recoveries; the scope order
of section 10 makes such a build impossible to produce from the plan (7.3),
and the gate voids them if such a snapshot is ever restored.

### 6.5 Operator CLI

No new command is required for recovery itself. RD18 (a) proposes one new
read-only command for restores (6.6). Changed help and output:

| Command | Change |
| --- | --- |
| `registrations-cleanup [--check]` | Also recovery (6.1). |
| `registrations-restore-gate` | Also voids pending recoveries (6.2). |
| `registrations-restore-status` | Also counts them (6.2). |
| `registrations-release-hold` | Consequence names recovery (6.2). |
| `registrations-clock-accept` | Unchanged; the mark is shared. |
| `clear-registration-limit --email` | Its note says it also releases the address's recovery budget, and that a recovery code of the address stops working. |
| `set-password`, `set-role`, `disable`, `enable`, `sign-out`, `clear-throttle`, `totp-*`, `create`, `list`, `cutover-check` | Unchanged (A9). |
| `restore-credential-diff --restored <file> --replaced <file>` | **New, read-only, only if RD18 (a) is selected** (6.6). |

`require_registration_tables` gains the recovery table and column once the
migration is recorded, with the same "apply migrations first" error.

### 6.6 Restore and credentials (RD18)

**What a restore brings back.** Every column of `users` returns to its value
in the snapshot. For credentials that means:

- **old password hashes.** A password changed after the snapshot (by a
  recovery, by `/account/password`, by `set-password`) reverts: the old
  password signs in again. If the change was made because the old password
  was known to someone else (the usual reason to recover), the restore hands
  the account back to that person. A sign-out does not help: it rotates the
  key and leaves the old password valid;
- **old session keys.** A rotation after the snapshot (a password change, a
  sign-out, a role change, a disable, a TOTP change) reverts: cookies it had
  ended are accepted again, until the next rotation;
- **old account state**: a disable, a demotion, a role change after the
  snapshot revert too; and **old TOTP state** (`totp_credentials`,
  `totp_recovery_codes`): an admin's reset authenticator or used recovery
  codes return.

Registration design 3.9 already states that "a password changed after the
snapshot returns to the old one, as with any restore today". Recovery makes
post-snapshot resets common and makes them the response to a compromise, so
this addendum proposes handling it. It does **not** change the restore
script or its ownership protocol: the script still ends by starting the
service, as its last command.

**What the operator compares, read-only.** The script keeps what it
replaced (`$STATE/replaced-<stamp>/questions.db`, a hard link made after the
database was written out completely into its main file; the script's
"keep the database being replaced" step), and the snapshot is a published
file that is never rewritten. The script already pairs the database being
replaced (at the live path, before the replacement) with the snapshot for
assignments (`manage_lifecycle reconcile --pre-restore-db --snapshot-db`);
the credential comparison is the same pairing, run by the operator after the
replacement on the kept copy, writing nothing. Comparing those two
needs neither the live database nor the running service, and nothing is
written: both are opened with `mode=ro&immutable=1` (a plain `mode=ro` can
create `-wal`/`-shm` files beside a WAL database) and Python runs with `-B`.
The comparison is between **the snapshot's `users` and TOTP rows (what the
restore put back) and the replaced database's (what was live)**. The restore
migrates its copy, but no migration rewrites the compared columns; the
command refuses if the two databases' `users` columns differ from the set it
knows.

For every account id present in either database, the command reports one
class and never prints a hash, a key or a TOTP value (equal or different
only):

| Evidence | Class | Why |
| --- | --- | --- |
| Present in both, same email; `password_hash`, `password_changed_at`, `session_key`, `role`, `is_active` and TOTP rows all equal | **unchanged (as far as compared)** | Equality is evidence, not proof: see below. |
| `password_hash` differs, or `password_changed_at` differs, or exactly one side is NULL | **password reverted** | The restored password is the old one. |
| `password_hash` differs and `password_changed_at` is equal (both values or both NULL) | **uncertain** | Possibly a hash upgrade at sign-in (`authenticate` rewrites the hash without the timestamp, `web/services/accounts.py`), possibly a password change the timestamp did not record (a stepped clock, a change inside the same second, a direct edit). Not decidable from these columns. |
| `password_changed_at` equal and hashes equal, but `session_key` differs | **sessions revived** | No password change is visible; a rotation after the snapshot was undone. |
| `role` or `is_active` differs | **state reverted** | A disable, demotion or role change was undone. |
| TOTP rows differ (presence, confirmation, recovery codes used) | **second factor reverted** | An admin's authenticator or recovery codes returned. |
| Only in the replaced database | **created after the snapshot** | Gone; no credential revived. Its address is free again for registration. |
| Only in the snapshot, or the same id with another email | **uncertain** | The replaced database has no such account under that id and address: an unsupported edit, or a database that is not the one replaced. |

**Equality is not proof.** `password_changed_at` is SQLite's
`CURRENT_TIMESTAMP`, one second of resolution, from a clock that can be
stepped; equal timestamps do not prove that no password changed, and NULL on
both sides proves only that neither database recorded a change. Equal hashes
are stronger evidence (every `set_password` writes a fresh salted hash), but
a direct edit could write an old hash back. The command says "unchanged as
far as compared", never "safe".

**When the replaced evidence is unavailable** (the restore ran with
`--replaced-database-unreadable`; the directory is missing; the file fails
`quick_check`; its schema is not one the command knows): nothing can be
compared, so **every account with a password is uncertain**, and every
session key with it. The command says so and exits non-zero.

**What the operator does before reopening (proposed policy, for Gabriel to
decide).** The restore script starts the service as its last command, so a
restored old password is usable from that moment. Proposed runbook,
without editing the script:

1. Optionally before the restore: the same comparison between the snapshot
   and a fresh `src.backup_sqlite` snapshot of the live database, as a
   preview, so the list is known in advance. It is not the evidence: changes
   can happen between that backup and the service stop.
2. Run the restore script, unchanged.
3. As soon as it has finished, stop the service (`systemctl stop`; the restore
   lock is gone by then, so nothing in the script's protocol is affected).
4. Run `restore-credential-diff` against the snapshot and
   `$STATE/replaced-<stamp>/questions.db`.
5. For every account classed **password reverted**, **uncertain** or
   **second factor reverted**: a **reviewed password replacement** (the
   operator runs `set-password` with a new password and either hands it to
   the verified owner out of band or discards it, so the owner recovers by
   email once recovery is open again) or a **disable**. Not a sign-out alone,
   which leaves the old password valid. For TOTP, `totp-reset` and a new
   enrolment as well. For **sessions revived**: `sign-out`. For **state
   reverted**: reapply the replaced state (`disable`, `set-role`).
6. Start the service. This is the "safe reopening".

Between the script's own start (step 2) and the stop (step 3) the restored
credentials are live, for as long as the operator takes; the preview of
step 1 shortens it, and only a change to the script (not proposed) could
remove it. Accepting that window is part of RD18.

**Credential repair is not the hold release.** `registrations-release-hold
--acknowledge-lost-history` restores registration's and recovery's
**accounting** (the ledger, the caps, the circuit); it repairs no account, and
sign-in is open whether the hold is set or not. Credential repair (steps 3–6)
is about who can sign in. Proposed order: repair, reopen, then release the
hold when the operator has accepted the lost accounting; the release's
printed consequence names the difference.

**Options (RD18):** (a) the runbook with the read-only command above,
recommended; (b) the same runbook with read-only SQL in `docs/DEPLOYMENT.md`
(`sqlite3 'file:...?mode=ro&immutable=1'` with both databases attached),
cheaper to build and harder to get right under pressure; (c) a sign-out note
only, as revision 1 had, which leaves restored old passwords valid and is
not recommended. Separately for Gabriel: whether "unavailable evidence makes
every password account uncertain" is accepted, which forces a reset or a
disable of every such account after such a restore. Tests: test plan
RR8–RR15.

## 7. Rollout and compatibility

### 7.1 Gate and configuration

- `recovery.PRODUCTION_ACTIVATION = False`, a build constant like the three of
  registration design 7.3; no setting or environment variable overrides it.
  While it is off every `/recover...` route is the generic 404, `/login` has no
  link, no row is created and no code is checked.
- Independent of `registration.PRODUCTION_ACTIVATION`. With RD1 as
  recommended it has nothing to recover before registration has created
  verified accounts; D10 places its activation before public opening, so the
  intended order is recovery on no later than the student registration gate.
- **Settings.** A `RecoverySettings` (mailer, total cap) under its own config
  key, built from the same `mail.load_settings` and `mail.build_mailer` as
  registration's. A production start builds it only while the recovery gate is
  on; `production_config_errors` reports malformed mail settings while either
  gate is on; `console` and `file` stay refused in production. Development
  enables recovery as it enables registration (usable development mail
  settings); `DZI_MAIL_BACKEND=console` shows the code in the terminal.
- `mail.sender_busy()` is per process and shared: one sender per worker,
  whatever the purpose.
- **Pending rows when the gate is turned off**: unreachable (404) and scrubbed
  by the next cleanup after they expire. Nothing redeems.

### 7.2 Schema compatibility

- The migration is additive: one table, one nullable column with a CHECK, three
  indexes. Existing rows already satisfy the CHECK (`recovery_id` NULL).
- Code from before the migration names its columns and runs unchanged on the
  new schema; registration's caps and circuit then also count recovery rows,
  which is the intended direction (never a second budget).
- As for every migration since the lifecycle release, a production build
  without the migration refuses to start on the migrated database, so
  "rolling back" means a later build that keeps the migration
  (`docs/ACCOUNTS_DESIGN.md`, migration table).

### 7.3 Old-code hazards

| Hazard | When it can occur | Constraint |
| --- | --- | --- |
| A build that ships the migration but not the recovery integration (cleanup, gate, status, snapshot scrub) leaves pending recovery rows unscrubbed in the live database and in snapshots, and unvoided by a restore | A partial or hand-made build | **Prohibited, and made impossible by the scope order** (10): the migration arrives in scope 8.1 together with the cleanup, the restore gate and status, the snapshot scrub and the schema allow-list. No scope produces a build with the table and without them. |
| A build that can create recovery rows without that integration | A build with the service and not the operations | **Prohibited**: no build capable of creating a recovery row may lack the cleanup, restore and snapshot integration. The service that creates rows arrives in scope 8.2, after 8.1's integration is merged; scope 8.1 contains no code that inserts a recovery row or a recovery ledger row (pinned by test RZ1). |
| A build whose row-creating code is present but unreached | Scope 8.2 merged, 8.3 not yet | **Not argued safe because routes are absent.** It is acceptable because (i) the integration of 8.1 is already present, so a row created by any path (a later bug, a developer's shell, a mis-wired import) is still expired, scrubbed, voided at restore and scrubbed from snapshots; and (ii) test RZ2 pins that no production entry point reaches the service's writers: no import from the application factory, the WSGI module, blueprints or startup hooks; no URL rule; no `manage_accounts` or other CLI subcommand; no Gunicorn hook, systemd unit or deploy script. |
| Development checkouts of older branches on a migrated development database | Development only | No rows exist unless recovery was used there; nothing is published. |
| Pending recoveries when a gate is turned off | Production after activation | Unreachable; scrubbed on schedule; nothing redeems. |
| Registration `settle_send` from before the companion change settling a recovery row | Only through a mis-wired call | Ledger-only effect; the change of 3.6 closes it. It ships in 8.1 with the column. |

While the recovery gate stays off in production, no recovery row and no
recovery ledger row is expected there: the gate (scope 8.3) and the no-caller
tests (scopes 8.1, 8.2) say so, and the integration of scope 8.1 handles one if it exists
anyway. A rollback then has no recovery data to carry.

### 7.4 Deployment order (for a later, separately authorized rollout)

1. Migration and code with every gate off (PR 8 merged). Nothing changes in
   production.
2. Confirm the cleanup timer, the restore gate and status and the snapshot
   step cover recovery (the Linux evidence of 9), and rehearse the restore
   credential runbook of 6.6 on a staging copy.
3. Mail settings and the evidence of registration design 0.3 (shared).
4. Gabriel's decisions RD1–RD18 (including the amendments RD4, RD7, RD10 and
   the interpretations of RD2 (a) and RD17), D16 amended for recovery, the
   wording.
5. Recovery gate on, no later than the student registration gate.

Each step's rollback is the previous state: set the constant back; the tables
stay.

### 7.5 Nginx (RD14)

The recovery locations mirror registration's (`deploy/nginx/learnpilot.conf`,
the block under "Registration"; registration design 3.2, `docs/DEPLOYMENT.md`
§33), for the same reasons:

| Location | `limit_req` | Why |
| --- | --- | --- |
| `location = /recover` | `zone=lp_register burst=90 nodelay` | A POST sends mail. The zone is keyed on POSTs only, so the GET form is not limited. |
| `location = /recover/resend` | `zone=lp_register burst=90 nodelay` | Sends mail. |
| `location = /recover/verify` | `zone=lp_login burst=40 nodelay` | A guess at a credential: the sign-in budget. |
| `location /recover/` (prefix) | none | `/recover/cancel` (a POST that sends nothing) and any unknown path under `/recover/`, which the application answers 404. |

Besides its `limit_req`, if any, every one of the four carries exactly these
lines, as the registration locations do:

```nginx
add_header X-Content-Type-Options "nosniff" always;
add_header X-Frame-Options "DENY" always;
add_header Referrer-Policy "no-referrer" always;
proxy_hide_header Referrer-Policy;
proxy_pass http://127.0.0.1:8000;
```

- **One effective `Referrer-Policy`.** An `add_header` in a location replaces
  every server-level `add_header`, so the other two security headers are
  repeated and the server-level `strict-origin-when-cross-origin` is not
  inherited; `proxy_hide_header` drops the application's own
  `no-referrer` (2.6), so exactly one value reaches the browser on every
  address under `/recover`, including error responses (`always`).
- **Prefix coverage.** `location = /recover` and `location /recover/` together
  cover the four routes and every other path under `/recover/`. A path such as
  `/recovery` or `/recover-x` is not a recovery route; it falls to
  `location /` and is the application's ordinary 404.
- No `proxy_set_header` in these locations, so the server-level ones stay
  inherited; no recovery value is in a query string, so the access log format
  is unchanged.
- The zones' values are registration's proposals; recovery adds its traffic
  to `lp_register` and `lp_login`. A class-sized run that mixes both is
  activation evidence (9).

Tests: test plan RP5 (effective location, effective headers and limits of
each address, resolved as Nginx resolves them, as `NginxTemplateTest` does
for `/register`).

## 8. Logging and residual risks

### 8.1 Logging

Logger `learnpilot.recovery`. Lines hold an event name, the recovery row id,
the send generation where there is one, and, for failures, an exception class
or an outcome: `requested`, `send_started`, `send_settled outcome=<o>`,
`resent`, `wrong_code`, `redeemed recovery=<id> user=<id>`, `invalidated`,
`cancelled`, `expired`, `gate_off`, `unavailable reason=<r>`,
`dispatch_refused`, `dispatch_failed error=<class>`, `settlement_refused`,
`settlement_failed error=<class>`,
`redeem_failed error=<class>`, `cleanup scrubbed=<n> deleted=<n>`. The
registration logger keeps its events for the shared states (`restore_hold`,
`clock_regressed`, `circuit_open`, `total_cap_reached`, `sender_unreaped`).

**Never logged, at any level:** the address, the code, the binding token, the
password, any hash, the session key or its digest, the issuance identity, the
message, and whether a row is a shadow (no line **names** it). `user=`
appears on `redeemed` only, which follows a correct code. No recovery value is
ever in a query string, so the access log holds none.

**What the presence of a line reveals, by option.** Naming nothing is not the
same as distinguishing nothing:

- **Under RD2 (a)** every request and resend dispatches one message, real or
  shadow, so the same send-related lines appear for both: `send_started`,
  `send_settled`, and on failure `dispatch_failed`, `settlement_failed`, the
  mail module's `sender_error` and `sender_unreaped`, and registration's
  `circuit_open`. Their **outcomes** may differ where the relay treats a code
  and a notice differently (registration design 3.7), as for registration.
  Before redemption no line's presence distinguishes a shadow from a real row.
- **Under RD2 (b)** a shadow dispatches nothing. Every send-related line above
  then exists for real rows only: a log reader who pairs `requested
  recovery=<id>` with a following `send_started recovery=<id>` learns which
  requests found a recoverable account. The ledger shows the same in the
  database (a real row's ledger row is settled; a shadow's stays `started`).
  **This is stated, not prevented.** The concrete alternative, logging a
  `send_started` and an invented outcome for a shadow, was rejected: it would
  record sends that never happened, it cannot cover the mail module's own
  lines or the ledger, and it would make the operator's evidence false. The
  logs and the database are the operator's, not the requester's; who may
  read the journal is an operations matter outside this design. Under (b) the
  invariant is therefore only: no line **names** the shadow state, and no
  line written before the dispatch decision differs.

Test plan RLog2 asserts the invariant of the selected option, and no more.

### 8.2 Residual risks, stated and not solved

- **Mailbox takeover is account takeover**, for every eligible account. A
  recycled or shared school mailbox hands the account to its new reader;
  nothing re-verifies an old address (registration design 5.3).
- **Phishing the code** works if the owner hands it over.
- **Timing.** Under RD2 (a) as registration: a relay may treat a code and a
  notice differently in time or outcome. Under RD2 (b) only an eligible
  address causes a send; with current synchronous dispatch, likely
  distinguishable; not measured (1.4). No option claims equal timing.
- **Logs under RD2 (b)** distinguish real rows by the presence of send events
  (section 8.1); operator-visible only.
- **Polling.** Under RD17 recovery's own pages do not reveal another browser's
  reset before a correct code (1.4). Under RD8 (b) `/login` does, to someone
  who keeps the address's sign-in locked (4.4). Under RD17's alternative the
  verify page does, to anyone who requested recovery for the address.
- **Circuit.** A relay that fails for one kind of message only can be
  observed through the shared circuit (registration design 3.7); under RD2 (b)
  any failing relay makes real rows close the circuit and shadows not.
- **Guessing**: the bound of 5.4 holds only under its assumptions, and its
  exceptions accumulate; at 6 digits a year-long daily attack on one account
  succeeds with about 1.5 %.
- **Retention under RD17**: other pending rows of a reset account keep their
  address, link, digest and code hash until they expire and are scrubbed
  (about 45 minutes), not at the reset.
- **Denial of recovery**: an address's daily budget can be used up by anyone,
  closing its registration and recovery for about a day; the total budget for
  everyone for up to an hour.
- **Restore undoes a reset**: the old password hash returns, so the old
  password signs in again, and the old session key returns, so the sessions
  the reset ended return. Any restore has this property today; recovery
  makes it more likely to matter. A sign-out alone does not repair it. RD18
  proposes a read-only comparison and a reviewed replacement or disable
  before reopening (6.6); the restore script still starts the service itself,
  so the restored credentials are live until the operator stops it, and the
  comparison's equalities are evidence, not proof.
- **No change notice** (RD9): an owner whose account was reset by a mailbox
  intruder learns it at their next failed sign-in.
- **Logical scrub only**; old bytes may remain (registration design 6.5).
- **Senders can overlap** across a worker replacement (registration design
  4.2).

## 9. Activation prerequisites (evidence; none done)

| Prerequisite | Evidence |
| --- | --- |
| Shared with registration | Everything of registration design 0.3 that concerns mail, capacity, Nginx and Linux: the deadline and orphan checks, transport security, provider and sender, delivery, capacity (now for registration and recovery together under one budget), the cleanup timer, the restore script with its gate, the snapshot step. |
| Recovery-specific Linux evidence | The cleanup timer scrubbing an expired recovery on a quiet server; a restore of a staging copy with a pending recovery (voided, status passing, hold set); the snapshot step on a copy with a pending recovery followed by a test restore. |
| Restore credential runbook (RD18) | A rehearsal on a staging host: a password reset and a sign-out made after a snapshot, the restore script unchanged, the service stopped after it, `restore-credential-diff` (or the SQL of option (b)) classifying both accounts, the repair, the start; and a rehearsal with the replaced evidence unavailable. The window between the script's start and the operator's stop measured, not assumed. |
| Timing | Under RD2 (a): measured durations of a recovery code and a recovery notice through the chosen provider. Under RD2 (b): the recorded acceptance that, with synchronous dispatch, eligibility is likely distinguishable by timing (not measured), or a measurement. No equal timing is to be claimed from either. |
| Lock time | The measured duration of `set_password` under the writer lock on the server. |
| Nginx | The locations of 7.5 accepted by the real `nginx -t` and observed: one `Referrer-Policy: no-referrer` on each `/recover` address, including a 404 and a 429; a class-sized run that mixes registration and recovery from one address. |
| Decisions | RD1–RD18, with the amendments (RD4, RD7, RD10) and interpretations (RD2 (a), RD17) explicitly accepted or declined; D16 amended for recovery (and for notices to addresses without an account under RD2 (a)); the wording. |

## 10. Implementation plan

**One scope at a time, in this order**, each green on the full suite, every
gate off, documents updated with what each makes untrue. Executor for each:
**Claude Opus, High**. Independent security review of each in a **separate
Opus, High session**, at the exact SHA, before the next scope starts.

**The rule the order serves:** no build capable of creating a recovery row may
lack the cleanup, restore-gate, restore-status and snapshot-scrub
integration. Revision 1 put the migration and the service in 8.1 and the
integration in 8.2, which contradicted its own 7.3 and rested on the absence
of routes; revision 2 moves the integration into the scope that introduces
the table, and the row-creating service after it.

| Scope | Content | Needs first | Blocks by decisions |
| --- | --- | --- | --- |
| **8.1 Storage and operations** | The migration (next free number: table, ledger column with the at-most-one CHECK, index); classification; registration's `settle_send` guard (`AND recovery_id IS NULL`); `web/services/recovery_scrub.py` with the column allow-list, `has_recovery_storage`, `void_pending`, `cleanup` and the snapshot scrub (6.3, 6.4); the CLI changes of 6.5 (cleanup and `--check`, restore gate, restore status, release-hold and clear-limit texts, `require_registration_tables`); `restore-credential-diff` if RD18 (a); the backup scrub; static tests of the restore script (unchanged); `docs/DEPLOYMENT.md` (§34 and a new section, including the 6.6 runbook). **No code that inserts a recovery row or a recovery ledger row**: the tests create rows by SQL fixtures. Tests RA, RN, RR (fixture rows), RQ, RH3, RZ1. Linux evidence listed for activation, not produced. | This addendum reviewed | RD3 (the states and columns), RD13 (the allow-list, in the same scope as the scrub and the checks it governs), RD18 (only for the command, under (a)) |
| **8.2 Service, no caller** | `accounts.recovery_candidate`; the public helpers of `registration` (`control_blocked`, `raise_mark`, `writer_now`); `web/services/recovery.py` (request, resend, redeem, cancel, `pending_view`, dispatch, settle); no Flask, no route, no caller. The 8.1 operations re-tested on rows the service creates (RR1–RR3 end to end). Tests RB (service parts), RC, RE, RK, RV, RX, RF, RI, RS, RL, RT, RCl, RH1–RH2, RPoll (service level), RZ2. | 8.1 merged | RD1 (only if changed), RD2, RD4, RD5, RD6, RD7, RD8, RD10, RD11, RD12, RD17 |
| **8.3 Routes and gate** | `recovery` blueprint, templates, the `rec` slot, `/login` link and notice, the build gate and its production and development wiring, the Nginx locations of 7.5, `AUTH_ROUTE_CONTRACT.md` and `ACCOUNTS_DESIGN.md` §12 updated; enumeration, polling, route, session, header and production tests. RZ2 is replaced by RW3 and RP1 (the routes exist and answer 404 while the gate is off). | 8.2 merged | RD14, RD16; RD15 blocks activation only |

Why this order rather than one scope with storage, service and operations
together (which would satisfy the rule too): the rule then holds by
construction, not by a test, even if a later scope had an accidental caller;
and each review stays the size registration's PR 3 and PR 5 were. The cost
is that 8.1's operations tests use fixture rows, so 8.2 repeats the key
restore and scrub tests on service-made rows.

**No-caller tests.** RZ1 (from 8.1): no production module contains a
statement that inserts into `password_recoveries` or writes
`registration_sends.recovery_id`, other than the migration. RZ2 (from 8.2):
the service's writers (`request`, `resend`, `redeem`, `cancel`, `dispatch`,
`settle_send`) are reached from no production entry point: importing
`web.app`, `web.wsgi`, `web.production`, every blueprint and every
`src` command module does not import `web.services.recovery`; no URL rule
names a recovery endpoint; no `manage_accounts` or other CLI subcommand calls
it; no `before_request`/startup hook, Gunicorn hook (`deploy/gunicorn.conf.py`),
systemd unit `ExecStart` or deploy script refers to it. Both are static and
import-level checks, stated as such: they pin the absence of callers, which
is a property of this repository, and are not the reason the build is safe
(7.3).

RD9 would be its own later scope if approved. Nothing in this plan activates
anything.

## 11. What was checked for this addendum

Read at `513b36c`: `AGENTS.md`; `docs/reviews/EMAIL_REGISTRATION_DESIGN.md`
(whole), `EMAIL_REGISTRATION_SCOPE.md`, `EMAIL_REGISTRATION_TEST_PLAN.md`;
the recovery passages of `docs/ACCOUNTS_DESIGN.md`, `AUTH_ROUTE_CONTRACT.md`,
`REFACTOR_ACCOUNTS_ROADMAP.md`, `DEPLOYMENT.md` §34; code:
`web/services/accounts.py`, `registration.py`, `registration_scrub.py`,
`login_throttle.py`, the TOTP rotations in `totp.py`,
`oidc_accounts.session_key_digest`, `web/account_session.py`,
`web/blueprints/registration.py`, `web/blueprints/account.py` (`/login`,
`/account/password`), `web/registration_settings.py`, the registration parts
of `web/app.py` and `web/production.py`, `src/manage_accounts.py`
(account and registration commands), `src/backup_sqlite.py`, the
registration calls of `deploy/learnpilot-restore.sh`,
`deploy/nginx/learnpilot.conf` (zones), migrations 021 and 022,
`src/canonical_tables.py`.

Executed, scratch only (in-memory SQLite 3.51.0, standard-library Python,
outside the repository): `ALTER TABLE ... ADD COLUMN` with the cross-column
CHECK of 3.1 (accepted; existing row valid; both-ids and zero id refused;
old-style insert accepted); `AUTOINCREMENT` reusing the id of a rolled-back
insert (reused). An attempt to time scrypt failed (no `hashlib.scrypt` in that
interpreter); nothing is claimed about its duration. No application test,
database, service, mail server, provider or deployment was used.

**Revision 2** (from `745a952`). Read in addition: registration design 0.2,
2.5, 3.2–3.9, 4.3, 5.2 and 6.2 again for the quotations of 0.1; the
`/register` block and the comments of `deploy/nginx/learnpilot.conf` and
`NginxTemplateTest` in `tests/test_registration_production.py` (7.5);
`registration._usable_row`, `create_pending`, `resend`, `verify`, `cancel`
and `cleanup` for the order of 3.5; every `INSERT`, `UPDATE` and `DELETE` of
`registration_sends` in `web`, `src` and `tests` (3.1); `accounts.create_user`
and the hash upgrade in `authenticate` (6.6); migration 016's TOTP tables;
the `/login` throttle answers in `web/blueprints/account.py` (4.4); the
replaced-database handling, `manage_lifecycle reconcile` call and final start
of `deploy/learnpilot-restore.sh` (6.6); `docs/DEPLOYMENT.md` §17 and §22.

Executed for revision 2, scratch only and **a model, not an implementation
test** (SQLite 3.51.0 in memory, Python with `-I`, outside the repository):
`ALTER TABLE ... ADD COLUMN` with the exactly-one-purpose CHECK of 3.1 on a
table holding a row with `pending_id` NULL (refused) and on one holding only
rows with it set (accepted); with the at-most-one CHECK the old-style insert
with `pending_id` NULL was accepted and a row naming both ids refused. Nothing
was timed. The application suite was not run (documentation only).
