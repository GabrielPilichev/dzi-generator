# Auth Route Contract

LearnPilot route access is currently enforced mostly by Flask endpoint names in
`protect_admin_routes()` in `web/app.py`. Future blueprint extraction must
preserve these endpoint access rules, or update the guard, tests, and this
document in the same PR.

## Endpoint Prefix Rules

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

Blueprint names can change Flask endpoint names. If route extraction changes
endpoint names, keep tests covering default-deny DZI behavior, tester teacher
allowlist behavior, admin-only teacher/admin pages, and quiz attempt ownership.
