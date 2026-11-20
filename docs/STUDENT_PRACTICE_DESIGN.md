# Student practice design (C1)

Status: design only; nothing implemented, migrated or activated. Authority: the 2026-11-18 update (`docs/REFACTOR_ACCOUNTS_ROADMAP.md:13`); review §5.4 is historical, re-checked here. **Current** is verified source at `437208a`; **Proposed** is an engineering decision. D16 (lawful basis, minors, retention), controller and contact facts, RD15 text and activation evidence stay unknown or pending.

## 1. Current facts

- `quiz_attempts.assignment_id` is NOT NULL (`web/migrations/001_quiz_tables.sql:14`); one attempt per `(assignment_id, user_id)` (`015_quiz_attempts_user.sql:58`). NULL-owner attempts are browser-bound (`web/access.py:146`, `web/attempt_access.py:14`).
- `quiz_create_assignment` requires teacher capability (`web/services/quiz.py:268`), enforces the per-account quota and mints a token (`:296-297`; 20 per 24 h, 10 open, admins exempt: `web/services/assignment_quota.py:54-59`).
- The token landing is public; only a signed-in account can start an attempt (`web/blueprints/quiz.py:77-82`). No `/quiz/<int>` rule exists (`:45`). Attempt and result pages are owner-only, without teacher/admin bypass (`web/attempt_access.py:33-44`); management is separate (`web/access.py:100`).
- `quiz_result_context` (`quiz.py:2404`) and `quiz_load_result_questions` (`:1920`) read `quiz_answers` by attempt. `quiz_result.html` uses `assignment.title_bg`, `attempt.student_name`, `student_quiz_path(assignment)` (lines 4, 17-18, 62).
- Usable questions: `quiz_section_question_ids_by_section` (`:1106`) requires approved (`:95`), multiple choice, consistent subject via `AssessmentScope` (`src/subject_assessment.py:23`), four valid options and a usable visual. Browseable is not eligible.
- Durable history (PR #327) and cleanup (#328) are not implemented (`docs/ASSIGNMENT_LIFECYCLE_DESIGN.md:17-19`).

## 2. Storage: dedicated runtime tables

| | A. System assignment per session | B. Dedicated tables (chosen) |
|---|---|---|
| Creation | Needs a teacher/admin principal and spends quota; forging or bypassing breaks both contracts. | No principal, no quota. |
| Repeat | One attempt per account and assignment: each repeat adds an assignment, token and ledger row. | A repeat is a session row. |
| Side effects | Unowned rows fill every admin listing (`web/blueprints/teacher.py:459`) and get lifecycle fields, tokens, vault notes (`web/blueprints/quiz.py:238-239`). | None. |
| Cost | Reuses submit, scoring, result. | Own submit/result over reused pure helpers. |

B is chosen: A is cheaper only by weakening two contracts and flooding admin surfaces.

Proposed shape (additive migration 026; none written here): `practice_sessions` (id, `user_id` NOT NULL `ON DELETE RESTRICT`, `section_id`, nullable `topic_id`, `seed`, `question_ids_json`, `started_at`, nullable `submitted_at`, `score_correct`, `score_total`) and `practice_answers` (`session_id`, `question_id`, `chosen_letter`, `is_correct`; unique per session and question). No question text, name or blobs.

- **Ownership:** `user_id` comes only from the validated principal (as `quiz.py:2095`).
- **Resume:** a partial unique index allows one open session per account and scope; start returns it.
- **Repeat:** only after submission, as a new session with a fresh random sample.
- **Idempotent submit:** mirror `quiz_submit_attempt` (`quiz.py:2298`): one immediate write transaction, re-read the row; already submitted writes nothing; else grade on the server (`quiz.py:1322`), insert answers, `UPDATE ... WHERE submitted_at IS NULL`, rowcount 1. Empty submissions are refused. No timer, deadline or vault note.
- **Later integration:** classify both tables RUNTIME, never canonical (`src/canonical_tables.py:35-80`; unclassified tables are reported, `:140-146`). A fresh build leaves them empty (`src/build_database.py:11-12`); the updater digests every non-canonical table (`src/update_canonical_content.py:639-654`). Add their question-id columns to `JSON_ID_COLUMNS` (`:129`) and the reference gate (`:731-736`). Existing `quiz_*` rows and NULL attempts are untouched; nothing links by name.

## 3. Access: all practice is signed in

- Every practice route uses `signed_in` (`web/access.py:32`), same-origin on POST: DZI practice (C3) and non-DZI practice alike. **Non-DZI practice is not public:** progress is account-owned, and an anonymous path would revive the browser marker that is no longer written (`web/attempt_access.py:14`). Browsing stays public; anonymous visitors see a sign-in prompt.
- Session and result pages check `user_id` equality and answer 404 to anyone else, teacher and admin included (as `web/access.py:146`).
- Unchanged: `/quiz/s/<token>`, quiz attempt ownership, the generic 404 on `/quiz/<assignment_id>`, admin-only `/dzi*` and `/teacher/dzi-training`, assignment management.
- C2 excludes `dzi_training` sections (`src/subject_assessment.py:59`) and `dzi_question_sql` rows (`web/services/dzi.py:39`). DZI pools wait for C3; the content gate is C5.

## 4. First-version progress

Progress is the account's own submitted practice sessions, per section or topic.

- **Counted:** sessions with `submitted_at`; score fixed at submission (unanswered wrong), shown `correct/total`, with the session count and latest score.
- **Repeats:** each submission is its own entry; none replaces, averages or "bests" another.
- **Incomplete:** an open session is resumable, never counted or scored, never expires in v1.
- **Content changes:** stored ids, answers and scores are a snapshot, never recomputed. Pages render only still-renderable questions and state the skipped count (`quiz.py:2409-2412`); an open session grades only what it still presents (`quiz.py:2275`).
- **Deferred:** mastery, ranking, calibrated difficulty, streaks, per-question history, teacher visibility, export, deletion, retention and any durability promise. PR #327's history witness is not implemented.
- C4 may add account-owned `quiz_attempts` as a labelled source, never NULL-owner ones.

## 5. C2 flow, pools, result reuse

- **Flow:** section page, "Упражнявай се", POST start (resumes an open session), answer, POST submit, result with "Нов опит". Topic practice is the section pool restricted to that topic.
- **Pool:** usable ids from `quiz_section_question_ids_by_section`, minus DZI. Proposed: start needs at least 5 usable, N = min(10, usable), both tunable. Below 5: no start button, a Bulgarian "not enough questions yet" message. No top-up from another topic, section or subject; no bank size is assumed.
- **Reuse:** `quiz_result_context` and `quiz_result` are assignment-coupled and stay unchanged, with scoring and access. Extract the review loop (`quiz_result.html:98` on) into a partial, reuse `quiz_load_questions` and the explanation fetchers, and add a loader over `practice_answers`. A render-equality test pins the old result page.

## 6. Future acceptance (not tested here)

- Anonymous practice requests redirect to login and write nothing; another account, teacher or admin gets 404 on a session and its result.
- A double submit keeps one answer set and score; an empty submit stores nothing.
- A thin or empty pool creates no session and borrows nothing.
- The migration is additive; classification, empty fresh build and updater preservation hold.
- Existing routes, the numeric 404, DZI admin tools and quota behaviour are unchanged; C3, C4 and C5 stay separate PRs.
