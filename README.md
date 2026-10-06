# LearnPilot

LearnPilot is a local learning and assessment app. Its current focus is
Bulgarian information technology preparation, including DZI preparation.

DZI is a feature/module inside LearnPilot, not the product name. DZI-specific
routes, scripts, schema tables, source slugs, and import flows keep their DZI
names because they model the Bulgarian state exam domain.

The repository and local folder are still named `dzi-generator` for historical
reasons. Path commands continue to use `~/dzi-generator` until the repo/folder
rename is handled separately.

## Stack

- Flask
- SQLite at `data/questions.db`
- Jinja templates in `web/templates`
- Obsidian vault in `vault/`

## Database Tracking Policy

`data/questions.db` is currently tracked intentionally as the project seed/dev
database. It gives local development, smoke tests, and the current DZI/quiz UI a
known dataset.

The file is also listed in `.gitignore`, but `.gitignore` does not untrack files
that are already tracked by Git. Git will still report modifications to
`data/questions.db` after tests or local runtime use. Restore accidental DB
dirtiness before commits unless the PR explicitly owns a planned DB/data change.

A fresh database no longer needs the tracked file: `python3 -m src.build_database
--output /path/to/new.db` builds one from the migrations and `data/canonical`
(`docs/CANONICAL_DATA.md`), and the test suite builds its databases the same way
(`docs/TESTING.md`). Untracking the seed itself is a later PR.

## Important Docs

- `AGENTS.md` - agent rules and project constraints
- `docs/reviews/dzi_expansion_log.md` - DZI expansion history and notes
- `docs/dzi_question_import_format.md` - reviewed JSON import format
- `docs/question_batch_review.md` - dry-run-only question batch review pipeline
- `docs/reviews/dzi_image_db_linking_plan.md` - confirmed DZI question image pipeline checklist
- `docs/DEPLOYMENT.md` - production deployment (Nginx -> Gunicorn `web.wsgi:app` -> SQLite/WAL), backups and operations

## Development Setup

```bash
cd ~/dzi-generator
python3 -m pip install -r requirements-dev.txt
python3 -B -m unittest discover -s tests
```

`requirements.txt` holds production/runtime dependencies only.
`requirements-dev.txt` includes it and adds `python-docx`, which the DOCX
build tools and the full test suite need. Optional script dependencies are
listed, commented out, in `requirements-dev.txt`.

## Local Run

Session cookies are `Secure` by default. When running over plain HTTP on
localhost, browsers will not keep the session cookie, so set
`DZI_SESSION_COOKIE_SECURE=0` for local runs only. Never set it in production.

Set a stable Flask secret key for local/deployed runs:

```bash
export DZI_SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
```

```bash
cd ~/dzi-generator
DZI_ADMIN_PASSWORD=admin123 DZI_TESTER_PASSWORD=tester123 python3 - <<'PY'
from web.app import app
app.run(host="127.0.0.1", port=5001, debug=True, use_reloader=False)
PY
```

## Architecture & Security Assumptions

### Recent safety review notes
- Recent safety follow-ups keep app behavior unchanged while tightening release defaults: direct dev-server debug mode is controlled by `FLASK_DEBUG`, session cookie defaults are hardened, SQLite runtime sidecars are ignored, SQLite WAL mode is enabled, internal SQL table-name helpers are guarded by an explicit whitelist, and `requirements.txt` documents the minimal app/test dependencies.
- Larger follow-up direction: `web/app.py` should gradually move toward smaller route modules or blueprints, and `web/static/css/ui-pass9.css` should keep being cleaned in small, safe PRs.

### UI polish direction
- Recent UI polish keeps logged-out navigation simple, the homepage cleaner, and student/teacher/DZI flows easier to scan.
- These UI-only changes are not intended to change routes, scoring, assignment behavior, filters, or database behavior.

### Tester visibility / multi-tenancy assumptions
- The tester role is currently a shared role, not an isolated per-teacher account.
- Tester-visible assignment pages may show assignments/attempts across the local instance.
- This is intentional for the current deployment/testing model (local/single-school/manual-testing).
- If per-teacher isolation becomes a goal later, it needs an explicit schema/auth design PR, not a small route guard patch.
