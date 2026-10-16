# LearnPilot — Accounts Design Specification

Status: **approved design; phase 1 is implemented** (see "Implementation
status" and "Current status and next dependency" below, which override any
older wording in the sections). It is the authoritative implementation
specification for the Accounts phase of `docs/REFACTOR_ACCOUNTS_ROADMAP.md`;
where the two differ on accounts, this document wins (the approved deviations
are listed in §1).

**Implementation status.** Phase 1 is implemented through PR 10: accounts,
login/session security, assignment and practical-review ownership, admin TOTP,
the production cutover (PR 9) and the physical removal of the shared
tester/admin password bridge (PR 10). Account authentication is now the only
authentication in every environment. Sections that describe the shared
passwords, `tester_authenticated` / `admin_authenticated`, `ui_profile`,
`/tester/login`, `/admin/login`, `/profile/<profile>` or
`DZI_LEGACY_SHARED_LOGIN` (§2 and §8 in particular) record the starting point
and the transition and are kept as history; none of it exists in the code. The
current contract is `docs/AUTH_ROUTE_CONTRACT.md`. Phase 2 (student accounts)
has started with account-owned quiz attempts (migration 015, §24).

**Current status and next dependency.** Do not redo any of this:

- Done: `users` with `users.password_hash` (the roadmap's separate
  `password_credentials` table was superseded and does not exist), the
  `manage_accounts` CLI, login/logout/throttle, assignment ownership (014),
  owner-scoped teacher and practical-review routes, admin TOTP with recovery
  codes (016), the cutover and the removal of the shared passwords.
- A student account can already be created by the CLI (`--role student` is its
  default) and sign in with a password. Attempt ownership is done (§24). What
  remains for students is onboarding (how they get accounts), not the account,
  the login or ownership.
- Application startup only checks that every migration is recorded and refuses
  to start otherwise; it never migrates.
- Done: the migration runner supports explicit table-rebuild migrations
  (`-- learnpilot-migration: table-rebuild`, `docs/DEPLOYMENT.md` section 9).
- Done: migration `015_quiz_attempts_user.sql` and account-owned quiz attempts
  (§5, §16, §24). A new attempt needs a signed-in account; attempts
  from before accounts stay `user_id` NULL and bound to their browser.
- Done: the session cookie is `SameSite=Lax` (§6, §14), as its own PR. It is
  the prerequisite of OIDC sign-in (§13), not a part of it.
- Done: migration `019_user_identities.sql` and the storage service
  `web/services/identities.py` (§5, §13). Google sign-in reads and writes
  identities through `web/services/oidc_accounts.py`; the table stays empty
  until the provider is enabled somewhere.
- Done: migration `020_oidc_consumed_transactions.sql` and the storage service
  `web/services/oidc_consumption.py` (§5, and the amendment in §13). Google
  sign-in calls it at three call sites (§13, "Google sign-in as implemented").
- Done: migration `021_registration_schema.sql`, the profile service
  `web/services/profiles.py` and the registration form validators
  `web/services/registration_forms.py` (email registration PR 1;
  `docs/reviews/EMAIL_REGISTRATION_DESIGN.md` section 6.1,
  `docs/reviews/EMAIL_REGISTRATION_PR1_STATUS.md`). Storage and validation only:
  `users.email_verified_at` (NULL for every account) and five runtime tables,
  with no route, no mail and no caller. `users.role` stays the only authority.
- Done (merged, #337): the registration service
  `web/services/registration.py` and the additions `hash_new_password`,
  `password_hash_is_current` and `create_verified_user` in
  `web/services/accounts.py` (email registration PR 3;
  `docs/reviews/EMAIL_REGISTRATION_PR3_STATUS.md`). `create_verified_user`
  accepts role `student` only, is the one function that sets
  `email_verified_at`, and returns `None` (writing nothing) when the address
  already has an account. Migration 022 adds `registration_sends.issuance_id`,
  the random identity of an issuance. `_USER_COLUMNS` does not read the column.
- Merged (#338), **not activated**: student
  registration routes (email registration PR 4;
  `docs/reviews/EMAIL_REGISTRATION_PR4_STATUS.md`, `docs/AUTH_ROUTE_CONTRACT.md`
  "Student registration"). A verified registration creates a `student` with
  `email_verified_at` set and a profile, and signs nobody in (D7). Production
  keeps every `/register...` route at 404 whatever is configured (the build
  gate `registration.PRODUCTION_ACTIVATION`, off); no teacher registration
  exists. Sign-in, the throttle, admin TOTP, Google sign-in and every
  CLI-created account are unchanged; `manage_accounts list` shows the
  verification date and the declared type.
- Merged (#339), **not activated**: registration operations (email registration
  PR 5; `docs/reviews/EMAIL_REGISTRATION_PR5_STATUS.md`): the cleanup unit and
  timer, the restore hold and the logical scrub of backup copies. No schema change.
- Implemented in draft, **not activated** and not independently reviewed: the
  account profile flow (email registration PR 6;
  `docs/reviews/EMAIL_REGISTRATION_PR6_STATUS.md`, `docs/AUTH_ROUTE_CONTRACT.md`
  "Account profile"). Migration `023_profile_required.sql` adds the nullable column
  `users.profile_required_at` (NULL for every account that exists); `/account/profile`,
  the profile on `/account`, the mark written when Google onboarding creates a
  student, and the profile check before a new attempt exist behind the build gate
  `profiles.PRODUCTION_ACTIVATION`, off. While it is off none of them is reachable or
  visible, and attempt creation is unchanged. A profile is a declaration and
  `users.role` stays the only authority.
- **Implemented, not activated: Google OIDC sign-in** (PR #331; §13, "Google
  sign-in as implemented"; specification
  `docs/reviews/GOOGLE_OIDC_DESIGN.md`). Automatic student onboarding, linking
  by confirmation for students and teachers, no Google for admins. It is
  disabled unless both credentials and a public base URL are configured, and
  **a production start of this build keeps it disabled whatever is
  configured** (the activation gate, §13). No provider was registered and no
  real credential exists. What activation still needs is listed in
  `docs/DEPLOYMENT.md`, section 29.
- **Not implemented:** Microsoft sign-in, unlinking and identity recovery
  (required before Google sign-in is activated), a local password or a second
  identity for an account created through Google.
- **Next:** the remaining phase-2 items of §17.
- The DZI visibility gate depends on account identity (a signed-in principal),
  not on OIDC specifically; OIDC is one way to obtain student accounts.

Implement it one PR at a time, in the order of §17. The roadmap's rules for
every PR (feature branch, numbered migrations only, tests never touch
`data/questions.db`, Bulgarian UI text, no merge without Gabriel's go-ahead)
apply unchanged.

## 1. Executive summary

- **Model:** one `users` table with a single `role` column
  (`student | teacher | admin`), email + password as the local credential, and
  no other account tables in the first migration.
- **Session:** the existing Flask signed cookie carries only `uid`, a per-user
  random `sk` (session key) and `iat`. Role, active state and name are read
  from the row once per signed-in request.
- **Seam:** `web/access.py` already isolates every access decision behind
  `current_principal()`. Accounts plug in there. The legacy tester/admin
  session keys kept working as a bridge until the last PR of phase 1 (PR 10),
  which removed them.
- **Rollout:** small PRs for teacher/admin accounts and assignment ownership
  (phase 1). Student accounts are phase 2 and additionally need one
  migration-runner fix (§16).
- **Registration:** no public self-registration in v1. Accounts are created by
  a server-side CLI, which is also how the first admin is bootstrapped.

**Approved decisions (Gabriel):**

1. Student authentication is deferred to phase 2. The preferred direction is
   Google/Microsoft OIDC. Open password self-registration is not implemented
   now.
2. Google/Microsoft OAuth transactions use the session-bound design of §13:
   state, nonce, PKCE verifier, validated return target and expiry in the
   signed session. Apple remains out of scope. Amended 2026-10-11: the single
   use of a transaction is recorded in a dedicated server-side consumption
   table (§13, "AMENDMENT … a dedicated consumption record"). The storage
   exists as migration 020 (§5); the sign-in code that has to use it does
   not.
3. Admin TOTP does not block the initial Accounts PRs. It is a separate
   security PR that must land before the production cutover and the permanent
   removal of legacy auth (§17, PR T).
4. Student-data retention and erasure remain a later product/legal decision.
   This design does not define a retention period.

**Approved deviations from the roadmap's conceptual model:**

- The local password credential is stored on `users`, not in a separate
  `password_credentials` table.
- OAuth transactions are session-bound, not a server-side transaction table
  plus a separate browser-binding cookie. (Amended 2026-10-11, §13: a
  consumption table records that a transaction was used. It stores no
  transaction, so this deviation stands.)
- TOTP is a separate pre-cutover security PR, not part of the first account
  schema.

## 2. Auth architecture before accounts (historical)

This section describes the application as it was when the design was written.
It is the baseline the PR sequence started from, not the current state.

**Mechanisms**

| Concern | Where | What it does |
| --- | --- | --- |
| Authentication | `web/blueprints/auth.py` | Two shared env passwords (`DZI_TESTER_PASSWORD`, `DZI_ADMIN_PASSWORD`) compared with `hmac.compare_digest`. Success sets `session["tester_authenticated"]` or `session["admin_authenticated"]`. |
| Authorization | `web/access.py`, `enforce_access_policy` in `web/app.py` | Per-view `@access_policy`, default deny. `principal_from_session` is the only reader of the auth keys. |
| Request integrity | `protect_same_origin_unsafe_methods` in `web/app.py` | Origin/Referer must match the host for every unsafe method; no exemptions; missing headers pass only under `TESTING`. |
| Attempt ownership | `web/attempt_access.py` | `session["quiz_attempt_owner:<id>"] = True`, checked in the view, answers 404. |
| Session | `web/app.py` | Signed client-side cookie, `SameSite=Strict`, `HttpOnly`, `Secure`. Not permanent, no server-side state. |
| Production | `web/production.py` | Requires `DZI_SECRET_KEY` (≥32 chars) and both passwords (≥12 chars, different, not the local defaults). |

**Session keys in use:** `tester_authenticated`, `admin_authenticated`,
`ui_profile` (cosmetic), `quiz_attempt_owner:<id>`.

**Route policies today**

| Policy | Routes |
| --- | --- |
| `public` | browsing, `/tester`, both logins, `/tester/logout`, `/quiz/s/<public_token>` (migration 017; the numeric `/quiz/<assignment_id>` entry is retired and answers 404), `/healthz` |
| `teacher` (tester or admin) | `/teacher/new`, `/teacher/assignment/<id>` |
| `admin` | `/teacher`, `/teacher/assignments`, results, `results.csv`, edit, duplicate, `/teacher/dzi-training`, `/dzi...`, practical review and downloads, `/admin/...`, `/admin/logout` |
| `attempt_owner` | `/quiz/attempt/<id>`, `/result`, practical page and upload |
| `profile_switch` | `/profile/<profile>` |

**Template assumptions**

- `base.html` branches on the context variables `tester_authenticated` and
  `admin_authenticated` for the nav, the login/logout links and the mobile
  menu, and prints `ui_profile_label` in the footer.
- `index.html`, `teacher_assignment.html` and `teacher_new.html` use
  `admin_authenticated` to show admin-only controls.
- No template knows about a user, a name or a role.

**Ownership today**

- `quiz_assignments` has no owner.
- `quiz_attempts` is identified by the typed `student_name`, with table-level
  `UNIQUE (assignment_id, student_name)`; the seed is
  `sha256(assignment_id|name)`.
- `practical_submissions` hangs off `quiz_attempt_id`.
- Nothing references a person.

**Test dependence on the legacy mechanism**

- 36 test files write the session keys directly and 23 post to
  `/tester/login` or `/admin/login`; about 30 private `_login_admin` /
  `_login_tester` helpers are duplicated across files.
- `tests/test_access_policy.py` pins a full route × principal matrix.
- `tests/test_auth_blueprint.py` asserts that `auth.py` uses no database.
- `tests/test_auth_guard.py` asserts that anonymous GETs set no cookie and
  that the cookie is `Strict`.

## 3. Problems and gaps

1. **Shared secrets, no identity.** Nothing can be attributed, revoked per
   person or scoped per teacher.
2. **Any tester can read any assignment.** `/teacher/assignment/<id>` is
   `teacher`-level with sequential ids and lists every attempt's student name
   and score.
3. **No revocation.** Logout only deletes the browser's cookie; a copied
   cookie stays valid until `DZI_SECRET_KEY` rotates.
4. **Logouts and profile switch are state-changing GETs.** Harmless under
   `Strict`, a logout-CSRF nuisance once the cookie becomes `Lax`. Resolved
   before the cookie became `Lax`: logout is POST-only (PR 4) and the profile
   switch no longer exists (PR 10).
5. **Attempt identity is a typed name.** A student who loses the cookie gets
   404 on their own attempt, and anyone can squat a name.
6. **Results, CSV and practical review are admin-only.** The roadmap wants
   teachers to see their own.
7. **`Strict` cookies break inbound links.** A signed-in student clicking a
   quiz link from Teams or Classroom arrives without a session. Resolved in
   phase 2: the cookie is `Lax` (§6).
8. **The migration runner could not safely rebuild `quiz_attempts`** (§16),
   which the roadmap's uniqueness change requires. Resolved by the opt-in
   table-rebuild directive; migration 015 must use it.

## 4. V1 account model

**DECISION — one `role` column with a CHECK constraint.**

- WHY: there are three roles and a fixed capability table, and `Principal`
  already exposes `is_teacher` / `is_admin`. Admin implies teacher capability;
  student is disjoint.
- ALTERNATIVES REJECTED: a role table or a capability model. Nothing needs
  per-user grants, and plans/entitlements are already a separate seam in the
  roadmap.

**DECISION — email, password hash, display name and state all live in
`users`.** (Approved deviation from the roadmap.)

- WHY: one indexed lookup on login, one row per person, and OAuth-only users
  simply have `password_hash NULL`.
- ALTERNATIVES REJECTED: the roadmap's separate `password_credentials` table.
  It is a 1:1 join that buys nothing; identities from providers still get
  their own table later.

**DECISION — defer `declared_type`, `plan`, `plan_started_at`,
`email_verified_at`, TOTP and recovery codes.**

- WHY: each is a nullable `ADD COLUMN` or a new table when its feature lands;
  none changes the shape of `users`.

## 5. Proposed SQL schema

Migrations are created by their PRs (§17), not by this document.

**`012_users.sql`**

```sql
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE
        CHECK (email IS NULL OR (length(email) BETWEEN 3 AND 254
               AND email = lower(trim(email)) AND instr(email, '@') > 1)),
    display_name TEXT NOT NULL
        CHECK (length(trim(display_name)) BETWEEN 2 AND 80),
    role TEXT NOT NULL DEFAULT 'student'
        CHECK (role IN ('student', 'teacher', 'admin')),
    password_hash TEXT
        CHECK (password_hash IS NULL OR (length(password_hash) > 0 AND email IS NOT NULL)),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    session_key TEXT NOT NULL CHECK (length(session_key) >= 32),
    password_changed_at TEXT,
    last_login_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

- `UNIQUE` on `email` provides the login index, and the primary key serves the
  per-request lookup. No other index is needed.
- The 2–80 name bound is the one the typed quiz name had; `display_name` is
  now what an attempt's `student_name` snapshot is copied from (§24).

**`013_login_throttle.sql`**

```sql
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS login_throttle (
    identifier_hash TEXT PRIMARY KEY CHECK (length(identifier_hash) = 64),
    failure_count INTEGER NOT NULL DEFAULT 0 CHECK (failure_count >= 0),
    window_started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    blocked_until TEXT
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_login_throttle_window
    ON login_throttle(window_started_at);
```

**`014_assignment_owner.sql`**

```sql
PRAGMA foreign_keys = ON;

ALTER TABLE quiz_assignments
    ADD COLUMN owner_user_id INTEGER REFERENCES users(id);

CREATE INDEX IF NOT EXISTS idx_quiz_assignments_owner
    ON quiz_assignments(owner_user_id, created_at, id);
```

**`015_quiz_attempts_user.sql`** (phase 2; table rebuild, see §16; implemented)

- Adds `user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT` and drops the
  table-level `UNIQUE`. `RESTRICT`, never `SET NULL`: NULL means "historical
  attempt", so an owned attempt must not be able to turn into one. Accounts are
  disabled, not deleted, so the restriction is not hit in normal operation.
- Replaces the `UNIQUE` with:
  - `CREATE UNIQUE INDEX uniq_quiz_attempts_user ON quiz_attempts(assignment_id, user_id) WHERE user_id IS NOT NULL;`
  - `CREATE UNIQUE INDEX uniq_quiz_attempts_legacy_name ON quiz_attempts(assignment_id, student_name) WHERE user_id IS NULL;`
  - `CREATE INDEX idx_quiz_attempts_user ON quiz_attempts(user_id, started_at, id) WHERE user_id IS NOT NULL;`
    (the "my attempts" list, newest first; historical rows are not in it)
- Recreates `idx_quiz_attempts_assignment`.

**`019_user_identities.sql`** (phase 2; implemented)

This was the "future `user_identities`" table of the original design, to come
with OAuth in phase 2. Its migration and storage service came first, as storage
only; Google sign-in (§13) is what uses them.

```sql
CREATE TABLE IF NOT EXISTS user_identities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    issuer TEXT NOT NULL,
    subject TEXT NOT NULL,
    provider TEXT NOT NULL CHECK (provider IN ('google', 'microsoft')),
    email_at_link TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_login_at TEXT,
    UNIQUE (issuer, subject)
);
CREATE INDEX IF NOT EXISTS idx_user_identities_user ON user_identities(user_id);
```

- The migration is this schema, unchanged. `users` is not altered and no row
  is written.
- An identity is the exact pair `(issuer, subject)` of a verified ID token,
  compared byte for byte (SQLite's default `BINARY` collation): no case
  folding, no trimming, no normalization. The same subject under another
  issuer is another identity. One identity belongs to at most one user; a user
  may have several.
- **The stored issuer of a Google identity is always
  `https://accounts.google.com`.** Google documents two spellings of `iss` for
  its ID tokens, that one and the bare `accounts.google.com`. Both exact
  strings are accepted, and the one constant is stored, so for a token with
  the bare spelling the stored issuer differs from the token's bytes. This is
  a closed mapping of two documented literals to one constant, made where the
  token is verified (`web/services/google_oidc.py`). It is not normalization:
  nothing is lower-cased, trimmed or parsed, no other issuer can enter through
  it, and the storage service still compares byte for byte. If the token's own
  spelling were stored, one Google account would become two identities.
- `email_at_link` is what the provider reported at linking time. It is
  metadata: not unique, not indexed, and never used to select a user, sign
  anyone in, deduplicate or link (§13).
- `user_id` has no `ON DELETE` action. An account that has identities cannot be
  deleted from under them, and no identity is removed or moved by a cascade.
  Accounts are disabled, not deleted (§15).
- The schema has no `CHECK` on `issuer` or `subject`. That neither is ever
  empty or malformed is enforced by the storage service, which refuses such a
  value instead of repairing it.

**Storage service — `web/services/identities.py`.** Same conventions as
`web/services/accounts.py`: an explicit SQLite connection, no commit and no
rollback (the caller owns the transaction), no import of `web.app`, a
blueprint or Flask.

| Function | Behaviour |
| --- | --- |
| `find_identity(conn, issuer, subject)` | The identity stored for exactly that pair, or `None`. A malformed key finds nothing. The only lookup that may lead from a provider sign-in to a user. |
| `add_identity(conn, user_id, *, issuer, subject, provider, email_at_link=None)` | Stores an identity for an existing user and returns its id. `ValueError` for malformed input, `LookupError` for an unknown user, `IdentityConflict` when the pair is already stored (for any user, this one included). |
| `list_identities(conn, user_id)` | That user's identities, oldest first. |
| `record_identity_login(conn, identity_id)` | Stamps `last_login_at` of one identity. It does not touch `users.last_login_at`. |

- A well-formed key is a non-empty string of visible ASCII characters (no
  space, no control character), at most 255 long. Only one part of that comes
  from OpenID Connect: Core limits `sub` to 255 ASCII characters. The other two
  are **local defensive restrictions** of this service, not rules of the
  specification: accepting only *visible* ASCII, in both keys, and the
  255-character limit on the issuer (Core defines `iss` as an `https` URL and
  sets no length for it). A value outside them is refused, never repaired.
  Whether an issuer is the right one for a provider is token verification and
  is not decided here.
- A duplicate is refused by `UNIQUE (issuer, subject)` through a plain
  `INSERT`. Nothing replaces the stored row or changes its owner.
- The helpers never commit and never roll back. After an `IdentityConflict`
  whatever the caller wrote earlier in its transaction is preserved. A write
  attempt may begin an implicit transaction (Python's `sqlite3` opens one
  before an `INSERT` or `UPDATE`), and it stays open when the attempt fails or
  updates no row: a duplicate on a connection that had no transaction open, a
  `record_identity_login` for an unknown id. The caller finishes that
  transaction, by committing or rolling back, after a failure as after a
  success.
- There is deliberately no function that unlinks an identity, moves it to
  another user, creates an account, changes a password, a session key or a
  role, or looks anything up by email. Those are decisions of the sign-in and
  linking flows (§13), not of storage.
- Issuers, subjects and emails are not logged and do not appear in exception
  messages.

**`020_oidc_consumed_transactions.sql`** (phase 2; implemented)

The one piece of server-side state behind "a transaction is used once" (§13,
amendment of 2026-10-11). Its migration and storage service came first, as
storage only; Google sign-in calls the service at three call sites (§13).
Full specification: `docs/reviews/OIDC_CONSUMPTION_SCOPE.md`.

```sql
CREATE TABLE IF NOT EXISTS oidc_consumed_transactions (
    tx_hash TEXT PRIMARY KEY
        CHECK (typeof(tx_hash) = 'text'
               AND length(tx_hash) = 64
               AND length(CAST(tx_hash AS BLOB)) = 64
               AND tx_hash NOT GLOB '*[^0-9a-f]*'),
    expires_at INTEGER NOT NULL
        CHECK (typeof(expires_at) = 'integer' AND expires_at >= 0)
) WITHOUT ROWID;
```

- The migration is this schema: one table, no index and no row. No other
  table is touched.
- `tx_hash` is `HMAC-SHA256` under the application secret, in lower-case
  hexadecimal, of a purpose and the value that was used. A state, nonce,
  verifier, code or token is never stored, and neither is a user, an address
  or a wall-clock time.
- The `CHECK` means "exactly 64 bytes, each one of `0-9a-f`", and needs both
  lengths. SQLite's `length()` of a text counts the characters before the
  first NUL, and `GLOB` stops reading there as well: those two alone accept 64
  hexadecimal characters followed by a NUL and anything else. The byte length
  alone accepts a 64-byte value with a NUL inside it. Sixty-four characters in
  sixty-four bytes leaves room for neither.
- `expires_at` is the expiry of the object the key was derived from, in whole
  seconds of the callers' **monotonic** clock, not a wall-clock time. The
  column has `INTEGER` affinity, so SQLite converts a value that reads as a
  whole number (`'600'`, `600.0`) before the `CHECK` sees it: what is stored
  is always an integer of at least 0, and the migration's tests pin that.
- There is no `instance` column. Which run of the service wrote a row is not
  recorded and decides nothing: rows are removed by their expiry only.
- `WITHOUT ROWID` and no secondary index. Rows go by their expiry only, in the
  cleanup that a later consumption runs; no index and no other cleanup is
  added for that. The table is **not** bounded to the sign-ins of a few
  minutes: records of earlier boots can remain and accumulate ("Records that
  outlive their boot" below).

**Storage service — `web/services/oidc_consumption.py`.** Standard library
only: no Flask, no blueprint, no logging and no clock. It takes an explicit
SQLite connection and never commits or rolls back.

| Name | Behaviour |
| --- | --- |
| `consumption_key(secret_key, purpose, value)` | `HMAC-SHA256(secret_key, b"oidc-consume\0" + purpose + b"\0" + value)` as 64 lower-case hexadecimal characters. The secret is non-empty `bytes` or `str`; the purpose is `callback` or `link-confirm` (a fixed set); the value is 1–128 characters of `A–Z`, `a–z`, `0–9`, `-`, `_`. Anything else is a `ValueError`. The two purposes never give the same key for one value. |
| `consume(conn, key, *, now, expires_at)` | Records the key inside the caller's transaction and returns `None`. In this order: `ValueError` for a malformed key or time (a boolean is not a time); `RuntimeError` outside a transaction; `Expired` when `now >= expires_at`, before any statement runs; cleanup of every row with `expires_at < now - 60`; a plain `INSERT`, which raises `AlreadyConsumed` when, and only when, the primary key refuses it as a duplicate (on Python 3.11 or newer; see "Python" below). |
| `ConsumptionRefused` | Base of `AlreadyConsumed` and `Expired`. Neither is a database error, and no message contains a key or a value. |

- **Acceptance is `consume` returning and the caller's `COMMIT` succeeding.**
  Everything else means "do not proceed": both refusals, `ValueError`,
  `RuntimeError`, any `sqlite3.Error` (a missing table, a read-only
  connection, a lock that was not obtained, another integrity failure) and a
  commit that fails. No error is read as "not used yet".
- Only the duplicate of the primary key is `AlreadyConsumed`, decided by
  SQLite's own result code and never by the error's class or text. After it
  the caller's transaction is still usable and its earlier writes are intact.
- **Python.** This PR's supported verification workflow uses Python 3.12,
  which is what production runs. Classifying a duplicate as `AlreadyConsumed`
  needs that result code, `sqlite_errorcode`, which Python's `sqlite3`
  provides from 3.11 on. On an older Python a duplicate propagates as the
  `sqlite3.IntegrityError` it is: the key is still refused and nothing is
  recorded for it, and the transaction is still the caller's, neither
  committed nor rolled back by the service. This describes how this PR was
  verified and how the service behaves; it does not declare a minimum Python
  version for the project.
- Cleanup has one condition: a row is deleted once the clock has passed its
  expiry by more than a 60-second allowance. From then on the expiry check
  refuses that row's key before any statement runs, so cleanup never opens a
  way back in.
- **Records that outlive their boot.** Within one boot, each consumption's
  cleanup leaves only the records whose expiry plus the allowance has not
  passed. A reboot restarts the monotonic clock near zero, so the records of
  the earlier boot stay until a consumption runs after the new clock has
  passed their stored expiry plus the allowance. That takes as long as the
  earlier boot had been up, and it does not happen at all if the host is
  rebooted again sooner. Repeated reboots therefore accumulate records of
  several earlier boots, and the table is not bounded to a few minutes of
  traffic. Those records are inert only under obligation 4 below: their keys
  can be derived only from objects of an earlier run, which a caller that
  validates the issuing run refuses before it calls `consume`. That validation
  belongs to the future callers; nothing performs it yet. Cleanup stays
  expiry-only: no other deletion, scheduled job or index is added for these
  records.
- **What every caller owes** (the service cannot check any of it; the PR that
  adds a caller pins it with tests at its call sites):
  1. `BEGIN IMMEDIATE` first, then one fresh clock reading taken while the
     write lock is held, passed as `now`.
  2. One clock for `now` and every `expires_at`: the monotonic clock of one
     host within one boot, never the wall clock.
  3. The `expires_at` of a key is the expiry stored in the authenticated
     object the key was derived from, passed unchanged: one key, one expiry.
  4. An object that the current run of the service did not issue is refused
     before `consume` is called. A restart, a reboot and a restore each begin
     a new run.
  5. Acceptance is as stated above.
  6. The caller finishes its transaction after every outcome.
  7. Keys come only from `consumption_key`.
  8. Nothing else deletes rows: no other code, job or operator procedure.
- **What the storage does not do by itself:** bind a transaction to a browser,
  invalidate anything at a restart, or make a restore safe. After a reboot it
  would accept an unused object of the earlier boot for as long as that
  object's old expiry exceeds the new clock; a restored snapshot lacks the
  rows written after it. Obligation 4 is what covers both, and it belongs to
  the sign-in PR.

**Not created:** sessions, password-reset tokens, email-verification tokens,
OAuth transactions. `oidc_consumed_transactions` is not a transaction table:
it holds keyed hashes of values that were used, and no transaction. The TOTP
tables come from PR T as `016_admin_totp.sql` (§23).

**Stable IDs**

**DECISION — `INTEGER PRIMARY KEY AUTOINCREMENT`, no UUID.**

- WHY: user ids never appear in a URL in v1. `AUTOINCREMENT` guarantees an id
  is never reused, which matters because both the session and the ownership
  foreign keys carry it.
- ALTERNATIVES REJECTED: public UUIDs. They become relevant only if profile
  URLs appear; assignments get an unguessable `public_token` in phase 2
  instead.

**Canonical vs runtime**

- `users` is classified `RUNTIME` and `login_throttle` `OPERATIONAL` in
  `src/canonical_tables.py::TABLE_CLASSIFICATION` (an unclassified table
  already fails tests). `user_identities` is `RUNTIME` as well, and
  `oidc_consumed_transactions` is `OPERATIONAL`: bookkeeping that holds no
  account state. A record is needed only while its key could still be
  accepted; how long it stays in the table is another matter ("Records that
  outlive their boot" above).
- None of them is exported to `data/canonical/`. Account data never becomes
  canonical content.
- `src.build_database` leaves them empty; `test_runtime_tables_are_empty`
  covers `users` and `user_identities`, and the tests of migration 020 cover
  `oidc_consumed_transactions`.

## 6. Authentication and session design

**Identifier and normalization**

- The identifier is the email, normalized as
  `unicodedata.normalize("NFKC", s).strip().lower()`.
- Rejected if it has no single `@`, contains whitespace or control characters,
  or exceeds 254 characters.
- Only the normalized form is stored. No provider-specific folding (Gmail
  dots, plus-tags).

**Password hashing**

**DECISION — `werkzeug.security.generate_password_hash(password, method="scrypt")`
and `check_password_hash`.**

- WHY: Werkzeug is already a dependency; its scrypt default
  (`scrypt:32768:8:1`) salts per hash and stores the parameters in the hash
  string.
- ALTERNATIVES REJECTED: argon2 or bcrypt (new dependencies); anything
  hand-rolled.

Rules:

- Minimum 10 characters (14 for admin, enforced by the CLI and the change
  form), maximum 256, no composition rules, and not equal to the email.
- On successful login, rehash if the stored method prefix differs from the
  current one.
- For an unknown email, verify against a module-level dummy hash so timing
  does not reveal whether the account exists.

**Session contents**

**DECISION — `session["uid"]`, `session["sk"]`, `session["iat"]` only.**

- WHY: role, name and active state are mutable and must take effect
  immediately.
- ALTERNATIVES REJECTED: copying the role into the cookie; a server-side
  session table; Redis.

**DECISION — a random per-user `session_key` instead of an integer version.**

- WHY: databases here get rebuilt and restored. After a restore, a new user
  can receive an id that an old cookie still names, and an integer version of
  0 would match; a random key cannot.
- The key is rotated on password change, disable, role change and CLI
  "sign out everywhere", and compared with `hmac.compare_digest`.

**Loading the principal**

- A `load_current_user` `before_request` hook in `web/app.py` runs after the
  same-origin check and before `enforce_access_policy`.
- If the session has `uid`, it runs one
  `SELECT id, role, display_name, is_active, session_key FROM users WHERE id = ?`
  on the request connection and stores `g.principal`.
- If there is no `uid` it does nothing, so anonymous requests still cost zero
  queries and set no cookie.
- `web.access.current_principal()` returns `g.principal` when present and
  otherwise falls back to `principal_from_session`. `web/access.py` stays free
  of service imports, which an existing test requires.
- `/healthz` and `static` are skipped.

**Login (`POST /login`)**

1. Check the throttle for the identifier; if blocked, answer 429 without
   hashing.
2. Look up the user and verify (or dummy-verify) the password.
3. On failure, record it and show one generic message.
4. On success: reset the throttle, call `session.clear()`, carry over only
   `quiz_attempt_owner:*` keys, set `uid`, `sk`, `iat`, update
   `last_login_at`, and redirect through `safe_redirect_target`.

**Fixation**

- Flask's cookie session has no server-side id to fixate.
- `session.clear()` on login and logout removes any pre-login state, including
  legacy `tester_authenticated` / `admin_authenticated`.

**Logout**

- `POST /logout` under the same-origin policy calls `session.clear()`.
- It does not rotate `session_key`, so other devices stay signed in.

**Remember-me**

**DECISION — none.** The cookie stays a browser-session cookie, with a
server-enforced absolute lifetime from `iat`: 30 days for student and teacher,
12 hours for admin.

- WHY: shared school computers, and no revocation UI.

**Password change (`/account/password`)**

- Requires the current password; failures count against the same throttle.
- Rotates `session_key`, re-issues the current session, and thereby signs out
  every other session.

**Disabled accounts and role changes**

- Both are read from the row, so they apply on the next request.
- The CLI also rotates `session_key`, which forces a fresh login.

**Cookie attributes**

- `SameSite=Lax`, `HttpOnly`, `Secure`, and no `Expires` or `Max-Age`. Every
  response that writes or clears the cookie uses them.
- Phase 1 kept `Strict`. The move to `Lax` was its own small PR in phase 2,
  after the POST-only logout existed. It changed that one attribute: no session
  key, lifetime, route, migration or environment variable.
- With `Lax`, a top-level GET that starts on another site arrives signed in, so
  no GET may change state for the visitor (§14).

**Caching**

- Add `Cache-Control: no-store` to responses for signed-in principals, so the
  back button on a shared computer does not replay results.

## 7. Authorization and roles

**Principal** gains `user_id`, `role` and `display_name` with defaults, so
existing `Principal(is_teacher=..., is_admin=...)` constructions stay valid.

| Role | `is_teacher` | `is_admin` |
| --- | --- | --- |
| student | no | no |
| teacher | yes | no |
| admin | yes | yes |

**Access vocabulary additions**

- `signed_in`: any active account. First used in phase 2.
- `assignment_owner`: the route admits an admin or a teacher account (teacher
  capability without an account id is never enough), and the
  view checks `can_manage_assignment` (admin, or an owner equal to a non-NULL
  account id) on the loaded row and answers 404. Implemented in PR 7; PR 8
  reuses the same policy, predicate and SQL scope from `web/access.py` for
  practical review.
- Practical review (PR 8, implemented): the submission list and the stored-file
  download are `assignment_owner`. Ownership follows file → submission →
  attempt → assignment → `owner_user_id`; the list is filtered in SQL and the
  download checks the predicate before the file is looked at on disk. An admin
  keeps every submission; an unowned (NULL) assignment's practical data is
  admin-only.

**Target flows**

| Who | Access |
| --- | --- |
| Anonymous | Browsing as today; `/login`; the assignment entry page (it only offers the sign-in); an attempt started before accounts, from the browser that started it. No new attempt. |
| Student | Sign in, take assigned quizzes, see own attempts (`/account/attempts`) and results, upload practical work for own attempts (§24). |
| Teacher | `/teacher/new`; own assignments' detail, edit, duplicate, results, CSV; practical review for own assignments. May also take any quiz with an attempt of its own (§24). |
| Admin | Management of everything (never answer access to another account's attempt, §24), including unowned legacy assignments, `/teacher` dashboard, `/teacher/dzi-training`, `/dzi...`, candidates, resource downloads. |

**Where UI state differs from authorization**

- `ui_profile` and `/profile/<profile>` were cosmetic and were retired by
  PR 10; the footer label is the account's role.
- The template variables `tester_authenticated` and `admin_authenticated` were
  computed from the principal during the bridge and removed by PR 10; templates
  test `current_account` (the account principal or `None`).

## 8. Tester/admin compatibility strategy (historical; ended by PR 10)

The bridge below existed from PR 3 to PR 9 and its code was deleted by PR 10.
The rules are kept to explain the transition; "Removal" at the end of this
section is the current state.

**Bridge rules (PR 3 until cutover)**

- A valid account identity in the session is the only identity; legacy keys
  are ignored while `uid` is present.
- Without `uid`, the legacy keys behave exactly as today, so the existing
  matrix tests stay green unchanged.
- A legacy tester session has teacher capability but no `user_id`. After
  ownership lands it creates unowned assignments and can open only unowned
  ones, which is today's behaviour and never exposes an account teacher's
  assignments. From PR 7 this is view-only detail compatibility: the tester
  cannot list, edit, duplicate, read results or CSV, or review answers of any
  assignment, unowned ones included. PR 8 adds no tester exception: practical
  review and submission-file downloads are closed to the tester.
- Denial redirects keep pointing at `/tester/login` and `/admin/login` until
  the cutover PR, except the `assignment_owner` routes (PR 7), which send to
  the account login `/login`.

**Switch (PR 9, superseded by PR 10)**

- PR 9 introduced `DZI_LEGACY_SHARED_LOGIN`. When off, the legacy routes
  answered 404 and the keys were ignored. Production was always off:
  `web/production.py` refused to start with an enabled or unreadable value and
  forced the bridge off otherwise, and stopped requiring the two shared
  passwords. Outside production the default stayed on until PR 10.
- `DZI_SECRET_KEY` was not rotated for the cutover.
- `manage_accounts cutover-check` is the read-only operator preflight; the
  application does not depend on a viable admin at startup. It remains.

**Removal (PR 10) — current state**

- Deleted: `web/blueprints/auth.py` and its routes (`/tester`,
  `/tester/login`, `/tester/logout`, `/admin/login`, `/admin/logout`,
  `/profile/<profile>`), their templates, the `profile_switch` access level,
  the session fallback in `web/access.py` (`principal_from_session`), the
  `ui_profile` hook and context, the `DZI_LEGACY_SHARED_LOGIN` flag with its
  parsing and production validation, every read of `DZI_ADMIN_PASSWORD` /
  `DZI_TESTER_PASSWORD`, the view-only tester exception for unowned assignment
  detail, and the Nginx zones of the two shared logins.
- `current_principal()` returns the account principal loaded for the request or
  anonymous. No code reads `tester_authenticated`, `admin_authenticated` or
  `ui_profile`; a cookie that still carries them is simply anonymous.
- No environment variable or configuration value can re-enable shared sign-in.
- No migration, no ownership backfill and no key rotation were involved.
- The exact contract is in `docs/AUTH_ROUTE_CONTRACT.md` ("Removed
  shared-password authentication"), the operator notes in `docs/DEPLOYMENT.md`
  (section 23).

**Tests**

- PR 3 added `tests/auth_helpers.py` (`login_as(client, role)`), and PR 5 moved
  the duplicated per-file helpers onto it and pointed it at real account rows.
- PR 10 deleted the tests that pinned bridge behaviour and moved the remaining
  authorization tests onto account sessions. The historical keys are injected
  only by regression tests that prove they grant nothing.

**Phases derived from the repository**

1. Schema + service.
2. CLI bootstrap.
3. Principal seam.
4. Login/logout.
5. Test helper migration.
6. Assignment ownership.
7. Owner-scoped teacher routes.
8. Practical review.
9. Admin TOTP (PR T), then cutover.
10. Legacy removal.

## 9. Ownership model

| Table | Class | Reason |
| --- | --- | --- |
| `quiz_assignments` | A — v1 (`owner_user_id`, nullable) | Closes gap 2 and enables teacher results. Legacy rows stay NULL and admin-only. |
| `quiz_attempts` | B — phase 2 (`user_id`, nullable) | Needs the table rebuild and student sign-in. Legacy rows stay NULL; no linking by name. |
| `quiz_answers`, `quiz_text_answers` | C | Owned through the attempt. |
| `practical_submissions`, `practical_submission_files` | C | Owned through the attempt; reviewer scope comes through attempt → assignment → owner. |
| `generated_exams` | C | Offline tooling log, no web writer. |
| `practical_submission_grades` | C | No migration creates it; the code only probes for it. No change. |

**Interacting ids**

- `quiz_attempts.question_ids_json` holds canonical question ids and is
  unaffected.
- The seed for account attempts becomes
  `quiz_seed(assignment_id, f"user:{user_id}")`.
- `student_name` stays as a display snapshot copied from `display_name`.
- Vault notes under `vault/Generated/Quizzes/` embed the name in filenames.
  The note of an account-owned attempt also carries the attempt id
  (`<date>-assignment-<id>-attempt-<attempt id>-<name>.md`), because two
  accounts may share a display name; historical notes keep their old names.

## 10. Registration

| Option | Assessment |
| --- | --- |
| Open registration with password | No email infrastructure means no verification and no recovery; it invites spam and stores minors' passwords. Not implemented. |
| Admin-created accounts | Fits a pilot of a handful of teachers and needs no web surface. **v1.** |
| Invite or join code | Possible for classes later; still needs an identifier and recovery story. |

**DECISION — v1 has no registration route.** Accounts are created by the CLI.

**DECISION (approved) — student authentication is phase 2, preferred via
Google/Microsoft OIDC.** Open password self-registration is not built now; it
would at minimum require transactional email for verification and recovery.

**Later amendment (2026-10).** Verified email/password registration was
designed afterwards (`docs/reviews/EMAIL_REGISTRATION_DESIGN.md`, decisions D1
to D20): a student registers with a code sent to the address, and the routes
exist behind a build gate that is off (email registration PR 4). The v1
decision above still describes every deployed build: production has no
reachable registration route.

## 11. Bootstrap admin

**DECISION — an explicit management command, `python3 -m src.manage_accounts`.**

- Subcommands: `create`, `set-password`, `set-role`, `disable`, `enable`,
  `sign-out`, `list`.
- The password is read with `getpass` (twice) or `--password-stdin`, never
  from argv or the environment.
- `--db` is required unless `DZI_DB` is set; the command never silently
  defaults to `data/questions.db`.
- Granting `admin` is possible only here; no web path can create or promote an
  admin.
- WHY: `web.wsgi` must not write on import, and an env bootstrap would keep a
  password in the env file and re-apply it on every start.
- ALTERNATIVES REJECTED: one-time environment bootstrap; seeding through a
  migration or canonical data. No credentials are ever committed.

## 12. Password reset and email

- **v1:** no reset flow and no email verification. (Migration 021 later added
  the nullable column `users.email_verified_at` for registration; it is NULL for
  every account except one created by a verified registration, which is not
  activated, and no access decision reads it. Migration 023 added
  `users.profile_required_at`, the profile mark; see §15 and the
  status list above.)
- **Recovery:** the operator runs `manage_accounts set-password` on the
  server, which rotates `session_key`, and passes the new password out of
  band. The user then changes it at `/account/password`.
- **Later:** a hashed, single-use, short-lived token table arrives with
  transactional email, in the same PR as the flow.

## 13. OAuth boundary (Google implemented; Microsoft later; phase 2)

Not part of phase 1. Apple is out of scope.

**Status.** Google sign-in is implemented by PR #331 and is **not activated**:
see "Google sign-in as implemented" at the end of this section. Its
specification is `docs/reviews/GOOGLE_OIDC_DESIGN.md`; how student accounts are
created through Google is the automatic student onboarding that Gabriel
approved, in which a first-time Google sign-in may create a `student` account
(that design, section 6). Identity storage (migration `019_user_identities.sql`,
`web/services/identities.py`) and the storage that single use of a transaction
needs (migration `020_oidc_consumed_transactions.sql`,
`web/services/oidc_consumption.py`; §5 and the amendment below) are used by it.
Microsoft sign-in is not implemented, and nothing below is implemented for
Microsoft. The decisions and requirements below are kept as approved; they are
what PR #331 was built to.

- `users` does not change. `user_identities` (§5) is added by its own
  migration, keyed on `UNIQUE (issuer, subject)` (done: migration 019).
- Authlib is the client library, as the roadmap states; PR #331 added it. No
  hand-rolled OAuth.
- The callback declares the `OAUTH_STATE` integrity policy of
  `web/request_integrity.py`; it is not an access level.
- The session cookie is `SameSite=Lax` (with `Secure` and `HttpOnly`; done as
  its own PR before OAuth, §6), so the provider's top-level GET redirect
  carries the session.

**DECISION (approved) — session-bound transaction.** `state`, `nonce`, the
PKCE verifier, the validated return target and an expiry are stored in the
signed session and removed on callback (success or failure).

- WHY: with a `Lax` cookie the Google and Microsoft GET callbacks carry the
  session, which is itself the browser binding. No table and no
  `SameSite=None` cookie are needed. (Amended below: no *transaction* table,
  but one table of consumption records.)
- ALTERNATIVES REJECTED: the roadmap's server-side transaction table plus a
  separate binding cookie. It is needed only for a cross-site form-post
  callback (Apple), which is out of scope.

**AMENDMENT (approved by Gabriel, 2026-10-11) — a dedicated consumption
record.** The decision above stands: the transaction stays in the signed
session and there is still no server-side transaction table. What the session
cannot deliver alone is "single-use" (the State row below). The server cannot
take a cookie back, so removing the transaction from a response does not stop
an earlier copy of the cookie from presenting it again. One piece of
server-side state is therefore added, by a prerequisite PR of its own, before
sign-in:

- a dedicated table, `oidc_consumed_transactions`, that records only a keyed
  hash of each value that was used (§5). `login_throttle` is not used for it;
- a duplicate and every storage error fail closed: the sign-in does not
  proceed;
- a record is kept for the whole time in which its transaction could still be
  accepted, plus an allowance, and cleanup cannot make an accepted transaction
  usable again.

**Status of the amendment.** The table and its storage service are implemented
(migration 020 and `web/services/oidc_consumption.py`, §5; specification and
approval in `docs/reviews/OIDC_CONSUMPTION_SCOPE.md`), and Google sign-in calls
them. **The storage alone is not replay protection.** "Used once" holds only
when the code that calls the service meets the caller obligations of §5: the
write lock before a fresh monotonic clock reading, one clock, an authenticated
and unchanged expiry per key, and refusal of every object that another run of
the service issued. The storage does not bind a transaction to a browser, does
not invalidate anything at a restart and is not safe across a restore by
itself. PR #331 is the code that has to meet those obligations; where it does
so is listed under "Google sign-in as implemented" below.

**Recording order (part of the amendment of 2026-10-11; a requirement on the
sign-in PR).** Use is recorded for every callback whose state matches, before
any other callback parameter is examined and before any outbound call, in a
write that persists whatever follows. How the Google sign-in PR calls the
storage and meets the caller obligations of §5 is specified in
`docs/reviews/GOOGLE_OIDC_DESIGN.md`, sections 3 and 9, and implemented by
PR #331 (`web/oauth_state.py`, `verify_oauth_state`). That design does not
override this section: where the two differ on what is approved, this section
wins.

**Security requirements for that PR**

| Topic | Requirement |
| --- | --- |
| State | Random, single-use, compared in constant time; mismatch, absence or expiry is a hard failure. |
| PKCE | S256 for both providers. |
| Nonce | Sent in the request and verified in the ID token. |
| Callback | Exact registered redirect URI built from `DZI_PUBLIC_HOST`; authorization-code flow only. |
| Issuer and audience | Validate the signature via provider JWKS, `aud` equals our client id, `exp`/`iat`. For Microsoft multi-tenant, the issuer must match the token's `tid`. |
| Email semantics | Google's `email_verified` may be trusted for display. Microsoft's email claims are not verified and are never used for identity. Authorization is never keyed on email. |
| Linking | Only while signed in to the existing account. Never by matching email. |
| Duplicate email | A provider sign-in whose email matches an existing account neither signs in nor links; it tells the user to sign in and link from the account page. |
| Login CSRF | Covered by the session-bound state. |
| Open redirect | The return target passes through `safe_redirect_target` when stored and again when used. Existing redirect confinement must not regress. |
| Uniqueness | `UNIQUE (issuer, subject)`; one identity maps to one user. |

### Google sign-in as implemented (PR #331)

Specification: `docs/reviews/GOOGLE_OIDC_DESIGN.md` (sections 1–9) with its
test plan; product decisions in `docs/reviews/GOOGLE_OIDC_SCOPE.md`. This is a
summary of what the code does. Where it and those documents differ, the design
is the specification and the difference is a defect.

**Not activated.** The provider is enabled only when both credentials
(`DZI_GOOGLE_CLIENT_ID`, `DZI_GOOGLE_CLIENT_SECRET`) and a public base URL are
configured. On top of that, a production start is governed by one constant in
the build, `PRODUCTION_ACTIVATION` in `web/services/google_oidc.py`. It is
`False`: production starts normally whatever the two settings hold, keeps
Google sign-in disabled and logs one warning that names the settings. No
setting overrides it. The release that delivers identity recovery tooling
(unlinking, and correcting a mistaken link) turns it on; that tooling does not
exist yet and is required first. Disabled, the five routes answer the generic
404, no page shows a Google button or message, and password and TOTP sign-in
are exactly what they were.

**Review status.** The implementation is uncommitted, not merged, not deployed
and not activated. An independent implementation review found no code
blockers; its documentation corrections are recorded in
`docs/reviews/GOOGLE_OIDC_DESIGN.md` 0.6, with the implementation departures it
assessed as justified, the activation prerequisites it added (real-provider
compatibility with the identity encoding; the production and service tests run
against the server's dependency versions; installation validated from the
declared requirements in a clean environment; the Google button branding) and
the limits it documented (URL outcome codes, abandoned workers, development
server logs). The deployment probes are in `docs/DEPLOYMENT.md` section 29 and
have not been run.

**Routes** (`web/blueprints/oidc.py`; the contract is
`docs/AUTH_ROUTE_CONTRACT.md`): `POST /auth/google/start`, `POST
/auth/google/link`, `GET /auth/google/callback`, `GET`/`POST
/auth/google/link/confirm`, `POST /auth/google/link/cancel`, and the account
page `GET /account` (`web/blueprints/account.py`).

**Modules.** `web/oauth_state.py` (the transaction and the pending link in the
signed session, the callback's integrity hook, retiring);
`web/services/google_oidc.py` (pinned provider metadata, the code exchange and
the ID-token checks behind one network budget; no Flask, no database);
`web/services/oidc_accounts.py` (sign-in, onboarding, the link decision and the
write; explicit connection, no commit); `web/account_session.py` (the three
functions that replace or clear a session).

**What it does**

- *Identity.* An account is found by `(issuer, subject)` and by nothing else.
  An email never selects, signs in or links an account.
- *Onboarding.* A first-time Google identity becomes a `student` in one
  `BEGIN IMMEDIATE` transaction that creates the user and the identity
  together. `users.email` and `password_hash` stay `NULL`; the display name is
  the cleaned name of the Google profile, or „Ученик“.
- *Email collision.* A new identity whose reported address, verified or not, is
  the sign-in email of an existing account creates nothing and signs nobody
  in; the person is told to sign in with the password and link from „Профил“.
- *Linking.* A student or a teacher with a password starts a link while signed
  in. After Google returns and the ID token is verified, a confirmation page
  names both profiles, and the identity is stored only after the account's
  current password on a same-origin POST, under the account's throttle
  counter, with the account, its role, its active state, its session key, the
  bound session's lifetime, the expiry and the identity's ownership checked
  again inside the write transaction. One Google identity per account; a link
  is refused when the reported address belongs to another account.
- *Admins.* An admin can neither link Google nor sign in through it, a teacher
  promoted after linking included. Password plus TOTP stays the only way an
  admin session comes into being, and the operator CLI refuses to promote an
  account that has no password.
- *Session.* A Google sign-in issues the ordinary account session, exactly
  `uid`, `sk` and `iat` with the carried attempt markers. Nothing in it says
  how the account signed in.

**What is guaranteed**

- A transaction is accepted at most once. The first callback whose state
  matches commits a consumption record before it looks at any other parameter
  and before any request to Google; every later callback for that state is
  refused, from the same browser, from a copy of the cookie, at the same
  moment or through another process. It is accepted only within its lifetime
  on a monotonic clock that wall-clock corrections do not move, and only by
  the run of the service that issued it. A wall clock set back can still make
  Flask refuse the signed session cookie that carries the transaction, and the
  callback then fails closed (`docs/reviews/GOOGLE_OIDC_DESIGN.md` 0.6).
- An accepted transaction completes only with an ID token that Google issued
  for that transaction's authorization request (nonce), for this client
  (audience), signed by a key that Google publishes (RS256), within its time
  claims.
- A pending link is stored as an identity at most once, by a request that
  presents the bound account's session and its current password.
- Timed-out provider work changes nothing: one total budget of 8 seconds per
  callback, at most two workers inside the network function per process, and
  a result that arrives late is never read.
- No authorization code, state, nonce, verifier, token, client secret, subject
  or provider email is logged. Gunicorn's access log has no query string and
  its error log passes a redacting filter; Nginx turns every error status of
  the callback address into a redirect and logs that address without its
  query string (`docs/DEPLOYMENT.md`, section 29).

**Limits, stated** (the design's 3.4 and 4.4 are the full text)

- Use is recorded before the outcome is known: a cancellation at Google, a
  provider error, a timeout or a validation failure also spends the
  transaction, and the person starts again.
- A restart ends every sign-in and link in flight.
- Removing a slot from a response cookie revokes no earlier copy of the
  cookie. What stops a copy is the consumption record, the instance and the
  expiry. Cancelling, signing out, starting another link and changing account
  each try to record that the linking in progress has ended; when that record
  cannot be written, a copy of the cookie stays usable until it expires, and
  confirming it still takes the account's password on an unchanged account
  and session key. A cancellation says „Свързването с Google беше отказано.“
  only when its own record ended the link and the account had no Google
  identity when it was read.
- A link confirmed onto the wrong account, a student account created by
  someone who meant to link, and a Google-only account whose Google account is
  lost cannot be corrected by this release. That is why activation waits for
  the recovery tooling.
- Signing out of LearnPilot does not sign the browser out of Google. The
  sign-in page and the account page say so.

**The caller obligations of §5, and where each is met.** Lock, then clock:
`begin_write` in `web/oauth_state.py`, at the callback, at the confirmation
and when retiring. One clock: `monotonic_seconds()` there. A fixed,
authenticated expiry: the `exp` of the signed session object, passed unchanged
by `record_use` and by `confirm_link`. Only the run that issued it: the
instance check before every call. Acceptance is return plus commit, and every
error ends the request without one. Keys come only from `consumption_key`.
Nothing but the cleanup inside `consume` deletes a row.

## 14. Security model

**CSRF and request integrity**

**DECISION — no CSRF tokens; the existing same-origin check is sufficient for
account forms.**

- WHY: every unsafe method already requires a matching Origin or Referer with
  no exemptions, and that includes the login POST, so login CSRF is covered.
  The cookie's SameSite attribute is a second, independent layer: `Lax` (§6)
  keeps the cookie off every cross-site POST, but not off a top-level GET, and
  it never tells apart the hosts of one site. The origin check does not depend
  on it.
- Conditions that must hold:
  - every new unsafe view declares `@integrity_policy(SAME_ORIGIN)`;
  - new state changes are POST only;
  - no GET changes state on the visitor's behalf, because a link from another
    site arrives signed in. The lifecycle observation stamp, with its clock
    bookkeeping, is the one deliberate write of a GET: the server's clock, no
    request input, written once, and caused by an anonymous request just the
    same;
  - authentication code never touches the integrity check (the separation
    from the D1/D2 refactor is preserved).

**Rate limiting**

- **Per identifier (SQLite):** 10 failures in 15 minutes blocks that
  identifier for 15 minutes, for existing and non-existing emails alike.
  - The key is `HMAC-SHA256(SECRET_KEY, normalized_email)`, so mistyped emails
    are never stored in clear.
  - Expired rows are deleted on write.
- **Per IP (Nginx):** a new `lp_login` zone on `= /login` at about `1r/s` with
  `burst=40`. A whole class behind one school NAT may sign in at once, and the
  current `5r/m` zones would lock a lesson out.
- ALTERNATIVES REJECTED: an in-process counter (silently wrong if workers ever
  increase); Redis.
- Accepted trade-off: someone can lock a known email out for 15 minutes; the
  CLI clears it.

**Error behaviour**

| Case | Behaviour |
| --- | --- |
| Invalid credentials, unknown email, disabled account, account without password | Same page, same status, one message: „Грешен имейл или парола.“ |
| Throttled | 429, „Твърде много опити. Опитай отново след няколко минути.“ regardless of whether the account exists. |
| Duplicate at creation | The CLI reports it plainly (operator context). No public surface in v1. |
| Anonymous on a protected route | 302 to login with `next=<path>`. |
| Signed in without the role | A Bulgarian 403 page (`403.html`). |
| Object not owned (assignment, attempt) | 404, never 403. |
| Stale, deleted, disabled or key-mismatched session | Identity keys dropped, request continues as anonymous. |
| Malformed `next` | `safe_redirect_target` falls back by role: admin → `/teacher`, teacher → `/teacher/new`, student → `/`. |

## 15. Privacy and data minimization

- **Collected:** email, display name, role, password hash, timestamps. For an
  account that uses Google sign-in, also: Google's issuer and subject
  identifier; the email address Google reported when the identity was added,
  whether or not Google marked it verified; the time the identity was added
  and last used. For an account created through Google, the display name is
  the name in the person's Google profile at that moment. Nothing else.
- **Held briefly, not stored:** while a link awaits confirmation, the Google
  subject, reported email and name are in the person's signed session cookie
  for at most three minutes.
- **Collected only behind gates that are off (registration PRs 4, 6 and 7; the
  amendment this paragraph needs is an activation prerequisite and is not made
  here):** the code can store a first and last name and a class (8 to 12) for a
  student, and a school and one to three subjects for a teacher, in
  `user_profiles`, and a "profile required" mark in `users.profile_required_at`.
  While the registration and profile gates are off nothing collects or shows them,
  so the next list is still true of every running build. Their purpose, who may see
  them and how long they are kept are not decided (decision D16 of
  `docs/reviews/EMAIL_REGISTRATION_DESIGN.md`) and none may be invented by an
  implementation.
- **Also recorded, for every account that creates an assignment (registration PR 7):** a ledger of
  creations in `assignment_creations`: the account id, the assignment number and the time. It holds
  no name, email or text, is kept as long as the account exists, and is what keeps the daily
  limit from being refunded when an assignment is closed or deleted. It is a running build's
  data, unlike the profile fields.
- **Not collected:** date of birth, school, class, phone, IP address, user
  agent, login history; from Google also the profile picture, given and family
  name, locale, organisation domain, and any Google token. No Google API is
  called on the person's behalf, and no scope beyond `openid email profile` is
  requested.
- **Sent to Google** when the person presses the button: the application's
  client id, the callback address and random values. Google thereby learns
  that this Google account signed in to LearnPilot, and when. Nothing is sent
  before that; no Google resource is loaded by any page (the logo on the
  button is a local file).
- **Never logged:** passwords, hashes, `session_key`, cookie values, failed
  identifiers; authorization codes, state, nonce, PKCE verifier, ID and access
  tokens, the client secret, subjects, provider emails. Emails never go in a
  query string. The consumption table holds keyed hashes only: exactly 64
  bytes of lower-case hexadecimal, which the table itself enforces (§5).
- **Signing out** of LearnPilot does not sign the browser out of Google.
- **Existing sinks that will carry account names:** CSV exports and the vault
  notes under `vault/Generated/Quizzes/`.
- **Deletion:** v1 only disables accounts. Identities are not removed when an
  account is disabled.
- **Not settled, and required before Google sign-in is activated:** a public
  privacy-policy page (LearnPilot has none; Google's consent screen needs one),
  and the data-controller questions for minors listed in
  `docs/REFACTOR_ACCOUNTS_ROADMAP.md`.
- **Retention and erasure (deferred, approved):** how long student accounts
  and attempts are kept, and who may request deletion, is a later
  product/legal decision. No retention period is defined here, and none may be
  invented during implementation. Erasure tooling is built once that decision
  exists.
- No analytics or tracking.

## 16. Migration strategy

| Migration | Content | Backfill | Downtime | Rollback |
| --- | --- | --- | --- | --- |
| `012_users.sql` | New table | None | Restart only | Old code ignores the table. |
| `013_login_throttle.sql` | New table and index | None | Restart only | Same. |
| `014_assignment_owner.sql` | Nullable column and index | None; legacy rows stay NULL | Restart only | Old code inserts with explicit column lists and is unaffected. |
| `015_quiz_attempts_user.sql` (phase 2) | Table rebuild | Copy all rows with `user_id NULL`, preserving ids and the `sqlite_sequence` value | Service stopped, seconds | Restore from the pre-migration backup. |
| `019_user_identities.sql` (phase 2) | New table and index | None; the table starts empty | Restart only | The table is read and written only by Google sign-in (§13), which is not activated, so while it is off nothing uses the table. Once it is on and identities exist, a build from before the sign-in code leaves them inert and accounts created through Google cannot sign in under it (`docs/DEPLOYMENT.md`, section 29, "Rollback and restore"). A build without 019 refuses to start on the migrated database, as for every migration since the lifecycle release: a recovery build keeps the migration, and a snapshot restore does not lead to a build from before it (`docs/DEPLOYMENT.md`, sections 21 and 27). |
| `020_oidc_consumed_transactions.sql` (phase 2) | New table, no index | None; the table starts empty | Restart only | Google sign-in (§13) is the only caller of the storage, at three call sites, and is not activated; while it is off nothing is written, and the records already written stay and are not deleted for a rollback. As for 019: a build without 020 refuses to start on the migrated database, a recovery build keeps the migration, and a snapshot restore does not lead to a build from before it (`docs/DEPLOYMENT.md`, sections 21 and 28). |
| `021_registration_schema.sql` (registration PR 1) | One nullable column on `users` (`email_verified_at`) and five new tables with their indexes | None; every account has the column NULL and the tables start empty | Restart only | Nothing reads or writes the column or the tables except the profile service, which has no caller, and no gate exists to turn on. Code from before the migration names its `users` columns and runs unchanged. As for 019 and 020: a build without 021 refuses to start on the migrated database, a recovery build keeps the migration, and a snapshot restore does not lead to a build from before it (`docs/DEPLOYMENT.md`, section 30). |
| `023_profile_required.sql` (registration PR 6) | One nullable column on `users` (`profile_required_at`) | None; every account has the column NULL, so no account is marked or asked | Restart only | Nothing reads the column while the profile gate is off, and the only writer is Google onboarding with the gate on. Code from before the migration names its `users` columns and runs unchanged. As for 019 to 021: a build without 023 refuses to start on the migrated database, a recovery build keeps the migration, and a snapshot restore does not lead to a build from before it (`docs/DEPLOYMENT.md`, section 35). |

**Runner constraint (applies to 015 only; runner support is in place)**

- `src/manage_migrations.py::apply_migrations` runs an ordinary file in
  `BEGIN … COMMIT` on a connection with `foreign_keys=ON`, and that pragma
  cannot be changed inside a transaction.
- Dropping `quiz_attempts` in that state runs an implicit delete, which
  cascades into `quiz_text_answers` and `practical_submissions` (both
  `ON DELETE CASCADE`). This is still what happens to a rebuild file that
  lacks the directive.
- The runner therefore has an opt-in file directive,
  `-- learnpilot-migration: table-rebuild`: foreign keys are turned off before
  `BEGIN`, `PRAGMA foreign_key_check` runs before `COMMIT`, the
  `schema_migrations` row is part of the same transaction, the file's SQL
  cannot commit or leave that transaction, and any error or violation rolls
  everything back. Contract and author rules: `docs/DEPLOYMENT.md` section 9,
  "Table-rebuild migrations".
- Migration 015 is written to these requirements
  (`tests/test_quiz_attempts_user_migration_sql.py` checks them):
  - start with the directive;
  - create the replacement table, copy every row with its id, drop
    `quiz_attempts`, then rename the replacement to `quiz_attempts`, in that
    order (renaming the old table first can redirect the child foreign keys,
    depending on the SQLite build);
  - carry the old `sqlite_sequence` value over explicitly, or a deleted
    attempt id is reused;
  - recreate `idx_quiz_attempts_assignment` and every other index explicitly;
  - leave the rows of `quiz_answers`, `quiz_text_answers` and
    `practical_submissions` untouched;
  - have its tests compare source and copied row counts: the runner does not
    detect silent row loss;
  - have its tests assert the resulting schema explicitly, also on a fresh
    database, and run them on the interpreter that production uses: the
    foreign-key check validates rows, not the schema.

**Interaction with the operational database**

- Production applies migrations only through `manage_migrations --apply`, with
  the service stopped and a `src.backup_sqlite` backup first; startup already
  refuses pending migrations.
- A developer's local `data/questions.db` is migrated the same way, never
  rebuilt or replaced.
- Account migrations reach production only after the consolidated deployment
  and the `000` baseline adoption are complete, as a separate deploy.

## 17. PR-by-PR implementation sequence

**Phase 1 — teacher and admin accounts**

| PR | Scope | Likely files | Migration | Tests | Non-goals | Depends on |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `users` schema and accounts service | `web/migrations/012_users.sql`, `web/services/accounts.py`, `src/canonical_tables.py` | 012 | Migration SQL, service, classification, fresh build | Routes, session, CLI | — |
| 2 | `manage_accounts` CLI | `src/manage_accounts.py`, `docs/DEPLOYMENT.md` | — | CLI on temp DBs; password never in argv; admin only via CLI | Web UI | 1 |
| 3 | Principal seam and bridge | `web/access.py`, `web/app.py`, `tests/auth_helpers.py` | — | Session with valid, stale, disabled, key-mismatched `uid`; legacy matrix unchanged; anonymous sets no cookie | Login routes | 1 |
| 4 | `/login`, `POST /logout`, `/account/password`, throttle | `web/blueprints/account.py`, `login.html`, `account_password.html`, `base.html`, `web/migrations/013_login_throttle.sql`, `deploy/nginx/learnpilot.conf`, `docs/AUTH_ROUTE_CONTRACT.md` | 013 | §18 login/session rows | Registration, reset, OAuth, changing denial redirects | 3 |
| 5 | Move test helpers to `tests/auth_helpers.py` | `tests/` only | — | Suite green; query budgets re-baselined by exactly the one user lookup | Any app change | 3 |
| 6 | Assignment ownership | `web/migrations/014_assignment_owner.sql`, `web/blueprints/teacher.py`, `web/services/quiz.py` | 014 | Stamped on create; foreign teacher gets 404; admin sees all; legacy tester sees unowned only | Changing which routes are admin-only | 4 |
| 7 | Owner-scoped teacher routes | `web/blueprints/teacher.py`, templates, access matrix test, contract doc | — | Results, CSV, edit, duplicate, list for owner; 404 for others; list query count bounded | Practical review, dashboard | 6 |
| 8 | Practical review for owning teachers | `web/blueprints/practical.py`, `web/services/practical.py`, review templates, contract doc | — | Owner scope on listing and file download; 404 for foreign and unowned files before any file access; path safety unchanged | Upload limits, student practical flow, official resource downloads | 7 |
| T | Admin TOTP (separate security PR; design in §23) | `web/migrations/016_admin_totp.sql`, `web/services/totp.py`, `web/blueprints/account.py`, `login_totp.html`, `src/manage_accounts.py`, `web/production.py`, `requirements.txt`, Nginx, docs | 016 | §23 | Anything outside admin second factor; web enrolment | 4 |
| 9 | Cutover | `web/access.py`, `web/app.py`, `web/production.py`, `403.html`, env template, docs | — | Denials go to `/login`; 403 for wrong role; flag off disables legacy; production config validation | Deleting legacy code | 4, 5, T |
| 10 | Remove legacy | `web/blueprints/auth.py`, legacy templates, `ui_profile`, Nginx zones, `AGENTS.md` smoke checklist | — | Legacy routes gone; url-map snapshots updated; no reference to the removed keys | — | 9 |

Notes:

- PR 4 uses a new blueprint because `tests/test_auth_blueprint.py` asserts
  `auth.py` touches no database.
- PR T does not block PRs 1–8. It must land before PR 9 is deployed with the
  legacy flag off in production and before PR 10. Its design is §23.
- PRs 4, 6–8 and T each get an independent security review.
- PRs 1–10 and T are implemented. PR 10 additionally removed the flag that
  PR 9 introduced and the view-only tester exception of PRs 6–7, which had no
  remaining principal.

**Phase 2 — student accounts** (after phase 1): the migration-runner
directive (done); migration 015 with account-owned attempts, the `quiz_taker`
policy and a minimal "my attempts" page (done, §24); the `Lax` cookie (done,
§6); assignment `public_token` links; `user_identities` storage (done as
migration 019 and `web/services/identities.py`, §5: storage only); single-use
consumption storage (done as migration 020 and
`web/services/oidc_consumption.py`, §5 and §13: storage only); Google OIDC
sign-in on top of that storage (PR #331: implemented, not activated, §13);
identity recovery tooling, which activation waits for (not started);
Microsoft sign-in (not started). The DZI gate and entitlements follow, as in
the roadmap.

## 18. Test matrix

| Area | Required tests |
| --- | --- |
| Schema | 012–014 apply on an empty and an adopted database; CHECK and UNIQUE violations rejected; foreign-key check clean; every table created by exactly one migration. |
| Fresh canonical build | `users` and `login_throttle` empty; classification complete; canonical manifest unchanged. |
| Password hashing | Hash verifies; wrong password fails; stored method is scrypt; rehash on method change; length bounds; dummy verify on unknown email. |
| Login/logout | Success sets exactly `uid`, `sk`, `iat`; generic failure message for all four failure causes; logout is POST-only and clears the session. |
| Session | Pre-login keys cleared except attempt-owner keys; absolute lifetime enforced; key rotation invalidates other sessions; stale `uid` becomes anonymous. |
| Session cookie | `SameSite=Lax`, `HttpOnly`, `Secure` and no expiry on every `Set-Cookie` (sign-in of each role, both admin steps, password change, dropped identity, sign-out); no cookie for an anonymous request; every GET route shown to write nothing; a cross-origin unsafe request refused on every unsafe route with the cookie present. |
| Roles | Route × role matrix for student, teacher, admin, anonymous; admin cannot be created through any web path. |
| Bridge | Legacy matrix unchanged while the flag is on; account identity overrides legacy keys; flag off disables legacy routes. |
| Redirects | External, protocol-relative, schemed and relative `next` values fall back; role fallbacks. |
| Request integrity | Cross-origin and header-less POSTs to `/login`, `/logout`, `/account/password` get 403 before any session write; coverage test still passes. |
| Disabled accounts | Cannot sign in; an existing session stops working on the next request. |
| Throttle | Block after the limit; block applies to unknown identifiers; reset on success; no plaintext identifier stored. |
| Ownership | Owner, other teacher, admin, legacy tester on every assignment route; always 404 for non-owners. |
| Bootstrap | CLI creates admin; duplicate email rejected; requires explicit database; never opens the runtime database in tests. |
| Duplicate identities | Email uniqueness case-insensitively after normalization. |
| Existing behaviour | Quiz, teacher and DZI suites unchanged; url-map snapshot tests updated only for the new routes. |
| Performance | Anonymous request runs zero account queries; signed-in request runs exactly one; owner-scoped list has no per-row query. |
| Identity storage | 019 applies on a database at 018 and on an empty one and equals the approved schema; existing accounts, credentials, session keys, roles and ownership unchanged; provider CHECK, `UNIQUE (issuer, subject)` and the foreign key enforced, nothing cascades; lookup exact and case-sensitive; the same subject under another issuer is distinct; a duplicate never changes the owner; several identities per user; equal `email_at_link` merges nothing; helpers never commit or roll back, a caller rollback undoes them, and a write refused by its statement (a duplicate, an unknown identity id) leaves the caller a transaction to finish; table `RUNTIME`, empty in a fresh build, absent from every canonical export. |
| Consumption storage | 020 applies on a database at 019 and on an empty one, equals the documented schema and changes no other table or row; the key `CHECK` refuses every shape but 64 lower-case hexadecimal bytes, a trailing or embedded NUL and a trailing newline included; the integer affinity of the expiry is pinned; keys are deterministic and separated by purpose; malformed input (a boolean as a time included) is refused before any statement; a caller transaction is required; expiry is decided before any statement; success, duplicate, rollback and commit, with the caller's earlier writes preserved and nothing committed or rolled back by the service; cleanup at the 60/61-second boundary and by expiry only; no reuse across connections or after cleanup with the key's one expiry; a seeded multi-connection model; real concurrent callers with exactly one committed winner; a missing table, a read-only connection and a held lock propagate; only the primary-key duplicate is `AlreadyConsumed`; table `OPERATIONAL`, empty in a fresh build, absent from every canonical export. |

| Google sign-in: state | The transaction and the pending link hold exactly their documented keys and no secret, code or token; lifetimes on the monotonic clock, and the wall clock does not decide them (a wall clock set back makes Flask refuse the session cookie and the flow fails closed, which the suite characterises without weakening session validation); another instance is refused; the cookie budget; the clock is read after the write lock at all three call sites, with the object's own expiry and key; every outcome of a retirement for every way its record can fail to be written, never a false success (`tests/test_oauth_state.py`). |
| Google sign-in: provider | ID tokens with real RS256 signatures: accepted and refused signatures, issuers, audiences, times, nonces and subjects; every row of the token endpoint's response rules, one request and no retry; the key cache; the network budget, the bound of two workers inside the network function, every start failure, and the cooldown (`tests/test_google_oidc_service.py`). |
| Google sign-in: accounts | Onboarding creates one student and one identity or neither; the identity is resolved first; the email table; `bound_account`, the link decision in both places, `confirm_link` with every refusal; the races (`tests/test_oidc_accounts_service.py`). |
| Google sign-in: routes | Start, callback, confirmation and cancellation; methods on the callback address; a callback without a matching transaction writes nothing; the order of the callback's steps; admins; every row of what a cancellation may say, with its exact text; sign-out and every change of account under every storage failure; the disabled provider; no secret in any log (`tests/test_google_oidc_routes.py`). |
| Google sign-in: replay | One test per scenario of the design's 3.3; restored cookies, simultaneous callbacks, two instances on one database; what a copy of a cookie can still do after a retirement that was not recorded, and what a successor can still do (`tests/test_google_oidc_replay.py`). |
| Google sign-in: deployment | The activation gate with the constant off and on; Gunicorn's real error handler and parser against the log filter; a failing body read answers 400 and logs nothing; the Nginx and environment templates (`tests/test_google_oidc_production.py`). |

No tests are required for Microsoft sign-in, reset, verification or
registration until those features are built. Admin TOTP tests are listed in
§23. The Google sign-in suites use a fake provider with real cryptography and
temporary databases; they register no client and reach no network.

## 19. Production rollout

1. Finish the consolidated deployment and baseline adoption first.
2. Deploy PRs 1–4 with the legacy flag on: stop, back up, apply 012–013,
   start.
3. Create the admin with the CLI and verify sign-in; then create pilot teacher
   accounts.
4. Deploy ownership (014). Existing production assignments stay unowned and
   admin-only.
5. Land and deploy admin TOTP (PR T) and enrol the admin account.
6. Deploy the cutover (PR 9) after `manage_accounts cutover-check` passes and a
   real admin password + TOTP sign-in has been verified
   (`docs/DEPLOYMENT.md`, section 23). Production then has the legacy bridge
   off unconditionally; the two passwords in the env file are inert and are
   removed once a rollback to pre-cutover code is no longer wanted.
7. Deploy the removal (PR 10): no migration and no data change. Remove the
   leftover `DZI_ADMIN_PASSWORD`, `DZI_TESTER_PASSWORD` and
   `DZI_LEGACY_SHARED_LOGIN` lines from the env file and re-apply the Nginx
   template when convenient.

Restoring a backup revives the password hashes and session keys of that
moment. After any restore, run `manage_accounts sign-out` for all users.

## 20. Explicitly deferred

- Student accounts, attempt ownership, `signed_in` routes, assignment tokens,
  the `Lax` cookie (phase 2).
- Public registration, email verification, password reset, forced password
  change after an operator reset.
- Microsoft sign-in (phase 2); Apple entirely. Google sign-in is implemented
  and not activated (§13); what it defers is unlinking and identity recovery
  (required before activation), a local password or a second identity for an
  account created through Google, and any refresh of a name or an address
  after the identity was added.
- Web-based TOTP enrolment or self-service recovery (admin TOTP and recovery
  codes themselves are PR T, §23, CLI-only).
- `declared_type`, plans, entitlements, billing. (Teacher creation quotas are no longer
  deferred: registration PR 7 limits creations per account to 20 in a rolling 24 hours and
  10 open assignments, `docs/reviews/EMAIL_REGISTRATION_PR7_SCOPE.md`; it is implemented on PR #341, reviewed
  at `e39fa9f` and at `5f834ae` (each APPROVE WITH NON-BLOCKING NOTES), not activated, and does not change any role's access.)
- Student-data retention period and erasure tooling (product/legal decision).
- An admin web UI for users.

## 21. Decisions and remaining open items

Resolved by Gabriel (see §1): student authentication deferred to phase 2 with
OIDC preferred; session-bound OAuth transactions approved; TOTP is a separate
pre-cutover PR; retention/erasure deferred.

Still open, none of which blocks phase 1:

| Item | Needed by | Until decided |
| --- | --- | --- |
| Student-data retention period and erasure process | Before student accounts launch | Accounts are only disabled; nothing is deleted automatically. |
| Whether the МОН tenant allows third-party sign-in for `edu.mon.bg` | Phase 2 OIDC design | Assume it must be verified before relying on Microsoft sign-in for students. |
| Legacy unowned assignments | Any time | Keep `owner_user_id = NULL`; admin-only (roadmap default). |

## 22. Risks

- **Data loss in the attempts rebuild** if migration 015 runs without the
  table-rebuild directive. Mitigated by the directive (§16), the
  foreign-key check before commit, and a snapshot taken with the service
  stopped.
- **Two auth paths during the bridge.** Closed: PR 10 removed the bridge, so
  there is one auth path.
- **Single sync worker.** A password check costs noticeable CPU; a login flood
  stalls every user. The Nginx limit and the pre-hash throttle check are the
  defence.
- **Targeted lockout** of a known email for 15 minutes.
- **Cookie sessions cannot be listed or individually revoked.** Only per-user
  key rotation and the absolute lifetime exist.
- **A link from another site arrives signed in (`Lax`).** Accepted, for inbound
  quiz links and OIDC. A GET that changed state would then be forgeable from
  any site, so state changes stay POST-only behind the origin check, and
  `tests/test_session_cookie_samesite.py` fails for a GET route it has not
  shown to write nothing.
- **Test churn.** PR 5 touches many test files mechanically; keep it
  tests-only.
- **Gap 2 stays open until PR 6.** Any holder of the tester password can read
  all assignments' student names until then.
- **Admin without a second factor between PR 4 and PR T.** Mitigated by the
  14-character minimum, the 12-hour admin session and the login throttle; PR T
  closes it (§23), and migration 016 ends every earlier admin session.
- **The legacy env-password admin has no second factor.** Closed: unavailable
  in production since PR 9, and removed everywhere, development runs included,
  by PR 10.
- **Rollback to pre-cutover code revives the bridge**, including legacy cookies
  signed with the unchanged `DZI_SECRET_KEY`. Accepted: rotating the key would
  sign out everyone and break open student attempts, so it is an explicit
  emergency choice documented in `docs/DEPLOYMENT.md`, not part of the cutover.
- **Loss of `DZI_TOTP_KEY`** makes every TOTP secret unreadable (TOTP codes fail
  closed; a malformed configured key stops production startup); recovery codes
  still work, and `totp-reset` plus re-enrolment restores access.

## 23. Admin TOTP (PR T)

Status: implemented by PR T. It touches only the admin second factor; the legacy
shared-password bridge was left unchanged by it and was removed by PRs 9–10.

**Schema — `016_admin_totp.sql`** (014 and 015 stay reserved for assignment
ownership and the phase-2 attempts rebuild). Both tables are `RUNTIME`.

- `totp_credentials`: `user_id` (primary key, references `users`),
  `secret_ciphertext`, `confirmed_at`, `last_used_step`, `last_verified_at`,
  `created_at`, `updated_at`.
- `totp_recovery_codes`: `id`, `user_id` (references the credential,
  `ON DELETE CASCADE`), `code_hash`, `used_at`, `created_at`.
- The migration also rotates the `session_key` of every existing admin, so no
  admin session issued without a second factor survives the upgrade. This is
  deliberately in the migration, not in a deployment step: production refuses to
  start with 016 pending, so the rotation is guaranteed to happen before TOTP
  enforcement is live and cannot be forgotten. The runner applies it once,
  inside the same transaction as the schema change and its `schema_migrations`
  row; `--baseline-apply` never adopts 016 without running it. Only runtime
  `users` rows change (canonical builds have none, so it is a no-op there). The
  replacement key comes from SQLite's `randomblob`; it never needs to be
  unpredictable, because no cookie can carry it: the next admin session requires
  `totp-confirm`, which rotates the key again with `secrets`.

**Dependencies.** `pyotp>=2.9,<3` computes the RFC 6238 codes (SHA-1, 6 digits,
30 s); `cryptography` provides Fernet. No QR library; no hand-written TOTP.

**Secret storage.** The base32 secret (160 bits, `pyotp.random_base32`) is stored
only as a Fernet token under `DZI_TOTP_KEY`, a dedicated key that is not derived
from or shared with `DZI_SECRET_KEY`. There is no generated or fallback key. Unset,
the application still starts and admin TOTP sign-in fails closed (503); production
startup rejects a malformed value. Keys, secrets and codes are never logged.

**Enrolment (CLI only).** `manage_accounts totp-enroll` (admins only) creates an
unconfirmed credential and prints the `otpauth://` URI once. `totp-confirm`
activates it only after a current code verifies; that step becomes
`last_used_step`, and ten recovery codes are printed once. `totp-recovery-codes`
replaces them. `totp-enroll`, `totp-confirm` and `totp-recovery-codes` require a
current admin, so a demoted account cannot establish or regenerate usable admin
MFA. `totp-reset` works for any role (it only removes material, e.g. after a
demotion); it deletes the credential and its codes and is the administrative
recovery path. Enrol, confirm and reset rotate `session_key`.
There is no web enrolment or recovery route.

**Sign-in.**

1. `POST /login` for an `admin`: the password is verified as for everyone. With
   no confirmed credential the sign-in is refused (403, with a pointer to the
   server operator; shown only after a correct password). Otherwise the session
   is cleared (attempt-owner keys carried) and set to `mfa_uid`, `mfa_sk`,
   `mfa_iat` only. The throttle is not cleared and `last_login_at` is not set.
2. The pending keys are not account keys. `load_current_user` ignores them, so the
   principal is anonymous and the central `enforce_access_policy` denies every
   protected route; no view checks MFA itself.
3. `POST /login/totp` (public, same-origin) accepts the pending state only if it
   is at most **5 minutes** old (not in the future), the account is an active
   admin whose `session_key` still equals `mfa_sk`, the credential is confirmed,
   and `last_verified_at` is older than the pending state (a completed sign-in
   spends every earlier pending state, so a captured one cannot be replayed).
   Otherwise the pending keys are dropped.
4. On success it clears the throttle, records `last_login_at` and calls the
   normal session issuance: the session becomes exactly `uid`, `sk`, `iat`
   (plus attempt-owner keys). No role or authority flag is added.

**Verification.**

- TOTP: the current step ±1; a step at or below `last_used_step` is refused.
  One conditional `UPDATE` both checks and advances `last_used_step` and
  `last_verified_at`, so two concurrent requests cannot use one step or
  complete one pending state twice.
- Recovery codes: 12 characters (60 bits) from a 32-symbol alphabet, shown as
  `xxxx-xxxx-xxxx`, stored as Werkzeug scrypt hashes, each single-use through a
  conditional `UPDATE … WHERE used_at IS NULL`. They complete the same pending
  state and need no `DZI_TOTP_KEY`: they are an independent factor that does not
  depend on the encrypted secret, and their purpose is access when TOTP is
  unavailable for any reason, a lost or misconfigured key included (otherwise a
  key outage would lock out every admin until an operator ran `totp-reset`).
  A missing key is never attacker-controlled and does not weaken them. Every
  recovery-code sign-in is logged as a warning with the account id, the number of
  unused codes left and, if so, that `DZI_TOTP_KEY` is unavailable; no code is
  logged.
- Malformed, missing, wrong, reused and out-of-window codes all fail with one
  message and count as a failure.

**Throttling.** Codes count against the same per-account `login_throttle` key as
the password (HMAC of the email): ten failures in fifteen minutes block both
steps. Because a correct password never clears the counter, restarting the
password step does not reset code guessing. Nginx applies `lp_login` to
`/login/totp` as well.

**Session-key rotation and account changes.** Disable, role change, password
change, `sign-out`, `totp-enroll`, `totp-confirm` and `totp-reset` rotate
`session_key`, which ends existing sessions and voids any pending state. Logout
clears pending and full state alike. The 12-hour admin lifetime from `iat`
applies unchanged after MFA.

**Deployment.** Set `DZI_TOTP_KEY` before enrolling, apply 016 with the service
stopped, then enrol and confirm each admin with the CLI (§19 step 5).

**Tests.** Migration 016; the service (encryption, window, replay, concurrency,
recovery codes, reset, missing key); the two-step web flow (no authority before
the code, pending expiry and tampering, throttle, same-origin, redirects,
logout, lifetime, leaks, legacy bridge); and the CLI commands.

## 24. Account-owned quiz attempts (phase 2)

**Rule.** `quiz_attempts.user_id` decides which of two disjoint cases an
attempt is in; nothing else does.

| Attempt | Who may open, answer, see the result, upload practical work |
| --- | --- |
| Account-owned (`user_id` set) | Only the account with that id, whatever its role (student, teacher or admin). Any browser it is signed in on. The browser marker is never consulted. |
| Historical (`user_id` NULL) | Only the browser holding the attempt's `quiz_attempt_owner:<id>` session marker, exactly as before accounts. Being signed in changes nothing. |

- The decision is `web.access.can_access_attempt`; every student-side attempt
  route reaches it through `web.attempt_access.require_quiz_attempt_access`,
  which loads the row and answers 404 for a missing attempt, a foreign one
  and a missing marker alike. It has no teacher or admin bypass.
- **Who takes quizzes:** every account role — student, teacher and admin
  (`can_take_quizzes`, route policy `quiz_taker`). A teacher or admin takes a
  test to see what students see ("Отвори теста" on the assignment page) and
  gets an ordinary account-owned attempt of their own. Anonymous visitors, a
  pending admin second factor and disabled or stale sessions have no
  principal and do not qualify.
- **Management is not ownership.** Assignment ownership and the admin's
  global management authority (§9) cover managing assignments and reviewing
  submitted answers on the teacher pages. They never grant answer access to
  another account's attempt: the teacher who owns the assignment and the
  admin get the same 404 on a student's attempt as any stranger. Each account
  reaches only the attempts whose `user_id` is its own id.
- **New attempts need an account.** `POST /quiz/s/<public_token>` starts or
  resumes the attempt of the signed-in account, found by
  `(assignment_id, user_id)`. `user_id` comes only from the validated
  principal; nothing in the request names an account. An anonymous visitor
  sees the assignment page with a sign-in link and is redirected to
  `/login?next=/quiz/s/<public_token>` on POST. No new browser-owned attempt is created
  any more and no marker is written.
- **`student_name` is a display snapshot** of the account's `display_name` at
  the start, for teachers, exports and notes. It identifies nobody: renaming
  the account neither creates a second attempt nor changes the first, and two
  accounts with one name get separate attempts.
- **Seed.** `quiz_seed(assignment_id, "user:<user_id>")`. The stored seed and
  question plan stay authoritative; historical attempts keep theirs.
- **No linking of historical attempts.** They are never matched to an account
  by name, email or anything else, and signing in does not claim them. The
  NULL state is historical compatibility, not anonymous ownership.
- **Concurrency.** Two simultaneous starts of one account are resolved by
  `uniq_quiz_attempts_user`: the loser of the insert resumes the winner's row.
- **Practical work** has no owner column: student access follows submission →
  attempt, teacher review follows attempt → assignment → owner (unchanged).
- **My attempts.** `GET /account/attempts` (`quiz_taker`: any account) lists the account's own
  attempts, newest first, with a resume or result link; one query on
  `idx_quiz_attempts_user`. Historical attempts are not listed.
- **Disabled accounts.** Ownership is checked against the principal loaded
  from the `users` row on every request, so a disabled or re-keyed account
  loses its attempts with its session. The row keeps its `user_id`, so the
  account reaches it again once it can sign in.

**Known limits.** An account whose session ends in the middle of an attempt
gets 404 on it until signing in again (the attempt is then in "my attempts").
A teacher's or admin's own attempt is an ordinary attempt: it appears in the
assignment's results and exports next to the students' (a separate preview
mode does not exist).
Students enter only through the unguessable `/quiz/s/<public_token>` (migration
017; `docs/DEPLOYMENT.md` section 24). The sequential `/quiz/<id>` entry is
retired and answers a generic 404 without any redirect; numeric
teacher-management and attempt URLs are unaffected. How students get accounts is not solved here: accounts are
created by the operator CLI (§10).

**Rollout prerequisite.** After this version is deployed, a student without an
account cannot start a quiz. Create the accounts of the affected students
(`src.manage_accounts create`, `docs/DEPLOYMENT.md` section 22) before
deploying it for a class.

## Recommended next PR

**Current:** PRs 1–10 and T are implemented, and so are the runner's
table-rebuild support and migration `015_quiz_attempts_user.sql` with
account-owned attempts (§24), the first PR of phase 2. Since then the `Lax`
session cookie (§6), the `user_identities` storage (migration 019, §5) and the
single-use consumption storage (migration 020, §5) have been added, and
Google sign-in on top of them (PR #331, §13: implemented, not activated). The
text below is the original recommendation, kept as history; do not implement
it again.

**PR 1 — `users` schema and accounts service** (implemented)

- **Scope:** add the table and a service module with no caller in the web app.
- **Migration:** `012_users.sql`, exactly as in §5.
- **Expected behaviour:**
  - No route, template, session or access change; the url map and every
    existing test are unchanged.
  - `web/services/accounts.py` offers `normalize_email`, `password_problem`,
    `create_user`, `get_user_by_id`, `get_user_by_email`, `verify_password`,
    `set_password`, `set_role`, `set_active`, `rotate_session_key`.
  - Each function takes an open connection and the caller commits.
  - `create_user` generates `session_key` with `secrets.token_urlsafe(32)`.
- **Likely files:**
  - `web/migrations/012_users.sql`
  - `web/services/accounts.py`
  - `src/canonical_tables.py` (classify `users` as runtime)
  - `tests/test_users_migration_sql.py`
  - `tests/test_accounts_service.py`
- **Tests:** the schema, fresh-build, password-hashing and duplicate-identity
  rows of §18; the service must import neither `web.app` nor a blueprint.
- **Non-goals:** CLI, login, session handling, `Principal` changes, throttle,
  ownership, any OAuth or TOTP table, any canonical data change.
