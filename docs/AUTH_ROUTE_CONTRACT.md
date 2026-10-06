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

Roadmap PR D1 added the explicit declarations; `protect_admin_routes()` still
enforces access, and `tests/test_access_policy.py` asserts the two agree for
every endpoint as anonymous, tester and admin. PR D2 removes the prefix guard.

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

`signed_in` and `assignment_owner` from the roadmap's vocabulary are not used
yet: there are no accounts and no assignment ownership. They arrive with the
accounts phase, not in D1.

`ui_profile` is a cosmetic display choice. `current_principal()` reads only
authentication state, so selecting the admin UI profile grants nothing.

Every route that accepts an unsafe method declares `@integrity_policy(SAME_ORIGIN)`.
`oauth_state` and `provider_signature` are reserved for future external flows
and are not in use.

## Endpoint Prefix Rules (still enforced in D1, removed in D2)

- `admin*` endpoints are admin-only, except `admin_login`.
- `dzi*` endpoints are admin-only by default. Intentionally public DZI endpoints
  must be added to `PUBLIC_DZI_ENDPOINTS`.
- `teacher*` endpoints are admin-only by default.
- `TESTER_TEACHER_ENDPOINTS` is the small tester allowlist for normal assignment
  creation/detail flows. Other teacher endpoints, including assignment lists,
  results, exports, edits, duplicates, and practical submission review, remain
  admin-only.

## Profile Switching

`switch_profile` is allowed through the prefix guard, but it has route-specific
protection:

- switching to `tester` is allowed;
- switching to `admin` redirects to admin login unless the session is already
  admin-authenticated;
- redirect targets are passed through `safe_redirect_target`.

## Quiz Attempts

Quiz attempt routes use a separate owner-session check instead of endpoint
prefix auth:

- `/quiz/attempt/<attempt_id>`
- `/quiz/attempt/<attempt_id>/result`
- `/quiz/attempt/<attempt_id>/practical/<source_slug>`
- `/quiz/attempt/<attempt_id>/practical/<source_slug>/task/<exam_task_id>/upload`

Each route must call `require_quiz_attempt_access(attempt_id)` before exposing or
mutating attempt data. Attempts created through the normal start flow are marked
with `mark_quiz_attempt_owner(attempt_id)`.

## Refactor Note

Blueprint names change Flask endpoint names, but declared policies live on view
functions, so a rename cannot change a route's access decision. While the prefix
guard is still active, keep endpoint names or update the guard, the policy
declarations, the tests and this document together.
