# LearnPilot

LearnPilot is a learning and exam-prep platform, live at [learnpilot.bg](https://learnpilot.bg)
and in testing with students and teachers. It serves 1,700+ questions across IT, physics and
biology, mapped to 180 curriculum topics and 11 official Bulgarian state exams (DZI).

## Highlights

- **Layered Flask app**: 11 blueprints (`web/blueprints/`) over a separate service layer
  (`web/services/`), Jinja templates, SQLite in WAL mode.
- **LLM pipelines on local models (Ollama)**, all kept out of the request path:
  - topic router: embeddings (`nomic-embed-text`) and cosine similarity shortlist candidate
    topics, the LLM picks one, and confidence and margin thresholds decide whether it is
    accepted (`src/reclassify_topics.py`);
  - answer predictor (`src/predict_answers.py`) and question generator with schema-validated
    output (`src/generate_questions.py`);
  - model output goes through validation, JSONL audit logs and human review before import,
    with dry-run by default (`docs/question_batch_review.md`).
- **Security**: deny-by-default role-based access (student, teacher, admin), same-origin checks
  on every unsafe request (`web/request_integrity.py`), Google sign-in (OpenID Connect), TOTP
  two-factor for admins, login throttling.
- **Operations**: 28 versioned SQL migrations; production refuses to start with pending ones.
  Nginx -> Gunicorn -> SQLite, with WAL-safe backups on systemd timers (`docs/DEPLOYMENT.md`).
- **Testing**: 5,700+ unit tests on temporary databases built from migrations and canonical data
  (`docs/TESTING.md`).
- **Workflow**: built in small, reviewed pull requests with AI coding agents; each larger feature
  has a design, scope and test plan in `docs/` (`AGENTS.md` holds the agent rules).

## Stack

- Flask
- SQLite at `data/questions.db`
- Jinja templates in `web/templates`
- Obsidian vault in `vault/`

## Database and Setup

`data/questions.db` is **ignored local runtime state**, not a tracked file. Git holds the
sources it is built from:

| What | Source of truth |
| --- | --- |
| Schema | `web/migrations/` (`000_baseline.sql` + numbered migrations) |
| Durable content | `data/canonical/` (`docs/CANONICAL_DATA.md`) |
| Runtime database | an ordinary SQLite file generated/provisioned from those sources |
| Production | an external persistent database outside the checkout, never stored in Git |
| Tests | temporary databases built from the same sources (`docs/TESTING.md`) |

A fresh clone has no database. Build one explicitly before running the app:

```bash
python3 -m src.build_database --output data/questions.db
```

The builder creates a new file from the migrations and `data/canonical` and refuses to
overwrite an existing one. The app never creates or rebuilds the database on startup, and
never reads `data/canonical` while serving requests. An existing local database keeps working:
keep it, and bring it up to date with the normal migration workflow
(`python3 -m src.manage_migrations`, see `docs/DEPLOYMENT.md`).

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
DZI_SESSION_COOKIE_SECURE=0 python3 - <<'PY'
from web.app import app
app.run(host="127.0.0.1", port=5001, debug=True, use_reloader=False)
PY
```

Sign-in is by account only, at `/login`; there are no shared passwords. Create
local accounts with the account CLI, which prompts for the password:

```bash
python3 -m src.manage_accounts --db data/questions.db create \
  --email <email> --name "<name>" --role teacher
```

An admin account additionally needs a confirmed TOTP second factor before it
can sign in: export `DZI_TOTP_KEY` (the same value for the CLI and the running
app), then run `totp-enroll --email <email>` and `totp-confirm --email <email>`.
`docs/DEPLOYMENT.md` (section 22) lists every command and
`docs/AUTH_ROUTE_CONTRACT.md` describes who may open what.

## Architecture & Security Assumptions

### Recent safety review notes
- Recent safety follow-ups keep app behavior unchanged while tightening release defaults: direct dev-server debug mode is controlled by `FLASK_DEBUG`, session cookie defaults are hardened, SQLite runtime sidecars are ignored, SQLite WAL mode is enabled, internal SQL table-name helpers are guarded by an explicit whitelist, and `requirements.txt` documents the minimal app/test dependencies.
- Larger follow-up direction: `web/app.py` should gradually move toward smaller route modules or blueprints, and `web/static/css/ui-pass9.css` should keep being cleaned in small, safe PRs.

### UI polish direction
- Recent UI polish keeps logged-out navigation simple, the homepage cleaner, and student/teacher/DZI flows easier to scan.
- These UI-only changes are not intended to change routes, scoring, assignment behavior, filters, or database behavior.

### Accounts and assignment visibility
- Every signed-in user is an individual account (`student`, `teacher` or `admin`); the former shared "tester" and "admin" passwords no longer exist.
- A teacher account sees and manages only the assignments it created, and their attempts and practical submissions. Another teacher's assignment answers 404.
- An admin account (password plus TOTP or a recovery code) sees everything, including assignments without an owner.
- Assignments created before ownership existed have no owner and are admin-only; nothing assigns them to a teacher automatically.

> **Naming:** DZI is a module inside LearnPilot, not the product name, so DZI-specific
> routes, scripts and tables keep their DZI names. The repository and local folder are still
> named `dzi-generator` for historical reasons.
