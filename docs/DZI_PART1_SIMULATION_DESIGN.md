# DZI Part 1 simulation design (C3)

Status: design only; nothing implemented, migrated, activated or exposed. Authority: the 2026-11-18 update (`docs/REFACTOR_ACCOUNTS_ROADMAP.md:13`) and its C3 line. Release intent comes from §5.4 of the pinned review `REVIEW_2026-11-11_NEXT_STEPS.md` (SHA-256 `d3112d4f572f7949a9f3a59a97c23b8490f3de82ff03cace3fee993e9368d0f3`, not tracked); that text approves none of the choices below. Scope: `docs/reviews/dzi-part1-simulation-design-scope.md` (SHA-256 `ff7fc96f37ddd66b941f98a8eeedf5ddd39e0a3fef6481b1acfe1b1e38272cab`). Sources read at `f5ab6e2` on base `ffa74c9` (#374); evidence dated 2026-11-22.

Labels. **Fact**: a tracked source, cited `path:line`. **Evidence**: counted on a disposable canonical build (Appendix A). **Proposal**: a new engineering decision, pending review. **Open**: unknown, or a decision for someone else. Unknown here and not decided: the official exam's timing, layout and marking scheme, legal basis and privacy (review D16), the activation date, the C5 public-content decision.

## 1. Proposals at a glance

| # | Proposal (all pending review) | § |
|---|---|---|
| P1 | Draw the 15 multiple-choice and 10 short-answer items **by kind** from official Part 1 task links, not by task number. Compose existing eligibility rules; add only the Part 1 link and a weight rule. No borrowed item: a thin pool refuses the start. | 4 |
| P2 | Store attempts in two **dedicated additive runtime tables** (migration 027). Not `quiz_attempts`, not the C2 `practice_*` tables, no system assignment. | 5 |
| P3 | The plan is a flat JSON array of 25 question ids in slot order; slot kind and points come from the tracked blueprint at start and are persisted per answer row. | 5.3, 6.2 |
| P4 | Server-owned deadline `started_at + part1_minutes x 60` (repo value 90, not externally verified), a short submit grace, and **lazy** expiry: no background close exists. | 7 |
| P5 | Take time from the existing lifecycle clock-health layer, so a bad clock refuses writes instead of extending or shortening an attempt. | 7.4 |
| P6 | Submit is one idempotent writer transaction (the C2 pattern), inputs are bounded, and no GET writes. | 6.4, 7.3 |
| P7 | Score = multiple choice by stored option key plus short answer by the existing normalizer and matcher with the stored sub-weights. The maximum is **45** (Part 1 only), never 100. Results read persisted verdicts and points. | 6.5-6.7 |
| P8 | Default-off build constant, `signed_in` + `same_origin`, owner-only, no teacher or admin read access, no link from admin DZI pages. | 8 |
| P9 | Five implementation PRs, then a separate activation PR. C4 and C5 stay out. | 10 |

## 2. Scope and preserved contracts

In scope: a signed-in account takes a timed Part 1 attempt, submits once, sees a saved result. Out of scope: Part 2 (tasks 26-28), the 100-point exam, teacher-generated tests, "my results" (C4), the DZI content gate (C5), legal text, mail, deployment, content additions, activation.

Preserved, guarded by the existing tests where they exist and by the tests of §9:

| Contract | Where it lives |
|---|---|
| Existing routes and the retired numeric `/quiz/<assignment_id>` 404 | `AGENTS.md`; `docs/AUTH_ROUTE_CONTRACT.md` |
| Assignments, tokens, quotas, lifecycle, teacher results | `web/services/quiz.py:240-305`, `web/services/lifecycle.py` |
| Admin-only DZI archive and training page | `web/blueprints/dzi.py:32,40,57,72`, `web/blueprints/teacher.py:95` (`ADMIN`) |
| C2 practice tables, service, default-off routes | `web/migrations/026_practice_runtime.sql`, `web/services/practice.py:299`, `web/app.py:647-650` |
| C4 (account-owned results by topic) and C5 (content gate) as separate scopes | roadmap; `docs/STUDENT_PRACTICE_DESIGN.md` §4 |

## 3. Where Part 1 comes from

### 3.1 Format rows

**Fact.** The format is data, not code: `src/seed_dzi_blueprint.py:11-72` seeds one blueprint and 28 slots, and the tracked canonical rows carry them (`data/canonical/dzi_blueprints.jsonl`, `dzi_blueprint_slots.jsonl`; schema `web/migrations/003_dzi_tasks_assets_blueprint.sql:87-110`). **Evidence** (loaded into the disposable build):

| Part 1 | Slots | Kind | Points each | Subtotal |
|---|---|---|---|---|
| Multiple choice | 1-15 | `multiple_choice` | 1 | 15 |
| Short answer | 16-25 | `short_answer` | 3 | 30 |
| **Part 1 total** | 25 | | | **45** |

Slots 26-28 (Part 2: `practical_spreadsheet` 15, `practical_graphics` 20, `practical_web` 20) bring the blueprint to 100. Slot numbers are contiguous 1-28 and the Part 1 order is exactly 15 multiple choice then 10 short answer. No Part 1 slot sets `topic_area`, `topic_id`, `section_id` or `required_asset_type`, so a slot constrains only kind, points and position. `blueprint.total_points` (100) and the per-exam skeleton sums (100 for all seven exams) are **not** Part 1: Part 1 is the sum of `exam_part = 1` slot points. The blueprint also holds `part1_minutes = 90` and `part2_minutes = 150`.

### 3.2 Repo format versus external verification

- **Fact, repo only.** The values above are what this repository seeded. `official_exam_sources` has seven rows, all `exam_pdf`, with `source_url` and `published_at` null (`data/canonical/official_exam_sources.jsonl`); nothing tracked records an official timing, layout or marking source.
- The exam skeleton is generated *from* the blueprint (`src/import_dzi_exam_skeleton.py:91-121,290`), so "skeleton agrees with blueprint" (zero mismatches in all seven exams, **Evidence**) is circular and verifies nothing external.
- Repo prose disagrees with the blueprint: `src/populate_curriculum.py:1261` and `src/setup_vault.py:202` say Part 1 is "~65 точки" and Part 2 "~35". The blueprint gives 45 and 55 and sums to 100. This design follows the executable blueprint; the prose is unverified and is not a source.
- The label `format_version = 'dzi_it_pp_2025_format'` (`web/services/dzi_exams.py:21`) is a repo import label, also given to 2022-2024 sessions. It says "imported under this blueprint", not "officially this format".
- **Open (external).** Official duration, scoring rules, sessions published after May 2025 (review D-1 says check, do not assume). Whatever the app shows must call 90 minutes the simulation's time limit, never the official one, until someone verifies it outside this repo.

### 3.3 Layout deviations are recorded in the importer

**Fact.** `src/import_dzi_questions_json.py:34-66,399-410` carries per-source overrides for four sources (`aug_2023_v2`, `may_2023_v2`, `may_2022_v1`, `aug_2022_v2`): tasks 11-13 are short answer (3 points) and 16-18 are multiple choice (1 point). **Evidence**: in those four exams the linked question types are `MMMMMMMMMMOOOMM | MMMOOOOOOO` (M multiple choice, O open); the other three (`may_2024_v1`, `may_2025_v2`, `aug_2024_v2`) match the blueprint. Each still has 15 multiple-choice and 10 open items overall. Whether the paper or the import is right is **Open (external)**. Consequence: pooling by task number would leave slots 11-13 and 16-18 with three candidates each. Proposal P1 pools by kind, which is also what the overrides encode.

## 4. Candidate pools

### 4.1 From official identity to a playable item

"Official exam identity" and "playable Part 1 item" are different layers. Each layer below is an existing rule unless marked new.

| Layer | Rule | Source |
|---|---|---|
| Official identity | `exam_task_questions` link (any role) or legacy parser row with `exams.level = 'DZI'` | `web/services/dzi.py:39-64` |
| Part 1 link (new) | `role = 'primary'`, task number 1-25, exam `format_version` equal to the blueprint slug | `web/services/dzi_exams.py:101-108` for the same join shape |
| Owned | Question and exam subject IT, `level = 'DZI'`, question year equals exam year | `src/dzi_open_questions.py:85-127` |
| Reviewed | `is_ai_generated = 0 OR quality_score >= 1.0` | `web/services/quiz.py:95` |
| Renderable (multiple choice) | four options, one correct, real texts, usable visual if the prompt needs one | `web/services/quiz.py:1016-1066`, `:988` |
| Auto-gradable (short answer) | every sub-answer has an accepted answer, no visual-dependent phrase, not task 26-28 | `src/dzi_open_questions.py:59-82`, `web/services/quiz.py:1042` |
| Slot weight (new) | stored `fill_in_subquestions.points` sum to the slot's points (3) | §6.5 |

C3 invents no eligibility rule beyond the two marked new. The existing short-answer readiness rule is the stricter of the two available (the quiz-eligibility rule alone, `web/services/quiz.py:1056-1066`, would admit 11 more items whose prompts name an image and which have a usable image file), and C3 keeps the stricter one. `fetch_dzi_pool_health` (`web/services/quiz.py:2441`) is admin readiness for one source and counts short answers as "not yet supported"; it is not reused.

Source and asset rules are preserved, not redefined: a prompt that needs a visual needs a usable file (`web/services/quiz.py:988`), visuals load through the existing loader, which accepts only two image asset types under `web/static/img/questions/dzi/` (`web/services/question_visuals.py:15-16,88`), so source PDFs are never surfaced; images are served as today without a session check, which the roadmap already accepts ("DZI content gate", item 6).

### 4.2 Evidence (disposable canonical build at `f5ab6e2`)

| Count | Value |
|---|---|
| DZI exams / with task links | 11 / 7 (`dzi_it_pp_2025_format`); four `modern_2023` exams have no links |
| Official-identity questions | 240 (140 multiple choice, 100 fill-in) |
| Linked to tasks 1-25, role primary | 175; linked to tasks 26-28: **0**; linked to two tasks: 0 |
| Linked and consistent (IT, DZI, year, `question.exam_id` = linked exam, `source_number` = task number, not AI) | 175 of 175 |
| Unlinked legacy rows (`jun_2022` 22, `may_2023` 21, `aug_2023` 22) | 65, of which 33 multiple choice pass eligibility |
| **Multiple-choice pool** (linked, approved, renderable) | **105** (93 at tasks 1-15, 12 at 16-18) |
| Open linked / quiz-eligible / readiness-eligible / weight rule | 70 / 69 / 58 / **56** |
| Multiple choice per source exam | 15 in each of seven |
| Open per source exam after the weight rule | 7, 7, 9, 8, 9, 7, 9 (exams 3, 5, 6, 8, 9, 10, 12) |
| Items with a resolved visual in the final pools | 7 of 105 multiple choice; 18 of 56 open |
| Duplicate normalized prompts inside either final pool | 0 |
| Sub-answers per open item (final pool) | 1 (20 items), 2 (2), 3 (34) |

Consequences: (a) the 15 and 10 requirements are met with margin (105 and 56); (b) **no single session supplies ten usable open items**, so the simulation is a mix across sessions ("built from the repository's official-session items following the blueprint"), not a replay of one paper, and its text must not claim otherwise; (c) `may_2025_v2` alone gives 15 multiple choice (as `AGENTS.md` expects) but nine usable open items: it has ten imported, and one prompt names an image, so even it cannot fill ten.

### 4.3 Thin pool, no borrowed question

**Proposal.** A start needs, per blueprint slot kind, at least as many distinct eligible items as slots of that kind (15 and 10 today, read from the blueprint, not hard-coded). If either pool is short, the start returns `pool_insufficient`, writes nothing and shows one generic Bulgarian "not enough questions yet" message with no counts and no source names. It never fills a slot from: a non-DZI or AI-generated question, a Part 2 item, an unlinked legacy item, an item of the other kind, another subject, or a repeated question. An open attempt keeps its stored plan (§6.2). Each point is a test (§9).

### 4.4 Content gaps that remain (none addressed here)

Needing separately approved content or data work, listed for the decision owner, not fixed by this PR: (1) 65 unlinked legacy items (33 multiple choice eligible) with no reviewed Part 1 link; (2) 11 open items excluded only because the readiness rule rejects any prompt that names an image, although each has a usable image file under the repository root; (3) two open items whose stored weights sum to 2 and 4 instead of 3 (`may_2022_v1` tasks 12 and 23): `validate_short_answer` (`src/import_dzi_questions_json.py:311-334`) requires positive weights but not that they sum to the slot; (4) the four deviating sessions of §3.3; (5) no explanation column on `questions`, so reviews show the existing "no explanation" message (`web/services/quiz.py:803-860`); (6) sessions after May 2025, unchecked; (7) the seven source PDFs are asset rows whose files are not tracked (`git ls-files data/reference` is empty), which C3 does not need.

## 5. Runtime storage

### 5.1 Constraints from the current code (Fact)

- `quiz_attempts.assignment_id` is `NOT NULL` (`web/migrations/001_quiz_tables.sql:14`) and unique per `(assignment_id, user_id)` (`015_quiz_attempts_user.sql:58`); `quiz_text_answers.attempt_id` references `quiz_attempts` with cascade (`005_quiz_text_answers.sql:24`), as do `quiz_answers` (`001_quiz_tables.sql:27`) and `practical_submissions` (`008_practical_submissions.sql:12`). Short answers cannot be stored without an attempt row.
- A new assignment needs a teacher or admin principal and the creation quota (`web/services/quiz.py:268-297`; `docs/STUDENT_PRACTICE_DESIGN.md` §2).
- C2 tables are section/topic scoped and multiple-choice only: `practice_sessions.section_id NOT NULL`, `score_total > 0`, the open-scope index over `(user_id, section_id, COALESCE(topic_id, 0))`; `practice_answers` stores a letter and a verdict (`web/migrations/026_practice_runtime.sql:32-95`). `_resolve_scope` rejects `dzi_training` sections and `_eligible_question_ids` excludes DZI rows (`web/services/practice.py:548-583`).
- No existing table has a deadline for an attempt. `assignment.time_limit_minutes` is advisory: the server computes remaining time for display and the client timer only ever allows an *empty* submission; the cutoff that binds is the assignment lifecycle (`web/blueprints/quiz.py:183-199`, `web/services/quiz.py:1775-1785`). The existing token-landing and attempt `GET` handlers can write lifecycle observation markers (`web/blueprints/quiz.py:96,234`); C3 must not.

### 5.2 Options compared

| | A. System assignment + `quiz_attempts` | B. Relax `quiz_attempts` | C. Extend `practice_*` | D. Dedicated tables (**P2**) |
|---|---|---|---|---|
| Principal and quota | Needs a teacher or admin principal and burns the quota; or forges ownership | None | None | None |
| Side effects | Unowned rows in admin lists, token, vault notes (C1 §2) | None directly | None | None |
| Schema change | None | Table rebuild of a populated runtime table with three child tables | `section_id NOT NULL` cannot be relaxed by `ALTER`; needs rebuild of C2's tables | Additive: two new tables |
| Mixed answers and deadline | Short answers fit; deadline does not | Both need new columns | Letter-only answers; no deadline | Designed for both |
| Contract risk | Teacher results, quotas, lifecycle, `/quiz/s` | Every reader of `quiz_attempts` meets a null assignment | Couples C2 invariants to a timed mixed attempt | None to existing readers |
| Updater and rollback | Existing | Rebuild under restore and rollback guards | Same | Same as #365 |

**E. One table with an answers JSON column** is the smallest in table count and was considered. It is not recommended: the updater's reference gate and the parity check read every integer in a JSON column as a question id (§5.4), so an answers document with letters, points and slot numbers would need either a bespoke walker or a flat shape that is no longer an answer document; RESTRICT keys and `CHECK`s cannot protect ids or points inside JSON; and per-question reads for C4 would parse JSON. D keeps ids in integer columns the existing gate already understands.

D is the smallest change that keeps the three existing contracts intact. The cost is one more pair of tables and a result page of its own; that is accepted.

### 5.3 Proposed shape (migration `027_dzi_part1_runtime.sql`, not written here)

`dzi_part1_attempts`: `id` (positive); `user_id` NOT NULL, RESTRICT to `users`; `blueprint_id` NOT NULL, RESTRICT to `dzi_blueprints`; `seed` text; `question_ids_json` text (**flat array of 25 distinct positive ids in slot order, slot = position**); `max_points_planned` integer (45 today); `duration_seconds` integer > 0; `started_at`, `deadline_at` text in the lifecycle format; nullable `submitted_at`, `expired_at`, `abandoned_at` (at most one set); `score_points`, `score_max` set iff submitted, `0 <= points <= max`, `max > 0`. A partial unique index on `(user_id)` while all three timestamps are null gives one open attempt per account; an index on `(user_id, started_at)` serves later reads.

`dzi_part1_answers`: primary key `(attempt_id, slot_number, subquestion_number)`; `attempt_id` RESTRICT to the attempt; `question_id` RESTRICT to `questions`; `kind` in (`multiple_choice`, `short_answer`); `subquestion_number` 0 for multiple choice, 1 or more for short answer; `chosen_letter` (multiple choice only, the stored option key); `raw_answer` (short answer only, text of bounded length); `is_correct` 0 or 1; `points_possible`, `points_awarded` whole numbers with awarded 0 or possible; `grader_version` text. A unique `(attempt_id, question_id, subquestion_number)` keeps a question out of two slots. Row-local `CHECK`s only (kind-consistent columns, types, ranges), as in 026; who may read, plan shape, eligibility, scoring and transitions are the service's job and are tested there. No question text, no accepted answer, no blob, no name, no timestamp from a client.

Why these columns: stable references (`question_id`, `blueprint_id`) survive a rebuilt database; the snapshot of duration, planned maximum and per-row points keeps a saved result independent of later blueprint or content changes; no correct answer is stored on an attempt (the review derives it from current content).

### 5.4 Classification, updater, backup

- Classify both tables `RUNTIME` in `src/canonical_tables.py:35-82` and regenerate the manifest's classification and `migration_level` (`data/canonical/manifest.json`; `tests/test_canonical_content.py:425-428`). A fresh build leaves them empty (`src/build_database.py:11-13`).
- The updater preserves every non-canonical table by digest (`src/update_canonical_content.py:641-654`) and validates question references. Add `("dzi_part1_attempts", "question_ids_json")` to `JSON_ID_COLUMNS` and `("dzi_part1_answers", "question_id")` to `INTEGER_ID_COLUMNS` (`:129-132`), and the matching checks to `runtime_reference_problems` (`src/canonical_parity.py:155-181`). **Both extractors treat every integer inside a plan as a question id** (`update_canonical_content.py:_json_ids`, `canonical_parity.py:_ids_in`), which is why the plan must hold nothing but ids: a slot number inside it would count as an unresolved reference.
- The updater is additive in v1: value changes and removals are unsupported and unrecognized divergence is refused (`docs/CANONICAL_CONTENT_UPDATE_DESIGN.md:41`). C3 therefore needs no updater change beyond the references above, and the current-content caveat (§6.6) is a guard, not an expected path.
- Backups: both tables live in the one SQLite file; back up only with `python3 -m src.backup_sqlite` (`AGENTS.md`). A snapshot restore drops attempts made after it (`docs/DEPLOYMENT.md` §21). No blob is stored.

### 5.5 Migration and rollback

Additive, no rewrite, no backfill, no seed row; code that predates it never names the tables. The same precedent applies as for 026 (`git show --stat 30e6b9e`: migration, classification, manifest, parity, updater, tests in one PR). **Rollback**: migrations are forward-only and an older build refuses a database with migrations it does not ship (`web/production.py:563-570`, `docs/DEPLOYMENT.md:1195-1203`), so rolling the code back is not an off-switch; the off-switch is the default-off build constant (§8). RESTRICT keys protect referenced accounts, questions and blueprints from silent deletion and state no retention rule; retention is out of scope.

## 6. Account-owned flow

### 6.1 Routes and access (Proposal)

Prefix `/simulation/dzi-part1`, deliberately not under `/dzi` (admin).

| Route | Methods | Access | Integrity |
|---|---|---|---|
| `/simulation/dzi-part1` | GET/HEAD: read-only intro; POST: start or resume (303 to the attempt) | `signed_in` | `same_origin` |
| `/simulation/dzi-part1/attempt/<int:id>` | GET/HEAD: attempt; POST: submit | `signed_in` | `same_origin` |
| `/simulation/dzi-part1/attempt/<int:id>/result` | GET/HEAD | `signed_in` | none |

Like C2 (`web/blueprints/practice.py:167-250`, `docs/AUTH_ROUTE_CONTRACT.md` "Student practice"): any active student, teacher or admin account, each only on its own attempts; the owner is `current_principal()`; the only inputs are the path id and the answer fields; a signed-out request redirects to `/login?next=`; a foreign or missing id, and the *result* of an open, expired or abandoned attempt, give the same generic 404 for everyone including a teacher or admin, while the owner's own attempt page for an expired or abandoned attempt shows a short Bulgarian notice (the C2 pattern). The intro GET is read-only (the C2 confirmation page pattern) and discloses no attempt state.

### 6.2 Start and resume

One `BEGIN IMMEDIATE` through the lifecycle layer (§7.4), in this order:

1. Active account, else `account_unavailable`.
2. Read the blueprint (`is_active`, the Part 1 slots). Validate: exactly the expected contiguous Part 1 slots, kinds only in {multiple choice, short answer}, positive points, `part1_minutes` positive and bounded, no unsupported slot constraint (`topic_id`, `section_id`, `required_asset_type`, `topic_area`). Otherwise `blueprint_unavailable`, nothing written (fail closed, not silently ignored).
3. The account's open attempt, if any: parse the stored plan (malformed fails closed as C2's `PracticePlanError`). If it is still within the cutoff and presents at least one item, **resume**, writing nothing. If it is past the cutoff, mark it `expired_at` (lazy expiry, §7.2). If it presents nothing renderable, mark `abandoned_at` (the C2 "stale" rule).
4. Build the pools (§4.1). If a kind is short, `pool_insufficient`; an expired or abandoned row from step 3 is still committed.
5. Otherwise insert: `seed = secrets.token_hex(16)`; sort each pool by id; `random.Random(seed).sample` draws the items of each kind without replacement; items fill the slots of their kind in slot order. `deadline_at = started_at + duration`, with `duration_seconds` and `max_points_planned` snapshotted.

Deterministic: given the stored seed and the same pools the draw and the option order (`quiz_shuffle_options`, `web/services/quiz.py:1242`, seeded by the attempt seed) are reproducible, which is how tests pin them; the stored plan, never a recomputation, is authoritative. The partial unique index makes concurrent starts of one account leave one open attempt; a second `POST` returns it. During the grace window of §7 a start still returns the same attempt (its page says time is up), so that an in-flight auto-submit is never refused by the start that raced it; only after the cutoff does a start expire and replace it.

### 6.3 Attempt page (read-only)

One read snapshot (`lifecycle.read_snapshot`, `web/services/lifecycle.py:887`) on a `query_only` connection, as the C2 confirmation does (`web/blueprints/practice.py:_confirm`). It writes nothing, expiry and clock bookkeeping included. It shows the stored questions in slot order with the **slot number and points** per item, options in seeded order **without** correct markers (`quiz_load_questions(..., include_correct=False)`, `web/services/quiz.py:1254`), the visuals the existing loader attaches, and the remaining seconds computed from `deadline_at` and the lifecycle read clock. Notes: the existing loader selects only `id, subquestion_number` for short-answer parts, so a C3 loader must also select `subquestion_text` (a label such as "(1) София"); **Evidence**: no label equals or contains an accepted answer in the 132 sub-answers of the 58 readiness-eligible items, and a test keeps that true for the active pool. If the clock is unhealthy the page shows persisted state and says submissions are unavailable (the `submissions_unavailable` pattern, `web/blueprints/quiz.py:257`). Past the deadline there is no form: the page says time is up and offers a new start.

### 6.4 Submit and bounded input

Fields: `q_<question id>` carrying the stored option key (never a display letter) and `a_<question id>_<n>` carrying text for sub-answer `n`; both are matched by a fixed pattern and only for ids in the stored plan. Everything else is ignored; a field sent twice counts as unanswered (as `web/blueprints/practice.py:99`). **Proposed bounds** (pending review): the route rejects a request body over 64 KiB before reading the form, because the application-wide limit is about 100 MiB (`web/app.py:175`, `src/practical_uploads.py:24-29`) and the existing open-answer field has no length limit (`web/templates/quiz_attempt.html:160-168`); a text answer is at most 200 characters (**Evidence**: the longest individual accepted answer in the pool is 73 characters, and a full attempt has at most 45 answer fields, so the worst case is far below the body bound) with `maxlength` in the markup; a value over the bound or containing NUL refuses the whole submit with 400 and nothing written; at most one value per field. Nothing from the client names a role, owner, plan, seed, score, timestamp or deadline.

Single transaction, idempotent, mirroring `quiz_submit_attempt` (`web/services/quiz.py:2298-2401`) and `submit_practice` (`web/services/practice.py:771-817`):

1. Account, then the attempt by `(id, user_id)`; none, `not_found`.
2. `submitted_at` set: `already_submitted`, **no write of any kind** (the lifecycle `discard()`), so a retry leaves every table, the clock row included, unchanged.
3. `abandoned_at` set: `abandoned`, refused, nothing written (an old tab can never grade a replaced attempt). `expired_at` set, or now at or past the cutoff: `expired`; if the row was still unmarked, mark `expired_at` (guarded on open) and commit; no answer rows. Otherwise continue.
4. Parse the plan; presentable items = stored ids that still satisfy the per-item eligibility predicate (not a rebuild of whole pools). None: `stale`, nothing written.
5. No usable answer: `empty`, nothing stored, the attempt stays open (§6.7).
6. Grade (§6.5), insert one row per presentable multiple-choice slot and per short-answer part, then `UPDATE ... WHERE open` and verify the row count. Any inconsistency raises and rolls back; nothing partial persists.

### 6.5 Grading and scoring

- **Multiple choice.** Chosen key must equal a current stored option key; verdict against the one correct key (`quiz_correct_mc_letters_by_id`, `web/services/quiz.py:1322`; C2 `_grade`, `web/services/practice.py:735`). 1 point.
- **Short answer.** Reuse the pure matcher `grade_quiz_text_answers` (`web/services/quiz.py:659`) with accepted answers from `correct_answer` and `answer_alternatives` (`:397`) and the existing ordered or order-independent mode (`:406`; 54 ordered and 4 order-independent among the 58 readiness-eligible items). Limits to state to students, not to hide: exact match after NFKC, whitespace collapse, quote folding and `casefold` (`:369`); no typo, synonym, number-format, unit or word-order tolerance; no partial credit inside one sub-answer; the grader cannot judge meaning. A correct answer phrased differently scores zero.
- **Weights.** The existing grader awards 1 point per sub-answer (`:638-656`), which would cap a three-point, one-part item at 1. C3 instead awards the stored `fill_in_subquestions.points` of each correct part. **Evidence**: 56 of 58 ready items sum to 3 (34 as 1+1+1, 20 as a single part of 3, 2 as 1+2); the other two are 1+1 and 1+1+1+1. The 4 order-independent items all have equal weights, which makes matching weights to parts unambiguous. The weight rule of §4.1 (sum must equal the slot's points; order-independent items need equal weights) keeps awards whole and slot-true; the two non-conforming items stay out until reviewed.
- **Persisted verdicts.** Each row stores verdict, possible and awarded points and `grader_version`; the score is the sum of awarded points of presentable rows and the maximum the sum of possible points. Neither is recomputed later.
- This is **mixed effective scoring**: unlike assignment attempts, where open answers are shown as an informational subtotal beside the stored multiple-choice score (`web/services/quiz.py:876-912`), the simulation score includes auto-graded short answers.

### 6.6 Result, saved score and the current-content caveat

Only a submitted attempt has a result; others give the generic 404. It shows `score_points / score_max` from the stored row, the multiple-choice and short-answer subtotals from the stored answer rows, and a review per slot. Same rule as C2 (`docs/STUDENT_PRACTICE_DESIGN.md` §4): stored ids, verdicts and points are never recomputed; prompts, options, correct answers, accepted answers and explanations are read from **current** content, so after a content change a review can disagree with the saved verdict; items no longer presentable are omitted and counted, and an empty review still returns the saved score. Correct answers appear only here, after submission, never in an attempt page or answer row. Reuse: the multiple-choice review partial (`web/templates/_multiple_choice_result_review.html`, which numbers by `loop.index` and needs a slot-number parameter, so a render-equality test pins the old result page) and the explanation fetchers; the short-answer review is a new block (the assignment page's block carries teacher-override state, `web/services/quiz.py:1971-2012`). The page must state that the short-answer check is automatic and strict, and that the 90 minutes are the simulation's limit.

### 6.7 Incomplete, timed-out, maximum, later eligibility

- **Maximum.** Part 1 only: `max_points_planned` is 45 for a full plan; `score_max` is the sum over presentable rows, so it equals 45 unless an item became unpresentable. Never the blueprint's 100.
- **Incomplete.** Unanswered items score 0 and are stored; a submission needs at least one usable answer, meaning a stored option key of a plan question or a non-blank text for a plan sub-answer (an all-empty submit is refused, writes nothing and stays open).
- **Timed out.** An attempt not submitted by the cutoff is `expired`: no answers are stored, no score, no result, no count. The page says time ran out. **Open**: a late submit is lost; the alternatives (accept it unofficially, or save drafts on the server) are listed in §11.
- **Later C4 (separate scope).** Stored data lets C4 take only attempts with `submitted_at` set and `score_max = max_points_planned` as a labelled "DZI Part 1 simulation" source, with `grader_version` and the short-answer caveat, never merged into C2 or assignment progress. Nothing here defines that screen or any rollup.

## 7. Time

### 7.1 App timing is a proposal

Official timing is not established by tracked evidence (§3.2). **Proposal**: `duration_seconds = blueprint.part1_minutes x 60` at start (5400 today) and a submit grace `SUBMIT_GRACE_SECONDS = 60`, both app timing, both pending review and both stored or named in one place. The grace only absorbs request latency of an auto-submit at zero; it is not extra working time and is not shown as such. The existing assignment grace is 15 minutes for a different purpose (`web/services/lifecycle.py:50`).

### 7.2 States and lazy expiry

State is a pure function of the row and `now`: **submitted**, **abandoned**, **expired** (`expired_at` set), or unmarked-open: **open** (`now < deadline_at`), **grace** (`deadline_at <= now < deadline_at + grace`), **lapsed** (`now >= deadline_at + grace`, unsubmitted). The cutoff is exclusive, as the lifecycle cutoff is (`web/services/quiz.py:2318`).

**There is no background close.** No job, timer thread, cron or startup hook exists or is proposed. "Lapsed" is an interpretation every reader applies; the row gets `expired_at` only inside a write transaction of the owner's own `POST` (start or submit). Consequences, all tested: an untouched lapsed row stays "open" in storage and still occupies the account's single open slot until that account's next start or submit; no read changes it; any aggregate must test stored `submitted_at`, never derived state; a periodic cleanup, if wanted, is a separate scope with its own approval.

### 7.3 Races, refresh, old tabs

| Situation | Result |
|---|---|
| Submit before the deadline | Graded and stored |
| Submit at or after the deadline, before the cutoff | Graded and stored (grace) |
| Submit at or after the cutoff | `expired`, nothing graded; row marked once; retry writes nothing |
| Double submit, or two tabs | One answer set; the second finds `submitted` and writes nothing |
| Submit racing a start of the same account | The writer lock orders them; a start cannot abandon an unexpired, presentable attempt, so a submit never loses to it; a lapsed attempt is expired first and the late submit is refused |
| Old tab after a new attempt | Its id differs; it can only reach its own row, which is submitted, expired or abandoned: no write, no effect on the new attempt |
| Refresh | Same stored plan, seed, order and `deadline_at`; the remaining seconds are recomputed, never reset; nothing written |
| Client clock wrong or JS off | The countdown is display only; the server decides from its own clock; without JS the user submits manually |
| Request body from a client claiming a time | Ignored |

### 7.4 Clock failure

**Proposal**: take time from `web/services/lifecycle.py` as the quiz does. Writes use `write_transaction` (`:1099`), which refuses a regressed, stepped, unsynchronised or suspect clock with `LifecycleUnavailable`, answered as 503 (`web/app.py:578`); `now` never runs behind the high-water mark; reads use `read_clock` (`:875`). So a clock that jumps back cannot extend an attempt, and a clock fault stores only the clock row's own fault record (`_record_clock_fault`, `web/services/lifecycle.py:909`) and no C3 row. C2 uses the raw UTC clock because it is untimed (`web/services/practice.py:525`); a binding deadline is the reason C3 should not. Trade-offs, for review: (a) a clock fault blocks C3 writes together with assignment writes until an operator recovers the clock (`docs/DEPLOYMENT.md` clock commands); (b) the outage does not pause the deadline, so a student refused during a fault may find the attempt expired afterwards; (c) tests use the existing `lifecycle_time` helper (`tests/support.py:140`), not hand-patched clocks.

## 8. Exposure, ownership, gate

- **Default off.** A build constant, separate from C2's (`web/services/practice.py:299`), registers the blueprint in `web/app.py` only when true, as `web/app.py:647-650` does; no setting or environment variable overrides it; tests arm it only in a separate interpreter before importing the app (`tests/test_practice_routes.py:99-125`). While off, no `/simulation` rule exists and the URL map is as before. Arming and any link are a separate, explicitly approved launch change under the pilot's own activation prerequisites (roadmap, R2). This PR sets nothing.
- **No admin exposure.** The simulation adds no link, redirect or data path to `/dzi`, `/dzi/source/<slug>`, `/teacher/dzi-training` or any teacher page; they stay `ADMIN` and unchanged. The student pages show no import counts, pool health, exam source slug, task number or PDF; error pages are generic and name no stored state. Students get no admin route, teacher results never list simulation attempts, and no teacher or admin can read another account's attempt.
- **No assignment machinery.** No system teacher, no `quiz_assignments` row, no token, no `quiz_attempts` row, no vault note, no quota use.
- **Ownership.** `user_id` comes only from the validated principal; the owner is in the same `WHERE` as the id.
- **Content gate.** C3 renders official DZI items only to signed-in accounts, consistent with the roadmap's DZI-gate item 4 (`docs/REFACTOR_ACCOUNTS_ROADMAP.md`, "DZI content gate"). The anonymous-surface filtering and cache tests are C5's and unchanged.
- **Unchanged prerequisites, owned elsewhere:** migration applied to the target database (production start-up already refuses pending ones, `web/production.py:554-561`), privacy and legal decisions (D16), approved Bulgarian copy, capacity and clock evidence, content review of §4.4.

## 9. Acceptance tests (future, temporary databases only)

Built from migrations plus canonical via `tests/support.py` (`build_test_database`); never `data/questions.db`.

- **Format and pools.** Blueprint has the expected Part 1 rows and the start fails closed on a tampered one (kind, count, points, gap, unsupported constraint). Pools equal the §4.2 counts on the canonical build (a content PR updates them deliberately). Every pool item is official, Part 1 linked, approved, renderable or gradable, never Part 2, never AI, one kind per slot. Thin pool in each kind: `pool_insufficient`, nothing written, nothing borrowed, message free of counts. Plan: 25 distinct ids, kinds and points per slot, same seed gives the same draw and order.
- **Storage.** Migration is additive from a populated 026 database; row-local checks with negative controls (as `tests/test_practice_runtime_migration_sql.py`); classification, empty fresh build, updater preservation, reference gate and parity see both tables; a slot number inside a plan would be flagged (documents §5.4).
- **Start and resume.** One open attempt per account under concurrent starts; resume writes nothing; lapsed row expired and replaced in one transaction; stale row abandoned; malformed plan fails closed.
- **Submit.** Idempotent (second submit changes no row and not the clock row); bounds (oversize body, over-length, NUL, duplicate fields, unknown ids ignored); empty refused; expired and abandoned refused with no answer rows; scores equal the persisted rows; weights per part; ordered and order-independent cases; a changed question after start never changes the saved score.
- **Time.** At the deadline minus 1 s, at the deadline, at the cutoff minus 1 s, at the cutoff (exclusive); with `lifecycle_time`: clock regression, step, unsynchronised, reboot, suspect state give 503 and write no C3 row, and reads show persisted state; all with no state change on any GET (digest of every non-canonical table and the clock row before and after, including a GET past the deadline).
- **Access.** Signed-out redirects write nothing; cross-origin POST is 403 before any write; another account, a teacher and an admin get 404 on attempt and result; an open, expired or abandoned attempt has no result; no correct answer in any attempt page or answer row; no label leaks an accepted answer.
- **Isolation and gate.** Routes absent when unarmed (URL map equal to before); existing routes, the numeric 404, admin DZI pages, assignments, quotas and C2 unchanged; no assignment, token, vault note or `quiz_attempts` row created; armed, the policies are exactly `signed_in` and `same_origin`.
- **Rendering.** The multiple-choice partial renders the old assignment and C2 result pages byte-equal after the slot-number parameter; the result shows 45 as the maximum and subtotals from stored rows.

## 10. Implementation order

One PR at a time, each separately reviewed; none activates anything.

1. **C3.1 Storage.** Migration 027, classification, manifest, parity, updater id columns, tests (the #365 shape).
2. **C3.2 Pools and start.** Blueprint validation, pool composition, start/resume/expire/abandon service, deterministic plan, tests incl. thin pool and no borrowing.
3. **C3.3 Submit and grading.** Idempotent submit, bounds, grading, weights, expiry races, clock tests.
4. **C3.4 Result loader.** Saved result, review rows, partial parameterization with the render-equality test.
5. **C3.5 Routes and templates, default off.** Policies, `AUTH_ROUTE_CONTRACT.md` section, armed-interpreter tests, Bulgarian copy once approved.
6. **Activation** is a later, separately authorized change (§8). C4 and C5 keep their own scopes.

## 11. Open decisions for review

1. Grace length (60 s) and whether a late submit is lost, accepted as an unofficial untimed result, or saved as server-side drafts (more storage and races). Recommendation: lost, with the client auto-submit and the existing browser-local draft script (`web/static/js/quiz-draft-autosave.js`) as the model for a mitigation.
2. Pool by kind (recommended) versus by task number (leaves six slots with three candidates and ignores the importer's overrides).
3. Mixed pool across sessions (recommended; no session supplies ten open items) versus replaying one session.
4. The weight rule (exclude the two non-conforming items) versus scaling their weights.
5. Lifecycle clock coupling (recommended) versus the raw clock of C2.
6. Input bounds (64 KiB body, 200 characters per text answer).
7. Whether to hide exam source and task number from students (recommended) and whether result text may name the year or session.
8. URL prefix, and whether teacher and admin accounts may take the simulation as accounts (as C2 allows).
9. A per-account attempt rate cap (none proposed; repeated attempts only add rows).
10. Duplicate-prompt guard (none needed today, 0 duplicates).
11. C4 eligibility wording and weight of the short-answer component (C4's scope).
12. External verification of duration, layout and marking before any "official" wording.

## Appendix A. Evidence record

All on **2026-11-22**, interpreter `/private/tmp/learnpilot-content-updater-implementation.auvobkfa/venv/bin/python -B` (site enabled), run from the worktree at `f5ab6e2`.

- Build: `python -B -m src.build_database --output <scratch>/tmp/disposable_canonical.db` → migration level `026_practice_runtime.sql`, 27 canonical tables, 12,288 rows, validation ok. Output path was in the scratch root only.
- Six throwaway read-only scripts opened that file with SQLite `immutable=1` and called the repository's own predicates (`is_quiz_question_eligible`, `fetch_dzi_open_questions`, `quiz_normalize_text_answer`, `dzi_question_sql`, `fetch_question_visual_assets`) so the counts are the production rules. They are not committed; their SHA-256 values and outputs are in the handoff manifest.
- Not done and out of boundary: any access to `data/questions.db` or its sidecars, any server or tunnel, any generated-vault write (the vault path passed to the quiz module pointed at a non-existent scratch path and was never created), any provider, credential, import or real migration.

Limitations. (1) Counts are a function of `data/canonical` at `f5ab6e2` and of which image files exist under the repository root; a deployed static directory was not inspected. (2) 90 minutes, the 15/10 split and the point values are repo facts, not verified against any official publication. (3) The proposed bounds, grace and table shapes are untested designs. (4) Concurrency, clock and rendering behavior described here is by reading code and by analogy with C2 and the quiz; none of it was run for C3. (5) The task-number findings of §3.3 depend on the importer's overrides being correct.
