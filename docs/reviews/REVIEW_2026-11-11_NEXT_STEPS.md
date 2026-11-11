# LearnPilot review and proposed next steps — 2026-11-11

- **Reviewed:** `main` at `2536481` (#351, "Add 17 Grade 8 Biology respiratory questions").
- **Reviewer:** Claude, read-only review requested by Gabriel. No code, data or
  configuration was changed; this file is the only output.
- **Status of this file:** a review and a *proposed* plan. It becomes work
  instructions only for the items Gabriel approves (section 4).

## 0. How to use this file (Codex)

1. `AGENTS.md` still governs everything: inspect before editing, small PRs,
   additive migrations, Bulgarian UI text, English identifiers, and **do not
   commit until Gabriel approves**.
2. This file does not replace `docs/REFACTOR_ACCOUNTS_ROADMAP.md`. Section 5
   proposes a different order for the roadmap's remaining steps. `AGENTS.md`
   forbids changing the sequence unless Gabriel changes the plan, so the
   reordering applies only after Gabriel records it (decision D1). If he
   approves it, the first PR is a short "Current roadmap update" entry in the
   roadmap that points here.
3. **Track A (section 5.2) needs no product decision.** Start there, one PR per
   item, in the listed order.
4. Every other track is blocked on a decision in section 4. Where a decision is
   blank, do not pick an option; use the stated default only if it says
   "safe default", and flag it in the PR report.
5. Never flip a `PRODUCTION_ACTIVATION` constant, deploy, or touch a production
   database as part of any item here. Those remain separately authorised work.
6. Line numbers below are as of `2536481`. Locate code by function name.

## 1. Verdict

The engineering foundation is strong: the refactor, access policies, accounts
and the canonical data pipeline are done and well tested. The problem is
balance and sequencing.

- `main` cannot be used by a class today. A student needs an account to start a
  test, every self-service way to get one is merged but switched off, and the
  remaining blockers are mostly not code.
- Roughly six weeks of work (#284–#349, 6–23 October) went into sign-in,
  registration, recovery and operations. The student-facing product and the DZI
  content did not move in that time.
- The DZI session in May 2027 is the natural deadline. Work that does not help a
  12th-grader practise by then should wait.

## 2. Roadmap status as found in the code

| Stage | State |
| --- | --- |
| Foundation (PR 0, A, B, C, C2, D1, D2, F, E1–E5, G0–G2, H) | Done. `web/app.py` is 697 lines (was 5,315). All 57 routes resolve to a declared access policy. A database builds from `web/migrations` + `data/canonical` in under 2 s. |
| Accounts, ownership, admin TOTP, public tokens and QR, deadlines and closure | Done. |
| Google sign-in | Merged, off (`web/services/google_oidc.py`, `PRODUCTION_ACTIVATION = False`). |
| Student and teacher registration, profiles | Merged, off (`web/services/registration.py`, `web/services/profiles.py`). |
| Password recovery | Merged, off (`web/services/recovery.py`). |
| Activation prerequisites | `docs/reviews/EMAIL_REGISTRATION_DESIGN.md` §0.3: "None is done." |
| DZI content gate | Not started. `web/services/dzi.py` defines `is_dzi_question`; nothing calls it. |
| Student practice experience | Not started. No self-practice route exists. |
| Entitlements, billing | Not started. No plan columns, no `has_feature`. |
| CSS split (PR I) | Not started. `web/static/css/ui-pass9.css` is 5,268 lines. |
| Production content-update workflow (roadmap H, item 3) | Not started (`docs/CANONICAL_DATA.md`, "Remaining work"). |

Size, for scale: `web/` 19,878 lines of Python, `tests/` 94,578 lines,
`docs/` 36,936 lines of Markdown, 26 migrations.

## 3. Findings

### F1. `main` cannot serve a classroom

Since #320 and #322 a new attempt requires a signed-in account
(`web/blueprints/quiz.py`, the start view redirects an anonymous POST to
`/login`). With all four gates off, the only way to create an account is
`python3 -m src.manage_accounts create`, one account at a time with an
interactive password prompt. Deploying `main` as it stands would be less usable
for a class than the name-based flow it replaced.

What stands between `main` and a usable release (design §0.3) is mostly outside
the code: a privacy page and a position on minors' data, a mail provider with
SPF, DKIM and DMARC, measurements on the real VPS, and a tested restore.

### F2. Effort has drifted away from the product

- #284–#349 are almost entirely authentication and operations.
- The last tester checkpoint in the repository is dated 2026-05-15
  (`docs/reviews/tester_feedback_plan.md`).
- Design documents have grown past what one person can review:
  `docs/ASSIGNMENT_LIFECYCLE_DESIGN.md` is 4,223 lines,
  `docs/reviews/GOOGLE_OIDC_DESIGN.md` 4,036, `docs/DEPLOYMENT.md` 3,940.
- The lifecycle design still has seven decisions marked "Unresolved" (items 5,
  6, 8, 14, 15, 18, 19 in its closing table). Item 18's proposed default is at
  least three independently operated time sources before old attempts may be
  deleted. That is out of proportion for a quiz application at this stage.

### F3. DZI content is behind, and there are no explanations

- The newest imported DZI session is May 2025. Seven sessions are in the
  current format (25 Part 1 questions each); three more come from the legacy
  parser.
- No question has an explanation. The schema has no explanation column, so
  `fetch_question_explanations` in `web/services/quiz.py` finds nothing and the
  result page prints `QUIZ_NO_EXPLANATION_MESSAGE`
  ("Няма въведено обяснение за този въпрос.") for every wrong answer. Testers
  asked for explanations in May. `tester_feedback_plan.md` ticks "Wrong
  multiple-choice/open-answer feedback added": the mechanism exists, the
  explanation text does not.
- `difficulty` carries no information: 1,108 of the 1,116 rated questions are
  `medium`; the 579 IT classroom questions have none.

### F4. A teacher's test is one section and random questions

`quiz_assignments.section_id` is `NOT NULL` (`web/migrations/001_quiz_tables.sql`),
so a test covers exactly one section. Each student gets N random questions.
`teacher_new` rejects hand-picked questions (`invalid_question_plan`). There
are no multi-section tests, no fixed variants and no preview. The sections
already carry `is_entry_check_scope` and `is_exit_check_scope`
(`000_baseline.sql`), which nothing uses for test building.

### F5. Student names and answers are written to vault files

`quiz_write_attempt_note` and `quiz_write_assignment_note`
(`web/services/quiz.py`) run on every submission and on every assignment
create, edit, duplicate and close. They write Markdown with the student's name,
answers and score under `DZI_VAULT`. Cleanup does not delete these files
(lifecycle design §10.7, open item 8). In production nobody reads them, they
duplicate personal data outside the database, and they are one of the three
stores the backup and restore scripts must keep consistent.

### F6. No CI, no pinned dependencies

There is no `.github/` directory. The suite (4,724 tests, about 12 minutes)
runs only when someone runs it. `requirements.txt` holds version ranges and
there is no lock or constraints file, so two installs can differ.

### F7. Mail is sent inside the only worker

`deploy/gunicorn.conf.py` runs one sync worker. Registration and recovery send
mail synchronously with `COMMUNICATION_DEADLINE = 8.0` seconds
(`web/services/mail.py`). A class of thirty registering together serialises
thirty sends, and quiz submissions wait behind them. The design already makes
"Single-worker capacity" an activation prerequisite and notes that a remedy
needs its own decision (D19).

### F8. Smaller code and data issues

Data, from a database built from `data/canonical` (ids are canonical):

- Question 329 (`true_false`, `classroom_tests_8_12_2026`, `quality_score` 1.0)
  is approved and public but has no option marked correct. Questions 516 and
  522 have the same defect and are not approved.
- 21 approved multiple-choice questions have three options, all in
  `classroom_tests_8_12_2026`: 268, 326, 336, 354, 389, 392, 393, 394, 400, 407,
  452, 471, 485, 486, 487, 492, 493, 499, 500, 504, 530. Question 496 has two
  and is not approved.
- 13 legacy DZI questions have the same prompt as a `_v2` import: 69, 70, 71,
  72, 73, 75, 80 (`aug_2023` vs `aug_2023_v2`) and 28, 29, 30, 33, 35, 36
  (`may_2023` vs `may_2023_v2`).
- 30 legacy fill-in questions (`aug_2023`, `may_2023`, `jun_2022`, ten each)
  have no sub-answers.
- 10 DZI questions have no `topic_id`: 22, 54, 83, 132, 164, 168, 170, 188,
  196, 834.

Code:

- Eight call sites still open a connection with `quiz_db()` and close it by
  hand instead of using `quiz_connection()`: in `web/blueprints/teacher.py`
  the views `teacher_dashboard`, `teacher_assignments`, `teacher_new`,
  `teacher_assignment_qr`, `teacher_assignment_results` and
  `teacher_assignment_results_export`; in `web/services/quiz.py` the two note
  writers. An unexpected exception between open and close leaks the connection.
- `teacher_new` is 291 lines with eight manual `conn.close()` calls and six
  `try` blocks.
- `web/app.py`: pyflakes reports 52 unused imports and three f-strings without
  placeholders; the module docstring still describes "Phase 1. Read-only". The
  rest of `web/` is clean under pyflakes.
- 19 calls to the deprecated `datetime.utcnow()` (`src/manage_accounts.py` 4,
  `web/blueprints/account.py` 5, `oidc.py` 2, `recovery.py` 4,
  `registration.py` 3, `web/services/quiz.py` 1).
- There is no privacy or terms page in `web/templates/`.
- The Nginx template sets no site-wide `Content-Security-Policy` or
  `Strict-Transport-Security`. Four templates carry inline scripts
  (`base.html`, `quiz_attempt.html`, `teacher_assignment.html`,
  `teacher_new.html`), which a CSP would have to allow or replace.
- Static files are cached for an hour without a version in the URL
  (`deploy/nginx/learnpilot.conf`), so a deploy can serve old CSS for an hour.
- Root-level leftovers: `REVIEW_2026-06-11.md`, `REVIEW_2026-06-11_pass2.md`,
  `audit_reclassification_report.md`, `audit_reclassification_state.py`,
  `repair_topic_areas_from_vault.py`, `cleanup_old_backups_keep_current.sh`.

What is in good shape and should be left alone: the default-deny access table
in `web/access.py`, the single connection factory in `web/db.py`, the
production start checks in `web/production.py`, template autoescaping (no
`|safe` anywhere), and the canonical data workflow.

## 4. Decisions for Gabriel

Codex does not decide these. Fill in the last column; a blank means "not
decided".

| # | Decision | Reviewer's suggestion | Gabriel's decision |
| --- | --- | --- | --- |
| D1 | Reorder the remaining roadmap as in section 5.1? | Yes. | |
| D2 | What is in the first public release, and by when? | Student registration, profiles and recovery; practice mode; fresh DZI content. Target the start of the second school term, well before the May 2027 session. | |
| D3 | Add a classroom entry path without email (teacher-issued class code or roster accounts)? | Decide after the thirty-submissions measurement of design §0.3. It avoids F7 and collects less from minors, but `users` requires an email for a password today, so it needs its own design and migration. | |
| D4 | Keep Microsoft sign-in excluded? | Check which accounts your students actually hold. If the school accounts are Microsoft ones, the provider-neutral identity storage makes adding it cheap compared with what is built. Not for the first release either way. | |
| D5 | Stop writing vault notes in production? | Yes, behind a configuration flag that keeps them for local development. Resolves lifecycle open item 8. | |
| D6 | Are new DZI source imports allowed again? | Yes, through the existing reviewed-JSON path only. | |
| D7 | How are explanations authored and approved? | Drafted per question, approved by Gabriel before they enter `data/canonical`, DZI pool first. | |
| D8 | Legacy duplicates (13 questions, F8): remove, or keep? | Remove from the public and quiz pools. Deleting rows needs explicit approval and a check that no production attempt references them. | |
| D9 | Three-option questions (21, F8): defect or intended? | Review each; add the missing option or record it as intended. | |
| D10 | Enable GitHub Actions for the test suite? | Yes. Check the minutes included in the account's plan first; a run takes about 12 minutes. | |
| D11 | Retention: build the cleanup tooling now? | No. Set a simple written policy and run it by hand until there are real users; close the seven open lifecycle items with the simplest safe answer. | |
| D12 | Teacher test builder scope (F4): which of multi-section, fixed variants, hand-picking comes first? | Multi-section, then fixed variants. | |

## 5. Work plan

### 5.1 Proposed order

1. Track A — hygiene that needs no decision (start now).
2. Track B — launch: the path to switching registration on.
3. Track C — student practice mode, then the DZI gate.
4. Track D — DZI content and explanations (can run alongside C).
5. Track E — teacher test builder.
6. Track F — operations simplification, as items become relevant.
7. Deferred (section 5.8).

This moves the roadmap's step 4 (DZI gate and practice) ahead of step 5
(entitlements), puts practice before the gate, and inserts the launch work the
roadmap leaves as "separate, explicitly authorised".

### 5.2 Track A — no decision needed

One PR each. Behaviour must not change unless stated.

**A1. Canonical question integrity test.**
Add a test that builds a database through `tests/support.py` and checks every
question that passes `QUIZ_APPROVED_FILTER`:

- `multiple_choice` and `true_false` have exactly one correct option;
- `multiple_choice` has four options;
- `fill_in` has at least one sub-answer with a non-empty `correct_answer`.

The current defects (F8) go into an explicit known-defects list in the test, by
question id, so the test passes today and any new defect fails it. Later data
PRs shrink the list. Acceptance: the list matches F8 exactly; adding a broken
question to a temporary database fails the test.

**A2. Unapprove question 329.**
It is public with no correct answer. Do not guess the answer. Through the
documented content update workflow (`docs/CANONICAL_DATA.md`), set its
`quality_score` below the approval threshold, re-export canonical, and remove
it from A1's list only when Gabriel supplies the correct option. Acceptance:
329 no longer passes `QUIZ_APPROVED_FILTER`; `canonical_export --check` passes.

**A3. One connection pattern.**
Convert the eight raw `quiz_db()` sites (F8) to `with quiz_connection() as conn:`.
No template, query or redirect changes. Acceptance: no `conn = quiz_db()`
outside `quiz_connection` itself; the full suite passes.

**A4. Split `teacher_new`.**
After A3. Move form parsing and validation, plan building, and persistence into
helpers with the same behaviour. Keep the server-side rejection of
`question_ids` and `question_plan` form keys. Acceptance: the existing
`/teacher/new` tests pass unchanged.

**A5. `web/app.py` cleanup.**
Remove unused imports, the three empty f-strings and the stale docstring. Some
names are imported so tests can patch `web.app.<name>`: remove a name only when
neither `tests/` nor another module reaches it through `web.app`. Acceptance:
the full suite passes; pyflakes reports only the names kept on purpose, each
with a one-line comment.

**A6. Replace `datetime.utcnow()`.**
19 calls. Stored and compared values must stay byte-identical: keep naive UTC
where the surrounding code expects it (for example
`datetime.now(timezone.utc).replace(tzinfo=None)`, in one shared helper).
Acceptance: no `utcnow()` left; no change in any stored timestamp format.

**A7. Tidy the repository root.**
Move the two June review files and the reclassification report under
`docs/reviews/`; list the three root scripts in `docs/SCRIPT_INVENTORY.md` as
historical or move them under `src/` if still used. No deletions.

### 5.3 Track B — launch

Goal: a class can register and take a test on the production site.

| Step | Owner | What |
| --- | --- | --- |
| B1 | Gabriel | Privacy text: lawful basis, minors and the digital consent age, retention, the mail provider named as a recipient (design D16, roadmap "Non-code checks"). |
| B2 | Gabriel | Choose the mail provider and sender domain; set SPF, DKIM and DMARC. |
| B3 | Codex | Privacy and terms pages: public routes, templates, a footer link. Bulgarian text comes from B1; do not draft legal text. |
| B4 | Codex | Design note (two pages at most), then the production content-update tool: apply new canonical content to a live database that holds accounts and attempts, keeping the stable-id contract of `docs/CANONICAL_DATA.md`. Dry run by default. |
| B5 | Codex | A one-page launch checklist extracted from `docs/DEPLOYMENT.md` §20 and design §0.3: record the currently deployed commit, the migrations between it and `main`, each evidence item, and who signs it off. No new procedure, only an index. |
| B6 | Gabriel | Deploy `main` with every gate off, following B5. |
| B7 | Gabriel + Codex | Run the §0.3 evidence on the VPS: bounded mail, thirty submissions at once, delivery to abv.bg, Gmail and a school mailbox, restore on a staging copy. Record results in a status file. |
| B8 | Gabriel | Decide D3 with the B7 numbers. If the capacity criterion fails, choose a remedy (F7) before activation. |
| B9 | Codex, on Gabriel's explicit instruction | Activation PRs in the order the designs allow (`EMAIL_REGISTRATION_DESIGN.md` §7.3; recovery before public opening). Google is separate and later. |

### 5.4 Track C — student practice (needs D1, D2)

The largest missing piece: today a student can take a teacher's test and browse
questions, but cannot practise alone.

**C1. Design note, two pages at most.** Settle:

- storage: `quiz_attempts.assignment_id` is `NOT NULL`, so practice needs either
  a system-owned assignment per practice session or its own tables. No schema
  change without a migration plan;
- access: `signed_in` for anything with DZI questions (roadmap, DZI gate item
  4); decide whether non-DZI practice is public;
- what "progress" means for the first version.

**C2. Practice by section or topic.** N approved questions, answers checked at
the end, the existing result page reused.

**C3. DZI Part 1 simulation.** 15 multiple-choice and 10 short-answer questions
from the DZI pool following `dzi_blueprints`, timed. Do not open `/dzi` or
`/teacher/dzi-training`; they stay admin tooling.

**C4. "My results" by topic.** Aggregated from the student's own attempts;
shows weak topics and links back into C2.

**C5. DZI content gate.** Exactly as specified in the roadmap's "DZI content
gate" section, including its acceptance tests. It comes after C2–C4 so that the
prompt to register leads to something.

### 5.5 Track D — DZI content and explanations (needs D6, D7)

**D-1. Import the sessions after May 2025.** Check which sessions МОН has
published; do not assume. Use the reviewed-JSON path only (`AGENTS.md`: no
direct OCR or PDF import). One session per PR, with images and task links.

**D-2. Explanations.** Migration plan first: an additive, nullable
`questions.explanation` column (the first name in
`QUESTION_EXPLANATION_COLUMNS`, so the result page picks it up without a code
change), plus the canonical manifest and export changes that a new content
column needs. Then content in batches through the reviewed import path, DZI
pool first (253 questions). An explanation enters `data/canonical` only after
Gabriel's approval (D7).

**D-3. Remaining data defects.** Per D8 and D9: the 13 duplicates, the 21
three-option questions, the 30 legacy fill-ins without sub-answers, the 10
questions without a topic. Each fix removes entries from A1's list.

**D-4. Leave `difficulty` alone.** Build nothing on it yet. Once practice
produces attempts, derive difficulty from the share of correct answers.

### 5.6 Track E — teacher test builder (needs D12)

Design note first; each bullet is at least one PR.

- **Multi-section tests.** `section_id NOT NULL` has to become a set of
  sections (a link table or the existing `question_plan_json`). Additive
  migration; existing assignments keep working.
- **Fixed variants.** The same questions for every student, or variants A and
  B, as an alternative to per-student random selection.
- **Preview and hand-picking.** The server keeps validating every question id
  against the pool the teacher may use; the current blanket rejection in
  `teacher_new` is a security property and is replaced, not removed.
- **Entry and exit level tests** built from `is_entry_check_scope` and
  `is_exit_check_scope`.

### 5.7 Track F — operations simplification

- **F-1. Vault notes off in production** (needs D5). A configuration flag; when
  off, both note writers return without opening a connection. A later, separate
  PR may drop the vault from backup and restore; that touches the restore
  scripts and needs its own review.
- **F-2. Continuous integration** (needs D10). One workflow running
  `python -m unittest discover -s tests` on pull requests. The suite needs the
  `sqlite3` command-line tool, full git history (`fetch-depth: 0`; one test
  reads a base commit) and a non-root user.
- **F-3. Pinned dependencies.** A constraints file generated from a clean
  environment on the production Python version; `docs/DEPLOYMENT.md` §5
  installs with it.
- **F-4. Mail outside the request path.** Only if B7 fails the capacity
  criterion. Needs a design decision first (design D19).
- **F-5. Security headers.** HSTS once TLS is stable. For a CSP, first move the
  inline scripts of the four templates into static files.
- **F-6. Versioned static URLs**, so a deploy never serves stale CSS.

### 5.8 Deferred until there are real users

- Entitlements and billing (roadmap steps 5 and later).
- The CSS split (PR I).
- Lifecycle cleanup tooling beyond what is merged (D11).
- Sign in with Apple.
- Capacity work beyond one class at a time.

## 6. Process suggestions

- **Scale the paperwork to the risk.** Keep design, test plan and independent
  review for authentication, sessions, personal data and restore. Give
  content, UI and teacher or student features a scope note of two pages at
  most.
- **Run a tester checkpoint every month.** The last one was in May. A feature
  is done when a tester has used it, not when its status file says so.
- **Clear open decisions in one sitting.** Section 4 and the seven lifecycle
  items together are about twenty decisions; unanswered, each one blocks work
  or gets a default nobody chose.
- **Track the gap between `main` and production.** Record the deployed commit
  in the repository after each deploy, so the distance is always visible.

## 7. How this was checked, and its limits

- Full suite on Linux, Python 3.13, fresh clone: 4,724 tests ran in 721 s;
  4,531 passed, 38 skipped, 155 did not pass. All 155 trace to the review
  sandbox, not to the code: 153 need the `sqlite3` command-line tool
  (`test_registration_restore_operations`, and the module setup of
  `test_lifecycle_restore_script`, whose tests therefore did not run at all),
  one needs git history that a shallow clone lacks (`test_recovery_no_caller`),
  and one changes file permissions, which has no effect when running as root
  (`test_google_oidc_routes`). The restore-script tests are therefore
  unconfirmed by this review.
- Data figures come from `python3 -m src.build_database` into a temporary
  directory and read-only queries. Totals: 1,695 questions (physics 619, IT
  classroom 579, DZI 253, biology 244); 1,588 multiple choice, 100 fill-in,
  7 true/false; 1,676 pass the approval filter.
- Lint figures come from pyflakes over `web/`.
- Not checked: the live site, any deployment script on a real server, the UI in
  a browser, and performance.

### Queries used for F8

```sql
-- approval predicate used below (QUIZ_APPROVED_FILTER)
--   (q.is_ai_generated = 0 OR q.quality_score >= 1.0)

-- no correct option
SELECT q.id, q.question_type, q.source_exam, q.quality_score
FROM questions q
WHERE q.question_type IN ('multiple_choice', 'true_false')
  AND NOT EXISTS (SELECT 1 FROM multiple_choice_options o
                  WHERE o.question_id = q.id AND o.is_correct = 1);

-- multiple choice without four options
SELECT q.id, q.source_exam,
       (SELECT COUNT(*) FROM multiple_choice_options o WHERE o.question_id = q.id) AS options
FROM questions q
WHERE q.question_type = 'multiple_choice'
  AND (SELECT COUNT(*) FROM multiple_choice_options o WHERE o.question_id = q.id) <> 4;

-- same prompt in two sources
SELECT a.id, a.source_exam, b.id, b.source_exam
FROM questions a JOIN questions b
  ON b.prompt = a.prompt AND b.id > a.id AND b.source_exam <> a.source_exam;

-- fill-in without sub-answers
SELECT q.id, q.source_exam FROM questions q
WHERE q.question_type = 'fill_in'
  AND NOT EXISTS (SELECT 1 FROM fill_in_subquestions s WHERE s.question_id = q.id);

-- no topic
SELECT id, source_exam FROM questions WHERE topic_id IS NULL;
```
