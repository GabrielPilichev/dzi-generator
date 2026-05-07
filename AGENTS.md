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
- /teacher/new
- /teacher/assignment/<id>
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
- Local-only passwords: tester `tester123`, admin `admin123`.
- Tester may use `/teacher/new` and the created assignment detail flow.
- Tester must not access `/dzi`, `/teacher`, `/teacher/assignments`, `/teacher/dzi-training`, teacher results, or admin-like teacher/DZI pages.
- Local smoke tests may dirty `data/questions.db` and create notes under `vault/Generated/Quizzes/`; restore/remove those before commits unless intentionally changing DB/vault content.
- Before commit, run:
  `python3 -m unittest tests.test_auth_guard tests.test_quiz_attempt_render`
  `python3 -m py_compile web/app.py tests/test_auth_guard.py tests/test_quiz_attempt_render.py`
  `sqlite3 "file:data/questions.db?mode=ro" "SELECT * FROM pragma_foreign_key_check;"`
  `python3 src/audit_dzi_state.py --source-slug may_2025_v2`
- Current DZI pool health expectation for `may_2025_v2`: imported 25, usable 15, filtered/excluded 10.

## Before finishing
Run relevant checks:
- python3 -m py_compile <changed Python files>
- sqlite3 data/questions.db "SELECT * FROM pragma_foreign_key_check;"
- git status --short
- git diff --stat

Do not commit until user approves.
