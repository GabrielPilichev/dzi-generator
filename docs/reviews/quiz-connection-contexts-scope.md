# A3 — one connection-lifetime pattern

Gabriel authorized A3 after PR #354 merged. Base:
`68a0470965f04fdf96e8973bebd310142fd46d24`.
Branch: `refactor/quiz-connection-contexts`.

## Goal and exact sites

Use the existing `with quiz_connection() as conn:` helper at the eight raw
production connection consumers below. It closes in `finally` and does not
commit. Keep `quiz_db()` and the helper itself unchanged; its one internal
raw call is intentional, and callers/tests may still use the public API.

| File | Functions at this base |
| --- | --- |
| `web/blueprints/teacher.py` | `teacher_dashboard`, `teacher_assignments`, `teacher_new`, `teacher_assignment_qr`, `teacher_assignment_results`, `teacher_assignment_results_export` |
| `web/services/quiz.py` | `quiz_write_assignment_note`, `quiz_write_attempt_note` |

The intended improvement is reliable connection closure on exceptions and
early exits. Preserve existing success, validation, authorization and error
responses. This does not split `teacher_new` or begin A4/A5 cleanup.

## Preservation and allowed edits

After this scope is committed and a dedicated draft PR exists, a fresh
Sonnet / High session may edit only those two production files within the
listed functions, their necessary connection imports/cleanup scaffolding,
and a new focused `tests/test_quiz_connection_lifetimes.py` module. Allow
narrow connection-factory patch adaptations in these existing tests:
`tests/test_assignment_owner_routes.py`, `tests/test_assignment_ownership.py`
and `tests/test_lifecycle_teacher_routes.py`. Retarget the affected teacher
patches to `web.services.quiz.quiz_db` (using existing module aliases), with
necessary imports or trace-capture assertions only. Preserve their fixtures
and existing assertions, including query counts, owner-index use and the
single snapshot. Confirm tracing observes actual queries, not an empty trace.
Leave all other existing tests, fixtures/support, functions, templates,
canonical data and schema unchanged. Remove the teacher module's
unused `quiz_db` import when its consumers are converted; preserve the API
and composition-root aliases used elsewhere.

- Keep SQL literals, bound parameters, query order and query counts unchanged.
  Reindent Python without changing the contents of multiline SQL strings.
- Preserve every explicit `BEGIN`, commit and rollback, including the single
  read snapshot across authorization and result/CSV reads. Do not substitute
  SQLite's committing connection context for the existing close-only helper.
  Keep results' lifecycle observation after its rollback and before close.
- End each managed scope at the former normal close boundary. Materialize
  rows before closing; keep rendering/response construction outside when it
  was outside before. Early returns/aborts must also close reliably.
- In both `teacher_new` success branches, record the created assignment id
  inside the managed scope. Run failure-only preselection and remaining form
  loading only when no assignment was created. Write the assignment note and
  redirect after leaving the scope, preserving close-before-note ordering.
  This bounded control-flow adjustment introduces no helper extraction and
  does not change parsing, validation, queries or their order.
- Preserve ownership, role checks, lifecycle holds/clock behavior, status
  codes, redirects, request integrity, public-token rules, QR/CSV output and
  the server-side rejection of forged `question_ids`/`question_plan` fields.
- Preserve note-writer best-effort exception handling and stderr messages,
  paths, frontmatter and Markdown bytes. Python reindentation must not add
  whitespace to the generated note strings or change filesystem-write order.
  Enter each note writer's managed scope inside its existing outer `try`.

No direct manual close remains in the eight converted function bodies.
Keep shared `_fetch_managed_assignment` unchanged: its existing close before
404 remains valid, and SQLite tolerates the manager's later repeated close.
Production `conn = quiz_db()` remains only
inside `quiz_connection()` itself. Other connections and tests are outside
this static acceptance check; do not rewrite the project broadly.

## Verification

Use existing test support and disposable state only. Add meaningful consumer
regressions for closure on success, early return/abort, query failure and
note-write failure, retaining existing no-implicit-commit checks. Verify
result/CSV snapshot/rollback behavior and compare note output bytes against
the base on the same fixed fixtures. Static/AST inspection should confirm the
eight conversions and unchanged SQL/string constants, rather than a new
source-only test that merely mirrors the implementation.

Run focused lifetime, quiz-service, teacher, ownership, QR, result/export and
lifecycle regressions first, plus required auth/quiz smoke checks. This
production refactor crosses six teacher handlers, transaction snapshots and
both note writers, so one final full unittest run is justified for A3's
acceptance. Do not repeat the full suite without a concrete reason. If a
failure appears, reproduce only its affected module on the exact base to
separate pre-existing failures; do not fix unrelated work in this PR.

Compile to scratch, check whitespace and the intended diff, and use explicit
temporary DB paths for FK/integrity/DZI checks. Disclose the existing absent
official-PDF audit exit 1 accurately. Do not download the PDF, perform broad
route crawls, install dependencies unnecessarily or mask failing checks.

Leave edits unstaged/uncommitted. Report actual PR number, branch/HEAD, all
eight sites, preservation evidence, tests and limits. Gabriel commits/pushes;
a separate fresh Opus / High review targets the exact pushed implementation
SHA. After approval and verification, print the complete guarded merge
commands directly in chat; Gabriel decides whether to execute them.

No runtime `data/questions.db` or sidecars, repository generated vault files,
real credentials/providers, gates, VPS, deployment, server or tunnel actions
are authorized. Note tests must use scratch vault paths.
