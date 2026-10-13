# LearnPilot — Refactor & Accounts Roadmap (Codex brief)

Oct 6, 2026 · @Gabriel Pilichev

> **Status note (Accounts PR 10).** This brief is kept as the original plan.
> Where it says "today" about authentication (two shared passwords, the tester
> and admin sessions, `tester_authenticated` / `admin_authenticated`, `/tester`,
> the tester/admin logins, profile switching) it describes the application
> before accounts. All of that has been removed: sign-in is by account only.
> The current contract is `docs/AUTH_ROUTE_CONTRACT.md`; the implementation
> history is `docs/ACCOUNTS_DESIGN.md`.

## Current roadmap update — 2026-10-13

This update records Gabriel's current product decisions. Historical sections below
remain useful context; where they conflict with these decisions, this update wins.
The existing account/storage contracts remain authoritative until the new
registration design is approved.

- Google is the only external sign-in provider planned. Microsoft sign-in is
  excluded; Apple remains out of scope.
- PR #331's Google sign-in and explicit linking implementation is reported complete
  in its working tree, with 3,324 tests passing. Independent implementation review
  is underway. It is not yet committed or merged, and production activation stays off.
- After #331, design and implement public email/password registration for students
  and teachers. This is not passwordless email-code sign-in.
- Send a verification code to the submitted email address to establish ownership.
  After verification, users sign in with email and password. Email verification
  does not establish that someone is a teacher.
- Existing teacher/admin password accounts remain supported. Public registration
  must never create an admin account.
- Google and email/password credentials must not be silently linked merely
  because their email addresses match.

### Required registration forms

| Field | Student | Teacher |
| --- | --- | --- |
| First name | required | required |
| Last name | required | required |
| Email | required | required |
| Password | required | required |
| Class | required | — |
| School name | — | required |
| Subject taught | — | required |

The registration design must settle the representation of class and subject,
validation, unverified-account lifecycle and permissions, duplicate-email handling,
verification-code expiry/retries/resends, delivery, recovery and account linking.
It must also address student-data minimisation and retention. These implementation
details are not approved by the field list alone.

### Immediate sequence

1. Finish independent review and required corrections for #331; commit and merge
   with Google production activation still off.
2. Create a separate design PR for email/password registration and email-code
   verification, including both required forms and compatibility with Google accounts.
3. Implement the approved registration design in scoped PRs with independent
   security review.
4. Complete the DZI content gate and student practice experience.
5. Complete the entitlement seam; billing remains deferred until the free-period
   end and payment provider are decided.

Deployment and Google activation remain separate, explicitly authorised work.
The CSS split can still proceed independently.

## How to use this brief

This is the agreed plan for LearnPilot's structural refactor and the path to user accounts; implement it one PR at a time, in the order of the PR sequence below. Each task from Gabriel names one PR; this brief is the shared context. Line numbers refer to the LearnPilot Technical Study of `main` at commit `39abbbd`, so verify them against the live code.

Rules for every PR:

- Work on a feature branch. Never commit to `main` and never merge without Gabriel's go-ahead.
- Small, behavior-preserving patches. Every URL stays identical unless the PR says otherwise.
- No schema change without a numbered migration in `web/migrations/`, applied only through `python3 -m src.manage_migrations`. App code never mutates the schema.
- Tests never write to `data/questions.db`; use the `tests/support.py` isolation. `data/questions.db` is ignored local runtime state (H).
- Keep Flask, SQLite, Jinja and plain CSS/JS. No ORM, DI framework, repository classes, npm, JS framework, Tailwind or Bootstrap.
- UI text stays Bulgarian and follows the `AGENTS.md` UI rules.
- Never commit secrets, logs, `.bak` files, tunnel URLs or DB dumps.
- During implementation, run targeted tests appropriate to the changed behavior. Before the final push for the PR, run the `AGENTS.md` pre-commit checks and the full suite once: `python3 -B -m unittest discover -s tests`. Run the full suite earlier only when a broad or high-risk change makes targeted tests insufficient.
- If a change would alter access behavior that the PR does not describe, stop and report instead of guessing.

End every PR with a short report: branch, files changed, what changed, tests added, full-suite result, and anything deferred or surprising.

## Product direction

LearnPilot moves from two shared passwords to individual accounts with subscriptions, and the refactor PRs exist to make that move safe. Subscriptions are paid by individual users (card, Apple Pay, Google Pay), not by schools, and are free at launch for an undecided period. Students and teachers may get different sign-in options.

| Capability | Visitor | Student | Teacher | Admin (Gabriel only) |
| --- | --- | --- | --- | --- |
| Browse the question bank (non-DZI) | yes | yes | yes | yes |
| See official DZI questions | no | yes | yes | yes |
| Take a test (account required) | no | yes | yes | yes |
| Create tests | no | no | yes | yes |
| Results and CSV export | no | own attempts | own tests | all |
| Review practical submissions | no | no | own tests | all |
| DZI admin tooling (`/dzi`, pool health, imports) | no | no | no | yes |

Today's roles map onto this model: the tester session becomes the teacher role, the admin session becomes admin, and the session-based attempt owner becomes the account that started the attempt. Assignment ownership is new.

## Architecture target

The goal is clear boundaries between HTTP, application logic and SQLite mechanics, without changing the stack. Today `web/app.py` (5,315 lines) couples routing, domain logic, authorization, migration behavior and database access.

```text
web/
├── app.py           # composition root only; shrinks with each PR
├── production.py, wsgi.py
├── db.py            # connect_db(path, ...) and lifecycle helpers
├── access.py        # current_principal() and explicit access policies
├── blueprints/      # HTTP layer: auth, browsing, quiz, teacher, dzi, practical
├── services/        # app logic: quiz, grading, assignments, dzi, practical, content gate
├── templates/
└── static/
src/                 # offline import and content tooling (role unchanged)
```

Dependencies point one way: blueprints and `app.py` call services, services call `db.py`. Nothing imports `web.app` except the entry points.

Keep as they are:

- Flask with server-rendered Jinja and plain JS as enhancement only.
- SQLite in WAL mode, raw SQL with bound `?` parameters, no ORM.
- One Gunicorn worker until load is measured.
- One deployment: no microservices, no PostgreSQL migration.

## PR sequence

The first four PRs create the seams that make splitting `app.py` safe, so the split itself comes after them. Each PR is green on the full suite before the next starts. Content imports (Physics, Biology) continue in parallel and must not be blocked.

| PR | Change | Starts after | Independent review |
| --- | --- | --- | --- |
| 0 | `requirements-dev.txt`, fully green suite, local cookie note | — | no |
| A | No schema mutation from app code | 0 | no |
| B | `web/db.py` connection factory and seed-DB tripwire | A | no |
| C | Quiz services; blueprints stop importing `web.app` | B | yes |
| C2 | `is_dzi_question` predicate with pinned count | C | no |
| D1 | Explicit access + request-integrity policies beside the old guard, agreement tests | C | yes (security) |
| D2 | Remove the prefix-based guard and old integrity exceptions | D1 | yes (security) |
| F | `/teacher/new` query batching | C | no |
| E1–E5 | Blueprints: DZI, practical, teacher, browsing, then auth/composition cleanup | D2, F | E2 yes |
| G0 | Schema-only baseline (optional) | A | no |
| G1 | Rebuild/parity audit with table classification and stable-id checks | G0 | yes |
| G2 | Build dev/test DBs from canonical inputs | G1, E5 | yes |
| H | Stop tracking the mutable seed DB; content/user-data separation | G2 | yes |
| Accounts design | Design-only account/auth/ownership migration plan | D2, G1 | yes |
| Accounts implementation | Accounts, sign-in and ownership | Accounts design, H, E5 | yes |
| Email/password registration design | Student/teacher forms, email-code verification and account lifecycle | Google sign-in #331 | yes (security) |
| Email/password registration implementation | Approved registration, verification and email/password login | Registration design | yes (security) |
| DZI gate | Signed-in DZI visibility and leak/cache tests | Accounts and registration implementation | yes |
| Entitlements | `has_feature` seam, everyone on early-access plan | Accounts implementation | no |
| Billing | Payment provider and webhooks | free-period end decided | yes |
| I | Split `ui-pass9.css` by surface | any time | no |

F runs before the E series because it changes `teacher_new`, which E3 moves.

Suggested executors are intentionally cost-aware rather than fixed globally. PR 0 and PR A fit Claude Sonnet 5.5 at Medium effort. PR B fits Sonnet 5.5 at High. PR C fits Sonnet 5.5 at High with an independent Claude Opus 5.5 High review. D1/D2 should be designed and independently reviewed by Opus 5.5 High and implemented by Sonnet 5.5 High. Reassess the executor for each later PR from its actual risk and ambiguity. Stop for a fresh design/review at the Accounts design PR before implementing account code.

## Foundation PRs

These PRs change no URLs, no access rules and no schema; they remove hidden side effects and give later PRs a clean base.

### PR 0 — Dev dependencies and a green baseline

The full suite currently has one error because `python-docx` is needed by `src/build_exam.py` and `tests/test_open_question_scope.py` but is not declared.

1. Add `requirements-dev.txt` that includes `-r requirements.txt` plus `python-docx`. List optional script dependencies (`watchdog`, Ollama clients) in a commented section.
2. Leave production `requirements.txt` unchanged.
3. Document `DZI_SESSION_COOKIE_SECURE=0` for local plain-HTTP runs in the README.

Acceptance: after `pip install -r requirements-dev.txt`, the full suite has zero errors and zero failures.

### PR A — No schema mutation from app code

Importing `web.app` runs `web/migrations/001_quiz_tables.sql` through a path relative to the current directory (study: `web/app.py:2313` and `:3902`). `DZI_SKIP_STARTUP_MIGRATION` also disables request-time schema guards, so mutation happens during requests too.

1. Inventory every schema-touching path under `web/`: `executescript`, `CREATE`/`ALTER`/`DROP`, reads of `.sql` files, request-time ensure helpers. List them in the PR description.
2. Remove all of them. Schema changes happen only through `python3 -m src.manage_migrations --db ... --apply`.
3. For local runs, add a read-only pending-migrations check that logs a clear warning naming the pending files and does not fail. Reuse the check in `web/production.py`; production keeps failing hard.
4. Handle `DZI_SKIP_STARTUP_MIGRATION` explicitly: remove it everywhere (production.py, deploy env templates, tests, docs) or keep it as a documented no-op for one release. State which.

Out of scope: fixing the old relative path, any schema change, any route change.

Acceptance: importing `web.app` and serving requests against a temp DB copy leaves its sha256 and `PRAGMA schema_version` unchanged; no DDL is reachable from request code; production startup tests pass.

### PR B — One SQLite connection factory

`get_db()` opens one connection per request (WAL, `sqlite3.Row`, closed at teardown). `quiz_db()` opens one per call (`foreign_keys=ON`, closed by hand). `CachedSubjectBrowser` opens query-only connections.

1. Add `web/db.py` with `connect_db(path, *, query_only=False)`. It owns `sqlite3.Row`, `foreign_keys=ON`, `busy_timeout` and query-only mode.
2. WAL is a database-level initialization choice, not a per-connection action. Configure `journal_mode=WAL` once when a writable database is created/initialized; ordinary connections, especially query-only/read-only ones, must not attempt to change journal mode.
3. Route `get_db()`, `quiz_db()` and the browse cache through the factory while keeping their current lifetimes. Do not merge transaction semantics.
4. Optionally give `quiz_db()` callers a context manager so connections close on exceptions.
5. `connect_db` takes the path as an argument. Callers keep reading the module globals that `tests/support.py` patches (`DZI_DB`, `QUIZ_DB_PATH`). Resolving paths inside `db.py` would silently bypass test isolation and write to the real seed DB.
6. Add a suite-level tripwire: sha256 of `data/questions.db` (and its WAL, if present) before and after the run; a mismatch fails the suite. Use `load_tests` in `tests/__init__.py` or an `atexit` hook in `tests/support.py`.

Acceptance: all accessors share one connection configuration without per-connection journal-mode changes; the tripwire is in place and passing; `PRAGMA foreign_key_check` is clean.

### PR C — Quiz services; break the circular import

`web/blueprints/quiz.py:11` imports `web.app` lazily through `_app_module()`, so the dependency points both ways. Moving more routes while this pattern exists would only create more modules that import `web.app`.

1. Create `web/services/quiz.py` (and `grading.py` if the split is natural) with attempt loading and ownership, question selection (`quiz_pick_questions`), option randomization, eligibility (`is_quiz_question_eligible`), answer normalization and grading.
2. `web/blueprints/quiz.py` imports services only; `web/app.py` uses the same services.
3. Preserve behavior exactly: seeds, option order, eligibility, legacy endpoint aliases, vault note writing.

Acceptance: nothing under `web/blueprints/` references `web.app` or `_app_module`; the quiz suites (`quiz_attempt_render`, `quiz_question_randomization`, `quiz_attempt_race_ownership`) pass unchanged; the tripwire stays green.

### PR C2 — DZI-question predicate

Add one helper in services, `is_dzi_question` plus a matching SQL fragment, identifying official DZI questions through `exam_task_questions` or `source_exam`/`source_number`. Include the legacy parser questions from `aug_2023`, `may_2023` and `jun_2022` unless Gabriel decides otherwise. Add a test pinning the expected count on the seed DB, so a later import cannot fall outside the predicate. Nothing uses the predicate yet.

## Access policy and performance

### PR D1/D2 — Explicit, default-deny access and request-integrity policies

Access is decided today by endpoint-name prefixes in `protect_admin_routes` (`admin*`, `dzi*`, `teacher*`, plus `TESTER_TEACHER_ENDPOINTS`), so moving a route into a blueprint changes who can open it. A `teacher` blueprint would turn `teacher_new` into `teacher.teacher_new` and lock testers out. A `practical` blueprint would make `practical.teacher_practical_submissions` public. This must be fixed before any blueprint move.

Keep two separate declarations on routes:

- **Access policy** answers who may use the route.
- **Request-integrity policy** answers how an unsafe/external request proves that it is legitimate. Authorization must never double as a CSRF exemption.

Design:

1. `web/access.py` defines `current_principal()`. Today it derives the principal from `admin_authenticated` and `tester_authenticated`; after the accounts phase it reads the user row. Access policies never read session keys directly.
2. Access policies attach to view functions (a decorator sets an attribute), not to endpoint strings, so legacy aliases and blueprint names share one policy.
3. Access vocabulary: `public`, `signed_in` (student or teacher), `teacher`, `admin`, `assignment_owner`, `attempt_owner`. Today the tester session satisfies `teacher`.
4. A separate request-integrity declaration covers ordinary same-origin protection and later external flows. The vocabulary should distinguish at least `same_origin`, `oauth_state` and `provider_signature`. `oauth_state` and `provider_signature` are not access levels.
5. Preserve the special cases: `switch_profile` depends on its argument (switching to admin needs an admin session), and owner checks keep returning 404, not 403, so ids cannot be probed.
6. Default deny for access: an endpoint without a declared access policy is refused. A test walks `app.url_map` and fails on any endpoint other than `static` that has no access policy.
7. Unsafe/external endpoints must have an explicit request-integrity policy. Ordinary unsafe routes keep the same-origin check. Future OAuth callbacks and payment webhooks can use their dedicated integrity mechanisms without weakening unrelated routes.

Rollout:

- **D1:** declare access and request-integrity policies while `protect_admin_routes` and the existing same-origin machinery remain in place. A test asserts that the new access policy and the old guard agree for every endpoint as anonymous, tester and admin; integrity tests prove ordinary unsafe routes still require same-origin requests.
- **D2:** remove the prefix-based access logic and the old special-case integrity exemptions only after coverage/agreement is proven.

Update `docs/AUTH_ROUTE_CONTRACT.md` in the same PRs. Acceptance: coverage and agreement tests pass, `test_auth_guard` passes, request-integrity tests pass, and no existing route changes access.

### PR F — `/teacher/new` query batching

`/teacher/new` runs about 53,000 SQL statements, and with one sync worker it blocks every other user while it runs. After PR C, eligibility lives in services.

1. Benchmark first: count statements (for example with `set_trace_callback`) for `/teacher/new` on the seed DB and record the baseline in the PR.
2. Replace per-section and per-question queries with a few set-based queries. Evaluate the image-reference phrase rule from `src/dzi_open_questions.py` in Python over batch-fetched rows; do not push phrase matching into SQL.
3. Keep `is_quiz_question_eligible` as the reference implementation, since attempt rendering still uses it. Add an equivalence test: for every section in the seed DB, the old per-question check and the new batched result give identical id sets and counts.
4. Add a regression test that caps the statement count (target: tens, not thousands).

Out of scope: cold browsing (about 11,000–13,000 statements) and the 3.18 MB DZI page, which are separate later work.

## Blueprint extraction (PR E1–E5)

Routes move out of `app.py` in five small PRs after D2 and F, and every URL stays identical. Do not combine them into one split-everything PR.

Endpoint names: moving a route into a blueprint changes its endpoint name (`teacher_new` becomes `teacher.teacher_new`), which breaks every `url_for`, redirect and test that uses the old name. Default strategy: rename all references to the blueprint name in the same PR. Endpoint names are internal, so nothing outside the repo depends on them, and policies attached to view functions are unaffected. Do not add new legacy aliases. E5 removes the existing quiz aliases from `register_legacy_quiz_endpoint_aliases` after renaming their references. If Gabriel prefers aliases instead, apply that choice consistently from E1 on.

In E1, add a snapshot test of `app.url_map`: every rule string, its methods and its access policy. Each later E PR may change endpoint names but not rules, methods or policies.

| PR | Routes and helpers | Policies |
| --- | --- | --- |
| E1 — DZI | `/dzi`, `/dzi/source/<source_slug>`, `/dzi/source/<source_slug>/practical`, `/admin/open-question-candidates`; DZI query helpers into `services/dzi.py` | admin |
| E2 — practical | resource downloads; student practical page and uploads; submission review and file download | admin for resources and review; `attempt_owner` for the student page and uploads |
| E3 — teacher | `/teacher`, `/teacher/new`, assignment detail, edit, duplicate, results, `results.csv`, `/teacher/dzi-training` | unchanged from D |
| E4 — browsing | `/`, `/grade/<n>`, `/subjects/...`, `/section/<slug>`, physics redirects, `/healthz`, `/favicon.ico` | unchanged from D |
| E5 — auth/composition cleanup | `/tester`, admin/tester login and logout, profile switching; composition-root cleanup; rename remaining internal endpoint references and remove the legacy quiz aliases | unchanged from D |

E2 gets independent review because it owns the upload boundary. Auth/composition cleanup is isolated in E5 so authentication changes never hide inside a broad browsing move. Upload limits in `src/practical_uploads.py` stay unchanged. Acceptance for every E PR: the `url_map` snapshot matches, the access-policy coverage test passes, request-integrity coverage stays green, the full suite is green and `app.py` is smaller.

## Schema baseline and data separation (PR G0, G1, G2, H)

With real accounts, content updates must never touch user data, and a fresh database must be buildable without the tracked seed file. Today the core tables (`questions`, `curriculum_*`, `exams`) predate the migration system, and their DDL lives only in `src/legacy_schemas/` and the seed DB.

### PR G0 — Schema-only baseline (optional, any time after A)

1. Add `web/migrations/000_baseline.sql` with the core-table DDL, built from `src/legacy_schemas/` and the seed DB's schema. Schema only, no data.
2. Existing databases record 000 through `manage_migrations --baseline-apply` without re-running it.
3. Test: an empty DB plus 000 and 001–010 reproduces the canonical schema (`sqlite_master`, columns, indexes and constraints). This catches schema drift while migrations are still being added.

G0 does not change the content import workflow, so it can land while content work continues.

### PR G1 — Rebuild/parity audit and table classification

Do not assume that every historical content row can already be recreated from current import batches. Older IT/DZI content may exist only inside the tracked database.

1. Classify every table as one of:
   - **rebuildable application/content state** — curriculum, questions, DZI structures/assets metadata and other canonical content;
   - **runtime/user state** — assignments, attempts, answers, future accounts/subscriptions and other data that must never be replaced by a content rebuild;
   - **derived/operational state** — caches/sync/audit data that can be regenerated or has its own lifecycle.
2. Build a scratch database from G0 + numbered migrations + the currently canonical tracked structures, reviewed import batches and seed inputs.
3. Compare it logically with the current canonical seed DB: schema, row identities, key relationships, canonical content and provenance. Byte-identical SQLite files are not required.
4. Preserve primary keys for canonical content. `quiz_attempts.question_ids_json` stores question ids without foreign keys, so renumbering question ids can silently corrupt historical attempts.
5. If G1 finds historical canonical content that cannot be rebuilt from today's inputs, do **not** rerun old parsers. Create a one-time reviewed canonical export/seed artifact with explicit stable ids and provenance, then include it in the rebuild.
6. Produce a parity report that names every intentional difference. H is blocked until rebuildable content is accounted for.

Acceptance: the scratch rebuild reproduces the classified rebuildable content with stable ids, or every remaining gap has an explicit reviewed canonical export path.

### PR G2 — Built development and test databases

1. Add a deterministic builder for development/test databases from the baseline, migrations and canonical tracked content inputs established by G1.
2. Switch `tests/support.py` from copying the tracked seed DB to a built fixture DB. Test-only users and runtime rows are fixtures, not production seed data.
3. Add stable-id regression checks for canonical question/content rows whose ids are externally embedded in historical runtime data.
4. Keep the importers idempotent and preserve the current local content-authoring workflow.

Acceptance: a clean checkout can build the test/dev database without `data/questions.db`; the full suite and parity checks are green.

### PR H — Untrack the mutable seed DB and separate deployment lifecycles

Status: the tracked binary was removed from Git (`data/questions.db` is ignored runtime state; fresh databases come from `python3 -m src.build_database`). Item 3 (production content-update workflow) is deferred to the consolidated deployment.

1. Stop tracking `data/questions.db` only after G2 passes. It remains ignored.
2. Content reaches production only through reviewed canonical seeds/importers applied to the production DB, never by shipping a database file.
3. Document the exact production content-update workflow in `docs/DEPLOYMENT.md`.
4. Content importers must never mutate account/subscription/runtime ownership tables.
5. Production backups continue to cover the full live database and user-generated files independently of content deployment.

Coordinate H with Gabriel because it changes how local and production content updates are performed.

## Accounts and sign-in

The first Accounts PR is **design-only**. Stop and redesign here before implementation. The sign-in method never decides permissions, and the data model must support more than one identity per account without email-based auto-linking.

**Status: the design is approved.** `docs/ACCOUNTS_DESIGN.md` is the authoritative Accounts implementation specification (schema, session, compatibility bridge, ownership, PR sequence). The rest of this section is the original conceptual brief; where it differs from `docs/ACCOUNTS_DESIGN.md`, that document wins. Approved deviations:

- The local password credential is stored on `users`, not in a separate `password_credentials` table.
- Google OAuth transactions use the session-bound design (state, nonce, PKCE verifier, validated return target and expiry in the signed session), not a server-side transaction plus browser-binding cookie.
- Apple sign-in remains out of scope.
- Admin TOTP is a separate security PR before the production cutover and legacy-auth removal; it does not block the initial Accounts PRs.
- Student authentication is phase 2: Google sign-in plus public email/password registration with email-code verification, as recorded in the 2026-10-13 update. Registration requires its own approved design before implementation. Student-data retention/erasure remains a later product/legal decision.

### Accounts design PR

The design PR settles schema, credential storage, OAuth transaction handling, ownership migration and account lifecycle before any account tables land.

Conceptual model:

```text
users                 id, role ('student'|'teacher'|'admin'), declared_type,
                      display_name, created_at, plan, plan_started_at

user_identities       id, user_id, issuer, subject, provider_label, created_at
                      UNIQUE(issuer, subject)

password_credentials  user_id, normalized_login, password_hash,
                      email_verified_at, created_at, updated_at

totp_credentials      user_id, protected_secret, enabled_at
recovery_codes        user_id, code_hash, used_at
```

The exact credential/recovery columns and secret-protection mechanism are decided in the design PR; do not improvise them during implementation.

`declared_type` is what the user picked at signup; `role` carries permissions. During early access, student and teacher roles may be self-declared as an explicit product decision. Self-declared teachers gain test-creation capability but no admin rights; apply conservative creation/rate/storage quotas and add verification before any teacher-only protected content or teacher-specific pricing.

### Identity rules

- External identities are keyed by the provider's stable **issuer + subject** pair, not email. Google is the only planned external provider; Microsoft is excluded. Never key authorization on a provider-reported email.
- Never auto-link identities because emails match. Link a new sign-in method only while the user is signed in to the existing account.
- Which sign-in methods each account type may use is configuration on top of this model; Gabriel decides the launch list.
- The normal session stores only the user id, and `current_principal()` reads the user row.
- Passwords use Werkzeug `generate_password_hash` and `check_password_hash`; OAuth/OIDC uses Authlib. No hand-rolled OAuth.
- Apple sign-in is **out of v1**. Add it only after its callback/state flow is designed and tested explicitly.

Status: the storage for external identities exists (`user_identities`, migration 019, and `web/services/identities.py`), keyed on the exact issuer + subject pair and never on email. Google OIDC implementation is reported complete in PR #331's uncommitted working tree and is under independent review; it is not yet merged or activated. See the current roadmap update above and `docs/ACCOUNTS_DESIGN.md` §5 and §13.

### OAuth transaction binding

Switch the normal session cookie to `SameSite=Lax`, keeping `Secure` and `HttpOnly`, so ordinary top-level GET redirects from Google can return with the application session.

Do not rely on server-side state keyed only by the `state` parameter. That can still allow login-CSRF/account-confusion flows if a callback is replayed in a different browser context.

For OAuth/OIDC v1:

1. Create a short-lived server-side OAuth transaction containing at least provider, random state, return target, expiry and single-use/consumed status.
2. Set a separate short-lived random browser-binding cookie for that transaction with `Secure`, `HttpOnly` where compatible with the flow, and `SameSite=None`.
3. Store only a verifier/hash or other non-sensitive binding value server-side as appropriate; the callback succeeds only when **both** the server-side transaction and browser-binding cookie match and the transaction is unused and unexpired.
4. The callback uses the `oauth_state` request-integrity policy. Route policy does not make cookies appear; it only selects the correct integrity mechanism.
5. Rotate/consume the transaction and binding on success or failure according to the design.

Apple's form-post callback is deferred from v1 because a route exemption cannot make a `SameSite=Lax` cookie accompany a cross-site POST.

### Admin

- The admin role is granted only by a server-side CLI command (for example `python3 -m src.manage_admin --grant <user>`). No web path, form or signup flow can create an admin.
- The admin account uses a strong password plus TOTP 2FA. Gabriel uses a separate teacher account for teaching.
- The env-password admin stays until accounts ship, then retires. The shared tester password retires at launch.

### Rate limits

Nginx currently limits each login by IP, and a whole class can share one school NAT IP. Before student accounts launch, use a looser per-IP safety limit plus per-account/per-identifier throttling for login and registration. Add teacher-side creation quotas/rate limits appropriate to early access.

### Ownership and the quiz flow

Status: assignment ownership and account-owned quiz attempts (`quiz_attempts.user_id`, migration 015) are implemented; a new attempt requires a signed-in account (student, teacher or admin) and belongs to that account alone — teacher/admin management authority never grants answer access to another account's attempt — and attempts from before accounts stay `user_id = NULL`, bound to their browser and unlinked. The unguessable assignment token and the landing after registration are not implemented. Current behavior: `docs/ACCOUNTS_DESIGN.md` §24 and `docs/AUTH_ROUTE_CONTRACT.md`.

- Add nullable `quiz_assignments.owner_user_id`. New teacher-created assignments are owner-scoped; admin sees everything. Legacy rows may remain unowned until Gabriel decides whether to assign them manually.
- Add nullable `quiz_attempts.user_id`. Existing name-based attempts stay `user_id = NULL`; do not guess identities from typed names.
- Keep `student_name` as the historical/display-name snapshot.
- New authenticated attempts are unique on `(assignment_id, user_id)` for rows where `user_id IS NOT NULL`.
- The old `(assignment_id, student_name)` uniqueness must apply only to legacy rows where `user_id IS NULL`; otherwise two real students with the same display name could not take the same test. If the existing uniqueness is a table constraint, use a numbered SQLite table-rebuild migration to replace it with the required partial unique indexes.
- Derive the new question seed from the user id rather than the typed name. Ownership checks use the account instead of `quiz_attempt_owner:<id>` session keys and keep returning 404.
- New student links use a random, unguessable token instead of sequential `/quiz/<assignment_id>`. Numeric links kept working during a compatibility period that has ended: the numeric student entry is now retired and answers a generic 404 with no redirect to the token link (`docs/DEPLOYMENT.md` section 24). Numeric teacher-management and account-owned attempt URLs remain.
- After registering, the student lands back on the test through `next` and `safe_redirect_target`.
- Do not auto-link legacy attempts to future accounts. Any later manual reconciliation must be explicit and auditable.
- Rate-limit assignment creation per teacher.
- Students are minors: collect the minimum personal data the flows need.

Implementation is split into small PRs after the design PR is reviewed. No account implementation starts until the schema, OAuth transaction binding, legacy ownership behavior and migration strategy above are agreed.

## DZI content gate

This phase follows the accounts and email/password registration implementation PRs. Official DZI questions are visible only to signed-in students, teachers and admin. The gate is content-level, not page-level, so every surface that renders questions must filter. МОН publishes these exams, so the gate drives registration rather than protecting secrets; per-surface filtering plus leak and cache-isolation tests is sufficient.

1. Apply `is_dzi_question` (PR C2) wherever questions render: section and topic pages under `/subjects/...`, legacy `/section/<slug>`, the `grade12-dzi-preparation` section (public today), and the home page search data if it includes question text.
2. `CachedSubjectBrowser` caches per database identity, not per viewer. Cache the full data and filter per request after the cache. Never filter inside the cached query, or one audience's view can get served to the other.
3. Where gated questions are hidden, visitors see a short Bulgarian prompt to register (copy from Gabriel).
4. Build a new student-facing DZI practice page with the `signed_in` access policy. Do not open `/dzi` or `/dzi/source/<slug>`; they are admin tooling that shows import counts and pool health.
5. Tests created by teachers may include DZI questions, since everyone taking them is registered.
6. DZI images stay in `web/static/img/questions/dzi/`, served by Nginx without a session check. This is accepted because an image without its question text reveals little.

Acceptance:

- At the data/query boundary used for anonymous rendering, no DZI question object is returned to the public surface.
- Render every public question-bearing page as an anonymous visitor and assert that no known DZI question id, prompt text, answer text or option text appears. Add `data-question-id` to question cards if needed as one signal, but do not rely on ids alone.
- Render the same surfaces as a signed-in student and assert that the expected DZI questions do appear.
- Test both cache directions: anonymous -> signed-in and signed-in -> anonymous. Each audience must receive its own filtered view even when the underlying full-content cache is warm.
- Apply the same leak checks to home/search payloads or any JSON/HTML data that includes question text.

## Entitlements and payments

Build the entitlement seam together with accounts, and build billing only once the end of the free period is decided. Keeping plans separate from roles means ending the free period becomes a data change, not a code sweep.

### Entitlements (with accounts)

- `has_feature(principal, feature)` is separate from access policies, and role checks never look at the plan.
- Every account starts on plan `early_access` with `plan_started_at` recorded. During the free period `has_feature` returns true for everything.
- Entitlements read local tables only and never call the payment provider during a request.
- Proposed default, pending Gabriel's decision: test access is covered by the assignment owner's plan, so a student never needs a paid plan to take their teacher's test. Implement the check against the assignment owner, not the student.

### Billing (later)

- Provider: Stripe Checkout (cards, Apple Pay, Google Pay; Apple Pay needs domain verification) or a merchant of record such as Paddle. Gabriel decides with his accountant.
- Store only the provider customer id, subscription status and current period end. No card data ever reaches the app.
- The payer may differ from the user (a parent paying for a student), so the subscription holds the payer's billing contact while the account is the student's.
- The webhook endpoint uses public/appropriate access plus the `provider_signature` request-integrity policy: verified signature, idempotent processing (store processed event ids), fast response. The same-origin rule is bypassed only because this route declares a different integrity mechanism; `provider_signature` is not an authorization role.
- Moving free users to paid requires explicit opt-in; no automatic charges.
- Payments stay on the web. A native iOS app would fall under Apple's in-app purchase rules.
- The return from hosted checkout works because the cookie is `SameSite=Lax` (see Accounts).

## Open decisions

Codex does not decide these; where a PR needs one before Gabriel answers, it uses the default and flags it in the PR report.

| Decision | Needed by | Default until decided |
| --- | --- | --- |
| Do legacy parser questions (`aug_2023`, `may_2023`, `jun_2022`) count as DZI? | C2 | Count them |
| Endpoint names: rename references or keep aliases | E1 | Rename |
| Sign-in methods per account type | Accounts / registration design | Decided: Google plus verified email/password registration for students and teachers; existing teacher/admin password accounts retained. Microsoft excluded; Apple out of scope. See the 2026-10-13 update. |
| Who covers test access: teacher's plan or student's plan | Entitlements | Teacher's plan; students never pay to take a teacher's test |
| How content reaches production today | G1/H | audit first; canonical reviewed inputs only after H |
| Payment provider: Stripe or a merchant of record | Billing | none; blocks billing |
| What happens to legacy assignments with no real owner account? | Accounts design | keep `owner_user_id = NULL`; admin can access them until manually assigned |
| Should legacy name-based attempts ever be linked to new accounts? | Accounts design | no automatic linking; keep `user_id = NULL` unless an explicit audited reconciliation is designed |
| How long may teachers self-declare during early access, and what verification triggers later? | Accounts design / launch | allow self-declaration with conservative quotas; require verification before teacher-only protected content or teacher-specific pricing |
| When, if ever, should Sign in with Apple be added? | post-v1 identity work | defer until the form-post callback/state/browser-binding flow has its own reviewed design and tests |

Non-code checks for Gabriel before launch: GDPR obligations as data controller for minors' data, including Bulgaria's digital consent age; consumer rules for subscriptions; invoicing and VAT for online card payments.
