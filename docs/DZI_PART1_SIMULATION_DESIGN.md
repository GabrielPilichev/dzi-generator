# DZI Part 1 simulation design (C3)

Status: design only; nothing implemented, migrated, activated or exposed. Authority: the 2026-11-18 update (`docs/REFACTOR_ACCOUNTS_ROADMAP.md:13`) and its C3 line. Release intent comes from §5.4 of the pinned review `REVIEW_2026-11-11_NEXT_STEPS.md` (SHA-256 `d3112d4f572f7949a9f3a59a97c23b8490f3de82ff03cace3fee993e9368d0f3`, not tracked); that text approves none of the choices below. Scope: `docs/reviews/dzi-part1-simulation-design-scope.md` (SHA-256 `ff7fc96f37ddd66b941f98a8eeedf5ddd39e0a3fef6481b1acfe1b1e38272cab`). Sources read at `f5ab6e2` on base `ffa74c9` (#374); evidence dated 2026-11-22, reproduced and extended 2026-11-23. Revision 2 (2026-11-23) applies the independent review of the exact first draft (`86e0dd5`; Appendix B). It changes this document only.

Labels. **Fact**: a tracked source, cited `path:line`. **Evidence**: counted on a disposable canonical build (Appendix A), or taken from the reviewer's logs and marked as such (Appendix B). **Proposal**: a new engineering decision, pending review. **Open**: unknown, or a decision for someone else. Unknown here and not decided: the official exam's timing, layout and marking scheme, legal basis and privacy (review D16), the activation date, the C5 public-content decision. App timing (§7.1) and activation (§8) are separate decisions: approving one arms or fixes nothing of the other.

## 1. Proposals at a glance

| # | Proposal (all pending review) | § |
|---|---|---|
| P1 | Draw the 15 multiple-choice and 10 short-answer items **by kind** from official Part 1 task links, not by task number. Compose existing eligibility rules; add only the Part 1 link, a weight rule and a C3-local **stimulus-present** layer (three reviewed question ids, 67, 131 and 202, are excluded by stable id until separately approved content repairs them). No borrowed item: a thin pool refuses the start. | 4 |
| P2 | Store attempts in two **dedicated additive runtime tables** (migration 027). Not `quiz_attempts`, not the C2 `practice_*` tables, no system assignment. | 5 |
| P3 | The plan is a flat JSON array of 25 question ids in slot order (`question_ids_json`). A **separate** `slot_format_json` column holds the slot kind and points, copied from the tracked blueprint **at start**; submit reads kind and slot points from that column and never from the current blueprint. | 5.3, 6.2, 6.4 |
| P4 | Server-owned deadline `started_at + part1_minutes x 60` (repo value 90, not externally verified), a short submit grace whose end is **stored at start** as `submit_cutoff_at`, and **lazy** expiry: no background close exists. Every state decision reads the stored cutoff; the grace constant affects only attempts started after a change. | 7 |
| P5 | Take time from the existing lifecycle clock-health layer. Every C3 path that writes calls `admit()` before its first write; read-only outcomes write nothing and do not admit; C3 never calls `check_factual` on its own rows, and a row stamped later than lifecycle `now` fails closed for that row only. | 7.4 |
| P6 | Submit is one idempotent writer transaction (the C2 pattern), inputs are bounded and a missing `Content-Length` is refused before the form is read, so overflow fails closed; no GET writes. | 6.4, 7.3 |
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
| Stimulus present (new) | the item is not on a C3-local list of reviewed question ids whose prompt refers to material that no student can see: 67, 131, 202 | §4.4 |
| Slot weight (new) | stored `fill_in_subquestions.points` sum to the slot's points (3) | §6.5 |

C3 invents no eligibility rule beyond the three marked new. The stimulus-present layer is **Proposal**: a constant of stable canonical question ids owned by the C3 service, applied when pools are composed at start (so an attempt already started keeps its items, §6.4) and lifted for an id only by a separately approved content repair. It is deliberately **not** an edit of `VISUAL_DEPENDENT_PATTERNS` (`src/dzi_open_questions.py:15`), whose readiness query the exam and DZI training-set builders share (`src/build_exam.py:65`, `src/generate_dzi_training_set.py:103`, `src/subject_assessment.py:94`) and which this PR must not change. It is kind-neutral: the list holds three short-answer ids today and no multiple-choice id (Appendix B states how far that was checked). The existing short-answer readiness rule is the stricter of the two available (the quiz-eligibility rule alone, `web/services/quiz.py:1056-1066`, would admit 11 more items whose prompts name an image and which have a usable image file), and C3 keeps the stricter one. `fetch_dzi_pool_health` (`web/services/quiz.py:2441`) is admin readiness for one source and counts short answers as "not yet supported"; it is not reused.

Source and asset rules are preserved, not redefined: a prompt that needs a visual needs a usable file (`web/services/quiz.py:988`), visuals load through the existing loader, which accepts only two image asset types under `web/static/img/questions/dzi/` (`web/services/question_visuals.py:15-16,88`), so source PDFs are never surfaced; images are served as today without a session check, which the roadmap already accepts ("DZI content gate", item 6).

### 4.2 Evidence (disposable canonical build at `f5ab6e2`)

Reproduced on 2026-11-23 on a fresh disposable build, and identical to the reviewer's independent run, except for the label result and the stimulus layer (both corrected below).

| Count | Value |
|---|---|
| DZI exams / with task links | 11 / 7 (`dzi_it_pp_2025_format`); four `modern_2023` exams have no links |
| Official-identity questions | 240 (140 multiple choice, 100 fill-in) |
| Linked to tasks 1-25, role primary | 175; linked to tasks 26-28: **0**; linked to two tasks: 0 |
| Linked and consistent (IT, DZI, year, `question.exam_id` = linked exam, `source_number` = task number, not AI) | 175 of 175 |
| Unlinked legacy rows (`jun_2022` 22, `may_2023` 21, `aug_2023` 22) | 65, of which 33 multiple choice pass eligibility |
| **Multiple-choice pool** (linked, approved, renderable) | **105** (93 at tasks 1-15, 12 at 16-18) |
| Open linked / quiz-eligible / readiness-eligible / after the weight rule | 70 / 69 / 58 / 56 |
| **Open pool after the stimulus-present layer** (reviewed ids 67, 131, 202 removed from the 56) | **53** |
| Multiple choice per source exam | 15 in each of seven |
| Open per source exam, after the weight rule (56) | 7, 7, 9, 8, 9, 7, 9 (exams 3, 5, 6, 8, 9, 10, 12) |
| Open per source exam, **final (53)** | **6, 7, 9, 7, 9, 7, 8** (same exams: `may_2022_v1`, `may_2024_v1`, `may_2025_v2`, `aug_2022_v2`, `may_2023_v2`, `aug_2023_v2`, `aug_2024_v2`) |
| Items with a resolved visual in the final pools | 7 of 105 multiple choice; 18 of 53 open (the three removed items have none: 18 of 56 before) |
| Duplicate normalized prompts inside either final pool | 0 (the 53 are a subset of a pool with 0) |
| Sub-answers per open item (final 53) | 1 (18 items), 2 (2), 3 (33); before the layer, 1 (20), 2 (2), 3 (34) |

The three removed items (reviewer-found, author-checked): question 131 (`aug_2024_v2`, task 16: "the given spreadsheet", a formula in D2), question 67 (`may_2022_v1`, task 24: "the following HTML and CSS code") and question 202 (`aug_2022_v2`, task 25: "the code provided below is incomplete"). On the disposable build each has no `image_path`, no resolved visual and no asset link of any type, and the prompt refers to material (the spreadsheet, the HTML and CSS code, the incomplete code) that is not stored anywhere. Question 202 could be guessed from its text, but its code is still absent. The existing readiness patterns miss these phrasings. 56 stays in this document only as the count **before** this layer (and as the weight-rule count that 53 is derived from); every threshold and test uses 53.

Consequences: (a) the 15 and 10 requirements are met with margin (105 and 53); (b) **no single session supplies ten usable open items** (the best has 9), so the simulation is a mix across sessions ("built from the repository's official-session items following the blueprint"), not a replay of one paper, and its text must not claim otherwise; (c) `may_2025_v2` alone gives 15 multiple choice (as `AGENTS.md` expects) but nine usable open items: it has ten imported, and one prompt names an image, so even it cannot fill ten.

### 4.3 Thin pool, no borrowed question

**Proposal.** A start needs, per blueprint slot kind, at least as many distinct eligible items as slots of that kind (15 and 10 today, read from the blueprint, not hard-coded). If either pool is short, the start returns `pool_insufficient`, creates no attempt and shows one generic Bulgarian "not enough questions yet" message with no counts and no source names. It never fills a slot from: a non-DZI or AI-generated question, a Part 2 item, an unlinked legacy item, an item on the stimulus-missing list, an item of the other kind, another subject, or a repeated question. An open attempt keeps its stored plan and slot format (§6.2). Each point is a test (§9).

### 4.4 Content gaps that remain (none addressed here)

Needing separately approved content or data work, listed for the decision owner, not fixed by this PR: (1) 65 unlinked legacy items (33 multiple choice eligible) with no reviewed Part 1 link; (2) 11 open items excluded only because the readiness rule rejects any prompt that names an image, although each has a usable image file under the repository root; (3) two open items whose stored weights sum to 2 and 4 instead of 3 (`may_2022_v1` tasks 12 and 23): `validate_short_answer` (`src/import_dzi_questions_json.py:311-334`) requires positive weights but not that they sum to the slot; (4) the four deviating sessions of §3.3; (5) no explanation column on `questions`, so reviews show the existing "no explanation" message (`web/services/quiz.py:803-860`); (6) sessions after May 2025, unchecked; (7) the seven source PDFs are asset rows whose files are not tracked (`git ls-files data/reference` is empty), which C3 does not need; (8) the three short-answer items with a missing stimulus (questions 131, 67 and 202, §4.2): their spreadsheet or code must be supplied and reviewed before the C3 list may release them, and the review in Appendix B is a manual reading, not proof that no other item lacks its stimulus.

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

**E. One table with an answers JSON column** is the smallest in table count and was considered. It is not recommended: the updater's reference gate reads every integer in a registered JSON column as a question id, and the parity check reads list items and `id`-like keys (§5.4), so an answers document with letters, points and slot numbers would be read differently by the two and would need either a bespoke walker or a flat shape that is no longer an answer document; RESTRICT keys and `CHECK`s cannot protect ids or points inside JSON; and per-question reads for C4 would parse JSON. D keeps ids in integer columns the existing gate already understands.

D is the smallest change that keeps the three existing contracts intact. The cost is one more pair of tables and a result page of its own; that is accepted.

### 5.3 Proposed shape (migration `027_dzi_part1_runtime.sql`, not written here)

`dzi_part1_attempts`:

- `id` (positive); `user_id` NOT NULL, RESTRICT to `users`; `blueprint_id` NOT NULL, RESTRICT to `dzi_blueprints` (provenance only: nothing reads the blueprint's slots after start); `seed` text.
- `question_ids_json` text: a **flat array of 25 distinct positive question ids in slot order, slot = position + 1**, and nothing else.
- `slot_format_json` text: an array of 25 objects `{"kind": "multiple_choice" | "short_answer", "points": <positive integer>}` in slot order, copied at start from the blueprint slots that start validated, and never rewritten. It holds no question id, so it is registered in **neither** `JSON_ID_COLUMNS` nor the parity check (§5.4); `question_ids_json` stays flat.
- `max_points_planned` integer (the sum of the snapshot points: 45 today); `duration_seconds` integer > 0.
- `started_at`, `deadline_at`, `submit_cutoff_at`: NOT NULL text in the lifecycle format (`web/services/lifecycle.py:180`). Written once, in the start transaction, from the one `now` of that transaction: `deadline_at = started_at + duration_seconds`, `submit_cutoff_at = deadline_at + SUBMIT_GRACE_SECONDS`. No later statement updates them. Every state decision reads the stored `submit_cutoff_at`; the constant is read only to compute it for a new attempt, so changing it cannot revive or kill a row already stored (§7.2).
- Nullable `submitted_at`, `expired_at`, `abandoned_at` (at most one set); `score_points`, `score_max` set iff submitted, `0 <= points <= max`, `max > 0`.

A partial unique index on `(user_id)` while all three terminal timestamps are null gives one open attempt per account; an index on `(user_id, started_at)` serves later reads.

Row-local `CHECK`s (**Proposal**, in the 026 style: `web/migrations/026_practice_runtime.sql:32-95`): types and ranges; the six timestamp columns match the fixed-width `YYYY-MM-DD HH:MM:SS` shape (026 only tests `typeof`, but the orderings below compare text, which equals time order only for that shape); `started_at < deadline_at AND deadline_at < submit_cutoff_at`; at most one terminal timestamp; if `submitted_at` is set, `started_at <= submitted_at < submit_cutoff_at`; if `expired_at` is set, `expired_at >= submit_cutoff_at`. The last two hold because the service refuses a submit before `started_at` or at or after the cutoff, and marks expiry only at or after the cutoff (§6.4, §7.2). `abandoned_at` has **no** ordering `CHECK`: a row stamped later than lifecycle `now` is marked abandoned at a `now` earlier than its `started_at` (§7.4). What the schema does **not** implement, and the service and its tests must: that `deadline_at` and the cutoff follow the arithmetic above, the shape of both JSON columns, the slot count, who may start, read or submit, eligibility, scoring and state transitions.

`dzi_part1_answers`: primary key `(attempt_id, slot_number, subquestion_number)`; `attempt_id` RESTRICT to the attempt; `question_id` RESTRICT to `questions`; `kind` in (`multiple_choice`, `short_answer`); `subquestion_number` 0 for multiple choice, 1 or more for short answer; `chosen_letter` (multiple choice only, the stored option key); `raw_answer` (short answer only, text); `is_correct` 0 or 1; `points_possible`, `points_awarded` whole numbers with awarded 0 or possible; `grader_version` text. Row-local `CHECK`s only (kind-consistent columns, types, ranges), as in 026. **`raw_answer` carries no length `CHECK`**: its bound is enforced by the service (§6.4), so changing the bound later needs no table rebuild. No question text, no accepted answer, no blob, no name, no timestamp from a client.

**Uniqueness, stated accurately.** The primary key makes each `(slot, part)` unique within an attempt. A second unique key `(attempt_id, question_id, subquestion_number)` prevents only a duplicate of the **same question part**. It does **not** keep a question out of two slots: the table accepts `(slot 16, Q, part 1)` together with `(slot 17, Q, part 2)`. That guarantee is a **service invariant**: the stored plan holds 25 distinct ids, and every row the service writes has `question_id` equal to the plan entry for its `slot_number`, `kind` and `points_possible` consistent with that slot's `slot_format_json` entry, and a `subquestion_number` that is a stored part of that question (0 for multiple choice). The tests of §9 pin both halves: a negative-control insert shows the table accepts the pair above, so nobody mistakes the key for the guarantee, and a service test shows a submit never writes such a pair.

Why these columns: stable references (`question_id`, `blueprint_id`) survive a rebuilt database. Answer rows exist only after submit, so per-row points cannot protect an attempt in progress; the start therefore stores everything that later decides its fate: `duration_seconds`, `deadline_at`, `submit_cutoff_at`, `max_points_planned` and `slot_format_json`. Without them a deploy that changes the grace constant, or a re-seed of the blueprint (`src/seed_dzi_blueprint.py:86,135` update blueprint and slot rows in place with `ON CONFLICT ... DO UPDATE`), would change the state or the grading of an attempt in flight; the reviewer's prototype showed the same row at the same instant `lapsed` with a 60 s grace and `grace` with 120 s (Appendix B). No correct answer is stored on an attempt (the review derives it from current content).

### 5.4 Classification, updater, backup

- Classify both tables `RUNTIME` in `src/canonical_tables.py:35-82` and regenerate the manifest's classification and `migration_level` (`data/canonical/manifest.json`; `tests/test_canonical_content.py:425-428`). A fresh build leaves them empty (`src/build_database.py:11-13`).
- The updater preserves every non-canonical table by digest (`src/update_canonical_content.py:641-654`) and validates question references. Add `("dzi_part1_attempts", "question_ids_json")` to `JSON_ID_COLUMNS` and `("dzi_part1_answers", "question_id")` to `INTEGER_ID_COLUMNS` (`:129-132`), and the matching checks to `runtime_reference_problems` (`src/canonical_parity.py:155-181`). `slot_format_json` is added to neither: it holds no question id, and its `points` integers would otherwise be read as ids. **The two extractors differ.** The updater reads every integer inside a registered JSON column, nested ones included (`update_canonical_content.py:_json_ids`, `:661`); parity reads list items and the values of keys named `question_id`, `question_ids` or `id`, plus anything nested in a list or object (`canonical_parity.py:_ids_in`, `:133`). On `[{"slot": 16, "question_id": 5}]` the updater reads `[5, 16]` and parity `[5]` (author-reproduced 2026-11-23; the reviewer found it first). A **flat** array of ids is read identically by both, which is why `question_ids_json` must hold nothing but ids: a slot number inside it would count as an unresolved reference for the updater and be missed by parity.
- The updater is additive in v1: value changes and removals are unsupported and unrecognized divergence is refused (`docs/CANONICAL_CONTENT_UPDATE_DESIGN.md:41`). C3 therefore needs no updater change beyond the references above, and the current-content caveat (§6.6) is a guard, not an expected path.
- Backups: both tables live in the one SQLite file; back up only with `python3 -m src.backup_sqlite` (`AGENTS.md`). A snapshot restore drops attempts made after it (`docs/DEPLOYMENT.md` §21). No blob is stored.

### 5.5 Migration and rollback

Additive, no rewrite, no backfill, no seed row; code that predates it never names the tables. The same precedent applies as for 026 (`git show --stat 30e6b9e`: migration, classification, manifest, parity, updater, tests in one PR). **Rollback**: migrations are forward-only and an older build refuses a database with migrations it does not ship (`web/production.py:563-570`, `docs/DEPLOYMENT.md:1195-1203`), so rolling the code back is not an off-switch; the off-switch is the default-off build constant (§8). RESTRICT keys protect referenced accounts, questions and blueprints from silent deletion and state no retention rule; retention is out of scope.

### 5.6 What C3.1 must settle, and what can wait

C3.1 writes the tables and nothing else (no service, route, template or activation). Everything that changes the table shape is decided here, because once 027 is applied a later change to a populated runtime table is at best an additive column and otherwise a rebuild. **Proposal**, all pending review:

| Decision | Fixes the 027 shape? | Status |
|---|---|---|
| `submit_cutoff_at` stored, written once at start | Yes | In this design (§5.3, §7.2) |
| `slot_format_json` stored at start, outside the id columns | Yes | In this design (§5.3, §5.4) |
| Service-level uniqueness (question equals plan entry; distinct plan), key described accurately, negative control | Yes (what the key is, and is not, claimed to do) | In this design (§5.3, §9) |
| No length `CHECK` on `raw_answer` | Yes | In this design (§5.3) |
| Timestamp shape and ordering `CHECK`s; no ordering `CHECK` on `abandoned_at` | Yes | Proposal (§5.3); may be tightened in the C3.1 review |
| One-open-attempt partial index | Yes | Carried from the C2 shape |
| Value of the grace (60 s) and of the duration (`part1_minutes x 60`) | No: constants and per-attempt snapshots | Wait: C3.2 review (§7.1) |
| Input bounds (64 KiB, 200 characters), `Content-Length` rule | No: service | Wait: C3.3 (§6.4) |
| Pool by kind, mixed sessions, weight rule, stimulus list | No: service | Wait: C3.2 (§4) |
| Clock admission and future-row rule | No: service | Wait: C3.2 (§7.4) |
| Client countdown, draft, label guard, copy, URL prefix | No | Wait: C3.5 (§6.8) |
| Activation, rate cap, official wording | No | Wait: activation (§8) |

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

One writer transaction through the lifecycle layer (§7.4), in this order. **Open-attempt handling comes before any blueprint validation**, so a resume never depends on the current blueprint and an open attempt keeps its stored plan and slot format (§4.3).

1. Active account, else `account_unavailable` (read-only outcome).
2. The account's open attempt (no terminal timestamp), if any. Parse its stored plan and slot format; a malformed or inconsistent pair fails closed as C2's `PracticePlanError` does, writing nothing. Then decide from the stored columns and lifecycle `now` only (§7.2):
   - `now < started_at` (the **future-start anomaly**, §7.4): call `admit()`, mark the row `abandoned_at` (guarded on open) and go on to step 3.
   - `started_at <= now < deadline_at` and the row presents at least one item: **resume**, writing nothing and not admitting.
   - `deadline_at <= now < submit_cutoff_at` (grace): return the same attempt, writing nothing; its page says time is up and offers no start action (§6.3).
   - `now >= submit_cutoff_at`: `admit()`, mark `expired_at` (lazy expiry, §7.2) and go on to step 3.
   - Open but presenting nothing renderable: `admit()`, mark `abandoned_at` (the C2 "stale" rule) and go on to step 3.
3. **Only now, and only to create a new attempt**, read the blueprint (`is_active`, the Part 1 slots) and validate it: exactly the expected contiguous Part 1 slots, kinds only in {multiple choice, short answer}, positive points, `part1_minutes` positive and bounded, no unsupported slot constraint (`topic_id`, `section_id`, `required_asset_type`, `topic_area`). Otherwise `blueprint_unavailable`: no attempt is created (fail closed, not silently ignored); a marking made in step 2 is still committed, because it is true whatever the blueprint says.
4. Build the pools (§4.1). If a kind is short, `pool_insufficient`: no attempt is created; a marking made in step 2 is still committed.
5. Otherwise, after `admit()`, insert: `seed = secrets.token_hex(16)`; sort each pool by id; `random.Random(seed).sample` draws the items of each kind without replacement; items fill the slots of their kind in slot order. From the one `now` of the transaction: `started_at`, `deadline_at = started_at + duration`, `submit_cutoff_at = deadline_at + SUBMIT_GRACE_SECONDS`; `duration_seconds`, `max_points_planned` and `slot_format_json` are snapshotted from the blueprint validated in step 3.

Deterministic: given the stored seed and the same pools the draw and the option order (`quiz_shuffle_options`, `web/services/quiz.py:1242`, seeded by the attempt seed) are reproducible, which is how tests pin them; the stored plan, never a recomputation, is authoritative. The partial unique index makes concurrent starts of one account leave one open attempt; a second `POST` returns it. During grace a start returns the same attempt so that an in-flight auto-submit is never refused by the start that raced it; only at or after the **stored** cutoff does a start expire and replace it.

### 6.3 Attempt page (read-only)

One read snapshot (`lifecycle.read_snapshot`, `web/services/lifecycle.py:887`) on a `query_only` connection, as the C2 confirmation does (`web/blueprints/practice.py:_confirm`). It writes nothing, expiry and clock bookkeeping included. It shows the stored questions in slot order with the **slot number and points** per item (points from `slot_format_json`), options in seeded order **without** correct markers (`quiz_load_questions(..., include_correct=False)`, `web/services/quiz.py:1254`), the visuals the existing loader attaches, and the remaining seconds computed from `deadline_at` and the lifecycle read clock. If the clock is unhealthy the page shows persisted state and says submissions are unavailable (the `submissions_unavailable` pattern, `web/blueprints/quiz.py:257`).

**Three different moments, three different pages (Proposal).** The wording below is a requirement on the Bulgarian copy, not the copy:

| Stored moment | Page |
|---|---|
| Before `deadline_at` | The form and a countdown to the **deadline** only. The grace and the cutoff are never shown. |
| `deadline_at` up to the stored `submit_cutoff_at` (**grace**) | No form. The page says time is up and submission is closing; it offers **no** start action and no countdown. Grace is request latency for an auto-submit already in flight, not working time. |
| At or after the **cutoff** (unmarked or `expired_at` set) | A notice that time ran out and answers cannot be saved, with a **new-start** action: offered only here and on the future-start notice below. A start at that moment expires the old row and creates a new attempt (§6.2). |

An attempt stamped later than lifecycle `now` (§7.4) shows a notice that it cannot be continued, with the same new-start action, and no remaining time. The intro page discloses no attempt state (§6.1) and keeps its generic start button; a start pressed during grace returns the grace page itself, which offers no action. (The first draft offered a new start as soon as the deadline passed, although a start during grace returns the same attempt, so for up to the grace length the offered action looped to the same page.)

**Short-answer labels.** The existing loader selects only `id, subquestion_number` for short-answer parts (`web/services/quiz.py:1276`), so a C3 loader must also select `subquestion_text` (a label such as "(1) София"). **Evidence** (reproduced 2026-11-23, Appendix A): across the 132 sub-answers of the 58 readiness-eligible items, **0** labels equal an accepted answer and **1** contains one: question 834, sub-answer 1, whose label is identical to the official prompt, so a student learns nothing the prompt does not already show. The first draft of this document claimed "no label contains an accepted answer"; that check was **vacuous**, because its query aliased the columns and the accepted-answer reader saw an empty answer set for all 132 rows. Label shapes also matter: of the 126 labels in the 56-item pool, 19 equal the prompt and 23 sit inside it (121 labels in the final 53: 17 and 23), so showing every label would repeat question text. **Rendering rule (Proposal): do not show a label that equals, or is contained in, the prompt** (compared after the existing normalizer, `quiz_normalize_text_answer`, `web/services/quiz.py:369`); show the rest. In the final 53-item pool that shows 81 of 121 labels and hides 40, and no shown label contains an accepted answer that the prompt does not already contain. A test keeps that true for the active pool (§9).

### 6.4 Submit and bounded input

Fields: `q_<question id>` carrying the stored option key (never a display letter) and `a_<question id>_<n>` carrying text for sub-answer `n`; both are matched by a fixed pattern and only for ids in the stored plan, and an `a_<id>_<n>` field is accepted **only when `n` is a stored part number of that planned question**. Everything else is ignored: the matcher `grade_quiz_text_answers` (`web/services/quiz.py:659`) turns any submitted part number into a graded row, so on the prototype a submitted part 9 of a one-part item became an extra row (Appendix B; reproduced 2026-11-23). A field sent twice counts as unanswered (as `web/blueprints/practice.py:99`).

**Proposed bounds** (pending review; enforced by the service, never by a table `CHECK`):

- **Body.** The application-wide limit is about 100 MiB (`web/app.py:175`, `src/practical_uploads.py:24-29`) and the existing open-answer field has no length limit (`web/templates/quiz_attempt.html:160-168`), so the route sets a 64 KiB limit of its own. **Overflow fails closed (Proposal):** a request without a usable `Content-Length` is refused with 411 **before the form is read**, and a declared length over 64 KiB is refused with 413 before the form is read. The alternative of reading at most the limit plus one byte and refusing on overflow would need a hand-written form parser and is not proposed. The reason is measured: with a per-request `max_content_length` of 65,536, Werkzeug 3.1.9 refuses a declared oversize body with 413, but a terminated chunked body of 70,000 bytes answers 200 with the value **silently cut to 65,534** (reviewer-found; author-reproduced 2026-11-23, Appendix A). A cut value would be graded and stored, contradicting "nothing written". The tracked proxy configuration buffers requests (`deploy/nginx/learnpilot.conf:86`, `proxy_request_buffering on`), but that is configuration, not something the service may rely on, and the deployed proxy was not inspected.
- **Text.** At most 200 characters per answer (Python code points), `maxlength` in the markup, no NUL, at most one value per field; a value over the bound or containing NUL refuses the whole submit with 400 and nothing is written. **Evidence**: the longest individual accepted answer in the pool is 73 characters; a full attempt has at most 45 answer fields (15 multiple choice and 30 short-answer parts, the ten largest items having three parts each). A browser that honours `maxlength` cannot exceed **54,509 bytes** (45 fields, 200 UTF-16 units of a three-byte character each in the 30 text fields, four-digit ids; author-reproduced model, 2026-11-23), which is under the 65,536 limit. A hand-crafted body of 200 four-byte characters per text field reaches 72,509 bytes, over the limit, and is refused. No honest submission is refused by the limit.
- Nothing from the client names a role, owner, plan, seed, score, timestamp or deadline.

Single transaction, idempotent, mirroring `quiz_submit_attempt` (`web/services/quiz.py:2298-2401`) and `submit_practice` (`web/services/practice.py:771-817`). A path that writes calls `admit()` first; a path that does not write ends with `discard()` and does not admit (§7.4).

1. Account, then the attempt by `(id, user_id)`; none, `not_found`. Read-only outcome.
2. `submitted_at` set: `already_submitted`, **no write of any kind** (the lifecycle `discard()`), so a retry leaves every table, the clock row included, unchanged.
3. `abandoned_at` set: `abandoned`, refused, nothing written (an old tab can never grade a replaced attempt). `expired_at` set: `expired`, nothing written.
4. Unmarked, `now < started_at` (future-start anomaly, §7.4): refused, **nothing graded and nothing written**; the row is left as it is and the next start marks it `abandoned_at`.
5. Unmarked and `now >= submit_cutoff_at` (the **stored** cutoff): `expired`; `admit()`, mark `expired_at` (guarded on open) and commit; no answer rows. Otherwise continue (before the deadline, or in grace).
6. Parse the plan and the slot format; they must agree (25 and 25, kinds and points as the start stored them), else fail closed with nothing written. The **kind and slot points of every item come from `slot_format_json`, never from the current blueprint**: a re-seed after start changes neither the grading nor the maximum of this attempt. Presentable items = stored ids that still satisfy the per-item predicate (the item still renders or grades as its stored kind; for short answer, its current part weights still sum to the slot's stored points). This is not a rebuild of whole pools, so a later addition to the stimulus list (§4.1) does not drop an item the student already saw. None presentable: `stale`, nothing written.
7. No usable answer: `empty`, nothing stored, the attempt stays open (§6.7). Read-only outcome.
8. `admit()`; grade (§6.5), insert one row per presentable multiple-choice slot and per short-answer part (each row: `question_id` equal to the plan entry for its slot, kind and `points_possible` from the slot format), then `UPDATE ... WHERE open` with `submitted_at = now` and verify the row count. Any inconsistency raises and rolls back; nothing partial persists.

### 6.5 Grading and scoring

- **Multiple choice.** Chosen key must equal a current stored option key; verdict against the one correct key (`quiz_correct_mc_letters_by_id`, `web/services/quiz.py:1322`; C2 `_grade`, `web/services/practice.py:735`). The slot's points from `slot_format_json` (1 today).
- **Short answer.** Reuse the pure matcher `grade_quiz_text_answers` (`web/services/quiz.py:659`) with accepted answers from `correct_answer` and `answer_alternatives` (`:397`) and the existing ordered or order-independent mode (`:406`; 54 ordered and 4 order-independent among the 58 readiness-eligible items). Limits to state to students, not to hide: exact match after NFKC, whitespace collapse, quote folding and `casefold` (`:369`); no typo, synonym, number-format, unit or word-order tolerance; no partial credit inside one sub-answer; the grader cannot judge meaning. A correct answer phrased differently scores zero.
- **Weights.** The existing grader awards 1 point per sub-answer (`:638-656`), which would cap a three-point, one-part item at 1. C3 instead awards the stored `fill_in_subquestions.points` of each correct part. **Evidence**: 56 of 58 ready items sum to 3 (34 as 1+1+1, 20 as a single part of 3, 2 as 1+2); the other two are 1+1 and 1+1+1+1. The 4 order-independent items all have equal weights, which makes matching weights to parts unambiguous. The weight rule of §4.1 (sum must equal the slot's points; order-independent items need equal weights) keeps awards whole and slot-true; the two non-conforming items stay out until reviewed. These shapes are counted over the 58 readiness-eligible items, before the stimulus layer; the three items that layer removes (§4.2) are one 1+1+1 item and two single-part items of 3.
- **Persisted verdicts.** Each row stores verdict, possible and awarded points and `grader_version`; the score is the sum of awarded points of presentable rows and the maximum the sum of possible points. Neither is recomputed later.
- This is **mixed effective scoring**: unlike assignment attempts, where open answers are shown as an informational subtotal beside the stored multiple-choice score (`web/services/quiz.py:876-912`), the simulation score includes auto-graded short answers.

### 6.6 Result, saved score and the current-content caveat

Only a submitted attempt has a result; others give the generic 404. It shows `score_points / score_max` from the stored row, the multiple-choice and short-answer subtotals from the stored answer rows, and a review per slot. Same rule as C2 (`docs/STUDENT_PRACTICE_DESIGN.md` §4): stored ids, verdicts and points are never recomputed; prompts, options, correct answers, accepted answers and explanations are read from **current** content, so after a content change a review can disagree with the saved verdict; items no longer presentable are omitted and counted, and an empty review still returns the saved score. Correct answers appear only here, after submission, never in an attempt page or answer row. Reuse: the multiple-choice review partial (`web/templates/_multiple_choice_result_review.html`, which numbers by `loop.index` and needs a slot-number parameter, so a render-equality test pins the old result page) and the explanation fetchers; the short-answer review is a new block (the assignment page's block carries teacher-override state, `web/services/quiz.py:1971-2012`). The page must state that the short-answer check is automatic and strict, and that the 90 minutes are the simulation's limit.

### 6.7 Incomplete, timed-out, maximum, later eligibility

- **Maximum.** Part 1 only: `max_points_planned` is the sum of the snapshot points (45 for a full plan); `score_max` is the sum over presentable rows, so it equals 45 unless an item became unpresentable. Never the blueprint's 100.
- **Incomplete.** Unanswered items score 0 and are stored; a submission needs at least one usable answer, meaning a stored option key of a plan question or a non-blank text for a plan sub-answer of a stored part (an all-empty submit is refused, writes nothing and stays open).
- **Timed out.** An attempt not submitted before its stored cutoff is `expired`: no answers are stored, no score, no result, no count. The page says time ran out. **Open**: a late submit is lost; the alternatives (accept it unofficially, or save drafts on the server) are listed in §11. The only mitigation proposed is the browser-local draft and the elapsed-time countdown of §6.8, which lowers the chance of a late auto-submit but cannot rescue one the server refuses.
- **Later C4 (separate scope).** Stored data lets C4 take only attempts with `submitted_at` set and `score_max = max_points_planned` as a labelled "DZI Part 1 simulation" source, with `grader_version` and the short-answer caveat, never merged into C2 or assignment progress. Nothing here defines that screen or any rollup.

### 6.8 Client countdown, local draft and submit (Proposal; deferred to C3.5)

Nothing here is written by this PR or by C3.1-C3.4; the late-submit rule of §6.7 needs it defined now. The server decides every outcome (§7.3); the client only reduces the chance of a late submit and of lost work. The two existing scripts are a **model only** and cannot be reused unchanged:

- The assignment countdown (`web/templates/quiz_attempt.html:209-267`) decrements a counter once per fired `setTimeout` (`:264-265`), so a callback delayed by a suspended device is counted as one second. Run against a virtual clock, a 10-minute suspend submits it at 1,199 s of a 600 s deadline with a 660 s cutoff, where the server refuses it and every answer is lost (reviewer's model, Appendix B).
- The draft script (`web/static/js/quiz-draft-autosave.js`) keys its storage `learnpilot:quiz-draft:<attemptId>` (`:20`), which would collide with an assignment attempt of the same number; it recognises only `q_` and `open_q_` names (`:33`, `:99`), so it ignores `a_<id>_<n>`; and although its comment says it clears on the result page (`:138-141`), no tracked template or script calls its `clear` (searched `web/` and `tests/`).

The C3 behaviour, a new client file and template block with their own names:

1. **Countdown from elapsed real time.** The attempt page carries the server's remaining seconds to the **deadline** (computed at render from `deadline_at` and the lifecycle read clock). The client records the load instant on both the wall clock and the monotonic clock and shows `server remaining - elapsed`, taking the larger of the two elapsed readings, so that neither a suspend the monotonic clock may not count (browser behaviour across a suspend was not measured here; the manual acceptance records it per browser) nor a wall clock stepped backwards can give back time. Timer callbacks only refresh the display; none is counted. It recomputes on every tick, on `visibilitychange` to visible and on `pageshow`. The display is advisory: the server owns the deadline. Trade-off for review: a student who steps the device's wall clock forward ends the countdown early and submits early; that is accepted, because the alternative (trusting the monotonic clock alone) can lose every answer after a suspend.
2. **Submit once at or past zero.** At or past zero the client submits the form **once** (a flag), by the same request as a manual submit. It sends no reason, time or flag, because the server accepts nothing a client claims (§6.4). If the page wakes already past the cutoff it still submits once, because only the server's clock decides, but it **does not claim the answers were saved**: it shows only a neutral "submitting" message and leaves every statement of success to the server's result page. This is a convenience of an open page, **not a background close**: a closed tab or a suspended device submits nothing, and the row then simply lapses and is expired lazily (§7.2).
3. **Local draft, C3-specific.** Storage key `learnpilot:dzi-part1-draft:<scope>`, where the server renders `<scope>` as `<attempt id>:<owner user id>:<started_at>` on the attempt page. The draft covers the C3 field names only: radios named `q_<id>` and text inputs named `a_<id>_<n>`, restored only into a control with the same name (and, for a radio, the same option key). The stored payload repeats the scope; a draft whose scope differs from the page's is **never restored** and is deleted, so a draft cannot be reused across owners, nor across a restored database that reissues an attempt number. Saves are best-effort and silent on a storage error.
4. **The draft survives a refused submit and is cleared only on a terminal page.** It is not cleared by submit, so a refused (400, 411, 413), empty or redirected submit loses nothing. It is cleared when the **result page** of that attempt loads and when the **expired or abandoned notice** of that attempt loads (both render a clearing call with the same scope). It is not cleared by a sign-out, which the client does not see; on a shared device an unfinished draft stays in the browser profile until one of the two pages loads or browser data is cleared, a limitation the approved copy must state.
5. **Without JavaScript** there is no countdown, no auto-submit and no draft: the student must submit manually before the deadline, and the page says so (the server still decides).

Tests and acceptance (no JavaScript test runner exists, and none is added: `AGENTS.md` allows no npm). Server-rendered markup tests assert the data attributes, the scope string, the field names and that a terminal page carries the clearing call, in the style of `tests/test_quiz_attempt_render.py:2174-2182`. Manual browser acceptance, recorded in the C3.5 PR, covers each case: a normal countdown to zero and one auto-submit; a laptop suspended across the deadline and across the cutoff; a hidden tab across the deadline; a refused and a redirected submit keeping the draft; the draft cleared on the result and on the expired notice; a draft of another attempt or owner not restored; JavaScript disabled.

## 7. Time

### 7.1 App timing is a proposal

Official timing is not established by tracked evidence (§3.2). **Proposal**: `duration_seconds = blueprint.part1_minutes x 60` at start (5400 today) and a submit grace `SUBMIT_GRACE_SECONDS = 60`, both app timing, both pending review. The duration is stored per attempt as `duration_seconds`; the grace is stored as the **cutoff** `submit_cutoff_at = deadline_at + SUBMIT_GRACE_SECONDS`, computed once at start (§5.3). The constants are read only when a new attempt is created, so **changing either affects only attempts started afterwards**. The grace only absorbs request latency of an auto-submit at zero; it is not extra working time and is not shown as such (§6.3). The existing assignment grace is 15 minutes for a different purpose (`web/services/lifecycle.py:50`). These are timing proposals: approving them is not an activation (§8) and says nothing about the official exam.

### 7.2 States and lazy expiry

State is a pure function of the stored row and lifecycle `now`, reading only stored columns (never the constants):

| State | Stored condition |
|---|---|
| **submitted** | `submitted_at` set |
| **abandoned** | `abandoned_at` set |
| **expired** | `expired_at` set |
| **future-start** (anomaly, §7.4) | no terminal timestamp and `now < started_at` |
| **open** | no terminal timestamp and `started_at <= now < deadline_at` |
| **grace** | no terminal timestamp and `deadline_at <= now < submit_cutoff_at` |
| **lapsed** | no terminal timestamp and `now >= submit_cutoff_at` |

Every upper bound is exclusive: a submit at the exact `deadline_at` is in grace, and a submit at the exact `submit_cutoff_at` is refused, as the lifecycle cutoff is exclusive (`web/services/quiz.py:2318`). Test: the same stored row at the same `now` has the same state whatever `SUBMIT_GRACE_SECONDS` is (the prototype showed the first draft's derived cutoff give `lapsed` at 60 s and `grace` at 120 s; Appendix B).

**There is no background close.** No job, timer thread, cron or startup hook exists or is proposed. "Lapsed" is an interpretation every reader applies; the row gets `expired_at` only inside a write transaction of the owner's own `POST` (start or submit), and then `expired_at = now >= submit_cutoff_at`, which is what the row-local `CHECK` of §5.3 states. Consequences, each a future acceptance test (§9): an untouched lapsed row stays "open" in storage and still occupies the account's single open slot until that account's next start or submit; no read changes it; any aggregate must test stored `submitted_at`, never derived state; a periodic cleanup, if wanted, is a separate scope with its own approval. The terminal timestamps are coherent by construction: `submitted_at` lies in `[started_at, submit_cutoff_at)`, `expired_at` is at or after the cutoff, and `abandoned_at` carries no ordering against `started_at` because the future-start anomaly stamps it at a `now` earlier than that.

### 7.3 Races, refresh, old tabs

| Situation | Result |
|---|---|
| Submit before the deadline | Graded and stored |
| Submit at or after the deadline, before the stored cutoff | Graded and stored (grace) |
| Submit at or after the stored cutoff | `expired`, nothing graded; row marked once; retry writes nothing |
| Double submit, or two tabs | One answer set; the second finds `submitted` and writes nothing |
| Submit racing a start of the same account | The writer lock orders them; a start cannot abandon an unexpired, presentable attempt, so a submit never loses to it; a lapsed attempt is expired first and the late submit is refused |
| Old tab after a new attempt | Its id differs; it can only reach its own row, which is submitted, expired or abandoned: no write, no effect on the new attempt |
| Refresh | Same stored plan, slot format, seed, order and `deadline_at`; the remaining seconds are recomputed, never reset; nothing written |
| Client clock wrong or JS off | The countdown is display only; the server decides from its own clock; without JS the user submits manually (§6.8) |
| Device suspended, or tab hidden, across the deadline | On wake the client recomputes from elapsed real time and submits once at or past zero (§6.8). Accepted if the stored cutoff has not passed; otherwise refused as `expired` and the answers are lost, as for any late submit. The client never claims "saved" |
| Session lost during the attempt (expired, signed out elsewhere) | The submit `POST` is answered by a redirect to `/login?next=<path>` (`web/app.py:529`, `docs/AUTH_ROUTE_CONTRACT.md:55-57`) and the form body is dropped; nothing is written. The answers survive only in the browser-local draft (§6.8). The deadline does not pause. Session lifetime is absolute: 30 days for student and teacher, 12 hours for admin (`web/services/accounts.py:29-33`), so loss mid-attempt needs expiry at that moment or a sign-out. After signing in, the attempt page restores the draft if time remains, and clears it on the expired notice |
| Stored row stamped later than lifecycle `now` | Fail closed for that row only: submit refused with nothing graded; the next start marks it `abandoned_at` (§7.4) |
| Body over the limit, chunked or declared; no `Content-Length` | Refused before the form is read (411 or 413); nothing graded, nothing written; the draft is kept (§6.4, §6.8) |
| Constant `SUBMIT_GRACE_SECONDS` changed in a deploy | Rows already stored keep their stored cutoff and state; only new attempts use the new value |
| Request body from a client claiming a time | Ignored |

### 7.4 Clock failure

**Proposal**: take time from `web/services/lifecycle.py` as the quiz does, with an explicit contract for what C3 calls and what it does not.

**What the layer gives.** Every C3 `POST` runs in `write_transaction` (`web/services/lifecycle.py:1099`), which takes the writer lock and refuses a regressed, stepped, unsynchronised or suspect clock with `LifecycleUnavailable` before the first statement; the refusal is answered as 503 (`web/app.py:578`). A clock fault stores only the clock row's own fault record (`_record_clock_fault`, `:909`) and no C3 row. Reads use `read_clock` and `read_snapshot` (`:875`, `:887`) and write nothing. C2 uses the raw UTC clock because it is untimed (`web/services/practice.py:525`); a binding deadline is the reason C3 should not.

**1. Admit on every writing path, and only there.** `write_transaction` alone does not raise the high-water mark or write the boot anchor: that is `admit()` (`:975`), and the layer's own contract is that nothing is written before it (`:935-941`). Without it the prototype accepted a clock moved back one hour and showed 9,000 s remaining on a 5,400 s attempt; with it the same step was refused as `clock_regression` (reviewer-found, Appendix B). So:

| Path | `admit()` | Ends |
|---|---|---|
| Start creates an attempt | before the insert | commit |
| Start marks `expired_at` or `abandoned_at` (§6.2 step 2) | before the marking | commit, even if the start then returns `blueprint_unavailable` or `pool_insufficient` |
| Submit stores answers | before the first answer row | commit |
| Submit marks `expired_at` (§6.4 step 5) | before the marking | commit |
| Read-only outcomes: start resumes, `account_unavailable`, grace return, or refuses with nothing marked; submit `not_found`, `already_submitted`, `abandoned`, `expired` already marked, future-start refusal, `empty`, `stale`, a malformed plan | **never** | `discard()`: nothing is written, the high-water mark included |
| `GET` of the intro, an attempt or a result | never: no writer transaction. The attempt and result pages read inside a read snapshot (a read transaction only, §6.3); the intro needs none | nothing written; the snapshot is released |

A read-only outcome still runs the clock check at the start of its transaction, so an unusable clock answers 503 even for a retry that would have written nothing.

**2. No `check_factual` on C3 rows.** `check_factual` (`:962`) records a clock fault and raises when any factual timestamp is later than `now`. Applied to a C3 row it would put the one shared lifecycle clock into `suspect` and block every assignment write because of a single student's row (prototype, reviewer-found, Appendix B). C3 never calls it, on any of its own columns.

**3. A future-stamped row fails closed for that row only.** `clock-recover` (`src/manage_lifecycle.py:515`) and `clock-inspect` (`:492`) read `quiz_assignments` and `quiz_attempts` (`_factual_values`, `:346-358`; `clock_lists`, `:363-402`); they **neither list nor hold C3 rows**, and recovery sets the high-water mark to the verified time, which can be earlier than a row stamped during a forward clock fault. Left alone, such a row would gain the whole gap as working time (the prototype showed 26,399 s remaining on a 5,400 s attempt). The rule (§6.2, §6.4, §7.2): `started_at > now` makes the row **future-start**: the page shows persisted state and a notice that it cannot be continued, a submit is refused with nothing graded and nothing written, and the next start marks it `abandoned_at`. That marking is a writing path, so it **does call `admit()`**: the high-water mark may advance as in any normal write (it never moves backwards, and `now` is never earlier than it). It never calls `check_factual` and never marks the shared clock `suspect`. A replacement attempt is created only if the blueprint validates and both pools pass (§6.2 steps 3-4); otherwise the marking is committed and the start returns `blueprint_unavailable` or `pool_insufficient`. It is a row-local judgement; the clock state is not otherwise touched. Extending `manage_lifecycle` to list C3 rows would be a separate, separately approved scope; this design does not need it.

Trade-offs, for review: (a) a clock fault blocks C3 writes together with assignment writes until an operator recovers the clock (`docs/DEPLOYMENT.md` clock commands); (b) the outage does not pause the deadline, so a student refused during a fault may find the attempt expired afterwards; (c) a student whose attempt is stamped by a faulty forward clock loses it at the next start (rule 3) rather than gaining unearned time; (d) tests use the existing `lifecycle_time` helper (`tests/support.py:140`), not hand-patched clocks.

## 8. Exposure, ownership, gate

- **Default off.** A build constant, separate from C2's (`web/services/practice.py:299`), registers the blueprint in `web/app.py` only when true, as `web/app.py:647-650` does; no setting or environment variable overrides it; tests arm it only in a separate interpreter before importing the app (`tests/test_practice_routes.py:99-125`). While off, no `/simulation` rule exists and the URL map is as before. Arming and any link are a separate, explicitly approved launch change under the pilot's own activation prerequisites (roadmap, R2). This PR sets nothing. The timing proposals of §7.1 and the storage shape of §5.3 are scoped separately from activation: approving either arms nothing, and the 90-minute, 60-second and 64 KiB values are engineering proposals that an activation decision may still change before any student sees them.
- **No admin exposure.** The simulation adds no link, redirect or data path to `/dzi`, `/dzi/source/<slug>`, `/teacher/dzi-training` or any teacher page; they stay `ADMIN` and unchanged. The student pages show no import counts, pool health, exam source slug, task number or PDF; error pages are generic and name no stored state. Students get no admin route, teacher results never list simulation attempts, and no teacher or admin can read another account's attempt.
- **No assignment machinery.** No system teacher, no `quiz_assignments` row, no token, no `quiz_attempts` row, no vault note, no quota use.
- **Ownership.** `user_id` comes only from the validated principal; the owner is in the same `WHERE` as the id.
- **Content gate.** C3 renders official DZI items only to signed-in accounts, consistent with the roadmap's DZI-gate item 4 (`docs/REFACTOR_ACCOUNTS_ROADMAP.md`, "DZI content gate"). The anonymous-surface filtering and cache tests are C5's and unchanged.
- **Unchanged prerequisites, owned elsewhere:** migration applied to the target database (production start-up already refuses pending ones, `web/production.py:554-561`), privacy and legal decisions (D16), approved Bulgarian copy, capacity and clock evidence, content review of §4.4.

## 9. Acceptance tests (future, temporary databases only)

Built from migrations plus canonical via `tests/support.py` (`build_test_database`); never `data/questions.db`. Each item below is testable on its own.

- **Format and pools.** Blueprint has the expected Part 1 rows and the start fails closed on a tampered one (kind, count, points, gap, unsupported constraint) **when creating**. Pools equal the §4.2 counts on the canonical build: **105** multiple choice and **53** open (56 only before the stimulus layer), per exam 6, 7, 9, 7, 9, 7, 8 (a content PR updates them deliberately). Every pool item is official, Part 1 linked, approved, renderable or gradable, never Part 2, never AI, one kind per slot. **Stimulus layer:** questions 67, 131 and 202 are never drawn, over many seeds and in both a full and a thin pool; the test also asserts each still exists with the source and task of §4.2, so repairing one is a deliberate edit of the list. Thin pool in each kind: `pool_insufficient`, no attempt created, nothing borrowed, message free of counts. Plan: 25 distinct ids, kinds and points per slot, same seed gives the same draw and order.
- **Storage.** Migration is additive from a populated 026 database; `submit_cutoff_at` and `slot_format_json` are present and NOT NULL; row-local checks with negative controls (as `tests/test_practice_runtime_migration_sql.py`), including the timestamp shape and orderings of §5.3 and that `abandoned_at` before `started_at` is accepted. **Uniqueness negative control:** `(slot 16, Q, part 1)` plus `(slot 17, Q, part 2)` is **accepted by the table**, which documents that the key is not the guarantee; the service test shows a submit never writes it and that every row has `question_id` equal to the plan entry for its slot. **No length check:** a `raw_answer` over the service bound is accepted by the table and refused by the service. Classification, empty fresh build, updater preservation, reference gate and parity see both tables; `slot_format_json` is registered in neither id list; a characterization test records that a non-flat document is read differently by the updater and by parity (§5.4) and that a flat plan is read identically.
- **Start and resume.** One open attempt per account under concurrent starts; resume writes nothing. **Order:** with the blueprint tampered, an open attempt still resumes and a start with none open returns `blueprint_unavailable`; after the blueprint is re-seeded with other points, the open attempt still shows its stored kinds and points. **Grace page:** during grace, a start returns the same attempt and the page offers no start action; at exactly the stored cutoff the page offers one and a start expires the old row and creates a new attempt in one transaction. Stale row abandoned; malformed plan or slot format fails closed; a future-stamped row is abandoned by the next start (which calls `admit()`) and a normal new attempt is created only if the blueprint and pools pass, else the marking alone is committed.
- **Submit.** Idempotent (second submit changes no row and not the clock row); bounds: over-length, NUL, duplicate fields, unknown ids ignored, an `a_<id>_<n>` with `n` not a stored part ignored (no extra graded row); **a missing `Content-Length` and a declared oversize body are refused before the form is read, and a terminated chunked body over the limit is refused with nothing graded, so no truncated value reaches the matcher** (driven as WSGI, as the reviewer's check was). Empty refused; expired and abandoned refused with no answer rows; scores equal the persisted rows; weights per part; ordered and order-independent cases; a changed question after start never changes the saved score; **changing the blueprint slots after start changes neither that attempt's grading nor its maximum**; adding an id to the stimulus list after start does not drop an item of a running attempt.
- **Time.** At the deadline minus 1 s, at the deadline, at the cutoff minus 1 s, at the cutoff (exclusive), always against the **stored** cutoff. **Constant:** the same stored row at the same `now` has the same state with `SUBMIT_GRACE_SECONDS` at 60 and at 120. With `lifecycle_time`: clock regression, step, unsynchronised, reboot, suspect state give 503 and write no C3 row, and reads show persisted state. **Admit:** after a C3 write, moving the clock back is refused (no 9,000 s remaining); a read-only outcome leaves the high-water mark unchanged. **Recovery:** after a forward fault and `clock-recover`, a future-stamped C3 row neither gains working time (submit refused, no grading) nor changes the clock state, and a later assignment write is not blocked by it. All with no state change on any GET (digest of every non-canonical table and the clock row before and after, including a GET past the deadline).
- **Client (C3.5).** Markup tests for the data attributes, the draft scope string, the field names and the clearing call on the result page and the expired notice; manual browser acceptance for each case of §6.8, recorded in that PR, including the suspend across the deadline and the cutoff, the hidden tab, the refused and redirected submit keeping the draft, a draft of another attempt or owner not restored, and JavaScript disabled (manual submit).
- **Access.** Signed-out redirects write nothing; cross-origin POST is 403 before any write; another account, a teacher and an admin get 404 on attempt and result; an open, expired or abandoned attempt has no result; no correct answer in any attempt page or answer row.
- **Labels.** Over the active pool, no label is **rendered** when it equals or is contained in the normalized prompt, and no **rendered** label reveals an accepted answer that the prompt does not already show. (The first draft's stronger claim, that no label contains an accepted answer, is false for question 834 and is not tested.)
- **Isolation and gate.** Routes absent when unarmed (URL map equal to before); existing routes, the numeric 404, admin DZI pages, assignments, quotas and C2 unchanged; no assignment, token, vault note or `quiz_attempts` row created; armed, the policies are exactly `signed_in` and `same_origin`.
- **Rendering.** The multiple-choice partial renders the old assignment and C2 result pages byte-equal after the slot-number parameter; the result shows 45 as the maximum and subtotals from stored rows.

## 10. Implementation order

One PR at a time, each separately reviewed; none activates anything.

1. **C3.1 Storage.** Migration 027 with the §5.3 shape (including `submit_cutoff_at` and `slot_format_json`), classification, manifest, parity, updater id columns (`question_ids_json`, `question_id` only), tests (the #365 shape). No service, route, template or activation.
2. **C3.2 Pools and start.** Open-attempt handling, then blueprint validation for creation only; pool composition with the stimulus layer; start/resume/expire/abandon service; state from stored columns; deterministic plan; the clock contract of §7.4 (admit paths, no `check_factual`, future-start rule); tests incl. thin pool and no borrowing.
3. **C3.3 Submit and grading.** Idempotent submit reading kind and points from the stored slot format, bounds including the `Content-Length` rule and part-number validation, grading, weights, expiry races, clock tests.
4. **C3.4 Result loader.** Saved result, review rows, partial parameterization with the render-equality test.
5. **C3.5 Routes and templates, default off.** Policies, `AUTH_ROUTE_CONTRACT.md` section, the attempt-page loader with the label guard and the deadline, grace and cutoff pages (§6.3), the client countdown, draft and submit (§6.8), armed-interpreter tests, Bulgarian copy once approved.
6. **Activation** is a later, separately authorized change (§8). C4 and C5 keep their own scopes.

## 11. Decisions for review

Every row is an **engineering proposal** from the independent review's recommendations, not an adopted decision. "Settle by" names the slice whose review decides it; timing values and activation are separate (§7.1, §8).

| # | Decision | Proposal | Evidence | Settle by |
|---|---|---|---|---|
| 1 | Grace and late submit | Grace 60 s (a timing value); **store the cutoff**; a late submit is refused and lost; client auto-submit from elapsed time plus a C3-namespaced local draft. Alternatives, not proposed: accept a late submit as an unofficial untimed result, or save server-side drafts (more storage and races). The existing draft script is a model only and cannot be reused unchanged (§6.8) | §5.3, §6.8, Appendix B | Cutoff column: C3.1. Grace value: C3.2. Client: C3.5 |
| 2 | Pool by kind or by task number | By kind (by task number leaves six slots with three candidates and ignores the importer's overrides) | 24 kind mismatches across four sessions | C3.2 |
| 3 | Mixed pool across sessions or one session | Mixed (no session supplies ten open items; the best has 9) | §4.2 | C3.2 |
| 4 | Non-conforming weights | Exclude the two items (weights sum to 2 and 4); do not scale marks. Also exclude the three stimulus-missing items by id | §4.2, §6.5 | C3.2 |
| 5 | Clock source | The lifecycle clock with `admit()` on every writing path, no `check_factual` on C3 rows, a future-stamped row failing closed alone; not the raw clock of C2 | §7.4, Appendix B | C3.2 |
| 6 | Input bounds | 64 KiB body and 200 characters, enforced in the service; refuse a missing `Content-Length` and an oversize declared length before reading the form; **no length `CHECK`** on `raw_answer`, so a later change needs no table rebuild | §6.4, Appendix B | `CHECK` choice: C3.1. Rest: C3.3 |
| 7 | Hide source and task number from students; whether result text may name the year or session | Hide (keeps the mixed-session claim honest) | §4.2 | C3.5 |
| 8 | URL prefix; which accounts may take it | `/simulation/dzi-part1`; any active account, as C2 does | C2 contract | C3.5 |
| 9 | Per-account attempt rate cap | None now; revisit with capacity evidence | Pool composition took a median 1.9 ms under the writer lock on the reviewer's machine (Appendix B); at most 45 answer rows per attempt | Activation |
| 10 | Duplicate-prompt guard | None; keep the pool test | 0 exact and 0 near duplicates | C3.2 |
| 11 | C4 eligibility wording and weight of the short-answer component | Leave to C4 | out of scope | C4 |
| 12 | External verification of duration, layout and marking | Leave open; never say "official" before someone verifies it outside this repository | §3.2 | Before activation |

## Appendix A. Evidence record (author)

Two runs, both on a fresh disposable canonical build, interpreter `/private/tmp/learnpilot-content-updater-implementation.auvobkfa/venv/bin/python -B` (site enabled), run from the worktree. The first is dated **2026-11-22** (worktree at `f5ab6e2`). The second, the **2026-11-23** reproduction for revision 2 (worktree at `86e0dd5`), found no code, canonical or migration difference from `f5ab6e2` (`git diff f5ab6e2 86e0dd5 -- data/canonical web/migrations src web` is empty).

- Build: `python -B -m src.build_database --output <scratch>/tmp/disposable_canonical.db` → migration level `026_practice_runtime.sql`, 27 canonical tables, 12,288 rows, validation ok, both dates. Output path was in the scratch root only.
- Read-only scripts opened that file with SQLite `immutable=1` and called the repository's own predicates (`is_quiz_question_eligible`, `fetch_dzi_open_questions`, `quiz_normalize_text_answer`, `_quiz_subquestion_accepted_answers`, `dzi_question_sql`, `fetch_question_visual_assets`) so the counts are the production rules. They carry an audit hook that refuses any open of a runtime `questions.db`. They are not committed; their SHA-256 values and outputs are in the handoff manifest.
- **Reproduced 2026-11-23 by the author** (same results as the reviewer's independent run): multiple-choice pool 105 (93 at tasks 1-15, 12 at 16-18); open linked, quiz-eligible, readiness-eligible, after weights: 70, 69, 58, 56 (the 56 ids are identical to the reviewer's list); **open after the stimulus layer 53**, per exam 6, 7, 9, 7, 9, 7, 8; the three removed items have no resolved visual and no asset link of any type; 24 kind mismatches against the skeleton (six in each of the four deviating sessions); parts per open item 18/2/33 (final 53) and 20/2/34 (56); **labels**: of 132 sub-answers of the 58 readiness-eligible items, 0 labels equal an accepted answer and 1 contains one (question 834, whose label equals the prompt), with or without a three-character minimum; the first draft's query, with its `ca`/`aa` aliases, reads an empty accepted-answer set for all 132 rows; of 126 labels in the 56 pool 19 equal and 23 sit inside the prompt; of 121 in the 53 pool 17 and 23, so 81 would be shown and 40 hidden, and no shown label reveals an accepted answer the prompt lacks.
- Reproduced on library code, no database: a submitted part 9 of a one-part item becomes an extra graded row in `grade_quiz_text_answers`; on `[{"slot": 16, "question_id": 5}]` the updater reads `[5, 16]` and parity `[5]`, on a flat array both read every item; with Flask 3.1.3 and Werkzeug 3.1.9, a per-request `max_content_length` of 65,536 refuses a declared 70,000-byte body with 413 while a terminated chunked 70,000-byte body answers 200 with the value cut to 65,534; the body-size model gives 54,509 bytes for a browser worst case and 72,509 for a hand-crafted one.
- Not done and out of boundary: any access to `data/questions.db` or its sidecars, any server or tunnel, any generated-vault write (the vault path passed to the quiz module pointed at a non-existent scratch path and was never created), any provider, credential, import or real migration, any network research.

Limitations. (1) Counts are a function of `data/canonical` at `f5ab6e2` and of which image files exist under the repository root; a deployed static directory was not inspected. (2) 90 minutes, the 15/10 split and the point values are repo facts, not verified against any official publication. (3) The proposed bounds, grace, table shapes, client behaviour and clock rules are untested designs: nothing described for C3 was run as C3 code. (4) Concurrency, clock and timer behaviour is by reading code, by analogy with C2 and the quiz, and, where Appendix B says so, from the reviewer's throwaway prototype. (5) The task-number findings of §3.3 depend on the importer's overrides being correct. (6) Whether any other item lacks its stimulus rests on a manual reading (Appendix B).

## Appendix B. Reviewer evidence (not re-run by the author)

The independent review of the first draft (exact SHA `86e0dd5820ad821d68a5dfdc651a3ab439c2e5d3`, 2026-11-23, Claude Opus 5.5 at High effort) is untracked; it is identified by SHA-256 (`REVIEW.md` `a694e9c21e34e2c1d9336231f8a5ccb1378cbddfac8babf90990c3c1a510087c`, `CORRECTIONS.md` `2de763c02e4a1f3946c4662040f513480e692888f9a771071c735baea61b689e`) and the logs below. This document relies on the following results **only as the reviewer's**: they came from throwaway scripts on a disposable copy, one Mac, a prototype of the proposed design and not C3 code. The author did not re-run them.

| Result used | Reviewer's log (SHA-256) | Where used |
|---|---|---|
| Cutoff derived from a constant: same row `lapsed` at 60 s and `grace` at 120 s (T2) | `logs/prototype_runtime.json` (`c2ae2c14002f71466c68867475a99bd642139981a2141b83ddaf209134db3aed`) | §5.3, §7.2 |
| A key `(attempt, question, part)` accepts `(slot 16, Q, 1)` with `(slot 17, Q, 2)` (T1) | same | §5.3 |
| Without `admit()` a clock moved back 1 h is accepted (9,000 s remaining on a 5,400 s attempt); with it, refused (T3) | same | §7.4 |
| After a forward fault and a real `clock-recover` a C3 row keeps its future `started_at` (26,399 s remaining); with `check_factual` the shared clock goes `suspect` and the next assignment write is refused (T4) | same | §7.4 |
| Races: 12 concurrent starts leave one open attempt; 8 concurrent submits store one answer set; grace-window and post-cutoff submits racing a start behaved as §7.3 states over 25 rounds each (T6) | same | §7.3 |
| `read_snapshot` on a `query_only` connection reads the clock and releases its transaction (T5); pool composition median 1.9 ms under the writer lock (T7) | same | §6.3, §11 |
| Existing countdown, run on a virtual clock by the repository's own script: a 10-minute suspend submits at 1,199 s of a 600 s deadline and 660 s cutoff | `logs/timer_model.json` (`c3bf240d513eb1223d99c1be55167ecceab22f423cfd8cf4eb911a52cb7311d0`) | §6.8 |
| Chunked-body truncation to 65,534 (also reproduced by the author, Appendix A) | `logs/chunked_bound.log` (`8c49aad263787a28737a3423c461259da134d4fee5a9f4ab98d028b6fa175d46`) | §6.4 |
| The three stimulus-missing items, read in full (131, 67, 202) | `logs/stimulus_targets.log` (`4f31cbbc4e563547792287ac112eede5a4ed0993f871db365d2b641fdef08048`) | §4.2 |
| Manual reading: the 35 other open prompts without a visual and the 8 multiple-choice prompts without a visual that matched a stimulus search were read in full and no other missing stimulus was found; the other 90 multiple-choice prompts without a visual were screened by that search only | `logs/stimulus_sweep.log` (`bf74f010350bfebd954f413eb5cee2561be43472baf6f276e2674e4e0e7b42b6`), `logs/stimulus_tails.log` (`11f4851960c2ec559a16ebd17fcd653b22ea088bf64c3adfe0be63e9c0feb8ad`) | §4.1, §4.4 |

Limits stated by the reviewer: browser timer throttling was not measured (the timer result models a delayed callback only); the production proxy, deployed static files and live data were not inspected; official duration, layout and marking were not checked; the stimulus review is a reading of Bulgarian prompts, not proof.
