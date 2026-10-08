# Auth Route Contract

Four separate concerns, deliberately kept apart:

- **Authentication** — who the session is. Account authentication is the only
  authentication the application has: an **account** identity (`uid`, `sk`,
  `iat`), issued by `web/blueprints/account.py` (endpoints `account.login`,
  `account.login_totp`, `account.logout`, `account.password`) and resolved to a
  principal on every request by `load_current_user()` in `web/app.py`. The
  shared tester/admin passwords that preceded accounts were switched off in
  production by Accounts PR9 and physically removed by PR10; see "Removed
  shared-password authentication" below.
- **Authorization (access policy)** — whether that session may use a route,
  declared per view function in `web/access.py`.
- **Request integrity** — how an unsafe or external request proves it is
  legitimate (`web/request_integrity.py`; today the same-origin check in
  `protect_same_origin_unsafe_methods()`). It is never an access level, and an
  access policy never exempts a route from it.
- **DZI content identity** — `web.services.dzi.is_dzi_question` says which
  questions are official DZI content. It is not an access check.

Roadmap PR D1 added the explicit declarations beside the old endpoint-prefix
guard and proved they agreed for every endpoint. PR D2 made them authoritative
and removed `protect_admin_routes()`, `TESTER_TEACHER_ENDPOINTS` and
`PUBLIC_DZI_ENDPOINTS`. There is now one route-level authorization mechanism.

## Enforcement

Every request passes through these `before_request` steps, in this order. The
first and the last are the two independent checks; the one between them only
establishes identity:

1. `protect_same_origin_unsafe_methods()` — request integrity. Every
   POST/PUT/PATCH/DELETE must carry a same-origin `Origin` (or `Referer`), or
   it gets 403 (only the `TESTING` configuration accepts a request carrying
   neither header). No route is exempt, and being signed in does not help.
2. `load_current_user()` — authentication only. If the session carries an
   account identity it is loaded with one `users` lookup; otherwise nothing
   happens (no query, no cookie). See "Account sessions" below.
3. `enforce_access_policy()` — authorization. It looks up the policy declared
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
| `public` | any session, signed in or not | `/`, `/grade/<n>`, `/section/<slug>`, `/subjects/...`, `/physics...`, `/healthz`, `/favicon.ico`, sign-in (`/login`, `POST /login/totp`), `POST /logout`, `/quiz/<assignment_id>` |
| `teacher` | teacher or admin account | `/teacher/new`, `/teacher/assignment/<id>` |
| `admin` | admin account only (after the second factor) | `/admin/open-question-candidates`, `/dzi...`, every other `/teacher...` route |
| `signed_in` | any active account (student, teacher or admin) | `/account/password` |
| `quiz_taker` | any account that may take quizzes: student, teacher or admin (`can_take_quizzes`) | `/account/attempts` |
| `assignment_owner` | admin account or teacher account; the view checks the loaded assignment and answers 404 | `/teacher/assignments`, `/teacher/assignment/<id>/edit`, `/duplicate`, `/results` (GET and the open-answer review POST), `/results.csv`, `/teacher/practical-submissions`, `/teacher/practical/submission-file/<file_id>/download` |
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
changes only the title and time limit; a submitted `owner_user_id` is ignored.
A duplicate belongs to the acting account, never to the source's owner. Student/public quiz loading
(`quiz_fetch_assignment`) is not owner-scoped.

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

`signed_in` is used only by `/account/password`.

`current_principal()` never reads the session: it returns the principal
`load_current_user()` put on `g`, or anonymous.

Every route that accepts an unsafe method declares `@integrity_policy(SAME_ORIGIN)`.
`oauth_state` and `provider_signature` are reserved for future external flows
and are not in use.

## Account sessions

Accounts are created only by `python3 -m src.manage_accounts`; there is no
registration, reset or OAuth route.

**Session contents.** A successful `POST /login` (for an admin, the completed
`POST /login/totp`) clears the session, carries
over only `quiz_attempt_owner:*` keys, and sets `uid`, `sk` (the user's
`session_key`) and `iat` (issue time, epoch seconds). Role, name and active
state are never in the cookie; they are read from the `users` row on every
request. The cookie itself is unchanged: a browser-session cookie, `Secure`,
`HttpOnly`, `SameSite=Strict`, no remember-me.

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
`GET /login` always renders the form.

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
session of the account stops working on its next request.

**Templates.** `current_account` is the account principal or `None`, and is
what navigation and role-gated controls test (`current_account.is_admin`,
`can_list_assignments`). A signed-out page offers one link, „Вход“ (`/login`).
The footer shows the account's role label, and nothing for a signed-out
visitor.

**Caching.** Every response to a signed-in account carries
`Cache-Control: no-store`. Anonymous, `static` and `/healthz` responses are not
changed.

## Safe redirects

`web/redirects.py` owns `safe_redirect_target`, which accepts only a same-site
absolute path and rejects `//host`, a scheme or host, a backslash (`/\host` is
read as `//host` by browsers) and control characters. The account login is its
only caller: `next` is validated when the form is rendered and again when it
is used. A denied request is redirected with `next=<request path>`, never with
a client-supplied target or the `Referer`.

## Quiz Attempts

**Starting.** `/quiz/<assignment_id>` is `public`: the page shows the
assignment to anyone. `POST` starts or resumes an attempt only for a signed-in
account (`web.access.can_take_quizzes`: student, teacher or admin): the attempt
is the one with that `(assignment_id, user_id)`, where `user_id` is the
validated principal's id and nothing submitted with the request. An anonymous
`POST` (also a pending admin second factor or a disabled account's session) is
redirected to `/login?next=/quiz/<assignment_id>`.
Knowing an assignment id gives no access to any attempt.

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

## Refactor Note

Blueprint names change Flask endpoint names, but declared policies live on view
functions, so a rename or a blueprint move cannot change a
route's access decision. A new view must declare `@access_policy(...)` (and
`@integrity_policy(...)` if it accepts unsafe methods); until it does, every
session is denied and the coverage test in `tests/test_access_policy.py` fails.

## Endpoint names

Every endpoint is blueprint-qualified: `account.*`, `browsing.*`, `dzi.*`,
`practical.*`, `quiz.*`, `teacher.*`. The unprefixed quiz endpoints
(`quiz_start`, `quiz_attempt`, `quiz_result`) and the `auth.*` endpoints no
longer exist.
