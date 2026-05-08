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

## Important Docs

- `AGENTS.md` - agent rules and project constraints
- `docs/reviews/dzi_expansion_log.md` - DZI expansion history and notes
- `docs/dzi_question_import_format.md` - reviewed JSON import format
- `docs/question_batch_review.md` - dry-run-only question batch review pipeline

## Local Run

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

### Tester visibility / multi-tenancy assumptions
- The tester role is currently a shared role, not an isolated per-teacher account.
- Tester-visible assignment pages may show assignments/attempts across the local instance.
- This is intentional for the current deployment/testing model (local/single-school/manual-testing).
- If per-teacher isolation becomes a goal later, it needs an explicit schema/auth design PR, not a small route guard patch.
