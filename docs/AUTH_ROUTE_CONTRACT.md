# Auth Route Contract

Four separate concerns, deliberately kept apart:

- **Authentication** — who the session is. Account authentication is the only
  authentication the application has: an **account** identity (`uid`, `sk`,
  `iat`), issued through `web/account_session.py` and resolved to a principal
  on every request by `load_current_user()` in `web/app.py`. There are two
  ways to prove who the account is: a password, with a TOTP code for an admin
  (`web/blueprints/account.py`: `account.login`, `account.login_totp`,
  `account.logout`, `account.password`), and Google sign-in for a student or a
  teacher (`web/blueprints/oidc.py`; see "Google sign-in" below; **not
  activated**). Both end in the same session. The shared tester/admin
  passwords that preceded accounts were switched off in production by Accounts
  PR9 and physically removed by PR10; see "Removed shared-password
  authentication" below.
- **Authorization (access policy)** — whether that session may use a route,
  declared per view function in `web/access.py`.
- **Request integrity** — how an unsafe or external request proves it is
  legitimate (`web/request_integrity.py`): the same-origin check in
  `protect_same_origin_unsafe_methods()` for every unsafe request, and, for
  the one OAuth callback, the state of the transaction in the browser's own
  session (`verify_oauth_state()` in `web/oauth_state.py`). It is never an
  access level, and an access policy never exempts a route from it.
- **DZI content identity** — `web.services.dzi.is_dzi_question` says which
  questions are official DZI content. It is not an access check.

Roadmap PR D1 added the explicit declarations beside the old endpoint-prefix
guard and proved they agreed for every endpoint. PR D2 made them authoritative
and removed `protect_admin_routes()`, `TESTER_TEACHER_ENDPOINTS` and
`PUBLIC_DZI_ENDPOINTS`. There is now one route-level authorization mechanism.

## Enforcement

Every request passes through these four `before_request` steps, in this
order. The first two are request integrity, the third only establishes
identity, and the last is authorization:

1. `protect_same_origin_unsafe_methods()` — request integrity. Every
   POST/PUT/PATCH/DELETE must carry a same-origin `Origin` (or `Referer`), or
   it gets 403 (only the `TESTING` configuration accepts a request carrying
   neither header). No route is exempt, and being signed in does not help.
2. `verify_oauth_state()` — request integrity of the one view that declares
   `oauth_state`, the OAuth callback. It does nothing for any other request.
   It is **not** an exemption from step 1 and not an alternative to it: the
   callback accepts no unsafe method, so it never needs one. See "Google
   sign-in" below for what it checks. It runs before the principal is loaded,
   so a callback that does not match is answered without a database read.
3. `load_current_user()` — authentication only. If the session carries an
   account identity it is loaded with one `users` lookup; otherwise nothing
   happens (no query, no cookie). See "Account sessions" below.
4. `enforce_access_policy()` — authorization. It looks up the policy declared
   on the view function Flask resolved for the request and decides with
   `current_principal()`, which is the account principal `load_current_user()`
   resolved, or anonymous. A denied request without an account is redirected to
   `/login` with `next=<path>`, whatever the route's policy (undeclared views
   included); a denied request from a signed-in account gets 403 (`403.html`),
   because signing in again cannot help. A request that matched no route is
   left to Flask's own 404/405.

Passing one check never implies the other: an admin's cross-origin post is
refused, and a same-origin post from a session without access is still sent to
login.

## Removed shared-password authentication (Accounts PR9, PR10)

Before accounts, two shared passwords (`DZI_TESTER_PASSWORD`,
`DZI_ADMIN_PASSWORD`) signed a browser in as "tester" or "admin" through
`/tester/login` and `/admin/login`, recorded as the session keys
`tester_authenticated` and `admin_authenticated`, with a cosmetic `ui_profile`
key switched by `/profile/<profile>`. PR9 made that bridge unavailable in
production behind `DZI_LEGACY_SHARED_LOGIN`; PR10 deleted it. This section is
the only place the old names are described, as history.

What no longer exists:

- the routes `/tester`, `/tester/login`, `/tester/logout`, `/admin/login`,
  `/admin/logout` and `/profile/<profile>`, the `auth` blueprint
  (`web/blueprints/auth.py`) that owned them and their templates. The URLs
  answer like any unknown URL: 404 for GET, and for a same-origin POST; a
  cross-origin POST is refused with 403 by the integrity check, as for every URL;
- any reading of `tester_authenticated`, `admin_authenticated` or `ui_profile`.
  No code reads, writes or cleans up these keys. A cookie signed while the
  bridge existed may still carry them: they authorize nothing, change nothing
  that is rendered, and disappear when the browser signs in or out
  (`session.clear()`). No signing-key rotation is involved;
- `DZI_LEGACY_SHARED_LOGIN`, `app.config["LEGACY_SHARED_LOGIN"]`, and every
  parsing or validation of them, in development, tests and production alike.
  There is no switch: no environment variable or configuration value can bring
  shared-password sign-in back, because the implementation is gone;
- `DZI_TESTER_PASSWORD` and `DZI_ADMIN_PASSWORD` as settings. Nothing reads
  them; a line left in an old env file is inert and can be deleted;
- the `profile_switch` access level, the role-specific denial targets and the
  Nginx `lp_admin_login` / `lp_tester_login` rate-limit zones;
- the ID-less "tester" and "admin" principals. Every principal other than
  anonymous is loaded from a `users` row. The tester's view-only access to an
  unowned assignment's detail page is gone with it: unowned
  (`owner_user_id IS NULL`) assignments and their practical data are visible
  to and manageable by an account admin only. Nothing claims or backfills
  ownership.

A pending second factor (`mfa_*`) is anonymous whatever else the cookie holds;
only `POST /login/totp` creates the admin principal.

Regression tests inject the historical keys to prove they grant nothing
(`tests/test_account_auth_cutover.py`, `tests/test_auth_blueprint.py`,
`tests/test_access_policy.py`). The operator's notes are in
`docs/DEPLOYMENT.md`, section 23.

## Declared Access Policies

`@access_policy(...)` sits below `@app.route(...)` and attaches the policy to
the view function, so a blueprint-prefixed endpoint name
resolves to the same decision. An endpoint without a declared policy is denied;
a coverage test walks `app.url_map` and fails on any undeclared endpoint.

| Policy | Meaning | Routes |
| --- | --- | --- |
| `public` | any session, signed in or not | `/`, `/grade/<n>`, `/section/<slug>`, `/subjects/...`, `/physics...`, `/healthz`, `/favicon.ico`, sign-in (`/login`, `POST /login/totp`, `POST /auth/google/start`, `GET /auth/google/callback`), `POST /logout`, `/quiz/s/<public_token>`, registration (`/register`, `/register/student`, `/register/teacher`, `/register/verify`, `POST /register/resend`, `POST /register/cancel`; see "Student registration") |
| `teacher` | teacher or admin account | `/teacher/new`, `/teacher/assignment/<id>` |
| `admin` | admin account only (after the second factor) | `/admin/open-question-candidates`, `/dzi...`, every other `/teacher...` route |
| `signed_in` | any active account (student, teacher or admin) | `/account`, `/account/password`, the four `/practice...` routes (registered only when the practice build gate is armed, which it is not; see "Student practice"), `POST /auth/google/link`, `/auth/google/link/confirm`, `POST /auth/google/link/cancel` (the link routes admit the session; an admin is then refused by `POST /auth/google/link` and `POST /auth/google/link/confirm` with 403, while `GET .../confirm` redirects to the expired outcome and `POST .../cancel` answers `link_not_pending`; see "An admin session on the link routes") |
| `quiz_taker` | any account that may take quizzes: student, teacher or admin (`can_take_quizzes`) | `/account/attempts` |
| `assignment_owner` | admin account or teacher account; the view checks the loaded assignment and answers 404 | `/teacher/assignments`, `/teacher/assignment/<id>/edit`, `/duplicate`, `/close` (POST only), `/results` (GET and the open-answer review POST), `/results.csv`, `/qr.svg`, `/teacher/practical-submissions`, `/teacher/practical/submission-file/<file_id>/download` |
| `attempt_owner` | route is reachable; the view checks attempt ownership and answers 404 | `/quiz/attempt/<id>`, `/quiz/attempt/<id>/result`, `/quiz/attempt/<id>/practical/...` |

Flask's built-in `static` view cannot be decorated; `web.access` treats it as
`public` by name.

## Assignment Ownership (Accounts PR6/PR7/PR8)

Management of an assignment is decided by one pure predicate,
`web.access.can_manage_assignment(principal, owner_user_id)`:

- an admin Principal (an account admin after the second factor) manages every
  assignment, including unowned ones;
- a teacher account manages only rows whose `owner_user_id` equals its
  non-NULL `users.id`;
- everything else is denied: anonymous, students, a pending second factor
  (not a Principal), and a teacher on an unowned (NULL)
  or foreign row. Both ids must be non-NULL before they are compared, so a
  missing account id never matches a missing owner.

`web.access.assignment_management_scope` is its SQL counterpart: the list
filters `qa.owner_user_id = ?` in the query (index
`idx_quiz_assignments_owner`), with no per-row lookup; an admin gets every row.
`web/access.py` imports no controller or service, so practical review (PR8)
reuses both without import cycles.

The `assignment_owner` routes first pass the central policy (admin or teacher
account; anonymous is redirected to `/login`, a student account gets 403),
then load the assignment and answer 404 when it is missing, foreign or, for a
teacher, unowned. Results, CSV (filtered or not) and the open-answer review all
use the same gate; a reviewed answer must also belong to an attempt of the
assignment in the URL, so an answer id from another assignment is 404. Edit
changes only the title, the time limit and the deadline with its timezone; a
submitted `owner_user_id` is ignored, as is any closure, hold or token field.
A duplicate belongs to the acting account, never to the source's owner. Student/public quiz loading
(`quiz_fetch_assignment`) is not owner-scoped.

Creation limits (registration PR 7; `docs/reviews/EMAIL_REGISTRATION_PR7_SCOPE.md`). Creating an assignment, by either
form of `POST /teacher/new` or by `POST /teacher/assignment/<id>/duplicate`, is limited **per account**: at most 20
creations in a rolling 24 hours and at most 10 open assignments. The decision is made inside
`quiz_create_assignment`, in the lifecycle writer transaction, after authorization (a foreign or unowned source is still 404)
and after input validation; it never changes who may do what. A refusal answers the form again, with the typed values, a
Bulgarian message and status **429** (daily limit) or **409** (open limit; the daily refusal takes precedence when both apply, and a held but otherwise open assignment counts as open while a passed deadline frees a slot), and writes nothing: no assignment, public token,
ledger row, clock mark or vault note. A third refusal, status **503** with its own Bulgarian message, means the account's creation history could not be
placed with confidence (an unrecorded assignment with a NULL, malformed or implausible `created_at`, or an inconsistent ledger row); it is cleared by an
operator, not by the teacher (`docs/DEPLOYMENT.md`, section 36). Before the limits are decided the admission records the account's unrecorded
assignments from the age they show; a build that keeps migration 024 must keep it. Closing frees an open slot and never a creation; an admin account's creations are
recorded and not refused. The owner management pages (detail, edit, close, results, CSV, QR, practical review) are
unaffected at any count.

Assignment lifecycle (`docs/ASSIGNMENT_LIFECYCLE_DESIGN.md`). `POST
/teacher/assignment/<id>/close` closes a test for good: same policy
(`assignment_owner`, same-origin) and the same 404 as edit and duplicate; a
`GET` matches no rule (405). The request body is not read: the closure time is
the server's and the actor is the session principal, so an admin who closes
another teacher's test is recorded as the closer. No role can reopen a test and
admin authority adds no lifecycle action. The order of decisions on every
assignment and attempt route is fixed: authorization first (404, 403 or the
login redirect exactly as before), then the hold (503 for a test that is
temporarily not served), then the lifecycle state (409 for an edit of a closed
test or a start or submission that came too late). A lifecycle response
therefore never reveals the existence or state of someone else's test or
attempt. A session waiting for its second factor is anonymous for all of it.

Assignment detail retains the `teacher` route policy and shows the row to
exactly whoever may manage it (`quiz_can_access_assignment` is
`can_manage_assignment` on the loaded row); there is no view-only exception.
Missing or inaccessible assignments return 404 before attempts are read.
Creation records the creator's `users.id`. Rows created before ownership
existed remain NULL without a backfill; they are admin-only. The `/teacher` dashboard stays
`admin`.

### Practical review (Accounts PR8)

Practical submission review uses the same policy, predicate and SQL scope; the
assignment is the ownership boundary and the practical tables carry no owner
column:

```
practical_submission_files.practical_submission_id -> practical_submissions.id
practical_submissions.quiz_attempt_id              -> quiz_attempts.id
quiz_attempts.assignment_id                        -> quiz_assignments.id
quiz_assignments.owner_user_id
```

- `/teacher/practical-submissions` and
  `/teacher/practical/submission-file/<file_id>/download` are
  `assignment_owner`: an admin or a teacher account passes the route policy;
  anonymous and a pending second factor are redirected to `/login`, a student
  account gets 403.
- The list filters `qa.owner_user_id = ?` in its one joined query
  (`assignment_management_scope`); an admin gets every submission. Stored files
  of rows outside the scope are never looked up on disk.
- The download loads the file row joined through to the assignment owner and
  checks `can_manage_assignment` before anything else. A foreign, unowned or
  missing file id answers the same 404.
- Authorization precedes filesystem access: the deleted flag, the stored-path
  confinement (`practical_uploaded_file_safe_path`), the on-disk check and
  `send_file` run only for an authorized principal, and are unchanged.
- Practical data of an unowned (NULL) assignment is admin-only.
- The student practical page and upload stay `attempt_owner` and are not
  owner-scoped. The official resource download
  (`/dzi/practical/resource/<id>/download`) and `/dzi...` stay `admin`.

`signed_in` is used by the account pages (`/account`, `/account/password`, `/account/profile`) and the
Google link routes.

`current_principal()` never reads the session: it returns the principal
`load_current_user()` put on `g`, or anonymous.

Every route that accepts an unsafe method declares `@integrity_policy(SAME_ORIGIN)`.
`oauth_state` and `provider_signature` are reserved for future external flows
and are not in use.

## Account sessions

Accounts are created by `python3 -m src.manage_accounts`, and, once Google
sign-in is activated, a first Google sign-in creates a `student` (see "Google
sign-in"). Once student registration is activated, a verified registration
creates a `student` too (see "Student registration"; **not activated**). There
is no password reset reachable in production (self-service recovery by an emailed code exists in the
build, see "Password recovery"; **not activated**, and the operator's `set-password` stays the recovery
path), and no teacher registration. A teacher or an admin account comes from the operator only. A profile (names and class, or school
and subjects) is a declaration and never an authority; see "Account profile".

**Session contents.** A successful `POST /login` (for an admin, the completed
`POST /login/totp`) clears the session, carries
over only `quiz_attempt_owner:*` keys, and sets `uid`, `sk` (the user's
`session_key`) and `iat` (issue time, epoch seconds). Role, name and active
state are never in the cookie; they are read from the `users` row on every
request.

**Session cookie.** One cookie, `session`: `Secure`, `HttpOnly`,
`SameSite=Lax`, `Path=/`, no `Domain`, and no `Expires` or `Max-Age` (it ends
with the browser session; there is no remember-me). Every response that writes
or clears it uses these attributes: a sign-in, the admin's pending second
factor and its completion, a password change, a dropped stale identity and the
sign-out. `Secure` is unconditional in production (`web.production` sets it and
startup rejects a false `DZI_SESSION_COOKIE_SECURE`); that variable exists only
for plain-HTTP local runs. `SameSite` is not configurable.

It was `SameSite=Strict` until phase 2 of accounts. `Lax` means the browser
sends the cookie with a top-level navigation that starts on another site (a
followed link, a redirect, a GET form) and with no other cross-site request:
not with a cross-site POST, nor with an image, frame or script request. What
follows from that is part of this contract:

- A signed-in visitor who follows a link from another site arrives signed in.
  Google's redirect back to the callback relies on it
  (`docs/ACCOUNTS_DESIGN.md` §13): it arrives with the cookie of the start.
- No GET (or HEAD) changes state on the visitor's behalf: nothing is signed
  out, created, edited, closed, submitted or reviewed by one, and `GET /logout`
  does not exist (405). A new state change is POST-only and declares
  `@integrity_policy(SAME_ORIGIN)`. Flask routes `HEAD` to the GET view, and
  `HEAD` is not an unsafe method, so no same-origin check or `POST` rate key
  meets it: every GET/POST form view (`/login`, `/account/password`,
  `/account/profile`, `/auth/google/link/confirm`, registration and recovery)
  takes its read path for every method that reaches it except `POST`, which is
  `GET` and `HEAD` (`OPTIONS` gets Flask's automatic answer after the
  before-request hooks and never runs the view; any other method reaches no
  view: 405, or 403 first when cross-origin; neither policy is changed here). A
  `HEAD` answers what the `GET` of the same request answers, with an empty body,
  and never reaches the form action, whatever body or origin it carries
  (`tests/test_account_head.py`, `tests/test_registration_head.py`). There are
  two exceptions, each with its own reason:
  - the lifecycle observation stamp with its clock bookkeeping
    (`docs/ASSIGNMENT_LIFECYCLE_DESIGN.md` §3.6, §6.3): the server's clock, no
    request input, written once, and caused by an anonymous request just the
    same;
  - **`GET /auth/google/callback`**, which signs an account in, creates a
    student or prepares a link. That is what a provider's redirect is, and it
    is why the callback has an integrity mechanism of its own (`oauth_state`)
    instead of none. A callback that does not carry the state of a transaction
    in the browser's own session changes nothing at all: no cookie, no row, no
    outbound request. A link from another site cannot have that state.
- Request integrity does not depend on the attribute. Step 1 of "Enforcement"
  refuses every cross-origin unsafe request whatever cookie arrives with it,
  including one from another host of the same site, which `SameSite` never
  tells apart.
- An anonymous request sets no cookie, and a GET with a valid session does not
  rewrite it (only a stale identity is dropped, see "Fail closed").

`tests/test_session_cookie_samesite.py` pins all of it; it fails for a GET or
unsafe route that is not in its sweeps.

**Fail closed.** The account keys are the only identity the application reads.
An identity that does not resolve (unknown or disabled user, key mismatch,
expired or malformed `iat`) makes the request anonymous and drops the account
keys from the cookie; there is nothing else to fall back to.

**Absolute lifetime.** Measured from `iat`, by the role in the database:
30 days for `student` and `teacher`, 12 hours for `admin`. A missing or
malformed `iat` is expired.

**Login (`/login`).** The identifier is the normalized email. In order: the
per-identifier throttle is checked (blocked → 429, no password hashing); the
account is looked up and the password verified, against a dummy hash when there
is no account or no password; a failure is recorded against the throttle and
answered with one message; a success clears the throttle, replaces the session,
records `last_login_at` and redirects. Unknown email, wrong password, disabled
account, account without a password and a malformed identifier are
indistinguishable: same page, status 200, „Грешен имейл или парола.“
`GET /login` renders the form, or sends an already signed-in account on (a pending
second factor still gets the form). A `HEAD` takes that same read path: it never
checks the throttle, verifies a password, records or clears a failure, records a
login, or issues an account session or a pending second factor, whatever it carries
and from whatever origin.

**Throttle.** Ten failures in fifteen minutes block the identifier for fifteen
minutes, whether or not an account exists: 429 with „Твърде много опити. Опитай
отново след няколко минути.“ Rows in `login_throttle` are keyed by
`HMAC-SHA256(SECRET_KEY, normalized email)`; no attempted email is stored or
logged. A malformed identifier is throttled under a bounded key that no valid
email can equal. Expired rows are deleted when a failure is recorded. Nginx adds
a per-IP limit on `POST /login` (`lp_login`, 1 r/s, burst 40). An operator lifts
one email's lockout with `manage_accounts clear-throttle --email <email>`.

**Admin second factor (`POST /login/totp`, PR T).** For an account whose role is
`admin`, a correct password does not issue the account session. If the account has
no confirmed TOTP credential the sign-in is refused (403, „Профилът изисква
двуфакторно удостоверяване, което още не е настроено. …“); otherwise the session is
replaced (attempt-owner keys carried over) by a pending second factor `mfa_uid`,
`mfa_sk`, `mfa_iat` and the code form is rendered. The pending keys are not account
keys: `load_current_user` never reads them, so the request stays anonymous and every
protected route is denied by the same central policy check. `POST /login/totp` (public,
same-origin) accepts the pending state only if it is at most 5 minutes old, names an
active admin whose `session_key` still matches, the credential is confirmed, and no
second factor completed since the pending state was issued; otherwise it drops the
pending keys and asks for a new sign-in. The code is a TOTP code (current step ±1,
only a step newer than `last_used_step`) or an unused recovery code. Failures,
including malformed and missing codes, count against the same per-account throttle
as the password form, and the password step never clears it; a blocked account gets
429. Success clears the throttle, records `last_login_at` and issues exactly `uid`,
`sk`, `iat` through the normal session issuance, then redirects through
`safe_redirect_target` (fallback `/teacher`). Without a usable `DZI_TOTP_KEY` a TOTP
code fails closed with 503 and is not counted. Nginx applies the `lp_login` limit to
`/login/totp` too. Enrolment and reset exist only in `manage_accounts`.

**Redirect after login.** `next` is accepted only if `safe_redirect_target`
approves it, when the form is rendered and again when it is used. Otherwise the
role decides: admin → `/teacher`, teacher → `/teacher/new`, student → `/`.

**Logout (`POST /logout`).** Clears the whole session of this browser and
redirects to `/`. It does not rotate `session_key`, so other devices stay signed
in; "sign out everywhere" is `manage_accounts sign-out`. There is no `GET
/logout` (405), and it is the only sign-out route.

**Password change (`/account/password`).** Requires the current password, a new
password that meets the account policy (10 characters, 14 for an admin, at most
256, not the email) and its confirmation. A wrong current password counts
against the same throttle as the login form, and a blocked identifier gets 429.
Success sets the hash, rotates `session_key` in one transaction, and re-issues
this browser's session with the new key and the original `iat`; every other
session of the account stops working on its next request. Only a `POST` does any of
this: a `HEAD` renders what the `GET` renders and verifies, hashes, counts, clears,
re-keys and re-issues nothing.

An account without a password (one created through Google) gets a notice
instead of the form, for a GET and a POST alike: it has no password to change,
and no throttle failure is counted against it.

**Account page.** `GET /account` (`signed_in`) shows the account's name, role
and email and how it signs in: whether a password is set, and each Google
identity with the address Google reported and the date. It reads only. It is
also where the outcome of a Google link is shown, and where a link is started.

**Sign-out.** `POST /logout` clears this browser's session and redirects to
the home page, for every account. It does not rotate `session_key`, so other
devices stay signed in, and it does not sign the browser out of Google.

**One place clears a session.** `web/account_session.py` holds the three
functions that replace or clear it: `issue_account_session`,
`issue_pending_second_factor` and `end_session`. No controller calls
`session.clear()` itself. Each of the three first tries to retire a Google
link in progress (see "Google sign-in") and goes on whatever that achieved.

**Templates.** `current_account` is the account principal or `None`, and is
what navigation and role-gated controls test (`current_account.is_admin`,
`can_list_assignments`). A signed-out page offers one link, „Вход“ (`/login`);
a signed-in one links to „Профил“ (`/account`) and „Парола“. The footer shows
the account's role label, and nothing for a signed-out visitor.
`google_sign_in_enabled` says whether the Google button and forms exist.

**Caching.** Every response to a signed-in account carries
`Cache-Control: no-store`. Anonymous, `static` and `/healthz` responses are not
changed.

## Google sign-in

Specification: `docs/reviews/GOOGLE_OIDC_DESIGN.md`; summary and guarantees:
`docs/ACCOUNTS_DESIGN.md` §13. **It is not activated.** It is enabled only with
both credentials and a configured public base URL, and a production start of
this build keeps it disabled whatever is configured
(`docs/DEPLOYMENT.md`, section 29). Disabled, every route below answers the
generic 404 page after the same-origin check (a cross-origin POST is still
403), no page shows a Google button, form or message, and nothing reads the
OAuth state in a cookie.

| Route | Endpoint | Methods | Access | Integrity |
| --- | --- | --- | --- | --- |
| `/auth/google/start` | `oidc.google_start` | POST | `public` | `same_origin` |
| `/auth/google/link` | `oidc.google_link` | POST | `signed_in` | `same_origin` |
| `/auth/google/callback` | `oidc.google_callback` | GET | `public` | `oauth_state` |
| `/auth/google/link/confirm` | `oidc.google_link_confirm` | GET, POST | `signed_in` | `same_origin` |
| `/auth/google/link/cancel` | `oidc.google_link_cancel` | POST | `signed_in` | `same_origin` |

**An admin session on the link routes.** The three link routes admit any
signed-in session. The first three rows below are pinned by
`tests/test_google_oidc_routes.py` (`AdminTest`); the cancel row is read from
`web/blueprints/oidc.py`, which has no role check there:

| Request by an admin account | Answer |
| --- | --- |
| `POST /auth/google/link` | `403`, no `Location`, nothing stored |
| `POST /auth/google/link/confirm` | `403`, before any pending link is read; a pending link in the cookie is left alone |
| `GET` or `HEAD /auth/google/link/confirm` | `302` to `/account?google=expired`: the pending link of an admin is never shown, and it is left in the cookie |
| `POST /auth/google/link/cancel` | `302` to `/account?google=link_not_pending` for an admin with nothing held and no Google identity. The route has no role check; it answers like any account (`account_linked` if the account has a Google identity) |

The blueprint is `oidc`. No path says "login" or "logout", and no route
registers, resets or unlinks anything.

**Onboarding and the profile gate (registration PR 6).** With the profile gate
off, a first Google sign-in redirects exactly as before and writes no mark. With
it on, the callback that creates a student (`CREATED`) writes
`users.profile_required_at` in the same transaction as the account and the
identity, and redirects to `/account/profile?next=<validated target>`; a failure
of any of the three writes rolls back all of them. A repeated sign-in, an
account that existed before, an email collision, a teacher and an admin are never
marked and redirect as before. See "Account profile".

**The `oauth_state` mechanism.** Exactly one view declares it, and a view that
declares it accepts no unsafe method; `provider_signature` stays unused. A
start puts a transaction into the signed session: state, nonce, PKCE verifier,
intent, validated return target, expiry, the service instance and, for a link,
the binding to the account session. The hook then decides, for the callback
address only:

| Request | Result |
| --- | --- |
| `HEAD`, `OPTIONS` | 405 with `Allow: GET`. Nothing is read, and the view does not run: a `HEAD` never redeems a code. |
| `POST`, `PUT`, `PATCH`, `DELETE` | No view resolves: 403 from step 1 when cross-origin, else Flask's 405. |
| `GET` whose `state` is missing, repeated, malformed or not the stored transaction's | **No match:** `302` to `/login?google=failed`. No cookie, no database access, no outbound request, and the stored transaction is left alone (clearing it would let any site cancel a sign-in in progress). |
| `GET` that matches a transaction of another run of the service | The slot is removed; `expired`. Nothing is recorded. |
| `GET` that matches | The slot is removed, and the use is recorded in its own committed write **before any other parameter is looked at** and before any request to Google. A second use, an expired transaction and every storage error end the request there. |

The view runs only for a transaction the hook matched and recorded, and it
never renders a page: every outcome, an unexpected exception included, is a
redirect to a local path with `Cache-Control: no-store` and
`Referrer-Policy: no-referrer`. The path carries one of a fixed set of codes
(`?google=<code>`) that the sign-in page or the account page looks up; no value
is ever echoed.

**Intent** is fixed by the route that starts the flow and travels inside the
signed transaction. A sign-in callback that meets a signed-in account fails
without touching the session. A link callback without its bound session fails
without signing anyone in. A link callback writes no identity.

**Sign-in.** `POST /auth/google/start` reads only `next`. A signed-in account
starts nothing. The identity is found by `(issuer, subject)`; an email never
selects, signs in or links an account. A new identity becomes a `student`
unless its reported address is the sign-in email of an existing account, in
which case nothing is created and the person is told to sign in with the
password and link from „Профил“. An inactive account and an admin's identity
are refused. The session issued is the ordinary one, and the return target
passes `safe_redirect_target` when stored and again when used, with the role
read at the write as its fallback.

**Linking.** `POST /auth/google/link` asks for nothing: a student or teacher
with a password and no Google identity is sent to Google. After the callback
the session holds a pending link (for at most 180 seconds) and the browser is
on the confirmation page, which names both profiles. `POST
/auth/google/link/confirm` stores the identity only with the account's current
password, under the account's throttle counter (429 when blocked), and checks
the account, its role and active state, its session key, the bound session's
lifetime, the expiry and the link decision again inside the write transaction.
After a rollback the pending link stays, and the answer claims nothing about
what reached the disk. A `GET` or `HEAD` of the confirmation only reads: it shows
the page for a live pending link bound to this session, and otherwise redirects to
`expired` and leaves whatever slot the cookie holds (missing, malformed, of
another instance, expired, or an admin's) as it was; only the `POST` drops it. It
checks no password, writes no throttle row, records no consumption and stores no
identity.

**Cancelling** (`POST /auth/google/link/cancel`) removes both linking objects
from this browser's session, tries to record that they have ended, reads once
whether the account has a Google identity, and answers with the first that
applies:

| Finding | Code | Text |
| --- | --- | --- |
| The account could not be read | `cancel_unconfirmed` | „Не успяхме да потвърдим отказа.“ |
| The account has a Google identity | `account_linked` | „Профилът ти вече е свързан с Google.“ |
| A record could not be written | `cancel_unconfirmed` | „Не успяхме да потвърдим отказа.“ |
| Its own record ended every linking object the session held | `link_cancelled` | „Свързването с Google беше отказано.“ |
| Anything else: nothing held, or an object already used, expired or unusable | `link_not_pending` | „Няма свързване с Google, което тази заявка да прекрати.“ |

It never reports a cancellation that the server did not record or could not
establish. Each answer is a statement about that request and about the account
as it was read, not about what follows.

**Copies of a cookie.** The cookie is signed, not encrypted, and the server
cannot take one back: removing a linking object from a response revokes no
earlier copy. What stops a copy is the consumption record, the service
instance and the expiry. A sign-out, a new link, a cancellation and every
change of the session's account try to write that record; when it cannot be
written they go on (only the cancellation says so), and a copy then stays
usable until it expires, within the checks of the confirmation.

## Student registration

Specification: `docs/reviews/EMAIL_REGISTRATION_DESIGN.md` (sections 2.3, 4.3,
6.2 to 6.4); implementation and its contract:
`docs/reviews/EMAIL_REGISTRATION_PR4_STATUS.md`. **It is not activated.** It
is enabled only with usable mail settings, and a production start of this build
keeps it disabled whatever is configured: the build gate
`registration.PRODUCTION_ACTIVATION` (`web/services/registration.py`) is off
and no setting overrides it (`docs/DEPLOYMENT.md`, section 33). Disabled, every
route below answers the generic 404 page after the same-origin check (a
cross-origin POST is still 403) and no page links to it.

| Route | Endpoint | Methods | Access | Integrity |
| --- | --- | --- | --- | --- |
| `/register` | `registration.choose` | GET | `public` | — |
| `/register/student` | `registration.student` | GET, POST | `public` | `same_origin` |
| `/register/teacher` | `registration.teacher` | GET, POST | `public` | `same_origin` |
| `/register/verify` | `registration.verify` | GET, POST | `public` | `same_origin` |
| `/register/resend` | `registration.resend` | POST | `public` | `same_origin` |
| `/register/cancel` | `registration.cancel` | POST | `public` | `same_origin` |

- **`/register/teacher` is behind `registration.TEACHER_ACTIVATION`** (decision D5,
  registration PR 7; the build gate is `False` and nothing overrides it), read at each request. While it
  is off the route is the generic 404 after the same-origin check, the chooser has no teacher entry and no page
  links to it; a teacher registration that is pending is **voided and scrubbed** at its next resend or
  verification, answers the „временно не е достъпна“ page, drops the binding, and never becomes a student.
  With it on (not in production), the form takes first and last name, email, the exact password and its
  confirmation, school name, one to three subjects of the registry and „Друг предмет“ with its name; its
  hidden `next` follows the student form's rules. The account type is this route's literal: no submitted field
  (`role`, `account_type`, `is_admin`, `grade`, `email_verified_at`) is read.
- **The browser binding is the session slot `reg`**: `{"v": 1, "t": <token>,
  "next": <validated path or "">}`. No address, name, code or password is in
  the cookie. It is not an account key: `load_current_user` never reads it,
  and `issue_account_session`, `issue_pending_second_factor` and `end_session`
  drop it with everything else (the first two carry over only the
  `quiz_attempt_owner:*` markers). A slot of any other shape is ignored.
  Verify, resend and cancel find the registration by the keyed hash of the
  slot's token and by nothing else; another browser has no registration.
- **No GET or HEAD writes a registration row or issues a registration slot.** The verify page reads only the
  caller's own row; expiry is written by the POSTs and by the cleanup. `student`, `teacher` and `verify` take the
  read path for `GET` and `HEAD`, the methods other than `POST` that reach them: a `HEAD` is a read and never
  submits or tries a code, whatever it carries, and a slot the browser already holds is left unchanged (correction
  recorded in `docs/reviews/EMAIL_REGISTRATION_PR4_STATUS.md`, merged in #347; host evidence pending). The cleanup
  of a stale account session is unrelated to this slot.
- **Every restart goes to the chooser.** Cancel, and every page that ends a registration (expired, cancelled elsewhere, replaced, no binding,
  teacher gate off, service unavailable, failure) link „Започни регистрацията отначало“ to `/register` with the stored `next` when it passed
  `accepted_next`, never to `/register/student` or `/register/teacher`: the binding does not say which form began the flow, and neither is offered
  to someone who began the other. The binding and lifecycle rules are unchanged. **The unavailable (503), failure (500) and ended pages keep the same validated
  target**: the submit pages take it from the validated form value, every other page from the revalidated slot, read before the binding is dropped, so a teacher
  whose gate was turned off, a failed verification or a resend during a restore hold still returns to the saved page. No binding, or a slot of another shape, gives a page
  with no target; a raw request value is never used and an unsafe one is never echoed.
- **A signed-in account** is sent to its role's home by every `/register...`
  route, and a POST of it starts nothing. No other route is affected.
- **Verification does not sign in (decision D7).** A correct code creates the
  `student` account, or, behind the teacher gate, the `teacher` account with its profile and subjects in one
  transaction (`email_verified_at` set) and redirects to
  `/login?next=<validated>&registration=activated`; sign-in is email and
  password, as for every account, and never asks for a code. Admin TOTP and
  Google sign-in are unchanged. `/login` shows the outcome only for a known
  `registration` code and only while registration is enabled; it echoes no
  value.
- **Transactions**: each service writer runs inside `BEGIN IMMEDIATE` begun by
  the route; nothing (session, response, durable log line, mail) acts on its
  outcome before `COMMIT` has returned; an exception or a failed commit rolls
  back and answers the generic failure (500). The code or notice of an
  issuance is handed to the mailer once, by the request that committed it,
  with no transaction open, and settled afterwards in its own transaction by
  generation number and issuance identity; never after `NotSendable`.
- **Responses** of every `/register...` route carry `Cache-Control: no-store`
  and `Referrer-Policy: no-referrer` (the Nginx template sends no second value
  for these addresses). No registration value is in a URL.
- **A repeated or late verification.** Only a registration that was already
  activated redirects silently to `/login` (no session, nothing written). A
  registration that expired, was cancelled or was replaced answers the „Регистрацията
  е изтекла или е започната от друг браузър“ page with the validated `next`.
- **`next`** passes `safe_redirect_target` and, for registration only, refuses
  any Unicode C or Z character; it is checked when the form is rendered, when
  the slot is read and again by `/login`.

## Password recovery

Specification: `docs/reviews/PASSWORD_RECOVERY_DESIGN.md` (sections 2.2 to 2.8, 3.5, 3.6, 7.1, 7.5
and 8.1); implementation and its contract: `docs/reviews/PASSWORD_RECOVERY_8_3_STATUS.md`
(scope 8.3; `dd39615` was reviewed with REQUEST CHANGES, and the correction `2c2a579` with APPROVE WITH NON-BLOCKING NOTES; neither review approves activation). **It is not activated.** It is enabled only with usable
mail settings, and a production start of this build keeps it disabled whatever is configured: the
build gate `recovery.PRODUCTION_ACTIVATION` (`web/services/recovery.py`) is `False`, no setting or
environment variable overrides it, and it is independent of the registration, teacher and profile
gates (`docs/DEPLOYMENT.md`, section 38). Disabled, every route below answers the generic 404 page
after the same-origin check, for every visitor (a cross-origin POST is still 403), and `/login` has no
link and no notice. **Known unresolved discrepancy**: with the gate off `OPTIONS` answers 200 with an
`Allow` header and a wrong method on a route (`PUT`, `PATCH`, `DELETE`, or `GET`/`HEAD` on the POST-only
`/recover/resend` and `/recover/cancel`) answers 405, so these addresses can still be told apart from an
unknown path (404); registration behaves the same way. No decision has been recorded and neither
blueprint's method policy was changed (`PASSWORD_RECOVERY_8_3_STATUS.md`).

| Route | Endpoint | Methods | Access | Integrity |
| --- | --- | --- | --- | --- |
| `/recover` | `recovery.start` | GET, POST | `public` | `same_origin` |
| `/recover/verify` | `recovery.verify` | GET, POST | `public` | `same_origin` |
| `/recover/resend` | `recovery.resend` | POST | `public` | `same_origin` |
| `/recover/cancel` | `recovery.cancel` | POST | `public` | `same_origin` |

- **Order, on every route**: the application's same-origin check (every unsafe method, before the
  access hook), then the gate (the generic 404), then the signed-in redirect, then the session slot.
  A redemption then reads this browser's row (`pending_view`), judges the password against the address
  that row holds (`accounts.password_problem(password, view.email)`), compares the confirmation exactly,
  and only then calls the service inside `BEGIN IMMEDIATE`. A refusal at an earlier step answers before
  a later step reads anything, and no step before the transaction writes or counts an attempt.
- **A signed-in account** is sent to its role's home by every `/recover...` route (`303`), and a POST of
  it starts nothing; the session is unchanged. A `HEAD` is a read and never submits, whatever it carries.
- **The browser binding is the session slot `rec`**: `{"v": 1, "t": <token>, "next": <validated path or
  "">}`. No address, code or password is in the cookie, a URL or a redirect. It is not an account key:
  `load_current_user` never reads it, and `issue_account_session`, `issue_pending_second_factor` and
  `end_session` drop it with everything else (the first two carry over only the `quiz_attempt_owner:*`
  markers). It is independent of the registration slot `reg`; a browser may hold both. A slot of any
  other shape (other keys, version, token shape, a `next` the rule refuses) is ignored; another browser
  has no recovery, and a copy of the cookie is the same browser and still needs the code.
- **No GET writes to the database**, and a GET with no usable recovery sets no cookie. The verify page
  reads only the caller's own row and never the account (`pending_view`); expiry is written by the POSTs
  and by the cleanup. The application's own stale-session cleanup in `load_current_user` is separate and
  unchanged: a request whose cookie carries an account identity that no longer resolves may still have those
  keys removed (a `Set-Cookie`), so no claim is made that no cookie is ever sent.
- **Transactions**: each service writer runs inside `BEGIN IMMEDIATE` begun by the route; nothing
  (the slot, the response, a durable log line, mail) acts on its outcome before `COMMIT` has returned. An
  exception **before** the commit followed by a rollback that returned means nothing took effect, and the
  page says the password was not changed; a commit that raised, or a rollback that raised, leaves an outcome
  the route cannot establish, and the page says so (`Не успяхме да потвърдим…`). No failed commit is ever
  presented as a reset. The code or notice of an issuance is handed to the mailer once, by the request that
  committed it, with that request's own result and no transaction open, and settled afterwards in its own
  transaction by generation number **and** issuance identity; never after `NotSendable`, and never retried.
- **A correct code does not sign in (decision D7).** It sets the password (one step: code, new password and
  confirmation in one POST), drops the slot and redirects to `/login?next=<validated>&recovery=done`. Sign-in
  is email and password, as for every account, and never asks for a code. The sign-in throttle is neither read
  nor written by any recovery route (RD8 (a), RD10): a reset clears nothing, and wrong codes count nowhere
  but in the row.
- **`/login?recovery=done`** shows a fixed notice selected by the URL, by lookup, never by echo, and only
  while recovery is enabled. It changes no state and **proves no reset**: a hand-built URL shows it too.
  `/login` links „Забравена парола?“ to `/recover?next=<validated>` only while recovery is enabled.
- **Responses** of every address under `/recover` (the four routes, an unknown path, a refused method, a 403
  and the disabled 404 included) carry `Cache-Control: no-store` and `Referrer-Policy: no-referrer` from the
  application; in production the Nginx template hides that copy and sends exactly one `no-referrer` itself
  (`docs/DEPLOYMENT.md`, section 38). The code input is `autocomplete="one-time-code"` with
  `inputmode="numeric"`, the password inputs `autocomplete="new-password"`; every control has a label, and a
  field with an error is `aria-invalid` and described by its error.
- **`next`** passes `forms.accepted_next` when the form is rendered, when it is stored in the slot and when the
  slot is read, and `safe_redirect_target` at `/login`; success goes to `/login`, never to `next` directly, and
  a refused value is dropped and never echoed. The unavailable (503), failure (500), ended and invalidated pages
  link back to `/recover` and `/login` with the same validated target.
- **A repeated or late redemption.** The row is read first (`pending_view`, design 2.4). When it finds nothing
  usable, the route asks one more read-only question of the same browser-bound row
  (`recovery.was_redeemed(conn, binding)`; a malformed or absent binding answers false). If this browser's
  own row is `redeemed` (a double click, a lost response, or a retry after a commit whose outcome was not
  established), the slot is dropped and the answer is `303` to `/login?next=<validated next>`, **without**
  `recovery=done` (it is a repeat, not a new success), with nothing written, sent, hashed or signed in, and
  before the password, confirmation or code are judged. Any other state (expired, cancelled or invalidated
  (`void`), absent, a pending row past its time) and every missing, malformed or foreign slot stays the
  „Заявката за нова парола е изтекла…“ page with the slot dropped on a POST. The service's own
  `NOT_PENDING (redeemed)` redirect is the same answer for a row that becomes final between the read and
  the transaction (two tabs posting together). The redirect is available **while the redeemed row exists**:
  cleanup makes a final row eligible for deletion once it is older than 24 hours (the elapsed time alone
  deletes nothing), and after the row has actually been deleted the same request is the ended page.

## Account profile

Specification: `docs/reviews/EMAIL_REGISTRATION_DESIGN.md` (sections 7.3 and
7.4); implementation and its contract:
`docs/reviews/EMAIL_REGISTRATION_PR6_STATUS.md`. **It is not activated.** The
build gate `profiles.PRODUCTION_ACTIVATION` (`web/services/profiles.py`) is off
and no setting overrides it in production (`docs/DEPLOYMENT.md`, section 35). A
development run turns the flow on with `DZI_PROFILES_ENABLED`; a production
start never reads that setting.

| Route | Endpoint | Methods | Access | Integrity |
| --- | --- | --- | --- | --- |
| `/account/profile` | `account.profile` | GET, POST | `public` at the hook; the view requires a signed-in account (below) | `same_origin` |

- **Order.** The policy is `public` on purpose: the access hook runs before the
  view, so `signed_in` would redirect an anonymous visitor to the login before
  the gate was looked at. The view checks, in this order: the same-origin
  integrity check (a separate before-request step: a cross-origin POST is 403
  whatever the gate says), the gate (off: the generic 404), then the `signed_in`
  rule with `access.is_route_allowed` and the same redirect to
  `/login?next=/account/profile` (anonymous, pending second factor, disabled or
  stale session).
- **Gate off (the build's default).** `/account/profile` is the generic 404 for
  everybody, anonymous visitors included: no redirect to the login, no cookie, no
  session, no write (approved by Gabriel).
  `/account` shows no profile section and no link to it. Google onboarding
  redirects exactly as before and writes no mark. Starting an attempt is exactly
  as before. A `users.profile_required_at` that exists stays stored and is
  ignored.
- **Gate on.** The form is the one of the account's **stored profile type**,
  whatever its role is now: a student-type profile shows first name, last name
  and class (8 to 12); a teacher-type profile shows first name, last name,
  school and subjects. An account with no profile is offered the student form
  while its current role is `student`; a teacher or an admin without a profile
  is told that there is none, is offered no form, and a POST creates nothing
  (403). No teacher onboarding exists. No email or password is asked for, read
  or stored, and no field named `role`, `account_type`, `is_admin`,
  `email_verified_at` or `profile_required_at` is read.
- **A save is one transaction owned by the view** (`BEGIN IMMEDIATE`, commit,
  rollback after any exception): the profile and, when the names are new, the
  `display_name` are written together or not at all. The role, email,
  `email_verified_at`, active state and `session_key` are never written, so every
  session stays valid; `quiz_attempts.user_id`, the `quiz_attempt_owner:*`
  markers and every stored `student_name` are untouched. Later attempts copy the
  new `display_name`. A failure answers the generic failure (500) and shows
  nothing as saved.
- **Who is marked.** A student created through Google onboarding while the gate
  is on gets `profile_required_at` in the transaction that creates the account,
  and is redirected to `/account/profile?next=<validated target>`. Nobody else is
  marked: not an existing Google account, a CLI-created account, an existing
  student or a teacher or an admin; no command marks existing accounts.
- **Where it is asked.** `profiles.needs_profile` is true only when the gate is
  on, the account's current role is `student`, `profile_required_at` is set and
  no `user_profiles` row of any type exists. It is read in one place:
  `quiz_start_or_resume_attempt`, under its writer lock, at the point where an
  attempt would be created. The order of `POST /quiz/s/<public_token>` is: not
  signed in or not allowed (as before); an existing attempt resumes or shows its
  result (closed assignment or not); a closed assignment shows its closed notice;
  only then `needs_profile`, answered with a `303` to
  `/account/profile?next=/quiz/s/<token>` and no attempt row; otherwise the attempt
  is created as before. The view looks up no attempt of its own. The new attempt's
  `student_name` is `users.display_name` as committed at the insert, selected by
  the `INSERT` itself under the writer lock (the service takes no name argument),
  so a name loaded before the request waited for the lock is never stored;
  existing attempts keep their snapshots. Attempt pages,
  saving, submitting, results, `/account/attempts`, browsing and the account pages
  are never blocked.
- **`next`** is kept only when the registration rule keeps it (a safe same-site
  path without a Unicode C or Z character, at most 512 characters) when the form
  is rendered and when it is submitted, and passes `safe_redirect_target` again
  when it is used. A target that was offered (a query string or a submitted field)
  and refused becomes the current role's home and is never echoed: the form
  remembers the refusal as the fixed hidden field `next_refused=1` (a yes/no, not
  the target), so a validation retry or the GET-then-POST round trip still ends at
  the role's home; a kept `next` always wins over the indicator. With no target and
  no indicator the saved profile is shown. The return is a plain GET: completing a
  profile never starts an attempt.
- **A role change** (`set-role`) never writes, deletes or reinterprets a profile
  or the mark. A marked student promoted to teacher or admin is no longer asked;
  demoted again, is asked again. A profile keeps the type it was declared with.
- **Only a `POST` saves.** A `HEAD` takes the `GET`'s path after the same gate and
  access checks (the 404 with the gate off, the login redirect, then the page): it
  validates nothing, writes no profile, no `display_name` and no change to the
  requirement, whatever body it carries, for every role and profile state; the
  `saved` notice is read for it as for the `GET`.
- **Responses** to a signed-in account carry `Cache-Control: no-store` like every
  account page.

## Safe redirects

`web/redirects.py` owns `safe_redirect_target`, which accepts only a same-site
absolute path and rejects `//host`, a scheme or host, a backslash (`/\host` is
read as `//host` by browsers) and control characters. Its callers are the
account login, Google sign-in, registration and the profile page (the last two add
their own stricter rule, see "Student registration" and "Account profile"): `next` is validated when the form is
rendered (for Google: when the transaction is created) and again when it is
used. A denied request is redirected with `next=<request path>`, never with
a client-supplied target or the `Referer`. The redirect to Google is the
pinned authorization endpoint plus encoded parameters: no part of its host or
path comes from a request, and the callback address in it is built from the
configured public base URL, never from a `Host` header.

## Quiz Attempts

**Starting.** The student link is `/quiz/s/<public_token>`, where the token is
the assignment's `quiz_assignments.public_token` (`secrets.token_urlsafe(32)`,
43 URL-safe characters; `web/assignment_tokens.py`). The route is `public`: the
page shows the assignment to anyone who has the link. A malformed or unknown
token, and the missing token of an assignment that has not been backfilled yet,
are the same generic 404. `POST` starts or resumes an attempt only for a
signed-in account (`web.access.can_take_quizzes`: student, teacher or admin):
the attempt is the one with that `(assignment_id, user_id)`, where `user_id` is
the validated principal's id and nothing submitted with the request. An
anonymous `POST` (also a pending admin second factor or a disabled account's
session) is redirected to `/login?next=<the request path>`.
Knowing a token or an assignment id gives no access to any attempt.

The token route is the only student entry. The earlier sequential entry
`/quiz/<assignment_id>` is **retired**: the application has no rule for it, so
every such URL, for any method and any session, is the generic 404 of an
unknown URL. There is no lookup, so an existing assignment, one without a
token and a missing one are indistinguishable; there is no redirect to the
token URL, no sign-in redirect, no token in the response and no attempt is
started or resumed. (A cross-origin `POST` is refused with 403 by request
integrity before routing matters.) Numeric student links shared before the
retirement stop working; the teacher shares the token link from the assignment
page instead. Do not reintroduce a numeric entry or an id-to-token redirect.

Every link the application produces for students (teacher pages, the result
page, generated assignment notes) is built by `quiz_student_entry_path` and
uses the token; an assignment without a token gets no link rather than a
numeric one.

The teacher's assignment page also offers that link as a QR code:
`GET /teacher/assignment/<id>/qr.svg` (`?download=1` for an attachment),
policy `assignment_owner`, so the owner or an admin only and 404 for any other
teacher or a missing assignment. It encodes the absolute token link on the
trusted public base URL (`web/public_url.py`: `https://DZI_PUBLIC_HOST` in
production), never the request's `Host`, and reads no parameter that could
change what is encoded. An assignment without a valid token has no QR code
(404), and without a trusted base URL the endpoint answers 503 and the page
shows a notice instead of the link and the QR code; the displayed link is the
same canonical one and is never built from the request's `Host`. Nothing is
stored. See `docs/DEPLOYMENT.md`, section 25.

Numeric ids remain in two other, unrelated places and keep working: the
teacher-management routes (`/teacher/assignment/<id>` and its `/edit`,
`/duplicate`, `/results`, `/results.csv`, `/qr.svg`), which are owner/admin only, and the
account-owned attempt routes (`/quiz/attempt/<attempt_id>...`) described below.

`GET /login` with an already valid account session redirects to the validated
`next` (or the role's home) instead of showing the form. A signed-in student
who follows an external link to an assignment arrives signed in (the cookie is
`SameSite=Lax`, see "Session cookie") and gets the start form at once. A
pending admin second factor is not an account session and still gets the form.

**Attempt routes** have the `attempt_owner` policy: the route-level check lets
every session through, and the view makes the object-level decision, answering
404 (not 403) so attempt ids cannot be probed:

- `/quiz/attempt/<attempt_id>`
- `/quiz/attempt/<attempt_id>/result`
- `/quiz/attempt/<attempt_id>/practical/<source_slug>`
- `/quiz/attempt/<attempt_id>/practical/<source_slug>/task/<exam_task_id>/upload`

Each route must call `require_quiz_attempt_access(conn, attempt_id)` and use the
row it returns before exposing or mutating attempt data. It applies
`web.access.can_access_attempt`:

| `quiz_attempts.user_id` | Allowed |
| --- | --- |
| set (account-owned) | the account with that id (student, teacher or admin), from any browser; the browser marker is not consulted |
| NULL (started before accounts) | the browser whose session holds `quiz_attempt_owner:<attempt_id>`; the signed-in account is not consulted |

Management authority is separate and never grants answer access: the teacher
who owns the assignment and an admin reach only their own attempts here, like
everyone else, and review other accounts' answers only on the `/teacher...`
pages. A missing attempt, another account's attempt (whoever asks), a disabled
or signed-out account and a historical attempt without the marker all answer
the same 404. New attempts
are always account-owned, so the marker is no longer written; it is only read,
for historical attempts, and is still carried across a sign-in. Historical
attempts are never linked to an account.

`/account/attempts` (`quiz_taker`) lists the signed-in account's own account-owned
attempts.

## Student practice

Design: `docs/STUDENT_PRACTICE_DESIGN.md` (sections 3 to 6). Services: `web/services/practice.py`
(start, submit, saved-result loader and the read-only attempt view). Routes:
`web/blueprints/practice.py`. **The routes are not registered.** The build constant
`practice.ROUTES_ACTIVATION` (`web/services/practice.py`) is `False`, no setting and no environment
variable overrides it, and `web/app.py` registers the blueprint only when it is true, in development and
in production alike. The ordinary application therefore has no `/practice` rule, endpoint or blueprint:
every such URL is the generic 404 and the rest of the URL map, every gate and every policy are exactly
what they were. Tests arm the constant only in a separate interpreter, before it imports the genuine
`web.app` (`tests/test_practice_routes.py`). Arming it, a browse link and a navigation entry are a
separate approved launch change.

| Route | Endpoint | Methods | Access | Integrity |
| --- | --- | --- | --- | --- |
| `/practice/section/<int:section_id>/start` | `practice.start_section` | POST | `signed_in` | `same_origin` |
| `/practice/section/<int:section_id>/topic/<int:topic_id>/start` | `practice.start_topic` | POST | `signed_in` | `same_origin` |
| `/practice/session/<int:session_id>` | `practice.attempt` | GET (and HEAD), POST | `signed_in` | `same_origin` |
| `/practice/session/<int:session_id>/result` | `practice.result` | GET (and HEAD) | `signed_in` | none (safe methods only) |

The scheme is independent of `/quiz`. Every `/quiz` path, the retired numeric `/quiz/<assignment_id>`
404 included, is unchanged.

- **Hooks.** A practice request passes the application's own hooks, in the order of "Enforcement": the
  same-origin check (every POST; a cross-origin one is 403 whatever the account, before any view or
  write), the account loader, then the `signed_in` decision: signed out, or a pending second factor, is
  redirected to `/login?next=<path>` and nothing is written; a disabled account never reaches a view.
  No role is refused here: a student, a teacher and an admin each practice, and each only on sessions it
  owns.
- **Inputs.** The ids in the path, and on submit the fields `q_<question id>` whose value is the *stored*
  option key (`multiple_choice_options.option_letter`), never the display letter. Everything else in a
  request is ignored: no role, account id, seed, plan, score, timestamp or label is ever read from it. A
  field sent twice is unanswered. The owner is always `current_principal()`.
- **Ownership.** One answer for a missing session and for one owned by another account, a teacher or an
  admin: the generic 404 (`404.html`), on the attempt page, its submit and the result, so an id cannot
  be probed. An open or an abandoned session has no result: the same 404.
- **No write on a read.** `GET` and `HEAD` of the attempt and of the result read one snapshot
  (`load_practice_attempt`, `load_practice_result`) and write nothing; the attempt view keeps the stored
  plan order and seed and applies the eligibility rules of a start and a submit, so it shows exactly the
  questions a submit would grade. It never renders a correct marker, verdict or explanation.
  `POST /practice/session/<id>` and the start routes write only through `submit_practice` and
  `start_or_resume_practice`.

| Situation | Answer |
| --- | --- |
| Start: session created or resumed | `303` to `/practice/session/<id>` |
| Start: section or topic missing, not a member, no single subject, or a DZI training section; an id of 0 or beyond the integer range | `404`, Bulgarian "not available" page (`practice_unavailable.html`), nothing written |
| Start: fewer than the minimum of eligible questions | `409`, Bulgarian "not enough questions" page; a stale open session of the scope is abandoned and not replaced |
| Attempt `GET`: own open session | `200` with the form |
| Attempt `GET` or `POST`: own submitted session | `GET` `302`, duplicate `POST` `303`, both to its own result; nothing written |
| Attempt: missing, foreign (teacher and admin too) | `404` |
| Attempt: own abandoned (replaced, old tab) | `409`, Bulgarian page with a POST to start the same scope again; nothing graded or adopted |
| Attempt: own open session that presents no question any more | `409`, Bulgarian page with a POST to start the same scope again |
| An own session whose scope is no longer available, or a malformed stored plan (start, attempt, submit, result alike) | `409`, one Bulgarian page that names no stored state |
| Submit: usable choices | `303` to the result; answers, verdicts and score stored once, server-side |
| Submit: no usable choice | `400`, the form again with a Bulgarian message; nothing stored, the session stays open |
| Result: own submitted session | `200`: the saved score, the shared multiple-choice review, the skipped count when there is one, and "Нов опит" as a `POST` to the session's own scope |
| Any other inconsistent stored state | `500` (`500.html`), logged by exception class only; no detail in the page |
| Account unavailable at the service | `403` (`403.html`) |

Known limits of this slice. The start routes accept `POST` only, so a signed-out visitor is redirected to
`/login?next=<start path>` and the return `GET` there is a `405`; nothing links to a start route until the
launch change adds the browse entry and the sign-in return. There is no timer, no progress indicator, no
script beyond the highlight of the chosen option, and the pages show no section or topic title.

## Refactor Note

Blueprint names change Flask endpoint names, but declared policies live on view
functions, so a rename or a blueprint move cannot change a
route's access decision. A new view must declare `@access_policy(...)` (and
`@integrity_policy(...)` if it accepts unsafe methods); until it does, every
session is denied and the coverage test in `tests/test_access_policy.py` fails.
The same module pins that exactly one view declares `oauth_state` and that it
accepts no unsafe method.

## Endpoint names

Every endpoint is blueprint-qualified: `account.*`, `browsing.*`, `dzi.*`,
`oidc.*`, `practical.*`, `practice.*` (registered only while the practice build gate is armed), `quiz.*`, `recovery.*`, `registration.*`, `teacher.*`. The unprefixed quiz endpoints
(`quiz_start`, `quiz_attempt`, `quiz_result`) and the `auth.*` endpoints no
longer exist.
