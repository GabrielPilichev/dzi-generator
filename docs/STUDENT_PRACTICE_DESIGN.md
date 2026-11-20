# Student practice design (C1)

Status: design only; nothing implemented, migrated or activated. Authority: the 2026-11-18 update (`docs/REFACTOR_ACCOUNTS_ROADMAP.md:13`). Sources verified at `437208a`; **Proposed** means engineering decision. D16 (lawful basis, minors, retention), controller and contact facts, RD15 text and activation evidence stay unknown or pending.

## 1. Current facts

- `quiz_attempts.assignment_id` is NOT NULL (`web/migrations/001_quiz_tables.sql:14`); one attempt per `(assignment_id, user_id)` (`015_quiz_attempts_user.sql:58`). NULL-owner attempts are browser-bound (`web/access.py:146`, `web/attempt_access.py:14`).
- `quiz_create_assignment` requires teacher capability (`web/services/quiz.py:268`), enforces the per-account quota and mints a token (`:296-297`; `web/services/assignment_quota.py:54-59`).
- The token landing is public; only a signed-in account can start an attempt (`web/blueprints/quiz.py:77-82`). No `/quiz/<int>` rule exists (`:45`). Attempt and result pages are owner-only, without teacher/admin bypass (`web/attempt_access.py:33-44`).
- `quiz_result_context` (`quiz.py:2404`) and `quiz_load_result_questions` (`:1920`) read `quiz_answers` by attempt. `quiz_result.html` needs `assignment`, `attempt.student_name`, `student_quiz_path` (lines 4, 17-18, 62).
- Usable questions (`quiz_section_question_ids_by_section`, `:1106`): approved (`:95`), multiple choice, consistent subject (`src/subject_assessment.py:23`), four valid options, usable visual. Browseable is not eligible.
- Durable history (PR #327) and cleanup (#328) are not implemented (`docs/ASSIGNMENT_LIFECYCLE_DESIGN.md:17-19`).

## 2. Storage: dedicated runtime tables

| | A. System assignment per session | B. Dedicated tables (chosen) |
|---|---|---|
| Creation | Needs a teacher/admin principal and quota; forging or bypassing breaks both contracts. | No principal, no quota. |
| Side effects | Unowned rows fill admin listings (`web/blueprints/teacher.py:459`) and get tokens, vault notes (`web/blueprints/quiz.py:238-239`). | None. |

A needs broader contract changes (principal, quota, listings, lifecycle); B is the smaller additive change.

Proposed shape (additive migration 026; none written here): `practice_sessions` (id, `user_id` NOT NULL `ON DELETE RESTRICT`, `section_id`, nullable `topic_id`, `seed`, `question_ids_json`, `started_at`, nullable `submitted_at` and `abandoned_at`, `score_correct`, `score_total`) and `practice_answers` (`session_id`, `question_id`, `chosen_letter`, `is_correct`; unique per session and question). No question text or blobs.

- **Ownership:** `user_id` comes only from the validated principal (as `quiz.py:2095`).
- **Resume:** start decides, abandons a stale session (§4) and inserts in one immediate write transaction. A partial unique index over `user_id`, `section_id`, `COALESCE(topic_id, 0)` while open (neither `submitted_at` nor `abandoned_at` set) allows one open session per account and scope, since NULLs never collide; start returns it.
- **Repeat:** only after submission, as a new session with a fresh sample; replacing an abandoned stale session is distinct (§4).
- **Idempotent submit:** mirror `quiz_submit_attempt` (`quiz.py:2298`): one immediate write transaction, re-read the row; already submitted writes nothing and an abandoned session is rejected; else grade on the server (`quiz.py:1322`), insert answers, update guarded on open (neither timestamp set). Empty submissions are refused; no timer or vault note.
- **Later integration:** classify both tables RUNTIME, never canonical (`src/canonical_tables.py:35-80`). A fresh build leaves them empty (`src/build_database.py:11-12`) and the updater digests them (`src/update_canonical_content.py:639-654`). Add their question-id columns to `JSON_ID_COLUMNS` (`:129`) and the reference gate (`:731-736`).

## 3. Access: all practice is signed in

- Every practice route uses `signed_in` (`web/access.py:32`), same-origin on POST, DZI (C3) and non-DZI alike. **Non-DZI practice is not public:** progress is account-owned and an anonymous path would revive the unwritten browser marker (`web/attempt_access.py:14`). Browsing stays public.
- Session and result pages require `user_id` equality; anyone else, teacher and admin included, gets 404 (as `web/access.py:146`).
- The profile gate stays on the token start only (`EMAIL_REGISTRATION_DESIGN.md` §7.4); practice stores no name.
- Unchanged: `/quiz/s/<token>`, the generic 404 on `/quiz/<assignment_id>`, admin-only `/dzi*` and `/teacher/dzi-training`, assignment management.
- C2 excludes `dzi_training` sections (`src/subject_assessment.py:59`) and `dzi_question_sql` rows (`web/services/dzi.py:39`). DZI pools wait for C3; the content gate is C5.

## 4. First-version progress

Progress is the account's own submitted practice sessions, per scope: a topic session counts only in its topic; no section roll-up before C4.

- **Counted:** sessions with `submitted_at`; score fixed at submission (unanswered wrong), shown `correct/total`, with session count and latest score.
- **Repeats:** each submission is its own entry; none replaces, averages or "bests" another.
- **Incomplete:** an open session is resumable, never counted or scored, without a timeout (a gameplay rule, not retention). One presenting no renderable question (`filter_renderable_attempt_question_ids`, `quiz.py:1549`) is stale: start sets its `abandoned_at`; it is uncounted, never submitted, no result. A replacement follows only if the §5 pool rule holds.
- **Content changes:** stored ids, answer verdicts and scores stay fixed, never recomputed; prompts, options, correct answers and explanations render from current content, not a snapshot. Pages show only renderable questions and the skipped count (`quiz.py:2409-2412`); an open session grades only what it presents (`web/blueprints/quiz.py:161`).
- **Deferred:** mastery, ranking, calibrated difficulty, streaks, per-question history, teacher visibility, export, deletion, retention and any durability or immutable-content promise.
- C4 may add account-owned `quiz_attempts` as a labelled source, never NULL-owner ones.

## 5. C2 flow, pools, result reuse

- **Flow:** section page, "Упражнявай се", POST start (resumes an open session), answer, POST submit, result with "Нов опит". Topic practice restricts the section pool by `questions.topic_id`.
- **Pool:** usable ids from `quiz_section_question_ids_by_section`, minus DZI. Proposed: start needs 5+ usable, N = min(10, usable), tunable. Below 5: a Bulgarian "not enough questions yet" message and no new session. A replacement obeys this: start abandons the stale session, shows the message, creates nothing. No top-up from another topic, section or subject; no bank size assumed.
- **Reuse:** `quiz_result_context` and `quiz_result` are assignment-coupled and stay unchanged, with scoring and access. Extract the multiple-choice review (`quiz_result.html:93-160`, stale wrapper included, open-answer blocks excluded) into a partial, reuse `quiz_load_questions` and the explanation fetchers, and add a `practice_answers` loader. A render-equality test pins the old result page.

## 6. Future acceptance (not tested here)

- Anonymous practice requests redirect to login and write nothing; another account, teacher or admin gets 404 on a session and its result.
- A double submit keeps one answer set and score; an empty submit stores nothing; an old tab cannot submit an abandoned, replaced session; concurrent starts leave one open session, topic-less scopes included.
- A stale open session never blocks a scope: abandoned, uncounted, replaced only with a sufficient pool, else nothing is created or borrowed.
- The migration is additive; classification, empty fresh build and updater preservation hold.
- Existing routes, numeric 404, DZI admin tools and quotas are unchanged; C3-C5 stay separate PRs.
