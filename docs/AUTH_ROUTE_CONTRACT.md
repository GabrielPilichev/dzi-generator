# Auth Route Contract

Four separate concerns, deliberately kept apart:

- **Authentication** — who the session is. Two mechanisms coexist during the
  accounts bridge:
  - an **account** identity (`uid`, `sk`, `iat`), issued by `web/blueprints/account.py`
    (endpoints `account.login`, `account.logout`, `account.password`) and resolved
    to a principal on every request by `load_current_user()` in `web/app.py`;
  - the **legacy shared passwords** (`tester_authenticated`,
    `admin_authenticated`), owned by the login/logout routes in `web/blueprints/auth.py` (endpoints `auth.tester_login`, `auth.admin_login`, `auth.tester_logout`, `auth.admin_logout`, `auth.switch_profile`, `auth.tester_home`).
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
   `current_principal()`. A denied request is redirected to the matching login
   with `next=<path>`: `/tester/login` for `teacher` routes, `/login` for
   `signed_in` routes, `/admin/login` for everything else, including undeclared
   views. A request that matched no route is left to Flask's own 404/405.

Passing one check never implies the other: an admin's cross-origin post is
refused, and a same-origin post from a session without access is still sent to
login.

## Declared Access Policies

`@access_policy(...)` sits below `@app.route(...)` and attaches the policy to
the view function, so a blueprint-prefixed endpoint name
resolves to the same decision. An endpoint without a declared policy is denied;
a coverage test walks `app.url_map` and fails on any undeclared endpoint.

| Policy | Meaning | Routes |
| --- | --- | --- |
| `public` | any session, signed in or not | `/`, `/grade/<n>`, `/section/<slug>`, `/subjects/...`, `/physics...`, `/tester`, `/healthz`, `/favicon.ico`, logins (`/login`, `/tester/login`, `/admin/login`), `POST /logout`, `/tester/logout`, `/quiz/<assignment_id>` |
| `teacher` | teacher or admin account, or a tester or admin session | `/teacher/new`, `/teacher/assignment/<id>` |
| `admin` | admin account or admin session only | `/admin/...`, `/dzi...`, every other `/teacher...` route, `/admin/logout` |
| `signed_in` | any active account (student, teacher or admin); a legacy tester/admin session is not an account | `/account/password` |
| `attempt_owner` | route is reachable; the view checks attempt ownership and answers 404 | `/quiz/attempt/<id>`, `/quiz/attempt/<id>/result`, `/quiz/attempt/<id>/practical/...` |
| `profile_switch` | `/profile/<profile>`: `admin` needs an admin session, other targets are public | `/profile/<profile>` |

Flask's built-in `static` view cannot be decorated; `web.access` treats it as
`public` by name.

`assignment_owner` from the roadmap's vocabulary is not used yet: there is no
assignment ownership. `signed_in` is used only by `/account/password`.

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

**Session contents.** A successful `POST /login` clears the session, carries
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

**Redirect after login.** `next` is accepted only if `safe_redirect_target`
approves it, when the form is rendered and again when it is used. Otherwise the
role decides: admin → `/teacher`, teacher → `/teacher/new`, student → `/`.

**Logout (`POST /logout`).** Clears the whole session of this browser and
redirects to `/`. It does not rotate `session_key`, so other devices stay signed
in; "sign out everywhere" is `manage_accounts sign-out`. There is no `GET
/logout` (405). The legacy `GET /tester/logout` and `GET /admin/logout` are
unchanged.

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
- switching to `admin` redirects to admin login unless the session is already
  admin-authenticated;
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
