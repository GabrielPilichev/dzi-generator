# LearnPilot — Agent Instructions

## Stack
- Python stdlib + Flask
- SQLite at data/questions.db
- Jinja templates in web/templates
- Obsidian vault in vault/
- No npm, no JS framework, no SQLAlchemy unless explicitly approved

## UI
- Prefer a warm dark palette and avoid blue-dominant UI.
- Button backgrounds and body text must meet contrast requirements.
- Use dark text on amber/orange filled buttons when needed.
- Keep success green visually distinct from amber/warning states.

## Rules
- Inspect before editing.
- Prefer additive DB migrations.
- Do not run destructive DB changes without explicit approval.
- Do not OCR/import official PDFs directly into DB without reviewed JSON.
- Visible UI text should be Bulgarian.
- Code identifiers should be English.
- Do not store binary blobs in SQLite.

## Database policy
- `data/questions.db` is **ignored, untracked local runtime state** (with its `-wal`/`-shm`/`-journal` sidecars). It is not a source of truth and a fresh clone does not contain it.
- Schema source of truth: `web/migrations/`. Durable content source of truth: `data/canonical/`.
- Fresh local database: `python3 -m src.build_database --output data/questions.db` (explicit; the app never builds it at startup).
- Tests build temporary databases from migrations + canonical (`tests/support.py`); they never read or copy `data/questions.db`.
- Never delete, restore, regenerate or overwrite a developer's existing local `data/questions.db`; migrate it with the normal migration workflow.
- Do not commit `data/questions.db` or any other SQLite file/replacement snapshot.

## Existing routes to preserve
- /
- /grade/<n>
- /section/<slug>
- /dzi
- /dzi/source/<source_slug>
- /teacher
- /teacher/new
- /teacher/assignments
- /teacher/dzi-training
- /teacher/assignment/<id>
- /teacher/assignment/<id>/results
- /quiz/<assignment_id>
- /quiz/attempt/<id>
- /quiz/attempt/<id>/result

## Obsidian
- Vault path: vault/
- Topic notes: vault/Topics/
- Generated quizzes: vault/Generated/Quizzes/
- Generated exams: vault/Generated/Exams/
- Official PDFs: data/reference/dzi/official_pdfs/
- Extracted assets: data/assets/exams/<source_slug>/
- Do not delete vault files unless explicitly asked.
- If adding browsable generated content, prefer Markdown under vault/Generated/.
- User may sync vault with:
  python3 src/sync_vault.py --no-gc --verbose

## DZI model
- exams = official exam identity
- official_exam_sources = official PDF/source/archive metadata
- exam_tasks = task 1–28 skeleton
- exam_task_questions = links task to questions
- assets + asset_links = PDFs/images/files
- practical_tasks = task 26–28 metadata
- dzi_blueprints + dzi_blueprint_slots = DZI format

## DZI format
- Tasks 1–15: multiple_choice, 1 point each
- Tasks 16–25: short_answer, 3 points each
- Task 26: practical_spreadsheet, 15 points
- Task 27: practical_graphics, 20 points
- Task 28: practical_web, 20 points
- Total: 100 points

## Localhost smoke/release checklist
- Local smoke server (needs a stable `DZI_SECRET_KEY`; `DZI_SESSION_COOKIE_SECURE=0` only for plain-HTTP localhost):
  `DZI_SESSION_COOKIE_SECURE=0 python3 -c 'from web.app import app; app.run(host="127.0.0.1", port=5001, debug=True, use_reloader=False)'`
- For Cloudflare tunnel sharing, run Flask with `debug=False`.
- Sign-in is by account only, at `/login`. There are no shared tester/admin passwords and no `/tester/login` or `/admin/login`. Create local accounts in the local runtime DB with the repository tooling; it prompts for the password (never put one in a command line, a file or a commit):
  `python3 -m src.manage_accounts --db data/questions.db create --email <email> --name "<name>" --role teacher`
  An admin account also needs a second factor before it can sign in: set `DZI_TOTP_KEY` (the same value for the CLI and the server), then run `totp-enroll --email <email>` and `totp-confirm --email <email>`. See `docs/DEPLOYMENT.md`, section 22, for every command.
- A teacher account may use `/teacher/new` and manages only the assignments it created (detail, edit, duplicate, results, CSV, practical review); another teacher's or an unowned assignment answers 404.
- A teacher account must get 403 on `/dzi`, `/teacher`, `/teacher/dzi-training` and the other admin-only pages. Signed out, every protected page redirects to `/login?next=...`.
- An admin account (password, then TOTP or a recovery code) reaches everything, including unowned assignments.
- Local smoke tests may create notes under `vault/Generated/Quizzes/`; remove those before commits unless intentionally changing vault content. The runtime DB is ignored by Git.
- Before commit, run:
  `python3 -m unittest tests.test_auth_guard tests.test_quiz_attempt_render`
  `python3 -m py_compile web/app.py tests/test_auth_guard.py tests/test_quiz_attempt_render.py`
  `sqlite3 "file:data/questions.db?mode=ro" "SELECT * FROM pragma_foreign_key_check;"`
  `python3 src/audit_dzi_state.py --source-slug may_2025_v2`
- The `sqlite3`/`audit_dzi_state.py` commands above need a local runtime DB; build one first with `python3 -m src.build_database --output data/questions.db` if it does not exist (`audit_dzi_state.py` fails clearly when it is absent). Its pool-health expectation below describes a DB built from `data/canonical`.
- `audit_dzi_state.py` reports structural DZI readiness; unittest/tests pin quiz pool-health numbers.
- Current DZI pool health expectation for `may_2025_v2`: 25 total imported Part 1 questions, 15 MC quiz-ready, 10 short-answer/not-yet-supported, 0 broken/invalid MC.

## Localhost / Cloudflare tunnel guardrails
- Treat localhost and Cloudflare quick tunnels as manual smoke testing only.
- Do not start, stop, restart, or kill Flask/local app/cloudflared unless the user explicitly asks.
- If a tunnel is already working, leave it alone.
- For Cloudflare quick tunnel smoke testing, Flask should be run with `ProxyFix` at runtime so same-origin login checks see the `trycloudflare.com` scheme/host.
- Tunneled Flask runs should use `debug=False` and `use_reloader=False`.
- Do not commit tunnel URLs, secrets, local passwords, logs, `.env` files, uploads, or generated quiz artifacts.

### Safe read-only/status commands
Agents may run these without asking when relevant:
- `git status --short`
- `git branch --show-current`
- `lsof -nP -iTCP:5001 -sTCP:LISTEN`
- `curl -I http://127.0.0.1:5001`
- `python3 -m unittest discover -s tests`
- `sqlite3 "file:data/questions.db?mode=ro" "SELECT * FROM pragma_foreign_key_check;"`
- `python3 src/audit_dzi_state.py`
- `python3 src/audit_open_question_readiness.py`

### Forbidden unless explicitly requested
Do not run these unless the user explicitly asks to start/stop/restart local testing:
- `pkill -f cloudflared`
- `pkill -f web.app`
- `pkill -f flask`
- `lsof ... | xargs kill`
- `kill <PID>`
- `cloudflared tunnel --url ...`
- `app.run`, `flask run`, or `python web/app.py`

### Production deployment
- Production runs Gunicorn with `web.wsgi:app` (see `docs/DEPLOYMENT.md`); development keeps importing `web.app`.
- `web.wsgi` requires secrets and absolute state paths outside the checkout, never changes schema on import, and refuses pending migrations.
- Back up live databases only with `python3 -m src.backup_sqlite` (WAL-safe); never copy a live SQLite file.

### Generated artifact hygiene
- Do not commit `vault/Generated/Quizzes/`.
- Do not commit local uploads.

## Before finishing
Run relevant checks:
- python3 -m py_compile <changed Python files>
- sqlite3 data/questions.db "SELECT * FROM pragma_foreign_key_check;"
- git status --short
- git diff --stat

Do not commit until user approves.

## Structural refactor roadmap

The canonical sequencing and architecture plan for the ongoing LearnPilot
refactor, accounts, ownership, DZI content gating, entitlements, and future
billing work is:

`docs/REFACTOR_ACCOUNTS_ROADMAP.md`

The approved Accounts implementation specification is
`docs/ACCOUNTS_DESIGN.md`; for accounts work it takes precedence over the
roadmap's conceptual sketch.

Follow that roadmap one PR at a time. Do not skip prerequisites, combine
phases, or change the architecture sequence unless Gabriel explicitly changes
the plan.
