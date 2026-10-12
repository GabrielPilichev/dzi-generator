# Google OIDC sign-in — security design

Status: **design proposal, revision 2.3 with the corrections of its review.
The independent review of revision 2.3 returned "approve with non-blocking
notes", for the technical specification only. A documentation pass has since
corrected that review's seven notes (0.3, last table). A Codex review of
that pass returned "approve with non-blocking notes", for the technical
documentation only. Three wording corrections were made after it (0.3) and
have not been independently reviewed. Gabriel approved the product
decisions P1–P4, T5 and M1–M3; they are recorded in 0.2. Implementation and
production activation remain unapproved, and no deployment check is
approved. Nothing of PR #331 is implemented.** Its storage prerequisite
exists: PR #332
(migration `020_oidc_consumed_transactions.sql` and
`web/services/oidc_consumption.py`) is merged on `main` at `008c236`. It is
not deployed, and nothing calls it.
PR #331, branch `feature/google-oidc-signin`, scope
`docs/reviews/GOOGLE_OIDC_SCOPE.md`. Revision 2 incorporated the six decisions
Gabriel approved on 2026-10-11 (0.2) and was reviewed independently. Revision
2.1 made that review's corrections and separated what is approved from what
is only proposed (0.5). Revision 2.2 specified what happens when a retirement
cannot be recorded (4.4) and reconciled the document with the storage as it
was merged. Its review found that blocker resolved and asked for one required
change: a cancellation must not say that linking was cancelled when the link
had been completed, or when the object had already been used. This revision
makes that change (1.6, 4.4) and the review's other corrections (0.3). The
pass after the review of revision 2.3 changed no decision and no
architecture. It made M1–M3 implementation gates, which Gabriel's decisions
have since lifted (0.2). Three of its corrections go beyond wording and are
named for a focused review (0.4).

Section 0 holds the decisions, the corrections and what blocks
implementation and activation.
Sections 1–8 specify PR #331. The consumption storage is specified by what
PR #332 merged, and only there; section 9 names those files and says how
PR #331 uses the storage. Section 10 defines the follow-up milestone (identity
recovery). The test inventory is in the companion
`GOOGLE_OIDC_TEST_PLAN.md`.

**How the facts here were established.** Five kinds of statement are kept
apart:

- *Repository*: read from this worktree at `64a84fd`, cited as `path:line`.
- *Merged prerequisite*: read from `origin/main` at `008c236` with
  `git show`, cited as a path with the word "merged". **This branch does not
  contain those files.** It has not been brought onto `main`, and this
  revision did not do that. The untracked
  `docs/reviews/OIDC_CONSUMPTION_SCOPE.md` in this worktree is the draft from
  before that file's review; the merged file replaces it and differs from it
  (section 9). Wherever this document cites the scope, the merged file is
  meant.
- *Documented*: from a primary source, cited as `[n]` (section 13), retrieved
  2026-10-11; `[22]`–`[25]`, the buffering directive of `[14]`, the
  request-body files of `[16]` and the parser, body-reader, worker and
  arbiter files of `[18]` on 2026-10-12. A statement taken from the
  source code of Nginx, Gunicorn or Werkzeug says which version was read.
  **Reading a version is not the same as verifying the server:** which
  versions production runs was not read (11.2 check 16).
- *Experiment*: measured in a scratch environment outside the repository
  (temporary databases, mocked or loopback transport, real RS256 signatures).
  Cited as `E1`–`E21` (11.3).
- *Not verified*: behaviour of the real Google service, and of Nginx on the
  server. No provider was registered or contacted beyond its public metadata,
  and no Nginx was available to run the proposed configuration. These items
  are marked and are activation checks (11.2). None of them has been carried
  out.

## 0. Summary and decisions

### 0.1 The design in brief

- **Routes.** `POST /auth/google/start` (sign-in), `POST /auth/google/link`
  (start linking), `GET /auth/google/callback`, `GET`/`POST
  /auth/google/link/confirm`, `POST /auth/google/link/cancel`, and a new
  `GET /account` page. The callback is the one view that declares
  `OAUTH_STATE`; every POST is an ordinary same-origin form.
- **Transaction.** One slot in the signed session, `session["oidc_tx"]`:
  state, nonce, PKCE S256 verifier, intent, validated return target, expiry,
  the service instance that issued it and, for linking, the binding to the
  initiating account session. No token and no secret is ever in the cookie.
- **Single use.** The first callback whose state matches records the
  transaction in a server-side table, in its own committed write, before any
  other parameter is looked at and before any outbound call. A second use is
  refused, from the same browser, from a copy of the cookie, or at the same
  moment. The table and its storage service came with PR #332, which is
  merged (section 9).
- **Linking.** Starting a link asks for nothing. After Google returns and the
  ID token is validated, a confirmation page names the Google profile and the
  LearnPilot account; the identity is stored only after the account's current
  password is given on a same-origin POST. The confirmation has its own
  single-use record, written in the transaction that stores the identity.
- **Ending a link.** Cancelling, signing out, starting another link and
  replacing the session's account each try to record that linking in progress
  has ended. The attempt reports what it achieved, object by object. When the
  record cannot be written, cancellation says so, the others go on, and
  nothing claims that a copy of the cookie was invalidated (4.4). A
  cancellation says „отказано“ only when its own record ended the link and
  the account had no Google identity when the route read it. It reads the
  account to know that. Every answer of a cancellation is a statement about
  the session of that response and about that one read (1.6).
- **Identity.** `(issuer, subject)` only. Google's two documented issuer
  spellings are accepted as two exact strings and stored as one constant.
  Email never selects, signs in or links an account.
- **Onboarding.** A first-time Google identity becomes a `student` in one
  `BEGIN IMMEDIATE` transaction that creates the user and the identity
  together. Admins can neither link nor sign in through Google.
- **Network.** One total budget of 8 seconds per callback covers name
  resolution, connections and both response bodies. The work runs in a worker
  thread that the request stops waiting for at the deadline; the thread is not
  cancelled, at most two are inside the network function per process, and a
  late result is discarded.
- **Operations.** Disabled unless both credentials are configured. Production
  does not activate the provider before recovery tooling exists (approved);
  the build holds that line with the activation gate of 8.1 (P2, approved).
  The callback URI comes from `DZI_PUBLIC_HOST`. Gunicorn's and Nginx's
  logging changes so that no log carries a code or a state.
- **Migrations.** PR #331 has none. Migration 020 came with PR #332.

### 0.2 Decisions approved by Gabriel on 2026-10-11

| # | Decision | Specified in |
| --- | --- | --- |
| A1 | Transactions are literally single-use. State, nonce, PKCE verifier and browser binding stay in the signed session; a dedicated server-side consumption table records use. It is delivered by a separate prerequisite PR (since merged as PR #332), does not overload `login_throttle`, and is not a full server-side OAuth transaction store. This amends the "no table" sentence of `docs/ACCOUNTS_DESIGN.md` §13. | 2, 3, 9, `OIDC_CONSUMPTION_SCOPE.md` |
| A2 | A link is confirmed after Google returns: both identities are shown, and the current LearnPilot password is required on a same-origin POST before the identity is stored. Starting a link asks for no password. An account without a password cannot add an identity in this release. | 1.6, 4 |
| A3 | A student created through Google keeps `users.email` `NULL`. The operator CLI selects by `--id` for `disable`, `enable`, `sign-out` and `set-role`. | 6.3, 8.6 |
| A4 | The email collision rule covers every usable provider email, verified or not. An identity that is already linked is resolved by issuer and subject first. An unusable email is omitted and is never an identifier or an account email. | 6.3 |
| A5 | Supported operator recovery for mistaken links is required before the provider is activated. It is a separate follow-up milestone; this PR implements no unlinking. | 0.4, 10 |
| A6 | One enforced total network budget. | 5.7 |

Also approved earlier and unchanged: automatic student onboarding; the issuer
mapping (5.5); password and admin TOTP sign-in untouched and no admin OAuth
(6.2); a callback that does not match leaves the stored transaction alone
(1.5).

**Also approved by Gabriel, and recorded here on 2026-10-12.** These were
the proposals P1–P4, T5 and M1–M3 of 0.5. They keep their numbers, which
mark them wherever they appear.

| # | Decision | Specified in |
| --- | --- | --- |
| P1 | A link is refused when the email Google reports belongs to another LearnPilot account. | 4.2, 6.3 |
| P2 | The build-time activation gate, as proposed. While it is off, production starts normally, Google sign-in stays disabled, and one warning is logged. | 8.1 |
| P3 | Sign-out keeps redirecting to the home page. | 1.7 |
| P4 | „Ученик“ is the display name when Google supplies no usable name for a student. | 6.4 |
| T5 | `set-role --role admin` is refused for an account without a password. | 8.6 |
| M1 | A cancellation whose outcome is not established (1.6, row 5) answers with the code `link_not_pending` and the text „Няма свързване с Google, което тази заявка да прекрати.“ | 1.6, 1.9 |
| M2 | A cancellation that finds a Google identity on the account (1.6, row 2) answers with the code `account_linked` and the text „Профилът ти вече е свързан с Google.“ | 1.6, 1.9 |
| M3 | A cancellation whose record could not be written, or whose reading of the account failed (1.6, rows 1 and 3), answers with the code `cancel_unconfirmed` and the text „Не успяхме да потвърдим отказа.“ | 1.6, 1.9 |

These are product decisions. They approve neither the implementation nor
production activation (0.4).

### 0.3 Corrections made after the review of revision 2

| Correction | Where |
| --- | --- |
| The consumption table has no `instance` column and deletes rows by expiry only. Its safety does not depend on one worker, on preloading or on import timing. Rows that outlive a restart or a reboot are inert only because every caller refuses objects of another run before it asks the storage; they can accumulate, and both facts are documented (3.4). | `OIDC_CONSUMPTION_SCOPE.md` §1, §3, §5; 3.3 row 17; 3.7 |
| One replaceable `time.monotonic()` function on every platform. No `CLOCK_BOOTTIME`, no platform branch, no clock subsystem. | 2.3, 3.5 |
| Callers read the clock after they hold the write lock, pass the object's own signed expiry, and reject an expired object before any cleanup. Purposes are a fixed set. Transaction boundaries and failures are spelled out for each call site. | scope §3, §4; 1.5 H7; 1.6; 4.3; 4.4; 9 |
| The affinity tests of the schema are corrected. | scope §1, §7 |
| The prerequisite is a standalone file; this document and the test plan refer to it. | `OIDC_CONSUMPTION_SCOPE.md`; 9 |
| Retirement of linking in progress is attempted on every replacement of the session's account, a password sign-in included. (What it achieves when the record cannot be written: the second table.) | 4.4 |
| The identity is resolved first, in the same order, at the callback and at the confirmation. | 4.2, 4.3 |
| Which component answers which method on the callback address. | 1.2 |
| A worker thread may only hand over its result and release its slot. Cache, cooldown and slot state are guarded by one lock. A worker's exception is reduced to its class name. | 5.7 |
| The breaker is a plain cooldown. The half-open probe and the limit of one key fetch per minute are removed. | 5.6, 5.8 |
| Gunicorn's log lines for requests its parser refuses. | 8.4 |
| With the provider disabled, a redirect from the callback address shows no Google message. | 1.8, 8.3 |
| Kept as reviewed: the 8-second budget and the bound of two tasks. | 5.7 |

**Corrections made after the review of revision 2.1 (revision 2.2)**

Three rows of this table are corrected again by the next one: the outcomes
of `retire_linking` (first row), when cancellation answers `link_cancelled`
(third row), and what request buffering has to do with tracebacks (the row
before the last).

| Correction | Where |
| --- | --- |
| **The blocker.** `retire_linking` returns an outcome for each object it found: `invalidated`, `dead` or `not_recorded`. A storage failure is never reported as a retirement. | 4.4 |
| Retirement needs a connection on which its caller has no pending transaction. If one is open, it executes nothing and reports `not_recorded`. It begins its own transaction only after that check, reads the clock after it holds the write lock, and rolls back only a transaction that it began itself. What a failure in the middle leaves is defined. | 4.4, 7.1, 9 |
| Cancellation answers `link_cancelled` only when nothing was left unrecorded, and `unavailable` otherwise. Sign-out, a new link and every replacement of the session's account go on in both cases. (Revision 2.2's rule. The rows and codes are now those of 1.6, where a cancellation that could not be recorded answers `cancel_unconfirmed`.) | 1.4, 1.6, 1.7, 4.4 |
| Every statement that a copy of the cookie "is refused" after a cancellation, a sign-out, a new link or a change of account now names its condition, and what remains without the record is stated exactly. | 2.6, 3.3 rows 14, 16 and 18, 3.4, 4.4, 4.6 |
| After the code exchange a `link` callback reads the account again and checks its binding before it creates a pending link, as the test plan already expected. | 1.5 V7, 4.2, 4.3 |
| A slot reserved for a worker thread that could not be started is given back. | 5.7, 5.8 |
| "Nothing is written" is corrected where a throttle counter or the storage's cleanup is written. | 1.5 V7, 3.3 row 13, 4.3, 5.3 |
| After a confirmation that was rolled back, the pending link stays in the session. | 1.6, 2.6, 4.3 |
| The Gunicorn filter cuts at whichever quotation mark comes first. A traceback is not rewritten by it; what that leaves, and why request buffering matters for it. | 8.4, 8.7 |
| The prerequisite is merged: migration 020, the real API, and the merged documentation for Python versions, the key's constraints and retained rows. | 0.4, 2.1, 3.4, 3.6, 8.7, 9 |

**Corrections made after the review of revision 2.2 (this revision, 2.3)**

| Correction | Where |
| --- | --- |
| **The required change.** `AlreadyConsumed` proves that an object was used. It does not prove that linking was cancelled. `retire_linking` reports it as a separate outcome, `consumed`, apart from `dead` (malformed, another instance, expired). An empty slot proves nothing either. | 4.4 |
| Cancellation reads whether the account now has a Google identity. It answers `link_cancelled` only when every object it found was invalidated by its own committed record and the account has none. A consumed object, an empty session, a dead object, a record that could not be written and an account that could not be read never produce `link_cancelled`. The slots are removed in every case. | 1.6, 1.9, 4.4 |
| A retirement acts on the objects the session holds. It cannot invalidate a pending link that a callback created for another copy of the cookie. `complete` and "every copy" are narrowed to what an outcome establishes, object by object. The password, the account and session-key binding, the issuing run and the expiry still stand between that pending link and an identity. | 3.3 rows 19–22, 3.4 limit 8, 4.4 |
| The wording for a cancellation whose outcome is not established, and for one that finds the account linked, was **proposed** (M1–M3). Gabriel has since decided all three (0.2). | 0.5, 1.9 |
| Tracebacks. Request buffering is delivery timing. It is neither validation nor redaction, and the design no longer credits it with keeping request bodies out of the log. Three mechanisms are told apart: Nginx's own parsing and re-framing of a request body, request buffering, and Werkzeug's input wrapper under `MAX_CONTENT_LENGTH`. The residual is restated, the loopback bind and the limit are named as required assumptions, and the server probe is described as what it is, a test of Nginx's parser. | 8.4, 8.7, 11.2 |
| A failed `COMMIT` and a failed rollback are defined for the callback and for the confirmation, as they already were for retiring. Ownership of a transaction begins when the call site's own `BEGIN IMMEDIATE` has returned. No outcome claims what a failed `COMMIT` left on the disk. | 1.5 H7, 1.6, 9 |
| A network slot is given back exactly once, also when `Thread.start()` raises after the thread exists. A refusal for lack of a slot and a failed start leave the failure count as it was. | 5.7, 5.8 |
| "Nothing more is written" in 5.3 no longer includes the successful row. The issuing-run check is placed where it runs, in the route's first validation. One preloaded instance per run is stated as a deployment requirement, and what two masters would do is characterised. | 5.3, 4.4, 3.7, 8.7 |
| The plan for bringing the branch onto `main` is corrected: which two places conflict, what `main`'s text lacks, which local text carries a status phrase, and in which order the files are handled. Nothing of it was carried out. | 12 |

**Corrections made after the review of revision 2.3 (a documentation pass on
revision 2.3)**

That review returned "approve with non-blocking notes" for the technical
specification and listed seven notes. Each is corrected here, under its
number. A Codex review of this pass returned "approve with non-blocking
notes" for the technical documentation; it approves neither implementation
nor activation. The three wording corrections that followed it are listed
below the table and have not been independently reviewed. The pass ran none
of the server checks.

| Note | Correction | Where |
| --- | --- | --- |
| 1 | The two malformed-body probes are specified to the byte: raw HTTP/1.1, every line ended by CRLF, `Connection: close`, one synthetic marker for each probe, a client timeout, and one run of each inside the deployment window. They carry nothing real and are excluded from the repetition with a real `state`. Pass criteria are stated, and every other result is not a pass. The direct probe gains a control request, because a request with an untrusted host is answered 400 as well, before any body is read (E20). The probes were not run and are not in `docs/DEPLOYMENT.md`. | 8.7 steps 3 and 8; 11.2 check 10; test plan D |
| 2 | M1–M3 were made implementation gates, with no default. Gabriel has since decided all three, and the gates are lifted (0.2). | 0.4, 0.5, 1.6, 1.9, 3.3, 4.4, 8.7, 11.2; test plan |
| 3 | A held write lock and a `query_only` connection fail at `BEGIN IMMEDIATE`. A database file that cannot be written does not, in rollback-journal mode: the statement returns, and the first write fails inside a transaction that the call site owns and rolls back. The two are kept apart, E16's sentence is corrected, and no statement depends on which of them a given SQLite and journal mode produce. | 4.4 step 7; 9; 11.3 (E16, E19); test plan Method, B1, B4, B5 |
| 4 | What a cancellation answers is a statement about the session of that response and about the account at the moment it was read. Two orderings are stated as limits: a callback of the same browser whose response comes after the cancellation's, and another session of the account that links after the read. `link_not_pending` is no longer called true "in every case". | 0.5, 1.6, 1.9, 4.4 |
| 5 | The bound of two is on workers inside the network function, not on thread objects. The give-back rule covers every exception between the taking of the slot and the return of `Thread.start()`, not only one raised by `start()`. The worker's release is in a `finally` that covers its first two actions. The ownership protocol is the reviewed one; where it applies is wider than what the review modelled (0.4). | 0.1, 5.7, 5.8; 11.3 (E21); test plan B2 |
| 6 | The sync worker class is named as a third required assumption of what 8.4 says about logs, beside the loopback bind and the request limit: a worker class that keeps connections alive reads an unread body itself, outside Werkzeug's wrapper. Its check on the server is outstanding, like the others. | 8.4, 8.7 step 3, 11.2 check 15; test plan B6, C, D |
| 7 | B7's `set-role` test carries its T5 marker. Section 12 names the heading that `main`'s §13 has. | test plan B7; 12 step 6 |

Three wording corrections made after the Codex review. **They have not been
independently reviewed:**

- **Section 9, "Ownership".** `Expired` is not the only way a request on a
  database that cannot be written can end. Expiry is what the clock reading
  taken there can safely decide by itself. The storage's `ValueError`, its
  cleanup and write failures, and an earlier failure of the throttle write
  remain. Acceptance still needs `consume` to return and the `COMMIT` to
  succeed, and PR #332's contract is unchanged.
- **8.7, probe N.** Nginx parses the framing in both buffering modes. The
  four pass criteria as a set are stated for the deployed configuration,
  buffering on. With buffering off the absence of an entry in Gunicorn's
  access log is not guaranteed. The distinction is derived from source and
  is not verified on a server.
- **8.7, probe D.** The optional second send after a 403 is removed. A 403
  is not a pass. Its cause is diagnosed and corrected before a new bounded
  verification run is scheduled, and that run includes the control `GET`.

Nothing in revisions 2.2 and 2.3, and nothing in the pass after the review,
changed a decision of 0.2. Revision 2.3 added three message choices to 0.5,
and the pass after the review made them implementation gates. **Gabriel has
since decided P1–P4, T5 and M1–M3 (0.2), and those gates are lifted.** The
tables above are kept as the record of what each pass did.

### 0.4 What blocks implementation, and what blocks activation

**Implementation is not approved.** The independent review of revision 2.3
approved the technical specification, with seven non-blocking notes. It
approved no product choice and no deployment check, and it has not seen the
corrections made for its notes (0.3, last table). Whether and when
implementation starts is Gabriel's decision, and the recording of his
product decisions here does not make it. The storage it needs is merged
(PR #332); this branch has still to be brought onto `main` to contain it,
which is Gabriel's step and was not done here (section 12 has the order of
that step).

**No product decision is open.** Gabriel approved P1–P4, T5 and M1–M3
(0.2). The gates that the pass after the review of revision 2.3 had placed
on M1–M3 are lifted with that approval: the cancel route has an approved
code and text in every row of 1.6, and it answers with those and with
nothing else.

**What the review of revision 2.3 has not seen.** Every correction of the
last table of 0.3. Most restate what the review asked for. Three go beyond
wording, and each is named here for a focused review instead of being
counted under the existing approval:

1. **5.7, the slot rule.** The same guard and the same "begun" flag, now
   applied to any exception after the slot is taken and until `start()` has
   returned, and a worker whose release is in a `finally` from its first
   statement. The review's model exercised a `start()` that raises. A
   scratch model for this pass exercised the wider rule (E21); that model is
   the author's and is not a review. One boundary stays uncovered and is
   stated in 5.7: an asynchronous exception between the taking of the slot
   and the region's first statement. Closing it would change the reviewed
   guard, and that change is not made.
2. **8.7 step 3, the control request of the direct probe.** It is not in the
   review's note. Without it the stated pass criteria can be met by a
   request that never reached the body read (E20).
3. **Section 9, "Ownership".** On a database file that cannot be written, a
   `BEGIN IMMEDIATE` that returned holds no write lock (E19). The clock is
   then read without one. The text states why nothing can be accepted with
   that reading. PR #332's contract is not changed by it.

**The Codex review of that pass.** A Codex review of the documentation as
the pass left it returned "approve with non-blocking notes". It is a
verdict on the technical documentation only. The three wording corrections
made after it (0.3) have not been independently reviewed, and neither has
the recording of Gabriel's decisions in this text. Implementation and
activation remain unapproved.

**Enabling the provider for real users is blocked by all of the
following, none of which has happened:**

1. Migration 020 deployed. PR #332 is merged, and its release has not been
   deployed (`docs/DEPLOYMENT.md` §28, merged).
2. The recovery milestone (section 10) delivered. This is approved (A5), and
   so is the mechanism that enforces it in the build (8.1, P2).
3. Privacy: a public privacy-policy page (Google's consent-screen branding
   needs one [7]); the data-controller questions for minors listed in
   `docs/REFACTOR_ACCOUNTS_ROADMAP.md`; school Workspace administrators
   allowing the app for accounts marked under 18 [8].
4. The log changes deployed and checked on the server (8.7 step 3).
5. The real-provider smoke checks (11.2).
6. Independent review of the implementation.

### 0.5 Choices, and their status

**Policy choices: approved by Gabriel (0.2).** They were the proposals
P1–P4 until then. Each is marked with its number where it appears.

| # | Decision | Section |
| --- | --- | --- |
| P1 | A link is refused when the email Google reports is the address of a *different* LearnPilot account. This extends A4's rule from sign-in to linking. | 4.2, 6.3 |
| P2 | The production activation gate: one constant in the build. While it is off, production starts normally whatever the two credential settings hold, keeps the provider disabled and logs one warning. The recovery milestone's release turns it on. While it is off, the presence of credentials never keeps the service from starting. | 8.1 |
| P3 | Sign-out keeps today's destination, the home page. The notice that the Google session stays open is on the sign-in page and on the account page. Revision 2's redirect to the sign-in page with a message is withdrawn. | 1.7 |
| P4 | The display name of a student whose Google profile supplies no usable name is „Ученик“. | 6.4 |

**Message choices: approved by Gabriel (0.2).** Revision 2.3 added them as
the proposals M1–M3. The behaviour they belong to was a correction and not a
choice: in these cases the cancel route never answers `link_cancelled`
(1.6). What Gabriel decided is what it answers instead.

| # | Case, and its rows in 1.6 | Code | Text |
| --- | --- | --- | --- |
| M1 | Row 5: a cancellation whose outcome is not established. The account had no Google identity when it was read, nothing was left unrecorded, and the cancellation's own record did not end the link: the session held nothing, or the object had already been used, had expired, was malformed or belonged to another run. | `link_not_pending` | „Няма свързване с Google, което тази заявка да прекрати.“ |
| M2 | Row 2: a cancellation that finds a Google identity on the account. | `account_linked` | „Профилът ти вече е свързан с Google.“ The page lists the identity below it. |
| M3 | Rows 1 and 3: a cancellation whose record could not be written, or whose reading of the account failed. | `cancel_unconfirmed` | „Не успяхме да потвърдим отказа.“ |

**What each text says.** 1.9 has them in full, and 1.6 ("Two limits") the
reasons.

- The texts of M1 and M3 describe this request and nothing after it: M1
  that it had no link to end, M3 that the cancellation could not be
  confirmed. They stay true whatever follows, and neither says what the
  browser holds afterwards.
- The text of M2 describes the account as it was read. PR #331 removes no
  identity, so it stays true until an operator's recovery (section 10) or a
  restore changes the account.
- No text says that no link can follow, and none says that a copy of the
  cookie was invalidated.

M1, M2 and M3 are three separate cases and are never merged: a known link, an
outcome that is not established, and a failure each have their own row,
their own code and their own text, and the log lines of rows 1 and 3 are
written as specified (1.6, 4.4).

**Technical choices, as put to the independent review.** These carry no
product decision. T5 is the exception: it was a pending choice, and Gabriel
has approved it (0.2).

| # | Choice | Section |
| --- | --- | --- |
| T1 | Transactions and pending links are bound to the running service instance and timed on the monotonic clock, not the wall clock. A restart, and therefore every restore, ends everything outstanding: a deploy during a lesson fails the sign-ins that are in flight. | 2.3, 3.5, 3.6 |
| T2 | Lifetimes: 600 s (sign-in transaction), 300 s (link transaction), 180 s (pending link). | 2.4 |
| T3 | Network budget 8 s; two slots; a cooldown of 60 s after three consecutive failures or one overrun. | 5.7, 5.8 |
| T4 | `email_at_link` stores the address Google reported whether or not Google marked it verified, as §5 of the accounts design defines the column. | 6.3 |
| T5 | `set-role --role admin` is refused for an account without a password. **Approved by Gabriel (0.2).** It follows from A3's `--id` selection, since such an account could not sign in as an admin. | 8.6 |
| T6 | Pinned provider endpoints; the dependency set; the `iss` response parameter is checked when present. | 5.2, 5.1, 1.5 |

## 1. Routes and policies

### 1.1 Route table

| Route | Endpoint | Method | Access policy | Integrity policy |
| --- | --- | --- | --- | --- |
| `/auth/google/start` | `oidc.google_start` | POST | `public` | `same_origin` |
| `/auth/google/link` | `oidc.google_link` | POST | `signed_in` | `same_origin` |
| `/auth/google/callback` | `oidc.google_callback` | GET | `public` | `oauth_state` |
| `/auth/google/link/confirm` | `oidc.google_link_confirm` | GET, POST | `signed_in` | `same_origin` |
| `/auth/google/link/cancel` | `oidc.google_link_cancel` | POST | `signed_in` | `same_origin` |
| `/account` | `account.overview` | GET | `signed_in` | — (safe method) |

- The blueprint is `oidc` in `web/blueprints/oidc.py`. It must not be named
  `auth`: the removed shared-password blueprint's names are pinned as absent
  (`tests/test_auth_blueprint.py`). No path contains "login" or "logout".
- The routes are registered in every environment, enabled or not, so the route
  map and the policy coverage tests see the same application everywhere.
- None of the routes preserved by `AGENTS.md` changes.

### 1.2 Enforcement order, and why unsafe-request checks are not weakened

Today every request passes three `before_request` steps (`web/app.py:316`,
`:330`, `:459`). One step is added between the first and the second:

1. `protect_same_origin_unsafe_methods` — **unchanged.** Every POST, PUT,
   PATCH and DELETE needs a same-origin `Origin` or `Referer`. No route is
   exempt, including the four new POST routes.
2. **New:** `verify_oauth_state`. It acts only when the request resolved to a
   view that declares `OAUTH_STATE`. It verifies a `GET` (1.5) and refuses
   every other method that resolves to that view.
3. `load_current_user` — unchanged.
4. `enforce_access_policy` — unchanged.

**Methods on the callback address.** The route is registered for `GET`.
Werkzeug and Flask then also resolve `HEAD` and `OPTIONS` to the view: left
alone, Flask runs the view for a `HEAD` and answers an `OPTIONS` itself. No
view resolves for any other method, but the hooks still run for it (E10,
Flask 3.1.3).

| Method | Resolves to the view | Answered by | Result |
| --- | --- | --- | --- |
| `GET` | yes | step 2, then the view | 1.5 |
| `HEAD`, `OPTIONS` | yes | step 2 | 405 with `Allow: GET`. The session, the database and the network are not touched, and the view does not run. A `HEAD` never redeems a code. |
| `POST`, `PUT`, `PATCH`, `DELETE` | no | step 1 for a cross-origin request (403); otherwise Flask's routing (405) | Step 2 does nothing: there is no view. |

The view runs only for a request that step 2 matched: it refuses to proceed
without the transaction that step 2 puts on `g`. Behind Nginx every 403 and
405 of this address reaches the browser as the redirect of 8.3.

The callback does not weaken step 1 because it never passes through an
exemption: it accepts no unsafe method. `OAUTH_STATE` is an additional
mechanism for one safe-method route, not an alternative to `SAME_ORIGIN` for
an unsafe one. Tests pin the separation:

- a view that accepts an unsafe method declares `SAME_ORIGIN` (existing test,
  unchanged);
- a view that declares `OAUTH_STATE` accepts no unsafe method (new);
- exactly one view declares `OAUTH_STATE`; `PROVIDER_SIGNATURE` stays unused;
- the verifier lives in `web/oauth_state.py`, which imports no blueprint.

**The callback is a GET that changes state.** `docs/AUTH_ROUTE_CONTRACT.md`
says no GET does that on the visitor's behalf, with the lifecycle observation
stamp as the one exception. The callback becomes the second. That is why it
has its own integrity mechanism, and why a callback without a matching
transaction writes nothing at all: no cookie, no row, no outbound request
(1.5). The contract text is amended accordingly (section 12).

### 1.3 `POST /auth/google/start`

Form fields: `next` (optional). Nothing else is read.

1. Provider disabled (1.8): 404.
2. The request's own origin differs from the configured public base URL: 404.
   The callback would arrive on the public host without this host's cookie.
3. A signed-in account is present: no transaction is created and nothing is
   sent to Google. Redirect to the validated `next` or the role's home, as
   `GET /login` does (`web/blueprints/account.py:191`). An admin's pending
   second factor is not a principal and may start (7.3).
4. The provider is cooling down (5.8): redirect to
   `/login?google=unavailable`.
5. Create the transaction with intent `signin` (2.1), replacing any earlier
   one. Check the cookie budget (2.8).
6. Respond `303` to Google's authorization endpoint with `Cache-Control:
   no-store`.

Authorization request parameters, built by Authlib's
`OAuth2Session.create_authorization_url` (E4): `response_type=code`,
`client_id`, `redirect_uri` (8.2), `scope=openid email profile`, `state`,
`nonce`, `code_challenge`, `code_challenge_method=S256`,
`prompt=select_account`.

- `prompt=select_account` is always sent: Google then "prompts the user to
  select a user account" [1]. It does not make Google ask for a password
  again: Google documents only `none`, `consent` and `select_account` [1]. A
  Google session left open on a shared computer is therefore one click away
  from the next person (4.6). *Not verified:* what Google shows in each case.
- Not sent: `access_type` (so no refresh token is issued [1]), `hd`,
  `login_hint`, `include_granted_scopes`, `response_mode`, `claims`.

### 1.4 `POST /auth/google/link`

Access policy `signed_in`. No form field is read: no password is asked here
(A2) and the flow returns to the confirmation page.

1. Provider disabled or origin mismatch: 404, as in 1.3.
2. The account's role is `admin`: 403 (`403.html`).
3. The account has no password, or already has a Google identity: redirect to
   `/account?google=not_allowed`.
4. The provider is cooling down: redirect to `/account?google=unavailable`.
5. Linking already in progress in this session is retired first (4.4). The
   route goes on whatever the result: both slots are empty afterwards in
   every case, and a retirement that was not recorded is logged, not shown.
6. Create the transaction with intent `link`, bound to this account session
   (4.1). Respond `303` to Google as in 1.3.

### 1.5 `GET /auth/google/callback`

**Step H — the hook `verify_oauth_state`, before the principal is loaded.**

1. The method is `HEAD` or `OPTIONS`: 405 with `Allow: GET`, and nothing is
   read (1.2). The steps below are for a `GET`.
2. Provider disabled: do nothing; the view answers 404.
3. The raw query string is longer than 2048 bytes, or `state` is not present
   exactly once, or it is not 43 characters of the URL-safe alphabet: **no
   match**.
4. `session["oidc_tx"]` is absent or is not a well-formed version-1
   transaction (2.1): **no match**. The slot is not touched.
5. `hmac.compare_digest(stored_state, state)` is false: **no match**. The
   stored transaction is left as it is. A request that does not match is not
   that transaction's callback, and clearing it would let any site cancel a
   sign-in in progress.
6. The transaction names another service instance (2.3): remove the slot;
   outcome `expired`. Nothing is recorded: no instance, now or later, accepts
   it.
7. **Match.** Remove the slot from the session. Then record the use, on the
   request's connection, in this order (section 9):
   1. `BEGIN IMMEDIATE`.
   2. Read the clock (2.3) once. Steps 1 and 2 are one function,
      `begin_write`.
   3. `consume` with the key of purpose `callback` derived from the state, that
      reading as `now`, and the transaction's own `exp` as `expires_at`.
   4. `COMMIT`.

   The transaction is **accepted only if all four returned normally.** That
   commit then stands whatever happens afterwards. Every other result ends
   the request here, with no outbound call and no account or session change.
   The hook rolls back if, and only if, its own `BEGIN IMMEDIATE` had
   returned; a failed `COMMIT` and a failed rollback are handled as section 9
   defines ("Ending a transaction that a call site began"), and neither
   changes the outcome below:
   - `AlreadyConsumed`: outcome `failed`.
   - `Expired` (`now >= exp`): outcome `expired`. Nothing was recorded, and
     nothing needs to be: the same transaction is expired at every later
     moment too.
   - a `sqlite3.Error` from any of the four statements, a busy database and a
     failed commit included: outcome `unavailable`; one log line with the
     exception's class name. On a Python older than 3.11 the storage reports
     a repeated key as the `sqlite3.IntegrityError` it is (section 9), so a
     replay answers `unavailable` there and not `failed`. It is refused
     either way.
   - anything else, the storage's `ValueError` and `RuntimeError` included:
     outcome `failed`; logged by class name.
8. Put the transaction on `g` for the view.

**No match** answers `302` to `/login?google=failed` and writes nothing: no
`Set-Cookie`, no database access, no outbound request. This is the property
the existing sweep `test_no_get_changes_state_or_touches_the_cookie` checks
for every GET route, and the callback joins that sweep.

**Step V — the view.** It runs only for a transaction that step H matched
and recorded.

1. `error` parameter present: `cancelled` when it is exactly `access_denied`
   [3], otherwise `failed`. The value is never echoed or logged.
2. `code` is not present exactly once, or is not 1–256 visible ASCII
   characters (Google's documented maximum is 256 bytes [5]): `failed`.
3. `iss` parameter present and not exactly `https://accounts.google.com`:
   `failed`. Google advertises the parameter in its discovery document [2]; its
   absence is accepted until a real flow shows that Google always sends it.
4. Intent checks that need the principal, before any network call:
   - `signin`: a signed-in account is present → `failed`. The session is not
     touched and **no linking happens** (4.5).
   - `link`: the first validation of 4.2.
5. The provider is cooling down (5.8), or no slot is free (5.7):
   `unavailable`.
6. Exchange the code and verify the ID token under the network budget
   (section 5). No SQLite transaction or lock is held meanwhile.
7. - `signin`: one `BEGIN IMMEDIATE` transaction, sign-in or onboarding
     (section 6), then the session (7.1).
   - `link`: the second validation of 4.2, read-only. The exchange took
     time, so the account is read again and its binding checked, and then the
     link decision is taken. If both allow the link, the pending link is
     written to the session (2.2). **This step writes nothing to the
     database.** What a `link` callback has written by then is the record of
     H7, with the storage's cleanup of expired records, and nothing else.
8. Respond `302` to a local path. The callback **never renders a page**: every
   outcome, including an unexpected exception (rolled back, logged by class
   name only), is a redirect with `Cache-Control: no-store`.

Targets: `/login?google=<code>` for a failed `signin`; `/account?google=<code>`
for a failed `link`; `/auth/google/link/confirm` for a `link` that reached a
pending link; section 7 for a successful sign-in. `<code>` is one of a fixed
set (1.9), looked up on render and never echoed.

Steps H7 and V1–V3 are in this order on purpose (A1): a callback that carries
an `error`, a malformed `code` or a wrong `iss` has already spent the
transaction. The person starts again.

### 1.6 Confirming and cancelling a link

**`GET /auth/google/link/confirm`** renders the confirmation page (1.7) when
the session holds a pending link that passes the first validation of 4.2.
Otherwise it redirects to `/account?google=expired`. It writes nothing and
does not change the cookie.

**`POST /auth/google/link/confirm`**, form field `current_password`:

1. Provider disabled: 404. Role `admin`: 403.
2. First validation of 4.2. On failure the pending link is removed from the
   session and the answer is `/account?google=expired`.
3. The account's throttle counter is blocked: the confirmation page again,
   status 429, „Твърде много опити. Опитай отново след няколко минути.“ The
   counter is the account's existing one (`_account_throttle_key`,
   `web/blueprints/account.py:77`), so sign-in, password-change and
   confirmation guesses share one limit of 10 failures in 15 minutes.
4. `accounts.verify_password` with the submitted password (longer than
   `MAX_PASSWORD_LENGTH` counts as wrong). Wrong: record a failure, commit,
   render the page with „Грешна текуща парола.“ The pending link stays, so the
   person can try again until it expires or the counter blocks.
5. Correct. One transaction on the request's connection, in this order:
   1. `BEGIN IMMEDIATE`.
   2. Read the clock (2.3) once. Steps 1 and 2 are `begin_write`, as at the
      callback.
   3. Clear the account's throttle counter, as a correct password does on the
      sign-in form.
   4. `confirm_link` (4.3), which returns an outcome.
   5. `COMMIT`, after **every** outcome.

   An exception from any of the five rolls the whole transaction back: no
   record, no identity, and the counter as it was. The transaction is the
   route's own from the moment step 1 has returned. If step 1 itself failed,
   the route owns no transaction and issues neither `COMMIT` nor `ROLLBACK`.
   The answer is then `unavailable` for a `sqlite3.Error` and
   `failed` for anything else, logged by class name. **After a rollback the
   pending link stays in the session.** Nothing was recorded, so it is as
   unused as before, in this browser and in any copy of the cookie, and it
   can be submitted again until it expires. Removing it from the cookie
   would end nothing on the server.

   **A `COMMIT` that fails** (step 5) is a `sqlite3.Error` like any other:
   the route calls `rollback()` once, answers `unavailable` and keeps the
   pending link. It does not retry, and the answer claims nothing about what
   reached the disk: `unavailable` says neither that the identity was stored
   nor that it was not. Whatever is true is found by the next request. The
   account page reads the identities from the database, and a second POST of
   the same pending link either links (nothing was stored) or meets
   `AlreadyConsumed` and answers `already_linked` or `failed` by what the
   database holds (4.3 step 1). **A rollback that fails** is caught and
   logged by class name. It changes no answer, and the route executes
   nothing further on the connection (section 9, "Ending a transaction that
   a call site began").
6. After a commit, whatever the outcome: remove the pending link from the
   session and redirect to `/account?google=<outcome>`. After a rollback, a
   failed `COMMIT` or a failed `BEGIN IMMEDIATE`: redirect to
   `/account?google=unavailable` or `failed`, with the pending link kept;
   the account page then still offers the confirmation (1.7).

**`POST /auth/google/link/cancel`**

1. Provider disabled: 404.
2. `retire_linking()` (4.4). It removes both slots from this browser's
   session in every case, before anything below is decided, and it returns
   an outcome for each object the session held.
3. **Read the account.** One read on the request's connection: does the
   signed-in account have a Google identity now (`identities.list_identities`,
   a row of provider `google`)? It comes after step 2 on purpose. A
   confirmation commits its record and its identity in one transaction
   (4.3), so once the retirement has met that record, the identity is
   visible to this read. It is one read, at one moment; "Two limits" below
   says what that leaves open.
4. Redirect to `/account?google=<code>`, where the code is that of the first
   row that applies:

   | # | Finding | Code |
   | --- | --- | --- |
   | 1 | The read of step 3 failed. | `cancel_unconfirmed` (M3) |
   | 2 | The account had a Google identity at that read. | `account_linked` (M2) |
   | 3 | An object is `not_recorded`. | `cancel_unconfirmed` (M3) |
   | 4 | The session held at least one linking object, and every object it held is `invalidated`. | `link_cancelled` |
   | 5 | Anything else: the session held nothing, or an object was `consumed` or `dead`. | `link_not_pending` (M1) |

   The rows, their order and their codes are specified. The codes of rows
   1, 2, 3 and 5 are Gabriel's decisions M1–M3 (0.2), and their texts are in
   1.9. In every row both slots are gone, and the answer is not
   `link_cancelled` unless the row is 4.

- **`link_cancelled` is a statement about this request's own record.** Row 4
  alone gives it. It then means three things, each established: the object
  had not been used before (4.4, `invalidated`), the record that refuses it
  from now on is committed, and the account had no Google identity at the
  read of step 3.
- **What never gives `link_cancelled`:**
  - a Google identity on the account (row 2): the link exists, whatever the
    retirement found;
  - an object that had already been used (`consumed`; row 2 or 5).
    `AlreadyConsumed` proves a use, by a confirmation, by a callback or by an
    earlier retirement, and this request cannot tell which;
  - a session that held nothing (row 5). An empty slot is what a completed
    link, an expired one, an earlier cancellation and an earlier cancellation
    that was *not* recorded all leave behind;
  - an object that was `dead` (row 5). An expired transaction may have been
    used before it expired (4.4);
  - a record that could not be written (row 3), and an account that could
    not be read (row 1). The read's failure is logged by class name.
- **Row 2 comes before row 3.** With a Google identity on the account nothing
  further can be linked to it: a link start is refused (1.4 step 3), and the
  link decision answers `already_linked`, `identity_taken` or `not_allowed`
  for every pending link (4.2). The record that could not be written would
  protect nothing more, and the link is what the person has to know. The
  log line of 4.4 is written all the same.
- **Row 3** is revision 2.2's rule, unchanged: the server could not record
  that the linking ended, so no page says that it was cancelled. A copy of
  the earlier cookie, if there is one, stays usable until the object
  expires; 4.4 says for what, and what it still takes. Rows 1 and 3 answer
  `cancel_unconfirmed` (M3), and not `unavailable`, whose text speaks of
  sign-in.
- **Row 5 claims nothing,** neither a cancellation nor a link. In particular
  it does not say that no link can follow. A callback made earlier with a
  copy of the start cookie leaves a pending link in that copy, and this
  session can neither name it nor invalidate it (4.4; 3.3 row 21).
- **Two limits of every answer.** A cancellation answers for the session
  that its own response sets, and for the account as step 3 read it. It
  does not answer for what happens afterwards. Two orderings show it, and
  neither has an effect on security:
  1. *A callback of the same browser whose response comes after this one.*
     The callback had been accepted, with its record of 1.5 H7 committed,
     before the cancellation retired anything. The cancellation finds the
     transaction `consumed` and its row is 5. The callback's response then
     sets a cookie that holds a pending link, and the browser holds an
     unfinished link again, after a response whose cookie held none. The
     text of M1 speaks of the request and not of the browser, so it is not
     contradicted. This takes requests that are handled concurrently (the
     development server; more than one worker or thread), or two responses
     that the browser applies in another order than the server wrote them.
     The one sync worker of production excludes the first and not the
     second, so the design relies on neither. That pending link is a
     successor (4.4, item 2): it is confirmed only with the password and
     under every check of 4.3, and „Отказ“ pressed again retires it by
     row 4.
  2. *Another session of the account that links after the read.* Two
     browsers signed in to one account share its session key (4.1), and
     each can hold a pending link of its own. This session's cancellation
     invalidates its own object, reads the account, finds no Google identity
     and answers `link_cancelled`. The other session's confirmation commits
     after that read. The account page that this browser loads next lists a
     Google identity below „Свързването с Google беше отказано.“ Both
     statements are true: this session's link was cancelled, and the account
     was linked from elsewhere. The message never said that the account
     stays without a Google identity. In the other order the read sees the
     identity and the row is 2 (E16).
- In every row the response sets a cookie without a linking object. The
  account page shows no unfinished link when the request that renders it
  carries that cookie, and it lists the account's sign-in methods as read
  from the database at that later moment.
- The codes `link_not_pending`, `account_linked` and `cancel_unconfirmed`
  are new with this PR. Their texts are in 1.9.

### 1.7 Pages

**Sign-in page (`login.html`).** Below the password form, shown only when the
provider is enabled: a second form, `POST /auth/google/start`, carrying the
already validated `next`, with one button „Вход с Google“. Next to it:

- „При вход с Google получаваме името ти, имейл адреса ти и идентификатор на
  профила ти в Google. Паролата ти не стига до нас. Ако влизаш за първи път,
  ще ти създадем ученически профил.“
- „Учител си и вече имаш профил? Влез с имейл и парола и свържи Google от
  „Профил“.“
- „Изходът от LearnPilot не те отписва от Google. На общ компютър излез и от
  Google.“

`GET /login` from a signed-in account redirects as today, except that, while
the provider is enabled, a known `google` code is carried to
`/account?google=<code>`, so that a redirect issued by Nginx (8.3) shows its
message to a signed-in person too.

**Account page (`account.html`, new, `GET /account`).** Name, role, email if
any, and the sign-in methods: whether a password is set, and each Google
identity with the email Google reported when it was added and the date. One
extra query (`identities.list_identities`). Then:

- student or teacher with a password and no Google identity, provider enabled:
  a form with one button „Свържи с Google“ (`POST /auth/google/link`);
- a pending link that is still valid: „Имаш незавършено свързване с Google.“
  with a link to the confirmation page and a „Отказ“ button;
- an account with a Google identity: „Изходът от LearnPilot не те отписва от
  Google.“;
- an account without a password: „Този профил влиза само с Google. Засега към
  него не може да се добави парола или друг начин за вход.“, no form;
- admin: „Администраторските профили влизат само с парола и код.“, no form;
- provider disabled: linked identities are still listed, with „Входът с Google
  в момента е изключен.“

**Confirmation page (`account_link_confirm.html`, new).** Rendered inside the
signed-in layout, so the account's name is also in the header.

- „Ще свържеш Google профила **{name} ({email})** с профила в LearnPilot
  **{display name} ({email}, {role})**.“ The Google name and email are the
  cleaned values of 6.3 and 6.4; a missing email reads „без имейл“.
- „След това всеки, който влезе с този Google профил, ще влиза в този профил в
  LearnPilot.“
- „Ако това не е твоят профил в LearnPilot, натисни „Отказ“ и излез от Google
  на този компютър.“
- Field „Текуща парола за LearnPilot“; buttons „Свържи“ and „Отказ“ (a
  separate form to the cancel route).

**Sign-out.** `POST /logout` (`web/blueprints/account.py:300`) gains one
thing: it tries to retire linking in progress before it clears the session
(4.4). It clears the session and answers as today whether or not that
retirement was recorded, and it shows nothing about it: signing out does not
fail because a record could not be written. Its destination is unchanged,
the home page (P3). The notice that the Google session stays open
is the static text on the sign-in page and on the account page above.

`base.html` gains a „Профил“ link to `/account` beside „Парола“.
`/account/password` shows a notice instead of the form to an account that has
no password; today it would answer „Грешна текуща парола.“ and count a
throttle failure against it.

**The button.** Google's branding rules apply [6]: Google's own "G" asset
unmodified, one of the published themes, an approved call to action localized.
The asset is a local static file. No Google script, font or image is loaded
from Google, so nothing reaches Google before the visitor presses the button.

### 1.8 Disabled provider

The provider is enabled only when both credentials are configured and valid, a
public base URL is configured, and the build permits it (8.1). Otherwise:

- no Google button, no link form;
- the five `/auth/google/...` routes answer the generic 404 page, after the
  same-origin check as for any URL (a cross-origin POST is still 403);
- **no page shows the outcome of a Google sign-in.** The `google` query
  parameter is ignored on the sign-in page and on the account page, and is not
  carried by the redirect of a signed-in `GET /login`. Behind Nginx the
  callback's 404, like every error status of that address, arrives as a
  redirect to `/login?google=unavailable` (8.3); the visitor then sees the
  ordinary sign-in page, exactly as it looks today. The one text about Google
  that remains is the account page's notice to an account that already has a
  Google identity (1.7);
- a transaction or pending link left in a cookie is ignored and never read,
  and nothing is retired;
- existing accounts created through Google keep their rows and cannot sign in
  until the provider is enabled again. Password and TOTP sign-in are
  unaffected.

### 1.9 Messages

| Code | Text | Shown on |
| --- | --- | --- |
| `cancelled` | „Входът с Google беше отказан.“ | both |
| `failed` | „Входът с Google не успя. Опитай отново.“ | both |
| `expired` | „Времето изтече. Опитай отново.“ | both |
| `unavailable` | „Входът с Google временно не е достъпен. Опитай по-късно.“ | both |
| `email_exists` | „Вече има профил с този имейл. Влез с имейл и парола и свържи Google от „Профил“.“ | sign-in |
| `not_allowed` | „Този профил не може да използва вход с Google.“ | both |
| `linked` | „Google профилът е свързан.“ | account |
| `already_linked` | „Този Google профил вече е свързан с твоя профил.“ | account |
| `identity_taken` | „Този Google профил вече е свързан с друг профил.“ | account |
| `email_other_account` (P1) | „Имейлът на този Google профил е на друг профил в LearnPilot. Влез в онзи профил и го свържи оттам.“ | account |
| `link_cancelled` | „Свързването с Google беше отказано.“ | account |
| `link_not_pending` (M1) | „Няма свързване с Google, което тази заявка да прекрати.“ | account |
| `account_linked` (M2) | „Профилът ти вече е свързан с Google.“ | account |
| `cancel_unconfirmed` (M3) | „Не успяхме да потвърдим отказа.“ | account |

A disabled account and an admin identity both get `not_allowed`: the person
has proven control of the Google identity, so the message tells nothing about
anyone else. An unknown `google` value shows nothing, and no value shows
anything while the provider is disabled (1.8).

**The four findings of a cancellation** (1.6) are kept apart, and none stands
in for another:

| What the cancel route established | Code | Decision |
| --- | --- | --- |
| Its own record ended the link, and the account had no Google identity when it was read (row 4). | `link_cancelled` | specified by 1.6 |
| The account had a Google identity when it was read: a known completed link (row 2). | `account_linked` | M2, approved |
| Neither of these, and nothing failed: the outcome is not established (row 5). | `link_not_pending` | M1, approved |
| The record could not be written, or the account could not be read (rows 1 and 3). | `cancel_unconfirmed` | M3, approved |

**What the texts are true of.** Each is a statement about the response that
carries it, and none is a statement about the future (1.6, "Two limits").

- „Свързването с Google беше отказано.“: this request's record ended the
  link that this session held, and the account had no Google identity at
  the read. It does not say that the account stays without one.
- „Няма свързване с Google, което тази заявка да прекрати.“ (M1): this
  request held no link that its own record could end. The session held no
  linking object, or the object had already been used, had expired or could
  not be accepted. It is a statement about the request, and it stays true
  whatever follows. It does not say that no link exists or that none can
  follow, and it says nothing about what the browser holds afterwards or
  about the account; the page below it shows the account's sign-in methods.
- „Профилът ти вече е свързан с Google.“ (M2): the account had a Google
  identity when the route read it.
- „Не успяхме да потвърдим отказа.“ (M3): the record could not be written,
  or the account could not be read. It says neither that the link can still
  be used nor that it cannot; 4.4 says what a copy of the cookie can still
  do when the record is missing.
- No message claims a cancellation that the server could not record or
  could not establish.

## 2. State kept in the signed session

### 2.1 The transaction

`session["oidc_tx"]`, a JSON object:

| Key | Value | Notes |
| --- | --- | --- |
| `v` | `1` | Layout version; anything else is malformed. |
| `intent` | `"signin"` or `"link"` | Set by the route, never by the client. |
| `state` | `secrets.token_urlsafe(32)` | 43 characters, 256 bits. |
| `nonce` | `secrets.token_urlsafe(32)` | 256 bits. |
| `cv` | `secrets.token_urlsafe(64)` | 86 characters; a valid PKCE verifier (43–128 unreserved characters [4]). |
| `next` | a validated local path or `""` | 7.2. Always `""` for `link`. |
| `inst` | the issuing service instance | 2.3. 32 hexadecimal characters. |
| `exp` | monotonic-clock seconds | 2.3, 2.4. A JSON integer: `type(...) is int`, from 0 to below 2^62, which is the storage's own bound for a time (section 9). Anything else is malformed. |
| `uid`, `skd`, `siat` | `link` only | 4.1. |

### 2.2 The pending link

`session["oidc_link"]`, written only by a `link` callback that validated an ID
token, and only for an account that may link:

| Key | Value | Bound |
| --- | --- | --- |
| `v` | `1` | |
| `id` | `secrets.token_urlsafe(32)` | 43 characters. The confirmation's single-use key is derived from it (4.3). |
| `inst`, `exp` | as in 2.1 | `exp` is 180 seconds after the callback. |
| `uid`, `skd`, `siat` | copied from the transaction | 4.1. |
| `sub` | Google's subject | 1–255 visible ASCII characters (5.4). |
| `email` | the address Google reported, or `""` | at most 254 characters (6.3). |
| `name` | the cleaned Google name, or `""` | at most 80 characters (6.4). |

The issuer is not stored; it is the constant of 5.5.

**Never in either object:** the client secret, the authorization code, the ID
token, the access token, any claim other than the three above. Once the
pending link exists, the state, nonce and verifier are gone with the
transaction.

The cookie is signed, not encrypted. Its holder can read the state, nonce and
verifier of a transaction, and the subject, email and name of a pending link
for the three minutes it lives. That is inherent in keeping this state in the
session (A1). The values do not help anyone who lacks the cookie, and the
client secret, which the code exchange also needs, never leaves the server.

### 2.3 Service instance and clock

- **Clock.** One function in `web/oauth_state.py`, `monotonic_seconds()`,
  returns `int(time.monotonic())`. It is the same code on every platform, and
  tests replace that one function. It is the only clock used for the expiry of
  a transaction or pending link, for accepting them, and for the consumption
  records. The wall clock is used for none of these. No other clock source,
  platform branch or stored clock state is added.
  - Python documents this clock as one that "cannot go backwards", is "not
    affected by system clock updates" and is "the same for all processes"
    [19]. On Linux it is `CLOCK_MONOTONIC`: the seconds the system has been
    running since boot, not counting time it was suspended [20].
- **Service instance.** `secrets.token_hex(16)`, drawn when
  `web/oauth_state.py` is first imported in a process. A transaction or
  pending link is accepted only by a process whose value equals its `inst`.
  The value exists to end everything outstanding when the service stops:
  after a restart, a reboot or a restore no process has the old value.
  Nothing about single use depends on how processes come to share it (3.7).

What a stop does:

- **Restart of the service.** A new instance; every outstanding transaction
  and pending link answers `expired`. The clock goes on.
- **Reboot.** A new instance, and the clock starts again near zero. `exp`
  values issued before the reboot mean nothing on the new clock, and they are
  never compared with it: the instance differs first.
- **Suspend.** On Linux the clock does not count time during which the system
  is suspended [20]. A transaction then lives longer in real time by the
  length of the suspension, and so does its record, so nothing is accepted
  twice. A production host is not suspended.

Why not the wall clock: E7 ran one randomised model twice. With a clock that
never goes back, no transaction was accepted twice (3744 accepted, 0 twice).
With a wall clock that was set two days ahead and then corrected, records were
deleted early and 769 of 6499 transactions were accepted a second time. A
longer retention only raises the size of the jump that is needed.

What it costs:

- A restart ends every sign-in and link in flight. The person sees `expired`
  and starts again.
- `exp` shows roughly the host's uptime to whoever holds the cookie.

### 2.4 Lifetimes

| Object | Lifetime | Why |
| --- | --- | --- |
| `signin` transaction | 600 s | A first Google sign-in on a school computer can include a password, a second step and a consent screen. |
| `link` transaction | 300 s | The person is already signed in. |
| pending link | 180 s | Typing one password. A shorter window narrows 4.6. |

- Accepted only while `now < exp` on the monotonic clock.
- A consumption record is kept at least until that clock passes `exp + 60`
  (`RETENTION_ALLOWANCE`, section 9). How much longer it can stay: 3.4,
  "Rows".
- Flask additionally refuses a session cookie whose signature is older than 31
  days. That is not a control for this flow.

### 2.5 Malformed input

| Input | Handling |
| --- | --- |
| `state` missing, repeated, wrong length or alphabet | no match (1.5 H3) |
| query string over 2048 bytes | no match |
| `oidc_tx` of the wrong type, version or shape | no match; not touched |
| `state` matches, `inst` is another instance | slot removed, `expired`, nothing recorded |
| `code` missing, repeated, empty, over 256 characters or not visible ASCII | `failed`; the transaction is already recorded as used |
| `error` together with `code` | treated as an error |
| unknown parameters (`scope`, `authuser`, `hd`, `prompt`) | ignored, never logged |
| `next` unsafe or over 512 characters at start | stored as `""` |
| `oidc_link` of the wrong type, version or shape | confirmation answers `expired`; a POST removes it |

### 2.6 Removal

- A matched callback removes the transaction from the session first (1.5 H7):
  on success, cancellation, provider error, validation failure, timeout and
  exception alike.
- Confirmation removes the pending link once its transaction has committed.
  After a rollback the pending link stays (1.6).
- Cancellation, a new link start, sign-out and every replacement of the
  session's account (a password sign-in, an admin's password step, a password
  change, a Google sign-in) remove both slots, and first try to retire the
  linking they held (4.4). The slots are removed whether or not the
  retirement was recorded. The session keeps only the attempt markers
  (`issue_account_session`, `web/blueprints/account.py:88`).
- A callback that does not match leaves the transaction.
- **Removing a slot from the response cookie revokes no earlier copy of the
  cookie.** What stops a copy is the consumption record (section 3), the
  instance and the expiry. Where a retirement could not write its record,
  only the instance and the expiry stop a copy, and for a pending link also
  the checks of the confirmation (4.4).
- **An empty slot is evidence of nothing.** A slot is empty after a completed
  link, after an expiry, after a cancellation, and after a cancellation that
  was not recorded. No answer is derived from a slot being empty (1.6).

### 2.7 Concurrent tabs

One transaction slot. A second start replaces the first transaction. The
earlier tab's callback then finds a different state: no match, `failed`, and
the newer flow is unaffected. Keeping several transactions would grow the
cookie with every attempt, which is what Authlib's own registry does (E1).

### 2.8 Cookie size

E1, re-measured for the layouts of 2.1 and 2.2 through Flask's real session
serializer (cookie value, without attributes):

| Session | Bytes |
| --- | --- |
| `signin` transaction, no `next` | 390 |
| the same with a 512-character random `next` (the maximum) | 918 |
| the same, plus 60 historical attempt markers | 1163 |
| `link` transaction beside an account session | 538 |
| the same, plus 300 markers | 1548 |
| pending link, typical values, beside an account session | 476 |
| pending link, every field at its bound with incompressible content | 1316 |
| the same, plus 300 markers | 2392 |

- Browsers accept at least 4096 bytes per cookie; Werkzeug warns above 4093.
- Budget: after writing a transaction or a pending link, serialize the session
  with the application's own signing serializer; if the value exceeds 3000
  bytes, remove what was just written and answer `failed`. The only unbounded
  content is historical attempt markers, which are no longer written.

## 3. Single use, replay and concurrency

Terms. The **start cookie** is the session cookie value the start response
set; it holds the transaction. The **callback** is one request to
`/auth/google/callback`. A **code** is Google's authorization code. A
**record** is a row of the consumption table.

### 3.1 Two kinds of single use

- **The provider's.** A code is redeemed once. RFC 6749 §4.1.2 requires the
  authorization server to deny a second use [10]; Google calls it "a one-time
  authorization code" [1]. *Not verified:* which error Google returns on
  reuse.
- **The application's.** A transaction is accepted once. This is enforced by
  the record written at 1.5 H7 and does not rely on Google: the second
  callback is refused before any request to Google is made.

Neither implies the other, and the design depends on the second only.

### 3.2 What the checks bind

| Check | Where | Binds |
| --- | --- | --- |
| `state` | application | the callback to the browser that holds the transaction |
| consumption record | application, SQLite | the transaction to one use |
| `inst` | application | the transaction to one run of the service |
| `exp` | application, monotonic clock | the transaction to 5 or 10 minutes |
| `nonce` in the ID token | application | the ID token to this transaction's authorization request |
| PKCE verifier | Google's token endpoint | the code to this transaction's authorization request |
| client secret | Google's token endpoint | the exchange to this server |
| `uid`, `skd`, `siat` | application | a link to one account session (4.1) |
| the account password | application | a link to a person who knows it (4.3) |
| `UNIQUE (issuer, subject)` | SQLite | one identity to one user, for good |

The nonce matters because it is checked by the application. A code from any
other authorization request produces an ID token with another nonce and is
refused, whether or not Google enforces PKCE for this client type. Google
documents PKCE for installed apps [4] and advertises `S256` in its discovery
document [2]. *Not verified:* that its token endpoint rejects a wrong verifier
for a web client.

### 3.3 Scenarios

| # | Scenario | Result | Decided by |
| --- | --- | --- | --- |
| 1 | The callback URL is requested again in the same browser (reload, back). | No match; nothing changes. | The cookie no longer holds the transaction. |
| 2 | **Sequential replay:** the start cookie is restored and the same callback is sent again. | Refused at 1.5 H7, before any outbound call. | The record. |
| 3 | **Simultaneous callbacks** with the same start cookie, same or different codes. | Exactly one passes 1.5 H7; every other is refused. | The record's primary key under SQLite's write lock (E11: 40 keys, 12 callers each, 40 accepted, 440 refused). |
| 4 | **Same transaction, different code:** the start cookie is restored after a completed sign-in and a fresh code is obtained from Google. | Refused at 1.5 H7. | The record. |
| 5 | An unused start cookie with a code from another authorization request. | The transaction is recorded as used; the ID token is then refused. | The nonce; PKCE at Google. |
| 6 | A callback carrying `error=…` for a matching transaction, then a second one with a code. | The first spends the transaction; the second is refused. | The record, written before the `error` branch. |
| 7 | Any use after `exp`. | Refused as `Expired`, before any cleanup runs. | The monotonic clock, read under the write lock. |
| 8 | Any use after a service restart, a reboot or a restore. | Refused at 1.5 H6. | The instance. |
| 9 | Two browsers sign in the same new Google identity at once (two transactions). | One user, one identity; both are signed in to it. | `BEGIN IMMEDIATE` and `UNIQUE (issuer, subject)`; E3. |
| 10 | Two accounts confirm the same Google identity at once. | The first commit wins; the second gets `identity_taken`. Nothing is reassigned. | `UNIQUE (issuer, subject)`. |
| 11 | A first sign-in and a link confirmation of the same new identity at once. | Whichever commits first decides: the identity belongs to the confirming account or to a new student. The second case needs the recovery tooling of section 10. | The same constraint. |
| 12 | A forged link from another site to the callback, with any parameters. | No match; no cookie, no row, no outbound request. | `state`. |
| 13 | **Duplicate confirmation:** the confirm POST is sent twice, or a copy of the cookie is confirmed after the link was made. | One identity row. The other request is told `already_linked` and stores no identity and no record. | The confirmation's own record, committed with the identity (4.3; E7: 8 simultaneous confirmations, 1 linked, 7 refused). |
| 14 | A copy of the cookie with a pending link, after a cancellation or a sign-out whose retirement invalidated that pending link. | Refused. | The confirmation's record, written by the retirement (4.4). Row 18 is the case without a record; row 21 is a pending link that the session never held. |
| 15 | A copy of the cookie with a pending link, after the bound account's session key rotated. | Refused. | `skd` against the row's current key, inside the write transaction. |
| 16 | A copy of the cookie with a pending link, inside its 180 seconds, after another account signed in on that browser without a sign-out. | Refused, when the retirement was recorded. | The confirmation's record: replacing the session's account retires the link first (4.4). Row 18 is the other case. |
| 17 | **Two service instances on one database** (several workers that do not share an instance, or an old and a new process side by side). One accepts a transaction, the other accepts a different one, and the first transaction is replayed. | Refused. | The record. Rows are deleted by expiry only, never because another instance wrote them (E11: the replay was accepted under revision 2's rule and refused under this one). |
| 18 | **A retirement that was not recorded** (the database was busy or failing, or the caller had a transaction open), and then a copy of the earlier cookie: after the cancellation, the sign-out, the new link or the change of account. | Not refused by any record. A pending link can still be confirmed until its `exp`, with the bound account's current password and on that account's unchanged session key. A link transaction can still reach a pending link until its `exp`, and that pending link is confirmed only under the same conditions. The cancellation did not answer `link_cancelled`: its row is 3, and it answered `cancel_unconfirmed` (M3). | The password, the binding and the expiry (4.3). Nothing the retirement did (E15). |
| 19 | **Two tabs.** Tab 1 confirms with the password: the identity is stored and the pending link leaves the session. Tab 2 still shows the confirmation page and presses „Отказ“. The session holds no slot. | The link stands. The retirement finds nothing (`none`). The cancel route reads the account, finds the Google identity and answers `account_linked` (row 2, M2). **Never `link_cancelled`.** | The read of the account (1.6 step 3). An empty slot decides nothing (E16). |
| 20 | **A stale pending link is cancelled after it was confirmed.** The cookie that reaches the cancel route still holds the pending link: the confirmation's response did not reach the browser, or both requests were sent at once and the confirmation committed first. | The link stands. The pending link is `consumed`, not cancelled, and its slot is removed. The answer is `account_linked` (row 2, M2). If the confirmation had recorded the use and stored nothing (a refusal at 4.3 steps 2–5), the account has no identity and the answer is `link_not_pending` (row 5, M1). **Never `link_cancelled`.** | The confirmation's record, met as `AlreadyConsumed`; the read of the account (E16). |
| 21 | **A copy of the start cookie reaches the callback first; then the original cancels** (or signs out, starts another link, or changes account). The copy's callback was accepted and its response holds a pending link. The original session still holds the transaction. | The transaction is `consumed`; the original's slots are removed. The cancel route answers `link_not_pending` (row 5, M1), or `account_linked` (row 2, M2) once that pending link has been confirmed. **Never `link_cancelled`.** The pending link in the copy is **not invalidated**: the original session does not hold it, cannot name its `id`, and the retirement of the transaction it came from does not reach it. It stays subject to every check it had: it is confirmed only from a cookie that holds it and whose bound session still loads, with the bound account's current password under the throttle, on the instance that issued it, before its own `exp` (180 seconds after that callback), on an active, non-admin account whose session key is unchanged, and with a link decision that allows it. The same holds when the original's transaction has expired by then and is reported `dead`. | The callback's record tells the original that the transaction was used, and no more. The successor is held by its own checks (4.3), not by anything a retirement does (E16). |
| 22 | **The account cannot be read at a cancellation.** The retirement returned, with any outcomes, and the read of 1.6 step 3 fails. | The slots are removed: that happened before the read. The answer is `cancel_unconfirmed` (row 1, M3). **Never `link_cancelled`**, also when the retirement invalidated the object. | 1.6 step 4, row 1 (E16). |

### 3.4 Guarantees and limits

**A transaction**

- is accepted at most once. The first matched callback commits a record
  before it looks at `error`, `code` or `iss` and before any request to
  Google; every later callback for the same state is refused, whether it comes
  from the same browser, from a copy of the cookie, at the same moment, or
  through another process. The argument, and what it assumes of the callers,
  is `OIDC_CONSUMPTION_SCOPE.md` §5 (merged); section 9 shows that PR #331
  meets it;
- is accepted only within its lifetime on a clock that wall-clock corrections
  do not move, and only by the instance that issued it;
- once accepted, completes only with an ID token Google issued for that
  transaction's authorization request (nonce), for this client (audience),
  signed by Google's current keys.

**A pending link**

- can exist only as the result of an accepted `link` transaction and a
  validated ID token;
- is stored as an identity at most once, by a request that presents the bound
  account's session and its current password, while the account is active, not
  an admin, and still has the session key the link was bound to;
- the record of the callback does **not** make the confirmation single-use: it
  says only that the callback ran once. The pending link is a new object in
  the cookie and can be copied. Its own record, keyed by `id` and committed in
  the same transaction as the identity row, is what makes it single-use;
  `UNIQUE (issuer, subject)` and "one Google identity per account" make a
  duplicate harmless even where a record is missing.

**Limits**

1. Use is recorded before the outcome is known. A cancellation at Google, a
   provider error, a timeout or a validation failure also spends the
   transaction.
2. The records are in the application database. If the database were rolled
   back or replaced **while the service keeps running**, records written since
   would be lost without the instance changing, and a transaction used in that
   interval could be accepted once more within its remaining lifetime. The
   supported restore stops the service first (3.6).
3. One clock per database. Every process that serves the database reads the
   same monotonic clock: one host, one boot, no separate time namespace
   (`OIDC_CONSUMPTION_SCOPE.md` §5, limit 3). The deployment has one host.
4. Removing a slot from a response cookie invalidates no earlier copy. Nothing
   here claims that it does.
5. Sign-out on one browser does not end the account's session elsewhere or in
   a copy of the cookie (`docs/ACCOUNTS_DESIGN.md` §22). It ends linking in
   progress in that browser's session, and in a copy of the cookie only when
   the retirement was recorded (4.4).
6. A retirement that cannot be recorded ends nothing on the server. The
   object stays usable in a copy of the cookie until it expires, within the
   bounds of 4.4. Cancellation reports that case; sign-out, a new link and a
   change of account log it and go on (3.3 row 18).
7. Records are not all gone after eleven minutes. Rows of earlier boots can
   accumulate, and they are harmless only because 1.5 H6, 4.2 and 4.4 refuse
   every object of another run before the storage is asked ("Rows" below).
8. A retirement reaches only the objects that the session holds. A pending
   link that a callback created for another copy of the cookie is unknown to
   this session, and retiring the transaction it came from does not
   invalidate it (3.3 row 21). The server keeps no relation between a
   transaction and what it produced: the consumption table stores keyed
   hashes and no transaction (A1), so nothing in this design can follow one
   to the other. Such a pending link ends with its own expiry and stays
   behind its own checks (4.3). A cancellation therefore never reports more
   than its own record and the account's identities establish (1.6).

**Outbound calls.** One transaction allows one code exchange. A transaction
needs a `POST /auth/google/start`, which Nginx limits per address (8.3).

**Rows.** The consumption table grows by one small row for each matched
callback, each confirmation whose `consume` returned, and each retired
object. Within one boot a row leaves at the first consumption after the
clock has passed its expiry plus the 60-second allowance; for a sign-in
transaction that is eleven minutes after its start. The table is still
**not** bounded to the traffic of a few minutes. A reboot restarts the clock
near zero, the rows of the earlier boot stay until the new clock has passed
their stored expiry plus the allowance, and repeated reboots accumulate the
rows of several boots. The merged documentation is authoritative for this:
`docs/ACCOUNTS_DESIGN.md` §5 ("Records that outlive their boot") and
`docs/DEPLOYMENT.md` §28 ("Rows, and who removes them"). This design adds no
deletion, job or index for those rows, and none is to be added.

### 3.5 Clock faults

- Transaction and pending-link lifetimes and record retention are on the
  monotonic clock (2.3). A wall clock that is set forward, set back or wrong
  for hours changes none of them. Suspend, restart and reboot: 2.3.
- The wall clock still decides ID-token `exp` and `iat` (5.4). A token is
  accepted only while the server's clock is no more than 60 seconds behind and
  no more than 11 minutes ahead of Google's. Outside that window every sign-in
  fails closed.
- The absolute lifetime of the linking account's session (`siat`) is on the
  wall clock, as for every session today.
- The lifecycle clock's `suspect` state (`docs/DEPLOYMENT.md` §26) neither
  blocks Google sign-in nor is touched by it. A sign-in writes nothing to
  `lifecycle_clock`, which the restore's reconciliation relies on.

### 3.6 Restore

A snapshot holds the consumption table as it was when the snapshot was taken.
Records written after that are not in it.

- A restore is carried out only by `deploy/learnpilot-restore.sh`
  (`docs/DEPLOYMENT.md` §17). It stops the service (step 2), holds a lock that
  keeps the service from starting (also enforced by `web.wsgi` and by
  `ConditionPathExists=` in the unit, `deploy/systemd/learnpilot.service:13`),
  and starts the service only as its last step (step 11).
- The service that starts is a new instance. Every transaction and pending
  link issued before the restore names the old one and is refused at 1.5 H6
  or by the first validation of 4.2, before any record is consulted. A lost
  record therefore cannot matter, and no operator step is needed to make that
  so.
- The restored table's rows are inert for one reason: their keys can only
  come from objects of an earlier run, and the instance check refuses those
  before the storage is asked. The storage does not make them inert by
  itself. They leave the table by expiry like any other row, which after a
  reboot can take long (3.4, "Rows").
- This does not cover replacing the database under a running service (limit 2
  of 3.4). The runbook forbids doing the script's steps by hand.

What a restore does to accounts is not changed by this design: users,
identities and session keys return to the snapshot. A student onboarded after
the snapshot is onboarded again as a new account at the next sign-in, and a
link confirmed after it is gone. §27 of the deployment guide already states
that the snapshot and the restore cover `user_identities` unchanged.

### 3.7 Workers and instances

Production runs one sync worker, loaded before the fork
(`deploy/gunicorn.conf.py:17`, `:27`). **Single use relies on none of that:**
not on the number of workers, not on `preload_app`, and not on when a module
is imported. One reporting statement does rely on it, and is the third item
here.

- Single use is decided by a primary key under SQLite's write lock, with a
  clock that every process of the host shares. It holds for any number of
  processes and instances that write to the database (3.3 row 17).
- The instance value only narrows who may accept: a transaction is refused by
  every process whose value differs from its `inst`.
- **What relies on one instance per run.** `retire_linking` reports an
  object of another instance as `dead` and does not record it (4.4), because
  an object of another run must be refused before the storage is asked
  (section 9, obligation 4). That `dead` means "no process accepts it" is
  exact when no process with the other value still serves the database:
  after a restart, a reboot or a restore, and always in the documented
  deployment, where one preloaded application is the only instance of a run
  (8.7, "One instance per run").
- **Two instance values on one database at the same time.** This takes
  workers that were not preloaded, or two Gunicorn masters: a second service
  started beside the unit, or Gunicorn's `USR2` re-execution, which starts a
  new master while the old one still runs [18]. Neither is part of the
  documented deployment. What it would do, and what it would not:
  - single use holds (3.3 row 17);
  - an object is accepted only by the instance that issued it, so a callback
    or a confirmation that reaches the other one answers `expired`;
  - a retirement that reaches the other instance reports the object `dead`,
    records nothing and logs nothing, while the instance that issued it
    still accepts it until its `exp`, within the bounds of 4.4. That `dead`
    is then not exact;
  - no page claims a cancellation because of it: a `dead` object never gives
    `link_cancelled` (1.6).

  The design adds nothing for this case: no shared instance value, no record
  across instances, no check at start-up. It states the requirement (8.7),
  the deployment test pins `preload_app`, and one test characterises the
  limit (test plan B5).

What does depend on the deployment is availability. Workers share one instance
value only when the application is loaded before the fork. Without that, a
callback that reaches another worker than its start answers `expired`. Raising
the worker count therefore needs `preload_app` to stay on, and the deployment
test pins it as an availability requirement.

Three structures are per process and are not security state: the key cache
(5.6), the slots (5.7) and the cooldown (5.8). One lock guards them (5.7).

## 4. Linking

### 4.1 Binding

A `link` transaction, and the pending link made from it, store:

- `uid`: the account's id;
- `skd`: `HMAC-SHA256(SECRET_KEY, "oidc-link\0" + session_key)` in hex, a
  digest so that the key is not in the cookie twice;
- `siat`: the `iat` of the session that started the link.

### 4.2 Three validations

**First validation** (callback step V4; `GET` and `POST` of the confirmation):

- a signed-in principal exists (`load_current_user` has checked `uid`, `sk`,
  active state and the role's absolute lifetime);
- its role is `student` or `teacher`;
- `principal.user_id == uid`, `session["iat"] == siat`, and the digest of
  `session["sk"]` equals `skd` in constant time;
- for a pending link also: well-formed, `inst` is this instance, `now < exp`.
  This is a first look, for the page. The expiry that binds is the one checked
  under the write lock (4.3 step 1).

**Second validation** (callback step V7, after the code exchange and the
ID-token checks). The first validation used the account as it was loaded when
the request began, and the exchange can take up to the network budget. Two
read-only checks follow it, in this order:

1. *The account, read again.* One function, `bound_account(conn, uid, skd,
   siat, *, secret_key, wall_now)` in `web/services/oidc_accounts.py`, reads
   the `users` row by `uid`. It refuses when the row is missing, inactive or
   an `admin`; when the digest of the row's **current** `session_key`
   differs from `skd`; and when `accounts.session_expired(row.role, siat,
   wall_now)`. These are steps 2–4 of 4.3, which calls the same function. A
   refusal ends the callback as `failed`, and no pending link is created. A
   password change, a disable, a role change or an operator `sign-out` that
   happens while the callback waits for Google is caught here.
2. *The link decision*, below.

**The link decision.** One read-only function, `link_decision(conn, uid,
identity)` in `web/services/oidc_accounts.py`, used at two points: by the
callback after the ID token is verified (step V7), so that nobody is asked
for a password that cannot lead anywhere, and inside the write transaction
(4.3 step 5). Because both call the one function, both run the same checks in
the same order. The identity comes first, as it does at sign-in (6.1 step 1):

1. `find_identity(issuer, subject)` finds this account → `already_linked`;
   another account → `identity_taken`.
2. The account has no password, or already has a Google identity →
   `not_allowed`. The schema allows several identities per user; this release
   allows one Google identity per account.
3. *(P1.)* The email Google reported, if comparable (6.3), equals
   the `users.email` of a **different** account → `email_other_account`.
4. Otherwise the link may be made.

At the callback, outcomes 1–3 create no pending link, and passing either
check of the second validation decides nothing: the write transaction asks
both again, under the write lock.

**The write transaction** (4.3) is the third.

### 4.3 The write transaction

`confirm_link(conn, pending, *, secret_key, session_key, session_iat, now,
wall_now)` in `web/services/oidc_accounts.py`. The confirm POST calls it after
the password was verified, inside the transaction of 1.6 step 5: the write
lock is held, and `now` was read after it was obtained. Under that lock a
concurrent change by another worker or by the operator CLI is either complete
before it or waits for it. The function returns an outcome and neither
commits nor rolls back.

1. `consume` with the key of purpose `link-confirm` derived from the pending
   link's `id`, with `now`, and with the pending link's own `exp` as
   `expires_at`.
   - `Expired` (`now >= exp`): outcome `expired`.
   - `AlreadyConsumed`: outcome `already_linked` if `find_identity` shows this
     identity on this account, otherwise `failed`.

   Neither stores a record or an identity. That is not the same as writing
   nothing. The caller's transaction already holds the cleared throttle
   counter (1.6 step 5), and after `AlreadyConsumed` it also holds the
   storage's cleanup of expired records, which ran before the refused
   insert (section 9). The caller commits both. After `Expired` the storage
   executed no statement.
2. Read the `users` row by `uid`. Missing, inactive or role `admin`: `failed`.
3. The digest of the row's **current** `session_key` differs from `skd`:
   `failed`. This is what a password change, a disable, a role change, a
   `sign-out` or a TOTP reset during the flow leads to.
4. `accounts.session_expired(row.role, siat, wall_now)`: `failed`.

   Steps 2–4 are `bound_account` (4.2), the function the callback calls
   after the exchange.
5. The link decision (4.2). Any outcome other than "may be made" ends here
   with that outcome.
6. `identities.add_identity(conn, uid, issuer=…, subject=…,
   provider="google", email_at_link=…)`; outcome `linked`.

The caller commits after every outcome (1.6 step 5). From step 2 on, the
record of step 1 therefore persists, and the pending link is spent whether or
not an identity was stored. An exception rolls everything back, the record
included; a second attempt then goes through the same steps, and the pending
link is still in the session for it (1.6). E7: a failure forced between the
record and the identity left neither.

The password is verified before the transaction, not inside it, so that the
write lock is not held during scrypt. A password changed in between rotates
the session key and is caught by step 3.

The session cookie is rewritten only to drop the pending link. A link changes
no session key and issues no session.

### 4.4 Retiring, and events while a link is in progress

**Retiring.** One function, `retire_linking()` in `web/oauth_state.py`, tries
to record on the server that the linking in progress in this session has
ended, and **returns what it achieved.** It covers both objects a session can
hold: a transaction of intent `link` (linking in flight: the browser is at
Google) and a pending link (the browser is on the confirmation page). A
`signin` transaction is not linking and is not recorded; it goes with its
slot.

**The result.** One outcome for each of the two objects, `transaction` and
`pending`:

| Outcome | Meaning | What it establishes |
| --- | --- | --- |
| `none` | The session held no such object. | Nothing. A slot is empty after a completed link, after an expiry and after an earlier retirement, recorded or not (2.6). |
| `invalidated` | This call's record of the object is committed: `consume` returned **and** the `COMMIT` succeeded. | The object **had not been used**, and it is refused from now on, in this browser and in every copy of a cookie that holds it. |
| `consumed` | `consume` raised `AlreadyConsumed`: a committed record of the object existed before this call. | The object **was used**, and it is refused. By what it was used, and what that use produced, this call does not know. |
| `dead` | The object cannot be accepted any more, for a reason that involves no record: it is malformed, it names another instance, or it has expired. | It is refused, for that reason. Whether it was used before is not known. |
| `not_recorded` | The object is live as far as this process can tell, and no record of it was committed. | **Nothing that this call did refuses it.** It stays usable until its `exp`, within the bounds stated below. |

**`invalidated` proves that the object had not been used.** A use writes the
same key: the callback and the confirmation derive it from the same value
with the same purpose as the retirement (section 9). That row is kept at
least until the clock has passed the object's `exp` plus the allowance, and
the retirement inserted while `now < exp`. An earlier use would therefore
have been met as `AlreadyConsumed`. This rests on what single use rests on:
one clock, and nothing else deleting rows (section 9, obligations 2 and 8).

**`consumed` proves a use, not a cancellation.** The earlier record was
written by one of three things, and `consume` does not say which:

- a confirmation (for a pending link). It stored the identity, or it recorded
  the use and then refused at 4.3 steps 2–5 and stored nothing;
- a callback (for a transaction). It was made with this cookie or with a
  copy of it, and it may have written a pending link into the response to
  whoever made it;
- an earlier retirement of the same object, whose response did not reach
  this browser.

**`dead` has exactly three causes,** and nothing else is reported as `dead`:

- the object is not well-formed (2.1, 2.2): no code path accepts it;
- it names another service instance: this process refuses it. It is not
  recorded, because an object of another run must be refused before the
  storage is asked (section 9, obligation 4). 3.7 says when "no process
  accepts it" is exact;
- `consume` raised `Expired`: the clock, read under the write lock, has
  reached the object's `exp`. The storage raises `Expired` before it
  executes any statement (section 9), so an expired object that was used
  before its expiry is reported `dead` as well. For a transaction that
  matters: the pending link made from it expires up to 180 seconds later
  than the transaction does (E16).

**A failure is never a retirement.** Every error maps to `not_recorded`: a
pending transaction of the caller, a connection that cannot be opened, a busy
database, a missing table, a read-only database or connection, a failed
statement, a failed `COMMIT`, the storage's `ValueError` and `RuntimeError`,
and any other exception. Nothing is read as "probably recorded". On a Python
older than 3.11 the storage reports a repeated key as a plain
`sqlite3.IntegrityError` (section 9); retirement then reports `not_recorded`
for an object that was in fact used, which errs towards claiming less.

The result has two derived values. Each is a statement about records and
about nothing else:

- `complete`: no outcome is `not_recorded`. It says that this call left no
  object that it found both live and unrecorded. **It does not say that
  linking was cancelled, that nothing was linked, or that no other linking
  object exists.** An empty session is `complete`; so is one whose object
  had been used to complete the link.
- `all_invalidated`: the session held at least one linking object, and every
  object it held is `invalidated`. Only the cancel route reads it (1.6).

**Steps**

1. Provider disabled: the slots are not read, and the result is `none` for
   both (1.8). Nothing is claimed by that: no object can be accepted while
   the provider is disabled, and enabling it takes a restart, which is a new
   instance.
2. Classify each slot without the database: `none` (empty, or a `signin`
   transaction), `dead` (malformed, or another instance), or **live**. If
   nothing is live, go to step 8. The database is not opened.
3. **Precondition: no pending transaction of the caller.** Take the request's
   connection. If `conn.in_transaction` is true, retirement executes **no
   statement on it**: no `BEGIN`, no `consume`, no `COMMIT` and no
   `ROLLBACK`. Every live object is `not_recorded`, the reason logged is
   `caller_transaction`, and the caller's pending writes are exactly as they
   were, for the caller to commit or to roll back. Go to step 8.
4. `begin_write(conn)`: `BEGIN IMMEDIATE`, and only after that statement has
   returned, one reading of the clock (section 9). The transaction that is
   open from then on is retirement's own. Before that statement has returned,
   retirement owns no transaction.
5. For each live object, the transaction first and the pending link second:
   `record_use`, with purpose `callback` for a transaction and `link-confirm`
   for a pending link, the object's own `exp`, and the `now` of step 4.
   - It returns: the record is pending in retirement's transaction.
   - `Expired`: the object is `dead`. `AlreadyConsumed`: the object is
     `consumed`. The transaction is still usable after either (section 9),
     and the loop goes on.
6. `COMMIT`. When it has returned, every object whose record was pending is
   `invalidated`.
7. **Failure:** an exception while the connection is taken in step 3, or in
   step 4, in step 5 (other than the two refusals) or in step 6.
   - If, and only if, the `BEGIN IMMEDIATE` of step 4 had returned,
     retirement rolls back. What it rolls back is its own transaction: step 3
     established that the caller had none. If `BEGIN IMMEDIATE` itself
     failed, no transaction was begun, no rollback is issued, and the clock
     was not read. That is the case for a write lock that is held elsewhere
     past the busy timeout and for a `query_only` connection (E16, E19).
   - **A database file that cannot be written is the other case** in
     rollback-journal mode. `BEGIN IMMEDIATE` returns, so retirement owns a
     transaction and has read the clock once. The first write, the cleanup
     `DELETE` inside `consume`, then raises, and retirement rolls its own
     transaction back once (E19). Which of the two a read-only database
     produces depends on the journal mode and on the SQLite version
     (section 9, "Ownership"). The outcome is `not_recorded` in both, and
     the rule needs no knowledge of which it was.
   - Every object that is neither `dead` nor `consumed` is `not_recorded`.
     **That includes an object whose `consume` had already returned:** its
     record was pending in the transaction that did not commit.
   - One log line (below). The exception is not passed on to the caller,
     except one that is not an `Exception`, such as the `SystemExit` of a
     worker abort: that one is raised again after the rollback.
   - A rollback that fails is caught and logged in the same way and changes
     no outcome. Whatever is then still open on the connection is
     retirement's own. Retirement executes nothing further on it, and it is
     discarded when the request's connection is closed (`web/app.py:494`;
     section 9, "Ending a transaction that a call site began").
8. Remove both slots from the session, on every path through steps 2–7 that
   returns.
9. Return the result.

**Several objects.** Both records are written in one transaction, so the
writes of one call are all or nothing: never one record committed and the
other not. A failure after the first `consume` returned therefore leaves the
first object `not_recorded` as well (E15: a failure forced on the second
insert left no row, and a copy of either object was accepted afterwards). A
finding of `dead` or `consumed` stands whatever happens later in the call,
because it does not depend on the call's write. A result can therefore be
mixed: `dead` or `consumed` beside `invalidated`, or beside `not_recorded`.
It is never `invalidated` beside `not_recorded`. In the flows of this design
a session holds at most one linking object at a time: a link start empties
both slots before it writes a transaction (1.4), and the callback removes
the transaction before it writes a pending link (1.5). The function handles
two all the same, and the tests give it two.

**A failed `COMMIT`.** SQLite leaves the transaction open after some failures
of `COMMIT` (a busy database) and has already rolled it back after others (a
full disk, an I/O error) [23]. Retirement does not tell these apart and does
not retry. It calls the connection's `rollback()`, which does nothing when no
transaction is open [22], and reports `not_recorded`. If a record did reach
the disk in spite of the error, the object is refused by it and the report
claimed less than was true; the opposite mistake cannot happen.

**Cost.** A busy database can hold a retiring request for the connection's
busy timeout, five seconds (`web/db.py:29`), while it waits for the write
lock. Retirement takes the request's connection only when the session holds
a live linking object, so a sign-in or a sign-out without one is unaffected.

**The log line.** One line on `learnpilot.oidc`, at WARNING, for a call that
left anything `not_recorded`: the fixed word `retire_not_recorded`, which
objects (`transaction`, `pending`), the reason (`caller_transaction` or an
exception's class name), and the local user id when one is known. No key,
state, id or exception text (8.4). `caller_transaction` is a defect in the
calling code; every other reason is the database's.

**Callers, and what each does with the result**

| Caller | Then |
| --- | --- |
| `POST /auth/google/link/cancel` | Reads the account's identities and answers by the table of 1.6. `link_cancelled` only when the result is `all_invalidated` and the account had no Google identity at that read. When anything is `not_recorded`, or the account cannot be read, never `link_cancelled`: the answer is `cancel_unconfirmed` (M3), or `account_linked` (M2) when the account was read and has a Google identity. |
| `POST /auth/google/link`, before the new transaction is created | Goes on (1.4). Refusing would change nothing: the slots are empty, so a second press would find nothing to retire and go through. |
| `end_session` (sign-out) | Goes on: clears the session and redirects as always (1.7). |
| `issue_account_session` (password sign-in, completed second factor, password change, Google sign-in) and `issue_pending_second_factor` (an admin's password step) | Go on: the session is replaced. A sign-in or a password change must not fail, or be undone, because a record could not be written. |

The last two rows are the three functions that replace or clear a session,
all in `web/account_session.py` (7.1). These are today's three
`session.clear()` calls (`web/blueprints/account.py:96`, `:111`, `:302`); no
other code clears the session, and a test pins that. None of the three
branches on the result, and none shows anything to the person. Only the
cancellation reports, because only its message is a statement about the
link. The other three rows say "go on" for every result, `consumed` and
`dead` included: a sign-out, a new link and a change of account claim
nothing about the link they found.

**The precondition at the call sites.** Each of today's call sites has
committed or rolled back its own work before it calls a session function:
`web/blueprints/account.py:223`–`224` (password sign-in), `:164`–`165` (an
admin's password step), `:279`–`293` (second factor) and `:338`–`343`
(password change); sign-out (`:302`) writes nothing before it. A Google
sign-in calls it after its commit (7.1). The request's connection begins a
transaction only at a writing statement or an explicit `BEGIN` [22], so
after a commit, or after reads alone, it has none (E15). The precondition
therefore holds at every call site of this design, and a test pins it for
each. Step 3 is what keeps a later mistake from costing a caller its writes.
Revision 2.1's rule, "a `sqlite3.Error` anywhere: roll back", did not: inside
an open transaction `BEGIN IMMEDIATE` fails, and the rollback that followed
discarded the caller's pending write (E15).

The loader that drops a stale identity from the cookie (`web/app.py:356`) is
not one of them and retires nothing. It acts only when the bound session is
already invalid (a rotated key, an expired or disabled account), and steps
2–4 of 4.3 refuse exactly those cases for every copy of the cookie.

**What may be claimed.** A claim follows from an outcome, object by object,
and from nothing else.

- An `invalidated` object is refused in this browser and in every copy of a
  cookie that holds **that object**. This, and no more, is how the scope's
  requirement that cancellation, sign-out and account switching invalidate a
  link is met on the server: the linking objects that the session held are
  refused wherever they are presented.
- A `consumed` or `dead` object is refused as well, though not by this call,
  and this call does not know what became of it.
- A `not_recorded` object is ended for this browser only (below).
- `none` establishes nothing.

Revision 2.2 said that with a `complete` result "linking has ended on the
server, for this browser and for every copy of its earlier cookie". That
sentence is withdrawn. Three things lie outside every result, `complete` and
`all_invalidated` included:

1. **An identity that is already stored.** A retirement reads no identity.
   The cancel route does (1.6 step 3), once: an identity that another
   session of the account stores after that read is outside its answer
   (1.6, "Two limits", 2). Sign-out, a new link and a change of account
   read none, and they claim nothing.
2. **A successor: a pending link that a callback created from the
   transaction.** The callback was accepted before this call. It wrote the
   pending link into its own response, before this call or after it, to
   whoever made that callback: this browser, or the holder of a copy of the
   start cookie. When it is this browser and that response comes after the
   cancellation's, the browser holds a pending link again (1.6, "Two
   limits", 1). Its `id` is random and exists only in that
   cookie, and the server keeps no relation between a transaction and what
   it produced (3.4 limit 8). **Retiring the parent transaction cannot
   invalidate it.** The retirement meets `AlreadyConsumed` and reports
   `consumed`, or `dead` if the transaction has expired meanwhile, and that
   is all this session learns (E16: the transaction was reported `consumed`,
   and the storage then accepted the successor's own key). The successor is
   not thereby unguarded. It is confirmed only under the checks listed below
   for a pending link, and every one of them is unchanged.
3. **An object of an earlier attempt of this session whose own retirement
   was not recorded.** A link start empties both slots whether or not its
   retirement was recorded (1.4). A later cancellation finds, and
   invalidates, the newer object only. The older one is 3.3 row 18.

**When an object is `not_recorded`,** it is ended for this browser only, and
the design says so instead of claiming more:

- **This browser.** The slots are gone from the cookie that the response
  sets. That is all that removing them does. It revokes no copy of the
  earlier cookie, and nothing in this design says that it does (2.6).
- **A copy of the earlier cookie that holds a pending link** stays usable
  until the pending link's `exp`, at most 180 seconds after its callback.
  Confirming it still takes all of the following, each at the stage where it
  is checked (1.6, 4.2, 4.3):
  - the copy itself, on a same-origin POST. It carries the bound account's
    session, and that session must still load: the account active, its
    session key unchanged, its absolute lifetime not over;
  - in the route's first validation, before the password is looked at: the
    role is `student` or `teacher`; the pending link is bound to that very
    session (`uid`, `siat`, and the digest of the session key against
    `skd`); **it was issued by this run of the service** (`inst` is this
    instance); and `now < exp`, as a first look;
  - the bound account's **current password**, under the account's throttle
    counter;
  - inside the write transaction: `now < exp` on the clock read under the
    write lock, the account active and not an admin, its current session key
    the one the link was bound to, the bound session within its absolute
    lifetime, and a link decision that allows it.

  If all of that holds, the Google identity named in that pending link is
  stored on the bound account, as if nothing had been cancelled. The same
  list is what stands between a successor (item 2 above) and an identity.
- **A copy of the earlier cookie that holds a link transaction** stays usable
  until the transaction's `exp`, at most 300 seconds after its start, for one
  callback with its state and a Google authentication. That yields a pending
  link in the response to that request, and nothing else. Confirming it
  takes everything in the previous item.
- **What ends both at once, for every copy,** with or without a record, and
  a successor with them: a password change, an operator `sign-out`, a
  disable or a role change of the bound account (its session key rotates;
  4.3 step 3), a restart of the service (the instance), and the expiry.

`not_recorded` says that a record is missing. It does not say that a copy of
the cookie exists.

**Events.** "Transaction" is the time at Google; "pending" is the time on the
confirmation page. "Recorded" means that the object's outcome is
`invalidated`, and "not recorded" that it is `not_recorded`.

| Event | Transaction | Pending link |
| --- | --- | --- |
| Confirmation with the right password | — | Stored once; record written with it. |
| Confirmation rolled back by an error, or whose `COMMIT` failed | — | Nothing is claimed to be recorded. It stays in the session and can be submitted again until it expires (1.6). |
| „Отказ“ | Retirement attempted; the slot is removed in every case; then as in the next column. The pages offer „Отказ“ only for a pending link, but the route retires a transaction as well. | Retirement attempted; the slot is removed in every case; the account is read. Recorded, and no Google identity on the account at that read: `link_cancelled`, and a copy of the cookie is refused. A Google identity on the account: `account_linked` (M2). `consumed`, `dead` or nothing found: `link_not_pending` (M1). Not recorded, or the account could not be read: `cancel_unconfirmed` (M3), and after a retirement that was not recorded a copy stays usable as stated above. |
| A callback made with a copy of the start cookie, before any event below | Used. A later retirement in this session reports `consumed`, or `dead` after its expiry. | A pending link now exists in that copy. Nothing in this session can end it, and its own checks hold (3.3 row 21). |
| Cancellation on Google's screen | `cancelled`; already recorded as used. | Never created. |
| Sign-out in this browser | Retirement attempted, then the session is cleared in every case. Recorded: a copy of the start cookie is refused at the callback. Not recorded: as stated above. | Retirement attempted, then the session is cleared in every case. Recorded: a copy of the cookie is refused. Not recorded: as stated above. |
| Starting another link | Retirement attempted, then replaced in every case. | Retirement attempted; removed in every case. |
| Another account signs in on this browser (password form, an admin's password step) | Retirement attempted, then the session is replaced in every case. | The same. Recorded: a copy of the cookie is refused (3.3 row 16). Not recorded: 3.3 row 18. |
| Password change, `sign-out`, disable, role change, TOTP reset of the bound account | The key rotates. The first validation fails, or the second if it happens during the exchange (4.2). | The key rotates; step 3 of 4.3 fails, for a copy of the cookie too, with or without a record. |
| The bound session reaches its absolute lifetime | The principal is gone. | Step 4 of 4.3. |
| Promotion to admin | Rotates the key; the role check refuses as well. | The same. |
| Service restart, reboot, restore | Refused (instance). | Refused (instance). |
| Provider disabled between the steps | 404. | 404. |
| Abandonment: the browser is left on Google's page or on the confirmation page | Expires after 300 s. | Expires after 180 s. See 4.6. |

### 4.5 Sign-in is never turned into linking

- Intent is fixed at the start by the route and travels inside the signed
  transaction. No parameter, header or session state changes it afterwards.
- A `signin` transaction is created only for an anonymous visitor (1.3).
- A `signin` callback that finds a signed-in account fails. It does not link,
  does not switch accounts and does not touch the session.
- A `link` callback without its bound principal fails. It does not fall back
  to a sign-in.
- A `link` callback never writes an identity. Only the confirm POST does, and
  only with the password.
- A sign-in whose Google email equals an existing account's email creates and
  links nothing (6.3).

### 4.6 Shared computers, and what remains

**A staged link.** Someone signs in to their own account, starts linking and
leaves the browser on Google's page. The next person authenticates to Google
there, believing they are signing in.

- They arrive on a page that says a link is about to be made, names both
  profiles, and asks for the *other* person's LearnPilot password. They cannot
  complete it.
- „Отказ“, „Изход“ and a sign-in to another account each remove the pending
  link from this browser, and each ends it on the server when the retirement
  is recorded (4.4). When it is not, a copy of the cookie could still be
  confirmed until it expires, and only with the first person's password.
- If their Google identity is already linked, the callback answers
  `identity_taken`. If their Google email is the address of their own
  password account, it answers `email_other_account` (P1).
- **What remains:** they walk away, or close the tab, without pressing
  either button, and the first person returns to that browser within 180
  seconds and types their own password. The second person's Google identity
  is then linked to the first person's account, and their later Google
  sign-ins land there. This needs the two people at one browser within three
  minutes and is undone only by the recovery tooling of section 10.

**An open Google session.** Signing out of LearnPilot does not sign the
browser out of Google. Google's discovery document has no end-session
endpoint [2], and its OpenID Connect guide describes no way for a client to
end the Google session or to force a new password prompt [1]. On a shared
computer the next person can press „Вход с Google“, pick the previous
person's Google account in the chooser and be signed in as them. The design
can only warn: on the sign-in page and on the account page (1.7).

**An accidental student account.** A teacher whose account has another email
than their Google account presses „Вход с Google“ before linking. A student
account is created for that identity, and a later link answers
`identity_taken`. The sign-in page's second sentence is there to prevent
this. Undoing it needs section 10.

## 5. Token validation

### 5.1 Authlib integration and dependencies

Used (all documented public API [9]):

- `authlib.integrations.requests_client.OAuth2Session` — builds the
  authorization URL and derives the S256 challenge; performs the code exchange
  with client authentication `client_secret_basic` (E4: the secret is in the
  `Authorization` header, not the body or the URL);
- its `access_token_response` compliance hook, "invoked before token parsing"
  [9], for the response rules of 5.3;
- `joserfc.jwt.decode` with `JWSRegistry(algorithms=["RS256"])` and a `KeySet`
  of Google's keys — signature verification;
- `authlib.oidc.core.CodeIDToken(...).validate(leeway=60)` — claim validation.

These are the calls Authlib's own Flask client makes internally
(`authlib/integrations/base_client/sync_openid.py`, 1.8.0). No OAuth or JOSE
logic is written by hand. What the application owns is the transaction, the
policy, and the checks Authlib does not make (5.3, 5.4).

Not used (E1, Authlib 1.8.0 source):

- `authlib.integrations.flask_client.OAuth`: it stores one session entry per
  start with a fixed 3600-second expiry; it has no place for intent, account
  binding or a consumption step; on a provider `error` it raises before
  clearing its state; its default claim options validate the issuer only.
- `authlib.jose`: deprecated in favour of joserfc [9].
- The UserInfo endpoint, refresh tokens and revocation: never called. The
  access token is discarded in memory.

`requirements.txt`:

```
Authlib>=1.8,<2
joserfc>=1.6.1,<2
requests>=2.32,<3
urllib3>=2.2,<3              # HTTPResponse.read1, for the bounded body reader (5.7)
cryptography>=45.0.1,<51     # floor raised from 44: Authlib 1.8 and joserfc require it
```

- Authlib 1.8.0 requires Python 3.10+, `cryptography>=45.0.1` and
  `joserfc>=1.6.1`. It does not depend on an HTTP library; its requests client
  needs `requests`. `urllib3` gained `read1` in 2.2.0 [17].
- The packages are imported at module load like every other dependency, so the
  application does not start without them, enabled or not.
- Never enable `DEBUG` logging for `authlib`: its Flask client logs the PKCE
  verifier at that level (E1). The path used here logged nothing sensitive
  (E4).

### 5.2 Trusted provider metadata

Pinned constants in `web/services/google_oidc.py`, equal to Google's discovery
document on 2026-10-11 [2]:

| Name | Value |
| --- | --- |
| issuer | `https://accounts.google.com` |
| authorization endpoint | `https://accounts.google.com/o/oauth2/v2/auth` |
| token endpoint | `https://oauth2.googleapis.com/token` |
| JWKS URI | `https://www.googleapis.com/oauth2/v3/certs` |
| ID-token algorithm | `RS256` (the only one Google lists) |

- No URL is ever taken from a token, a callback parameter, a response body or
  a redirect. A token's `iss` is compared, never dereferenced. A JWK's `x5u`
  or `jku` is never followed.
- Google recommends reading the endpoints from the discovery document [1].
  Pinning departs from that on purpose: one outbound request fewer inside the
  budget, no start-up dependency on the network, and one input fewer to
  validate. The cost is that a change of endpoint by Google breaks sign-in
  until a release. There is no command that compares the two; the comparison
  is one line of the activation checks (11.2) and of any later incident.

### 5.3 Provider responses

Every request is made once. Nothing is retried, by the application or by the
libraries: the transport adapter is mounted with `max_retries=0` explicitly
(it is also the default of `requests`, E5), each call uses its own HTTP
session that is closed afterwards (no pooled connection to revive), and an
authorization code is not sent twice under any circumstance.
Redirects are never followed (`allow_redirects=False`); a 3xx is a failure.
The session has `trust_env=False`: no proxy, `.netrc` or CA override from the
environment. Certificate verification is on.

**Token endpoint.** E4 and E5 show why these checks are the application's.
Authlib's `fetch_token` looks at the status only from 500 up: it returned a
302 with a JSON body and a 200 without `id_token` as tokens, returned a JSON
array as a Python list, and for a 302 or a 200 with a body that is not JSON
raised the JSON library's own error. The checks below run in Authlib's
`access_token_response` hook, before it parses anything.

| Response | Class | Outcome |
| --- | --- | --- |
| 200, body at most 64 KiB, a JSON object, `id_token` a non-empty string of at most 16 KiB | answer | continue with 5.4 |
| 400 or 401, a JSON object whose `error` is a string | provider answer | `failed`. `invalid_client` and `unauthorized_client` are logged at ERROR: the credentials are wrong. |
| 3xx | failure | `unavailable` |
| 429, 5xx | failure | `unavailable` |
| any other status; a body over 64 KiB; not JSON; JSON that is not an object; `id_token` missing or not a string | failure | `unavailable` |
| name resolution, connection, TLS or timeout error | failure | `unavailable` |
| the budget ran out (5.7) | overrun | `unavailable` |

"Failure" and "overrun" count for the cooldown (5.8). A provider answer does
not. An exception in the worker that none of the rows names is a failure too,
known by its class name only (5.7). In every row the transaction is already
spent: the record of 1.5 H7, with the storage's cleanup, was committed before
the call. In every row **except the first** nothing more is written to the
database and no session is issued. The first row goes on to the ID-token
checks; if they pass, a sign-in writes what section 6 describes and issues a
session, and a link writes nothing to the database and puts a pending link
into the session (1.5 V7).

Authlib's `OAuthError` carries the provider's `error_description` (E4). It is
never shown or logged; only an allow-listed `error` code and an exception's
class name are (8.4).

**Signing keys:** 5.6.

### 5.4 ID-token checks, in order

1. Signature: `joserfc.jwt.decode` with the RS256-only registry and the key
   set. E2: an unsigned token (`alg: none`), an HS256 token keyed with the
   public key, a token signed by another key under a published `kid`, an
   altered payload, an unknown `kid` and a missing `kid` are all refused.
2. The payload is a JSON object.
3. **Types, before anything is compared.**
   - `exp` and `iat` are JSON integers: `type(value) is int`, greater than 0
     and below 2^40. Booleans, floats, strings, `NaN` and `Infinity` are
     refused. Google documents both as "integer seconds" [1].
   - `aud` is a JSON string equal to the client id. A list is refused, a
     one-element list included. Google documents one client id as the
     audience [1].
   - `azp`, if present, is a JSON string equal to the client id.
   - `iss` is a JSON string, one of the two of 5.5.
   - `sub` is a JSON string of 1–255 visible ASCII characters (Google: at most
     255 ASCII characters [1]; the storage service's rule). A malformed
     subject is a validation failure, **never** "identity not found".
   - `nonce` is a JSON string.
4. `CodeIDToken.validate(leeway=60)` with `iss`, `aud`, `sub`, `exp` and `iat`
   essential, the transaction's nonce, the client id, and the access token (so
   that `at_hash` is checked when present).
5. `exp > iat`, and `iat` is not more than 600 seconds old (plus the leeway).
   The token has just come from the token endpoint.
6. Result: `VerifiedIdentity(subject, email, name)` with the email and name
   reduced as in 6.3 and 6.4. The issuer is the stored constant.

A token that fails any of steps 1–5 ends the flow as `failed`. Google
answered, so it is not a failure for the cooldown.

Why step 3 exists (E6, real signatures): with revision 1's checks a token
whose `exp` was `Infinity`, `NaN`, `1e400`, a float or 2^70 was accepted, as
was one whose `iat` was `NaN` or a float, and one whose `iat` was later than
its `exp`. A comparison with `NaN` is false in both directions, so it passes
"not expired" and "not too old" alike. Authlib's defaults also accepted an
`aud` of another client when `azp` was ours, an empty `azp`, and an audience
list. With the checks above every one of these is refused.

The claims that are validated are the claims that are used: there is one
parse, by joserfc. A payload that repeats a member name is read as its last
value by that one parser (E6), and that value is what every check sees.

Google states that a token received directly from its token endpoint over
HTTPS may be trusted without signature validation [1], and OpenID Connect
Core permits the same [11]. The design validates the signature regardless.

`hd` is not checked; no domain is restricted. `email_verified` is not used for
any decision (6.3).

### 5.5 Issuer

- Google documents that `iss` is "always `https://accounts.google.com` or
  `accounts.google.com`" [1]. Both exact strings are accepted. E2: a trailing
  slash, another case, `http`, and a longer host are refused.
- **Stored:** always `https://accounts.google.com`, the discovery document's
  `issuer`. If the token's own spelling were stored, one Google account would
  become two identities, and two student accounts, whenever Google used the
  other spelling.
- This is a closed mapping of two documented literals to one constant, defined
  beside the provider's other constants. It is not normalization: nothing is
  lower-cased, trimmed or parsed, and no other issuer can enter through it.
  `web/services/identities.py` keeps comparing byte for byte.
- §5 of the accounts design calls an identity "the exact pair … of a verified
  ID token". For a bare-spelled token the stored issuer differs from the
  token's bytes; §5 gets one sentence saying so when PR #331 is implemented.
- *Not verified:* which spelling real tokens carry.

### 5.6 Signing keys

- Fetched from the pinned JWKS URI under the rules of 5.3. Accepted only as a
  200 response of at most 64 KiB that is a JSON object with 1–16 keys; used:
  keys with `kty` RSA, `use` absent or `sig`, `alg` absent or `RS256`, modulus
  at least 2048 bits.
- Cached per process for the response's `Cache-Control: max-age`, clamped to
  5 minutes–24 hours (Google sent about 5.8 hours on 2026-10-11 [2]); one hour
  if absent. Google asks that the cache directives be respected [1].
- A fetch happens inside a callback's worker (5.7) and only then, at most
  once per callback: when the cached set has expired, or when the token names
  a `kid` the cached set lacks. It runs inside that callback's one budget. No
  further rate limit is added: ID tokens reach the verifier only from Google's
  token endpoint, so a visitor cannot trigger fetches with invented `kid`s,
  and every callback that gets this far has already spent a transaction.
- A failed fetch uses the cached set if it is younger than 24 hours; otherwise
  the sign-in ends `unavailable`.
- The worker is given a copy of the cached set and returns a freshly fetched
  one with its result. The request thread stores it in the cache, and only if
  the result arrived in time. The cache is read and written only by request
  threads, under the provider lock (5.7).

### 5.7 One network budget

Production has one sync worker with a 30-second timeout
(`deploy/gunicorn.conf.py:17`–`20`). While a callback waits for Google, no
other request is served. A `requests` timeout does not bound that wait. E5,
against a loopback server, with `timeout=(2, 1)`:

| Server behaviour | Time until `requests` returned |
| --- | --- |
| response body sent one byte every 0.4 s | 8.1 s |
| response headers sent one byte every 0.4 s | 8.1 s |
| name resolution taking 3 s (simulated), with `timeout=(0.5, 0.5)` | 3.0 s |

Each socket read stayed under its timeout; the whole call did not. Name
resolution is outside the timeout altogether.

**The budget.** `NETWORK_BUDGET = 8` seconds per callback, measured on the
monotonic clock from the moment the worker is started. It covers everything
that touches the network: name resolution, connecting, TLS, sending, the
response headers and body of the code exchange, and the same for a signing-key
fetch if one is needed. When it runs out the request thread stops waiting and
answers `unavailable`. With the two short database transactions the callback
stays far below Gunicorn's 30 seconds and Nginx's 35.

**The mechanism.** A provider gate in `web/services/google_oidc.py`.

*State and its lock.* The module keeps three pieces of state per process: the
number of free slots, the key cache (5.6) and the cooldown (5.8). One
`threading.Lock`, the provider lock, guards all three: every read and every
write of any of them happens while it is held, and nothing slow happens while
it is held (no network call, no database call, no waiting). Each task also
has a lock of its own for handing over its result.

*The flow.*

- The network work of one callback runs in one daemon thread. The request
  thread waits for it for at most the budget.
- **The bound: at most `PROVIDER_SLOTS = 2` workers are inside the network
  function at any moment, per process.** A worker enters the function only
  while its task holds a slot, and a slot is given back only when its worker
  is not, or no longer, inside the function. The bound is on workers inside
  the function, which is where the sockets are. **It is not a bound on
  thread objects.** A thread whose start raised can exist for a moment
  beside two working ones. By then its slot has been given back; it finds
  its task abandoned, never enters the function, and ends. The review's
  model saw three threads with three tasks, and E21 four with four, with
  never more than two inside the function.
- The request thread takes a slot, without waiting, before it builds or
  starts the thread. With no slot free the callback answers `unavailable`
  at once and starts no thread. The task, with its lock and its flags, is
  created before the slot is taken, so that the rule below can run from the
  first statement after the taking.
- **A slot is given back exactly once.** One function gives a slot back. It
  runs under the provider lock and is guarded by a flag on the task: the
  first call for a task returns the slot, and every later call for the same
  task does nothing. Every release goes through it, from the worker and from
  the request thread alike, so the count of free slots never exceeds
  `PROVIDER_SLOTS`.
- **From the slot to the start: one guarded region.** It begins with the
  first statement after the taking of the slot and ends when
  `Thread.start()` has returned. Everything the request thread does in
  between is inside it: building the thread object, and `start()` itself.
  **An exception raised anywhere in the region is handled by the one rule
  below.** Revision 2.3 named `start()` only; an exception raised before it
  would have lost the slot in the same way. There are three situations, and
  the exception does not tell the last two apart:
  - *raised before `start()` was called.* No thread exists, and nobody else
    will give the slot back;
  - *raised by `start()`, and no thread exists.* The operating system
    refused one, for lack of threads or memory. Nobody else will give the
    slot back;
  - *raised by `start()`, and the thread exists.* The exception arrived
    after the thread was created. CPython's `start()` creates the thread and
    then waits for it [25], and the `SystemExit` of a worker abort (8.4) can
    arrive during that wait. That thread runs, and gives the slot back
    itself.

  One rule covers all three. Under the task's lock the request thread marks
  the task abandoned and reads whether the worker has begun its work (the
  worker's first action, below).
  - *Not begun:* the request thread gives the slot back. This is always the
    branch when no thread exists. A thread that exists after all then finds
    the task abandoned, makes no request, and its own release is the call
    that does nothing.
  - *Begun:* the request thread leaves the slot with the worker, which gives
    it back when it is no longer inside the function. The slot stays held
    for as long as that thread's sockets live, so the bound of two still
    holds.

  The callback then answers `unavailable`. An exception that is not an
  `Exception` is raised again after these steps. The failure is logged by
  class name, a result from such a worker is discarded like that of any
  abandoned task, and the failure count is left as it was (5.8). Without the
  release a slot would be lost for the life of the process, and two such
  failures would disable Google sign-in until a restart. With a release from
  both sides and no guard, the count would rise above two (E18: three free
  slots where two exist).

  *The one place the region does not cover* is the boundary between the
  taking of the slot and the region's first statement. No statement of the
  code can raise there. An asynchronous exception can arrive there. In a
  Gunicorn worker those are the `SystemExit` that its abort and quit
  handlers raise, and both handlers end the worker process [18], and its
  slot count with it.
  Closing that boundary too would need the taking itself to record on the
  task that it holds a slot, a third state of the guard. That is a change
  to the reviewed rule. It is not made here, and it is named for the
  focused review (0.4).
- Under the task's lock exactly one of two things happens: the result is
  taken because it arrived before the deadline, or the task is marked
  abandoned. Never both.

*What the worker thread may do.* It runs one function that is given plain
values (the code, the verifier, the nonce, the redirect URI, the credentials,
a copy of the cached keys, the deadline) and returns a value. Around that
function the thread performs exactly three synchronization actions:

1. first, under its task's lock: if the task is already abandoned, it skips
   the function; otherwise it marks that its work has begun;
2. under its task's lock, it stores its result unless the task was abandoned,
   and signals the waiting request;
3. under the provider lock, through the one release function, it gives its
   slot back. **This is the `finally` of a `try` that begins with the
   thread's first statement.** It therefore covers action 1, the function
   and action 2: the slot is given back when the function has returned,
   when it was skipped, and when action 1 or action 2 itself raised. In
   each of these the thread is not, or no longer, inside the function, so
   its sockets are finished. The waiting side gives a slot back in one case
   only: an exception in the guarded region, for a worker that has not
   begun.

Nothing else. The thread has no application or request context, so Flask's
`session`, `g`, `request` and `current_app` raise if touched. It is given no
database connection. It neither reads nor writes the key cache or the
cooldown, and it changes no account, session or request state. Those are
touched only by the request thread, and only for a result that arrived in
time.

*Exceptions in the worker.* The thread catches everything its function
raises, `BaseException` included. Its result is then a failure that carries
the exception's class name and nothing else. The exception object is not kept
and its text is never read or logged: a `requests` error names URLs,
Authlib's error carries the provider's description, and a JSON error keeps
the whole document it failed on (E12: the token response was in
`JSONDecodeError.doc`). Nothing reaches `threading.excepthook`, which would
print the traceback, with that text, to the service's log (E12). The slot is
given back on this path too.

*An exception raised by action 1 or action 2 themselves* is not expected:
they take a lock and read or set a flag. The same containment applies to
the whole body of the thread, so such an exception does not reach
`threading.excepthook` either, and the `finally` gives the slot back. No
result is handed over in that case. The request thread waits until its
deadline, answers `unavailable`, and the task counts as an overrun (5.8).

**What this does and does not guarantee**

- **The request is bounded.** It returns at the deadline whatever the network
  does. E5: with a 2-second deadline against the 8-second response, the caller
  had its answer after 2.0 s.
- **The underlying request is not cancelled.** The thread and its socket go on
  until the operation they are in ends. E5 observed exactly that: the thread
  was still running after the caller had its answer.
- **Timed-out work cannot change anything.** A worker thread has only the
  three actions above. None of them touches an account, a session, the
  database, the key cache or the cooldown.
- **A late result is discarded.** The value of an abandoned task is never
  read.
- **Working threads cannot accumulate.** Two slots bound the workers inside
  the network function, and with them the sockets of abandoned work, however
  many callbacks time out. A thread beyond the two is one whose start
  raised: it never enters the function, opens no socket, and ends as soon as
  it has run. E5 and E12: two
  abandoned calls held both slots, the third call was refused at once and
  started no thread, and after the slow work ended the slots were free again.
  E12: 200 calls from 8 threads at once never took the count below zero and
  left it at two.
- **How long an abandoned thread lives.** Inside the worker every connect and
  read timeout is at most the time remaining (connect at most 2 s, a read at
  most 3 s), bodies are read one bounded read at a time with the deadline
  checked between reads, and no second request starts with less than 2 seconds
  left. In the ordinary failures (an unreachable host, a refused or stalled
  connection, a slow or oversized body) the thread therefore ends at about the
  deadline; E5: the dripped body was abandoned by the reader itself at 2.0 s,
  the 5 MiB body at once. Two cases outlive it: a name lookup, which only the
  operating system's resolver bounds, and a peer on the network path that
  keeps delivering a few bytes of the TLS handshake or of the response headers
  inside each read timeout (E5: the dripped headers ran their full 8.1 s).
  Such a thread holds one slot and one socket until it ends. While both slots
  are held, Google sign-in answers `unavailable` at once; nothing else in the
  application waits on them.
- Daemon threads do not delay a worker's exit. The state is created when the
  module is imported, and no thread is started at import: a process forked
  from a preloading master starts with the lock free and both slots free, and
  without preloading each worker creates its own.

### 5.8 Cooldown

Per process; an availability device, not security state. Its two values, a
count of consecutive failures and the `time.monotonic()` reading until which
the provider is cooling down, are guarded by the provider lock (5.7).

- **Failure:** a row marked "failure" in 5.3. **Overrun:** the budget ran out
  and the task was abandoned. A provider answer, a token that fails
  validation, a refusal for lack of a slot, and an exception between the
  taking of the slot and the return of `start()` (5.7) are none of these.
- **Starting a cooldown:** three consecutive failures, or one overrun. An
  overrun alone is enough because it cost the single worker the whole budget.
- **The count.** A failure adds one. A result that arrived in time from the
  provider and is not a failure (a provider answer, a token that then fails
  validation, a success) sets it to zero, and so does the start of a
  cooldown. **A refusal for lack of a slot, and an exception between the
  slot and the start, leave it exactly as it was:** they neither add to it
  nor reset it. Neither produced a result that the request thread read, so
  neither says anything about the provider.
- **During the 60 seconds:** `start` and `link` create no transaction and
  answer `unavailable`; a callback is still matched and recorded as used
  (1.5 H7) and then answers `unavailable` without contacting Google.
- **Afterwards** every request is treated as before the cooldown. There is no
  probe and no intermediate state: the next cooldown starts after three more
  consecutive failures or one more overrun.
- During a complete outage in which every call runs out of budget, the one
  sync worker therefore spends at most one budget (8 s) in every 68 on
  Google.

## 6. Atomic onboarding and metadata

### 6.1 One transaction

The view runs `BEGIN IMMEDIATE`, calls a service function, and commits or
rolls back. The service (`web/services/oidc_accounts.py`) follows the
repository's convention: explicit connection, no commit, no Flask. It is a new
module because `web/services/identities.py` is pinned by tests to contain no
`users` write.

`sign_in_or_onboard(conn, identity, now)`:

1. `found = identities.find_identity(issuer, subject)`. **This comes first and
   the email plays no part in it.**
2. **Found:** read the user row.
   - inactive → `not_allowed`, roll back. No new account is created for an
     identity that belongs to a disabled one.
   - role `admin` → `not_allowed`, roll back (6.2).
   - otherwise `identities.record_identity_login`, `accounts.record_login`,
     commit; sign in that user.
3. **Not found:**
   - the reported email, if comparable, equals an existing `users.email` →
     `email_exists`, roll back (6.3);
   - otherwise `accounts.create_user(display_name=…, role="student")` with no
     email and no password, then `identities.add_identity(…,
     provider="google", email_at_link=…)`, the two login stamps, commit; sign
     in the new user.
4. Any exception: roll back; `failed`.

### 6.2 Properties

- **No orphan users.** The user and the identity commit together or not at
  all. E3, 8 connections racing on each of 40 new identities:

  | Shape | users | identities | users without identity |
  | --- | --- | --- | --- |
  | `BEGIN IMMEDIATE`, lookup then both inserts (this design) | 40 | 40 | 0 |
  | deferred, roll back on conflict | 40 | 40 | 0 |
  | user committed before the identity | 320 | 40 | 280 |

- **No reassignment.** Nothing updates `user_identities.user_id`. A duplicate
  resolves to the existing row's user.
- **`BEGIN IMMEDIATE`, not deferred.** The lookup and the inserts are then one
  serialized unit; a deferred transaction that reads and later writes can fail
  with a busy error that the busy timeout does not retry.
- **Role and active state are read at the write**, from the row, on every
  Google sign-in. A teacher who linked Google and is later promoted keeps the
  identity row and is refused here from then on. Password plus TOTP remains
  the only way an admin session comes into being.
- **Only `student` is ever created.** The role is a literal in the service.
  Granting `teacher` or `admin` stays with the operator CLI.
- **No password is generated.** `password_hash` stays `NULL`, which the schema
  allows for exactly this case.

### 6.3 Email

The provider email is **metadata**. Identity validation is complete before it
is looked at, and nothing about it can fail a token.

Two terms:

- **comparable**: the `email` claim is a string of at most 254 characters that
  `accounts.normalize_email` accepts. `email_verified` plays no part.
- **storable**: comparable, and the reported value also passes the storage
  service's rule for `email_at_link` (printable, no whitespace).

| Google's `email` claim | Collision rule | `email_at_link` | `users.email` |
| --- | --- | --- | --- |
| comparable and storable, verified or not | applies, on the normalized form | the value as Google reported it | `NULL` |
| comparable but not storable | applies | `NULL` | `NULL` |
| absent, not a string, over 254 characters, or refused by `normalize_email` | does not apply | `NULL` | `NULL` |

- **Collision at sign-in (A4).** The identity is new and the comparable email
  equals an existing account's `users.email`: no account is created, nothing
  is linked, nobody is signed in. The message tells the person to sign in with
  email and password and to link Google from „Профил“.
- **Collision at linking (P1).** The comparable email equals the
  `users.email` of an account other than the linking one: nothing is linked
  (4.2, step 3 of the link decision).
  Equal to the linking account's own email, or to no account's: no objection.
- **An identity that is already linked is resolved first** (6.1 step 1). For
  it the email is not read at all, and `email_at_link` is never updated
  afterwards. Google warns that the address may change and may not be unique
  [1].
- **Never invented, never truncated, never an identifier.** An email that is
  not comparable is absent. It does not fail the sign-in, does not become
  `users.email`, and selects nobody.
- **Unverified addresses take part,** as approved. The consequence is stated
  plainly: whoever controls a Google account that reports an address learns
  from `email_exists` or `email_other_account` whether a LearnPilot account
  has that address. The password form does not reveal that. *Not verified:*
  whether Google issues tokens with an address its holder has not confirmed.
- **Why `users.email` stays `NULL` (A3).** `users.email` is the identifier of
  the local password credential. With `NULL`, a collision always means that
  an account with that sign-in email exists, which is what the message says,
  and two Google-only accounts can never collide with each other.
- **What a Google-only account cannot do in this release.** It cannot get a
  password: the schema refuses a password hash without an email
  (`web/migrations/012_users.sql:16`) and nothing sets an email. It cannot add
  another identity: linking requires the account's password (A2). If its
  Google account is lost, the LearnPilot account can be managed by the
  operator (8.6) but not signed in to. Changing either belongs to a later
  release.

### 6.4 Display name

`users.display_name` must be 2–80 characters. It becomes the `student_name`
snapshot that teachers see in results, exports and note filenames.

1. Take `name` only if it is a string.
2. Remove every character of Unicode category `C` (control, format, including
   bidirectional overrides), collapse whitespace, strip.
3. Longer than 80 characters: cut to 80 and strip again.
4. Shorter than 2 after cleaning, or absent: „Ученик“ (P4).

- No name is derived from the email, and `given_name`, `family_name`,
  `picture`, `locale` and `hd` are not read.
- The name is set at creation only. It is not refreshed on later sign-ins, and
  linking does not change an existing account's name. On the confirmation page
  the cleaned name is shown and nothing else is done with it.
- The name is chosen by the student, through their Google profile. The
  existing sinks already treat names as untrusted: templates escape, the CSV
  export neutralizes formula prefixes (`_quiz_csv_text`,
  `web/blueprints/teacher.py:1472`), and note filenames are slugged. Tests pin
  all three with a hostile name.
- Two students may share a name; attempts are keyed by account, not by name
  (`docs/ACCOUNTS_DESIGN.md` §24).

### 6.5 Never collected

Date of birth, school, class, phone, address, gender, profile picture, locale,
hosted domain, Google tokens, and any claim not named in 5.4, 6.3 and 6.4. No
scope beyond `openid email profile` is requested and no further API is called.

## 7. Session issuance and redirects

### 7.1 Issuance

- `issue_account_session` and `issue_pending_second_factor` move from
  `web/blueprints/account.py:88` and `:103` to a new neutral module
  `web/account_session.py`, together with the attempt-marker prefix, the
  role-home table and the account throttle key. Sign-out's `session.clear()`
  becomes a third function there, `end_session`. Each of the three calls
  `retire_linking()` (4.4) before it clears the session and is otherwise
  unchanged. None of them branches on the result: the session is cleared or
  replaced whether or not anything was recorded. Each is called only after
  its caller's own transaction has ended (4.4, "The precondition at the call
  sites"). Both blueprints import the module; no blueprint imports another,
  which a test forbids.
- A Google sign-in calls it with the user's id and the row's `session_key`,
  after the commit. The session is then exactly `uid`, `sk`, `iat` and the
  carried-over `quiz_attempt_owner:*` markers. No provider name, no "signed in
  with Google" flag, no claim.
- Everything else follows from the existing loader: the role, name and active
  state are read from the row on each request; the absolute lifetime is 30
  days for a student or teacher; a rotated key ends the session; historical
  attempts stay bound to their browser marker.
- A link does not call it.

### 7.2 Return target

- **Stored:** `next` from the sign-in form, only if
  `safe_redirect_target(next, "/") == next` and it is at most 512 characters;
  otherwise `""`.
- **Used:** `safe_redirect_target(tx.next, <home of the role in the row>)`
  again at the callback (`web/redirects.py:17`). The role is the one read at
  the write, so a target chosen while anonymous cannot pick the fallback.
- A link returns to the confirmation page and then to `/account`.
- The redirect to Google is the pinned authorization endpoint plus encoded
  parameters. No part of its host or path comes from a request.

### 7.3 An authenticated user who starts a sign-in

- A signed-in account that posts to `/auth/google/start` is redirected to its
  target or home. No transaction, no request to Google, no account switch.
- If a `signin` callback nevertheless meets a signed-in account, it fails and
  leaves the session alone.
- An admin with a pending second factor is anonymous and may start. If the
  Google sign-in succeeds, the new session replaces the pending keys. That is
  no bypass: the Google identity is never an admin's (6.2), and the pending
  state grants nothing by itself.

## 8. Operations and privacy

### 8.1 Configuration and the activation gate

| Variable | Rule |
| --- | --- |
| `DZI_GOOGLE_CLIENT_ID` | The OAuth client id. Visible ASCII, at most 255 characters, ending in `.apps.googleusercontent.com`. |
| `DZI_GOOGLE_CLIENT_SECRET` | The client secret. Visible ASCII, 16–255 characters. Never logged, never in a cookie, a URL, a template or an error message. |

**Approved (A5):** the provider is not activated for real users before the
recovery tooling of section 10 exists.

**Approved (P2): how the build holds that line.**

- `web/services/google_oidc.py` has one constant, `PRODUCTION_ACTIVATION`. It
  is `False` in PR #331. The release of the recovery milestone sets it to
  `True` (section 10, criterion 9). Nothing else does, and no setting or
  environment variable overrides it.
- The provider is **enabled** when all of these hold: both variables are set
  and valid; a public base URL is configured (8.2); and the application is
  either a development run (`web.app`) or a production build whose constant
  is `True`.
- **Production while the constant is `False`** (the build of PR #331):
  - the service starts normally whatever the two variables hold: unset, one
    of them, both, well-formed or not. The presence of credentials is never a
    reason to refuse to start;
  - the provider is disabled (1.8);
  - if either variable is set, startup logs one WARNING that names the
    settings and not their values: "Google sign-in credentials are
    configured, but this build does not enable Google sign-in." The
    configuration check of `docs/DEPLOYMENT.md` §10 imports the application
    and therefore shows the same line.
- **Production once the constant is `True`:**
  - both unset: disabled. This is the default;
  - both set and valid: enabled;
  - exactly one set, or either malformed: `web.wsgi` refuses to start, with a
    message that names the setting and not its value. This is the rule the
    optional `DZI_TOTP_KEY` already follows (`web/production.py:136`): unset
    is fine, and a value that is set must be usable.
- **A development run** never refuses to start: a missing or malformed value
  warns and leaves the provider disabled. Development uses a separate OAuth
  client (8.2).
- There is no separate on/off flag. Once the provider is active, removing the
  two lines and restarting is the off switch.
- The values live only in `/etc/learnpilot/learnpilot.env` (root, 0600) and in
  `app.config`. The env template gains both lines, empty and commented.

Gabriel approved this gate as it is described here (P2, 0.2). It does not
replace the checklist of 0.4: every activation prerequisite there stands
beside it.

### 8.2 Callback URI

- `redirect_uri = <public base URL> + "/auth/google/callback"`, where the base
  is the configured one of `web/public_url.py`: in production always
  `https://` + `DZI_PUBLIC_HOST`, set at startup (`web/production.py:355`).
- For this flow the request-derived loopback fallback of
  `trusted_public_base_url` is **not** used. Without a configured base the
  provider is disabled. A development run sets `DZI_PUBLIC_BASE_URL`.
- The same string is sent in the authorization request and in the code
  exchange. The request's `Host`, `X-Forwarded-Host` and `Referer` never enter
  it. Google compares it exactly with the registered value [3].
- A development client is a **separate** OAuth client. The production client
  registers exactly one redirect URI.

### 8.3 Nginx

*Not run.* No Nginx was available where this was written. What follows is
derived from Nginx's documentation and source [14][15][16] and is confirmed
only by the server checks of 8.7.

**Rate limits.** A Google sign-in is one POST and one GET per person; thirty
students behind one address produce thirty of each within a minute.

| Location | Zone | Key | Limit |
| --- | --- | --- | --- |
| `= /auth/google/start`, `= /auth/google/link` | `lp_oidc_start` (new) | client address, POST only (the existing map) | 1 r/s, `burst=40 nodelay` |
| `= /auth/google/link/confirm` | `lp_login` (existing) | the same | 1 r/s, `burst=40 nodelay` |
| `= /auth/google/callback` | `lp_oidc_callback` (new) | client address, every method | 1 r/s, `burst=40 nodelay` |

- `nodelay` on all of them, as on every existing zone
  (`deploy/nginx/learnpilot.conf:98`). Without it Nginx queues a burst and
  releases one request per second [15]: the thirtieth student's callback would
  wait half a minute on a blank page while the transaction's lifetime runs.
- The confirmation carries a password, so it shares the sign-in budget that
  already protects password hashing.
- The callback zone is keyed for every method because the existing map charges
  POSTs only.
- In the application: the account's counter for the confirmation password
  (1.6), the slots and the cooldown (5.7, 5.8). There is no per-identifier
  throttle for a Google sign-in; there is no identifier before Google answers.
- A development or tunnel run has no Nginx and so no rate limit.

**The callback location.** Nginx itself answers for this URL when a limit is
hit (429) and when the application is down or slow (502, 504). Its default
error page would then be a document at the address that carries the code and
the state, and its error log would record the request line [16].

```nginx
log_format lp_no_args '$remote_addr [$time_local] "$request_method $uri $server_protocol" '
                      '$status $body_bytes_sent lr=$limit_req_status up=$upstream_status';

location = /auth/google/callback {
    limit_req zone=lp_oidc_callback burst=40 nodelay;
    access_log /var/log/nginx/access.log lp_no_args;
    error_log /dev/null emerg;
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-Frame-Options "DENY" always;
    add_header Referrer-Policy "no-referrer" always;
    proxy_intercept_errors on;
    error_page 400 403 404 405 429 500 502 503 504 = @oidc_leave;
    proxy_pass http://127.0.0.1:8000;
}

location @oidc_leave {
    access_log /var/log/nginx/access.log lp_no_args;
    error_log /dev/null emerg;
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-Frame-Options "DENY" always;
    add_header Referrer-Policy "no-referrer" always;
    add_header Cache-Control "no-store" always;
    return 302 /login?google=unavailable;
}
```

- **Leaving the URL.** Every error status for this location, whether Nginx
  produced it (429 from the limit, 502 and 504 from the upstream) or the
  application did (`proxy_intercept_errors`), is turned into a redirect to a
  page without secrets. A named location is needed: `error_page` with a path
  performs an internal redirect and would serve the sign-in page's content
  *at* the callback address [14][16]. The application's own redirects (302)
  are not in the list and pass through.
- **With the provider disabled** the redirect still names
  `google=unavailable`: the configuration is static and cannot know. The
  application ignores the parameter while the provider is disabled (1.8), so
  the visitor sees the ordinary sign-in page and no message about Google.
- **Access log.** `$request_method $uri $server_protocol` instead of
  `$request`: the path without the query string. `$limit_req_status`
  (Nginx 1.17.6) and `$upstream_status` keep a refused or failed callback
  visible to the operator now that it ends as a 302.
- **Error log.** A refusal by `limit_req` is logged at `error`, an upstream
  timeout or refused connection at `error`, other connection failures at
  `crit` or `alert`, and each such line ends with the full request line [16].
  The level `crit` that revision 1 proposed would therefore still record some
  of them.
  `error_log /dev/null emerg` discards every error-log line for requests in
  these two locations; Nginx switches to a location's error log when the
  location is chosen, before the limit and before the upstream [16].
- **What that leaves.** Lines written before the location is chosen (while the
  request line and headers are parsed) go to the server's error log. The
  parsing complaints read in the source are written at `info` [16], below the
  default `error` threshold, and a well-formed browser request produces none.
  The main `error_log` must stay at `error` or stricter; `info` or `debug`
  there would record request lines for every location. 8.7 step 3 checks it.
- The `log_format` line goes beside the zones at the top of the file (`http`
  context).
- `return 302` with a local path is sent as an absolute `Location` built from
  the request's scheme and host [14]. The server block answers only for the
  configured name; unknown hosts get 444
  (`deploy/nginx/learnpilot.conf:22`).
- `add_header` in a location replaces the inherited headers [14], so the two
  existing security headers are repeated beside the stricter referrer policy.
- A callback that Nginx refuses never reaches the application: the
  transaction is not spent and the code is not redeemed. The person starts
  again from the page they are sent to.
- Certbot later adds the TLS listener to the same server block; the locations
  are unaffected.

### 8.4 Gunicorn and application logs

**What leaks today** (E8 and E9, Gunicorn 23.0.0 driven in-process):

- The access log line contains the request line with the query string
  (`deploy/gunicorn.conf.py:39`, `%(r)s`).
- When an exception escapes the application, **or the worker is aborted on
  its timeout**, Gunicorn logs `Error handling request <URI>` at ERROR with
  the query string (`gunicorn/workers/base.py:263`). The abort raises
  `SystemExit` inside the request, and the sync worker passes every
  `BaseException` to that handler [18]; E8 called the handler with both kinds.
- **When Gunicorn's own parser refuses a request,** the same handler logs
  `Invalid request from ip=…: <the exception's text>` at WARNING
  (`gunicorn/workers/base.py:259`), and the text quotes what was refused
  [18]: the whole request line, what follows a second space in it, or a
  header line. E9, with the real parser:

  | Request | Logged at WARNING |
  | --- | --- |
  | a callback address with a space inside its query | `Invalid HTTP Version: 'x&state=… HTTP/1.1'`: the state, in a line that has no `?` |
  | a request line without a version | the code and the state |
  | a header line without a colon | the session cookie |

  Revision 2's logger class changed none of these lines. It overrode
  `exception()` only, and it cut at a `?`.
- For the same requests Gunicorn's 400 response repeats the text in its
  body. For the callback address Nginx replaces every such response by the
  redirect of 8.3.

*Not verified:* whether such a request line reaches Gunicorn through Nginx.
Nginx refuses a space in a request line since 1.21.1 and accepted one before
[21]; the server's version was not read. Gunicorn also answers any local
process on its loopback port. The change below depends on neither.

**Changes** in `deploy/gunicorn.conf.py`:

1. `access_log_format = '%({x-forwarded-for}i)s "%(m)s %(U)s %(H)s" %(s)s
   %(b)s %(L)ss'`: method, path without query string, protocol. Global: every
   route loses its query string from the access log (`?next=…`,
   `?download=1`).
2. `logger_class`: a subclass of Gunicorn's logger, defined in the same file,
   whose `setup()` adds one `logging.Filter` to the error logger
   (`gunicorn.error`). Every record of that logger passes the filter,
   whichever method Gunicorn called for it (`warning`, `exception`, `error`,
   `debug`). The filter formats the message once, and then:
   - a message that begins with `Invalid request from ip=` is cut at its
     first quotation mark of **either** kind, `'` or `"`, whichever comes
     first. The parser's exceptions quote the refused text with `%r`, and
     Python chooses the mark: `'` normally, `"` when the text contains an
     apostrophe and no double quote. The reason stays and the text goes. A
     cut at `'` alone would begin inside such a text and keep what precedes
     the apostrophe (E13: from a line built with a state followed by an
     apostrophe, `Invalid HTTP Version: "x&state=<the state>` was kept);
   - every message is cut at its first `?`;
   - what was cut is replaced by a fixed marker, and the record's arguments
     are cleared so that nothing is formatted a second time.

   The `logger_class` setting accepts the class itself (E8).
3. `loglevel` stays `info` (`deploy/gunicorn.conf.py:36`). At `debug`
   Gunicorn logs the unread buffer of a client that disconnects in the middle
   of a request, a cookie included, for every route and with or without the
   filter (E9). The deployment test pins the level.

E8 and E9 with these changes: neither the code nor the state appears in any
line, for an escaping exception, for a worker abort and for each request of
the table. Eleven parser exceptions built with a code, a state and a cookie:
eight of the eleven lines carried one of them with Gunicorn's default logger
and with revision 2's, none with the filter. Other lines are unchanged up to
a `?`.

E13, for the quotation marks: nine parser exception classes, each built with
five texts that hold a state and no `?` (plain, with an apostrophe after the
state, with a double quote, with both, with an apostrophe before the state),
as `str` and as `bytes`. A cut at `'` alone left the state in 18 of the 90
messages. The cut at whichever mark comes first left it in none, and kept
the reason in every one. Gunicorn's real parser, given a request line
without a version and a header line without a colon, each with an apostrophe
in it, produced texts in double quotes.

The first rule of the filter depends on the wording of one Gunicorn message.
`requirements.txt` holds Gunicorn to one major version, and the deployment
test runs Gunicorn's real handler for every parser exception it treats as a
bad request, with apostrophes and double quotes in the refused text, so a
release that changes the wording fails the suite instead of leaking.

**What the filter does not rewrite: tracebacks.** The filter rewrites a
record's message. When Gunicorn logs with a traceback (`Error handling
request …`, `Socket error processing request.`;
`gunicorn/workers/base.py:263`, `gunicorn/workers/sync.py:148`), the
traceback is added to the line afterwards, by the log formatter, and it ends
with the exception's own text. The same holds for Flask's `Exception on
<path> [<method>]` line, which is on the application's logger and never
passes this filter. Neither line carries the query string: Gunicorn's
message is cut, and Flask's names the path only.

**Tracebacks are therefore unfiltered, in general.** Whatever an exception's
text holds is logged when the exception is logged with its traceback. The
design relies on that text holding no secret, not on a filter. For the OIDC
modules this is a rule of the code (below: class names only, and the view
and the worker catch everything). For request bodies it rests on the
mechanisms that follow, and revision 2.2 described them wrongly.

- **What such a text could hold: bytes of a request body.** Gunicorn's body
  reader raises exceptions whose text quotes the bytes it could not use:
  `Invalid chunk size: b'…'`, `Invalid chunk terminator …: b'…'`, `No more
  data after: b'…'` [18]. They are raised while the application reads the
  body, and only for a body that reaches Gunicorn with malformed or
  incomplete chunk framing. The same exceptions given to Gunicorn's own
  `log.exception`, with the filter installed: the message was cut, and the
  traceback still held the bytes (E14).

Three mechanisms stand between a visitor's request body and such a line.
They are different things, and each is credited here with what it does and
no more.

1. **Nginx parses the request body's framing itself, and frames what it
   sends on.** *Derived from Nginx's source; not run.* Nginx decodes a
   chunked body from the client with its own parser and answers a malformed
   one with 400. The `Content-Length` and `Transfer-Encoding` of the request
   it sends to the application are written by Nginx from what it decoded;
   they are not the client's [16]. With request buffering on, as deployed,
   that is a `Content-Length`, and nothing of a refused request is sent on.
   With it off, and HTTP/1.1 towards the application as the template sets
   (`deploy/nginx/learnpilot.conf:57`), a chunked body would be sent on in
   chunks that Nginx frames, up to the point where its parser refuses. In
   neither case does a chunk-size line of the client reach Gunicorn. This is
   validation, by Nginx's parser, and it does not depend on buffering. It
   was read at release 1.24.0, the version that Ubuntu 24.04 packages
   (`docs/DEPLOYMENT.md` names that system), and at `master`. Which version
   the server runs was not read.
2. **Request buffering** (`proxy_request_buffering on`,
   `deploy/nginx/learnpilot.conf:67`) decides *when* the request is sent on:
   with it, "the entire request body is read from the client before sending
   the request to a proxied server" [14]. That is delivery timing. It keeps
   a slow upload from occupying the one sync worker, which is the reason the
   template gives for it. **It is not validation, and it is not redaction.**
   It inspects nothing and removes nothing from any log. Its one effect on
   this subject is indirect: a proxied request reaches Gunicorn with a
   length, so Gunicorn's chunked reader is not used for it.
3. **Werkzeug's input wrapper, under `MAX_CONTENT_LENGTH`.** The application
   sets a request-wide limit (`web/app.py:195`). With a limit set, and
   because Gunicorn marks its input stream as terminated [18], Werkzeug
   gives every reader of the body (`request.form`, `request.stream`,
   `request.get_data()`) a wrapper that turns an `OSError` or a `ValueError`
   from the server's stream into `ClientDisconnected`, a 400 response [24].
   Gunicorn's three body-reader exceptions are `OSError`s [18], and Flask
   answers a 400 without logging it. Without a limit, Werkzeug hands out
   Gunicorn's stream unwrapped [24].

   E17 ran this with Gunicorn's real parser and body reader. The application
   of this worktree, on a temporary database and with its configured limit,
   answered 400 to `POST /login` and `POST /account/password` for a
   malformed chunk size, a missing chunk terminator and a truncated chunk,
   and nothing was logged. With the limit removed, as a control, the same
   requests answered 500, and for the malformed size the traceback ended
   with the submitted password. **E14 had missed this mechanism:** it used a
   bare Flask application without a limit, and its result, a 500 with the
   password in the traceback, does not describe this application.

- **Sufficiency.** A visitor reaches the application only through Nginx,
  which is what the loopback bind is for. For such a request mechanism 1
  and mechanism 3 each suffice without the other. Mechanism 3 does so only
  for a body that the application reads, and only under the sync worker
  class ("The worker class" below). Neither mechanism has been verified on
  the server (8.7 step 3).
- **The residual, exactly.** For bytes of a request body to reach a
  traceback, two things must both happen:
  - *raw framing reaches Gunicorn.* That takes a client that speaks to
    Gunicorn's port directly. Gunicorn binds to a loopback address only
    (`deploy/gunicorn.conf.py:14`), so such a client is a process on the
    server, and the bytes are those of its own request; **and**
  - *the body is read past Werkzeug's wrapper.* That takes code that reads
    `environ["wsgi.input"]` directly (E17: with the limit set, such a read
    still answered 500 with the bytes in the traceback), or a request for
    which the limit is unset or set to `None`, or a Werkzeug that behaves
    differently, or a Gunicorn worker class that keeps connections alive
    and reads an unread body itself ("The worker class" below). The
    application has no such read today, and the deployed worker class is
    not such a class.

  This was **not observed on production**; nothing was run there. It is what
  the sources and E17 leave possible.
- **What is no longer claimed.** Revision 2.2 said that with request
  buffering off "a visitor's interrupted or malformed POST … could leave
  part of its form in the journal". That is not supported. Nginx would still
  parse and re-frame the body (mechanism 1), and an interrupted body with a
  length reached the application as a shorter body, with nothing raised and
  nothing logged (E17). Request buffering is therefore not a logging
  requirement. It stays pinned as the capacity setting it is.
- **Required assumptions.** What this section says about logs depends on
  three settings that exist today for other reasons:
  1. Gunicorn's `bind` is a loopback address (`deploy/gunicorn.conf.py:14`);
  2. `MAX_CONTENT_LENGTH` is a number for every request (`web/app.py:195`);
  3. Gunicorn runs the **sync worker class**: `worker_class = "sync"` with
     `threads = 1` (`deploy/gunicorn.conf.py:18`–`19`). Both lines are
     needed, because Gunicorn replaces the sync worker by its threaded one
     when `threads` is above 1 [18].

  All three are pinned by existing deployment tests
  (`tests/test_production_deployment.py:737` for the bind, the worker class
  and the threads; `:765` for the limit). PR #331's tests pin them as
  logging assumptions too, add a static check that no module reads
  `wsgi.input` or assigns a request's `max_content_length`, and run the
  failing body read itself against the three routes that take a password
  (test plan B6). **None of the three has been verified on the server**
  (8.7 step 3; 11.2 check 15).
- **The worker class, and why it is an assumption.** Revision 2.3 named the
  first two settings only. Mechanism 3 acts on a body that the application
  reads. A body that the application leaves unread is never read through
  Werkzeug's wrapper: a 403 from the same-origin check, a 404, a 429 and a
  400 for an untrusted host are all answered before any read. What happens
  to those bytes depends on the worker class. *Read from the source of
  Gunicorn 23.0.0 [18]. The parser's part was also run in-process (E20);
  no worker of either class was run.*
  - *The sync worker,* as deployed, handles one request for each
    connection: it asks its parser for one request, forces the response to
    close the connection, and closes the client. Nothing reads the unread
    bytes, so nothing can raise on them. `keepalive = 2`
    (`deploy/gunicorn.conf.py:22`) has no effect under this worker:
    Gunicorn's own documentation of the setting says that the sync worker
    ignores it.
  - *A worker class that keeps connections alive* (the threaded worker, and
    the asynchronous ones) asks its parser for the next request on the same
    connection. Before it parses that request, Gunicorn's parser discards
    what the previous one left unread, by reading it through **Gunicorn's
    own body reader** (`Parser.__next__`). A malformed chunk among those
    bytes raises the reader's exception inside the worker, outside the
    application and outside Werkzeug's wrapper (E20: asked for a second
    request, the parser raised `Invalid chunk size: b'…'` with the unread
    bytes, an `OSError` without an `errno`; with `Connection: close` it
    stopped and read nothing). The threaded worker logs such an `OSError`
    as `Socket error processing request.` with its traceback, and a
    traceback ends with the exception's text.
  - That path needs what the residual above needs, raw framing at
    Gunicorn's port, and in addition a request that does not ask to close
    the connection and a connection that is read once more. It needs no
    fault in the application.

  The design adds no control for this. It names the setting as an
  assumption, because a change of worker class or of `threads`, made for
  capacity, would otherwise silently remove what mechanism 3 is credited
  with.
- **Versions.** The wrapper was run with Werkzeug 3.1.9 and Flask 3.1.3
  (E17), and by the review of revision 2.2 with Werkzeug 3.1.8.
  `requirements.txt` allows any Werkzeug from 3.1 below 4, and the version
  installed on the server was not read. The behaviour is therefore pinned by
  a test that runs against whatever is installed, and checked on the server
  for what is deployed (8.7 step 3; 11.2 check 16).
- **Not affected: the callback.** It is a GET, its secrets are in the query
  string, and it reads no body. Its own exceptions never reach a traceback:
  the view catches everything and logs class names (1.5 V8), and so does the
  worker thread (5.7).
- No filter for tracebacks is added. This is a statement of what the log
  changes cover, not a new control.

**Application.**

- Never log `request.url`, `request.full_path`, `request.args`, the query
  string, a token response, a claim, a subject or an email.
- One line per outcome on logger `learnpilot.oidc`, with a fixed vocabulary:
  outcome, intent, the local user id when one is known, an allow-listed
  provider error code, an exception's class name. Never `logger.exception`
  and never `str(exc)` in the OIDC modules.
- Account creation and linking are logged at INFO with the user id;
  production attaches a handler as it does for the lifecycle logger
  (`configure_lifecycle_logging`, `web/production.py:234`).
- `authlib`, `urllib3`, `requests` and `http.client` stay at their default
  levels.

**Referrers.** The callback never renders a page, in the application (1.5 V8)
or in Nginx (8.3), and both send `Referrer-Policy: no-referrer` for it.

A code or a state that survives somewhere outside this server's control (a
browser's history, for one) is already spent: the code at Google, the
transaction in the consumption table.

### 8.5 Privacy wording

Proposed replacement for the lists in `docs/ACCOUNTS_DESIGN.md` §15, applied
when PR #331 is implemented:

- **Collected:** email, display name, role, password hash, timestamps. For an
  account that uses Google sign-in, also: Google's issuer and subject
  identifier; the email address Google reported when the identity was added,
  whether or not Google marked it verified; the time the identity was added
  and last used. For an account created through Google, the display name is
  the name in the person's Google profile at that moment.
- **Held briefly, not stored:** while a link awaits confirmation, the Google
  subject, reported email and name are in the person's signed session cookie
  for at most three minutes.
- **Not collected:** date of birth, school, class, phone, IP address, user
  agent, login history; from Google also the profile picture, given and
  family name, locale, organisation domain, and any Google token. No Google
  API is called on the person's behalf.
- **Sent to Google** when the person presses the button: the application's
  client id, the callback address and random values. Google thereby learns
  that this Google account signed in to LearnPilot, and when. Nothing is sent
  before that; no Google resource is loaded by any page.
- **Never logged:** additionally authorization codes, state, nonce, PKCE
  verifier, ID and access tokens, the client secret, subjects, provider
  emails. The consumption table holds keyed hashes only: exactly 64 bytes of
  lower-case hexadecimal, which the table itself enforces
  (`docs/ACCOUNTS_DESIGN.md` §5, merged).
- **Signing out** of LearnPilot does not sign the browser out of Google.
- **Deletion and retention:** unchanged and still undecided. Identities are
  not removed when an account is disabled.

LearnPilot has no privacy-policy page; 0.4 item 3.

### 8.6 Operator CLI

`src.manage_accounts` selects accounts by `--email` only
(`src/manage_accounts.py:264`). An account created through Google has none.

- **Selectors.** `disable`, `enable`, `sign-out` and `set-role` take exactly
  one of `--email EMAIL` and `--id N` (an argparse mutually exclusive group,
  required). `--id` is a positive decimal integer; an unknown id is
  `error: no account with id N`. The other commands keep `--email`: they
  act on a password or on an admin's second factor, and such an account has
  an email.
- **Output names the account by id, always.** For example
  `disabled account 42 (no email, student); existing sessions invalidated` and
  `role of account 17 (ivan@example.bg) is now teacher; existing sessions
  invalidated`. Today's messages print the email alone, which would read
  `None`.
- **`list`** gains two columns: `password` (`yes`/`no`) and `google` (the
  address Google reported, `yes` when it reported none, `-` without an
  identity). Subjects are not printed.
- **`set-role --role admin` is refused for an account without a password**
  (T5): an admin signs in with password and TOTP only, so the account would
  have no way in.
- Promoting a Google-created student to `teacher` by `--id` is allowed. It is
  an operator's grant, like any other, and the person then signs in with
  Google as a teacher.
- With this, no routine operation on a Google-created account needs SQL:
  finding it (`list`), disabling and enabling it, ending its sessions,
  changing its role. Removing or moving an identity is not routine and is
  section 10.

### 8.7 Deployment, rollback, restore

None of this is carried out by the PR. No credential is created, no provider
is registered and nothing is deployed.

1. **Prerequisite release.** PR #332, merged and not yet deployed: migration
   020 by the standard procedure (`docs/DEPLOYMENT.md` §20, and §28 as
   merged). It changes no behaviour. §28 is also the runbook for the table
   once PR #331 writes to it: no job removes its rows, nobody deletes them by
   hand, and every writer runs on one host with one clock.
2. **PR #331 with the provider disabled** (§20; no migration). Check: no
   Google button; `POST /auth/google/start` with a same-origin `Origin`
   answers 404.
3. **Log changes, and their check.** The Gunicorn format and logger class
   (restart) and the Nginx template (keep the Certbot lines; `nginx -t`;
   reload). Then, with the provider still disabled:
   - request `https://<host>/auth/google/callback?code=LOGTEST&state=LOGTEST`
     once, then about sixty times in a few seconds to pass the burst, and once
     while the service is stopped inside a deploy window;
   - each answer is a 302 to `/login?google=unavailable`, and the page it
     leads to is the ordinary sign-in page, with no message about Google;
   - send one request whose query contains a raw space
     (`…/callback?code=LOGTEST x&state=LOGTEST`, from a client that does not
     encode it): once through Nginx, and once on the server straight to
     `127.0.0.1:8000`; then the same with an apostrophe after the second
     value (`…/callback?code=LOGTEST x&state=LOGTEST'y`), which makes
     Gunicorn quote the text with the other mark (8.4);
   - **probe N, of Nginx's parser** (8.4, mechanism 1), and **probe D, of
     Werkzeug's wrapper as deployed** (8.4, mechanism 3): each once, with
     the exact bytes, the bound and the pass criteria of "The two body
     probes of step 3" below. They carry nothing real;
   - none of `LOGTEST`, `LPPROBE_NGINX_BODY` and `LPPROBE_DIRECT_BODY`
     appears in `journalctl -u learnpilot`, `/var/log/nginx/access.log` or
     `/var/log/nginx/error.log`, after all of the requests above;
   - the assumptions of 8.4 that are server facts: `ss -ltnp` shows Gunicorn
     listening on `127.0.0.1:8000` and on no other address, and the journal
     of the running start holds Gunicorn's line `Using worker: sync` [18].
     Gunicorn prints `gthread` there when `threads` is above 1. The third
     assumption, the request limit, is set in the code for every environment
     (`web/app.py:195`) and is pinned by tests; probe D shows its effect on
     the server;
   - `nginx -T | grep proxy_request_buffering` shows `on` and no `off`. This
     is the capacity setting; no statement about logs rests on it (8.4).
     Probe N's four criteria are stated for it (below);
   - `nginx -T | grep error_log` shows no level looser than `error`;
   - the versions are noted, because 8.3 and 8.4 were derived from sources
     of particular versions: `nginx -v`, and the lines for Flask, Werkzeug
     and Gunicorn of `/opt/learnpilot/venv/bin/pip freeze` (11.2 check 16).
4. **Outbound HTTPS** as the service user to the token endpoint and the JWKS
   URI. The unit does not restrict the network.
5. **Recovery milestone release** (section 10). Only from this build on can
   the provider be active in production (8.1; the gate is P2, approved).
6. **Register the client** in Google Cloud: type "Web application"; one
   authorized redirect URI, `https://<DZI_PUBLIC_HOST>/auth/google/callback`;
   no JavaScript origins; consent screen with the scopes `openid`, `email`,
   `profile` only.
7. **Configure**: `sudoedit /etc/learnpilot/learnpilot.env`, both variables.
   The secret is never typed on a command line or stored in the repository.
   Run the `--check-config` of §10, then restart.
8. **Smoke checks** (11.2), including the callback requests of step 3
   repeated with a real `state`. **The two body probes are not repeated,**
   here or anywhere, and never carry a real value.
9. **Off switch**: remove the two lines, restart. **Secret rotation**: new
   secret in Google and in the env file, restart; no session ends, sign-ins in
   flight end with `expired`.

**The two body probes of step 3. Specified here; not run, and not in
`docs/DEPLOYMENT.md`.** The pass that corrected the review's note 1 wrote
this specification and sent nothing to any server. It becomes a runbook
section only with PR #331's own documentation, after review.

*Rules for both probes.*

- **Nothing real.** The bytes below are the whole request. They hold no
  email address, no password, no cookie, no `state`, no code and no other
  credential, and none is added. `<host>` is the value of
  `DZI_PUBLIC_HOST`, and it is the only part that differs between
  deployments.
- **Never repeated with a real value.** Step 8, and check 10 of 11.2,
  repeat the callback requests with a real `state`. They do not repeat
  these two.
- **Raw HTTP/1.1.** Every line ends with CRLF, written `\r\n` below, the
  malformed chunk-size line included. A client that encodes or frames a
  body itself cannot send them. Through Nginx the client speaks TLS and
  does not offer HTTP/2.
- **`Connection: close`, and a client timeout of 5 seconds** in total, for
  connecting, sending and reading. At the timeout the client closes its
  socket, whatever it has received.
- **One run of each, inside the deployment window of step 3,** by the
  operator, on the server. Not in a loop, not from a monitoring job, and
  not a second time in that window, whatever the first result was.
- **One marker for each probe.** `LPPROBE_NGINX_BODY` and
  `LPPROBE_DIRECT_BODY` stand where a form body would be. They differ from
  each other and from the `LOGTEST` of the callback requests, so a hit in a
  log names the request that caused it. Each begins with a letter that is
  no hexadecimal digit, so that neither parser reads it as a chunk size.
- The client is any tool that writes exactly these bytes to a socket and
  enforces the timeout. Which tool is a matter for the runbook section,
  which this pass does not write.

*Why the bound.* Probe D reaches the one sync worker with no Nginx in front
of it. Gunicorn's chunk reader reads until it has the CRLF of the size line
[18]. Without that CRLF, and with a client that keeps the connection open,
the worker would wait until the client closes or until the 30-second abort,
and would serve nothing else meanwhile. The terminated line, `Connection:
close` and the client timeout are what exclude that. *From the source; not
run on a socket.*

*Probe N: Nginx's parser.* To `<host>`, port 443, over TLS:

```
POST /login HTTP/1.1\r\n
Host: <host>\r\n
Content-Type: application/x-www-form-urlencoded\r\n
Transfer-Encoding: chunked\r\n
Connection: close\r\n
\r\n
LPPROBE_NGINX_BODY\r\n
```

It carries no `Origin`: a request that did reach the application would be
answered 403 before any read. **The four criteria below are stated, as a
set, for the configuration that is deployed: `proxy_request_buffering
on`.** There, probe N passes when all four hold:

1. the answer has status 400;
2. Nginx's error log has one new entry for it with the text `client sent
   invalid chunked body` [16]. Nginx writes that entry at level `error`;
3. Gunicorn's access log, in the journal, has no entry for it: the request
   did not reach the application. Nginx's own access log shows it with 400;
4. `LPPROBE_NGINX_BODY` is in none of the three logs.

*Buffering on and off. Derived from Nginx's source [16]; not verified on
any server.* Nginx parses the framing of the client's body in both modes
(8.4, mechanism 1), so a chunk-size line of the client reaches Gunicorn in
neither. What differs is when Nginx turns to the application. With
buffering on it has read and parsed the whole body first, so a request
whose framing it refuses is never sent on, and criterion 3 follows. With
buffering off Nginx may open the connection to the application, and send
the request's headers, before the malformed body has arrived. An entry in
Gunicorn's access log is then possible, and its absence is not guaranteed.
The four criteria are therefore no test for a server with buffering off,
and this document states none for one. Step 3 reads the setting from
`nginx -T`, and a server that does not show `on` has not passed probe N.
This qualifies the probe and adds no logging requirement: 8.4 credits
Nginx's parser, not buffering, with keeping the client's framing from
Gunicorn.

*Probe D: Werkzeug's wrapper as deployed.* On the server, to
`127.0.0.1:8000`, in plain HTTP. Its control request comes first:

```
GET /login HTTP/1.1\r\n
Host: <host>\r\n
X-Forwarded-Proto: https\r\n
X-Forwarded-Host: <host>\r\n
X-Forwarded-For: 192.0.2.77\r\n
Connection: close\r\n
\r\n
```

The control must answer 200, the sign-in page. It shows that the
application accepts this host and these forwarding headers. It is needed
because a request with a host the application does not trust is answered
400 as well, before any body is read (E20): without the control, a wrong
`<host>` would meet every criterion below without the wrapper having been
reached. If the control does not answer 200, probe D is not sent. Then the
probe, with the same headers:

```
POST /login HTTP/1.1\r\n
Host: <host>\r\n
Origin: https://<host>\r\n
X-Forwarded-Proto: https\r\n
X-Forwarded-Host: <host>\r\n
X-Forwarded-For: 192.0.2.77\r\n
Content-Type: application/x-www-form-urlencoded\r\n
Transfer-Encoding: chunked\r\n
Connection: close\r\n
\r\n
LPPROBE_DIRECT_BODY\r\n
```

`192.0.2.77` is an address reserved for documentation. Nothing in the
application reads it. Gunicorn's access log prints it, which is where it
belongs and what identifies the two entries of this probe. Probe D passes
when all five hold:

1. the control answered 200;
2. the answer has status 400, and Gunicorn's access log has its entry:
   `POST /login` with 400, from `192.0.2.77`;
3. the journal has no parser-level entry for it: no line `Invalid request
   from ip=` and no line `Error handling request`. Either would mean that
   Gunicorn's own parser or error handler answered, and that the wrapper
   was not what produced the 400;
4. the journal has no traceback for it, in particular no `Exception on
   /login [POST]` and no `Socket error processing request.`;
5. `LPPROBE_DIRECT_BODY` is in none of the three logs.

*Any other result is not a pass,* for either probe: another status, a
missing or an additional log entry, a marker in any log, a client that ran
into its timeout, or a result that cannot be attributed to the probe.

- *A 403 for probe D is not a pass, and the probe is not sent again.* It
  means that the same-origin check refused the request before the body was
  read, so the wrapper was not reached. Its cause is diagnosed and
  corrected first. Only then is a new bounded verification run scheduled,
  and that run is complete in itself: the control `GET` first, then the
  probe, once, under the rules above.
- *A control that did not answer 200* is handled in the same way. Probe D
  is not sent, the result is not a pass, and the control belongs to the new
  run.
- *A finding, and activation stops there:* a 500, a traceback or a marker
  in a log. The deployed versions then do not behave as 8.4 describes.
- Anything else is not interpreted on the spot. It is recorded with the
  versions of the last item of step 3, and activation waits for it to be
  understood.

*What was checked without a server (E20).* The bytes of probe D and of its
control were given to Gunicorn 23.0.0's real parser and body reader
in-process, with no socket and no service, and from there to the
application of this worktree on a temporary database, with production's
proxy trust and host list. The control answered 200, the probe answered 400
with nothing logged and no throttle row, a request without `Origin`
answered 403, and with the limit removed, as a control, the probe answered
500 with the marker in a traceback. **That is not the server check.** It
ran no Nginx and no socket, and not the versions that production has.
Probe N was not run in any form.

**One instance per run: a requirement of this design.** The planned section
of `docs/DEPLOYMENT.md` states it in these terms:

- `preload_app = True` (`deploy/gunicorn.conf.py:27`). The application, and
  with it the instance value of 2.3, is created once, in the master, and
  every worker inherits it.
- One master per database. The unit starts one
  (`deploy/systemd/learnpilot.service:23`) and defines no reload command. A
  deploy is `systemctl restart learnpilot` (`docs/DEPLOYMENT.md` §11, §20),
  which stops the old master before the new one starts.
- A `HUP` sent to the master by hand keeps the preloaded application
  (`deploy/gunicorn.conf.py:24`–`26`; Gunicorn's `reload` re-reads the
  configuration and forks new workers from the application that is already
  loaded [18]). The instance value stays the same: nothing outstanding ends,
  and no second value appears.
- **Not supported while Google sign-in is enabled:** a second master on the
  same database. That is Gunicorn's `USR2` re-execution, a second unit, or a
  process started by hand beside the service. 3.7 says what two instance
  values would do: single use still holds, sign-ins and links in flight
  answer `expired` at the other master, and a retirement that reaches the
  other master reports `dead` for an object that its own master still
  accepts until it expires. No page claims a cancellation because of it.

Nothing is added to enforce this. It is the deployment that exists; the
design depends on it for the one statement of 3.7, and says so.

**Rollback.** PR #331 has no migration, so its code can be reverted on the
same database: accounts created through Google remain and cannot sign in under
the earlier build; their sessions are ordinary account sessions and stay
valid until they expire; linked identities are inert. Migration 020 is
forward-only like every other (§21): a rollback target must ship it, together
with its classification and manifest entries (§28, "Rollback", merged). The
records PR #331 wrote stay in the table and are not deleted for a rollback.

**Restore.** 3.6. The one script of §17, unchanged, with nothing added to it.

## 9. Prerequisite: consumption storage (PR #332, merged)

**Status.** Merged on `main` at `008c236`. Not deployed. Nothing calls it.
This branch does not contain it yet. The storage is specified by what that
PR merged. This section does not specify it again; it says where it is, and
how PR #331 uses it:

| What | Where (merged) |
| --- | --- |
| The table | `web/migrations/020_oidc_consumed_transactions.sql`; the same SQL block in `docs/ACCOUNTS_DESIGN.md` §5 |
| The service and what every caller owes it | `web/services/oidc_consumption.py` (its module documentation numbers the obligations); `docs/ACCOUNTS_DESIGN.md` §5 |
| Scope, guarantee, limits, review refinements | `docs/reviews/OIDC_CONSUMPTION_SCOPE.md`, whose last section overrides conflicting wording above it |
| Deployment, restore, rows, one clock, rollback | `docs/DEPLOYMENT.md` §28 |
| Tests | `tests/test_oidc_consumed_transactions_migration_sql.py`, `tests/test_oidc_consumption_service.py` |

Where this document and those files differ, those files are right. Three
things in them are relied on here, and were worded differently in the draft
scope that revision 2.1 was written against:

- **Python.** `consume` raises `AlreadyConsumed` only for a duplicate of the
  primary key, which it recognises by SQLite's result code; Python exposes
  that code from 3.11 on [22]. Production, and the supported verification
  workflow, run 3.12. On an older interpreter a duplicate propagates as the
  `sqlite3.IntegrityError` it is. The key is still refused, through the
  storage-error branch of each call site: `unavailable` at the callback and
  at the confirmation, `not_recorded` when retiring. No call site reads an
  error as an acceptance, or as a retirement, on any version. PR #331
  declares no minimum Python version for this.
- **The key.** The table accepts exactly 64 bytes of lower-case hexadecimal,
  by a character length and a byte length together. PR #331 builds no key
  itself: every key is the return value of `consumption_key`.
- **Retained rows.** Rows are removed by expiry only, by the cleanup inside
  `consume`. Rows of earlier boots can accumulate, and they are inert only
  under obligation 4 (3.4, "Rows").

**The API as merged.** PR #331 calls it as it is.

- `consumption_key(secret_key, purpose, value) -> str`, with `purpose` one
  of `PURPOSE_CALLBACK` (`"callback"`) and `PURPOSE_LINK_CONFIRM`
  (`"link-confirm"`), the only members of `PURPOSES`, and `value` 1–128
  characters of the URL-safe alphabet. A state and a pending-link id are 43
  such characters (2.1, 2.2).
- `consume(conn, key, *, now, expires_at) -> None`, inside a transaction the
  caller has begun. In this order it raises `ValueError` for a malformed key
  or time (a time is `type(...) is int`, from 0 to below 2^62),
  `RuntimeError` outside a transaction, and `Expired` when `now >=
  expires_at`, before any statement. Then it deletes the rows whose expiry
  lies more than `RETENTION_ALLOWANCE` (60) seconds back, and inserts;
  `AlreadyConsumed` is the duplicate. Every other `sqlite3.Error`
  propagates. It never commits and never rolls back. After
  `AlreadyConsumed` the caller's transaction is still usable, with that
  cleanup and the caller's earlier writes pending in it.
- `ConsumptionRefused` is the base of `AlreadyConsumed` and `Expired`.

**Two functions in `web/oauth_state.py`.**

- `begin_write(conn)` executes `BEGIN IMMEDIATE` and then returns one reading
  of `monotonic_seconds()`. It is the only place where a call site obtains
  the `now` it passes on.
- `record_use(conn, purpose, obj, now)` derives the key from the object's own
  secret value (the `state` of a transaction, the `id` of a pending link),
  with `consumption_key`, and calls `consume` with the object's own `exp`.
  The callback and `retire_linking` use it.

`confirm_link` is a service without Flask and does not import that module. It
calls `consume` itself, in the same way: the key from the pending link's
`id`, `expires_at` from its `exp`, and the `now` that its caller got from
`begin_write`. Nowhere is an expiry computed at the time of use.

**The three call sites**

| | Callback (1.5 H7) | Confirmation (1.6 step 5, 4.3) | Retiring (4.4) |
| --- | --- | --- | --- |
| Purpose | `callback` | `link-confirm` | `callback`, `link-confirm` |
| Transaction | its own | its own | its own, begun only when the caller has none pending |
| Commit | by itself, before anything else is looked at | together with the identity row and the throttle counter | by itself |
| `consume` returned and the `COMMIT` succeeded | accepted | steps 2–6 of 4.3 decided the outcome | `invalidated` |
| `AlreadyConsumed` | `failed` | `already_linked` or `failed` | `consumed`: used before, by something this call cannot name. Never read as a cancellation (4.4) |
| `Expired` | `expired` | `expired` | `dead` |
| `sqlite3.Error`, a failed `COMMIT` included | roll back; `unavailable` | roll back; `unavailable`; the pending link stays | roll back its own transaction; `not_recorded`; the caller goes on |
| `ValueError`, `RuntimeError`, anything else | roll back; `failed` | roll back; `failed`; the pending link stays | as the row above |
| `BEGIN IMMEDIATE` itself fails | no rollback; `unavailable` | no rollback; `unavailable`; the pending link stays | no rollback; `not_recorded` |
| A rollback that fails | caught and logged; the outcome is unchanged | the same | the same |
| A transaction of the caller is already open | does not arise: the hook runs before any other database work of the request | does not arise: the route has only read before it begins | nothing is executed; `not_recorded`; the caller's writes are untouched |

Only retiring can meet a transaction that is not its own, because only it
runs inside the request handling of other code.

**Ending a transaction that a call site began.** One rule for the three call
sites. "Roll back" in the table above always means this.

- *Ownership.* A call site owns a transaction from the moment its own `BEGIN
  IMMEDIATE` has returned, and not before. It commits and rolls back only a
  transaction that it owns. When `BEGIN IMMEDIATE` raised, it owns nothing,
  issues neither a `COMMIT` nor a `ROLLBACK`, and has not read the clock.
  Whatever was open before stays as it was, for its owner.
- *Where a failure arrives is SQLite's matter, not this rule's.* Two groups
  were observed, and they are not the same:
  - **`BEGIN IMMEDIATE` itself raises:** a write lock held elsewhere past
    the busy timeout; a `query_only` connection; and a connection that
    already holds a transaction, on which SQLite refuses a second `BEGIN`
    (E15, E16, E19). No transaction begins, the trace holds no `ROLLBACK`,
    and the clock is not read.
  - **`BEGIN IMMEDIATE` returns, and the first write raises:** a database
    file that the process cannot write, by its file mode or because it was
    opened read-only, in rollback-journal mode (E19). The connection is in
    a transaction, the call site owns it, and the clock was read once. The
    first writing statement then fails: the cleanup `DELETE` of `consume`
    at the callback and when retiring, the clearing of the throttle counter
    at the confirmation. The call site rolls back once.

  **The timing is not the same in every configuration, and nothing here
  says that it is.** In rollback-journal mode the second group behaved
  alike on SQLite 3.53.4 and 3.51.0, with the directory writable or not. In
  WAL mode it behaved the same way, with one exception: on 3.53.4, with the
  directory unwritable as well, `BEGIN IMMEDIATE` itself raised. That
  exception is the configuration E16 had used, and E16's general sentence
  about "a read-only database file" came from it (11.3). The test databases
  are rollback-journal files (`src/build_database.py:99`). A live database
  is in WAL mode (`web/db.py:16`–`18`), and the SQLite of the server was
  not examined. No rule, outcome or answer of this design depends on the
  group a failure falls into: each call site rolls back exactly what it
  began, and the answer is `unavailable` or `not_recorded` in both. The
  tests assert each failure where SQLite puts it for the database they run
  on (test plan, Method).
- *A `BEGIN IMMEDIATE` that returned is not always a held write lock.* On a
  database file that cannot be written, two connections both had `BEGIN
  IMMEDIATE` return at the same time (E19). The clock reading that follows
  is then not taken under a write lock. **Nothing can be accepted with
  it.** Acceptance is still `consume` having returned **and** a successful
  `COMMIT` (obligation 5), and on such a file the `INSERT` that `consume`
  has to make does not succeed. What that reading can safely decide by
  itself is expiry. `Expired` compares the reading with the object's own
  `exp` before any statement, reads and writes nothing, and is the same
  finding at every later reading of a clock that does not go back.
  `Expired` is not the only way such a request can end. The others are as
  they are everywhere else, and none of them is an acceptance:
  - the storage's `ValueError` for a malformed key or time, which `consume`
    raises before it looks at the expiry;
  - a `sqlite3.Error` from a write of the storage: its cleanup `DELETE`,
    which comes first, or the `INSERT`;
  - a `sqlite3.Error` from an earlier write of the call site itself. At the
    confirmation the throttle counter is cleared before `confirm_link` is
    called (1.6 step 5), so the transaction can fail there, and `consume`
    is then not reached.

  Each is answered as the table above says for its kind. Obligation 1 is
  met as PR #332 words it, the statement first and then one reading. This
  paragraph adds an observation, changes nothing in that contract, and is
  named for the focused review (0.4).
- *A `COMMIT` that fails.* The call site calls the connection's `rollback()`
  once and treats its write as not made: `unavailable` at the callback and
  at the confirmation, `not_recorded` when retiring. It does not retry, and
  it does not tell apart the failures after which SQLite leaves the
  transaction open from those after which SQLite has already rolled it back
  [23]; `rollback()` does nothing when no transaction is open [22].
- *What is not claimed after a failed `COMMIT`:* that nothing reached the
  disk. No outcome says so. `unavailable` asserts neither a link nor its
  absence, and `not_recorded` asserts only that this call does not vouch for
  a record. If the commit did take effect in spite of the error, the
  mistake is on the safe side at every call site: the callback refused a
  transaction that is in fact spent, the retirement claimed less than was
  true, and the confirmation answered `unavailable` for a link that exists.
  The next request then reads the truth from the database: the account page
  lists the identity, and a second POST of that pending link answers
  `already_linked` (4.3 step 1).
- *A rollback that fails.* It is caught, logged by its class name on
  `learnpilot.oidc`, and changes no outcome. Nothing is raised to the
  person. What is then still open on the connection is the call site's own
  transaction. Nothing writes or commits on that connection afterwards: the
  callback and the confirmation end the request, and after a retirement the
  callers of 4.4 clear or replace the session and answer. What they still
  execute is reads: the cancel route's read of the account, and a rendered
  page's own (`web/blueprints/account.py:165`–`166`, `:224`–`225`,
  `:293`–`294`, `:302`–`303`, `:343`–`344`). The transaction is discarded
  when the request's
  connection is closed at teardown (`web/app.py:494`): closing a connection
  commits nothing (E16: a row inserted in an open transaction was gone after
  the close).
- An exception that is not an `Exception`, such as the `SystemExit` of a
  worker abort, is raised again after the rollback at every call site.

**The caller obligations, and where each is met.** The obligations are
those of the merged service, numbered as its module documentation numbers
them. That text binds. The left column below is a label, not a restatement,
and nothing here narrows an obligation.

| # | Obligation | In PR #331 |
| --- | --- | --- |
| 1 | Lock, then clock | `begin_write`, at each of the three call sites. Retiring checks its precondition first and reads the clock only after `BEGIN IMMEDIATE` has returned (4.4 step 4). |
| 2 | One clock | `monotonic_seconds()` (2.3) is the only clock of this flow. One host, no time namespace of its own (`docs/DEPLOYMENT.md` §28, merged; 11.2 check 13). |
| 3 | A fixed, authenticated expiry | `exp` is a field of the signed session object, read after Flask has verified the cookie's signature and the object's shape (2.1), and passed unchanged by `record_use` and by `confirm_link`. An object is created once, with one `exp` and a random secret value, so one key has one expiry. An expired object is never retried with a later expiry. |
| 4 | Only the run that issued it | 1.5 H6 refuses another instance before the callback's `consume`. The first validation of 4.2 does so in 1.6 step 2, before `confirm_link` is called. `retire_linking` classifies such an object as `dead` in its step 2 and never passes it to `consume`. |
| 5 | Acceptance is return plus commit | 1.5 H7; 1.6 step 5. Retiring applies the same rule to its own claim: `invalidated` only after `consume` returned and the `COMMIT` succeeded, and no error is read as a retirement. |
| 6 | The caller finishes the transaction | Every path of the three call sites ends in a commit or a rollback of the transaction that call site began. Retiring never finishes a transaction it did not begin. |
| 7 | Keys only from `consumption_key` | `record_use` and `confirm_link`, each with one fixed purpose per kind of object. |
| 8 | Nothing else deletes rows | PR #331 contains no `DELETE` on the table; a static test pins that. No job, script or operator step is added (`docs/DEPLOYMENT.md` §28, merged). |

The tests that pin these are in the test plan (B1, B5).

## 10. Follow-up milestone: identity recovery tooling

Required before the provider is activated (A5). **Not part of PR #331, and
not designed here.** This PR implements no unlinking, and this document does
not say what removing or moving an identity does: that is the first operation
of its kind (`web/services/identities.py` deliberately has no such function)
and needs its own approved scope and security design.

Mistakes this design can produce and cannot undo:

- a Google identity confirmed onto the wrong account (4.6, staged link);
- a Google identity that created its own student account when the person
  meant to link it to an existing account (4.6);
- a Google-only account whose Google account is lost (6.3).

**Acceptance criteria for the milestone**

1. An operator can, without SQL, see the identities of an account and find
   the account an identity belongs to.
2. A supported, tested command path corrects each of the first two mistakes
   above, so that the Google identity afterwards signs in to the account its
   owner intended, or to none. What happens to the identity row, to an
   account left without any way to sign in, and to that account's attempts
   and assignments is defined by the milestone's own design.
3. Every session of every account the command changes ends, and the tool
   issues no session.
4. Accounts are named by id. The command shows what it will change and
   requires an explicit confirmation. No step selects, matches or links an
   account by email.
5. It grants no role and cannot make Google sign-in possible for an admin.
6. It is safe against a sign-in or a confirmation of the same identity at the
   same moment: after it reports success, the identity does not sign in to
   the old account.
7. Each use is written to an operator audit log (who, when, account ids).
   Subjects, emails and tokens stay out of application logs.
8. Tests on temporary databases; a runbook section in `docs/DEPLOYMENT.md`;
   independent security review.
9. It opens the activation gate of 8.1 (P2) in the same release. Nothing
   else enables the provider.

## 11. Verification

### 11.1 Automated

The inventory is `docs/reviews/GOOGLE_OIDC_TEST_PLAN.md`. In short:

- Temporary databases built from migrations and canonical data
  (`tests/support.py`); `data/questions.db` is never read.
- A fake Google with real cryptography: a test support module generates an RSA
  key pair, publishes its JWKS and mints ID tokens signed with RS256. It is
  mounted as the innermost transport, so Authlib's real client, the real
  signature verification, the real claim validation, the bounded reader and
  the provider gate all run. No network: any real socket is an error.
- "During the network call" is made deterministic: the fake token endpoint
  accepts a hook that runs while the application waits (disable the account,
  rotate the key, complete a competing callback), and can block on an event to
  produce an overrun without sleeping.
- The clock function and the instance value of `web/oauth_state.py` are
  replaced by the tests; the wall clock is moved independently to show that
  it does not matter.
- Races at the service level use threads with one connection each.

### 11.2 Real-provider and server checks: activation prerequisites, not done

Manual, after the recovery milestone, with a registered client. **None of
these has been performed.**

1. First sign-in creates a student; the second signs in the same account.
2. Cancel on Google's screen.
3. Link from a teacher account: confirmation page, wrong password, right
   password; sign out; sign in with Google as the teacher.
4. A Google account whose email equals an existing account's email: at
   sign-in, and at linking from another account (P1).
5. Two tabs; the back button after sign-in; reload of the callback address;
   a second confirmation.
6. The account chooser appears on every start; what it offers after a
   LearnPilot sign-out with the Google session still open.
7. A Workspace for Education account marked under 18.
8. *Not verified items:* the error Google returns for a reused code; whether a
   wrong PKCE verifier is refused for a web client; whether `iss` is always
   present on the callback; which issuer spelling the ID token carries;
   whether a token can carry an unverified address.
9. The session cookie attributes in the browser's developer tools.
10. The callback requests of the log check of 8.7 step 3, with a real
    `state` value; the forced 429 and the stopped-service case each end on
    the sign-in page. **The two body probes of that step are excluded.**
    They are run in step 3 only, once each, with their synthetic markers,
    and never with a real email, password, cookie, `state` or other
    credential (8.7, "The two body probes of step 3").
11. The pinned endpoints against the live discovery document (5.2).
12. A restart between start and callback ends with `expired`.
13. On the server, `time.get_clock_info("monotonic")` names
    `clock_gettime(CLOCK_MONOTONIC)`, and the service runs in the host's own
    time namespace. The service's interpreter is Python 3.11 or newer
    (`/opt/learnpilot/venv/bin/python --version`), so that a replay is
    reported as `AlreadyConsumed` and not as a storage error (section 9).
14. Cancel a pending link, sign out with one, and sign in as another account
    with one, each while the database is writable: the account page shows
    „Свързването с Google беше отказано.“ after the cancellation, and a copy
    of the earlier cookie is refused in all three (4.4). The failing case is
    covered by the automated tests only; it is not provoked on the server.
    Then, with two tabs on the confirmation page: confirm in the first and
    press „Отказ“ in the second. The page does not say „Свързването с Google
    беше отказано.“, and it lists the Google identity (3.3 row 19). It
    shows „Профилът ти вече е свързан с Google.“ instead (M2).
15. Gunicorn listens on the loopback address only, one master serves the
    database, and the worker is the sync worker: `ss -ltnp`; `systemctl
    status learnpilot` shows one master process; and the journal of the
    running start holds `Using worker: sync` (8.4, 8.7). These are the
    server facts among the assumptions of 8.4, and none has been checked.
16. The versions that the source-derived statements were not checked
    against: `nginx -v` (8.3 was read at `master`; the request-body handling
    of 8.4 at release 1.24.0 and at `master`), and the installed Flask,
    Werkzeug and Gunicorn (8.4 was run with 3.1.3, 3.1.9 and 23.0.0). The
    checks of 8.7 step 3 are what verify the behaviour on the server; the
    versions are recorded with their result.

### 11.3 Experiments behind this document

Scratch scripts outside the repository; they are not part of any PR. Python
3.12.15, Authlib 1.8.0, joserfc 1.7.5, requests 2.34.2, urllib3 2.8.0,
Flask 3.1.3, Werkzeug 3.1.9, SQLite 3.53.4; E8, E9, E13, E14 and E17 with
Gunicorn 23.0.0. E9–E12 were run for revision 2.1, E13–E15 for revision 2.2
and E16–E18 for revision 2.3. E15 and E16 ran against the merged service and
migration themselves, taken from `origin/main` at `008c236` with `git show`.
E17 also ran the application of this worktree at `64a84fd`, loaded as the
test suite loads it, on a temporary database. E19–E21 were run for the pass
after the review of revision 2.3. These entries record the author's
experiments; independent review checks are reported separately. E19 ran on
SQLite 3.53.4 and, with the system interpreter (Python 3.9.6), on SQLite
3.51.0; E20 with Gunicorn 23.0.0 on an export of `64a84fd`; E21 is a model
and runs no part of the application.

| | What was run | Result used |
| --- | --- | --- |
| E1 | Authlib's Flask registry with static metadata, three starts; the layouts of 2.1 and 2.2 through Flask's real session serializer. | Stored fields, 3600 s expiry, accumulation, verifier logged at DEBUG; the sizes of 2.8. |
| E2 | 38 ID tokens with real RS256 signatures through `joserfc.jwt.decode` and `CodeIDToken.validate`. | Signature, issuer and subject cases of 5.4 and 5.5. |
| E3 | Three onboarding shapes, 8 connections racing on 40 identities, real services, temporary database. | The table of 6.2. |
| E4 | `OAuth2Session` against a mocked transport. | Authorization parameters; client authentication in the header; no default timeout; error contents; a 302 and a body without `id_token` returned normally. |
| E5 | A loopback server that answers slowly; `requests` timeouts; a worker thread with a deadline and a slot bound; a bounded body reader; `fetch_token` with a response check. | The table and the guarantees of 5.7; the response rules of 5.3. |
| E6 | 32 ID tokens with real signatures and hand-built payloads (`NaN`, `Infinity`, floats, booleans, lists) through revision 1's checks and through those of 5.4. | 5.4 step 3. |
| E7 | A prototype of revision 2's consumption table (it had an `instance` column) on temporary databases: a randomised model with a monotonic and with a jumping clock, storage failures, consumption with the real identity insert. | 2.3 (monotonic against wall clock); 4.3 (record and identity in one transaction). E11 replaces it for the schema and the cleanup rule. |
| E8 | Gunicorn's real `handle_error` and logger driven in-process for a callback URL, with today's settings and the proposed ones. | 8.4. |
| E9 | Gunicorn's real request parser on raw request bytes, its failures given to the real `handle_error`; eleven parser exceptions built with secrets; three loggers (default, revision 2's, the filter). | 8.4. |
| E10 | A Flask application with the two hooks in the design's order and a route registered for `GET`; each method, same-origin and cross-origin. | The table of 1.2. |
| E11 | A prototype of the schema and service of `OIDC_CONSUMPTION_SCOPE.md` on temporary databases: column affinity and each `CHECK`; two instances (A, then B, then A again) under revision 2's rule and under the new one; a clock read before and after the write lock; a randomised model with three instances; 40 keys with 12 simultaneous callers each; a clock that restarts. | 3.3 rows 3 and 17; section 9; the scope's sections 1, 4, 5 and 7. |
| E12 | A prototype of the provider gate: failures raised in a worker thread, with and without containment; overruns holding both slots; 200 calls from 8 threads. | 5.7. |
| E13 | Gunicorn's parser exceptions built with texts that contain an apostrophe, a double quote, both or neither, as `str` and `bytes`; the real parser on request and header lines with an apostrophe; a cut at `'` alone against a cut at whichever mark comes first. | 8.4 (the quotation marks). |
| E14 | Gunicorn's real parser and WSGI environment with a Flask view that reads a form, **in a bare Flask application that sets no `MAX_CONTENT_LENGTH`**: three malformed chunked bodies and one complete body with its length; the body reader's exceptions given to Gunicorn's `log.exception` under the message filter. | 8.4: only that the filter leaves a traceback as it is. Its 500 with the password in the traceback does not describe this application, which sets the limit; E17 replaces it for that. |
| E15 | The retirement rules of 4.4 as a prototype around the merged `consume` and `consumption_key`, on temporary databases built from migration 020: both objects live; one expired; one already consumed; a caller transaction open, with every executed statement traced; the write lock held elsewhere; a missing table; a read-only connection; a failure forced on the second insert by a trigger; a commit that fails, for a reader in rollback-journal mode and through a connection subclass; a rollback that fails as well; and revision 2.1's rule on an open transaction. After each, whether the storage would still accept a copy of the object. | 4.4; 3.3 row 18; section 9. |
| E16 | The same prototype with `consumed` kept apart from `dead`, and the cancel route's decision of 1.6 beside revision 2.2's: a confirmation, then a cancellation from an empty session; a cancellation with the confirmed pending link still in the cookie; a confirmation that left a record and no identity; a callback made first with a copy of the start cookie, then the original's cancellation, then the successor's own key given to the storage; the same after the transaction expired; a second cancellation after one that was not recorded; two sessions of one account; a failing read of the account; a clean cancellation. Also: a `query_only` connection and a read-only database file, with the statements traced; and a connection closed with a transaction open. **The read-only file of E16 was one configuration only:** a WAL database whose file and directory were both unwritable, on SQLite 3.53.4. E19 replaces it for where a read-only database fails. | 1.6; 3.3 rows 19–22; 4.4; section 9. |
| E17 | Gunicorn's real parser and body reader as the WSGI input, with three malformed chunked bodies, an interrupted body with a length and a complete one. (a) A bare Flask application without and with `MAX_CONTENT_LENGTH`, read through `request.form`, `request.stream`, `request.get_data()` and `environ["wsgi.input"]`. (b) The application of this worktree on a temporary database, `POST /login` and `POST /account/password`, with its configured limit and, as a control, with the limit removed. Every logger captured at DEBUG. | 8.4 (the three mechanisms, the residual). |
| E18 | A prototype of the slot rule of 5.7: one guarded release function; a start that raises with no thread; a start that raises after the thread exists, before and after the worker has begun; a `SystemExit` after the thread exists; a release from both sides without the guard; 200 calls from 8 threads with the three kinds of start mixed. | 5.7, 5.8. |
| E19 | Where a write transaction fails on a database that cannot be written. Temporary databases with a table of their own, in rollback-journal and in WAL mode, on SQLite 3.53.4 and 3.51.0: `BEGIN IMMEDIATE`, a `DELETE`, an `INSERT` and a rollback, every statement traced, for a `query_only` connection, a write lock held by another connection, a file of mode 0400 in a writable directory, a file opened with `mode=ro`, and a file of mode 0400 in an unwritable directory. Also two connections that begin at the same time on a file of mode 0400 and then commit without a write. The merged service was not run; which statement of `consume` writes first was read from its source at `008c236`. | 4.4 step 7; section 9, "Ownership". |
| E20 | The bytes of probe D of 8.7 and of its control request, given to Gunicorn's real request parser and body reader in-process, with no socket and no service, and from there to the application exported from `64a84fd`, loaded as the test suite loads it on a temporary database, with `TESTING` off and production's proxy trust and host list. Variants: no CRLF after the marker and then the end of the input; no `Origin`; an untrusted `Host`; an untrusted `X-Forwarded-Host`; no `X-Forwarded-Proto`; the request limit removed; a header line without a colon. Every logger captured at DEBUG. Separately, the parser alone, with no application: asked for a second request on a connection whose first request left the malformed body unread, with and without `Connection: close`. | 8.7, "The two body probes of step 3"; 8.4, "The worker class". |
| E21 | An exhaustive model of the slot rule of 5.7 as corrected, for one to four tasks: every interleaving of the request threads and the workers, with an exception before `start()`, a `start()` that raises with and without a thread, and a worker whose first or second action raises before or after it marked that it had begun. Four controls, each with one element removed. It is an abstraction and runs no thread. | 5.7. |

E15 in figures. With a transaction of the caller open, the prototype executed
no statement, the caller's pending write survived and could be committed or
rolled back, and both objects were `not_recorded`. Under revision 2.1's rule
the same situation ended with the caller's write gone. In each of the seven
storage failures every live object was `not_recorded`. Where the experiment
then asked the storage (the failure on the second insert, and the two failed
commits), no row was left and a copy of either object was accepted. Where a
record had been committed, the copy was refused. With one object already
consumed and a failure on the other, the first kept its outcome (`dead` in
that prototype; `consumed` since revision 2.3) and its copy was refused, and
the second was `not_recorded` and its copy accepted. A failed
`BEGIN IMMEDIATE` left no transaction open. The prototype is not the
implementation: it has no session, no Flask and no logging, and the tests of
the test plan (B1, B5) are what pin the real function.

E16 in figures. In seven orderings revision 2.2's rule answered
`link_cancelled` and the rule of 1.6 did not:

| Ordering | Outcome | Answer by 1.6 |
| --- | --- | --- |
| a confirmation, then a cancellation from an empty session | `none` | `account_linked` |
| a cancellation with the confirmed pending link still in the cookie | `consumed` | `account_linked` |
| the same after a confirmation that left a record and no identity | `consumed` | `link_not_pending` |
| a callback made first with a copy of the start cookie, then the original's cancellation | `consumed` | `link_not_pending`; the storage then accepted the successor's key |
| the same once the transaction had expired | `dead` | `link_not_pending`; the successor's key was still accepted |
| a second cancellation after one that was not recorded | `none` | `link_not_pending`; the storage still accepted the earlier pending link |
| another session of the same account had linked meanwhile | `invalidated` | `account_linked` |

The codes in the last column are the ones that Gabriel has since approved
for rows 2 and 5 of 1.6 (M2, M1). For the failing read of the next sentence
the prototype answered `unavailable`; the approved answer of rows 1 and 3
is `cancel_unconfirmed` (M3).

A failing read of the account gave `unavailable` although the object was
`invalidated`. A clean cancellation gave `link_cancelled`, and the copy was
then refused. On a `query_only` connection `BEGIN IMMEDIATE` itself raised
("attempt to write a readonly database"), no transaction was open
afterwards, and no rollback was issued. **Corrected after the review of
revision 2.3:** revision 2.3 said the same of "a read-only database file".
E16 had run that case on a WAL database whose file and directory were both
unwritable, on SQLite 3.53.4, and the sentence generalised from that one
configuration. It does not hold for a read-only file in rollback-journal
mode (E19). Like E15, this is a prototype and not the implementation.

E19 in figures. A `query_only` connection and a write lock held elsewhere:
`BEGIN IMMEDIATE` raised, the connection was in no transaction, and the
trace held that one statement, in both journal modes and on both SQLite
versions. A file of mode 0400 and a file opened with `mode=ro`, in
rollback-journal mode: `BEGIN IMMEDIATE` returned, the connection was in a
transaction, the `DELETE` raised "attempt to write a readonly database", and
the trace held `BEGIN IMMEDIATE`, the `DELETE` and one `ROLLBACK`. That was
so on both versions, with the directory writable and unwritable. In WAL
mode the result was the same, except for a file of mode 0400 in an
unwritable directory on 3.53.4, where `BEGIN IMMEDIATE` itself raised; on
3.51.0 it returned there too. On a file of mode 0400 two connections both
had `BEGIN IMMEDIATE` return at once, where a writable control refused the
second with "database is locked", and each then committed without error.
The server's SQLite was not examined.

E20 in figures. The control request answered 200. Probe D answered 400,
with no line logged by any logger, no traceback and no row in
`login_throttle`; Gunicorn's parser had accepted the request, with a chunked
body and a connection to be closed. Without the CRLF and at the end of the
input it answered 400 in the same way. Without `Origin`, and without
`X-Forwarded-Proto`, it answered 403. **With an untrusted `Host` or
`X-Forwarded-Host` it answered 400 as well,** and so did the control, which
is why the control exists. With the request limit removed it answered 500,
and Flask logged `Exception on /login [POST]` with the marker in the
traceback. A header line without a colon was refused by Gunicorn's parser
before any application code ran. The parser alone, asked for a second
request after one that had left the malformed body unread and had not
asked to close the connection, raised `InvalidChunkSize`, an `OSError`
without an `errno`, whose text held the marker; after `Connection: close`
it raised `StopIteration` and read nothing. Nothing here ran Nginx, a
socket or a worker process of any class.

E21 in figures. With four tasks the model has 1,342,409 states, and in none
of them, for one to four tasks, was the count of free slots outside zero to
two, were more than two workers inside the function, was a slot given back
while its worker was inside the function, or did a run end with a slot not
given back. Thread objects were not bounded by two: three existed with
three tasks and four with four. Each control failed as it should. Without
the "begun" flag, three workers were inside the function. Without the guard
on the release, the count of free slots reached five. With an exception
before `start()` left unhandled, and with the worker's release outside a
`finally`, runs ended with a slot lost. The model is the author's own check
of the correction. It is not the review's model and not a review.

E17 in figures. (a) Without a limit, every way of reading the body answered
500 for the three malformed bodies, and for the malformed chunk size the
traceback held the submitted password. With the limit, `request.form`,
`request.stream` and `request.get_data()` answered 400 and nothing was
logged; the direct read of `wsgi.input` still answered 500 with the
password. An interrupted body with a length answered 200 on every path, as
a shorter body, with nothing logged. (b) The application answered 400 on
both routes for the three bodies, with nothing logged. With the limit
removed it answered 500, with the password in the traceback for the
malformed size. `/auth/google/link/confirm` does not exist yet and was not
run; the test plan covers it (B6).

E18 in figures. Ten starts that raised without a thread left two free slots
and ran no work. When the start raised after the thread existed, the free
count stayed between zero and two and returned to two once the workers had
ended; a `SystemExit` was raised again and left two. With a release from
both sides and no guard, the count reached three. 200 calls from 8 threads,
with the three kinds of start mixed, ended at two and never went above.

E7 and E11 ran before PR #332 existed, on prototypes of the draft schema,
which lacked the byte-length condition of the merged one. What they
established here (the cleanup rule, lock before clock, the clock's kind) is
unchanged in the merged service; the merged schema and service are tested by
PR #332's own suites.

Not covered by an experiment: Nginx (8.3, and its handling of request bodies
in 8.4; both are derived from its documentation and source, at the versions
named in 13), two Gunicorn masters on one database (3.7), a worker class
that keeps connections alive (8.4, "The worker class"; read from Gunicorn's
source), the two body probes on a real socket (8.7; E20 ran probe D's bytes
in-process only), and `time.monotonic()` on Linux.
The experiments ran on macOS, where Python reads another system clock for it,
and the consumption models passed plain integers as the clock. What the
design assumes of the Linux clock is taken from [19] and [20] and is check 13
of 11.2.

## 12. Proposed implementation files

**Changed in the design passes (documents only, uncommitted):** this file
(new), `docs/reviews/GOOGLE_OIDC_TEST_PLAN.md` (new) and
`docs/reviews/GOOGLE_OIDC_SCOPE.md`. Revisions 2.2 and 2.3, and the pass
after the review of revision 2.3, changed these three and nothing else.

**Bringing the branch onto `main`: a plan, not carried out.** No file was
deleted, moved, staged, merged or rebased in any design pass. Every step
below is Gabriel's, and the commits in it need his approval.

*The state.* The common ancestor is `3bfdf82`. The branch is one commit
ahead of it (`64a84fd`, which adds only `docs/reviews/GOOGLE_OIDC_SCOPE.md`),
and so is `main` (`008c236`, PR #332). Two things in this worktree stop a
merge or a rebase before it begins, and each has to be cleared first:

- **The untracked draft `docs/reviews/OIDC_CONSUMPTION_SCOPE.md`.** `main`
  tracks the reviewed file at the same path. **The draft holds nothing
  unique to retain.** Compared line by line, the 30 lines of it that the
  merged file lacks are its own status ("awaiting independent review",
  "nothing is implemented"), wording that the review replaced (retained rows
  described as few and short-lived; the schema not yet in the accounts
  design) and its execution notes.
- **The modified `docs/ACCOUNTS_DESIGN.md`**, which `main` changed as well.
  It has four local hunks. A three-way merge of the three versions on
  scratch copies (`git merge-file`) gave exactly two conflicts and applied
  the other two hunks cleanly:

  | # | Local hunk | In a merge | Its text today |
  | --- | --- | --- | --- |
  | 1 | The status list above §1: a new bullet, "Planned, not implemented … No such table, migration or service exists yet." | **Conflict.** `main` rewrote the end of the bullet before it and added "Done, as storage only: migration `020` …". | Its label and its last sentence are false since PR #332. |
  | 2 | §1, decision 2: "Amended 2026-10-11: the single use of a transaction is recorded in a dedicated server-side consumption table (§13, "Amendment"); planned, not implemented." | Clean. | True, except for the status phrase and for its cross-reference, which names a heading that `main`'s §13 does not have (step 6). **This is the one clean hunk that says "planned, not implemented".** |
  | 3 | §1, the deviation list: "(Amended 2026-10-11, §13: a consumption table records that a transaction was used. It stores no transaction, so this deviation stands.)" | Clean. | True. It carries no status phrase. |
  | 4 | §13: the block "Amendment … consumption table. Planned, not implemented." | **Conflict.** `main` added its own "AMENDMENT" and "Status of the amendment" paragraphs at the same place. | Superseded by `main`'s text, except for the two items below. |

  `main`'s §13 is authoritative for the storage contract and is kept whole.
  It lacks two things that hunk 4 holds, and both are to be kept:
  - **A1's recording order:** use is recorded for every callback whose state
    matches, before any other callback parameter is examined and before any
    outbound call, in a write that persists whatever follows;
  - **the pointer to this design** for how the sign-in PR uses the storage
    (sections 3 and 9).

*The procedure, in order.*

1. **Safety copies.** Copy the five files that differ from `HEAD` to a
   directory outside the repository: this file and the test plan
   (untracked), the Google scope and `docs/ACCOUNTS_DESIGN.md` (modified),
   and the draft. The two untracked Google documents exist nowhere else
   until they are committed.
2. **Set the local `docs/ACCOUNTS_DESIGN.md` change aside.** Save it as a
   patch outside the repository (`git diff -- docs/ACCOUNTS_DESIGN.md`),
   then restore the file to `HEAD`. Restoring is preferred to committing:
   committed, hunks 1 and 4 become conflicts inside the merge, and most of
   their text is no longer true.
3. **Clear the obsolete draft.** Remove the untracked
   `docs/reviews/OIDC_CONSUMPTION_SCOPE.md` from the worktree. Step 1 kept a
   copy, although nothing in it is needed.
4. **Preserve the Google documents.** A merge leaves all three alone: `main`
   has no file at any of their paths. A rebase refuses to start while the
   tracked scope is modified. So either commit the three Google documents on
   the branch first, once their review allows it, or set the scope's change
   aside and put it back after step 5.
5. **Bring the branch onto `main`.** With steps 2–4 done there is nothing to
   resolve: the two commits touch no common path.
6. **Re-apply to `docs/ACCOUNTS_DESIGN.md` only what `main` lacks,** by
   hand, around `main`'s text and without changing a line that PR #332
   wrote:
   - hunk 1: not re-applied. `main`'s two bullets say it, in the merged
     state;
   - hunk 2: re-applied, with "planned, not implemented" replaced by the
     merged state: the storage exists as migration 020 (§5), and the code
     that has to use it does not. **Its cross-reference is corrected as
     well.** The hunk points to `§13, "Amendment"`. That is the heading of
     the local block of hunk 4, which is not re-applied, and `main`'s §13
     has no heading of that name. `main`'s paragraph begins "AMENDMENT
     (approved by Gabriel, 2026-10-11) — a dedicated consumption record."
     The re-applied sentence points to that paragraph, in `main`'s words;
   - hunk 3: re-applied as it is;
   - hunk 4: not re-applied as a block. After `main`'s "Status of the
     amendment" paragraph, add A1's recording order as a requirement on the
     sign-in PR, and the pointer to this design, marked as a design under
     review that does not override §13.
7. **Check.** `git diff origin/main -- docs/ACCOUNTS_DESIGN.md` shows the
   three additions of step 6 and no line of PR #332 removed or changed. §5's
   SQL block is `main`'s, byte for byte; a test of PR #332 compares it with
   the migration. `docs/reviews/OIDC_CONSUMPTION_SCOPE.md` is the tracked
   file. The three Google documents equal the copies of step 1. PR #332's
   two test modules pass.

**Prerequisite PR #332 (merged):** the files of section 9.

**PR #331 — new**

| File | Content |
| --- | --- |
| `web/oauth_state.py` | The transaction and the pending link: create, match, shape checks, cookie budget; the service instance and `monotonic_seconds`; `begin_write`, `record_use`, and `retire_linking` with its result (section 9, 4.4). Flask session, the request's connection, the consumption service and the standard library only. |
| `web/account_session.py` | `issue_account_session`, `issue_pending_second_factor`, `end_session`, the attempt-marker prefix, role homes, the account throttle key. Moved; changed only by the call to `retire_linking` (7.1). |
| `web/services/google_oidc.py` | Pinned metadata, configuration rules, the activation constant (P2), authorization URL, the provider gate (lock, slots, worker, budget, cooldown), code exchange, response rules, key cache, ID-token checks. No Flask, no database. |
| `web/services/oidc_accounts.py` | `sign_in_or_onboard`, `bound_account`, `link_decision`, `confirm_link`, email and name rules. Explicit connection, no commit. |
| `web/blueprints/oidc.py` | The five routes and the template flag. |
| `web/templates/account.html`, `account_link_confirm.html` | The account page and the confirmation page. |
| `web/static/img/brand/` | Google's "G" asset, unmodified. |
| `tests/google_oidc_support.py` and the test modules of the test plan, part B | |

**PR #331 — changed**

| File | Change |
| --- | --- |
| `requirements.txt` | 5.1. |
| `web/app.py` | Register the blueprint; the `verify_oauth_state` hook; read the two settings. |
| `web/request_integrity.py` | The comment: `OAUTH_STATE` is in use. |
| `web/production.py` | Validation of the two settings; the activation gate (8.1, P2); the `learnpilot.oidc` log handler. |
| `web/blueprints/account.py` | Import from `web/account_session.py`; `GET /account`; the Google messages and the signed-in redirect on `/login`; sign-out through `end_session` (1.7); the no-password notice on `/account/password`. |
| `web/templates/login.html`, `account_password.html`, `base.html`, one stylesheet | 1.7. |
| `deploy/nginx/learnpilot.conf`, `deploy/gunicorn.conf.py`, `deploy/learnpilot.env.example` | 8.1, 8.3, 8.4. |
| `src/manage_accounts.py` | 8.6. |
| `docs/ACCOUNTS_DESIGN.md` | Status; §5 (the issuer sentence); §13 (implemented, with the guarantees of 3.4); §15 (8.5); §18 (test rows). |
| `docs/AUTH_ROUTE_CONTRACT.md` | The routes; the fourth enforcement step; `oauth_state` in use; the callback as a state-changing GET. |
| `docs/DEPLOYMENT.md` | §5, §6, §10, §12, §18, §22, and a new section for Google sign-in (8.7). |
| The existing tests listed in the test plan, part C | |

**Not changed by PR #331:** migrations, `data/canonical`,
`web/services/identities.py`, `web/services/accounts.py`, `web/access.py`,
`web/services/login_throttle.py`, `web/services/oidc_consumption.py` (PR #331
calls it and changes nothing in it), `deploy/learnpilot-restore.sh`, the
vault.

## 13. Sources

Retrieved 2026-10-11; 22–25, the buffering directive in 14, the
request-body files in 16 and the files that 18 lists for the pass after the
review of revision 2.3 on 2026-10-12.

1. Google, *OpenID Connect* (server flow, ID-token claims and validation,
   discovery, `prompt`):
   <https://developers.google.com/identity/openid-connect/openid-connect>
2. Google's discovery document and signing keys (contents and cache headers;
   no `end_session_endpoint`):
   <https://accounts.google.com/.well-known/openid-configuration>,
   <https://www.googleapis.com/oauth2/v3/certs>
3. Google, *Using OAuth 2.0 for Web Server Applications* (redirect URI rules,
   errors):
   <https://developers.google.com/identity/protocols/oauth2/web-server>
4. Google, *OAuth 2.0 for Mobile & Desktop Apps* (PKCE parameters):
   <https://developers.google.com/identity/protocols/oauth2/native-app>
5. Google, *Using OAuth 2.0 to Access Google APIs* (token size limits):
   <https://developers.google.com/identity/protocols/oauth2>
6. Google, *Sign in with Google branding guidelines*:
   <https://developers.google.com/identity/branding-guidelines>
7. Google, *Brand verification*:
   <https://developers.google.com/identity/protocols/oauth2/production-readiness/brand-verification>
8. Google Workspace Admin Help, *Manage access to unconfigured third-party
   apps for users designated as under 18*:
   <https://knowledge.workspace.google.com/admin/getting-started/editions/manage-access-to-unconfigured-third-party-apps-for-users-designated-as-under-18>
9. Authlib documentation, *HTTP Clients* (OAuth2Session, PKCE, compliance
   hooks, ID-token parsing with joserfc and `CodeIDToken`) and *Flask
   Integration*:
   <https://docs.authlib.org/en/latest/oauth2/client/http/index.html>,
   <https://docs.authlib.org/en/stable/oauth2/client/web/flask.html>;
   Authlib 1.8.0 source as installed from PyPI
10. RFC 6749, *The OAuth 2.0 Authorization Framework*, §4.1.2:
    <https://www.rfc-editor.org/rfc/rfc6749.html>
11. *OpenID Connect Core 1.0*, §2, §3.1.3.7, §15.5.2, §16.9:
    <https://openid.net/specs/openid-connect-core-1_0.html>
12. RFC 9700, *Best Current Practice for OAuth 2.0 Security*, §2.1.1, §4.5,
    §4.7: <https://www.rfc-editor.org/rfc/rfc9700.html>
13. Google, *OAuth 2.0 best practices* (background; not cited above):
    <https://developers.google.com/identity/protocols/oauth2/resources/best-practices>
14. Nginx documentation: `error_page`, `absolute_redirect`
    (<https://nginx.org/en/docs/http/ngx_http_core_module.html>); `error_log`
    (<https://nginx.org/en/docs/ngx_core_module.html>); `access_log`,
    `log_format` (<https://nginx.org/en/docs/http/ngx_http_log_module.html>);
    `proxy_intercept_errors` and `proxy_request_buffering` ("the entire
    request body is read from the client before sending the request to a
    proxied server": a statement about when the request is sent, and about
    nothing else)
    (<https://nginx.org/en/docs/http/ngx_http_proxy_module.html>); `return`
    (<https://nginx.org/en/docs/http/ngx_http_rewrite_module.html>);
    `add_header`
    (<https://nginx.org/en/docs/http/ngx_http_headers_module.html>)
15. Nginx documentation, `ngx_http_limit_req_module` (`burst`, `nodelay`,
    `limit_req_log_level`, `$limit_req_status`):
    <https://nginx.org/en/docs/http/ngx_http_limit_req_module.html>
16. Nginx source, `master` branch (<https://github.com/nginx/nginx>):
    `src/http/modules/ngx_http_limit_req_module.c` (the refusal is logged at
    the limit's level on the request's log);
    `src/http/ngx_http_core_module.c`, `ngx_http_update_location_config` (the
    location's error log is installed for the request);
    `src/http/ngx_http_request.c`, `ngx_http_log_error` (`request: "…"` with
    the request line); `src/http/ngx_http_upstream.c`,
    `src/event/ngx_event_connect.c`, `src/core/ngx_connection.c` (levels of
    upstream and connection errors);
    `src/http/ngx_http_special_response.c` (`error_page`: a path is an
    internal redirect, `@name` a named location, anything else a `Location`).
    For request bodies (8.4), read at the tag `release-1.24.0` and at
    `master`, with the same result at both:
    `src/http/ngx_http_request_body.c`,
    `ngx_http_request_body_chunked_filter` (Nginx parses the client's chunks
    with `ngx_http_parse_chunked`; an invalid one is logged as "client sent
    invalid chunked body", a text without body bytes, and answered with
    `NGX_HTTP_BAD_REQUEST`); `src/http/modules/ngx_http_proxy_module.c`
    (the default headers `Content-Length: $proxy_internal_body_length` and
    `Transfer-Encoding: $proxy_internal_chunked` are set by Nginx; the body
    is passed on unbuffered only when request buffering is off, and a
    chunked one only with HTTP/1.1 towards the upstream;
    `ngx_http_proxy_body_output_filter` then writes the chunk framing).
    Neither version is known to be the server's
17. urllib3 change log, 2.2.0 ("Added support for `HTTPResponse.read1()`"):
    <https://github.com/urllib3/urllib3/blob/main/CHANGES.rst>
18. Gunicorn 23.0.0 source as installed from PyPI:
    `gunicorn/workers/base.py` (`handle_error`, `handle_abort`),
    `gunicorn/workers/sync.py` (`handle`, `handle_request`),
    `gunicorn/glogging.py` (`atoms`, the level methods, `setup`),
    `gunicorn/http/errors.py` (the text of each parser exception and of the
    body reader's `NoMoreData`, `InvalidChunkSize` and
    `ChunkMissingTerminator`, which are subclasses of `IOError`),
    `gunicorn/http/message.py` (`parse_request_line`, `parse_headers`),
    `gunicorn/http/wsgi.py` (`wsgi.input_terminated` is set to true),
    `gunicorn/arbiter.py` (`reload`: the configuration is read again and new
    workers are forked; `reexec`, the handler of `USR2`: a new master beside
    the old one) and `gunicorn/app/base.py` (`wsgi()` returns the
    application it has already loaded). For the pass after the review of
    revision 2.3 also: `gunicorn/http/parser.py` (`Parser.__next__` reads
    and discards the unread body of the previous message before it parses
    the next one, unless that message asked to close the connection);
    `gunicorn/workers/sync.py` (`handle` asks the parser for one request,
    `handle_request` forces the response to close, and the client is closed
    afterwards); `gunicorn/workers/gthread.py` (`handle` asks the same
    parser again on a connection that was kept alive, and logs an `OSError`
    without a socket `errno` as `Socket error processing request.` with its
    traceback); `gunicorn/http/body.py` (`parse_chunk_size` reads until it
    has a CRLF, and raises `InvalidChunkSize` for a size that is not
    hexadecimal); `gunicorn/config.py` (the sync worker ignores `keepalive`;
    with `threads` above 1 the sync worker class becomes the threaded one,
    and `worker_class_str` then reads `gthread`); `gunicorn/arbiter.py`
    (the line `Using worker: %s` at start); `gunicorn/workers/base.py`
    (`handle_abort` and `handle_quit` set the worker's `alive` flag to
    false and raise `SystemExit`)
19. Python documentation, `time.monotonic()`:
    <https://docs.python.org/3/library/time.html#time.monotonic>
20. Linux man-pages, `clock_gettime(2)` (`CLOCK_MONOTONIC`) and
    `time_namespaces(7)`, source text from
    <https://git.kernel.org/pub/scm/docs/man-pages/man-pages.git>
21. Nginx change log, 1.21.1 ("now nginx always returns an error if spaces or
    control characters are used in the request line"):
    <https://nginx.org/en/CHANGES>
22. Python documentation, `sqlite3`: `Connection.in_transaction`;
    `Connection.rollback()` and `commit()` ("If … there is no open
    transaction, this method does nothing"); a transaction is opened
    implicitly only before an `INSERT`, `UPDATE`, `DELETE` or `REPLACE`;
    `sqlite_errorcode` ("Added in version 3.11"):
    <https://docs.python.org/3/library/sqlite3.html>
23. SQLite documentation, *Transaction* ("When COMMIT fails in this way, the
    transaction remains active"; the errors after which a transaction "may or
    may not be rolled back automatically"; a `ROLLBACK` after an automatic
    rollback "will fail with an error, but no harm is caused by this"):
    <https://www.sqlite.org/lang_transaction.html>
24. Werkzeug 3.1.9 source as installed from PyPI: `werkzeug/wsgi.py`,
    `get_input_stream` (with `wsgi.input_terminated` in the environment and a
    `max_content_length`, the stream is wrapped in `LimitedStream`; without a
    limit the server's stream is returned as it is) and `LimitedStream`
    (`readinto` catches `OSError` and `ValueError` from the wrapped stream
    and calls `on_disconnect`, which raises `ClientDisconnected`). The
    review of revision 2.2 ran the same behaviour on 3.1.8. Neither version
    is known to be the server's
25. CPython 3.12.15 source as installed, `threading.Thread.start`: the
    thread is created by `_start_new_thread`, whose failure is raised again
    after the thread object is taken off the list of starting threads, and
    `start()` then waits on the thread's started event

Related: RFC 7636 (PKCE) and RFC 9207 (the `iss` response parameter) are
referred to through [4], [12] and Google's discovery document [2]; they were
not read separately.

Repository files read for this revision, in addition to those of revision 1
(`AGENTS.md`; the scope documents; `docs/ACCOUNTS_DESIGN.md`;
`docs/AUTH_ROUTE_CONTRACT.md`; `web/app.py`; `web/access.py`;
`web/request_integrity.py`; `web/redirects.py`; `web/public_url.py`;
`web/production.py`; `web/wsgi.py`; `web/db.py`; `web/request_db.py`;
`web/blueprints/account.py`; the accounts, identities, throttle and TOTP
services; migrations 012, 013, 019; the deployment templates; the sign-in
templates): `docs/DEPLOYMENT.md` §10–§12, §17, §18, §20–§22, §26, §27;
`web/services/lifecycle.py` (clock evidence and the writer transaction);
`web/migrations/018_assignment_lifecycle.sql`; `src/canonical_tables.py`;
`data/canonical/manifest.json`; `src/manage_accounts.py` (parser, lookup,
every command's output); the file list of commit `3bfdf82`.

For revision 2.1 also: `web/db.py` and `web/request_db.py` (connection policy
and busy timeout); `web/blueprints/account.py` again (every place a session is
replaced or cleared); `web/production.py` (`production_config_errors`);
`web/services/login_throttle.py`; `web/migrations/019_user_identities.sql` and
the diff of commit `3bfdf82` (how a migration enters the manifest, the
classification and the tests); `tests/test_user_identities_migration_sql.py`
(the comparison with the design's SQL block); `docs/DEPLOYMENT.md` §17 and
§27; `deploy/gunicorn.conf.py`; `requirements.txt`.

For revision 2.2, from `origin/main` at `008c236` with `git show` (not in
this worktree): `web/services/oidc_consumption.py`;
`web/migrations/020_oidc_consumed_transactions.sql`;
`docs/reviews/OIDC_CONSUMPTION_SCOPE.md`; the changes of that commit to
`docs/ACCOUNTS_DESIGN.md` (the status list, §5, §13 and the test table of
§18) and to `docs/DEPLOYMENT.md` (§28); the test names of
`tests/test_oidc_consumption_service.py`. In this
worktree: `web/blueprints/account.py` again (what each route has committed
before it calls a session function); `web/request_db.py` and `web/db.py`
(transaction control and the busy timeout of the request's connection);
`web/services/accounts.py` (`verify_password` only reads);
`web/services/login_throttle.py` (what a recorded failure and a cleared
counter write); `web/app.py` (the teardown that closes the connection);
`deploy/nginx/learnpilot.conf` (request buffering).

For revision 2.3: the independent review of revision 2.2 (verdict: request
changes; one required finding and four groups of smaller ones). In this
worktree: `web/app.py` (the request limit at `:195`, the error handlers, the
same-origin hook, the teardown); `deploy/gunicorn.conf.py` (`bind`,
`preload_app` and its comment); `deploy/nginx/learnpilot.conf` (`:44`–`71`:
the body limit, the HTTP version towards the application, both buffering
directives); `deploy/systemd/learnpilot.service` (one `ExecStart`, no reload
command); `docs/DEPLOYMENT.md` §10, §11 and §20 (how the service is
restarted); `tests/test_production_deployment.py` (what is pinned today:
`bind`, one worker, the request limit); `tests/support.py` and
`tests/auth_helpers.py` (to load the application for E17);
`web/services/identities.py` (`list_identities`); `requirements.txt` (the
version ranges); the local changes of `docs/ACCOUNTS_DESIGN.md` against the
common ancestor `3bfdf82` and against `origin/main`, and the draft
`docs/reviews/OIDC_CONSUMPTION_SCOPE.md` against the merged file (section
12).

For the pass after the review of revision 2.3: that review's report
(verdict: approve with non-blocking notes, technical specification only;
seven notes). In this worktree: `deploy/gunicorn.conf.py` (`worker_class`,
`threads`, `keepalive`); `deploy/nginx/learnpilot.conf` (the `/login`
location and the forwarding headers); `web/app.py` (the same-origin check);
`web/production.py` (`trusted_proxy`, the trusted host list); `web/db.py`
(why the journal mode is not set there); `src/build_database.py:99` (a
built database is a rollback-journal file); `tests/support.py` (how a test
database is built); `tests/test_production_deployment.py:737` and `:765`
(the pins of the bind, the worker class, the threads and the request
limit). From `origin/main` at `008c236` with `git show`:
`web/services/oidc_consumption.py` (the order of the statements in
`consume`) and §13 of `docs/ACCOUNTS_DESIGN.md` (its amendment heading).
The scratch scripts of E16 from the earlier pass, to see which read-only
configuration it had run.
