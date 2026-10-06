# Auth Route Contract

Four separate concerns, deliberately kept apart:

- **Authentication** — who the session is (`tester_authenticated`,
  `admin_authenticated`), owned by the login/logout routes in `web/app.py`.
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

Every request passes through two independent `before_request` checks, in this
order:

1. `protect_same_origin_unsafe_methods()` — request integrity. Every
   POST/PUT/PATCH/DELETE must carry a same-origin `Origin` (or `Referer`), or
   it gets 403 (only the `TESTING` configuration accepts a request carrying
   neither header). No route is exempt, and being signed in does not help.
2. `enforce_access_policy()` — authorization. It looks up the policy declared
   on the view function Flask resolved for the request and decides with
   `current_principal()`. A denied request is redirected to the matching login
   with `next=<path>`: `/tester/login` for `teacher` routes, `/admin/login` for
   everything else, including undeclared views. A request that matched no
   route is left to Flask's own 404/405.

Passing one check never implies the other: an admin's cross-origin post is
refused, and a same-origin post from a session without access is still sent to
login.

## Declared Access Policies

`@access_policy(...)` sits below `@app.route(...)` and attaches the policy to
the view function, so a legacy alias or a blueprint-prefixed endpoint name
resolves to the same decision. An endpoint without a declared policy is denied;
a coverage test walks `app.url_map` and fails on any undeclared endpoint.

| Policy | Meaning | Routes |
| --- | --- | --- |
| `public` | any session, signed in or not | `/`, `/grade/<n>`, `/section/<slug>`, `/subjects/...`, `/physics...`, `/tester`, `/healthz`, `/favicon.ico`, logins, `/tester/logout`, `/quiz/<assignment_id>` |
| `teacher` | tester or admin session | `/teacher/new`, `/teacher/assignment/<id>` |
| `admin` | admin session only | `/admin/...`, `/dzi...`, every other `/teacher...` route, `/admin/logout` |
| `attempt_owner` | route is reachable; the view checks attempt ownership and answers 404 | `/quiz/attempt/<id>`, `/quiz/attempt/<id>/result`, `/quiz/attempt/<id>/practical/...` |
| `profile_switch` | `/profile/<profile>`: `admin` needs an admin session, other targets are public | `/profile/<profile>` |

Flask's built-in `static` view cannot be decorated; `web.access` treats it as
`public` by name.

`signed_in` and `assignment_owner` from the roadmap's vocabulary are not used
yet: there are no accounts and no assignment ownership. They arrive with the
accounts phase.

The tester session satisfies `teacher` exactly as before. Whether a tester
password should keep standing in for a teacher identity is a deferred product
question for the accounts phase, not something the access layer decides.

`ui_profile` is a cosmetic display choice. `current_principal()` reads only
authentication state, so selecting the admin UI profile grants nothing.

Every route that accepts an unsafe method declares `@integrity_policy(SAME_ORIGIN)`.
`oauth_state` and `provider_signature` are reserved for future external flows
and are not in use.

## Profile Switching

`switch_profile` has the `profile_switch` policy, which depends on its
argument:

- switching to `tester` is allowed;
- switching to `admin` redirects to admin login unless the session is already
  admin-authenticated;
- redirect targets are passed through `safe_redirect_target`.

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
functions, so a rename, a blueprint move or a legacy alias cannot change a
route's access decision. A new view must declare `@access_policy(...)` (and
`@integrity_policy(...)` if it accepts unsafe methods); until it does, every
session is denied and the coverage test in `tests/test_access_policy.py` fails.
