# Auth Route Contract

Four separate concerns, deliberately kept apart:

- **Authentication** — who the session is. Since the cutover (Accounts PR9)
  account authentication is the only authority in production:
  - an **account** identity (`uid`, `sk`, `iat`), issued by `web/blueprints/account.py`
    (endpoints `account.login`, `account.login_totp`, `account.logout`, `account.password`) and resolved
    to a principal on every request by `load_current_user()` in `web/app.py`;
  - the **legacy shared passwords** (`tester_authenticated`,
    `admin_authenticated`), owned by the login/logout routes in `web/blueprints/auth.py` (endpoints `auth.tester_login`, `auth.admin_login`, `auth.tester_logout`, `auth.admin_logout`, `auth.switch_profile`, `auth.tester_home`),
    are a transitional bridge that exists only while `DZI_LEGACY_SHARED_LOGIN`
    is enabled, which production never allows. See "Legacy shared-login
    cutover" below; wherever this document describes a legacy tester or admin
    session, it describes that bridge and nothing that can happen in production.
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
   `current_principal()`. With the legacy bridge disabled: a shared-password
   route answers 404 first; a denied request without an account is redirected
   to `/login` with `next=<path>`, whatever the route's policy; a denied
   request from a signed-in account gets 403 (`403.html`), because signing in
   again cannot help. With the bridge enabled the transitional behaviour
   applies: every denial is a redirect, to `/tester/login` for `teacher`
   routes, `/login` for `signed_in` and `assignment_owner` routes, and
   `/admin/login` for everything else, including undeclared views. A request
   that matched no route is left to Flask's own 404/405.

Passing one check never implies the other: an admin's cross-origin post is
refused, and a same-origin post from a session without access is still sent to
login.

## Legacy shared-login cutover (Accounts PR9)

One setting decides whether the shared-password bridge exists:
`DZI_LEGACY_SHARED_LOGIN`, held as the boolean `app.config["LEGACY_SHARED_LOGIN"]`
and read through `web.access.legacy_shared_login_enabled()`. Only the boolean
`True` enables it; a missing value means disabled.

| Where | Value |
| --- | --- |
| Production (`web.wsgi`) | Always disabled. `web/production.py` refuses to start when the variable is enabled (`1`/`true`/`yes`/`on`) or not a recognised spelling, accepts unset, empty or a false spelling (`0`/`false`/`no`/`off`), and then forces the configuration to `False`. |
| Development (`web.app` imported directly) | Enabled when unset, so the documented local tester/admin smoke flow keeps working. An explicit value is parsed with the same spellings; an unrecognised one disables the bridge with a warning. |
| Tests | `tests.support.isolated_app` enables it explicitly; the cutover tests disable it. |

With the bridge **disabled**:

- `/tester/login`, `/admin/login`, `/tester/logout` and `/admin/logout`
  (the views marked `legacy_shared_login_route`) answer 404 for GET and POST,
  for every session, before any access decision and without touching the
  session. A correct shared password gets the same 404.
- `current_principal()` never reads `tester_authenticated` or
  `admin_authenticated`. A request without a resolved account is anonymous
  whatever its cookie holds, so a cookie signed before the cutover authorizes
  nothing. This is the decision itself, not a cleanup: `load_current_user()`
  additionally drops the two keys from any cookie that still carries them, but
  no authority depends on that having happened. No signing-key rotation is
  involved.
- `ui_profile` remains cosmetic and is clamped to the Principal; with stale
  keys it cannot show an admin profile.
- A pending second factor (`mfa_*`) next to stale legacy admin keys is still
  anonymous. Before the cutover that combination was a legacy admin, because
  `/admin/login` could be completed while the account's second factor was
  pending; now only `POST /login/totp` creates the admin Principal.
- Every sign-in denial targets `account.login`; no redirect points at a
  shared-password route. A signed-in account that lacks the role gets 403.
  Object-level ownership answers are unchanged: a foreign, unowned or missing
  assignment, attempt or practical file is still 404.
- Templates receive `legacy_shared_login_enabled` and offer only „Вход“
  (`/login`); `tester_authenticated` and `admin_authenticated` are computed
  from the Principal and are therefore false for stale keys.
- The legacy tester and the legacy admin do not exist as principals. The
  tester's view-only access to unowned assignment detail is unreachable;
  unowned (`owner_user_id IS NULL`) assignments and their practical data are
  manageable by an account admin only. Nothing claims or backfills ownership.

The bridge code itself (views, templates, `ui_profile`, `/profile/<profile>`,
`/tester`, the Nginx zones for the two logins) is still present and is removed
by PR10. The operator's preflight and recovery procedure are in
`docs/DEPLOYMENT.md`, section 23.

## Declared Access Policies

`@access_policy(...)` sits below `@app.route(...)` and attaches the policy to
the view function, so a blueprint-prefixed endpoint name
resolves to the same decision. An endpoint without a declared policy is denied;
a coverage test walks `app.url_map` and fails on any undeclared endpoint.

| Policy | Meaning | Routes |
| --- | --- | --- |
| `public` | any session, signed in or not | `/`, `/grade/<n>`, `/section/<slug>`, `/subjects/...`, `/physics...`, `/tester`, `/healthz`, `/favicon.ico`, logins (`/login`, `POST /login/totp`, `/tester/login`, `/admin/login`), `POST /logout`, `/tester/logout`, `/quiz/<assignment_id>` |
| `teacher` | teacher or admin account, or a tester or admin session | `/teacher/new`, `/teacher/assignment/<id>` |
| `admin` | admin account or admin session only | `/admin/...`, `/dzi...`, every other `/teacher...` route, `/admin/logout` |
| `signed_in` | any active account (student, teacher or admin); a legacy tester/admin session is not an account | `/account/password` |
| `assignment_owner` | admin (account or legacy session) or teacher account; the view checks the loaded assignment and answers 404 | `/teacher/assignments`, `/teacher/assignment/<id>/edit`, `/duplicate`, `/results` (GET and the open-answer review POST), `/results.csv`, `/teacher/practical-submissions`, `/teacher/practical/submission-file/<file_id>/download` |
| `attempt_owner` | route is reachable; the view checks attempt ownership and answers 404 | `/quiz/attempt/<id>`, `/quiz/attempt/<id>/result`, `/quiz/attempt/<id>/practical/...` |
| `profile_switch` | `/profile/<profile>`: `admin` needs an admin session, other targets are public | `/profile/<profile>` |

Flask's built-in `static` view cannot be decorated; `web.access` treats it as
`public` by name.

## Assignment Ownership (Accounts PR6/PR7/PR8)

Management of an assignment is decided by one pure predicate,
`web.access.can_manage_assignment(principal, owner_user_id)`:

- an admin Principal (account admin after the second factor, or the legacy
  admin session) manages every assignment, including unowned ones;
- a teacher account manages only rows whose `owner_user_id` equals its
  non-NULL `users.id`;
- everything else is denied: anonymous, students, the legacy tester, a
  pending second factor (not a Principal), and a teacher on an unowned (NULL)
  or foreign row. Both ids must be non-NULL before they are compared, so a
  missing account id never matches a missing owner.

`web.access.assignment_management_scope` is its SQL counterpart: the list
filters `qa.owner_user_id = ?` in the query (index
`idx_quiz_assignments_owner`), with no per-row lookup; an admin gets every row.
`web/access.py` imports no controller or service, so practical review (PR8)
reuses both without import cycles.

The `assignment_owner` routes first pass the central policy (admin or teacher
account; anonymous, students and the legacy tester are redirected to `/login`),
then load the assignment and answer 404 when it is missing, foreign or, for a
teacher, unowned. Results, CSV (filtered or not) and the open-answer review all
use the same gate; a reviewed answer must also belong to an attempt of the
assignment in the URL, so an answer id from another assignment is 404. Edit
changes only the title and time limit; a submitted `owner_user_id` is ignored.
A duplicate belongs to the acting account (NULL for a legacy admin session),
never to the source's owner. Student/public quiz loading
(`quiz_fetch_assignment`) is not owner-scoped.

Assignment detail retains the `teacher` route policy and shows the row to whoever
may manage it, plus the legacy tester on an unowned row. That legacy branch is
temporary view-only compatibility until the cutover: it shows no management
controls and grants none of the routes above. Missing or inaccessible
assignments return 404 before attempts are read. Creation records the creator's
`users.id`, or NULL for a legacy session. Existing rows remain NULL without a
backfill; they are admin-only for management. The `/teacher` dashboard stays
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
  anonymous, students, the legacy tester and a pending second factor are
  redirected to `/login`.
- The list filters `qa.owner_user_id = ?` in its one joined query
  (`assignment_management_scope`); an admin gets every submission. Stored files
  of rows outside the scope are never looked up on disk.
- The download loads the file row joined through to the assignment owner and
  checks `can_manage_assignment` before anything else. A foreign, unowned or
  missing file id answers the same 404.
- Authorization precedes filesystem access: the deleted flag, the stored-path
  confinement (`practical_uploaded_file_safe_path`), the on-disk check and
  `send_file` run only for an authorized principal, and are unchanged.
- Practical data of an unowned (NULL) assignment is admin-only. The legacy
  tester has no practical-review access; the view-only detail compatibility
  above does not extend to it.
- The student practical page and upload stay `attempt_owner` and are not
  owner-scoped. The official resource download
  (`/dzi/practical/resource/<id>/download`) and `/dzi...` stay `admin`.

`signed_in` is used only by `/account/password`.

The tester session satisfies `teacher` exactly as before. Whether a tester
password should keep standing in for a teacher identity is a deferred product
question for the accounts phase, not something the access layer decides.

`ui_profile` is a cosmetic display choice. `current_principal()` reads only
authentication state, so selecting the admin UI profile grants nothing.

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

**Precedence.** While any account key is present, the account is the only
identity: legacy keys in the same cookie are ignored. An identity that does not
resolve (unknown or disabled user, key mismatch, expired or malformed `iat`)
makes the request anonymous and drops the account keys together with the legacy
auth keys, so it can never fall back to a legacy privilege.

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
/logout` (405). Clearing the session also removes any legacy key, and with the
bridge disabled such a key would grant nothing anyway. The legacy
`GET /tester/logout` and `GET /admin/logout` exist only while the bridge is
enabled (404 otherwise).

**Password change (`/account/password`).** Requires the current password, a new
password that meets the account policy (10 characters, 14 for an admin, at most
256, not the email) and its confirmation. A wrong current password counts
against the same throttle as the login form, and a blocked identifier gets 429.
Success sets the hash, rotates `session_key` in one transaction, and re-issues
this browser's session with the new key and the original `iat`; every other
session of the account stops working on its next request.

**Templates.** `tester_authenticated`, `admin_authenticated` and
`can_generate_tests` are computed from `current_principal()`, so an account
identity decides them and conflicting legacy keys cannot. `current_account` is
the account principal or `None`. The footer label is the account's role; for
legacy sessions it is still the cosmetic `ui_profile`.

**Caching.** Every response to a signed-in account carries
`Cache-Control: no-store`. Anonymous, legacy-session, `static` and `/healthz`
responses are not changed.

## Profile Switching

`switch_profile` has the `profile_switch` policy, which depends on its
argument:

- switching to `tester` is allowed;
- switching to `admin` needs an admin Principal: with the bridge disabled an
  anonymous request is redirected to `/login` and a non-admin account gets 403;
  with the bridge enabled it redirects to admin login unless the session is
  already admin-authenticated;
- redirect targets are passed through `safe_redirect_target`, which accepts
  only a same-site absolute path and rejects `//host`, a scheme or host, a
  backslash (`/\host` is read as `//host` by browsers) and control characters.

## Quiz Attempts

Quiz attempt routes have the `attempt_owner` policy: the route-level check lets
every session through, and the view makes the object-level decision with the
owner-session check, answering 404 (not 403) so attempt ids cannot be probed:

- `/quiz/attempt/<attempt_id>`
- `/quiz/attempt/<attempt_id>/result`
- `/quiz/attempt/<attempt_id>/practical/<source_slug>`
- `/quiz/attempt/<attempt_id>/practical/<source_slug>/task/<exam_task_id>/upload`

Each route must call `require_quiz_attempt_access(attempt_id)` before exposing or
mutating attempt data. Attempts created through the normal start flow are marked
with `mark_quiz_attempt_owner(attempt_id)`.

## Refactor Note

Blueprint names change Flask endpoint names, but declared policies live on view
functions, so a rename or a blueprint move cannot change a
route's access decision. A new view must declare `@access_policy(...)` (and
`@integrity_policy(...)` if it accepts unsafe methods); until it does, every
session is denied and the coverage test in `tests/test_access_policy.py` fails.

## Endpoint names after E5

The unprefixed legacy quiz endpoints (`quiz_start`, `quiz_attempt`, `quiz_result`) and the unprefixed auth endpoints (`tester_login`, `admin_login`, ...) no longer exist; use `quiz.*` and `auth.*`. Public URLs are unchanged. `ui_profile` is cosmetic UI state and never grants access; `web/redirects.py` owns `safe_redirect_target`.
