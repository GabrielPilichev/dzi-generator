# LearnPilot (dzi-generator) — code review, pass 2
2026-06-11 · branch: main · HEAD: 5a200ec · reviewer: pass-2 (adversarial)

## TL;DR
- IMMEDIATE: every `/quiz/attempt/<id>...` route is unauthenticated and
  ownership-blind. Anyone who can guess the integer attempt_id reads
  another student's answers, sees their submitted files, and (POST with
  same-origin Origin) overwrites their practical uploads. RED. See N1.
- Pass-1 missed a working (if conservative) Origin/Referer-based CSRF
  defense at `web/app.py:191-196`; their CSRF "Y2" finding overstates
  risk for cookie-bearing browsers but understates risk for the IDOR
  paths above (where no auth gates the POST in the first place).
- Pass-1 was correct on R1 (test bug, not route bug), correct that all
  16 f-string SQL sites are safe, correct that `data/questions.db` is
  tracked. Wrong/incomplete on the auth model and on `inject_app_meta`
  (silently broken — N2).

## Phase A — adversarial audit of REVIEW_2026-06-11.md

| Pass-1 claim | Verdict | Evidence |
| --- | --- | --- |
| 614 pass / 1 FAIL on `python3 -m unittest discover -s tests` | CONFIRMED | re-ran in `.venv`: `Ran 615 tests in 7.132s — FAILED (failures=1)`; the lone failure is the same `test_practical_task_page_and_download_failures_are_safe`. (Discrepancy: 615 vs 614 — minor; pass-1 pre-counted before a recent test add.) |
| R1 root cause: every may_2025_v2 resource hits the "файлът липсва" branch because files aren't on disk | CONFIRMED | `data/reference/Май 25/May_2025/zad_26/Shipments.xlsx` referenced by `practical_task_resources.id=1` does not exist on this clone (`ls data/reference/` → No such file or directory). The template branch at `web/templates/practical_tasks.html:86` only emits `/dzi/practical/resource/.../download` when `resource.is_available` is true (`web/app.py:1029-1030`). So `is_available` is false for every row → no `/dzi/practical/resource/` substring in the rendered HTML. |
| R1 cause: "small, recent UI change broke a smoke contract" | REFUTED (in flavor) | The `is_available`-guarded `<a>` was already present in `web/templates/practical_tasks.html` BEFORE commit 5e79420 (verified via `git show 5e79420^:web/templates/practical_tasks.html`). The test never tolerated the missing-file branch. It only "ever passed" on machines/CI where `data/reference/Май 25/...` was actually populated. Not a UI regression — a fixture-completeness regression on this clone. Concrete fix shape pass-1 suggested (seed dummy file in setUp, or accept either branch) is correct. |
| All 16 f-string `execute(` sites are safe | CONFIRMED | Manual walk of each: `746` (interpolates `owner_clause`/`placeholders` — both literal), `1125` (`PRAGMA table_info({table_name})` gated by `ALLOWED_INTERNAL_TABLES` membership at line 1121), `1159/1191` (`{column}` resolved through `first_existing_column` against `sqlite_table_columns` — column candidate tuples are Python literals at line 1140-1143 etc.), `2007/2084` (`{source_filter}/{section_filter}` are static `"AND q.source_exam = ?"` strings — only the placeholder `?` carries user input), `2343` (`teacher_status_expr` literal), `2411/2434` (`{column}` from whitelist; `{placeholders}` literal), `2636/2655` (`{QUIZ_APPROVED_FILTER}` constant), `2725/3014` (`{placeholders}` literal), `3173` (no f-string here in current head — actually `quiz_difficulty_breakdown_from_questions` plain Python, false hit), `3627/3812` (`where_clause` literal). No injection. Pass-1's "fragile under maintenance — add `# SAFE:` comments" call is right. |
| Auth model: "centralized before_request, default-deny" | REFUTED (incomplete) | The `protect_admin_routes` guard (`web/app.py:347-379`) only fires on endpoints starting with `admin`, `dzi`, or `teacher` (plus `switch_profile` and `TESTER_TEACHER_ENDPOINTS`). Endpoints `quiz_start`, `quiz_attempt`, `quiz_result`, `quiz_practical_attempt`, `quiz_practical_upload` (`web/app.py:4667/4761/4932/1595/1611`) match none of these — they are PUBLIC. None of the route bodies verify that the session corresponds to the student who created the attempt; they just look up by `attempt_id`. This is not "default-deny." Confirmed live: a brand-new test client with no cookies returns 200 from `/quiz/attempt/1/practical/may_2025_v2` and `/quiz/attempt/1/result`, and 302 (to /result) from `/quiz/attempt/1`. Detail in N1. |
| CSRF: "not enabled" / "rely solely on session cookie + same-site Strict" | REFUTED (incomplete) | A second `before_request` at `web/app.py:191-196` (`protect_same_origin_unsafe_methods`) blocks every POST/PUT/PATCH/DELETE whose `Origin` (or `Referer` fallback) is not same-origin with `request.host_url`. Verified live: cross-origin POST to the upload route returns 403; missing-Origin POST also returns 403. With `SESSION_COOKIE_SAMESITE=Strict` plus this Origin gate, browser-side CSRF on cookie-authenticated routes is genuinely closed. Pass-1's recommendation to add tokenized CSRF is still defensible (defense-in-depth, non-browser clients), but this is a YELLOW, not the open hole pass-1's prose suggests. The remaining real CSRF concern: the two unauthenticated POSTs `/quiz/<assignment_id>` (start) and `/quiz/attempt/<id>` (submit) and `/quiz/attempt/<id>/practical/.../upload` — with no cookie auth required, any same-origin XSS or any same-origin form anywhere can abuse them; see N1. |
| Upload safety: extension whitelist + path build | CONFIRMED | `src/practical_uploads.py:64-79` (`sanitize_original_filename`) strips control chars, replaces `\\/:*?"<>|`, blocks `..` parts, enforces 200-char cap; `:84-91` (`validate_upload_extension`) checks dangerous-extension blocklist before allowed-extension allowlist (so `.pdf.exe` is rejected); `:114-121` (`ensure_under_upload_root`) calls `resolve(strict=False)` then `relative_to(root)`. Stored filename is always `{uuid_hex}{ext}` — original is never used as the on-disk name (`:138`). I could not construct a traversal mentally. |
| Resource download: `practical_resource_safe_path` | CONFIRMED, defense-in-depth | `web/app.py:1007-1023`: rejects `::` (Windows ADS-style), absolute paths, `..` parts, and paths that don't `relative_to` an allowed root after `resolve(strict=False)`. `resource_path` is sourced from the DB (`practical_task_resources.resource_path`, populated by import scripts), not from request input — so this is defense-in-depth, not load-bearing on user input. `safe_download_name` (`:1034-1038`) strips NUL/CR/LF/`/`/`\`. Symlinks: `resolve(strict=False)` follows symlinks before the `relative_to` check, so a symlink inside the allowed root pointing outside would escape, but the import path doesn't write symlinks. Acceptable. |
| Schema/migration coherence: migrations are truth, `src/schema*.sql` are stale snapshots | CONFIRMED | `src/schema_v3.sql` declares 8 tables (curriculum/topic/note tables only); `web/migrations/001..009.sql` declares 15 tables (quiz_*, exam_*, asset_*, practical_*, blueprint_*) and the live DB has 39 tables. v3 snapshot is at best a partial historical artifact. Pass-1's recommendation (move under `src/legacy_schemas/`) is correct. |
| `data/questions.db` tracked in git | CONFIRMED | `git ls-files | grep -E '\\.db$'` → exactly `data/questions.db`. `.gitignore:31` lists `data/questions.db` but the file is already tracked, so the ignore is a no-op (git doesn't ignore tracked files). Content audit: 4 quiz_attempts (student_name `1`, `petar`, `da`, `test1`), 19 quiz_answers, 0 practical_submissions, 0 practical_submission_files, 2 quiz_assignments, 0 quiz_text_answers. These are obvious test artifacts, not real student PII. No secrets. Still: `git rm --cached data/questions.db` and ship a seed script, per pass-1. |

## Phase B — new findings (severity-tagged, file:line + fix shape)

### RED

**N1. IDOR on every `/quiz/attempt/<int:attempt_id>...` route**
`web/app.py:1594-1607` (`quiz_practical_attempt`),
`web/app.py:1610-1701` (`quiz_practical_upload`),
`web/app.py:4760` (`quiz_attempt`),
`web/app.py:4931` (`quiz_result`).

None of these match the `protect_admin_routes` filter (`:347-379`) and
none of them check the session for ownership. They look up by integer
attempt_id (`SELECT * FROM quiz_attempts WHERE id = ?`) and proceed.
Live confirmation:
```
quiz_practical_attempt unauth: 200
quiz_attempt unauth: 302   # redirected because attempt 1 is submitted
quiz_result unauth: 200
```

Reachable consequences (assuming an adversary can guess/enumerate small
integer IDs — they are sequential AUTOINCREMENT):
- Read another student's MC answers and submitted text answers
  (`quiz_result` renders full review).
- Read another student's submitted practical files: the
  `quiz_practical_attempt` view emits filenames, and the file-download
  endpoint at `/teacher/practical/submission-file/<id>/download` is
  admin-only — but the **listing** of `original_filename`s leaks under
  the public route at `web/app.py:1235`.
- Add files to another student's submission: POST
  `/quiz/attempt/<victim>/practical/<slug>/task/<id>/upload` from any
  same-origin context (any submission form on the same host). The
  `protect_same_origin_unsafe_methods` gate is satisfied for any
  same-origin attacker (e.g. same-origin XSS, or a malicious local
  proxy / tester sharing the same Cloudflare tunnel). The upload then
  silently mutates `practical_submissions` rows owned by the victim
  attempt — `INSERT INTO practical_submissions (quiz_attempt_id, ...)`
  with `quiz_attempt_id = attempt_id` from the URL.
- Submit answers on behalf of another student: POST `/quiz/attempt/<victim>`
  is gated only by `attempt["submitted_at"]` being NULL.

Why it has held so far: in single-school deploys students don't share
the link. The protocol is "send a /quiz/<assignment_id> link, students
type their name, sequential IDs are issued." Anyone who watches the
URL bar sees `/quiz/attempt/47` and can substitute `46`, `48`, ...

Fix shape:
- Sign the attempt_id into the URL (HMAC over `(attempt_id, student_name)`
  with `app.config["SECRET_KEY"]`) and verify in each `/quiz/attempt/...`
  handler. URL becomes `/quiz/attempt/<id>-<sig>`.
- OR set `session[f"quiz_attempt_owner:{attempt_id}"] = True` at
  attempt creation (`web/app.py:4744`) and refuse routes whose attempt
  is not in the session. Cookie size grows over time but covers the
  common case.
- OR adopt a per-attempt opaque token (e.g. `secrets.token_urlsafe(24)`)
  as the URL key, kept in `quiz_attempts.access_token`.

This is the fix that should ship before the next public smoke. None
of pass-1's 4 small wins / 4 sprint items mention it.

**N2. `inject_app_meta` is silently broken for all renders**
`web/app.py:213-229`. The function references `BASE_DIR` and `datetime`
neither of which is imported at the top of the module (imports stop at
`:33` with `Path, PurePosixPath, urlparse, Flask, abort, g, ...`; the
underscored aliases like `_quiz_datetime` are introduced at `:1747+`
inside the bolt-on block, not at module scope). Every call into the
context processor raises `NameError` on the very first line touching
`BASE_DIR`, which is then swallowed by the bare `except Exception`.
Result: `last_sync_label` is permanently `None` and the footer "последна
промяна: ..." (`web/templates/base.html:156`) never renders.

Verified live:
```
$ python3 -c 'from web.app import app; ...'
status 200
has последна промяна: False
```

Severity is RED-borderline because the bug is a functional regression
hidden by a too-broad except — the same shape that masks real bugs
elsewhere. Pass-1 missed it entirely.

Fix shape:
```python
from datetime import datetime
# ... and replace BASE_DIR / "vault" with PROJECT_ROOT / "vault"
```
And tighten `except Exception` → `except OSError`.

### ORANGE

**N3. SAMESITE=Strict + Origin gate is good; the matching Origin gate
short-circuits on `app.config["TESTING"]`**
`web/app.py:188`: `return bool(app.config.get("TESTING"))` — when no
Origin and no Referer header is present, the gate becomes permissive
under Flask `TESTING` mode. The Flask test client never sets
`TESTING=True` automatically, so production isn't affected. But: any
test that flips `app.testing = True` (e.g. for log-suppression or
custom plugin) silently disables the entire CSRF defense for the rest
of the test process. Add a comment explaining the contract; consider
removing the bypass and requiring tests to set `Origin` explicitly.

**N4. `data/questions.db` ships with test artifacts**
4 attempts named `1`, `petar`, `da`, `test1` and 19 answers and 2
assignments. Not PII, but every clone of the repo starts with these
fake students populating `/teacher/assignments` and `/teacher/results`.
The DB also drifts from the migration sequence (it's been hand-touched
during dev). Recommend: `git rm --cached data/questions.db`, add a
`scripts/build_seed_db.py` that runs `web/migrations/*.sql` against an
empty file plus loads `data/import/*.json` and `data/import_batches/*.json`.
Pass-1 noted the tracking but didn't audit content.

**N5. N+1 query in `quiz_attempt` POST path**
`web/app.py:4818-4826` — for every question_id in the attempt, a
separate `SELECT question_type FROM questions WHERE id = ?`. Same
pattern at `:4858-4866` (one SELECT per MC qid for the correct
option_letter). For typical 25-question DZI attempts this is 50
extra round-trips on every submit. Not load-bearing for one local
school, but trivially fixable:
```sql
SELECT id, question_type FROM questions WHERE id IN ({placeholders})
```
plus a single batch fetch for `multiple_choice_options is_correct=1`.
The codebase already has `quiz_load_questions` (`:2725`) using exactly
this pattern; reuse it.

**N6. `inject_app_meta` masks all errors with bare `except Exception`**
Same site as N2. Even after fixing the imports, a broad except
swallows `OSError`, `PermissionError`, etc. Replace with the narrow
catch above; let `NameError` and `TypeError` bubble in dev so the next
typo doesn't sit silently for two months.

### YELLOW

**N7. `subprocess.run` in `teacher_dzi_training`**
`web/app.py:3382-3477`. `name` and `seed` flow into argv as
`--name`/`--seed` values. No `shell=True` (good). Admin-only (good).
But the values land in `src/generate_dzi_training_set.py`'s argparse;
that script reads/writes under `vault/Generated/DZI-Training/sets/`.
Confirm that script clamps `name` to a path-safe value before using
it as a filename. Note for a future audit, low priority — admin trust
boundary.

**N8. Cookie `SESSION_COOKIE_SECURE=True` + plain HTTP smoke**
`web/app.py:72`. AGENTS.md's localhost smoke runs HTTP on port 5001.
Modern browsers (Chrome 89+, Firefox 75+) carve out localhost as
"secure context" so `Secure` cookies do work — but the moment dawg
points the browser at `127.0.0.1` via a non-localhost name (e.g.
`mac-dev.local`), session cookies silently stop being set. Symptom:
"login appears to succeed but next page is logged out." Recommend a
`if app.debug: SESSION_COOKIE_SECURE = False` sniff, or read it from
env. Cloudflared HTTPS is unaffected.

**N9. `sqlite3.OperationalError` (DB locked) is uncaught everywhere**
`grep -nE "OperationalError" web/app.py` → 0 hits. WAL mode is set
(`:430`) which lowers the chance, but a long-running write (e.g.
`/teacher/dzi-training` POST holding the DB while writing to vault)
plus a concurrent write returns `database is locked` to the Flask
error handler — which renders Werkzeug's default 500 page. No 500.html
is wired (`web/templates/404.html` exists, no `500.html`). Add a
generic error handler.

**N10. `is_deleted` filter present on download lookup but absent on
`fetch_practical_attempt_page` listing**
`web/app.py:1228-1234` — selects `is_deleted` from
`practical_submission_files` but does not filter it out at this site
(filter is `is_deleted = 0`, OK — present at `:1232`). Cross-checked
against `:3598` (`if file_row["is_deleted"]: abort(404)`) — consistent.
False alarm; logging here so the next reviewer doesn't re-trip on it.

**N11. `protect_admin_routes` does NOT cover `quiz_*` endpoints**
By design (students need them). But the filter logic is a
prefix-startswith allowlist that's easy to break: a future endpoint
named `dzi_*` (e.g. helper for student-side DZI navigation) will be
auto-quarantined to admin even if intended public; conversely, a
future `student_*` endpoint will be auto-public unless someone updates
the guard. A single `REQUIRES_ADMIN`/`REQUIRES_TESTER` decorator with
explicit opt-in is harder to misuse than a startswith filter. Y-level
code-shape concern.

**N12. JS files: I read all four. Clean.**
- `web/static/js/home-search.js` (39 LoC): pure DOM filter, no fetch,
  no innerHTML, no eval.
- `web/static/js/section-tools.js` (278 LoC): localStorage bookmark
  toggle + filter; only `textContent`/`classList`, no innerHTML.
- `web/static/js/quiz-draft-autosave.js` (151 LoC): localStorage draft
  persistence, scoped per attempt_id; no fetch.
- `web/static/js/quiz-progress-indicator.js` (53 LoC): progress bar.

No XSS sinks, no CSRF concerns (no JS-driven POSTs at all). Pass-1's
"not a frontend project" was right.

**N13. Templates: no `|safe`, no `Markup`, no `render_template_string`**
Confirmed via `grep -rn "|safe\\|Markup\\|render_template_string"`
across `web/templates/` and `web/app.py` (0 hits). Jinja autoescape is
on by default for `.html` (Flask wires this; not overridden). Sampled
high-risk surfaces: `attempt.student_name` rendered in
`quiz_attempt.html:17` and `quiz_result.html:*` is autoescaped, even
though it originates from `request.form` at `web/app.py:4675` (note
that input is squashed to whitespace-collapsed text but not stripped
of HTML). Safe because of autoescape, but if anyone ever flips a
`|safe` it's an XSS. Document.

**N14. Three logically-distinct logger sinks: stdlib `logging` is not
imported anywhere**
`grep -n "^import logging\\|app.logger\\|logger\\." web/app.py` → 0
hits. The app emits via `print()` (none in the route bodies, but
`warnings.warn` in `build_secret_key`) and `proc.stdout/stderr` of the
subprocess in DZI training. There is effectively no auth-failure log,
no upload-failure log, no 500 log other than what Flask emits to
stderr. Operationally: nobody can tell from logs whether the
`/quiz/attempt/47` IDOR was exploited. Add `app.logger.warning(...)`
at the four `abort(404)` spots in upload/download paths.

### GREEN

- `protect_same_origin_unsafe_methods` (`web/app.py:191-196`): a real
  CSRF defense, missed by pass-1.
- `tester_login`, `admin_login` use `hmac.compare_digest`
  (`web/app.py:267-270`) — constant-time, surrogatepass-encoded.
- `safe_redirect_target` (`web/app.py:151-161`) blocks `//evil` open
  redirects on `next=`.
- Origin/Referer gate plus SameSite=Strict closes browser CSRF.
- `practical_uploads.py` is the cleanest sub-module in the codebase.

## Disagreements with pass-1's prioritization

- Pass-1 ranks R1 (failing test) as RED. I would call it ORANGE: it's
  fixture-completeness, not a code regression. Nothing in production
  is broken by it. Pass-1's confidence in the route correctness is
  earned, but the RED tag overstates it.
- Pass-1 puts CSRF (Y2) below structural concerns. Given the IDOR
  finding (N1), CSRF is the wrong axis to obsess over — N1 needs to
  be fixed first, and once attempt_ids are signed, CSRF becomes
  near-immaterial because the signed token IS the second factor.
- Pass-1's "GREEN: single before_request guard, easier to reason
  about than scattered decorators" is half-true. It IS easier to
  reason about, but the prefix-allowlist is fragile (N11) and missed
  the `/quiz/...` public surface. A scattered-decorator world would
  have made the missing decorator on `quiz_practical_upload` more
  visible to reviewers.
- Pass-1's structural-sprint ordering (extract `quiz` blueprint first)
  is fine, but N1 should land before any blueprint extraction —
  blueprint moves rename endpoints (`quiz.attempt` instead of
  `quiz_attempt`), which would silently widen or narrow the
  startswith-based guard. Lock the auth contract first.
- Pass-1 calls 16 f-string SQL sites "no injection found" — I confirm
  this, but pass-1 lists site `3173` which in current head is plain
  `quiz_difficulty_breakdown_from_questions` (no SQL). That's a stale
  line number, not a finding; mention only to prevent the next
  reviewer from chasing it.

## What pass-2 ALSO did not cover

- I did not enumerate every `{{ ... }}` in every template. Sampled
  base.html, practical_tasks.html, quiz_attempt.html, quiz_result.html,
  practical_resource label_bg/original_filename. ~18 templates total;
  I read 5.
- I did not run `pip-audit` or check transitive dep CVEs. The repo
  pins only Flask>=3.1,<4 (`requirements.txt`); resolved is Flask
  3.1.3 / Werkzeug 3.1.8 — both clean as of mid-2026 to my knowledge,
  but unverified.
- I did not exercise the IDOR fix mentally end-to-end against
  `tests/test_quiz_attempt_render.py` — there's a chance the tests
  rely on hitting `/quiz/attempt/<id>` without setting up an owner
  cookie, in which case the fix shape needs a test-mode shortcut.
- I did not read `audit_*.py` scripts; pass-1 already inventoried
  them.
- I did not check `web/templates/_question_visuals.html` or the
  `dzi_index.html`/`grade.html`/`section.html` family for IDOR-shaped
  references to user-controlled IDs.
- I did not verify that all 9 migrations applied to an empty file
  produce the same shape as the live DB — only counted CREATE TABLEs
  and confirmed `src/schema_v3.sql` is missing 7 tables.
- I did not run `coverage` on the test suite. The 615 tests cover
  enough that the IDOR slipped through, so coverage is not the
  primary gap; ownership-test discipline is.
- I did not inspect `src/generate_dzi_training_set.py` for argv
  injection from N7.

---
generated 2026-06-11, macOS, Python 3.9.6 venv, no live server touched,
no commits, no DB writes
