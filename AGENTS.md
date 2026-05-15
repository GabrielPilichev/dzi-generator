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
- Local smoke server:
  `DZI_ADMIN_PASSWORD=admin123 DZI_TESTER_PASSWORD=tester123 python3 -c 'from web.app import app; app.run(host="127.0.0.1", port=5001, debug=True, use_reloader=False)'`
- For Cloudflare tunnel sharing, run Flask with `debug=False`.
- Local-only passwords: tester `tester123`, admin `admin123`.
- Tester may use `/teacher/new` and the created assignment detail flow.
- Tester must not access `/dzi`, `/teacher`, `/teacher/assignments`, `/teacher/dzi-training`, teacher results, or admin-like teacher/DZI pages.
- Local smoke tests may dirty `data/questions.db` and create notes under `vault/Generated/Quizzes/`; restore/remove those before commits unless intentionally changing DB/vault content.
- Before commit, run:
  `python3 -m unittest tests.test_auth_guard tests.test_quiz_attempt_render`
  `python3 -m py_compile web/app.py tests/test_auth_guard.py tests/test_quiz_attempt_render.py`
  `sqlite3 "file:data/questions.db?mode=ro" "SELECT * FROM pragma_foreign_key_check;"`
  `python3 src/audit_dzi_state.py --source-slug may_2025_v2`
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
- `git restore data/questions.db 2>/dev/null || true` only when cleaning unintended runtime/test DB dirtiness.

### Forbidden unless explicitly requested
Do not run these unless the user explicitly asks to start/stop/restart local testing:
- `pkill -f cloudflared`
- `pkill -f web.app`
- `pkill -f flask`
- `lsof ... | xargs kill`
- `kill <PID>`
- `cloudflared tunnel --url ...`
- `app.run`, `flask run`, or `python web/app.py`

### Generated artifact hygiene
- Do not commit `vault/Generated/Quizzes/`.
- Do not commit local uploads.
- If tests/runtime dirty `data/questions.db` unintentionally, restore it unless the DB change is explicitly planned.

## Before finishing
Run relevant checks:
- python3 -m py_compile <changed Python files>
- sqlite3 data/questions.db "SELECT * FROM pragma_foreign_key_check;"
- git status --short
- git diff --stat

Do not commit until user approves.
