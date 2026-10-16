# Password recovery by emailed code — design addendum (registration PR 8)

Scope and status: `docs/reviews/PASSWORD_RECOVERY_SCOPE.md`. Tests:
`docs/reviews/PASSWORD_RECOVERY_TEST_PLAN.md`. Parent design:
`docs/reviews/EMAIL_REGISTRATION_DESIGN.md` (cited below as "registration
design", with its section numbers), whose section 5.2 requires this addendum
and whose decision D10 approved self-service recovery by emailed code as PR 8.

**Proposal. Not reviewed, not approved, not authorized.** Section 0.1 restates
the requirements Gabriel approved before this addendum was written. Everything
else is a proposal: section 0.3 lists the open decisions with a
recommendation each, and nothing in this document is labelled selected or
approved unless section 0.1 says so. No migration number is reserved. Nothing
here authorizes an implementation, a deployment or an activation; every
production gate stays off.

Existing behaviour is referenced, not restated. "Unchanged" means the
registration design, `docs/ACCOUNTS_DESIGN.md`, `docs/AUTH_ROUTE_CONTRACT.md`
and the code at `513b36c` (branch `docs/password-recovery-design`, whose code
equals `3d4306a`, PR #341 merged).

## 0. Summary

### 0.1 Requirements approved by Gabriel (input to this addendum)

These come from D7, D10 and registration design 5.2, and from the brief of this
addendum. They are restated so that the proposals can be checked against
them; this document does not change them.

| # | Approved requirement |
| --- | --- |
| A1 | Recovery uses emailed codes and is a flow of its own, separate from registration verification. |
| A2 | Ordinary sign-in stays email and password; no emailed code is sent or asked at sign-in. Admin TOTP is unchanged. |
| A3 | A request's response does not reveal whether an account exists; the limits of that claim (timing, delivery) are stated honestly. |
| A4 | Recovery sends only to `users.email`, never to an OIDC `user_identities.email_at_link`. (Wording of the brief. RD2 (a) would need Gabriel to read it as governing the code, the credential, and to allow a credential-free notice to the address the person typed; RD2 (b) keeps it as written.) |
| A5 | Redemption decides eligibility again, under the writer lock: the account exists, is active, has role `student` or `teacher`, has a password, and still has the `users.email` the code was issued to. |
| A6 | An admin never recovers by email, whatever the account was when the code was issued. |
| A7 | A change of the account's `session_key` invalidates its recovery credentials. |
| A8 | A successful reset uses the existing password-setting contract, rotates `session_key`, invalidates every other pending recovery of the account and issues no session. |
| A9 | Operator recovery through the CLI (`manage_accounts set-password`) and admin TOTP handling are preserved. |
| A10 | Recovery sends share the existing global mail budget and the conservative sender outcomes of registration design 4.2–4.3. |
| A11 | Recovery is integrated with cleanup, the clock policy and holds, the restore gate and status, and the isolated snapshot scrub. |
| A12 | Every production gate stays off. |
| A13 | An address that was never verified is **not** approved for recovery: until Gabriel decides otherwise, it gets no code. |
| A14 | Out of scope unless Gabriel says otherwise: email change, adding a password to a Google-only account, identity linking or merging, admin recovery, TOTP reset (D15). |

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
   `accounts.set_password`, clears the address's sign-in throttle (if RD8 is
   approved), voids every other pending recovery of the account and closes the
   row. No session is issued; the person is sent to `/login`.
5. The send runs through the existing sender process with its 8-second
   deadline. A recovery send's outcome is recorded in the ledger only; it never
   changes a recovery row and never changes what the browser sees (RD7).
6. The restore gate, restore status, snapshot scrub and cleanup timer reach the
   new table through the CLI commands and the backup module they already use;
   the restore script is not edited.
7. A separate build gate `recovery.PRODUCTION_ACTIVATION`, `False`.

### 0.3 Open decisions (proposals with recommendations; none approved)

"Blocks" says what a decision holds up: an implementation scope of section 10,
or only activation.

| # | Question | Recommendation | Main alternative | Blocks |
| --- | --- | --- | --- | --- |
| RD1 | Are never-verified addresses (`email_verified_at` NULL) eligible? | **No.** They get the shadow treatment of RD2. Legacy accounts keep operator recovery (1.3). | Eligible; or eligible after an operator attestation command (not designed). | Implementation only if (b) is chosen; the approved default (A13) is what is built. Activation: Gabriel's confirmation. |
| RD2 | What does an address that may not recover receive? | **(a) One neutral notice** to the typed address, issued, counted, sent and settled exactly like a code. This needs Gabriel to confirm that A4 governs **codes** (the credential) and does not forbid a credential-free notice to the address the person typed. | **(b) Nothing**, with identical accounting (the ledger row is written and never sent). Keeps the literal wording of A4; makes eligibility readable from response time (1.4). | Implementation (the shadow dispatch path) |
| RD3 | One step (code and new password together) or a separate reset credential? | **One step**, one transaction (2.4). | Two steps with a short-lived reset token (rejected in 2.4). | Implementation |
| RD4 | Limits | **D6's values unchanged** (30 min recovery, 10 min code, 5 attempts per code, 4 sends 60 s apart, 5 per address per hour, 8 per 24 h 15 min, the shared 300 per hour), **except the code length: 8 digits** (5.4). | 6 digits as in registration: 4×10⁻⁵ per day per targeted account, about 1.5 % over a year of daily attack (5.4). | Implementation |
| RD5 | Per-address budget shared with registration or separate? | **Shared**: the same ledger rows under the same keyed address hash (5.2). | Separate budgets: doubles the unwanted mail an address can receive, gains nothing (5.2). | Implementation |
| RD6 | Circuit history shared or separate? | **Shared**: one relay, one history (5.3). | A recovery circuit of its own: doubles the failing attempts admitted. | Implementation |
| RD7 | What does a `not_sent` recovery send change? | **The ledger only.** No visible message, no waived wait, the code kept (4.5). | Registration's behaviour (message, retired code, waived wait): an eligibility oracle under RD2 (b), and under RD2 (a) a second code path for no gain. | Implementation |
| RD8 | Clear the address's sign-in throttle on a successful reset? | **Yes**, in the same transaction, as `/account/password` does today (4.4). | Leave it: a person who locked themselves out waits up to 15 minutes after the reset. | Implementation (small) |
| RD9 | Send a "your password was changed" notice after a reset? | **Not in PR 8** (8.2). | Yes: a second send path after the commit with its own accounting. | Implementation only if yes |
| RD10 | Count wrong recovery codes in `login_throttle`? | **No** (5.6). This corrects registration design 3.4, which anticipated it. | Yes: lets anyone lock a victim out of sign-in by typing wrong recovery codes. | Implementation |
| RD11 | Does a resend decide eligibility again? | **Yes**: every issuance decides it and binds afresh; a row may change between real and shadow (2.3). | Freeze the decision at the first issuance. | Implementation |
| RD12 | Cancellation while a restore hold is set | **Allowed**, mirroring the approved registration behaviour (registration design 0.7). | Refuse during a hold. | Implementation |
| RD13 | Snapshot and restore schema checks | **An allow-list** of the table's columns; an absent table is accepted only if its migration is not recorded (6.4). | Registration's rule (required columns only). | Implementation |
| RD14 | Routes, Nginx zones, login link | `/recover`, `/recover/verify`, `/recover/resend`, `/recover/cancel`; `lp_register` on request and resend, `lp_login` on verify; „Забравена парола?“ on `/login` while enabled (2.6). | Other paths or a zone of its own. | Implementation; zone tuning is activation evidence |
| RD15 | Bulgarian texts (2.7, 2.8) | Proposed wording. | — | Activation only |
| RD16 | The `/login?recovery=done` notice is URL-selected, as registration's activation notice is (approved there, PR 4). | Accept the same display limitation. | A one-shot session flag. | Implementation (small) |

Not decided here and not decided by any of these: D16 (privacy, minors,
retention), which must now also cover recovery data and, under RD2 (a),
notices to addresses without an account; the configuration of D17.

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
| Enumeration through timing | Under RD2 (a) both kinds of row send one message, so the difference is that of a code and a notice through the relay, as in registration. Under RD2 (b) the difference is a whole send: a reliable oracle. Stated, not hidden. | 1.4 |
| Guessing a code | Keyed hash, 5 attempts per code, at most 8 codes per address per 24 h 15 min shared with registration, browser binding needed. 8 digits recommended (RD4). | 5.4 |
| Takeover through the mailbox | Inherent to email recovery: whoever reads the mailbox can reset. Verified-only eligibility (RD1) narrows it to addresses that once proved control; nothing re-verifies an old address (registration design 5.3). | 8.2 |
| Phishing the code | The binding means the code works only in the requester's browser; an attacker who requests and then persuades the owner to hand over the code succeeds. The message says never to share it. Same residual as registration. | 2.8, 8.2 |
| Stolen cookie | The slot alone redeems nothing without the code; redemption is single-use under the lock. | 2.2 |
| A code that outlives a change of the account | Eligibility and the session-key digest are rechecked at redemption under the lock (A5, A7). | 2.4 |
| Admin takeover | Admins are never eligible at issuance and are rechecked at redemption: a code issued before a promotion redeems nothing. | 1.3 |
| Budget exhaustion | Shared, non-refundable hourly budget and per-address caps; exhausting them closes registration and recovery for up to an hour, or one address for about a day, and nothing else. | 5.5 |
| Concurrent operator change | Serialized by SQLite's single writer; the recheck at redemption sees whichever committed first. | 3.6 |
| Resurrection by restore | The restore gate voids pending recovery rows; spent attempts and cancelled codes do not return. | 6.2 |
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

**Teacher accounts with leftover TOTP state.** Sign-in asks a second factor of
`role == 'admin'` only (`web/blueprints/account.py`, `login`). A demoted admin
that still has a TOTP row signs in as a teacher by password, so recovery of
such an account weakens nothing that sign-in enforces. Recovery never reads or
writes TOTP tables (A9).

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
- no scrypt in either branch (neither has a password before redemption).

**Under RD2 (a)** both kinds of row send one message and settle one outcome,
so the application's own steps are the same: one lookup, one row, one
sender process, one exchange. What differs is outside it: the relay may take
longer, accept or refuse differently for a code and for a notice (registration
design 3.7, unchanged). Measured durations through the chosen provider are
activation evidence (9).

**Under RD2 (b)** an eligible address costs a sender process and an SMTP
exchange (registration's measurements: about 0.11 s of interpreter start on the
development machine, plus the relay's exchange, up to the 8-second deadline);
every other address costs milliseconds. That is not a subtle difference: anyone
timing one request learns whether the address has a recoverable account. A3
allows the limitation to be stated rather than closed, and this is the
statement. Padding the shadow path is not proposed: with one synchronous
worker a delay of seconds per request is a capacity cost, and no padding can
match a relay's distribution.

**Not established in either case:** equal duration, equal delivery and the
circuit's independence of content (registration design 3.7). The owner of the
mailbox learns that someone asked; that informs the owner, not the requester.

The only responses that depend on the account by design come after a correct
code (`REDEEMED`, `INVALIDATED`), which only the mailbox holder can send.
Sign-in stays uniform and unchanged.

## 2. Lifecycle and UX

### 2.1 States

```
POST /recover ──► pending ──► redeemed   (password set, session_key rotated)
                     │
                     ├──► void      (replaced, cancelled, invalidated at redemption,
                     │              voided by another redemption, restore gate)
                     └──► expired   (30 minutes after creation)
```

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

**Resend** (`POST /recover/resend`): hold and clock first; the row must be
`pending` and unexpired (else it is expired and scrubbed, or "no usable
recovery"); sender, circuit and total budget; at most 4 sends; 60 seconds
after the previous issuance; the address caps; then **eligibility is decided
again** (RD11) and the issuance replaces the code, its attempts and its
binding: an eligible account gets a code bound to its current id and
session-key digest (`is_shadow = 0`), anything else the shadow treatment
(`is_shadow = 1`, `user_id` and digest NULL). A resend therefore behaves as a
fresh request on the same row, and a password change between request and
resend does not strand an eligible person.

Repeated requests from one browser each void the previous row. Requests from
several browsers make several rows, each with its own code; all are bounded by
the shared address caps (5.2) and all are voided by a successful redemption.

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
the lock, in this order:

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
| 12 | If RD8: `login_throttle.clear` of the address's throttle key | — |
| 13 | Void and scrub every other `pending` row with this `user_id` or this `email` | — |
| 14 | Set this row `redeemed`, scrub | `REDEEMED` (with `user_id`) |

Steps 1–9 are identical for a real and a shadow row, so nothing before a
correct code depends on the account. Steps 10–14 are reachable only with a
correct code, which only the mailbox holder has.

`set_password` hashes with scrypt under the writer lock. That is deliberate:
it happens once per successful reset and never for a wrong code, so the lock
is held for one scrypt computation a handful of times a day, and no
pre-computed hash has to be trusted. The duration was not measured here (the
development interpreter has no `hashlib.scrypt`); it belongs to the capacity
evidence of 9.

**Failures.** Any exception rolls the whole transaction back: the password,
the key, the attempt count, the code and the other rows are as before. The
answer is the generic failure with status 500 and the slot stays, so the same
code can be tried again. A failed commit is the same case. The success page is
reached only after the commit has returned. `set_password` raising
`ValueError` (a password the pre-check accepted but the account's current
address refuses) cannot happen once step 10 has established that the
addresses are equal; if it does, it is this failure, never a reported success.

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
| exception, failed commit | „Нещо се обърка и паролата не беше сменена. Опитай отново.“, status 500 | kept |

`INVALIDATED` tells the mailbox holder that the account changed. That is not
enumeration: the correct code already proves control of the mailbox, as
registration's `EMAIL_TAKEN` does.

### 2.5 Cancellation, expiry, restart

- **Cancel** (`POST /recover/cancel`, „Грешен имейл? Започни отново“): void and
  scrub this browser's pending row, drop the slot, `303` to
  `/recover?next=<next>`. A final row is left as it is. A clock behind the
  mark refuses (nothing voided). A restore hold does **not** refuse (RD12): the
  hold and the mark are left exactly as they were.
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
  and `Referrer-Policy: no-referrer`.
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
| Failure | „Нещо се обърка и паролата не беше сменена. Опитай отново.“ |
| Success on `/login` | „Паролата е сменена. Влез с новата парола.“ |

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
CREATE INDEX idx_password_recoveries_user ON password_recoveries(user_id) WHERE state = 'pending';
CREATE INDEX idx_password_recoveries_email ON password_recoveries(email) WHERE state = 'pending';

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
| Two browsers, two rows of one account, both correct | The first redeems, rotates the key and voids the other row; the second → `NOT_PENDING (void)`. If the voiding were missing, the digest would still refuse it (step 10). |
| Redemption and CLI `set-password` | CLI first: key rotated → `INVALIDATED`. Redemption first: the CLI then sets its own password; the operator's later action stands. |
| Redemption and `set-role admin`, `disable`, `sign-out`, `totp-reset`, `/account/password` | Whichever commits first. Afterwards the recheck refuses (role or active state, and the rotated key), or the operator's change follows the reset. |
| Redemption and a resend of the same row | Serialized; after a resend the old code is gone and the new one counts its own attempts. |
| Two requests for one address at its cap minus one | Issuance time is read under the lock; the second sees the first and is refused. |
| Five wrong codes at once | Five attempts counted; a sixth is refused. |
| Registration activation creating the account while a shadow recovery is pending | The recovery row stays a shadow; a resend decides again and may now issue a code (RD11). |

**Failed commits** anywhere: rolled back; the request is answered with the
generic failure and nothing it decided is acted on (no slot written, nothing
dispatched, no success page).

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
- **Throttle (RD8).** Recommended: clear the address's `login_throttle` row
  in the redemption transaction, with the key `/login` uses
  (`login_throttle.identifier_hash(SECRET_KEY, users.email)`, which is
  `throttle_identifier` of a valid address). Only the mailbox holder can
  reach that step, and `/account/password` already clears it after a change.
  Without it, a person who locked themselves out waits up to 15 minutes after
  the reset.
- **Account-wide invalidation.** Step 13 voids every other `pending` row of the
  account (by `user_id`) and of the address (by `email`, which also closes
  shadows of that address). Rows of other accounts are not touched.
- **Other key changes** (operator commands, `/account/password`, TOTP) do not
  void recovery rows explicitly; the digest refuses their codes at
  redemption, and the rows expire and are scrubbed on schedule. No existing
  writer is changed for recovery.
- **A plain sign-in does not end a recovery in progress** (it changes no key).
  Proposed as is; sign-in stays unchanged (A2).
- **Pending registrations.** For an address with an account they are shadows
  and are not touched. A real pending registration of the address (begun
  before the account existed) ends as `EMAIL_TAKEN` at its own verification.
  Recovery reads and writes no registration row.
- **Google identities, profiles, assignments, attempts and their markers**:
  neither read nor written.

### 4.5 `not_sent` in recovery (RD7)

Registration retires a `not_sent` code, waives the wait and says so. Recovery
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

### 5.4 Guessing bound (quantitative; RD4)

Under the three assumptions of registration design 3.3 (clock not stepped,
key unchanged, ledger intact: no restore, released hold or limit clearing),
**at most 8 codes of one address, of either purpose, can be guessed in any
24 hours**, because each code lives at most 10 minutes and at most 8
issuances of the address lie in any `W`. The code lifetime must stay 10
minutes for `W` to remain valid. For an address with an eligible account,
registration issues only notices (shadows), so those codes are recovery
codes: at most **40 guesses per targeted account per day**.

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
recovery codes. The input rule stays registration's: trim at the ends, then
exactly 8 ASCII digits; inner whitespace is wrong. The other D6 values are
reused unchanged.

**Exceptions, stated as in registration design 3.3 and 3.8:** clock steps
(two steps of 300 s already admit 13 codes in a real day); secret-key rotation
(a fresh budget, up to 16 codes around it); a released restore hold (up to 8
more codes in a day that saw 8); `clear-registration-limit` (the same);
repeated forward steps of the clock (no bound). Each now applies to recovery
codes too.

### 5.5 Unavailable before expensive work

| Request | Checked before any write or process | Checked again under the lock |
| --- | --- | --- |
| Request | hold, clock, unreaped sender, circuit, total budget, address caps | the same, before the inline cleanup and any row |
| Resend | — | hold and clock first, then the row, then sender, circuit, budget, row limits, address caps |
| Redeem | slot, row state and password rules (reads) | hold and clock; scrypt only after a correct code and a passed recheck |

No scrypt is computed for a request or a resend (neither has a password,
for either kind of row), and none for a wrong code. The only expensive work an
unauthenticated client can cause is a send, which every cap above precedes.

### 5.6 Recovery attempts and the sign-in throttle (RD10)

Registration design 3.4 said `login_throttle` would be reused for "later,
recovery attempts". This addendum recommends **not** doing so: the throttle
is keyed by the address, so anyone typing wrong recovery codes from their own
browser would lock the victim out of sign-in for 15 minutes at a time. The
guessing bound already rests on the per-code and per-address counters, and
Nginx's `lp_login` zone limits the rate per client. Recovery neither reads nor
increments the throttle; it only clears it on success (RD8).

### 5.7 Clock and hold

The shared `registration_control` row governs recovery too:

- every recovery writer but `settle_send` checks the hold (not `cancel` and
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
| A pending row's address, account link, session-key digest, code hash | 30 minutes | Scrubbed by the first cleanup after expiry: about 45 minutes after the request. At once on redemption, cancellation, replacement or invalidation. |
| A final row (state, binding hash, `is_shadow`, counters, times) | — | Deleted 24 hours after creation. |
| A ledger row | the cap windows | Deleted 48 hours after the attempt, by registration's cleanup, unchanged. |

`recovery.cleanup(conn, now)` expires and scrubs pending rows past their time
(or dated beyond the skew in the future, once the clock is not behind the
mark) and deletes final rows older than 24 hours. It needs no secret and
ignores gates and holds.

**Integration point:** `manage_accounts registrations-cleanup` runs, in its
existing single `BEGIN IMMEDIATE` and after its existing restore-lock checks,
`registration.cleanup` and then, only if that did not report `clock_behind`,
`recovery.cleanup` with the same clock. `--check` fails when a pending
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
  8 further codes, registration or recovery").
- The restore lock and lifetime-lock protocol, the timer notes, the cleanup
  coordination and the fingerprint steps of `deploy/learnpilot-restore.sh` are
  not touched. A static test pins that the script still calls the two commands
  at the same positions (before the final fingerprint, and after the
  replacement).

Effect on a restored database: a code that was cancelled, spent or replaced
after the snapshot does not return; a reset made after the snapshot is undone
with everything else (the old password **and the old session key** return,
so sessions the reset had ended become valid again; 8.2).

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

Callers: the backup scrub (raises → `BackupError`, nothing published), the
restore gate and status commands (non-zero exit, the script stops before
anything is replaced), the cleanup command. Registration's own check
(required columns only) is not changed.

**Older snapshots.** A snapshot from before the migration has no table; the
restore script migrates the copy before the gate, so the gate finds the empty
table. A snapshot made while a build without the recovery integration ran on
a migrated database could hold unscrubbed pending recoveries; such a build is
prohibited (7.3), and the gate voids them when that snapshot is restored.

### 6.5 Operator CLI

No new command is required. Changed help and output:

| Command | Change |
| --- | --- |
| `registrations-cleanup [--check]` | Also recovery (6.1). |
| `registrations-restore-gate` | Also voids pending recoveries (6.2). |
| `registrations-restore-status` | Also counts them (6.2). |
| `registrations-release-hold` | Consequence names recovery (6.2). |
| `registrations-clock-accept` | Unchanged; the mark is shared. |
| `clear-registration-limit --email` | Its note says it also releases the address's recovery budget, and that a recovery code of the address stops working. |
| `set-password`, `set-role`, `disable`, `enable`, `sign-out`, `clear-throttle`, `totp-*`, `create`, `list`, `cutover-check` | Unchanged (A9). |

`require_registration_tables` gains the recovery table and column once the
migration is recorded, with the same "apply migrations first" error.

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
| A build that ships the migration but not the recovery integration (cleanup, gate, status, snapshot scrub) leaves pending recovery rows unscrubbed in the live database and in snapshots, and unvoided by a restore | A partial or hand-made build | **Prohibited**: every build that contains the migration contains the integration, like PR 7's admission rule. |
| Development checkouts of older branches on a migrated development database | Development only | No rows exist unless recovery was used there; nothing is published. |
| Pending recoveries when a gate is turned off | Production after activation | Unreachable; scrubbed on schedule; nothing redeems. |
| Registration `settle_send` from before the companion change settling a recovery row | Only through a mis-wired call | Ledger-only effect; the change of 3.6 closes it. |

While the recovery gate stays off in production, no recovery row and no
recovery ledger row exists there, and every rollback is trivial.

### 7.4 Deployment order (for a later, separately authorized rollout)

1. Migration and code with every gate off (PR 8 merged). Nothing changes in
   production.
2. Confirm the cleanup timer, the restore gate and status and the snapshot
   step cover recovery (the Linux evidence of 9).
3. Mail settings and the evidence of registration design 0.3 (shared).
4. Gabriel's decisions RD1–RD16, D16 amended for recovery, the wording.
5. Recovery gate on, no later than the student registration gate.

Each step's rollback is the previous state: set the constant back; the tables
stay.

## 8. Logging and residual risks

### 8.1 Logging

Logger `learnpilot.recovery`. Lines hold an event name, the recovery row id,
the send generation where there is one, and, for failures, an exception class
or an outcome: `requested`, `send_started`, `send_settled outcome=<o>`,
`resent`, `wrong_code`, `redeemed recovery=<id> user=<id>`, `invalidated`,
`cancelled`, `expired`, `gate_off`, `unavailable reason=<r>`,
`dispatch_refused`, `settlement_refused`, `settlement_failed error=<class>`,
`redeem_failed error=<class>`, `cleanup scrubbed=<n> deleted=<n>`. The
registration logger keeps its events for the shared states (`restore_hold`,
`clock_regressed`, `circuit_open`, `total_cap_reached`, `sender_unreaped`).

**Never logged, at any level:** the address, the code, the binding token, the
password, any hash, the session key or its digest, the issuance identity, the
message, and whether a row is a shadow. `user=` appears on `redeemed` only,
which follows a correct code. No recovery value is ever in a query string, so
the access log holds none.

### 8.2 Residual risks, stated and not solved

- **Mailbox takeover is account takeover**, for every eligible account. A
  recycled or shared school mailbox hands the account to its new reader;
  nothing re-verifies an old address (registration design 5.3).
- **Phishing the code** works if the owner hands it over.
- **Timing.** Under RD2 (a) as registration: a relay may treat a code and a
  notice differently in time or outcome. Under RD2 (b) a whole send separates
  an eligible address from every other: anyone can tell, by timing a request,
  whether an address has a recoverable account.
- **Circuit.** A relay that fails for one kind of message only can be
  observed through the shared circuit (registration design 3.7); under RD2 (b)
  any failing relay makes real rows close the circuit and shadows not.
- **Guessing**: the bound of 5.4 holds only under its assumptions; at 6 digits
  a year-long daily attack on one account succeeds with about 1.5 %.
- **Denial of recovery**: an address's daily budget can be used up by anyone,
  closing its registration and recovery for about a day; the total budget for
  everyone for up to an hour.
- **Restore undoes a reset**, including the session key that the reset
  replaced: sessions the owner meant to end return with the snapshot. Any
  restore has this property today; recovery makes it more likely to matter.
  The restore runbook should advise `sign-out` for accounts reset since the
  snapshot (an operator note, not a mechanism).
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
| Timing | Measured durations of a recovery code and a recovery notice through the chosen provider (RD2 (a)), or the recorded acceptance of the timing oracle (RD2 (b)). |
| Lock time | The measured duration of `set_password` under the writer lock on the server. |
| Nginx | The new locations in the `lp_register` and `lp_login` zones; a class-sized run that mixes registration and recovery from one address. |
| Decisions | RD1–RD16; D16 amended for recovery (and for notices to addresses without an account under RD2 (a)); the wording. |

## 10. Implementation plan

**One scope at a time, in this order**, each green on the full suite, every
gate off, documents updated with what each makes untrue. Executor for each:
**Claude Opus, High**. Independent security review of each in a **separate
Opus, High session**, at the exact SHA, before the next scope starts.

| Scope | Content | Needs first | Blocks by decisions |
| --- | --- | --- | --- |
| **8.1 Storage and service** | Migration (next free number); classification; `accounts.recovery_candidate`; the public helpers of `registration` and the `settle_send` guard; `web/services/recovery.py` (request, resend, redeem, cancel, `pending_view`, dispatch, settle, cleanup) and `recovery_scrub.py`; no Flask, no route, no caller. Tests RA–RX of the test plan. | This addendum reviewed | RD1, RD2, RD3, RD4, RD5, RD6, RD7, RD8, RD10, RD11, RD12 |
| **8.2 Operations** | The CLI changes of 6.5; the backup scrub of 6.3; schema checks of 6.4; static tests of the restore script (unchanged); `docs/DEPLOYMENT.md` (sections 34 and a new section). Linux evidence listed for activation, not produced. | 8.1 merged | RD13 |
| **8.3 Routes and gate** | `recovery` blueprint, templates, the `rec` slot, `/login` link and notice, the build gate and its production and development wiring, Nginx locations, `AUTH_ROUTE_CONTRACT.md` and `ACCOUNTS_DESIGN.md` §12 updated; enumeration, route, session and production tests. | 8.2 merged | RD14, RD16; RD15 blocks activation only |

Operations precede the routes so that, in every environment, no recovery row
can be created before cleanup, restore and snapshot handle the table. RD9
would be its own later scope if approved. Nothing in this plan activates
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
