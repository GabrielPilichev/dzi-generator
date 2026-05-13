# DZI Practical Task Workflow — Manual Smoke-Test Checklist

This checklist is for manual smoke testing the DZI practical-task workflow for
`may_2025_v2` tasks 26-28. It is a docs-only operational checklist; it does not
define new schema, routes, imports, scoring, or deployment behavior.

## Preconditions

- Work from a clean branch, or save/stash unrelated local work before starting.
- Localhost may be used for manual smoke testing.
- Use a generated `DZI_SECRET_KEY` and rotated admin/tester passwords for the
  session.
- Do not commit `.env`, logs, tunnel URLs, uploaded files, secrets, or local
  runtime config.
- If `data/questions.db` is dirtied unintentionally, restore it unless the
  smoke test intentionally checks DB writes.

## Localhost / Tunnel Caution

- Do not start, stop, or restart the Cloudflare tunnel unless explicitly
  testing remote access.
- If the tunnel already works, leave it alone.
- If running behind a tunnel, use `debug=False` and `use_reloader=False`.
- Do not run broad process-kill commands such as `pkill`, `kill`, or
  `lsof ... | xargs kill` unless explicitly asked.

## Student / Tester Flow

- Open the practical task page for `may_2025_v2`.
- Confirm tasks 26, 27, and 28 instructions render.
- Confirm resource download links appear for the practical tasks.
- Download the official resource files and verify the filenames are sane.
- Upload one valid file for a practical task.
- Upload multiple valid files for a practical task if the current UI supports
  multiple file selection.
- Verify uploaded original filenames display on the practical task page.
- Verify raw `stored_path` values and absolute filesystem paths are not exposed.
- Try an invalid extension, such as `.exe`, and confirm it is rejected safely.
- Try an empty upload and confirm it is rejected safely.
- If easy to test without creating large local files, verify oversized upload
  behavior.

## Teacher / Admin Flow

- Open the practical submission review page.
- Confirm submitted practical files appear with student/attempt context.
- Confirm task number and source/task context are visible.
- Download uploaded files from the review page.
- Verify raw `stored_path` values and absolute filesystem paths are not exposed.
- Enter a valid manual score and teacher note.
- Confirm the saved score and note appear on the review page.
- Try a score below `0` and confirm it is rejected safely.
- Try a score above the task maximum points and confirm it is rejected safely.
- Confirm invalid score submissions do not crash the app.
- Confirm tester/student users cannot access teacher review or save routes.

## Data Hygiene

- Uploaded files should be stored outside tracked source files.
- `data/uploads/` must not be committed.
- Check `git status --short` after the smoke test.
- Clean test uploads if needed, but do not delete official source/resource
  files.
- Do not restore intentional DB writes made solely to test upload/review
  persistence unless the branch policy requires a clean DB afterward.

## Safe Status / Check Commands

```bash
git status --short
git branch --show-current
python3 -m unittest discover -s tests
sqlite3 "file:data/questions.db?mode=ro" "SELECT * FROM pragma_foreign_key_check;"
python3 src/audit_dzi_state.py
python3 src/audit_open_question_readiness.py
```

## Known Non-Goals

- No practical combined score integration yet.
- No practical tasks in the normal mixed/open assignment picker yet.
- No import of other sources' practical tasks yet.
- No production deployment assumptions.
