# Verified email/password registration — design

Scope and status: `docs/reviews/EMAIL_REGISTRATION_SCOPE.md`. Tests:
`docs/reviews/EMAIL_REGISTRATION_TEST_PLAN.md`.

**Design with recorded decisions; implementation and activation not
authorized.** Section 0.2 records the decisions Gabriel approved (D1–D15 and
D18–D20, and the behaviour of D17); D16 and the configuration of D17 stay
activation prerequisites. Values not covered there remain proposals.
Existing behaviour is
referenced, not restated: `docs/AUTH_ROUTE_CONTRACT.md` (contract),
`docs/ACCOUNTS_DESIGN.md` (accounts), `docs/reviews/GOOGLE_OIDC_DESIGN.md`
(Google). Where this document says "unchanged" it means those documents and
the code at fe72d02.

**Revision 4.** Revision 1 received an independent REQUEST CHANGES review
(nine findings); revision 2 answered it (0.4) and was reviewed again, with
six further findings on sender containment, the guessing bound's clock
assumptions, restore and physical retention, answered in revision 3 (0.5).
A third review found two remaining defects in the sender contract (F1, F2)
and twelve notes; this revision answers them (0.6).

**Review status.** An independent review of commit
`2b29519e6483b707950023fd199ca947d861381c` returned **APPROVE WITH
NON-BLOCKING NOTES**: both blockers (F1, F2) resolved, N1–N12 accepted, no
technical blocker remaining. It approved none of D1–D20. Six wording and
test-plan corrections (R1–R6, 0.7) were made **after** that review and are
**not independently reviewed**; no further broad review is required for
them. Recording Gabriel's decisions (0.2) followed, also without repeat review.
Nothing here authorizes an implementation, a deployment or an activation.

## 0. Summary and decisions

### 0.1 The design in brief

1. `POST /register/student` or `/register/teacher` validates the form, hashes
   the exact password (scrypt, as today) and stores a **pending
   registration**: a row of a new table, not a `users` row. It carries no
   privilege and reserves nothing.
2. A 6-digit code goes to the address. The row keeps only a keyed hash of it.
   Every send attempt is a row of a small **send ledger**; the per-address
   caps and the total work budget count those rows by the time each was issued
   and are never given back.
3. The browser that submitted the form holds a random token in its signed
   session; the row holds the token's keyed hash. `/register/verify` works
   only with that token **and** the code.
4. A correct code creates the account in one `BEGIN IMMEDIATE` transaction:
   `users` row (role from the route, `email_verified_at` set), profile,
   subjects. Only a real `users.email` collision is answered as "address
   taken"; any other failure rolls everything back and fails generically.
5. That request does not sign the person in: the confirmation page links to
   `/login` (D7). Verification happens once, at registration.
6. An address that already has an account produces the same pages, a row that
   no code can activate, and a notice email to the owner (D8).
7. The send runs in a short-lived sender process that the request ends at an
   8-second deadline; an outcome that is not certain is treated as unknown.
8. Sign-in, the throttle, admin TOTP, the existing CLI commands, Google
   sign-in, assignment and attempt ownership are not modified.

### 0.2 Decisions approved by Gabriel

Recorded as approved; this document does not change them. "Built by" names
the PR of section 9 that implements the selected behaviour. Alternatives that
were not selected are named only as history and are neither designed nor
tested. Approval of a decision is not authorization to implement: each PR
still starts only when Gabriel says so.

| # | Selected behaviour | Built by |
| --- | --- | --- |
| D1 | "Class" means the grade only; no section letter. | PR 1 |
| D2 | Grades 8–12, stored with the date set, editable on „Профил“; no automatic advancement. | PR 1 |
| D3 | A teacher selects 1–3 of the application's subjects, plus „Друг предмет“ with a short text. | PR 1 |
| D4 | School: free text, 3–120 characters, with a town hint („Име и населено място“). | PR 1 |
| D5 | A self-registered teacher receives role `teacher` after verified activation, **only after quotas exist**; the teacher gate stays off until then. **Quota values are not yet selected.** | PR 7 |
| D6 | Six-digit code valid 10 minutes, five guesses per generation; three resends at least 60 seconds apart; pending registration lifetime 30 minutes; 5 sends per address per hour, 8 per 24 h 15 min, and the reviewed global cap of 300 per hour. Accounting is non-refundable; the browser binding stays; the lockout and security trade-offs of 3.3 and 3.6 stand as documented. | PR 3 |
| D7 | Verification does not sign the user in. After the **one-time** registration verification the person is redirected to `/login`. Later sign-in is email and password only; no code is sent or asked at login. Admin TOTP is unchanged. Password recovery is a separate flow, not a repeated registration verification. | PR 4 |
| D8 | An address that already has an account gets the generic registration response and a notice email; nothing is overwritten or linked. | PR 3 |
| D9 | Per-IP limiting in Nginx; no IP address is stored in SQLite. | PR 4 |
| D10 | Self-service password recovery in a separate PR, before public opening. | PR 8 |
| D11 | Profile completion is required of new Google students while the profile gate is on; existing accounts are unaffected. | PR 6 |
| D12 | The ten-character password minimum stays. | PR 1 |
| D13 | The public form accepts an ASCII address with a dotted domain. | PR 1 |
| D14 | Three reviewed build gates (student registration, teacher registration, profiles), initially off. | PR 4 |
| D15 | Email change, a password for a Google-only account and account merging are deferred (5.3). | — |
| D17 | Sender behaviour: the application sends verification codes **automatically** from a configured no-reply address. The address, domain, credentials and provider or relay are configured later with the VPS; this design names none and does not assume VPS-hosted SMTP. Production `console` and `file` delivery is prohibited. | PR 2 |
| D18 | Cleanup timer every 10 minutes; snapshots: option (b), a logical scrub of the isolated backup copy. The residual-byte and old-backup limits of 6.5 stand. Not selected: leaving pending rows in snapshots, and an offline rebuild. | PR 5 |
| D19 | Option (a): the reviewed sender child process with an 8-second communication deadline, with the worker-replacement overlap documented in 4.2. Not selected: a host-wide lock, `TasksMax=`, a local relay. | PR 2 |
| D20 | Option (a): every restore voids pending credentials and holds registration (and recovery) until the operator releases it, acknowledging the lost history. Not selected: a ledger carry-over. | PR 5 |

**Not decided, and not approved here.** D16 (privacy page, lawful basis,
minors, purpose and retention of the profile fields) is an activation
prerequisite; no legal conclusion is recorded. The configuration and
delivery validation of D17 are activation prerequisites. The quota values of
D5 and the sub-decision of 5.2 (recovery for never-verified addresses) are
open.

**Email and sign-in semantics, exactly.**

1. A code is sent by the application itself, automatically, when a
   registration is submitted and when a resend is requested; an address that
   already has an account gets the notice of 4.5 instead (D8). The sender is
   the configured no-reply address (`DZI_MAIL_FROM`); there is no default,
   and without it registration is disabled.
2. A registration is verified **once**. A correct code creates the account
   with `email_verified_at` set and sends the person to `/login`. No session
   is issued by registration.
3. Sign-in is `/login` with email and password, as today. It never sends,
   asks for or checks a verification code, at the first or any later sign-in.
   The admin second factor (TOTP or recovery code) is unchanged.
4. Recovery (D10, PR 8) is its own flow with its own codes and limits. It is
   not registration verification repeated and it is not part of sign-in.

### 0.3 Activation prerequisites (evidence, not design decisions)

None is done. These do not block the implementation PRs, which ship with
every gate off.

| Prerequisite | Evidence required |
| --- | --- |
| Bounded mail behaviour | On the server, under the real unit: the deadline tests of 4.2; a controlled send to an address Gabriel owns; a forced hang (a host that accepts the connection and never answers) ending at the deadline with no sender process left; a worker killed during a send, with the orphan gone within its lifetime and the replacement worker serving; the sender holding no descriptor of the worker. |
| Single-worker capacity | Measured duration of one send and of thirty submissions at once through Nginx, against the criterion of 3.5. If it is not met, activation waits and a remedy needs its own decision; a local relay was not selected (D19). |
| Transport security | Verified TLS to the chosen relay; `console` and `file` backends refused by a production start. |
| Provider and sender | Sender address and domain, credentials and provider or relay chosen and configured with the VPS (D17's behaviour is decided; no address or host is assumed); SPF, DKIM and DMARC; delivery checked to abv.bg, Gmail and a school mailbox. An accepted SMTP exchange is not treated as delivery to a mailbox. |
| Nginx | The `lp_register` zone tuned on a classroom-sized run; trusted client-IP handling confirmed (the zone keys on the same client value as `lp_login`); Gunicorn reachable only from Nginx. |
| Shared networks and the total cap | An explicit assessment of a class behind one NAT address, and of deliberate exhaustion of the total budget (3.6). |
| Timing of the two messages | Measured durations of a code and of a notice through the chosen provider (3.7). |
| Cleanup and restore | The timer installed and observed on a quiet server; the full restore script with its gate, including an interrupted run and a run while a cleanup is executing, against a staging copy on Linux; the snapshot step of D18 followed by a test restore (6.5, 3.9). |
| Privacy | D16: privacy page, lawful basis, minors, retention, the provider named as a recipient. |
| Teacher form only | Quotas implemented with values Gabriel approves (not yet selected), PR 7. Not needed for the student form. |
| Recovery | PR 8 (D10) delivered before public opening. |
| Google | Its own prerequisites, unchanged and separate. |

### 0.4 What revision 2 changed

Sections 0.4–0.6 are history. Where they mention an option that D1–D20 did not select, section 0.2 governs.

| Review finding | Answer | Section |
| --- | --- | --- |
| 1 SMTP deadline | A deadline enforced by ending a sender process (its containment claims were corrected in revision 3, 0.5) | 4.2 |
| 2 Send accounting | A send ledger counted by real issuance time; daily window of 24 h 15 min; bound recalculated | 3.3, 3.4 |
| 3 Failure settlement | Nothing is refunded; settlement is conditional on one send generation and idempotent | 3.5, 4.3 |
| 4 Restore and clocks | A restore gate that voids pending credentials; a clock policy without a fixed bound on repeated steps (the ledger carry-over was reworked in revision 3) | 3.8, 3.9 |
| 5 Passwords | Never stripped, collapsed or normalised; dedicated email and redirect rules | 1.1 |
| 6 Gates and profiles | Three gates with coverage through activation; new attempt versus resume; no retroactive enforcement | 7.3, 7.4 |
| 7 Retention | Validity apart from retention; a timer; scrub of every sensitive field; snapshots (physical claims withdrawn in revision 3) | 6.5 |
| 8 Enumeration | Equal-outcome claim withdrawn; uniform visible behaviour defined; residuals listed | 3.7 |
| 9 Activation errors | Only an email collision is "address taken"; everything else rolls back | 2.4, 6.3 |
| Consistency items | D7, D10, recovery eligibility, CLI transaction wording, final states, logout and markers, admin Google linking, signed-in redirects, sequential PRs | 0.2, 5.2, 2.4, 6.3, 2.3, 2.6, 6.2, 9 |

### 0.5 What revision 3 changed

| Second-review finding | Answer | Section |
| --- | --- | --- |
| 1 Sender lifetime and concurrency | Four separate time quantities; no unconditional request duration; one sender per worker, bounded overlap across a worker replacement; cleanup on every parent exit path | 4.2 |
| 2 Progress marker | Written and flushed before the recipient may be transmitted; `not_sent` only on positive evidence; everything else `unknown` | 4.1, 4.2, 4.3, 3.7 |
| 3 Entry point | A standalone script run by absolute path with the deployment interpreter; a fixed, bounded protocol; explicit environment and descriptors | 4.2 |
| 4 Bound and clock | The 40-guess claim limited to a steady clock, an unchanged key and an intact ledger; issuance time sampled under the write lock; exceptions listed | 3.3, 3.4, 3.8 |
| 5 Restore | Holds are never cleared by the gate; both D20 options specified, the simpler one recommended; no automatic release; ordering, cleanup job and final validation in the script | 3.9, 6.6 |
| 6 Physical retention | A logical-retention target with conditions; remnants acknowledged; snapshot options including an offline rebuild | 6.5 |
| Smaller items | Circuit ordering and its assumptions; decision dependencies of D5, D11 and D20 moved to the PRs they affect | 3.5, 6.1, 6.3, 9 |

### 0.6 What revision 4 changed

| Third-review item | Answer | Section |
| --- | --- | --- |
| F1 Deadline did not bound the pipes | `communicate` replaced by a selector exchange: input written and output read without blocking, under one monotonic deadline, output capped at 16 bytes, no read to end-of-file after a kill | 4.2 |
| F2 Alarm depended on an inherited disposition | The script resets `SIGALRM` to its default action and unblocks it before arming | 4.2 |
| N1 Verify order against the clock policy | Hold and clock are decided before any row is changed | 6.3, 3.8 |
| N2 `refused` in the circuit | Left out of the circuit's history: it neither closes nor resets | 3.5, 4.3 |
| N3 `SIZE` in the sender command | The sender command carries no options | 4.2 |
| N4 `site` at start-up | The script is started with `-I -S` | 4.2 |
| N5 Unreaped sender spent the budget | Checked in `availability` before anything is admitted | 3.5, 4.2 |
| N6 Service-stop clean-up; `TasksMax` | The unit's existing kill mode is credited for service stops only; `TasksMax=` is an option under D19 | 4.2, 0.2 |
| N7 Restore script details | Cleanup coordination after the lock and before the live fingerprint; timer restored on the failure paths that restore the backup timer; D20 (a) needs no replaced database | 3.9 |
| N8 D20 wording; `pending_id` | No option clears a hold that existed before the gate; merged rows carry no `pending_id` | 0.2, 3.9 |
| N9 Remnants; D18 (c) validation | Unused space inside pages named; per-table logical digests with table-specific identity; transform before the backup's checks, fsync, link and hash | 6.5 |
| N10 Profile check placement | Decided inside the writer-locked start decision as a "would create" outcome | 7.4 |
| N11 Blockers of two kinds | Each item labelled | 8.1 |
| N12 Circuit arithmetic | Labelled an upper bound | 3.5 |

### 0.7 Corrections and decision recording after the approving review (not independently reviewed)

| Item | Correction | Section |
| --- | --- | --- |
| R1 | The hold and clock check comes before any expiry, scrub, voiding or other registration mutation in submission, resend and the cleanup a submission runs, not only in `verify` | 3.8, 6.3, 6.4, 6.5 |
| R2 | Test C8 says "inside the selector loop", not "inside communicate" | test plan |
| R3 | `accepted` and `refused` need end-of-file, exactly one `R` and the matching exit status; an input `BrokenPipeError` is `unknown` | 4.2, test plan C6, C8 |
| R4 | The marker-removal assertion of D18 (c) was restored; moot once (c) was not selected, kept as a historical note | 6.5 |
| R5 | The tables by kind were named; the analysis is kept as a historical note for the unselected rebuild | 6.5 |
| R6 | D15 is a scope deferral and is not an implementation blocker | 8.1, scope |
| Decisions | Gabriel's approvals recorded; unselected mechanisms (D7 (B), D8 (b), D10 (b), D11 (a)/(c), D18 (a)/(c), D19 (a+)/(b), D20 (b)) removed from the implementation scope and the tests | 0.2 and throughout |
| PR 2 review | Recorded after the independent review of PR 2 (#336, REQUEST CHANGES, narrow). **Reply policy approved by Gabriel (option 3):** a 421 is `unknown` at every stage up to the relay's final `250` for the message (a later `QUIT` failure does not revoke that acceptance, and `accepted` is never a statement about a mailbox); any other 4xx reply to the data command or to the message is `unknown`; any other 4xx reply to the recipient command is `refused`; a 5xx reply after the marker is `refused`. **`SIGCHLD` guard:** no sender is started while the process ignores `SIGCHLD`. The log event `sender_error` is named. The one-sender statement is tied to the pinned synchronous, single-thread worker. The production-start wiring of the mail settings stays with PR 4 | 3.10, 4.2, 7.5 |
| PR 3 implementation | Recorded with draft PR 3 (#337); **not independently reviewed and not approved**. The service differs from the signatures of 6.3 in ways listed in `EMAIL_REGISTRATION_PR3_STATUS.md` ("Deviations"): the writers take a `clock` callable read under the write lock instead of `now` (3.4, test DL11), and take `secret_key`; `create_pending` and `resend` also take `sender_busy`, `total_cap` and `teacher_enabled`; a `dispatch` function enforces the order of 4.3 and that what it sends is the committed issuance, but not that a generation is sent once (approved: caller sequencing, row below); `create_verified_user` reports a collision on `users.email` structurally (a conflict-targeted insert) and no exception is classified; `cancel` is stopped by a clock behind the mark (3.8). `_USER_COLUMNS` is not widened by PR 3, which writes `email_verified_at` and has no reader of it | 3.4, 3.8, 4.3, 6.1, 6.3 |
| PR 3 contract points approved by Gabriel | After the first review of #337, recorded here and implemented in the draft. A later independent review of the revision implementing them returned **APPROVE WITH NON-BLOCKING NOTES** (all three earlier blockers resolved; no activation or deployment approval): **(1)** one send per generation is caller sequencing: `dispatch` invokes the mailer once per call, not per generation, PR 4 prevents a duplicate `dispatch` in its handler paths, and there is no automatic retry, durable send-claim state or exactly-once claim about SMTP; **(2)** a stored random issuance identifier, `registration_sends.issuance_id` (migration 022; 32 lower-case hexadecimal characters, new for every issuance and resend), so a result of a rolled-back transaction matches no later generation even when SQLite reuses numeric ids and the code digits coincide; **(3)** verification-code input is trimmed of leading and trailing whitespace only, inner whitespace is rejected and the rest must be six ASCII digits; **(4)** settlement requires the same issuance identity, callers never settle after `NotSendable`, and a stale result cannot settle an unrelated generation that reused its number. Ledger rows from before migration 022 have no identity and are neither sent nor settled | 3.1, 4.3, 6.1, 6.3, 6.4 |
| Cancellation during a hold (approved by Gabriel with PR 4) | **A registration hold does not block the cancellation of pending data.** `cancel` voids and scrubs this browser's pending row while a `restore` hold is set; the hold and the high-water mark are left exactly as they were, so a cancellation never reopens registration. A clock behind the mark still refuses a cancellation under the existing service contract (nothing is voided, 3.8). This confirms the reading PR 3 implemented (its deviation 7) | 3.8, 6.3, 6.4 |
| PR 4 implementation | Recorded with draft PR 4 (#338). **Independently reviewed: APPROVE WITH NON-BLOCKING NOTES at exact SHA `6d370aca7f3d8e914dd5dcfa25bbb2f85ba30f42`, with production registration OFF.** Deployment and activation are **not** approved. Changes after that SHA (the not-sent message precedence, the extended accessibility test, the G5 wording qualification and this record) are subsequent cleanup and were not part of the reviewed content. The student blueprint, templates and `reg` slot, the two build gates (`registration.PRODUCTION_ACTIVATION`, `registration.TEACHER_ACTIVATION`, both off), production and development wiring of the mail settings, the `learnpilot.registration` handler, the Nginx zone and the operator commands `registrations-cleanup [--check]`, `registrations-clock-accept` and `clear-registration-limit`. Its deviations and open points are in `EMAIL_REGISTRATION_PR4_STATUS.md`: among them, the profile gate is left to PR 6 (approved by Gabriel; D14 lists three gates, section 9 puts the profile gate with PR 6), and the fixed URL-selected `/login` activation notice is kept (approved; a hand-built URL can show it without a flow, an accepted display limitation), and `_USER_COLUMNS` is still not widened because no account reader needs `email_verified_at` (`list` reads it with its own query and tolerates its absence) | 6.2, 6.4, 6.6, 7.3, 7.5 |

## 1. Forms and profiles

### 1.1 Fields

Identifiers are the form field names. **Names, school and „Друг предмет“**
are stripped and have inner whitespace collapsed to single spaces before they
are measured. That rule applies to those fields and to no other:

- `password` and `confirm_password` are used **exactly as received**: not
  stripped, not collapsed, not Unicode-normalised, not truncated. The exact
  string is measured, compared with the confirmation and hashed, which is
  what `/login` later compares (it passes the submitted value to
  `authenticate` untouched). Leading, trailing and repeated spaces are
  significant.
- `email` goes to `accounts.normalize_email` (NFKC, trim, lower-case, as
  sign-in does) and then to the rule of 1.3. Inner whitespace is an error,
  never collapsed.
- `next` is not altered. It is kept only if `safe_redirect_target` accepts it
  as it is **and** it holds no character of Unicode category C (control, format
  such as a right-to-left override, surrogate, private use, unassigned) or Z
  (separators, spaces of any kind): a registration-specific rule added after the
  PR 1 review; the shared function and sign-in are unchanged.
- The code field has its own rule (3.1).

| Field | Form | Bulgarian label | Rule |
| --- | --- | --- | --- |
| `first_name` | both | Име | 1–39 characters; letters of any script, combining marks, space, `-`, `'`, `’`, `.`; at least one letter |
| `last_name` | both | Фамилия | as `first_name` |
| `email` | both | Имейл | section 1.3; at most 254 characters |
| `grade` | student | Клас | one of 8, 9, 10, 11, 12, shown as „8. клас“ … „12. клас“ (D1, D2) |
| `school_name` | teacher | Училище | 3–120 characters, no control or format characters, at least one letter; help text „Име и населено място, напр. СУ „Васил Левски“, Пловдив“ (D4) |
| `subject` | teacher | Предмети, по които преподаваш | 1–3 values, each a subject id of `src.subject_registry` or `other` (D3) |
| `subject_other` | teacher | Друг предмет | required with `other`: 2–60 characters, letters, spaces, `-`. **Ignored when `other` is not selected** (approved after the PR 1 review): nothing is stored and no error is raised |
| `password` | both | Парола | `accounts.password_problem(password, email)`, unchanged, on the exact value: 10–256 characters, not the email |
| `confirm_password` | both | Повтори паролата | the same string as `password`, compared exactly |
| `next` | both | hidden | kept only if `safe_redirect_target` accepts it and it holds no C or Z character, at most 512 characters |

Buttons: „Регистрирай се като ученик“, „Регистрирай се като учител“. The
chooser page `/register` offers „Ученик съм“ and, while the teacher gate is
on, „Учител съм“. The sign-in page gains one link, „Нямаш профил?
Регистрирай се“, only while registration is enabled.

No field named `role`, `account_type` or `is_admin` is read. The account type
is the route.

The 39-character bound makes `first_name + " " + last_name` at most 79
characters, inside the 2–80 rule of `users.display_name`. Names are not
restricted to Cyrillic: foreign students and teachers exist.

A failed validation re-renders the form with status 200, the field errors and
every submitted value except the two passwords, each cut to its maximum
length before it is echoed. `next` is the exception to the exception: only an
accepted `next` is echoed, and a rejected one never is.

### 1.2 Errors and messages

| Case | Text |
| --- | --- |
| Name empty | „Въведи име.“ / „Въведи фамилия.“ |
| Name characters | „Името може да съдържа само букви, интервал, тире и апостроф.“ |
| Name too long | „Името трябва да е най-много 39 символа.“ |
| Email | „Невалиден имейл.“ (existing) |
| Grade | „Избери клас.“ |
| School | „Въведи име на училището (от 3 до 120 символа).“ |
| Subjects | „Избери поне един предмет.“ / „Избери най-много 3 предмета.“ / „Въведи името на предмета.“ / „Избери предмети от списъка.“ (approved after the PR 1 review: an id outside the list, a repeated id, or a value that is not text) |
| Password | the existing texts of `password_problem` |
| Confirmation | „Паролите не съвпадат.“ |
| Verify page | „Изпратихме 6-цифрен код на {email}. Въведи го, за да потвърдиш имейла си.“ |
| Wrong, expired or missing code | „Грешен или изтекъл код.“ |
| Attempts used up | „Твърде много грешни опити. Поискай нов код.“ |
| Resent | „Изпратихме нов код.“ |
| Resend too early or used up | „Можеш да поискаш нов код след малко.“ / „Не можеш да поискаш повече кодове. Започни регистрацията отново.“ |
| Per-address cap | „Твърде много имейли към този адрес. Опитай отново по-късно.“ |
| Mail server not reached (4.3, outcome `not_sent`) | „Не успяхме да изпратим имейла. Опитай отново след малко.“ |
| No usable registration in this browser | „Регистрацията е изтекла или е започната от друг браузър. Започни отново.“ |
| Activated (D7) | „Имейлът е потвърден. Влез с имейл и парола.“ |
| Address taken at activation | „Вече има профил с този имейл. Влез с имейл и парола.“ |
| Registration, mail or the teacher form unavailable; total budget used; hold (3.5, 3.9, 7.3) | „Регистрацията временно не е достъпна. Опитай по-късно.“ |
| Any other failure (storage, commit, programming error) | „Нещо се обърка и нищо не беше записано. Опитай отново.“ |
| Profile needed before a new attempt (7.4) | „Попълни профила си, за да започнеш теста.“ |

The email shown on the verify page is read from the row of the browser that
typed it. It is never in a URL or in the cookie.

### 1.3 Email normalisation and uniqueness

`users.email` stays the one login identifier, unique on the form
`accounts.normalize_email` returns (NFKC, trimmed, lower-case; dots and plus
tags are kept, so `a+1@x.bg` and `a@x.bg` are two accounts). Registration
normalises with that function and compares with nothing else, so an address
registered in any letter case collides with the CLI-created account of the
same address.

The public form applies one more rule on top (D13): ASCII only; local part of
`a-z 0-9 . _ % + -`, 1–64 characters, no leading, trailing or doubled dot;
domain of at least two labels of `a-z 0-9 -`, a last label of at least two
letters. It narrows what the form accepts and changes nothing for existing
rows: `normalize_email`, `authenticate` and the CLI are untouched, so every
existing account signs in exactly as before.

**Known limitation (PR 1, informational).** The rule is the one above and no
stricter: a domain label may begin or end with a hyphen (`a@-x.bg`,
`a@x-.bg`) and has no length bound of its own beyond the 254 characters of the
whole address. Such an address passes the form, and delivery to it may fail.
No particular send outcome (4.3) is assigned to it: which one results depends
on where the exchange with the mail server stops (corrected with PR 2; the
earlier text named `not_sent`). Validation is not broadened here; tightening
it is a separate, later decision.

### 1.4 Names and the `display_name` snapshot

`users.display_name` keeps its meaning: what is shown, and what an attempt's
`student_name` is copied from (`docs/ACCOUNTS_DESIGN.md` §9, §24). First and
last name are stored separately in the profile (6.1). At activation
`display_name` is `"<first_name> <last_name>"`. Existing accounts keep their
`display_name` and have no profile row. Existing `student_name` snapshots are
never rewritten.

### 1.5 Where profile data appears

On the owner's `/account` and `/account/profile` pages only. Teacher results,
CSV and vault notes keep showing `student_name`. Class, school and subjects
are therefore collected and shown to nobody else in this milestone. Their
purpose, who may see them later and how long they are kept are not settled by
this design: they are part of D16, and the forms are not enabled before the
privacy notice states them.

## 2. Account and credential lifecycle

### 2.1 States

```
form submitted ──► pending ──► activated   (users row exists)
                      │
                      ├──► void      (replaced, cancelled, address taken, gate off, restore)
                      └──► expired   (30 minutes after creation)
```

- Every state other than `pending` is **final and is never rewritten**. A
  later request that finds a final row changes nothing and is answered as
  "no usable registration" (6.3).
- A row that leaves `pending` is scrubbed in the same statement: email,
  names, grade, school, subjects, password hash, code hash and code expiry
  become NULL. What remains is the state, the keyed binding hash, counters
  and times.
- **Validity** (how long a registration can be completed) is 30 minutes.
  **Retention** (how long the database still returns its data) is a separate,
  logical target, set by the cleanup of 6.5. Neither is a statement about
  bytes on disk.

### 2.2 Privileges before verification

None. A pending registration is not a `users` row, so `authenticate` cannot
find it, `load_current_user` cannot load it, Google's collision rule does not
see it and the CLI does not list it. The session key `reg` is, like `mfa_*`,
not an account key: no principal is built from it and every protected route
answers as for an anonymous visitor.

### 2.3 Browser binding

On a successful submission the server generates `token =
secrets.token_urlsafe(32)`, stores `binding_hash = HMAC-SHA256(SECRET_KEY,
"registration-binding\0" + token)` in the row and puts `reg = {"v": 1, "t":
token, "next": <validated path>}` in the signed session. No personal data is
in the cookie.

- Verify, resend and cancel find the row by the hash of the session's token
  and by nothing else. There is no row id or email in any URL or form.
- **Another browser.** A browser without the token has no registration: the
  verify page redirects to `/register`, and a posted code is answered with
  „Регистрацията е изтекла или е започната от друг браузър…“ without any row
  being read by code or by address. Reading the email on a phone and typing
  the code into the browser that registered works, which is why a code is
  used and no link (a link would open in the other browser).
- One registration per browser: a new submission voids the row of the token
  the session held.
- A copy of the cookie is the same browser as far as the server can tell. It
  gains nothing: the code is still needed, and activation is single-use
  because the row's state changes under the write lock. Unlike the OIDC
  transaction, the authoritative state is the server's row, so no consumption
  record is needed.
- The session functions of `web/account_session.py` keep their contract.
  `issue_account_session` and `issue_pending_second_factor` drop `reg` with
  everything else and carry over only the `quiz_attempt_owner:*` markers.
  `end_session` (sign-out) clears the whole session, the markers included.
  A browser session that ends loses the binding; the person registers again,
  which nothing blocks.

### 2.4 Duplicate and concurrent registrations

Pending rows are not unique by address. Therefore:

- **No reservation.** Submitting someone's address creates a row that expires
  in 30 minutes and blocks no one. Only activation takes the address, and
  activation needs the mailbox.
- **Attacker-chosen password.** An attacker who submits a victim's address
  with their own password holds the binding but not the code. The victim
  holds a code but not the binding: it works in no browser of theirs. The
  account cannot come into being unless the victim hands the code to the
  attacker. The email says not to (4.5). That residual risk is the one every
  emailed code has.
- **Victim registers at the same time.** They get their own row, password and
  code. Whichever row is verified first creates the account; the other
  answers „Вече има профил с този имейл…“ at its own correct code and is
  voided. Showing that text is not enumeration: the code proves the reader
  controls the mailbox.
- **Same person, two tabs or devices.** Same as above; the first wins.
- **Race.** Activation runs inside `BEGIN IMMEDIATE` and re-reads
  `users.email` there. The CLI's `create` does not issue `BEGIN IMMEDIATE`:
  it runs a plain `INSERT` in the implicit transaction of its connection and
  commits in `main`. The two are still ordered, because SQLite admits one
  writer at a time and the `UNIQUE` constraint on `users.email` is checked
  by whichever `INSERT` comes second: the CLI then reports its normal
  "already exists" error, or the activation finds the address taken.
- **What counts as "address taken".** Only this: under the write lock, a
  `users` row with the normalised address exists. That is established by
  reading `users`, before the insert and again if the insert raises
  `IntegrityError`. An `IntegrityError` with no such row (a profile CHECK, a
  subject foreign key, a `display_name` rule) is a defect of storage or code:
  the whole activation is rolled back, nothing is accepted, and the answer is
  the generic failure of 1.2, never "address taken" (6.3).

### 2.5 An address that already has an account

Whatever the account is (student, teacher, admin, active or disabled), the
submission is handled as a **shadow** registration:

- the same redirect, verify page, cookie, counters and messages;
- a row with `is_shadow = 1` and no password hash. Its `code_hash` is the
  keyed hash of 32 random bytes that are sent nowhere, with the usual expiry,
  so every step of verification (expiry, attempt counting, the limit, a
  resend) behaves as for a real row and no 6-digit value can match.
  Activation refuses a shadow row in any case;
- one email to the address, the notice of 4.5 instead of a code (D8), entered
  in the same ledger and counted against the same caps; a resend sends the
  notice again;
- the password is hashed and the hash discarded, so the application runs the
  same steps (3.7 says what that does and does not establish).

Nothing is linked, merged, changed or signed in. The existing password is not
touched, and the submitted one is never stored.

### 2.6 Google and password accounts

Rules that stay exactly as they are: an identity is found by `(issuer,
subject)` only; an email never selects, signs in or links an account; linking
happens only from a signed-in account with its password
(`oidc_accounts.confirm_link`); **an admin account can neither sign in
through Google nor link a Google identity** (`LINKABLE_ROLES` is `student`
and `teacher`), whichever way the admin's account was first created.

| Situation | Behaviour |
| --- | --- |
| Registration with the address of a password account | Shadow (2.5). |
| Registration with an address that only appears as `user_identities.email_at_link` of a Google-created account (`users.email` NULL) | Not a collision: the registration proceeds and a second, separate account results. `email_at_link` is unverified metadata; letting it block a registration would let anyone reserve an address through a Google profile. |
| New Google identity whose reported address is the email of a registered account | Unchanged: `email_exists`, nothing created, the person is told to sign in with the password and link from „Профил“. |
| New Google identity while a registration for that address is pending | Unchanged: a Google student is created (`users.email` NULL). A later activation creates a second account. |
| Registered student or teacher links Google | Unchanged flow. |
| Registered account later promoted to admin | Linking is refused from then on, as for any admin. |
| Two accounts of one person (rows 2 and 4) | Not merged by anything here. Linking the Google identity to the password account is refused (`identity_taken`). Correction is the identity-recovery milestone that Google activation already waits for. |

`email_at_link` is never a login identifier, a recovery address or a merging
key, in this design or in the recovery addendum.

### 2.7 Existing accounts

- CLI-created accounts have no profile row and `email_verified_at` NULL. They
  sign in, change passwords and link Google as today. Nothing asks a teacher
  or an admin to complete a profile, and nothing asks an existing student to
  do so (D11).
- `manage_accounts` keeps every command. `create` still creates any role;
  `set-role` stays the only way to an admin. `list` gains the verification
  date and the declared type.
- `quiz_assignments.owner_user_id`, `quiz_attempts.user_id` and
  `student_name` are not written by any part of this design.

## 3. Verification codes and abuse controls

### 3.1 Generation and storage

- `f"{secrets.randbelow(10**6):06d}"`.
- Stored as `HMAC-SHA256(SECRET_KEY, "registration-code\0" + <row id> + "\0" +
  code)`, lower-case hex. The key is not in the database, so a database or
  backup copy does not reveal a live code; a plain or salted hash of a
  6-digit value would. A stretching hash adds nothing for a million values.
- Compared with `hmac.compare_digest`. A submitted value is trimmed of
  leading and trailing whitespace only and must then be exactly six ASCII
  digits; anything else, whitespace inside the value included, is a wrong
  code (approved, 0.7).
- Rotating `DZI_SECRET_KEY` ends every pending registration, as it ends every
  session.

### 3.2 Limits (constants of the service, D6)

| Limit | Value | Counted by |
| --- | --- | --- |
| Registration validity | 30 minutes from creation, not extended | the row |
| Code lifetime | 10 minutes, never past the registration's expiry | the row |
| Attempts per code | 5; wrong, malformed and missing codes all count | the row |
| Sends per registration | 4 (the first and 3 resends), at least 60 seconds apart; each replaces the code and resets its attempts | the row |
| Send attempts per address | 5 in any 60 minutes and 8 in any 24 h 15 min, codes and notices together, whatever their outcome | the ledger (3.4) |
| Send attempts in total | 300 in any 60 minutes, whatever their outcome (configurable downwards) | the ledger (3.5) |
| Mail circuit | closed for 5 minutes after 3 consecutive settled attempts that ended `not_sent` or `unknown` | the ledger (3.5) |
| Per IP | Nginx zone `lp_register` on the submit and resend addresses, keyed like `lp_login` (POST only); proposed `rate=2r/m burst=90 nodelay`. `/register/verify` uses `lp_login`. | Nginx |

None of these counters is ever decreased, by a failure or by anything else.
Single use: a correct code clears `code_hash` before anything else is
decided. A resend clears the previous code in the statement that stores the
new one.

### 3.3 Guessing bound

A guess needs a live code and the binding of its row. Each code allows five
attempts. Every code is one ledger row of its address (3.4).

Let `W = 24 h 15 min`: 24 hours, the 10-minute code lifetime and five minutes
of margin. The daily cap admits an issuance only when fewer than 8 ledger
rows of the address lie in the `W` before it.

**The claim and its assumptions.** Take any interval of `W`. If, throughout
it,

1. the wall clock the application reads advances with real time and is **not
   stepped**, in either direction, by any amount;
2. `DZI_SECRET_KEY` is not changed;
3. no ledger row of the address is removed or made unreachable other than by
   the 48-hour cleanup: no restore, no released hold, no
   `clear-registration-limit`;

then **at most 8 codes of that address can be guessed in the 24 hours that
end the interval: at most 40 guesses in a million, 4×10⁻⁵**, while the owner
receives eight emails they did not ask for. With 8 digits the same count
gives 4×10⁻⁷.

Why: under assumption 1 a code can be guessed for at most 10 minutes after
it was issued, so a code that can be guessed at some instant of the 24 hours
ending at `T` was issued inside the `W` ending at `T`. That span holds at
most 8 issuances, because the cap was checked at the last of them against
exactly that span, with every earlier one already in the ledger (issuance
times are sampled under the write lock, 3.4).

**Outside those assumptions no number is claimed.** In particular:

- **Clock steps, including small ones.** The five minutes of margin are for
  a clock that runs slightly fast or slow, not for steps. Two steps of 300
  seconds, each inside what 3.8 tolerates, already break the count: five
  codes at 0–4 s; clock back 300 s, so those codes are still accepted at real
  899 s; clock forward again and 300 s further; three codes at real
  3601–3603 s; five more at real 87 006–87 010 s. All 13 are admitted and all
  13 can be guessed inside one real 24 hours: 65 guesses (the review's
  schedule, reproduced in 8.3; test DC7).
- **Secret-key rotation.** The address hash is keyed, so after a rotation the
  earlier ledger rows of an address are no longer found and it has a fresh
  budget. The codes issued before the rotation stop working at that moment,
  but may already have been guessed: up to 16 codes (80 guesses) in the 24
  hours around a rotation.
- **Restore, hold release, operator clearing.** 3.9 and 6.6 state each.

Notes:

- Notices and failed attempts use up the same budget, so the real number of
  codes is usually lower.
- The window must be longer than 24 hours even with a steady clock. With a
  plain 24-hour issuance window a scratch model (8.3) reached 13 guessable
  codes in 24 hours (five still alive from just before the window, eight
  inside it).
- The schedule the first review found (rows at 0 s and 3601 s with three
  resends each, then four rows from 86 401 s) admitted 11 issuances in 24
  hours under revision 1. Under this rule the four late submissions are
  refused, because eight issuances lie in the `W` before them.

### 3.4 The send ledger

`registration_sends` (6.1) has one row per send attempt: the keyed hash of
the address (`HMAC-SHA256(SECRET_KEY, "registration-email\0" + email)`), the
kind (`code` or `notice`), `attempted_at`, an outcome and the pending row it
belongs to. It holds no address, no name and no code.

- **`attempted_at` is sampled under the write lock.** The clock is read after
  `BEGIN IMMEDIATE` has returned, never taken from a value read earlier in
  the request (before hashing, or while waiting for the lock). Issuances are
  therefore stamped in the order they were admitted, and each cap check sees
  every earlier issuance. The time basis is the wall clock of 3.8; this is
  the "issuance time" every statement of 3.3 refers to.

- A row is inserted in the transaction that stores the code, **before** the
  send, with outcome `started`. Its id is the **send generation**: the
  pending row stores it as `code_send_id` next to the code it issued.
- Caps are `COUNT(*)` over rows of the address (or of all addresses) with
  `attempted_at` inside the trailing window, read and written under the write
  lock. A row whose `attempted_at` is later than `now` counts as inside every
  window.
- A ledger row is never updated except to settle its own outcome (4.3), and
  is deleted only by the cleanup, 48 hours after `attempted_at` (6.5).

`login_throttle` is reused where its policy fits (sign-in and, later,
recovery attempts). It is not used for these caps: its window and its cleanup
are fixed at fifteen minutes for every row.

### 3.5 Work budget and the mail circuit

Two different things are kept apart:

- **Work** is what the server spends: an attempt to send. It is counted when
  the attempt is admitted and is **not refundable**. The per-address caps,
  the total cap and the per-registration send count are all work counters.
- **Delivery status** is what happened to one attempt (4.3). It can retire
  the code that attempt issued and waive the 60-second wait. It moves no
  counter.

So a server that refuses every message does not reopen the budget: 300
attempts in an hour, then none (the scratch model of section 8 admitted
exactly 300 of 400 failing attempts; revision 1 admitted all of them).

The circuit keeps a failing mail server from occupying the worker for the
whole budget. It is read from the ledger, with no state of its own. Take
the ledger rows settled `accepted`, `not_sent` or `unknown`, in the order of
their **generation id**, not of their settlement time, so a late settlement
of an old generation cannot reorder them. A row settled `refused` is left
out of this history altogether: it neither closes the circuit nor interrupts
a run of failures. When the three with the highest ids all ended `not_sent`
or `unknown` (4.3) and the newest of them was attempted less than 5 minutes
ago, submit and resend
answer „Регистрацията временно не е достъпна…“ before any hashing, row or
send. Because `refused` is left out, nobody closes registration for everyone
by submitting addresses a relay rejects, and nobody reopens it that way
either.

The same early answer is given while this worker still has a sender it could
not reap (4.2). That is checked with the hold, the clock, the circuit and
the total budget, **before** a row or a ledger entry is written, so no
address is charged an attempt for a message that will not be tried.

What this bounds, by arithmetic and not by measurement, **assuming
attempts are admitted one after another and each is settled before the next
is admitted**, which is what one synchronous worker does:

- one attempt occupies the worker for the communication deadline of 4.2
  (8 s) plus process creation and reaping, which that section does not bound;
- a mail server that never answers: **as an upper bound**, 3 attempts per 5
  minutes, at most 36 in an hour, about 5 minutes of worker time per hour.
  The real figure is lower: once the circuit has closed, a single further
  failure closes it again, which is about 14 attempts an hour.

Where the assumption fails the circuit is weaker, and the non-refundable
hourly cap is the bound that remains:

- with several handlers, as many attempts as there are handlers can be
  admitted before the first failure is settled;
- an attempt whose settlement never ran stays `started` and is not counted
  by the circuit, so a worker that dies during every send never closes it.

Not covered by the circuit at all:

- a mail server that answers slowly but successfully: 300 attempts of 8 s
  would be 40 minutes of an hour. That is
  why capacity is an activation prerequisite with a criterion: the measured
  99th-percentile duration of a send, times the configured hourly cap, must
  stay under 6 minutes, and thirty simultaneous submissions must all be
  answered inside Nginx's 35-second upstream timeout. With one sync worker a
  burst is served one after another, so thirty submissions fit only if one
  takes about a second or less, hashing included. If measurements say
  otherwise activation waits; a remedy needs its own decision (a local relay was not selected, D19).

### 3.6 Shared school networks

A class registers from one address at once, so the application has no per-IP
counter and the Nginx zone is sized for a class (the same reasoning as
`lp_login`). `burst=90 nodelay` means Nginx **lets 90 POSTs through at
once** and then two a minute; it does not spread them out. Nothing in Nginx
smooths the mail work of a class: the single worker serves the submissions
one after another, and the bound on that is 3.5.

To assess before activation, not settled here:

- a second class behind the same NAT address inside the same hour meets the
  zone's refill rate of two a minute; the zone's numbers are a proposal;
- the total budget can be exhausted deliberately from several addresses.
  That stops registration (and recovery, which shares the budget) for up to
  an hour and nothing else; it is logged as `total_cap_reached`;
- someone can use up an address's eight emails and keep its owner from
  registering with it for about a day. The existing sign-in lockout
  (`docs/ACCOUNTS_DESIGN.md` §14) is fifteen minutes; a day is a different
  size of harm and is **not** accepted by analogy. It is the trade-off in D6:
  the daily cap is what the guessing bound of 3.3 rests on. The operator can
  clear one address (6.6), at the price stated there.

### 3.7 Enumeration

What the design establishes, for an address with and without an account:

- **The same visible answers.** Status, redirect, cookie shape, page,
  counters, cap messages and the wording after a resend are the same. They
  depend on the submitted values, on the row's own counters and on the
  ledger, never on whether a `users` row exists.
- **The same accounting.** A code and a notice are both one ledger row,
  counted alike whatever became of them. Nothing is given back, so the
  delivery outcome cannot be read from a cap.
- **The same application steps.** One scrypt computation, one row, one send
  attempt.
- **Failure feedback that cannot depend on the recipient or the content.**
  The only delivery failure a person is told about is `not_sent`, and that
  outcome needs positive evidence (4.2): the sender process was never
  created, or it ended by itself, with a status that says so, before it
  wrote the marker that precedes the recipient command. At that point
  neither the address nor the message had been given to the mail server.
  Every other ending, including a sender killed at the deadline before the
  marker, is `unknown` or `refused`: recorded and logged, and changing
  nothing the browser can see: the same page, the code left as it is, the
  same wait before a resend.

What it does **not** establish, and no longer claims:

- **Equal delivery outcomes.** A relay may accept a code and refuse a notice,
  or the reverse. The application hides that from the submitter; it cannot
  make the two messages fare alike.
- **Equal duration.** The response waits for the send. A relay may take
  measurably longer for one recipient or one of the two texts, and the
  submitter can time the request. The application adds no work of its own
  that depends on the account, and does not pad or equalise. Measuring both
  messages through the chosen provider is an activation prerequisite (0.3);
  a stable, large difference is recorded as a limit with the provider
  assessment.
- **The circuit.** Three consecutive `unknown` attempts close registration
  for five minutes for everyone. Someone able to make the relay stall on
  notices but not on codes could see that. It needs a relay that misbehaves
  by content and is assessed with the provider, not excluded here.
- **Mailbox contents.** The owner of an existing account receives a notice.
  That informs the owner, not the submitter.

The only response that depends on an account's existence by design is
activation with a correct code (2.4). Sign-in is unchanged and stays uniform:
a registered but never verified address has no account, so „Грешен имейл или
парола.“ is its answer too.

### 3.8 Clocks

This flow uses the **application wall clock in UTC**, passed in by the caller
and stored in the `CURRENT_TIMESTAMP` text layout, exactly as
`login_throttle` does. The monotonic clock and service-instance check of the
OIDC storage are deliberately not copied: a monotonic reading means nothing
after a restart, a registration waits on a person reading mail, and a deploy
must not discard a class's registrations.

`registration_control` (6.1) holds a **high-water mark**: the latest `now`
any issuance or cleanup run has seen. Policy:

- **Small disagreement, up to 300 seconds either way.** Tolerated only in
  the sense that nothing is refused. It is **not** covered by the bound of
  3.3, which assumes a clock that is not stepped at all: two steps of 300
  seconds are enough for 13 guessable codes in a day (3.3, test DC7).
- **Clock behind the high-water mark by more than 300 seconds.** The clock
  went back. Submit, resend and verify answer „Регистрацията временно не е
  достъпна…“; nothing is issued, no attempt is counted and no row changes. It
  ends when the clock passes the mark again, or when the operator, having
  checked the time, lowers the mark (6.6). While the clock is behind the
  mark no registration row is expired, scrubbed, voided or deleted by a
  request, by the cleanup a submission runs, or by a timer run either: in
  every writer this check comes before any such change (6.3). The only
  control-state writes that remain are those this design already permits:
  raising the mark when `now` is later than it, never lowering it, and the
  operator's `registrations-clock-accept` (6.6). A row or a code dated more than 300 seconds in the future is
  treated as expired only once the mark no longer says the clock is behind,
  that is, after the operator has accepted the clock.
- **Clock stepped forward.** Rows and codes expire early, which is safe.
  Ledger rows leave their windows early by the size of the step, and the
  cleanup may delete them. This is **not detected**: from inside the
  application a step forward looks like time passing. A step of at least `W`
  releases one further daily budget for every address, and each further step
  releases another: **there is no bound on the number of budgets across
  repeated forward steps.** What limits it is outside this design: the server
  clock is disciplined by NTP and set by the operator, not by a visitor.
  Revision 1's "one extra round" was unsupported and is withdrawn.
- **Forward, then back.** Detected when anything recorded the later time: an
  issuance, or the cleanup timer, which runs every 10 minutes. It is then the
  "behind the mark" case above. Ledger rows the cleanup deleted in between
  are gone, and their budgets with them.
- The counters of a code (attempts) are counts, not times; no clock fault
  raises the number of guesses a code allows.
- The lifecycle clock-health record is not consulted, and this flow does not
  write it.

### 3.9 Restarts and restore

- **Restart, deploy.** Nothing is lost: the rows and the signed session
  survive. A pending registration can be completed after a restart.
- **Restore.** A snapshot holds pending rows and the ledger as they were. Put
  back as it is, it would bring a cancelled registration's code back to life,
  return the five attempts of a code that had used them (reproduced in 8.3),
  and forget every send made since the snapshot. Whole-database consistency
  does not protect those decisions. The restore has two parts: credentials,
  which are voided, and accounting, which is held (D20).

**Always: the gate.** `manage_accounts registrations-restore-gate`, run by
`deploy/learnpilot-restore.sh` on the **staged copy**, in one
`BEGIN IMMEDIATE` transaction:

1. every `pending` registration becomes `void` and is scrubbed; the recovery
   credentials of PR 8 likewise;
2. the hold `restore` is set. If the copy already carries a hold, it is kept
   as it is, with its original time. The gate is given no other database and
   needs none: every result is held.

The gate never clears a hold it finds. It is repeatable (a second run
changes nothing) and atomic (any error rolls the transaction back, the
command exits non-zero and the script stops before anything is replaced).

**Accounting (D20): hold after every restore.** The gate does only steps 1
and 2 and reads nothing but the staged copy. After
every restore, registration and recovery answer „…временно не е достъпна…“
until the operator runs `registrations-release-hold
--acknowledge-lost-history`. The command says what is being accepted: the
accounting since the snapshot is gone, so an address may receive up to 8
further codes in a day that already saw 8 (80 guesses, once), and the total
budget and the circuit start from the snapshot's state. There is **no
automatic release**: after a restore the wall clock is the one thing the
script itself makes the operator confirm, so "enough time has passed" is not
something the application may conclude by itself. Cost: every restore closes
registration until a person acts. Sign-in is not affected. A carry-over of
the ledger from the replaced database was considered and not selected; no
merge logic, extra column or replaced-database input exists in this design.

**In the restore script:**

- **The cleanup job is stopped, not only prevented.** In the script's step
  2, **after the lock is taken and before the live database is checkpointed
  and fingerprinted**, the script stops
  `learnpilot-registration-cleanup.timer` and requires the cleanup service to
  be `inactive` or `failed`, read the way it already reads the backup
  service's state; a start condition on the unit does not stop a run that has
  already begun. A cleanup that wrote later than that would still be caught,
  by the comparison of the live fingerprint in step 8, but as a failed
  restore. The cleanup command also refuses to run while the restore lock
  exists.
- **The timer comes back on every path the backup timer does.** Whether it
  was active is noted beside the lock, as for the backup timer, and it is
  started again wherever the script's `finish` handling starts the backup
  timer again, failure paths included. A restore that stops half-way must
  not leave scrubbing switched off with no failed unit to show it.
- **The gate runs before the script's final check-and-fingerprint step**
  (its step 8), after the migration of the copy. Every mutation the gate
  makes is therefore inside the bytes that step verifies and that the
  replacement step compares. Nothing of the gate runs afterwards.
- **Final validation is of the control state, not only of the absence of
  pending rows.** `registrations-restore-status --expect <state>` runs in the
  script's step 8 and again after the replacement (step 10). It fails unless:
  no pending registration and no pending recovery credential exists; the
  control row exists; and the hold is `held`.
- **Interrupted restore.** As for the rest of the script, a run that stopped
  is not finished but repeated from the beginning with a new copy. The gate
  is repeatable: the new copy is held by the gate whatever came before.

The change is made to the copy, so it cannot be undone by the snapshot, and
it is made by the one script. A database replaced by hand, outside the
script, gets none of it; that is already unsupported (`docs/DEPLOYMENT.md`
§17, §28) and stays so.

- Registrations pending at the moment of a restore are lost; those people
  start again. An account activated after the snapshot is gone, and its owner
  registers again. A password changed after the snapshot returns to the old
  one, as with any restore today.
- Snapshots themselves: 6.5.

### 3.10 Logging

Logger `learnpilot.registration`. A line holds an event name, the pending
row's id, the send generation where there is one and, for failures, an
exception class name or an outcome:
`submitted`, `send_started`, `send_settled outcome=<o>`, `resent`,
`wrong_code`, `activated user=<id>`, `email_taken`, `activation_failed`,
`gate_off`, `total_cap_reached`, `circuit_open`, `clock_regressed`,
`restore_hold`, `hold_released`, `sender_unreaped`,
`sender_error error=<word>` (a fixed word such as `sigchld` or `input`, or an
exception class name; never a value, and no traceback),
`cleanup scrubbed=<n> deleted=<n>`.

Never logged, at any level: the address, names, school, the password, the
password hash, the code, the token, the message body. `smtplib` debug output
is never enabled (it prints the message). Gunicorn's access log has no query
string and no body; no registration value is ever in a query string.

## 4. Email delivery

### 4.1 Interface (`web/services/mail.py`, no Flask, no database)

```python
@dataclass(frozen=True)
class OutboundEmail:
    to: str        # one normalised address
    subject: str
    text: str      # plain text only

class SendOutcome(Enum):
    ACCEPTED = "accepted"    # the server accepted the message for the recipient
    NOT_SENT = "not_sent"    # established: nothing was given to the server for any recipient
    REFUSED = "refused"      # a definite refusal of the recipient or the data
    UNKNOWN = "unknown"      # everything that is not established (4.2)

class Mailer(Protocol):
    def send(self, message: OutboundEmail) -> SendOutcome: ...   # never raises for delivery
```

| Backend | Use | Behaviour |
| --- | --- | --- |
| none configured | default | Registration is disabled (7.3). |
| `smtp` | production, or local against a real relay | A sender process (4.2) using the standard library `smtplib`, `ssl`, `email.message`. TLS with certificate verification (STARTTLS or implicit); plain only to a loopback host. Optional AUTH. One recipient, one attempt, no retry. |
| `console`, `file` | development only | Writes the message to the terminal or to a directory outside the checkout. Refused by a production start, because the code is in clear. |
| in-memory | tests | Records messages; can be told to return any outcome, per message kind, and to take a stated time. |

Headers are built with `email.message.EmailMessage`, which refuses line
breaks; the only request-derived header is `To`, an address that already
passed 1.3. Names are not put into the message at all.

### 4.2 The sender process and its deadline

`smtplib` applies its timeout to each socket operation. A server that sends
one reply line just inside every timeout keeps the exchange going
indefinitely, and name resolution has no timeout at all. Per-operation
timeouts are therefore **not** the bound. The send runs in a child process
that the request ends at a deadline.

**Entry point.** `web/services/mail_sender.py` is a **standalone script**:
standard library only, nothing imported from `web` or `src`, no database and
no file opened other than its three standard streams. It is started as
`[sys.executable, "-I", "-S", "<absolute path>/mail_sender.py"]`: the
interpreter of the deployment, isolated mode, no `site` import, the script by
its absolute path (resolved once from the location of `mail.py`), no shell.
`-S` costs nothing, because the script uses only the standard library, and
it keeps the environment's `.pth` files from running before the alarm is
armed. It is not started with `-m`: in
isolated mode the checkout is not on the import path, so a module name under
`web` cannot be found (checked, 8.3).

**Contract between the request (parent) and the sender (child).**

| Channel | Rule |
| --- | --- |
| Arguments | The interpreter, `-I`, `-S` and the script path. No setting, address or secret. |
| Environment | Built for the child, not inherited: the locale and, when set, `SSL_CERT_FILE` and `SSL_CERT_DIR`. `DZI_SECRET_KEY`, `DZI_TOTP_KEY`, the Google secrets and every state path are not in it. |
| Standard input | One JSON document of at most 16 KiB: the mail settings this send needs (host, port, security mode, user name, password, sender) and the message. The parent writes it without blocking, inside the deadline, and closes the pipe (see the exchange below). The child reads at most 16 KiB plus one byte; longer, truncated or malformed input ends it before any network call. |
| Standard output | At most one byte, `R`, written with a single unbuffered `os.write` **before** the child may begin to transmit the recipient command. It means "from here on the recipient may be on the wire". The parent reads at most 16 bytes in total and then stops reading; it never reads to end-of-file to tidy up. |
| Standard error | `/dev/null`. The child prints nothing, so no traceback, which could hold the address, reaches the journal. |
| Other descriptors | None: `close_fds=True`, stated explicitly. The child holds no database file, WAL, listening socket or log file of the worker. |
| Exit status | `0` accepted. `10`–`19` ended before `R`, nothing sent to any recipient (input, lookup, connect, TLS, greeting, AUTH, sender refused). `20` a definite refusal of the recipient or the data, as the reply policy below defines it. `31` the server answered 421 at any stage up to its final `250` for the message, before `R` or after it (once that `250` is received, a later `QUIT` failure or 421 does not change `0`, though the parent still reads `unknown` if its deadline expires before the child ends); it is deliberately outside `10`–`19`. Anything else has no meaning of its own (the script uses `30` for a failure after `R` that is not a refusal). The parent reads `31` and every unlisted status as `unknown`. |

The child applies a 3-second timeout to every socket operation and makes one
attempt. It issues the sender, recipient and data commands itself (`mail`,
`rcpt`, `data`), not `sendmail`, which is what lets it place the marker, and
it sends the sender command **with no options**: in particular no `SIZE`,
which `sendmail` would derive from the length of the message. Nothing the
server is given before the marker depends on the recipient or on which of
the two messages is being sent.

**Outcome, decided by the parent.** `not_sent` needs positive evidence;
everything unclear is `unknown`.

| Evidence | Outcome |
| --- | --- |
| The process could not be created (`Popen` raised) | `not_sent` |
| The parent process ignores `SIGCHLD` (checked before `Popen`); no process is created | `not_sent` |
| The child exited by itself with `10`–`19`, and its output reached end-of-file with nothing written | `not_sent` |
| The output reached end-of-file with **exactly one** `R` and nothing else, and the child exited `0` | `accepted` |
| The output reached end-of-file with **exactly one** `R` and nothing else, and the child exited `20` | `refused` |
| Anything else: killed at the deadline, **with or without `R`**; output that hit the 16-byte cap or is not a single `R`; end-of-file not seen; ended by a signal; an unlisted status; a status that contradicts the marker (`0` or `20` without `R`, `10`–`19` with it); an **`EPIPE` / `BrokenPipeError` while writing the input**; any other exception in the parent after the child was created | `unknown` |

**Reply policy (approved by Gabriel after the PR 2 review).** What the script
makes of a complete negative reply:

| Reply | Stage | Status | Outcome |
| --- | --- | --- | --- |
| 421 | any stage up to the final `250` for the message, before `R` or after it (not `QUIT` after that `250`) | `31` | `unknown` |
| any other 4xx | recipient command | `20` | `refused` |
| any other 4xx | data command, or the reply to the message | `30` | `unknown` |
| 5xx | recipient command, data command, or the reply to the message | `20` | `refused` |
| 4xx other than 421, or 5xx | before `R` (greeting, EHLO, STARTTLS, AUTH, sender) | `10`–`19` | `not_sent` |
| a reply that cannot be read, a 1xx–3xx reply where acceptance was expected, a lost connection or a timeout | after `R` | `30` | `unknown` |

421 means the server is closing the channel, which says nothing certain about
what it did with the transaction, so it is never `not_sent` and never
`refused`: before `R` it has its own status precisely so that the absence of
the marker is not read as "nothing was sent". `unknown` keeps the code and the
budgets and counts toward the circuit of 3.5; `refused` stays left out of it.
Nothing is retried in any case.

**`SIGCHLD`.** A process that ignores `SIGCHLD` has its children reaped by the
kernel, and the interpreter then reports an exit status of 0 that it never
observed, which would turn `R` with any status into `accepted`. So `send`
reads the disposition before `Popen` and, when it is "ignore", creates no
process, logs `sender_error error=sigchld` and returns `not_sent`. It never
changes the disposition. The check is made once, before the child exists:
callers must not change the `SIGCHLD` disposition of the process while a send
is running. Gunicorn's sync worker resets `SIGCHLD` to the default action.

`accepted` and `refused` are therefore never concluded from a timeout, from
capped output, from unexpected bytes or from an exit status alone: end-of-file,
exactly one `R` and the matching status must all be present. A broken input
pipe is an exception after the child was created and falls under the
cleanup rule below: the child is killed and reaped, the outcome is
`unknown`, the code and the consumed budgets are kept and nothing is
retried, even if the child would have exited with a `10`–`19` status. No new
transport mechanism is involved.

The absence of `R` is never evidence by itself. A child killed before it
wrote the marker has probably sent nothing, and is still `unknown`: the code
stays valid, the wait before a resend applies, the budget is spent and
nothing is retried. A relay that accepted the message an instant before the
child died is the same case.

**Four different quantities of time.** They are not one bound.

| Quantity | Value | Enforced by |
| --- | --- | --- |
| Communication deadline: from `Popen` returning to the decision to kill | 8 seconds on the monotonic clock | the exchange below, in the parent |
| Process creation before it | Not bounded by this design. Ordinarily milliseconds. If it fails, `not_sent`. | — |
| Kill and reap after it | `SIGKILL`, then `wait` for at most 2 seconds. Ordinarily immediate. A child still not reaped is logged as `sender_unreaped`, the outcome is `unknown`, and that worker starts no further send until the child is gone: registration answers "unavailable" before anything is admitted (3.5), and `send`, if it is reached anyway, returns `not_sent` without creating a process. | the parent |
| Orphan lifetime: a sender whose worker has died | At most 9 seconds **after the child has armed its alarm**, which its first statements do (below). Interpreter start-up before those statements is not covered. | the child |

**The exchange, under one monotonic deadline.** `communicate` is not used:
it buffers the child's output without limit and checks its timeout only
between reads. Instead, once `Popen` has returned:

1. `deadline = time.monotonic() + 8`. Both pipes are set non-blocking and
   registered with a selector.
2. Until the deadline: when the input pipe is writable, write the next piece
   of the input with `os.write`; when all of it is written, close the pipe.
   When the output pipe is readable, `os.read` at most as many bytes as are
   still missing to 16. At end-of-file, or once 16 bytes have been read,
   stop reading and unregister the pipe. Each wait on the selector is for
   the time remaining and no longer.
3. A child that never reads its input therefore does not block the parent:
   the write simply never completes and the deadline passes. A child that
   keeps writing blocks on its own full pipe, because the parent has stopped
   reading, and is ended at the deadline. The parent holds at most the 16 KiB
   of input and 16 bytes of output in memory.
4. When both pipes are finished, `wait` for the child for the time that
   remains, and no longer.
5. At the deadline, or on any exception: `SIGKILL`, the bounded `wait` of
   the table above, close both pipes. Nothing more is read from the output
   after a kill.

"Wrote nothing" is concluded only from end-of-file with zero bytes; output
that reached the 16-byte cap, or that is anything but a single `R`, makes the
outcome `unknown`. The outcome rule above is otherwise unchanged. In a
scratch run of this exchange (8.3) a flooding child, one that never read its
megabyte of input, and one that wrote `R` and then blocked were each ended
at 1.00–1.01 s of a 1-second deadline (the model read one byte past the cap
to detect overflow, 17 in all; the design stops at 16); `R` with
exit 0 and exit 12 with no output returned at once.

**The alarm, armed safely.** A signal that is ignored or blocked stays so
across `fork` and `exec`, and `subprocess` does not reset `SIGALRM`. So the
script's first statements, before any other import, are, in this order:

1. `signal.signal(signal.SIGALRM, signal.SIG_DFL)`;
2. `signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGALRM})`;
3. `signal.alarm(9)`.

The default action is in place before the signal can be delivered, and both
precede the arming, so there is no moment after the alarm is set at which it
is ignored or blocked. In a scratch run (8.3) a child started with `SIGALRM`
ignored, or blocked, by its parent outlived a plain 1-second alarm (exit 0
after 3 s) and was ended by the alarm at 1.05 s with these three statements.
What remains uncovered is unchanged: the time the interpreter takes to reach
them.

No unconditional duration is claimed for a request. Ordinarily it is the
hashing, at most 8 seconds of sending, and milliseconds around it. The
backstop for everything else is Gunicorn's 30-second worker timeout, which
ends the worker; the sender is then ended by the cleanup below or by its own
alarm.

**Cleanup on every path.** Everything from the return of `Popen` onwards is
inside one `try`/`finally`. Whatever leaves it, the deadline, a broken pipe
while writing the input, a memory error, an interrupt, or the `SystemExit`
Gunicorn raises in a worker it is aborting, the parent kills the child if it
is still running, waits for it within the bound above and closes the pipes.
The `finally` does not run only when the worker itself is killed outright;
that is what the alarm is for.

**Concurrency: what is bounded and what is not.**

- **Within one worker: one sender.** The worker is synchronous, does not
  return before it has reaped its child, and refuses to start another while
  one is unreaped. This rests on the pinned deployment (`worker_class =
  "sync"`, `threads = 1` in `deploy/gunicorn.conf.py`): `send` takes no lock,
  so calls from several threads, as a threaded development server makes, may
  overlap.
- **Across a worker replacement: no exclusion.** A worker killed outright
  leaves its sender running, and the replacement worker can start another.
  The two overlap for up to the orphan lifetime. With one worker that is
  ordinarily two senders; in general one plus the number of workers killed
  outright in the preceding seconds. This is accepted, not prevented:
  senders share nothing (each has its own connection and its own send
  generation), and an orphan's result is recorded nowhere, so its ledger row
  stays `started` and its code stays usable (4.3). A host-wide lock was not
  selected (D19).
- **On a service stop or restart: the unit already cleans up.**
  `deploy/systemd/learnpilot.service` has `KillMode=mixed`, so stopping or
  restarting the service, the restore's stop included, kills any sender left
  in its control group. That covers service stops only. It does nothing when
  Gunicorn replaces a worker inside a running service, which is the overlap
  case above. A `TasksMax=` limit was not selected (D19).

Cost: one interpreter start per send. On the development machine a process
that imports `smtplib`, `ssl` and `email` started in about 0.11 s; the
server's figure is part of the capacity measurement of 0.3.
No queue, thread pool or framework is introduced.

**Not selected (D19):** a host-wide lock, a `TasksMax=` limit and a local
queueing relay. This design neither includes nor requires them.

### 4.3 Outcomes and settlement

The order of a send is fixed: the code and its ledger row are committed, the
transaction is closed, the send runs with **no transaction open** and no
write lock held, and then a second short transaction settles the ledger row.

`settle_send(conn, send_id, issuance_id, outcome, clock)` (approved, 0.7: a
generation is named by its number **and** the random identity stored with
it, because SQLite gives the numbers of a rolled-back issuance to the next
one):

0. The ledger row of `send_id` must hold `issuance_id`. If there is no such
   row, the identity differs, or the row has none (it predates migration
   022), nothing is changed at all and the function says so
   (`NOT_THIS_ISSUANCE`); that answer says nothing about whether a message
   was sent. A caller never settles after `dispatch` refused (`NotSendable`).
1. `UPDATE registration_sends SET outcome = ?, settled_at = ? WHERE id = ?
   AND issuance_id = ? AND outcome = 'started'`. If the generation is already
   settled the function does nothing more. Settling twice, or settling with a
   different outcome later, has no effect. Settlement belongs to the ledger
   row: it does not need the registration to be pending or current.
2. Only for `not_sent`: `UPDATE pending_registrations SET code_hash = NULL,
   code_expires_at = NULL, resend_after = NULL WHERE state = 'pending' AND
   code_send_id = ?`. The code that was never sent is retired and the
   60-second wait is waived, so the person can resend at once. Because
   `not_sent` is returned only on positive evidence (4.2), a code that may
   have left the machine is never retired here.

Consequences, each a test:

| Situation | Effect of a late failure of the old send |
| --- | --- |
| A resend has issued a newer code | None. `code_send_id` is the newer generation; the newer code, its attempts and its wait stay. |
| The registration was verified | None. The row is `activated`. |
| The registration was cancelled, replaced or expired | None. The row is not `pending`. |
| The same outcome is reported twice | None the second time. |
| The settlement never runs (the worker died) | The ledger row stays `started`. It counts as an attempt like any other; the code stays usable in case the message arrived. |

No path decreases `send_count`, deletes a ledger row or changes another
generation's code. The single synchronous worker makes these interleavings
rare today; the rules and the tests do not depend on it.

| Outcome | What the person sees | Row | Counts toward the circuit |
| --- | --- | --- | --- |
| `accepted` | the verify page | unchanged | no (resets it) |
| `not_sent` | „Не успяхме да изпратим имейла…“ with the resend button | code retired, wait waived | yes |
| `refused` | the verify page, as for `accepted` | unchanged | no: left out of the circuit's history, so it neither closes nor resets it (3.5) |
| `unknown` | the verify page, as for `accepted` | unchanged: the code and the wait stay, the budget is spent | yes |

An accepted exchange means the relay took the message. It does not mean the
message reached a mailbox; bounces and complaints are not processed and
`provider_signature` stays unused. The application never retries by itself.

**One send per generation is the caller's sequencing (approved, 0.7).** The
service's `dispatch` hands a committed issuance to the mailer once per call;
it does not record a claim, so a second call for the same generation before
its settlement sends again. The blueprint (PR 4) must make a second call
impossible in every handler path. No durable send-claim state exists and
nothing claims that SMTP delivered exactly once.

### 4.4 Delivery configuration (D17)

**Decided:** the application sends the verification code, and the notice of
4.5, **automatically** through the `smtp` backend of 4.1, from a configured
no-reply address. **Not decided here, and configured later with the VPS:** the
sender address and domain, the credentials, and the provider or relay. No
address is invented and VPS-hosted SMTP is not assumed. Production refuses the
`console` and `file` backends. The table lists what the configuration may
later point at; none is selected.

| Possible target | For | Against |
| --- | --- | --- |
| A transactional provider's SMTP relay (for example Brevo, Mailjet, Postmark, Mailgun, Amazon SES) | Deliverability, DKIM handled, logs of delivery | An account, possibly a cost at volume, a processor of students' addresses to name in the privacy notice; data location to check; every send is a remote exchange inside a request |
| SMTP of an existing mailbox service (for example Google Workspace) | Nothing new to buy for a pilot | Low sending limits; not meant for application mail |
| Postfix on the VPS, sending directly | No third party | Deliverability to abv.bg, Gmail and school mailboxes is doubtful; many hosts block port 25 |

Pricing and free allowances change; they must be checked at the time, and no
account is created by this work.

Settings (all optional; none set means disabled): `DZI_MAIL_BACKEND`,
`DZI_MAIL_FROM` (address and display name), `DZI_SMTP_HOST`, `DZI_SMTP_PORT`,
`DZI_SMTP_SECURITY` (`starttls`, `tls`, or `none` for loopback only),
`DZI_SMTP_USERNAME`, `DZI_SMTP_PASSWORD`, `DZI_MAIL_SPOOL_DIR` (file backend),
`DZI_MAIL_HOURLY_CAP` (at most 300). The SMTP password lives in the
environment file beside `DZI_SECRET_KEY`, never in the repository. The sender
domain needs SPF, DKIM and DMARC before launch.

### 4.5 The two messages (plain text)

Code. Subject: „Код за потвърждение в LearnPilot“.

> Кодът ти за потвърждение е: 123456
> Валиден е 10 минути. Въведи го в страницата, от която започна
> регистрацията.
> Ако не си започвал(а) регистрация в LearnPilot, не прави нищо и не
> споделяй този код с никого.

Notice. Subject: „Опит за регистрация в LearnPilot“.

> Някой се опита да създаде профил в LearnPilot с този имейл, но такъв вече
> съществува. Ако си ти, влез с имейла и паролата си. Ако не си, не е нужно
> да правиш нищо.

No link that verifies anything, no name, no tracking.

## 5. Passwords and recovery

### 5.1 Password

- Hashing, method and limits are `web/services/accounts.py` unchanged
  (scrypt through Werkzeug, 10–256 characters, not the email), applied to the
  exact submitted string (1.1).
- The form adds a confirmation field. The hash is made at submission, before
  the write lock is taken, and lives in the pending row while the
  registration is pending; the password itself is never stored or logged.
  How long the hash stays readable is the retention target of 6.5, not the
  30 minutes of validity, and remnants can outlive both.
- The caps and the circuit are read before hashing, so a refused submission
  costs no scrypt work. They are checked again under the lock.
- Activation copies the hash into `users` through a new function that accepts
  only a hash this module produced (`password_hash_is_verifiable` and the
  current method prefix). `create_user` is not changed.

### 5.2 Recovery (D10)

Today a forgotten password is an operator task (`manage_accounts
set-password`, `docs/ACCOUNTS_DESIGN.md` §12). Gabriel approved **self-service
recovery by emailed code**, as its own PR (PR 8 of section 9) with a short
design addendum and security review, delivered before registration is opened
to the public. It is a separate flow: its codes are not registration
verification repeated, and sign-in never asks for one. Operator-assisted
recovery remains available through the CLI in the meantime and is not the
selected design.

Requirements the addendum must meet:

- request by email, the same visible answer for every address, known or not,
  with the limits of 3.7 on what "same" covers;
- a code to the account's `users.email`, never to `email_at_link`, with the
  generation, storage, limits, send ledger, settlement, clock policy, restore
  gate, cleanup and logging of sections 3, 4 and 6. Recovery sends count
  against the same total budget;
- **eligibility is decided again at redemption**, under the write lock, from
  the account as it is then: it exists, is active, has role `student` or
  `teacher`, has a password, and its `users.email` is still the address the
  code was sent to. A code issued before a promotion to admin, a disable or
  a role change redeems nothing. An admin never recovers by email, whatever
  the account was when the code was issued: the CLI and TOTP reset stay the
  only way;
- a recovery code is void once the account's `session_key` differs from the
  one recorded when it was issued (a password change, a disable, a role
  change, an operator sign-out and a TOTP reset each rotate it);
- success calls `accounts.set_password`, which rotates `session_key`, voids
  **every** other pending recovery code of that account, and issues no
  session;
- whether an address that was never verified is eligible is not covered by
  the approval of D10. Until Gabriel decides it in the addendum such accounts
  get no code and the same visible answer.

### 5.3 Deferred, with limits and support handling (D15)

| Deferred | Until it is built | Support handling |
| --- | --- | --- |
| Changing an account's email | A person who loses the mailbox cannot use recovery. | The operator; the CLI has no command for it and would need one, or the person registers a new account. |
| A password for an account created through Google | Such an account depends entirely on the Google account (approved decision 3 of the Google scope). It has no `users.email`, so email recovery never applies to it. | The identity-recovery tooling or the operator. |
| Unlinking and merging | The duplicate accounts of 2.6 stay two accounts. Nothing merges on a matching address. | The operator, case by case. |
| Re-verification of old addresses | `email_verified_at` is the time of the one verification; nothing re-checks it. | — |

## 6. Schema and application boundaries

### 6.1 Schema (sketch; additive; no migration is created or numbered here)

```sql
ALTER TABLE users ADD COLUMN email_verified_at TEXT;    -- NULL: never verified here
-- PR 6 (D11):
-- ALTER TABLE users ADD COLUMN profile_required_at TEXT;  -- NULL: no profile is required (7.4)

CREATE TABLE user_profiles (
    user_id INTEGER PRIMARY KEY REFERENCES users(id),
    account_type TEXT NOT NULL CHECK (account_type IN ('student', 'teacher')),
    first_name TEXT NOT NULL CHECK (length(first_name) BETWEEN 1 AND 39),
    last_name TEXT NOT NULL CHECK (length(last_name) BETWEEN 1 AND 39),
    grade INTEGER CHECK (grade IS NULL OR grade BETWEEN 8 AND 12),
    grade_set_at TEXT,
    school_name TEXT CHECK (school_name IS NULL OR length(school_name) BETWEEN 3 AND 120),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK ((account_type = 'student' AND grade IS NOT NULL AND school_name IS NULL)
        OR (account_type = 'teacher' AND grade IS NULL AND school_name IS NOT NULL))
);

CREATE TABLE teacher_subjects (
    user_id INTEGER NOT NULL REFERENCES user_profiles(user_id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK (position BETWEEN 1 AND 3),
    subject_id TEXT,                 -- a src.subject_registry id, or NULL for „Друг предмет“
    label TEXT NOT NULL CHECK (length(label) BETWEEN 2 AND 60),
    PRIMARY KEY (user_id, position)
);

CREATE TABLE pending_registrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    binding_hash TEXT NOT NULL UNIQUE,      -- 64 hex
    account_type TEXT NOT NULL CHECK (account_type IN ('student', 'teacher')),
    state TEXT NOT NULL CHECK (state IN ('pending', 'activated', 'void', 'expired')),
    is_shadow INTEGER NOT NULL CHECK (is_shadow IN (0, 1)),
    email TEXT, first_name TEXT, last_name TEXT, grade INTEGER,
    school_name TEXT, subjects_json TEXT, password_hash TEXT,   -- NULL once not pending
    code_hash TEXT, code_expires_at TEXT,
    code_send_id INTEGER,                   -- the send generation that issued the live code
    code_attempts INTEGER NOT NULL DEFAULT 0 CHECK (code_attempts >= 0),
    send_count INTEGER NOT NULL DEFAULT 0 CHECK (send_count >= 0),
    resend_after TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    closed_at TEXT,
    CHECK (state = 'pending' OR (email IS NULL AND first_name IS NULL AND last_name IS NULL
        AND grade IS NULL AND school_name IS NULL AND subjects_json IS NULL
        AND password_hash IS NULL AND code_hash IS NULL AND code_expires_at IS NULL))
);
CREATE INDEX idx_pending_registrations_state ON pending_registrations(state, expires_at);

CREATE TABLE registration_sends (
    id INTEGER PRIMARY KEY AUTOINCREMENT,   -- the send generation; never reused
    pending_id INTEGER,                     -- no foreign key: the pending row is deleted first
    email_hash TEXT NOT NULL,               -- 64 hex, keyed
    kind TEXT NOT NULL CHECK (kind IN ('code', 'notice')),
    attempted_at TEXT NOT NULL,
    outcome TEXT NOT NULL DEFAULT 'started'
        CHECK (outcome IN ('started', 'accepted', 'not_sent', 'refused', 'unknown')),
    settled_at TEXT
);
CREATE INDEX idx_registration_sends_email ON registration_sends(email_hash, attempted_at);
CREATE INDEX idx_registration_sends_time ON registration_sends(attempted_at);

-- Added by migration 022 (approved, 0.7): the identity of an issuance. NULL for a row
-- from before that migration, which is then never sent and never settled.
-- ALTER TABLE registration_sends ADD COLUMN issuance_id TEXT
--     CHECK (issuance_id IS NULL OR <exactly 32 bytes of 0-9a-f>);
-- CREATE UNIQUE INDEX idx_registration_sends_issuance
--     ON registration_sends(issuance_id) WHERE issuance_id IS NOT NULL;

CREATE TABLE registration_control (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    high_water_at TEXT NOT NULL,
    hold_reason TEXT CHECK (hold_reason IS NULL OR hold_reason IN ('restore')),
    hold_set_at TEXT
);
```

- `'admin'` cannot be stored as an `account_type` in either table.
- `account_type` is what the person declared. `users.role` remains the only
  authority; no access code reads `user_profiles`. This replaces the
  `declared_type` column the roadmap sketched.
- `pending_registrations` no longer carries the address hash; the ledger
  does. The CHECK names every sensitive column, so a row cannot leave
  `pending` with any of them set.
- `registration_control` has no row until the first issuance or cleanup; an
  absent row means no hold and no high-water mark.
- The hash-format checks of migration 020 (64 bytes of lower-case hex) should
  be applied to the hash columns; omitted above for brevity.
- `user_profiles` is apart from `users` so that a profile can later be
  erased while the `users` row that attempts and assignments reference stays.
- Classification in `src/canonical_tables.py`: all five tables **runtime**,
  never exported to `data/canonical`. `pending_registrations` holds personal
  data and a credential hash, so it is not "operational" like the keyed-hash
  tables. A fresh database has all of them empty.
- No existing table is rebuilt and no existing row is written by a
  migration. `ADD COLUMN` with a NULL default is the only change to `users`:
  `email_verified_at` in PR 1, and `profile_required_at` in PR 6 (D11). `_USER_COLUMNS` is **not** widened by PR 1 (approved after its review): it gains `email_verified_at` in the first PR that reads the column, together with the CLI's schema check (`REQUIRED_USER_COLUMNS`), because widening it earlier fails every suite that builds `users` from migration 012 alone and turns the CLI on an unmigrated database into a bare `no such column`; it gains `profile_required_at` with PR 6. Every account
  that exists when the second column is added has it NULL, which is what
  makes "nothing retroactive" a property of the data and not of a condition
  in code.
- Old code runs against the new schema (it names its columns), so a rollback
  of the code needs no schema change.

### 6.2 Routes

All but the last in a new blueprint `registration`
(`web/blueprints/registration.py`).

| Route | Methods | Access | Integrity | Notes |
| --- | --- | --- | --- | --- |
| `/register` | GET | `public` | — | Chooser. Reads `next`. |
| `/register/student` | GET, POST | `public` | `same_origin` | |
| `/register/teacher` | GET, POST | `public` | `same_origin` | 404 while the teacher gate is off |
| `/register/verify` | GET, POST | `public` | `same_origin` | |
| `/register/resend` | POST | `public` | `same_origin` | |
| `/register/cancel` | POST | `public` | `same_origin` | „Грешен имейл? Започни отново“ |
| `/account/profile` | GET, POST | `signed_in` | `same_origin` | View and edit (7.4); 404 while the profile gate is off |

Rules, each an existing convention applied to the new routes:

- **Disabled** (7.3): every `/register...` address answers the generic 404
  after the same-origin check, and no page links to it.
- **No GET writes.** The GETs render and read at most the caller's own row.
  Expiry is evaluated on read and written only by POSTs and by the cleanup. A
  GET without a usable registration sets no cookie.
- **Signed in, on the `/register...` routes only.** A GET redirects to the
  role's home; a POST starts nothing and redirects there too. This rule does
  not apply to `/account/profile`, which exists for signed-in accounts.
- **`next`** passes `safe_redirect_target` when stored and again when used.
  The confirmation page links to `/login?next=<it>` (D7). A student who came
  from a quiz link lands on the test's start page, where they press
  „Започни“; no redirect starts an attempt.
- **Responses** of `/register/verify`, `/resend` and `/cancel` carry
  `Cache-Control: no-store` and `Referrer-Policy: no-referrer`.
- A successful submission answers `303` to `/register/verify`. A repeat of a
  successful verification finds no usable registration and redirects to
  `/login` and writes nothing.
- `tests/test_access_policy.py` and `tests/test_session_cookie_samesite.py`
  must cover every new route in their sweeps.

### 6.3 Services

All independent of Flask; each function takes an open connection and `now`,
and never commits (the convention of `accounts`, `login_throttle`,
`oidc_accounts`). **The blueprint owns every transaction**: it begins,
commits after a returned outcome and rolls back after an exception.

| Module | Functions |
| --- | --- |
| `web/services/registration_forms.py` | `validate_student(form)`, `validate_teacher(form)` → cleaned values or field errors; `public_email(value)` |
| `web/services/registration.py` | `availability(conn, now, *, sender_busy)` (hold, clock, circuit, total budget, an unreaped sender in this worker); `address_capped(conn, email_hash, now)`; `create_pending(conn, *, cleaned, account_type, password_hash, previous_binding, now)` → token, row id, send generation, code or notice marker; `resend(conn, binding_hash, now)`; `settle_send(conn, send_id, outcome, now)`; `verify(conn, binding_hash, code, now, *, teacher_enabled)` → outcome; `cancel(conn, binding_hash, now)`; `cleanup(conn, now)`; in PR 5 (D20): `restore_gate(conn, now)`, `restore_status(conn)`, `release_hold(conn, now)` |
| `web/services/profiles.py` | `get_profile(conn, user_id)`, `create_profile(...)`, `update_profile(...)`, `display_name(first_name, last_name)` (PR 1); `needs_profile(conn, user)` arrives with PR 6, because it reads `profile_required_at` and the profile gate (approved after the PR 1 review) |
| `web/services/accounts.py` (additions) | `hash_new_password(password, email)`; `create_verified_user(conn, *, email, display_name, role, password_hash, verified_at)` with `role` limited to `student`; PR 7 extends it to `teacher` (D5) |
| `web/services/mail.py`, `web/services/mail_sender.py` | section 4 |

**As implemented by PR 3** the signatures above differ in their parameters
(a `clock` callable and `secret_key` in the writers, the issuance identity in
`settle_send`, and more): the exact API and the caller's obligations are in
`EMAIL_REGISTRATION_PR3_STATUS.md`, which awaits review.

**Clock-first rule, for every writer.** In `create_pending`, `resend`,
`verify` and `cleanup`, the applicable check is made first, inside the
caller's `BEGIN IMMEDIATE`, and **before** any expiry, scrub, voiding of a
previous pending row, deletion or other registration mutation:

- submission, resend and verification: the hold, then the clock against the
  high-water mark (3.8);
- `cleanup`, whether run by the timer, the CLI or a submission: the clock
  against the high-water mark. A hold does not stop it, since it only
  removes data (N3); the restore lock does (6.4).

When the check fails the function returns `UNAVAILABLE` (for `cleanup`, a
"clock behind" outcome, which the CLI reports with a non-zero status) with
the registration tables unchanged. The mark is raised only by a run whose
`now` is not behind it, and never lowered (DC6).

`verify`, in this order, inside the caller's `BEGIN IMMEDIATE`:

1. No row for the binding → `NO_PENDING`.
2. The row is `activated`, `void` or `expired` → `NOT_PENDING`. **Nothing is
   written; the row keeps the final state it has.**
3. Hold, or clock behind the high-water mark → `UNAVAILABLE`, nothing
   written. The clock policy of 3.8 is decided here, before any row is
   changed.
4. The row is `pending` and past `expires_at`, or dated in the future beyond
   the skew → set `expired`, scrub → `EXPIRED`. A row dated in the future
   can reach this step only after the operator has accepted the clock.
5. The row is a teacher registration and the teacher gate is off → set
   `void`, scrub → `UNAVAILABLE` (7.3). No code is needed and no attempt is
   counted. Before PR 7 this is every teacher registration in every
   environment: D5 gives a verified teacher the role `teacher` only once quotas
   exist (7.1), so until PR 7 the registration is refused.
6. No live code, or past `code_expires_at` → `CODE_EXPIRED`, no attempt
   counted.
7. `code_attempts` at the limit → `TOO_MANY`.
8. Count the attempt.
9. Compare. No match → `WRONG_CODE`. A shadow row never matches (2.5) and
   has no branch of its own before this step.
10. Clear the code. If the row is a shadow, or a `users` row with the address
    exists → set `void`, scrub → `EMAIL_TAKEN`.
11. `create_verified_user` with the role literal for the row's
    `account_type` (`student`; for a teacher registration, from PR 7, `teacher`), then the profile and subjects; set `activated`, scrub →
    `ACTIVATED`.

Failures in steps 10 and 11:

- `IntegrityError` from the `users` insert: read `users` by the address
  again, in the same transaction. A row exists → continue as step 10
  (`EMAIL_TAKEN`). No row → re-raise.
- Any other exception, from any statement, propagates.
- The caller rolls back on every exception. The roll-back undoes the whole
  transaction: no `users`, profile or subject row, the registration still
  `pending` with its code and its attempt count as before the request. The
  answer is the generic failure of 1.2 with status 500, and `reg` is kept.
  It never says "address taken" and never says the email was confirmed.
- A failure of the commit itself is the same case: roll back, generic
  failure. The confirmation page is rendered only **after** the commit has
  returned.
- An exception is reachable only after a correct code, so it gives a guesser
  nothing.

The caller commits after every returned outcome, so a counted attempt
persists. The `now` a caller passes to a function that issues a code is read
after its `BEGIN IMMEDIATE` has returned (3.4).

### 6.4 Transactions (owned by the blueprint)

| Request | Transactions |
| --- | --- |
| Submit | Read availability and the address caps (no lock) → hash the password → `BEGIN IMMEDIATE`: availability (hold, clock) first, then the caps again; only then the cleanup this submission runs, void this browser's previous row, insert the row and its ledger row, raise the high-water mark, commit → set `reg` → send, no transaction open → `BEGIN IMMEDIATE`: `settle_send`, commit. |
| Resend | `BEGIN IMMEDIATE`: hold and clock first, before the row is read for expiry or changed; then row, limits, caps, new code and ledger row, commit → send → `BEGIN IMMEDIATE`: `settle_send`, commit. |
| Verify | One `BEGIN IMMEDIATE` around `verify`; commit; then drop `reg` when the row left `pending`. No session is issued (D7). |
| Cancel | One `BEGIN IMMEDIATE`; commit; drop `reg`. |
| Cleanup (timer, CLI) | Refuses while the restore lock exists; one short `BEGIN IMMEDIATE`; clock check before any scrub or delete; commit. |

A settlement transaction that fails (busy, disk) is logged by class name and
leaves the ledger row `started`, which is a safe state (4.3).

### 6.5 Retention and cleanup

Validity and retention are different, and "retention" here is **logical**:
what the database returns to a query. It is a target with conditions, not a
statement about bytes on a disk.

| Data | Valid for | Logical-retention target |
| --- | --- | --- |
| A pending registration's address, names, class, school, subjects, password hash, code hash | 30 minutes | Scrubbed by the first cleanup run after it expires: about **45 minutes** after submission (30 + a 10-minute interval + scheduling slack). Earlier on activation, cancellation or replacement, which scrub at once. |
| A final row (state, binding hash, counters, times) | — | Deleted 24 hours after creation. |
| A ledger row (keyed address hash, kind, times, outcome) | the cap windows | Deleted 48 hours after the attempt. |

The target holds only while all of this is true:

- **the timer is scheduled and its runs succeed.** A systemd timer
  (`learnpilot-registration-cleanup.timer`, every 10 minutes, with the same
  `ConditionPathExists=!…/restore-in-progress` as the service) runs
  `manage_accounts registrations-cleanup`, which needs no secret. It does not
  depend on traffic: on a site that receives no request at all, expired
  registrations are still scrubbed. Submissions also run the same function,
  as a second line, not as the mechanism, and under the same clock-first
  rule (6.3): a submission that finds the clock behind the mark answers
  "unavailable" and runs no cleanup;
- **the database is available to it.** A busy database, a stopped service
  with the database being worked on, or a restore in progress postpones a
  run;
- **the wall clock is not set back.** Expiry is judged by the wall clock; a
  clock set back postpones it by the size of the step.

When a condition fails, the target is missed and nothing in the application
makes up for it. It has to be noticed: the unit's failure state and last-run
time belong in the health checks of `docs/DEPLOYMENT.md`, and
`registrations-cleanup --check` exits non-zero when a `pending` row is older
than an hour.

**Scrub** sets every column of the 6.1 CHECK to NULL in the statement that
changes the state. It also covers a registration whose gate was turned off
(7.3).

**What a scrub does not do.** It does not erase. After the values are NULL,
the old bytes can still exist:

- in the write-ahead log. In a scratch run (8.3) a marker was still in the
  WAL file after a passive checkpoint that reported every frame
  checkpointed; a checkpoint copies frames, it does not blank the log;
- in the main database file, in two places: on free pages, and in unused
  space inside pages that are still in use. Whether a scrub leaves bytes
  there depends on the sequence of writes and on the SQLite build: with
  `secure_delete` off, the third review found 182 copies of a marker inside
  live pages of a scrubbed copy that had no free page at all, and none with
  a build that has it on by default. My own runs on the development Mac
  found none; **results from one machine do not transfer to another**, and
  nothing here relies on the absence of remnants;
- in every snapshot taken while the registration was pending, in off-server
  copies of those snapshots, and in copies made below the application
  (filesystem or provider snapshots of the volume).

**No bound on physical remnants is claimed**, and the cleanup specifies no
checkpoint or `secure_delete` setting, since neither would support one.

**Backups (D18, approved: option (b)).** The live backup is unchanged: the
snapshot's database is made by `src.backup_sqlite` with SQLite's online
backup API, which is what keeps it consistent with the WAL. The scrub is done
**to that isolated copy**, never to the live database, and before the snapshot
is verified and published. It is the void-and-scrub step of the restore gate
without the hold: the published snapshot's database holds no pending
registration and no value in the columns of the 6.1 CHECK. Old bytes may
remain in the copy's free pages and in unused space inside its pages.

**Placement.** The transformation is applied to the isolated partial file
`backup_database` has just written with the backup API, **before** that
function's `quick_check`, its fsync, the link that publishes the file and the
`sha256` it returns. Every check and the recorded hash therefore describe the
file that is published, after the transformation.

**Not selected.** Leaving pending rows in snapshots, and an offline rebuild
with `VACUUM INTO`. History worth keeping if the rebuild is ever reconsidered:
it would need its own design, with per-table logical digests whose identity
fits the table's kind (the four rowid tables without an `INTEGER PRIMARY KEY`
in migrations through 020 are `topic_classes`, `topic_prerequisites`,
`assessment_event_sections` and `exam_task_questions`; `login_throttle` and
`oidc_consumed_transactions` are `WITHOUT ROWID`), a check that nothing relies
on a rowid, and marker evidence about the published file only. None of that is
part of this design or its tests.

What the snapshot step does not do: change a snapshot that was already published,
an off-server copy of one, or the blocks of the partial files it replaces.
Snapshots also keep accounts, profiles and the ledger's keyed hashes for as
long as they are kept; the privacy notice must say so (D16).

Retention of **accounts and profiles** is not designed here
(`docs/ACCOUNTS_DESIGN.md` §15, D16).

### 6.6 Operator CLI (`src/manage_accounts.py`, additions only)

| Command | Does |
| --- | --- |
| `registrations-cleanup [--check]` | Runs `cleanup`. No secret needed. Refuses while the restore lock exists. |
| `registrations-restore-gate` | The gate of 3.9, on the database named by `--db`. One transaction; repeatable; never clears a hold it finds. |
| `registrations-restore-status --expect <state>` | Read-only. Fails unless no pending registration or recovery credential exists, the control row exists and the hold is in the expected state (3.9). |
| `registrations-release-hold --acknowledge-lost-history` | The only way a `restore` hold ends. Refuses without the flag; prints the consequence of 3.9 before it acts. |
| `registrations-clock-accept` | Lowers the high-water mark to the present after the operator has checked the time (3.8). |
| `clear-registration-limit --email <email>` | Deletes the ledger rows of one address (needs `DZI_SECRET_KEY`, like `clear-throttle`). It releases one further daily budget for that address, which the command says before it acts: up to 8 more codes inside the same day. |
| `list` | Adds the verification date and the declared type. |

Every existing command keeps its behaviour.

## 7. Permissions and deployment

### 7.1 Teacher self-declaration and quotas

Verification proves the mailbox. Nothing here checks that a person teaches.

What the `teacher` role grants today (`docs/AUTH_ROUTE_CONTRACT.md`): creating
assignments at `/teacher/new`, managing and closing the assignments it owns,
their results and CSV with students' names, QR sharing and practical review
of its own assignments. It does not grant `/teacher`, `/dzi...` or anything
unowned.

**No per-teacher quota exists.** `web/blueprints/teacher.py` has no creation
limit, `docs/ACCOUNTS_DESIGN.md` §20 lists "teacher creation quotas" as
deferred, and the only limit is the Nginx write zone. Each assignment also
writes a note into the vault. Opening the role to anyone with a mailbox
(plus-addresses included) without limits would be a new privilege granted by
assumption, so this design does not grant it:

- the teacher gate is its own constant, off (D14), and covers activation as
  well as the form (7.3);
- turning it on requires the quota PR (section 9, PR 7): at least a
  number of assignments created per day and a number of open assignments per
  teacher, with values Gabriel approves and has **not yet selected**. D5 is
  approved: a verified teacher registration then receives role `teacher`;
- `user_profiles` has no "verified teacher" field. Teacher verification, when
  designed, adds its own.

The student role grants what it grants today: its own attempts and `/account`.

### 7.2 Privacy and minors (activation prerequisites, not designed here)

- New personal data: first and last name, grade, school name and subjects
  (teachers), a verified address, and the data of unverified registrations
  for the storage period of 6.5. `docs/ACCOUNTS_DESIGN.md` §15 must be
  amended with the implementation; it currently lists school and class as not
  collected.
- The mail provider becomes a recipient of addresses (and of the code).
- Required before the forms are enabled: a privacy page linked from both
  forms; the lawful basis and whether a consent control is needed; the
  consent age and parental consent for the youngest students; the purpose and
  visibility of class, school and subjects; the retention period for accounts
  and for backups. None is decided here and none may be invented during
  implementation.
- Existing sinks carry the new `display_name`: CSV exports and vault notes.

### 7.3 Gates

Three build constants, all `False` (D14, the mechanism approved for Google as
P2). In production each applies in addition to a usable mail backend where
mail is involved. A constant is changed by a deploy, which restarts the
service; registrations pending at that moment are handled as the last column
says.

| Gate | Covers | While off | Pending rows when it is turned off |
| --- | --- | --- | --- |
| `registration.PRODUCTION_ACTIVATION` | every `/register...` route: form, submission, resend, verification and activation, cancel | Generic 404 on all of them; no link on the sign-in page; no row is created, no code is checked, no account is activated. | Unreachable (every route is 404) and scrubbed by the next cleanup run when they expire. Nothing activates. |
| `registration.TEACHER_ACTIVATION` | the teacher form **and the activation of a teacher registration** | `/register/teacher` is 404; the chooser has no teacher option; `resend` and `verify` for a teacher row void and scrub it and answer „Регистрацията временно не е достъпна…“. | Voided at their next request or scrubbed at expiry. A teacher registration is never activated, and is never turned into a student. |
| `profiles.PRODUCTION_ACTIVATION` | `/account/profile`, its link on `/account`, the redirect after Google onboarding, the new-attempt check, the writing of `profile_required_at` | `/account/profile` is 404; Google onboarding redirects exactly as today; starting an attempt is exactly as today; no account is marked. | Not applicable. Marks already written stay and are ignored while the gate is off. |

The teacher gate has nothing to turn on before PR 7: until then a teacher
registration is refused at verification in every environment (6.3), because
quotas do not exist (D5).

With all three off, production behaves as before this milestone for every
existing route, which is what "production behaviour unchanged" means in
section 9. Email-registered accounts get their profile at activation and do
not need the profile gate to exist; they need it only to view or edit it.

Besides the gates, registration answers „…временно не е достъпна…“ (not 404)
while a restore hold is set, the clock is behind the high-water mark, the
mail circuit is open or the total budget is used (3.5, 3.8, 3.9).

### 7.4 Profiles: who is asked, and when

`users.role` decides access. A profile and `profile_required_at` decide
nothing about access to data; they can only send a student to a form before a
**new** attempt.

**Who is marked** (`profile_required_at` set), under D11:

- a student account created by Google onboarding (`CREATED`) while the
  profile gate is on. The callback sets the mark in the transaction that
  creates the account and redirects to `/account/profile?next=<target>`;
- nobody else. Accounts created by the CLI, accounts created through Google
  before the gate was on, and every teacher and admin have NULL. No
  command marks existing students.

**`needs_profile(conn, user)`** is true only when all hold: the profile gate
is on; the account's **current** role is `student`; `profile_required_at` is
set; the account has no `user_profiles` row of any type.

**Where it is enforced.** In `POST /quiz/s/<token>` only, and only where a
new attempt would be created. The order in that request:

1. Not signed in, or not allowed to take quizzes → as today.
2. The account already has an attempt at this assignment → resume it or show
   its result, as today. **A missing profile never blocks a resume**, whether
   the assignment is open or closed.
3. The assignment is closed → the closed notice, as today.
4. `needs_profile` → `303` to `/account/profile?next=/quiz/s/<token>` with
   „Попълни профила си, за да започнеш теста.“ No attempt is created.
5. Otherwise start the attempt, as today.

These five are **one decision, made where it is made today**:
`quiz_start_or_resume_attempt` already chooses between resuming, refusing a
closed assignment and creating, under the writer lock. `needs_profile` is
evaluated inside that same locked decision, at the point where the function
has established that no attempt exists and that it **would create** one; it
then returns a "profile required" outcome and inserts nothing. The route
does not look for an existing attempt itself and then decide separately, so
two requests of one account cannot disagree, and a resume, a result and a
closed notice are reached exactly as before.

`/quiz/attempt/<id>`, saving answers, submitting, results, „Моите опити“,
browsing and the account pages are never blocked.

**Return.** `next` passes `safe_redirect_target` when stored and when used;
anything it refuses becomes the role's home. The return is a GET of the start
page; the student starts the attempt themselves.

**The page.**

| Account | `/account/profile` |
| --- | --- |
| Student without a profile | The student form: first name, last name, class. On success creates the profile and sets `display_name` to the two names. Asks for no email and no password (5.3). Optional unless `needs_profile`. |
| Account with a profile | Shows and edits the fields of the profile's stored `account_type`. |
| Teacher or admin without a profile | Shows that there is no profile. Nothing to fill and nothing required in this milestone. |

**What completing or editing a profile does not touch:** `role`, `email`,
`email_verified_at`, `is_active`, `session_key` (the session stays valid),
the `quiz_attempt_owner:*` markers, `quiz_attempts.user_id` and every stored
`student_name`. Later attempts copy the new `display_name`.

**Operator role changes** (`set-role`) do not write, delete or reinterpret a
profile or the mark:

- student → teacher or admin: `needs_profile` becomes false at once; the
  profile, if any, stays as the declared type it was;
- teacher → student: asked only if the account carries the mark and has no
  profile row, which can happen only to an account that was a marked student
  before. An account that registered as a teacher has a profile row and a
  CLI-created one has no mark; neither is asked;
- a mismatch between `role` and `account_type` is expected and visible in
  `list`. It grants and removes nothing (test I6).

**Google.** A first Google sign-in still creates a `student` with the Google
profile name as `display_name`, no email and no profile row. A teacher does
not come into being through Google; a teacher registers by email and may then
link Google with the existing flow. An admin never links (2.6). Google stays
off in production; nothing here changes `google_oidc.PRODUCTION_ACTIVATION`
or its prerequisites.

### 7.5 Configuration and rollout

- Registration is **enabled** when a mail backend is configured and usable
  and, in production, its gate is on. While a gate is off, production starts
  normally, logs one warning if mail settings are present, and never refuses
  to start because of them.
- With the registration gate on, malformed mail settings are reported by
  `production_config_errors`, as `google_sign_in_errors` are. That wiring,
  and the refusal of `console` and `file` by a production start, arrive with
  the gate in PR 4: PR 2 provides `mail.settings_problems(...,
  production=True)` for it and changes no start-up code.
- Development: `DZI_MAIL_BACKEND=console` shows the code in the terminal. No
  real message is sent by any test or smoke check.
- Order of a later, separately authorised rollout: migration → code with
  gates off → cleanup timer and restore gate installed → mail settings, a
  controlled send and the evidence of 0.3 → student gate → profile gate → quotas and teacher gate. Each step has the previous one's rollback:
  set the constant back; the tables stay.
- Documents to update with the implementation: `AUTH_ROUTE_CONTRACT.md`
  (routes, and "There is no registration form…"), `ACCOUNTS_DESIGN.md` §10,
  §12, §15, §20, `DEPLOYMENT.md` (mail, sender process, Nginx zone, timer,
  restore gate, snapshot step, CLI), `TESTING.md`, the Nginx template.

## 8. Blockers, evidence and unresolved assumptions

### 8.1 Blockers

Two kinds, which must not be confused. An **implementation blocker** stops a
PR of section 9 from being written. An **activation prerequisite** stops a
gate from being turned on and blocks no PR (0.3).

| # | Item | Kind |
| --- | --- | --- |
| 1 | **No open decision blocks PR 1–6.** D1–D15 and D18–D20 are approved (0.2). Approval does not authorize an implementation; each PR starts when Gabriel says so. | Neither |
| 1a | **Quota values for PR 7** are not selected, and PR 7 needs its own short scope. **PR 8** needs its own addendum, including the recovery eligibility of never-verified addresses (5.2). | Implementation blocker for PR 7 and PR 8 only |
| 2 | **D16** (privacy and minors) and the **configuration of D17** (sender address and domain, credentials, provider or relay, set with the VPS). | Activation prerequisite |
| 3 | **Teacher quotas do not exist** (7.1). | Implementation blocker for PR 7 only; activation prerequisite for the teacher gate. Not for the student form. |
| 4 | **No privacy page, no retention period, no settled position on minors' consent, no stated purpose for the profile fields** (7.2). | Activation prerequisite |
| 5 | **No delivery provider or sender domain.** Deliverability to the mailboxes students actually use (abv.bg, Gmail, school `edu.mon.bg` accounts) is untested, and whether school mailboxes accept outside mail at all is unknown. | Activation prerequisite |
| 6 | **Capacity of one synchronous worker** (3.5). Unmeasured. The criterion is stated; whether a remote relay meets it is not known; a local relay was not selected (D19), so a failed measurement needs its own decision. | Activation prerequisite |
| 7 | **Linux-only evidence:** the systemd timer, the restore script's gate with an interrupted run and a running cleanup job, the snapshot step, and the sender process under the real unit (worker replacement, descriptor isolation, signal state, sandboxing). None can be run from the development Mac. The logic is tested portably. | Activation prerequisite. For PR 5 it is also review evidence the PR must bring. |
| 8 | **Duplicate accounts** across Google and password sign-in (2.6) have no correction until the identity-recovery milestone. | Neither: a known limit, tied to Google's own activation |

### 8.2 Residual risks stated, not solved

- The 40-guess bound holds only for a steady clock, an unchanged key and an
  intact ledger (3.3). Small clock steps reach 65 guesses in a day; a key
  rotation or a released hold reaches 80.
- Repeated forward steps of the clock release repeated budgets (3.8).
- Senders can overlap across a worker replacement, and neither process
  creation nor reaping has a bound of its own (4.2).
- A send whose outcome is not certain is `unknown`: a person may wait a
  minute for a code that was never sent (4.2).
- Duration and relay behaviour can differ between a code and a notice (3.7).
- A slow but successful relay is bounded only by the hourly cap (3.5).
- A daily cap lets someone block an address for a day (3.6, D6).
- A restore done by hand, outside the script, skips the gate (3.9).
- A scrub is logical. Old bytes can remain in the WAL, in free pages and
  unused page space, in
  earlier snapshots and in copies below the application, with no bound (6.5).
- Every restore closes registration until the operator acts, and a released
  hold loses the accounting since the snapshot (3.9, D20).

### 8.3 What was executed for this revision

Scratch models only, outside the repository, with the standard library and
in-memory or temporary SQLite. They are not application tests; no application
code, real database, socket, mail server or provider was involved.

| Claim | Model | Result |
| --- | --- | --- |
| Revision 1 admitted more than 8 codes a day | The review's schedule under creation-time counting | 12 of 12 sends admitted; 11 issuances in a trailing 24 h |
| The ledger refuses that schedule | The same schedule under 3.4 | 8 of 12 admitted; the four late submissions refused |
| The bound of 3.3, **steady clock only** | 300 randomised greedy senders over four simulated days, probing every cap boundary | At most 8 guessable codes in any 24 h (40 guesses) |
| The bound does not survive small clock steps | The second review's schedule: two steps of 300 s | 13 of 13 admitted; first five guessed at real 899 s, last issued at real 87 010 s: 65 guesses inside one real 24 h |
| The window must exceed 24 h | The same senders against a plain 24-hour window | 13 guessable codes (65 guesses) |
| Late failures do not touch newer state | Settlement after a resend, after activation, after cancellation; double settlement; failure after acceptance | Row unchanged in every case |
| The work budget is not refundable | 400 attempts in one hour, each failing definitely | Exactly 300 admitted |
| Restore without a gate revives credentials; with it, none remain | Snapshot, then cancel and exhaust attempts, then restore | 2 live codes without the gate; 0 pending with it. (The model's naive ledger copy is not the validated merge of 3.9, which no model here covers.) |
| The isolated entry point | `-I` from the checkout, by module name and by absolute script path | `web` is not importable in isolated mode; a standard-library script run by absolute path starts |
| A child can be ended at a deadline (revision 2; run with `communicate`, which revision 4 replaces) | A child producing a little output forever, and one blocked in a sleep, killed by the parent | Ended at 1.00 s and 1.01 s of a 1 s deadline; no process left; start cost 0.11 s. Left alone with a 1 s alarm: ended by itself at 1.04 s |
| A scrub is not erasure | Byte search of the live file, the WAL and a backup-API copy after several write sequences | Sequence-dependent. A marker remained in the WAL after a passive checkpoint that reported every frame checkpointed (126 of 126); in other sequences it was found nowhere. I did not reproduce the review's free-page remnants. No absence is claimed. |
| An offline rebuild of a snapshot copy (option not selected) | `VACUUM INTO` on a backup-API copy with 58 free pages | New file: 0 free pages, `quick_check` ok, rows kept, one file in rollback-journal mode |
| The exchange of 4.2 bounds both pipes (revision 4) | A selector exchange with a 16-byte output cap and a 1 s monotonic deadline, against stand-in children; input of 1 MiB for the child that never reads | Flood: ended at 1.01 s, 17 bytes read (the model's cap plus one; the design stops at 16). Never reads its input: 1.00 s. `R` then block: 1.00 s. `R` and exit 0, and exit 12 with no output: at once, clean end-of-file |
| The alarm survives an inherited signal state (revision 4) | Children started with `SIGALRM` ignored, and blocked, by the parent | Plain `alarm(1)`: exit 0 after 3.0 s in both. Reset, unblock, then arm: ended by `SIGALRM` at 1.05 s in both |

The randomised run supports the steady-clock bound; the argument of 3.3 is
what establishes it, under its three assumptions and no others. The worker
replacement overlap and the merge defects the second review modelled were
not re-run here: this revision accepts them as findings. Provider behaviour, server capacity, classroom traffic and
every Linux-only path remain unverified.

## 9. Implementation sequence

**One PR at a time, in this order**, as the roadmap requires. That two PRs
do not depend on each other technically is not permission to run them in
parallel. Each PR is green on the full suite, leaves production behaviour
unchanged (every gate off, 7.3) and updates the documents it makes untrue.

Executors are suggestions. Schema and form work is routine. The pending
lifecycle, send settlement, the mail deadline and recovery are security
boundaries: High, with independent review. Nothing here calls for Extra High
as a rule; it would be justified only for a concrete open problem.

| PR | Content | Decisions needed first | After | Executor | Independent review |
| --- | --- | --- | --- | --- | --- |
| 1 | Migration(s): `users.email_verified_at`, `user_profiles`, `teacher_subjects`, `pending_registrations`, `registration_sends`, `registration_control`; classification; `profiles` service (without `needs_profile`, which is PR 6); form validators (exact passwords, dedicated email and `next` rules); migration and service tests. No route. Does not widen `accounts._USER_COLUMNS`. | None open (D1–D4, D6, D12, D13 decided) | — | Sonnet High | yes (schema) |
| 2 | `web/services/mail.py` and the standalone sender script: interface, the process contract and outcome rule of 4.2, the deadline and cleanup paths, SMTP, console, file and in-memory backends; settings parsing (sender address from configuration, no default); production refusal of the development backends. No caller. | None open (D17 behaviour, D19 decided) | 1 | Opus High | yes (security: process containment, TLS, headers, logging) |
| 3 | `web/services/registration.py` and the `accounts` additions: pending lifecycle, codes, ledger, caps, circuit, settlement, shadow rows, activation of **student** registrations and its error classification, clock policy, cleanup. A teacher registration is never activated here. No Flask. | None open (D6, D8 decided) | 2 | Opus High | yes (security) |
| 4 | `registration` blueprint for students, templates, the `reg` session slot, the registration and teacher gates, the cleanup and limit CLI commands, Nginx zone, contract and deployment documents. The teacher route and its gate exist and answer 404 in every environment until PR 7. | None open (D7, D9, D14 decided) | 3 | Opus High | yes (security) |
| 5 | Operations: cleanup timer unit; the restore gate, status and release functions and commands (hold after every restore, D20); the restore script's gate, cleanup-job handling and final validation; the logical snapshot scrub (D18 (b)); health checks. | None open (D18, D20 decided) | 4 | Opus High | yes (operations; Linux evidence required) |
| 6 | `/account/profile`, profile on `/account`, the profile gate; the migration adding `users.profile_required_at`, the mark at Google onboarding, `profiles.needs_profile` and the new-attempt check. | None open (D2, D11 decided) | 5 | Sonnet High | yes |
| 7 | Teacher quotas (needs its own short scope), then role `teacher` for a verified teacher registration (D5), the teacher form and subjects behind `TEACHER_ACTIVATION`. | Quota values (not selected) | 6 | Sonnet High | yes (security) |
| 8 | Password recovery, from its own addendum (5.2), before public opening (D10). | The addendum | 7 | Opus High | yes (security) |
| — | Activation: separate, explicitly authorised, after the prerequisites of 0.3. Student registration does not wait for PR 7. | D16; configuration of D17 | — | — | — |

**Approved after the PR 1 review (not independently reviewed).** (1) The first PR
that reads `users.email_verified_at` (expected PR 3 or PR 4) widens
`accounts._USER_COLUMNS` and the CLI's schema check; PR 1 does not (6.1).
(2) `needs_profile` is implemented in PR 6 with `profile_required_at` and its
gate (6.3, 7.4). (3) „Избери предмети от списъка.“ is the message for an invalid
subject selection (1.2). (4) The „Друг предмет“ text is ignored when `other` is
not selected (1.1); teacher-form UX is PR 7. Details and the PR 1 review
outcome: `EMAIL_REGISTRATION_PR1_STATUS.md`.
