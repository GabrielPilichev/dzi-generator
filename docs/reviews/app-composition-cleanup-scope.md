# A5 — clean the Flask composition root

Gabriel authorized A5 after PR #356 merged. Base:
`1a1edc5ab9865f07a0ac2baa4cab9538ddd20d92`.
Branch: `refactor/app-composition-cleanup`.
Goal: remove obsolete imports, refresh the module header and simplify three
literal-only f-strings in `web/app.py`, preserving application behavior.

## Allowed implementation

After this READY scope is committed and a draft PR exists, fresh Sonnet / High
may edit only `web/app.py`: audited unused import bindings, one-line reasons for
intentional exports, the top-level module docstring and the three literal-only
prints in the `__main__` block (base lines 692, 694, 695). Remove only their
unnecessary `f` prefixes; keep the printed text and all interpolated strings.
The new header describes composition/configuration/blueprint registration,
not a Phase 1 read-only browser. It contains no launch, deployment,
dependency-install, port or route-list instructions (drop the stale
`pip3 install flask`, `python3 web/app.py` and route list; those procedures
stay in AGENTS.md and `docs/DEPLOYMENT.md`). Static tests read the whole file,
docstring included: the header and new comments add no pinned strings such as
`remember`, `.sql`, `/quiz/<int:`, `MAX_CONTENT_LENGTH`, `session.`, `900` or
`15 minutes`. Leave function docstrings and other code alone.

Preparation found 52 Pyflakes unused-import diagnostics and three empty
f-strings. AST name-use alone is insufficient: callers and source pins matter.
The consumer inventory proposes these 35 removal candidates, subject to an
import-side-effect and external-reference check before removal:

```
copy, json, math, AssessmentScope, question_belongs_to_subject,
all_subjects, get_subject, dzi_source_title, fetch_question_visual_assets,
QUIZ_APPROVED_FILTER, QUIZ_DURATION_FALLBACK_BG, QUIZ_SUCCESS_RATE_FALLBACK_BG,
TEACHER_OPEN_ANSWER_STATUSES, attach_attempt_question_image_counts,
attach_question_image_indicators, quiz_attempt_duration_display,
quiz_attempt_success_rate_display, quiz_combined_score_summary,
quiz_fetch_assignment, quiz_parse_attempt_question_plan,
quiz_section_question_ids_by_section, quiz_teacher_difficulty_breakdown,
quiz_text_answer_informational_subtotal, quiz_text_auto_points,
sqlite_table_exists, _quiz_csv, _quiz_io, _quiz_json, _quiz_unicodedata,
_quiz_response, _quiz_abort, _quiz_redirect, _quiz_request, _quiz_url_for,
_quiz_render_template
```

Keep these 17 otherwise-unused bindings with a specific one-line consumer or
contract reason per binding (inline or immediately above it):

```
quiz_attempt_owner_session_key, DZI_FORMAT_VERSION, dzi_json_text_list,
dzi_parse_source_slug, dzi_source_slug, build_mixed_quiz_plan,
fetch_open_question_candidates, fetch_quiz_text_answers_for_attempt,
is_quiz_question_eligible, open_question_scope_summary,
quiz_assignment_mixed_status, quiz_db, quiz_parse_assignment_question_plan,
quiz_section_question_ids, quiz_write_assignment_note, sqlite_table_columns,
unique_text_values
```

The last name is required by `tests/test_dzi_predicate.py`'s exact import-line
pin, even without a runtime `web.app` caller. Keep the historical DZI aliases
pinned by `test_dzi_blueprint`. Preserve all locally used imports and
`from __future__ import annotations`; do not consolidate working aliases such
as `_quiz_os` / `_QuizPath`. If further usage or an import side effect is found,
retain the binding and document the concrete reason; do not remove it to satisfy
a count. No wrappers, synthetic uses, export tricks or lint suppression.

## Preservation

All functions, decorators, hook/blueprint registration order, config, gates,
secrets handling, path/provider callbacks, DB behavior, request integrity,
authentication/ownership, responses and SQL remain unchanged. Keep retained
bindings' names, origins/identity and relative import order. Inspect direct
imports, aliases, qualified/string patch targets, dynamic lookup lists and
source contracts across the tracked repository before declaring a name unused.
Do not change existing tests/support, services, templates, migrations, canonical
data, requirements or lint configuration. No A6 timestamp or A7 root cleanup.

## Verification and handoff

Scope preparation is source/document/consumer-inventory and whitespace review;
no application execution, unittest suite or DB build. Pyflakes 4.1.0 was installed
with approval only in a scratch lint environment; project dependencies are unchanged.

Implementation first runs focused unchanged compatibility, import-direction,
composition/production, isolation and auth/quiz smoke checks. Verify retained
exports resolve to the same objects, removed names have no tracked consumers,
and an isolated base/head URL map, hook order and stable configuration comparison
matches. AST comparison permits only import removals/comments, the module header
and the three literal-only f-string conversions; all functions remain identical.
Do not start the `__main__` server to check prints.

Run the same Pyflakes version on `web/app.py`. Remaining diagnostics must be
only the documented intentional-unused imports (normally 17); no empty f-string
or other warning. Raw exit 1 for those intentional diagnostics is expected,
not a clean lint result. Do not broaden cleanup to other modules.

Because this is the composition root and A5 requires it, one final complete
`python -m unittest discover -s tests` must pass on the implementation. Preflight
real dependencies (including `-S` subprocess imports), SQLite CLI and full Git
history first. If it fails, reproduce affected cases at the exact base and
report honestly; no unrelated fixes. Repeat a full run only when a concrete
correction or unresolved failure justifies it. No inferred full-suite success.
Compile to scratch and check whitespace; AGENTS DB/audit checks use explicit
temporary databases only, disclosing the known absent official-PDF audit exit 1.

Leave edits unstaged/uncommitted; report actual PR, exact HEAD, per-name evidence,
lint diagnostics, tests and limits. Gabriel commits/pushes; fresh Opus / High
reviews the exact pushed SHA. After verified approval print complete guarded
merge commands directly in chat; Gabriel decides whether to run them.
No real runtime DB/sidecars, generated vault, credentials/providers, gates,
VPS, deployment, local server or tunnel operations are authorized.
