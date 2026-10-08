# LearnPilot — Assignment Lifecycle, Closure, Grace Period, Retention and Cleanup Design

Status: **design proposal, nothing implemented, not approved for merge.** This
document changes no application behaviour. It adds no migration, route,
template, service, timer or configuration. Every schema element, helper, route
and command described as "proposed" does not exist yet.

Scope: deadlines, manual closure, the in-flight submission grace period, the
retention clock, the boundary between deletable detail and durable learning
history, and the monthly cleanup job.

Conventions used below:

- **Existing** means verified in the repository at commit `8c443b7`. File and
  function references carry the line number at that commit.
- **Proposed** means new in a later PR.
- `E` is an assignment's effective closure instant, `G` the grace duration,
  `R` the retention period.
- `now` is lifecycle time: the server's clock as accepted by §3.6.
- `F` is a time fix: an interval `[F.lo, F.hi]` of UTC established from
  evidence outside the host (§10.1). Only the cleanup tool of PR #328 has one.

---

## 0. Corrections in this revision

The third independent review returned REVISE after reproducing three
counterexamples: early cleanup after a tolerated clock jump, a manual close
that updated zero rows, and an incomplete reconstruction of historical open
answers. This revision corrects those and the related findings. The
implementation sequence is unchanged: PR #326 lifecycle enforcement, PR #327
durable learning history, PR #328 cleanup and retention.

| # | Finding | Correction | Where |
|---|---|---|---|
| 1 | A clock step below the tolerance let cleanup delete early. | Deletion no longer rests on the server's clock. It requires a lower bound of the present and a recorded upper bound of the closure, both from evidence outside the host. Without that evidence cleanup is skipped. The boot reference is anchored, so small steps add up instead of disappearing. | §3.6, §10.1, §10.2 |
| 2 | An open question with "at least one row" was reconstructed as complete. | Removed. Completeness needs evidence that is independent of the rows. None is stored for open answers, so such attempts are unverifiable, keep their history and block cleanup. | §9.5 |
| 3 | Future scheduled deadlines were treated as clock failures. | Timestamps are classified. Only factual events and terminal markers may not lie in the future. | §3.9, §3.6, §10.2, §13.5 |
| 4 | A restore replaced existing holds with `restore_pending`. | A hold is a set of reasons. Reasons are added and resolved one at a time and never overwritten. A restore weakens no hold, incident, marker, floor or uncertainty. | §3.8, §13.4 |
| 5 | A verified snapshot of another environment satisfied the recovery gate. | Snapshots carry a database identity, the upload root, and digests of both artefacts. Cleanup recomputes the digests and compares the detail it will delete with the snapshot by content. | §10.8 |
| 6 | Recovery stamped closures from the global high-water mark. | Removed. Recovery writes no closure marker. Uncertain assignments are held. Ending a held assignment without evidence is not part of this design. | §3.7, §13.4 |
| 7 | Manual closure advanced `closed_at` past attempt starts and could match no row. | `closed_at` is the sampled time. The order of a start and a close is decided by guarded transactions under the writer lock. | §6.1, §7.1, §11.3 |
| 8 | PR #326 restores required PR #328 verification; "no write after a clock fault" contradicted the sticky suspect state. | Each phase's restore uses what that phase ships. One narrow clock-fault bookkeeping write is permitted; every ordinary lifecycle write stays refused. | §3.6, §10.1, §13.4 |
| 9 | Test expectations that contradicted the design. | L37, L62, L102, L103, L112, L113, L121 and C61 are corrected, further cases are adjusted to match, and new cases are added. | §15 |

A follow-up review accepted corrections 1 to 8 and raised two further points,
both corrected here without changing the accepted architecture:

| # | Finding | Correction | Where |
|---|---|---|---|
| 10 | Re-clean after a restore was circular: `restore_reclean` was resolved only by a re-clean, a re-clean needed a closure bound, and a bound was recorded only without a hold. | One operator-only path for an assignment held **exactly** for `restore_reclean`: the closure is confirmed with the hold in place, the full retention interval is waited out, the detail is re-cleaned, the result is verified, and only then is the reason resolved. The assignment is not served at any point, no other hold can use the path, and every step can be interrupted and run again. | §3.8, §10.1, §10.2, §13.4; C67, C88 |
| 11 | L113 said an assignment with an earlier closure marker can never become submittable. | New starts stay refused after closure. A valid in-flight attempt may still submit inside the exclusive grace window when no hold applies, `submissions_closed_at` is NULL and the clock is healthy. Holds, a set `submissions_closed_at`, expired grace and cleaned detail stay terminal for submissions. | §15.1, L113 |

Limits that remain after these corrections are listed in §16.5.

---

## 1. Approved product decisions

These are final and are not reopened here.

### 1.1 Lifecycle rules

| # | Decision |
|---|---|
| A | Deadlines are optional. The default offered to teachers is 14 days after creation. A teacher may choose no deadline. Timestamps are stored consistently and enforced server-side. |
| B | A student who started before closure gets a short, bounded submission grace period. No attempt may start after effective closure. Grace never gives unlimited access. |
| C | Detailed attempt data is kept for at least 30 days after effective closure. The clock does not start at creation or at the student's submission. Cleanup never deletes earlier than the threshold. |
| D | A closed or expired assignment cannot be reopened, by any teacher route, admin action or API parameter. A correction means a new assignment with a new public token. |
| E | Cleanup runs monthly, is gated on durable learning history being implemented and verified, deletes nothing before that, and is idempotent and safe to retry. |

### 1.2 Decisions approved for PR #326

| # | Decision | Approved rule | Specified in |
|---|---|---|---|
| P1 | Grace period | **15 minutes.** Grace begins at effective closure `E`. Only attempts validly started before closure qualify. A submission at exactly `E + 15 minutes` is rejected. Grace does not extend retention eligibility. Start and closure ordering is enforced transactionally. | §7, §11.2–§11.4 |
| P2 | Default deadline | **23:59 local time, 14 calendar days ahead**, in the teacher's selected IANA timezone, by calendar-day arithmetic, converted to UTC for storage. The deadline stays optional. | §5.1–§5.3 |
| P3 | Maximum deadline | **180 days**, applied to teacher-selected deadlines and validated server-side. "No deadline" stays available. | §5.4 |
| P4 | Title edits after closure | **Prohibited**, for manual closure and for expiry alike. | §6.6 |
| P5 | Duplication | **Offers the same deadline or no-deadline choice as normal creation**, prefilled with the P2 default. The source's deadline is never copied, and no deadline is imposed without the choice. The copy gets a new identity and public token. | §5.5 |

### 1.3 What is not yet confirmed for PR #326

The five product decisions above are approved. Implementing them required
choices the approvals do not state. These are **proposed, pending
confirmation**, and PR #326 is **not** fully unblocked until each is
confirmed or replaced (§16.2):

| # | Proposed implementation detail |
|---|---|
| I1 | The timezone is selected and stored **per assignment** (an IANA name), not per account. |
| I2 | `Europe/Sofia` is the **editable preselection** of the timezone selector. |
| I3 | The 180-day maximum is counted **from the date the deadline is set or edited**. |
| I4 | The stored cutoff is **exclusive, at the next local midnight**, and is displayed as "through 23:59". |
| I5 | An append-only **lifecycle event log** outside the database, as restore evidence (§13.4). Nothing in the design depends on it. |

None of I1 to I5 is approved by this document. One further behaviour is
explicitly **not approved and not included**: ending a held assignment
without evidence of its state (§16.3, item 17). Everything else still
undecided belongs to PR #327 or PR #328 and is listed in §16.3.

---

## 2. Current architecture

### 2.1 Tables

| Table | Defined in | Relevant columns | Notes |
|---|---|---|---|
| `quiz_assignments` | `001_quiz_tables.sql`, `006`, `014`, `017` | `id`, `section_id`, `title_bg`, `question_count`, `time_limit_minutes`, `created_at`, `question_plan_json`, `owner_user_id`, `public_token` | No deadline, closure or status column exists. |
| `quiz_attempts` | `001`, rebuilt by `015_quiz_attempts_user.sql` | `id`, `assignment_id`, `student_name`, `seed`, `question_ids_json`, `started_at`, `submitted_at`, `score_correct`, `score_total`, `user_id` | `submitted_at IS NULL` is the only "in progress" marker. |
| `quiz_answers` | `001` | `attempt_id`, `question_id`, `chosen_letter`, `is_correct` | Multiple-choice answers. |
| `quiz_text_answers` | `005`, `009` | `attempt_id`, `question_id`, `raw_answer`, `is_correct`, `points_awarded`, `points_possible`, `teacher_override`, `teacher_override_status`, `teacher_note`, `graded_at` | Open answers and teacher review. |
| `practical_submissions` | `008` | `quiz_attempt_id`, `exam_task_id`, `status`, `submitted_at` | `status` is inserted as `'draft'` (`web/blueprints/practical.py:283`); no code in `web/` moves it to `'submitted'` or `'reviewed'`. |
| `practical_submission_files` | `008` | `practical_submission_id`, `stored_path`, `is_deleted` | Bytes live on disk under the upload root, not in SQLite. |
| `users` | `012` | account rows | Accounts are disabled, never deleted (`docs/ACCOUNTS_DESIGN.md`). |

All of these are classified `RUNTIME` in `src/canonical_tables.py:63-68`. None
is exported to `data/canonical/`.

`web/db_introspect.py:11` and `web/services/practical.py:176` probe for a
`practical_submission_grades` table. No migration creates it
(`tests/test_practical_submissions_migration_sql.py:60` asserts its absence
from 008). Practical grading therefore has no persisted state today.

### 2.2 Foreign keys that matter for deletion

| Child → parent | Action | Consequence |
|---|---|---|
| `quiz_attempts.assignment_id → quiz_assignments.id` | none declared (NO ACTION) | An assignment row cannot be deleted while it has attempts. |
| `quiz_attempts.user_id → users.id` | `ON DELETE RESTRICT` (015) | Deliberate: an attempt never silently loses its owner. |
| `quiz_answers.attempt_id → quiz_attempts.id` | none declared (NO ACTION) | Deleting an attempt fails until its `quiz_answers` rows are deleted first. |
| `quiz_answers.question_id` | **no foreign key** | Plain integer. |
| `quiz_attempts.question_ids_json` | **no foreign key** | Question ids inside JSON; `src/canonical_parity.py:165-172` checks them separately. |
| `quiz_text_answers.attempt_id → quiz_attempts.id` | `ON DELETE CASCADE` | Deleting an attempt deletes its open answers and teacher review. |
| `quiz_text_answers.question_id → questions.id` | `ON DELETE CASCADE` | Existing hazard in the other direction: deleting a canonical question deletes student answers. Cleanup never deletes from `questions`. |
| `quiz_text_answers.subquestion_id → fill_in_subquestions.id` | `ON DELETE SET NULL` | Not affected by cleanup. |
| `practical_submissions.quiz_attempt_id → quiz_attempts.id` | `ON DELETE CASCADE` | Deleting an attempt deletes submission rows. |
| `practical_submission_files.practical_submission_id → practical_submissions.id` | `ON DELETE CASCADE` | Rows cascade; **files on disk do not**. |
| `quiz_assignments.owner_user_id → users.id` | none declared (NO ACTION) | Unaffected. |

Foreign keys are on for every application connection
(`web/db.py:49`, `connect_db`).

### 2.3 Question plans

- A plain assignment has `question_plan_json` NULL. Questions are picked per
  attempt by `quiz_pick_questions` and stored as a JSON list in
  `quiz_attempts.question_ids_json` (`web/services/quiz.py:2074-2076`).
- A mixed assignment stores a plan object (`question_ids`, `open_question_ids`,
  `mixed_open_enabled`, `include_open_answers_in_final_score`), validated by
  `quiz_parse_assignment_question_plan` (`quiz.py:1447`). Each attempt stores
  its own shuffled copy (`quiz_shuffle_plan_within_groups`, `quiz.py:2004`).
- `quiz_parse_attempt_question_plan` (`quiz.py:1348`) reads both shapes.

`question_ids_json` is the only record of which questions a student was shown.

### 2.4 Routes and functions

| Concern | Route | Function |
|---|---|---|
| Create | `/teacher/new` | `teacher_new` (`web/blueprints/teacher.py:384`), calling `quiz_create_assignment` (`quiz.py:240`) at `teacher.py:532` and `:545` |
| Duplicate | `/teacher/assignment/<id>/duplicate` | `teacher_assignment_duplicate` (`teacher.py:791`), new row and new token |
| Edit | `/teacher/assignment/<id>/edit` | `teacher_assignment_edit` (`teacher.py:730`); only `title_bg` and `time_limit_minutes` |
| Detail, list, dashboard | `/teacher/assignment/<id>`, `/teacher/assignments`, `/teacher` | `teacher_assignment` (`:647`), `teacher_assignments` (`:323`), `teacher_dashboard` (`:208`) |
| QR | `/teacher/assignment/<id>/qr.svg` | `teacher_assignment_qr` (`:691`) |
| Student entry | `/quiz/s/<public_token>` | `quiz_start_public` (`web/blueprints/quiz.py:41`) |
| Start or resume | — | `quiz_start_or_resume_attempt` (`quiz.py:2032`) |
| Attempt page and submit | `/quiz/attempt/<id>` | `quiz_attempt` (`quiz.py` blueprint `:94`), `quiz_submit_attempt` (`quiz.py:2164`) |
| Student result | `/quiz/attempt/<id>/result` | `quiz_result` (`:188`), `quiz_result_context` (`quiz.py:2219`) |
| My attempts | `/account/attempts` | `my_attempts` (`:85`), `fetch_account_attempts` (`quiz.py:2104`) |
| Practical upload | `/quiz/attempt/<id>/practical/<slug>/task/<id>/upload` | `quiz_practical_upload` (`web/blueprints/practical.py:174`), `_store_upload` (`:180`) |
| Teacher results and review | `/teacher/assignment/<id>/results` (GET, POST) | `teacher_assignment_results` (`teacher.py:945`) |
| CSV export | `/teacher/assignment/<id>/results.csv` | `teacher_assignment_results_export` (`teacher.py:1244`) |
| Practical review | `/teacher/practical-submissions` | `teacher_practical_submissions` (`practical.py:353`) |

### 2.5 Authorization

- Assignment management: `can_manage_assignment` (`web/access.py:100`), applied
  by `_fetch_managed_assignment` (`teacher.py:299`). Owner or admin; everything
  else is 404.
- Attempt access: `can_access_attempt` (`access.py:146`) through
  `require_quiz_attempt_access` (`web/attempt_access.py:33`). Only the owning
  account; no teacher or admin bypass. Historical attempts (`user_id` NULL) are
  reachable only from the browser holding the session marker.
- Starting: `can_take_quizzes` (`access.py:131`).
- TOTP applies to admin accounts only (`016_admin_totp.sql`).
- State-changing routes carry `@integrity_policy(SAME_ORIGIN)`.

### 2.6 Timekeeping

- Stored timestamps come from SQLite `CURRENT_TIMESTAMP`: UTC, naive,
  `YYYY-MM-DD HH:MM:SS`, second resolution.
- The application clock is `quiz_current_timestamp` (`quiz.py:1729`), naive
  UTC. Tests patch it.
- `quiz_parse_timestamp_utc` (`quiz.py:1583`) parses stored values.
- `login_throttle.py:32` and `totp.py:102` write the same UTC format.
- **No display timezone conversion exists.** Templates print raw UTC strings
  (`teacher_assignment.html:241-242`, `my_attempts.html:45-46`, and others).

### 2.7 Existing gaps this design must account for

1. **The time limit is not enforced on the server.** `quiz_attempt`
   (`quiz.py` blueprint `:129-157`) computes `quiz_remaining_seconds` only to
   allow an empty timer-driven submission. A POST after the timer has expired is
   accepted and graded.
2. **Submission has no state guard in SQL.** `quiz_submit_attempt` updates
   `WHERE id = ?` (`quiz.py:2207-2213`) and upserts `quiz_answers`. The only
   protection is the earlier `if attempt["submitted_at"]` read in the route
   (`:103`), which is not in the same transaction. Two concurrent POSTs can both
   pass it; the second overwrites the first.
3. **No server-side draft.** Answers before submission exist only in the
   browser (`web/static/js/quiz-draft-autosave.js`, `localStorage`). An
   unsubmitted attempt has no answer rows.
4. **No persisted "pending review" state.** `quiz_text_answers.is_correct` is
   `NOT NULL DEFAULT 0`, so `prepare_quiz_text_answer_review` (`quiz.py:1942`)
   never reaches its `"pending"` branch for stored rows. "Reviewed by a teacher"
   is `teacher_override = 1` with a valid `teacher_override_status`.
5. **The assignment vault note hard-codes `"status": "open"`**
   (`quiz.py:1799`).
6. Production runs one sync Gunicorn worker with one thread
   (`deploy/gunicorn.conf.py:17-19`). Races are unlikely there today, but the
   design must be correct with several workers and with a concurrent CLI.

### 2.8 Authoritative and derived data

| Data | Status | Source of truth |
|---|---|---|
| `quiz_assignments` row | Authoritative | SQLite |
| `quiz_attempts` row, including `question_ids_json`, `seed`, `score_*` | Authoritative | SQLite |
| `quiz_answers`, `quiz_text_answers` | Authoritative | SQLite |
| Practical upload rows and file bytes | Authoritative | SQLite and the upload root |
| Open subtotal, combined score, percent, difficulty breakdown | Derived on each read | `quiz_text_answer_informational_subtotal` (`quiz.py:839`), `quiz_combined_score_summary` (`:863`) |
| Option order shown to the student | Derived | `seed` + `quiz_shuffle_options` (`quiz.py:1205`) |
| Question text, options, correct answers, accepted answers | Authoritative elsewhere | canonical content tables; not copied into attempts |
| `vault/Generated/Quizzes/*.md`, `.../attempts/*.md` | Derived, best-effort | `quiz_write_assignment_note` (`quiz.py:1749`), `quiz_write_attempt_note` (`:1826`); write failures are swallowed |
| CSV export | Derived on request | `teacher_assignment_results_export` |
| Backup snapshots | Copy | `src/backup_application.py` |

Two consequences:

- The combined score of a mixed quiz is **not stored**. It exists only while
  `quiz_text_answers` rows exist. Deleting them without first recording the
  final score loses it.
- Vault attempt notes contain the student's name, answers and score. They are a
  second copy of detailed data outside SQLite.

---

## 3. Lifecycle state machine

### 3.1 Persisted facts (proposed)

Additive, nullable columns on `quiz_assignments`. No table rebuild.

| Column | Added in | Meaning |
|---|---|---|
| `deadline_at` | PR #326 | UTC `YYYY-MM-DD HH:MM:SS`. The **exclusive** cutoff: the assignment is closed from this instant. NULL means no deadline. |
| `display_timezone` | PR #326 | IANA timezone name the teacher selected (§5.3). Stored for **every** new assignment, with or without a deadline, so closure and cutoff times can always be shown in a named zone. Display, editing and audit only; never used in a time comparison. |
| `closed_at` | PR #326 | UTC instant of the manual close, exactly as sampled under the writer lock (§6.1). Never adjusted. Write-once. Terminal. |
| `closed_by_user_id` | PR #326 | `REFERENCES users(id)`. Who closed it. Audit only; always the server-side principal. |
| `expiry_observed_at` | PR #326 | UTC instant at which a writer transaction first observed that `deadline_at` had passed. Write-once. Terminal for starts and edits. **Not** the closure time; never used for retention. |
| `submissions_closed_at` | PR #326 | UTC instant at which a writer transaction first observed that the grace window had ended (`now >= E + G`). Write-once. Terminal for every student detail write, **independent of the clock**. No recovery or restore procedure writes it without that observation. |
| `retention_base_at` | PR #326 | The retention floor (§3.9): an optional later start for the retention clock. Set only by operator tooling in restore reconciliation or clock-incident recovery when `E` cannot be trusted (§3.7, §13.4). Can only be raised. |
| `lifecycle_hold` | PR #326 | The **set of unresolved hold reasons** (§3.8); NULL when there is none. While non-NULL the assignment is **not served, and not cleaned by any ordinary run**; from PR #328 one operator path re-cleans an assignment held only for `restore_reclean` (§13.4). Reasons are added and resolved one at a time, only by operator tooling; nothing overwrites the set. |
| `details_cleaned_at` | PR #328 | UTC instant at which cleanup committed the **database** deletion for this assignment. Write-once. Terminal. It does not assert that every file was unlinked (§10.6). |
| `closure_upper_bound_at` | PR #328 | A UTC instant, established from evidence outside the host, that is not earlier than the moment the assignment's closure was persisted (§10.1). Written only by the cleanup tool. Cleared when a hold reason is added and by a restore. A clock-health observation (§3.9), not a lifecycle event, and never an input to `E`. |

One single-row table, `lifecycle_clock` (PR #326), holds the clock-health
state shared by every process (§3.6). PR #328 adds a second single-row table,
`database_identity`, which names the database for backup provenance (§10.8).

No `status` column. A stored status would need a job to keep it true, which
decision A forbids relying on.

### 3.2 Computed values

```
E  = effective closure instant
   = min of the non-NULL values among (deadline_at, closed_at); NULL if both NULL

submit_cutoff          = E + G                                   (NULL when E is NULL)
retention_start        = max(E, retention_base_at)               (retention_base_at NULL counts as absent)
retention_eligible_at  = retention_start + R                     (R = 30 × 24 h; NULL when E is NULL)
```

`E` needs no stamping step because both inputs are immutable once the
assignment is closed (§3.5). Neither observation marker is an input to `E`: the
server may first notice an expiry long after it happened, and retention must
run from the deadline itself. `retention_base_at` never changes `E`; it only
prevents retention from being counted from an `E` that could not be verified.

All four values are statements in the server's own clock. How far the true
closure instant differs from `E` is not known to the application (§3.6).
`retention_eligible_at` is therefore the earliest instant **by the server's
clock**: it is necessary for cleanup and never sufficient. Deletion also
requires the trusted-time rule of §10.2, in which the server's clock supplies
no evidence.

### 3.3 States

Per assignment, at lifecycle time `now` (§3.4):

| State | Condition | Kind |
|---|---|---|
| `OPEN` | `closed_at` NULL **and** `expiry_observed_at` NULL **and** `submissions_closed_at` NULL **and** (`deadline_at` NULL **or** `now < deadline_at`) | computed |
| `CLOSED_MANUALLY` | `closed_at` not NULL | persisted, terminal |
| `EXPIRED` | `closed_at` NULL **and** (`expiry_observed_at` not NULL **or** `now >= deadline_at`) | computed until first observed, then persisted and terminal |
| `GRACE_PERIOD` | closed **and** `submissions_closed_at` NULL **and** `now < submit_cutoff` | computed window inside closed |
| submissions ended | `submissions_closed_at` not NULL, or closed and `now >= submit_cutoff` | computed until first observed, then persisted and terminal |
| `RETENTION_ELIGIBLE` | closed and `now >= retention_eligible_at` and not `CLEANED` | computed window inside closed |
| `CLEANED` | `details_cleaned_at` not NULL (from PR #328) | persisted, terminal |

`EXPIRED` and `CLOSED_MANUALLY` differ only in cause; enforcement treats both
as **closed**. `RETENTION_ELIGIBLE` only means "old enough by the server's
clock"; whether cleanup may act is decided solely by §10.2, which also needs
trusted time. A non-NULL `lifecycle_hold` overrides every state for serving,
without exception, and for cleanup with the single operator exception of
§13.4 (§3.8).

```
OPEN ──(now reaches deadline_at)──────────► EXPIRED ─────────┐
  │                                                           ├─► [grace] ─► submissions ended ─► [retention] ─► RETENTION_ELIGIBLE ─► CLEANED
  └─(teacher closes, closed_at := now)────► CLOSED_MANUALLY ──┘
```

There is no edge back to `OPEN`, and none back into grace.

**What is terminal without consulting the clock.** Four markers are write-once
and are never cleared by any code path, any operator command or any clock
movement:

| Marker set | Guaranteed, whatever the clock reads |
|---|---|
| `closed_at` | No start, no edit. |
| `expiry_observed_at` | No start, no edit, no manual close. |
| `submissions_closed_at` | No submission, no answer change, no practical upload, no other student detail write. |
| `details_cleaned_at` | Everything above, plus cleaned-state handling of results and review. Cleanup acts only on an assignment whose `submissions_closed_at` is already set (§10.2), so the rule holds even for code that predates the cleanup column and for abandoned attempt stubs that still have `submitted_at` NULL. |

**What is still computed.** An expiry, or the end of a grace window, that no
writer transaction has observed yet exists only as a comparison with the
clock. §3.6 states exactly how far that can be guaranteed.

Per attempt:

| Attempt condition | Meaning |
|---|---|
| submittable | `submitted_at` is NULL **and** the assignment has no hold **and** `submissions_closed_at` is NULL **and** (from PR #328) `details_cleaned_at` is NULL **and** (assignment `OPEN` **or** (the attempt is in flight at the closure (§7.1) **and** `now < submit_cutoff`)) |
| submitted | `submitted_at` is not NULL |
| abandoned | `submitted_at` is NULL and not submittable |

The first four conditions do not involve time. Abandoned is computed; nothing
is written to the attempt row when it becomes abandoned.

### 3.4 Time rules

1. **Lifecycle time.** `now` is `quiz_current_timestamp()` truncated to whole
   seconds, accepted only after the clock-health check of §3.6. While the
   clock state is `ok` it never decreases from one committed lifecycle write
   to the next.
2. **Sampled under the lock.** For any decision that leads to a write, `now` is
   sampled **after** `BEGIN IMMEDIATE` has returned, never before. Time spent
   waiting for the writer lock counts against the request. Read-only pages
   sample once at the start of their read transaction.
3. **One value per transaction.** The same `now` is used for every check and
   every timestamp written in that transaction.
4. **Explicit writes.** Lifecycle writes store that `now`. They do not use
   `DEFAULT CURRENT_TIMESTAMP`. This includes `quiz_attempts.started_at` and
   `submitted_at`.
5. **Precision and normalisation.** Every lifecycle timestamp is stored as
   exactly `YYYY-MM-DD HH:MM:SS`: UTC, whole seconds, zero-padded, a space
   separator, no `T`, no offset, no fractional part. One serialiser produces
   it; nothing else writes these columns.
6. **Strict parsing.** One parser reads lifecycle timestamps. It accepts only
   the format of rule 5 and only calendar-valid values (no `2026-02-30`, no
   `24:00:00`). The existing `quiz_parse_timestamp_utc` (`quiz.py:1583`) is
   lenient (ISO forms, offsets, `Z`) and is **not** used for lifecycle
   decisions.
7. **Malformed values fail safe.** A stored lifecycle timestamp that does not
   parse makes the assignment closed for student actions, frozen for teacher
   edits, and **not** eligible for cleanup. It is reported.
8. **Half-open intervals.** Open while `now < E`; closed from `now = E`. Grace
   while `E <= now < E + G`; a submission at exactly `E + G` is refused.
   Retention eligible from `now = retention_eligible_at`.
9. **Fixed durations.** `G` = 900 seconds (15 minutes, approved); `R` =
   2 592 000 seconds. Grace and retention use no calendar or local-time
   arithmetic. Calendar-day arithmetic in a named timezone is used in exactly
   two places, both before conversion to UTC: computing the default deadline
   and checking the 180-day maximum (§5.1, §5.4).
10. **Comparison.** In Python on parsed values; in SQL on the stored strings,
    which order identically because of rule 5.
11. **Start ordering.** Whether an attempt started before a closure is decided
    by the order of the two writer transactions, not by comparing equal
    timestamps (§11.2, §11.3). The stored values then satisfy
    `started_at < deadline_at` for a deadline closure and
    `started_at <= closed_at` for a manual closure (§7.1). No stored time is
    ever adjusted to make a comparison strict.
12. **Timestamp classes.** A scheduled deadline, a factual event, a terminal
    marker, a retention floor and a clock-health observation are different
    things and are checked differently (§3.9). A deadline in the future is the
    normal state of an open assignment.

### 3.5 Transition precedence

| Situation | Rule |
|---|---|
| Teacher closes before the deadline | `closed_at := now` is written only if the assignment is `OPEN` at `now`. `E` becomes `closed_at`. The later `deadline_at` stays stored, possibly still in the future, and has no further effect (§3.9). |
| Teacher closes in the last second before the deadline | Accepted: `deadline_at > now`. `closed_at = now`, and `E = closed_at`. |
| Deadline passes | Enforcement needs no write: every decision computes closed from `deadline_at`. The first writer transaction that sees it also sets `expiry_observed_at` (§6.3). |
| Grace window ends | Enforcement needs no write. The first writer transaction that sees `now >= E + G` sets `submissions_closed_at` (§6.3). |
| Teacher closes at or after the deadline | Refused as already closed. `closed_at` stays NULL, so `E` stays `deadline_at`. The refusal stamps `expiry_observed_at` if it was NULL. |
| Close and deadline at the same second | `E` is that second either way. The close is refused because `now < deadline_at` is false. |
| Close in the same second as a start | Decided by the order of the two transactions under the writer lock (§11.3). Start first: the attempt exists, `started_at <= closed_at`, and it is in flight. Close first: the start inserts nothing. `closed_at` is the sampled `now` either way. |
| Deadline edited while `OPEN` | Allowed. New value must be NULL, or strictly greater than `now` and within the 180-day maximum (§5.4). |
| Two deadline edits at once | Both are conditional updates under the writer lock; the later commit wins if the assignment is still `OPEN` when it runs. |
| Any edit after closure, including the title | Refused. No column changes (§6.6). |
| Student starts at the boundary | Allowed only if `OPEN` at the `now` sampled under the lock. At `now = E` it is refused. |
| Student submits during grace | Accepted if submittable (§3.3). |
| Student submits at exactly `E + G` | Refused. Nothing is stored. `submissions_closed_at` is stamped. |
| Second submission of a submitted attempt | `already_submitted`, at any time, including after the cutoff and after cleanup (§7.5). |
| Any student write to a cleaned assignment | Refused, independent of time. |
| Cleanup at the retention boundary | Old enough by the server's clock at `now = retention_eligible_at`, and by trusted time when `F.lo` reaches the recorded closure bound plus `R` (§10.2). Both are required; one second short on either, it is not. Other conditions in §10.2 still apply. |
| Cleanup and grading at the same time | One fully precedes the other (§11.6). |
| Clock anomaly | Lifecycle writes stop; the only write is the clock-fault bookkeeping update; persisted markers stay terminal (§3.6). |

A conflict between two writes is always resolved by a conditional `UPDATE`
whose `WHERE` clause restates the precondition, never by an earlier read.

### 3.6 Clock health

The server's wall clock is the only time source the web application has, and
it can be wrong in either direction. Nothing stored in SQLite can prove that it
is right: a high-water mark shows only that time has not gone backwards
relative to an earlier reading from the same clock. A snapshot's timestamp, a
row's `created_at` and the high-water mark are all readings of that one clock
and are **not** evidence of its correctness.

**What these checks are, and what they are not.** The checks in this section
detect two kinds of fault: a regression against the high-water mark, and a
step within one boot. They bound nothing else. Passing them does not show that
the clock holds correct UTC. Neither does the operating system's
"synchronised" flag, and neither does a recent backup. An error smaller than a
tolerance, an error present since boot and an error that builds up over
several reboots all pass. Two consequences follow, and the rest of the design
depends on both:

- **Serving decisions** (has the deadline passed, has the grace window ended)
  are made with this clock and can be early or late by its undetected error.
  That is a stated limit (below), not something the checks remove.
- **No deletion is justified by this clock.** Cleanup uses the trusted-time
  rule of §10.1 and §10.2, which needs evidence from outside the host and
  takes nothing from the tolerances of this section.

**What independent evidence exists on the production host**

| Evidence | What it shows | Limits |
|---|---|---|
| The kernel's boot-time monotonic clock together with the boot id | Whether the wall clock stepped, forwards or backwards, relative to elapsed real time since a stored reference, across processes and application restarts within one boot. | Lost at reboot. Says nothing about whether the reference itself was right. |
| The operating system's report that the clock is NTP-synchronised | That the system time daemon currently holds the clock against external servers. | It is the host's own claim, and is not proof of correct UTC. The exact probe available to the service user under the unit's sandboxing must be confirmed on the production host. |
| A direct query to external time sources by the tool that needs certainty | An interval that contains true UTC, as wide as the sources' own error and the network round trip. | Needs outbound network access; must use several sources. Used only by the cleanup tool (§10.1). |
| An operator reading the time from an independent device | Human verification, with human-sized uncertainty. | Manual. |

**Shared state.** `lifecycle_clock` holds, in its single row: `high_water_at`;
a **boot anchor** (`ref_wall_at`, `ref_boot_id`, `ref_boottime_ms`); and
`state` (`ok` or `suspect`) with its reason and instant. Every lifecycle writer
transaction, in every worker and in every command-line tool, reads this row
while holding the writer lock. Because the state lives in the database and not
in process memory, it is the same for all processes and survives an
application restart.

**The boot anchor is not refreshed by ordinary writes.** It is written when
the first lifecycle write of a boot is admitted, and by the operator
confirmation of §3.7. Every later check in that boot compares the wall clock
with `ref_wall_at` plus the boot time elapsed since the anchor. What is
measured is therefore the **sum of all steps since the anchor**, not the size
of the latest one. A series of adjustments, each below the tolerance, is
caught as soon as their sum exceeds it. An ordinary write raises
`high_water_at` and nothing else. (Refreshing the reference on every write, as
an earlier draft did, would compare each reading only with the previous one
and never see such a series.) Legitimate corrections, such as a step at
start-up or a leap second, count against the same tolerance; if their sum
exceeds it the state becomes `suspect` and an operator confirms the clock.

**The row must exist and be well-formed.** The `CHECK (id = 1)` on the table
only prevents a second row; it does not make the first one exist. A missing
row, more than the expected content, or a malformed timestamp in it is itself a
clock-health failure: lifecycle writes are refused, production start-up refuses
to start (§13.5), and cleanup refuses to run.

**Check performed in every lifecycle writer transaction**

| Finding | Result |
|---|---|
| Row missing or malformed | Refuse (503). Nothing written. |
| `state = suspect` | Refuse (503). Nothing written. |
| Clock earlier than `high_water_at` by no more than a small tolerance (proposed 5 seconds; an implementation parameter) | `now := high_water_at`. Proceed. |
| Clock earlier than `high_water_at` by more | Clock fault (below). |
| Same boot as the anchor, and the clock differs from `ref_wall_at` plus elapsed boot time by more than a tolerance (proposed 60 seconds), in either direction | Clock fault. The difference is cumulative since the anchor. |
| A **factual event timestamp or terminal marker** (§3.9) of the rows being acted on is later than `now` | Clock fault. |
| A **scheduled deadline** of the row being acted on is later than `now` | Normal. Not a finding (§3.9). |
| New boot, or no anchor yet, so no monotonic comparison is possible | Require the operating system's synchronisation evidence before the first lifecycle write of that boot. If it is absent or cannot be read, refuse; after a bounded wait, clock fault. If it is present, write a new boot anchor and proceed. |
| Otherwise | Proceed; raise `high_water_at` to `now`. |

"The rows being acted on" are the assignment and, for an attempt request, that
attempt; for a manual close they include the latest `started_at` among the
assignment's attempts (§6.1). Both tolerances are detection thresholds. They
are not bounds on the clock's error.

The order is fixed: first the clock row itself (missing, `suspect`,
regression, step); then the hold (§3.8); then the timestamps of the rows being
acted on. A held assignment is refused before its own timestamps are compared
with the clock, so an assignment that is held because one of its markers lies
in the future does not raise a new clock fault each time someone requests it.

**Lifecycle time never decreases while the state is `ok`.** Every admitted
transaction has `now >= high_water_at` and raises the mark to its `now`. So of
two committed lifecycle writes, the later one never carries an earlier time.
Only the operator confirmation of §3.7 can lower the mark, and it does so in
the same transaction that holds every assignment with a factual timestamp
later than the verified time. §7.1 and §11.3 rely on this.

**Clock-fault bookkeeping: the one write that is allowed.** A detected fault
has to be remembered, or the next request would act on the same clock. And
once a fault is detected, nothing that depends on time may be written. Both
hold because exactly one statement is permitted after a fault is found:

```sql
UPDATE lifecycle_clock
   SET state = 'suspect', state_reason = :reason, state_changed_at = :wall_reading
 WHERE id = 1 AND state = 'ok';
```

- It is the only thing the detecting transaction commits. The check runs
  first, so normally nothing precedes it; anything that did is rolled back.
- It touches one row of one table. It writes no `quiz_*` row, no
  `high_water_at`, no anchor, no observation marker and no hold.
- It is idempotent: once the state is `suspect` it changes nothing.
- `state_changed_at` stores the wall reading as it was observed, for the
  operator. It is a clock-health observation (§3.9); no decision reads it.
- In the web application the detecting transaction already holds the writer
  lock. In a command-line tool the statement runs in its own
  `BEGIN IMMEDIATE`, on a connection used for nothing else. If that cannot
  take the lock, the tool still refuses, exits non-zero and says so; the next
  lifecycle transaction detects the fault again if it persists.

Wherever this document says that a refused request or a refused run "writes
nothing", this update is the single exception. Ordinary lifecycle writes
(starts, submissions, uploads, closes, edits, duplications, observation stamps
and cleanup) stay refused. The only other writer while the state is `suspect`
is the operator procedure of §3.7 and, after a restore, its restore-hold step
(§13.4): they add hold reasons, raise retention floors and, at the end, return
the state to `ok`. They write no closure and no attempt data.

**Suspect is sticky and fails closed.** While `state = suspect`:

- Every lifecycle write is refused with a 503: starts, submissions, practical
  uploads, manual closes, edits, duplications. Read pages keep working and
  show persisted state.
- This includes submissions for in-flight attempts. Accepting them would
  require measuring a 15-minute window with a clock known to be unreliable,
  which is how a grace period becomes unbounded. Students keep their browser
  draft (§7.7); the teacher's remedy is a new assignment.
- Cleanup refuses to run.
- **The clock becoming plausible again changes nothing.** Only the operator
  procedure of §3.7 returns the state to `ok`. Neither the web application nor
  the cleanup tool ever clears it on its own.

**Stated limits.** Without a trusted time source the web application cannot
bound its own error, and the design does not claim that it can:

- **An error below the tolerance.** A forward step of 30 seconds, or a sum of
  steps below the tolerance, is not detected. A deadline can take effect, and
  a grace window can end, early or late by that amount. A closure stamped that
  way is terminal.
- **An error present since boot.** The first anchor of a boot rests on the
  operating system's synchronisation flag alone. A clock that is wrong at that
  moment while the flag is set passes, whatever the size of the error, and
  small errors can add up from one reboot to the next.
- **An expiry or a grace-window end that no writer transaction ever
  observed** is protected only by the checks above. If the high-water mark is
  stale because nothing was written for days, the observation stamp was skipped
  (it is best-effort, §6.3), and the clock then moves back across a reboot in a
  way the synchronisation probe does not catch, that one assignment computes as
  open again.
- **A closure stamped while the clock was wrong and not yet detected** carries
  that error in `closed_at`.

The policy is fail-closed where it can be: every observer tries to persist the
marker; a detected fault stops all lifecycle writes; recovery holds every
assignment whose state is in doubt and manufactures no closure (§3.7). None of
these limits can cause an early deletion, because deletion does not rest on
this clock (§10.1).

### 3.7 Clock incident recovery

Operator procedure. Nothing in it happens automatically, and **nothing in it
closes an assignment or ends its submissions.**

1. **Contain.** Lifecycle writes are already refused. From PR #328, disable
   the cleanup timer.
2. **Correct and verify the clock** against a source outside the host.
3. **Inspect.** A proposed operator command prints the clock row, the reason
   and wall reading of the fault, the old `high_water_at`, and two lists:
   - **(a)** every assignment and attempt with a **factual event timestamp or
     terminal marker** (§3.9) that is later than the verified time or falls
     inside the interval the operator declares as suspect;
   - **(b)** every assignment whose state computed at the verified time
     differs from its state computed at the old `high_water_at`, with no
     persisted marker for the difference: a `deadline_at`, or an `E + G`,
     that lies after the verified time and at or before the old mark.

   A scheduled deadline in the future is not a reason to list an assignment.
4. **Manufacture nothing.** The command writes no `closed_at`, no
   `expiry_observed_at` and no `submissions_closed_at`. The high-water mark is
   one reading of a clock now known to have been wrong. It does not show that
   any particular assignment was ever observed as expired, so no marker is
   derived from it. Markers that were actually persisted stay, including any
   written during the incident: they are terminal (decision D). A deadline at
   or before the **verified** time has really passed; it is recorded by
   ordinary observation (§6.3) once writes resume, with the instant of that
   observation.
5. **Hold what is uncertain.** Every assignment on list (a) or (b) gets the
   hold reason `clock_review`, added to whatever reasons it already carries
   (§3.8). For the closed assignments of list (a), `retention_base_at` is
   raised to the verified current time, so a mis-stamped closure can never
   shorten the 30 days. The assignments of list (b) keep all their columns:
   this procedure neither closes them nor declares them open.
6. **Confirm.** The operator supplies the current UTC time read from an
   independent source. The command accepts it only if the local clock agrees
   within tolerance, and only in the same transaction as step 5, so the state
   cannot return to `ok` while an affected assignment is unheld. It then sets
   `state = ok`, sets `high_water_at` to the verified time (which may lower
   it), writes a new boot anchor, and logs who confirmed and when.
7. **Resolve `clock_review` one assignment at a time** (§3.8).
   - List (a): only after review, and only once lifecycle time has passed
     every factual timestamp and terminal marker of the assignment and its
     attempts. Until then it stays held. This is what keeps a closure stamped
     in the future from opening a grace window that lasts until that future
     arrives.
   - List (b): after the operator has checked the deadline, or the grace end,
     against the verified clock. Resolving changes no column. The assignment
     then computes exactly as its stored deadline says. That is not a
     reopening: no closure of it was ever persisted, and by the verified
     clock its deadline had not passed.
   - Without that review an assignment stays held. Resolving never clears a
     terminal marker.
8. **Cleanup (PR #328) resumes only by an explicit operator action**: a dry
   run, then re-enabling the timer. A forward jump that closed assignments
   early, with a persisted marker, is not undone; closures are terminal and
   the remedy is a new assignment (decision D).

### 3.8 Holds

`lifecycle_hold` is the single fail-closed switch for an assignment whose
lifecycle state cannot currently be trusted. It stores the **set of unresolved
hold reasons**. NULL means there is none.

**Encoding.** One or more reason codes from the fixed list below, in a fixed
order, separated by commas, for example `clock_review,restore_pending`. Any
non-NULL value holds the assignment, **including one that does not parse**: an
unreadable hold is still a hold, and is reported. Every guard in this document
therefore stays the single test `lifecycle_hold IS NULL`, and the reasons
travel with the row through a backup and a restore.

**Each reason is a separate blocker with its own condition.**

| Reason | Added by | Resolved only when |
|---|---|---|
| `clock_review` | Clock incident recovery (§3.7). | The conditions of §3.7 step 7 are met for this assignment. |
| `restore_pending` | The restore procedure, for every assignment (§13.4). | Its lifecycle state has been reconciled completely from sufficient evidence. |
| `restore_unreconciled` | Reconciliation that found the evidence missing, incomplete or contradictory (§13.4). | An operator has entered the state from a record the system produced at the time, and that record is kept with the resolution. |
| `restore_reclean` (from PR #328) | Restore of an assignment that had been cleaned (§13.4). | The operator re-clean of §13.4 has committed and its result has been verified. This reason is resolved last: the re-clean path applies only while it is the one reason left. |

Rules:

- **Adding never removes.** Tooling adds one reason to the set, or resolves
  one reason from it. No operation writes the column as a whole. A later
  procedure therefore cannot replace an unresolved reason with a different or
  weaker one; a restore in particular adds `restore_pending` to a
  `clock_review` that was already there.
- **Resolving one reason leaves the others.** The assignment is served again
  only when the set is empty, that is, when every applicable blocker has been
  resolved.
- **No evidence, no release.** If the condition of a reason cannot be met, the
  reason stays, indefinitely if need be. No command ends, closes or otherwise
  terminates a held assignment in order to release it (§16.3, item 17). The
  teacher's remedy is a new assignment made on the normal creation form.
- Resolving a reason never clears or changes a terminal marker and never
  lowers `retention_base_at`.
- While the set is not empty, the assignment is **not served**: the student
  entry, attempt, submission, upload and result routes and the teacher edit,
  close, duplicate, results, review and export routes for it answer with a
  temporary "unavailable" response. Authorization is still decided first, so a
  hold reveals nothing about a foreign assignment.
- While the set is not empty, the assignment is **never eligible for ordinary
  cleanup** (§10.2), and no ordinary run records a closure bound for it. From
  PR #328, adding a reason also clears `closure_upper_bound_at`, so its
  retention has to be confirmed again (§10.1).
- **One operator exception, for cleanup only.** An assignment that was
  cleaned and then restored is held with `restore_reclean` until it has been
  cleaned again, and cleaning it needs a closure bound. So when the set is
  exactly `restore_reclean`, an operator may confirm its closure and, after
  the full retention interval, re-clean it, with the hold in place throughout
  (§13.4). The exception does not serve the assignment, does not apply to any
  other reason, and does not apply while any second reason is present.
- Reasons are added and resolved only by operator tooling. No request and no
  scheduled job does either. Each addition and each resolution is logged with
  the reason, the operator, the instant and the evidence relied on.
- Notifying a teacher about a held assignment is a courtesy. The hold, not the
  notification, is what enforces access.

### 3.9 Timestamp classes

The stored times are not all the same kind of thing. Each check in this
document names the class it applies to. "No timestamp in the future" is never
applied to all of them.

| Class | Values | What it is | May be later than `now` | Role in clock checks |
|---|---|---|---|---|
| Scheduled deadline | `quiz_assignments.deadline_at` | An instant a teacher chose in advance. | **Yes. That is its normal state** for every open assignment. It also stays stored, possibly still in the future, after a manual close that came first. | None. Compared with `now` only to compute the state (§3.3). Never evidence of a clock fault. |
| Factual lifecycle event | `quiz_attempts.started_at`, `quiz_attempts.submitted_at`, `quiz_assignments.closed_at` | The server clock's reading when something happened. | No. | Later than `now`: a clock fault for the request (§3.6); listed and held by recovery (§3.7) and by deployment verification (§13.5). |
| Terminal marker | `closed_at`, `expiry_observed_at`, `submissions_closed_at`, and from PR #328 `details_cleaned_at` | A write-once fact that ends something (§3.3). `closed_at` is also a factual event. | No. | As for factual events. Never cleared, whatever the clock shows. |
| Retention floor | `retention_base_at` | A lower limit for the start of retention, written by operator tooling as the verified current time. | Not when it is written. If it is later than `now` afterwards, the assignment is simply not old enough. | None. It never makes the clock suspect. It can only delay cleanup. |
| Clock-health observation | `lifecycle_clock.high_water_at`, `ref_wall_at`, `state_changed_at`; from PR #328 `closure_upper_bound_at` and the bounds of a time fix (§10.1) | What was observed about time itself. | `high_water_at` is the subject of the regression check. `closure_upper_bound_at` is an upper bound and may exceed the server clock's reading of the same moment. | Only `high_water_at` and the boot anchor take part in §3.6. |

Other stored times (`quiz_assignments.created_at`,
`quiz_text_answers.graded_at`, `practical_submission_files.uploaded_at` and
similar) take no part in lifecycle decisions or clock checks. They are read
only by reports and by the exposure audit of §13.3.

Rules that follow from the classes:

- A scheduled deadline in the future never makes the clock suspect, never
  holds an assignment, never blocks cleanup and never fails deployment
  verification.
- A value of any class that does not parse fails safe (§3.4 rule 7).
- A deadline further ahead than validation could ever have accepted (more
  than the approved 180 days plus two days, the allowance covering the cutoff
  convention and the zone offset) is a **data anomaly**. Deployment
  verification reports it for an operator to look at. It is not a clock fault.

---

## 4. Proposed schema change (summary)

PR #326: eight nullable columns on `quiz_assignments` (`deadline_at`,
`display_timezone`, `closed_at`, `closed_by_user_id`, `expiry_observed_at`,
`submissions_closed_at`, `retention_base_at`, `lifecycle_hold`) and the
single-row `lifecycle_clock` table. This revision adds no column and no table
to PR #326: `lifecycle_hold` now stores a set of reasons in the same column,
and the clock row's reference triple is used as a boot anchor. PR #327: the
history schema, including the submission manifest (§9.5). PR #328: two
nullable columns (`details_cleaned_at`, `closure_upper_bound_at`), the
single-row `database_identity` table (§10.8) and the pending-unlink ledger
(§10.6).

All additive; no table rebuild; no backfill of deadlines for existing
assignments, which stay `OPEN` with no deadline, exactly as they behave today.

No section of this document uses a column or table before the PR that adds
it. In particular, nothing in PR #326 or PR #327 reads or writes
`details_cleaned_at`, `closure_upper_bound_at` or `database_identity`, and
neither PR contains a deletion, a time-source query or a snapshot
verification.

The full proposal, with constraints, indexes, ordering, compatibility and
rollback, is in §12 and §13.

---

## 5. Deadline semantics

### 5.1 Default (approved, P2)

The deadline offered on the creation form is **23:59 local time, 14 calendar
days ahead** (approved).

**Proposed implementation, pending confirmation (I4): an exclusive cutoff at
the next local midnight, displayed as "through 23:59".** A deadline shown as
"through 23:59 on 3 April" means the whole of that minute is usable; the
stored instant is 00:00:00 on 4 April, and the assignment is closed from that
instant. The same convention applies to any time a teacher picks: the form and
every notice show the last usable minute, and `deadline_at` stores the instant
one minute later.

Calculation of the default, in the selected timezone `Z` (§5.3):

1. `today` = the local calendar date of `now` in `Z`.
2. Displayed deadline = "through 23:59" on `today + 14 calendar days`.
3. Cutoff = local midnight starting `today + 15 calendar days`.
4. `deadline_at` = that cutoff converted from `Z` to UTC.

- Steps 2 and 3 are date arithmetic. They are **not** `now + 14 × 24 h`. The
  result does not depend on the time of day at which the form is opened.
- Worked example, `Europe/Sofia`. Created on 20 March 2026, when the zone is at
  UTC+2. Displayed: through 23:59 on 3 April 2026. DST begins on 29 March, so
  the cutoff, 4 April 00:00 local, is at UTC+3: `deadline_at` =
  `2026-04-03 21:00:00`. Applying the creation-day offset would give 22:00 and
  be wrong by an hour.
- The default is an **offer**, not a server fallback. The form carries an
  explicit choice, "with deadline" or "no deadline". A POST without a valid
  choice is a 400. The server never invents a deadline for a request that did
  not state one, and never drops one silently.
- The deadline remains optional (decision A).

If I4 is not confirmed, the alternative is to store 23:59:00 itself as the
cutoff, which makes the last displayed minute unusable. Nothing else in the
design depends on the choice.

### 5.2 Storage

- `deadline_at`: the exclusive cutoff, UTC, `YYYY-MM-DD HH:MM:SS`, as defined
  in §3.4 rule 5. NULL when there is no deadline.
- `display_timezone`: the IANA name the teacher selected (§5.3). Stored for
  every assignment created from PR #326 on, **including those without a
  deadline**.
- Both are written only by the lifecycle serialiser from parsed values, never
  from raw form text.
- Enforcement uses `deadline_at` alone. The stored zone is for display,
  editing and audit; it never takes part in a time comparison.

### 5.3 Timezone (approved, P2; implementation proposed, I1 and I2)

**Current state.** The application has no timezone concept: no setting, no
`users` column (`012_users.sql`), no use of `zoneinfo`, and templates print raw
UTC strings (§2.6).

**Smallest implementation for PR #326 (proposed, pending confirmation).**

1. **I1 — per assignment.** A timezone selector on the create, duplicate and
   edit forms: a list of IANA names, validated server-side against
   `zoneinfo.available_timezones()`. An unknown or missing name is a 400. The
   selection is required whether or not a deadline is chosen, and is stored in
   `quiz_assignments.display_timezone`.
2. **I2 — editable preselection.** The selector is preselected to
   `Europe/Sofia`, or to the zone of the teacher's most recent assignment when
   one exists. The teacher can change it; it is always visible and always
   submitted explicitly.
3. Standard library `zoneinfo` only; no new dependency.

A per-account timezone preference (a `users` column and a settings page) is the
alternative to I1. It is more work and is not proposed.

**Why assignments without a deadline keep a zone.** They still have times to
show: a manual close, the grace cutoff after it, and the retention dates.
Without a stored zone those would have to be shown in UTC or in a guessed zone.

**The server timezone is never used.** No lifecycle code reads the operating
system's zone, calls a naive local "now", or converts without naming a zone. If
the zone database is unavailable, the form fails with an error. It does not
fall back to UTC or to the server zone.

**Assignments created before PR #326** have `display_timezone` NULL. Their
times are shown in the application default zone with the zone named, and the
teacher is asked to select a zone the first time they edit the assignment.

**DST handling**, for typed values and for the computed default alike:

| Local time | Rule |
|---|---|
| Does not exist (spring gap) | A typed value is rejected with a Bulgarian validation message. A computed cutoff that falls in a gap (a zone that changes at midnight) moves forward to the first valid local instant. |
| Occurs twice (autumn overlap) | Resolved to the **earlier** instant. |
| Ordinary | Converted directly. |

**Display.** Deadlines, closures and cutoffs are shown to teachers and students
in the assignment's stored zone, with the zone named. Existing raw-UTC columns
in templates are out of scope for PR #326 unless they sit next to a lifecycle
time.

### 5.4 Validation and the 180-day maximum (approved, P3)

All validation is server-side, in the write transaction, with `now` sampled
after the lock.

1. The choice is explicit: a deadline, or "no deadline". A zone is always
   required.
2. The value parses strictly as a local date and time in the selected zone
   (§5.3 for DST).
3. **Lower bound.** The stored cutoff is strictly greater than `now`.
4. **Upper bound (180 days, approved).** With `today` the local date of `now`
   in the selected zone, and `d` the local date of the **displayed** deadline
   (its last usable minute):

   ```
   accepted  ⇔  d <= today + 180 calendar days
   ```

   - "Through 23:59" on `today + 180` is the latest acceptable deadline. It is
     accepted. Its stored cutoff is local midnight starting `today + 181`.
   - Any displayed time on `today + 181` or later is rejected with a validation
     message; nothing is stored.
   - The comparison is made on **local calendar dates before conversion to
     UTC**, the same arithmetic as the default. A DST change cannot move the
     boundary by an hour, and the default (`today + 14`) is always inside it.
5. **I3 (proposed, pending confirmation).** `today` is the date on which the
   deadline is set or edited, not the assignment's creation date. The
   alternative, counting from creation, caps the total life of an assignment
   with a deadline at 180 days.
6. "No deadline" is always acceptable and is not subject to the maximum.
7. Stored at second resolution; the form supplies minutes.

### 5.5 Duplication (approved, P5)

`teacher_assignment_duplicate` (`teacher.py:791`) is a single POST without
fields today (`teacher_assignment.html:33`). In PR #326:

- The duplicate control becomes a small form with the **same deadline or
  no-deadline choice and timezone selector as creation**, prefilled with the
  §5.1 default.
- The POST is validated exactly like creation (§5.4). A POST without a valid
  choice is a 400 and creates nothing. There is no code path that creates a
  copy with an implied deadline.
- The source's `deadline_at` and `display_timezone` are **never copied**, even
  when the source is still open. The source's `closed_at`,
  `closed_by_user_id` and `expiry_observed_at` are never copied.
- The teacher may choose "no deadline" for the copy whatever the source had.
- The copy is a new row with a new id, the acting account as owner and a new
  `public_token` (`quiz_create_assignment`, `quiz.py:240`), as today. Attempts
  are not copied.
- Authorization and request integrity are unchanged: `ASSIGNMENT_OWNER`,
  `_fetch_managed_assignment`, `SAME_ORIGIN`.

### 5.6 No deadline

`deadline_at` NULL; `display_timezone` set as for any other assignment. The
assignment stays `OPEN` until a teacher closes it. `E` is NULL, so there is no
grace window and no retention clock. Its detailed data is never eligible for
cleanup. That is the direct consequence of decision C and is recorded as an
open policy question in §16, item 6.

### 5.7 No dependency on a background job

Every read and write that depends on the state computes it from the stored
columns and `now`. Nothing waits for a job to mark an assignment expired. If no
maintenance ever runs, a passed deadline still closes the assignment for every
request.

Proposed single source: one pure function in `web/services/` that takes the
assignment row and `now` and returns `E`, the state, `submit_cutoff` and
`retention_eligible_at`. Routes, templates, the vault note writer and the
cleanup CLI all use it. No route re-implements the comparison.

---

## 6. Closure semantics

### 6.1 Manual close

Proposed route: `POST /teacher/assignment/<id>/close`, with
`@access_policy(ASSIGNMENT_OWNER)`, `@integrity_policy(SAME_ORIGIN)` and
`_fetch_managed_assignment`, exactly like edit and duplicate. A foreign or
missing assignment is the same 404 as today.

```sql
BEGIN IMMEDIATE;
-- clock-health check; :now sampled here, after the lock (§3.4, §3.6)
UPDATE quiz_assignments
   SET closed_at = :now, closed_by_user_id = :principal_user_id
 WHERE id = :id
   AND lifecycle_hold IS NULL
   AND closed_at IS NULL
   AND expiry_observed_at IS NULL
   AND submissions_closed_at IS NULL
   AND (deadline_at IS NULL OR deadline_at > :now);
-- rowcount 1: closed now.
-- rowcount 0: re-read the row in this transaction and classify it (table below).
COMMIT;
```

- **`closed_at` is the sampled `now`, unchanged.** It is not moved past any
  attempt's `started_at`, and it is never later than the clock. The value
  stored is the instant at which the close actually took effect.
- **The predicate compares the deadline with the value that is stored.** A
  close at any instant before the deadline, including its last second,
  therefore updates exactly one row.
- `rowcount = 1`: closed now, and `E = closed_at`.
- `rowcount = 0` is never answered with "closed" without looking. The handler
  re-reads the row inside the same transaction and acts on what it finds:

  | Row as re-read | Meaning | Response |
  |---|---|---|
  | `lifecycle_hold` is not NULL | Held. | 503 (§3.8). Nothing written. |
  | `closed_at` is not NULL | Closed earlier by a manual close. | Redirect to the detail page, which shows the closed state. Nothing written. |
  | `expiry_observed_at` or `submissions_closed_at` is not NULL | Closed earlier by expiry. | The same redirect. Nothing written. |
  | `deadline_at <= :now`, no marker | The deadline has passed and no transaction has recorded it yet. | Stamp `expiry_observed_at` (§6.3), then the same redirect. `closed_at` stays NULL and `E` stays `deadline_at`. |
  | None of the above | The row is `OPEN` at `:now`, yet the update matched nothing. With the statement above this cannot happen. | Roll back and fail with a server error, logged. The response never claims a closure that was not persisted. |

- An attempt of this assignment whose `started_at` is later than `:now` is a
  factual timestamp ahead of the clock. The request stops as a clock fault
  (§3.6) before the update.
- Which attempts count as started before the close is decided by the order of
  the transactions, not by adjusting a timestamp (§7.1, §11.3).
- `:principal_user_id` is `current_principal().user_id`. The request body
  carries no field that is read for any lifecycle or audit column.

### 6.2 Idempotence

Repeating the close changes nothing. `closed_at` is never overwritten, so `E`
and the retention clock cannot move by clicking again.

### 6.3 Effective closure and the observation markers

`E` is derived, and is accurate without a recording step:

- For a deadline closure, `E = deadline_at`, a value fixed before it passed.
- For a manual closure, `E = closed_at`, written in the statement that
  performed the closure.

Separately, writer transactions record what they observe:

```sql
-- a passed deadline
UPDATE quiz_assignments
   SET expiry_observed_at = :now
 WHERE id = :id AND closed_at IS NULL AND expiry_observed_at IS NULL
   AND deadline_at IS NOT NULL AND deadline_at <= :now;

-- an ended grace window (:cutoff = E + G, computed from the row just read)
UPDATE quiz_assignments
   SET submissions_closed_at = :now
 WHERE id = :id AND submissions_closed_at IS NULL
   AND :cutoff <= :now;
```

- The markers make the closure and the end of submissions terminal against any
  later clock movement (§3.3). Neither is an input to `E`, so a late
  observation cannot shorten or lengthen retention.
- Enforcement does not depend on them. With a healthy clock, behaviour is
  identical whether or not a marker has been written.
- **Who stamps.** Every writer transaction that evaluates the assignment
  (start, submit, upload, close, edit, review, cleanup), and any request,
  including a `GET` of the entry, attempt or teacher page, that computes a
  closure or an ended grace window and finds the marker NULL. The stamp records
  what that transaction observed with a clock in state `ok`, takes no user
  input and is idempotent.
- **No stamp without an observation.** A marker is written only by a
  transaction that itself compared the assignment's own deadline, or its own
  `E + G`, with `now`. Nothing stamps a marker for a set of assignments from a
  global value such as the high-water mark (§3.7). No marker is stamped while
  the clock state is `suspect` or the assignment is held.
- **Stamping is best-effort.** If it cannot take the lock, it is skipped and
  the next observer retries. A skipped stamp never changes the response to the
  current request, which is decided by the comparison. The consequence of a
  stamp that is never written is stated in §3.6.
- Once `expiry_observed_at` is set, the manual-close and edit predicates fail,
  so `deadline_at` and `closed_at` can no longer change and `E` is frozen.

### 6.4 No reopening

- No route, CLI command or admin action sets `closed_at`, `expiry_observed_at`
  or `details_cleaned_at` back to NULL.
- No route changes `deadline_at` once the assignment is closed.
- **Field whitelist.** The edit handler reads exactly three inputs: the title,
  the time limit and the deadline choice (a deadline value or "no deadline").
  It is the only place a client-supplied `deadline_at` is accepted, and only
  while the assignment is `OPEN`. `closed_at`, `closed_by_user_id`,
  `expiry_observed_at`, `details_cleaned_at`, `owner_user_id` and
  `public_token` are never read from any request, as a submitted owner is
  already ignored today (`teacher.py:775`).
- Admin authority (`can_manage_assignment` returns true for admins) grants the
  same actions as the owner and no extra one.
- `src/manage_accounts.py` and `src/manage_assignment_tokens.py` get no
  lifecycle options.
- Two tests pin this, and neither is accepted alone (§15, L37). A static check
  confines every assignment of a lifecycle column to the lifecycle module and
  the reconciliation command, and finds none that sets a terminal marker to
  NULL. A behavioural test runs every lifecycle-writing function and every
  operator command against assignments in each closed state and finds no
  marker cleared or changed.
- Serving traffic with application code that does not enforce the lifecycle is
  a reopening and is prohibited (§13.2).

Direct SQL by an operator on the server remains physically possible. It is out
of policy and is not a supported path. The reconciliation command of §13.4
writes lifecycle state only from evidence. It can add a closure that the
evidence shows; it never clears or changes a terminal marker, and it changes
`deadline_at` only on a restored row that carries no terminal marker.

### 6.5 Correcting a closed assignment

The existing duplicate route already creates a new row with a new owner check
and a new `public_token` (`quiz_create_assignment`, `quiz.py:240`;
`write_with_new_public_token`, `web/assignment_tokens.py:46`). It is the
correction path. Duplicating a closed assignment stays allowed. Attempts are
not copied.

### 6.6 Editing (title freeze approved, P4)

| When | `title_bg` | `time_limit_minutes` | `deadline_at` and zone |
|---|---|---|---|
| `OPEN` | editable | editable | editable: later, earlier (still `> now`, within the 180-day maximum), or removed |
| closed, by manual close or by expiry | **frozen** | frozen | frozen |

```sql
BEGIN IMMEDIATE;
-- clock-health check; :now sampled here
UPDATE quiz_assignments
   SET title_bg = :title, time_limit_minutes = :limit,
       deadline_at = :deadline, display_timezone = :zone
 WHERE id = :id
   AND lifecycle_hold IS NULL
   AND closed_at IS NULL
   AND expiry_observed_at IS NULL
   AND submissions_closed_at IS NULL
   AND (deadline_at IS NULL OR deadline_at > :now);
-- rowcount 0: closed (or held: 503); stamp the observation if the deadline has passed
COMMIT;
```

- The freeze is enforced by this predicate, not by hiding the form. A direct
  POST to the edit endpoint for a closed assignment changes nothing and gets
  the detail page with a Bulgarian "the test is closed" message and status 409.
- It applies identically after a manual close and after expiry, including an
  expiry no request has observed yet.
- Owner or admin authorization is unchanged and is decided first: a foreign
  assignment is a 404 before any lifecycle message.
- The endpoint reads only the whitelisted inputs of §6.4. Forged lifecycle or
  audit fields are ignored whether the assignment is open or closed.

Extending a deadline while the assignment is still open is not a reopening.
Removing it is not either: the assignment was never closed.

### 6.7 The public link after closure

The token keeps resolving. The entry page shows a closed notice instead of the
start form. Returning 404 would make a closed test indistinguishable from a
mistyped link for a student who is entitled to see their result.

---

## 7. In-flight grace period

**Approved (P1): `G` = 15 minutes.** Not implemented by this PR.

- Grace begins at effective closure `E` and ends at `E + 15 minutes`.
- Only attempts validly started before closure qualify (§7.1).
- A submission at exactly `E + 15 minutes` is rejected (§3.4 rule 8).
- Grace does not extend retention eligibility (§7.10).
- The ordering of a start against a closure is enforced transactionally
  (§11.2, §11.3).

`G` is one named constant in code, so every boundary derives from a single
value.

### 7.1 Eligibility

An attempt is **in flight** at a closure if it was started before that
closure. "Before" is decided by the order of the two writer transactions
(§11.2, §11.3). The stored values record the outcome:

| Closure | In flight ⇔ | Why this comparison is right |
|---|---|---|
| By deadline (`E` is `deadline_at`) | `started_at < deadline_at` | The start's insert requires `deadline_at > now` and stores `started_at = now`. A start in the deadline second is refused, so equality never belongs to a valid attempt. |
| Manual (`E` is `closed_at`) | `started_at <= closed_at` | The close stores the sampled `now` unchanged (§6.1). An attempt that exists when the close commits was inserted earlier under the writer lock, at a lifecycle time that is not later (§3.6). After the close commits, the insert guard `closed_at IS NULL` admits no further start. So an equal second can only mean "started first". |

If both `deadline_at` and `closed_at` are set and equal, which the close
predicate rules out, the stricter comparison applies.

Every attempt created by lifecycle-enforcing code is in flight at the closure
of its assignment. The comparison is still kept as an explicit check:

- An attempt created by non-enforcing code after the closure, during a
  prohibited exposure (§13.3), fails it and cannot be submitted.
- **Limit of the check.** Stored values cannot order a start and a manual
  close that carry the same second. Under enforcing code nothing depends on
  that, because the insert guard has already fixed the order. A row written by
  non-enforcing code in the very second of a manual close would pass; the
  exposure audit lists such rows as ambiguous (§13.3). An exact record, such
  as the highest attempt id at the moment of the close, would need another
  column and is not proposed.

No stored time is adjusted to make either comparison come out. In particular
`closed_at` is never advanced past a `started_at`.

### 7.2 Starting

`quiz_start_or_resume_attempt` currently inserts without any state check.
Proposed order inside one `BEGIN IMMEDIATE` transaction, with `now` sampled
after the lock is held:

1. Look up the account's existing attempt. If found, return it. Resuming is
   never blocked by closure.
2. Insert with `started_at = now`, conditional on the assignment being `OPEN`
   (the single statement of §11.2).
3. If nothing was inserted, stamp the expiry observation if applicable, commit,
   and return a "closed" outcome.

The unique index `uniq_quiz_attempts_user` still resolves a double start to one
row.

### 7.3 Resuming and submitting after closure

| Request | Attempt state | Response |
|---|---|---|
| GET entry page, no attempt | — | Closed notice. No start form. |
| POST entry page, no attempt | — | 409 with the closed notice. Nothing created. |
| GET or POST entry page, existing attempt | any | Redirect to the attempt or its result, as today. |
| GET attempt | submittable, in grace | The form, with a visible closure notice and the cutoff time. |
| POST attempt | submittable, in grace | Graded and stored as a normal submission. |
| GET attempt | abandoned | Closed notice. No form, no questions. |
| POST attempt | abandoned | 409 with the closed notice. Nothing stored. |
| GET attempt or result | submitted | The result, as today. |

### 7.4 Submission cutoff and bypass prevention

The decision is made by the server, inside the write transaction of §11.4.

- The cutoff never comes from the form. `submission_reason`, the client timer
  and any hidden field are ignored for this decision.
- `submitted_at` is the server's `now`, not a client value.
- The grace window does not restart. It is `[E, E + G)` for everyone, whenever
  the student opens the page.
- Practical uploads use the same predicate. `_store_upload` already re-checks
  `submitted_at IS NULL` under `BEGIN IMMEDIATE` (`practical.py:256-261`); the
  submittable check is added beside it.
- Answer rows are inserted only after the checks, in the same transaction, so
  a refused submission leaves no rows.

### 7.5 Submission outcomes

The submission service returns exactly one of three outcomes. They are checked
in this order, after the writer lock is held:

| Order | Outcome | Condition | Effect | Response |
|---|---|---|---|---|
| 1 | `already_submitted` | `submitted_at` is not NULL | No write of any kind. No grading, no history refresh, no vault note. | Redirect to the result. |
| 2 | `closed` | not submittable (§3.3) | No answer rows. Observation markers may be stamped. | 409 with the closed notice. |
| 3 | `submitted` | otherwise | Answers written, graded, attempt marked, history written (from PR #327), all in one transaction. | Redirect to the result. |

A held assignment or an unhealthy clock is neither of these: the request is
refused with a 503 before the outcome is evaluated, and nothing is written.

The `closed` condition, spelled out, in the order it is evaluated:

| Check | Depends on the clock |
|---|---|
| `submissions_closed_at` is not NULL | No |
| (PR #328) `details_cleaned_at` is not NULL | No |
| assignment closed and the attempt is not in flight (§7.1) | No |
| assignment closed and `now >= E + G` | Yes; stamps `submissions_closed_at` |

- Checking `already_submitted` first means a duplicate POST for a submitted
  attempt gets its result at any time, **including after the grace cutoff**
  and after cleanup. It is never answered with "closed".
- A retry of a POST whose first attempt committed but whose response was lost
  is the same case: it finds `submitted_at` set and is redirected to the
  result. Nothing is written twice.
- Only the request that returns `submitted` writes final answers, grades,
  writes history and triggers the post-commit vault notes
  (`web/blueprints/quiz.py:166-168`). The route calls the note writers only for
  that outcome.
- If anything fails before the commit, the transaction rolls back: no answer
  rows, `submitted_at` still NULL, no note. The student may retry while still
  permitted.
- A cleaned assignment's abandoned attempt keeps `submitted_at` NULL if its row
  is retained as a stub (§9.6). It is rejected by the first two checks, which
  do not involve time.
- Practical uploads use the same four checks (§11.14).
- This closes existing gap 2 in §2.7 and is required work in PR #326 (§11.5).

### 7.6 Interaction with the time limit

- The lifecycle cutoff is independent of `time_limit_minutes`. A student with
  40 minutes left on the timer when the teacher closes has 15 minutes, not 40.
- The time limit can only make the window shorter, and only if server-side
  timer enforcement is added. That is existing gap 1 and is a separate decision
  (§16, item 2). The lifecycle phases do not change timer behaviour silently.
- The attempt page shows the earlier of the timer end and `E + G` when in
  grace.
- `G` is far below `R`; the cleanup predicate uses `max(R, G)` in both of its
  time rules regardless, and requires the end of submissions to be persisted
  (§10.2).

### 7.7 Saved answers and abandoned attempts

- There are no server-side drafts (§2.7, gap 3). After the cutoff, answers that
  were only in the browser cannot be submitted. The browser draft stays in
  `localStorage`; the server does not read it.
- An abandoned attempt keeps its row with `submitted_at` NULL and
  `score_correct` NULL. It is **not** auto-submitted and not graded as zero.
- Teachers see it as "not submitted" (the existing `unsubmitted` filter,
  `QUIZ_RESULTS_ALLOWED_STATUS`, `teacher.py:837`).

### 7.8 Manual closure while a student is active

The student keeps working until the next request. That request finds the
assignment closed and the attempt in grace. The page then shows the notice and
cutoff. Proposed: the attempt page also receives `submit_cutoff` at render
time, so a page rendered before a deadline can warn in the browser. That is
display only; enforcement stays on the server.

A manual close gives the student no advance warning. The grace window exists
for exactly this case.

### 7.9 Student notices (Bulgarian, wording to be finalised in the UI PR)

- Entry page, closed: the test is closed and no longer accepts new attempts.
- Attempt page, in grace: the test has closed; submit by the stated time.
- Attempt page, after cutoff: the time to submit has passed.

### 7.10 Grace and the retention clock

Retention is measured from `E`, never from `E + G` and never from
`submitted_at`. A submission during grace does not move any clock. Because
`G < R`, every grace window has ended long before eligibility.

The same holds for the trusted-time rule of §10.2. Its starting point is the
confirmed closure (§10.1), which is recorded without regard to the grace
window or to any submission.

---

## 8. Grading and results

### 8.1 Effect of closure

| Item | Effect of closure |
|---|---|
| Multiple-choice grading | None. Graded at submission by `quiz_submit_attempt`. |
| Automatic open-answer matching | None. Recorded at submission by `quiz_record_planned_open_text_answers` (`quiz.py:876`). |
| Teacher review of open answers | **Stays allowed after closure**, until `CLEANED`. Closure ends student input, not teacher grading. |
| Mixed-quiz combined score | Still derived on read. Changes when the teacher reviews. |
| Teacher results page, CSV | Unchanged until `CLEANED`. |
| Student result page | Unchanged until `CLEANED`. |
| Unsubmitted attempts | Listed as not submitted. No score. |
| Late submissions | Do not exist as a category. A POST after the cutoff is refused and stores nothing. It is logged (attempt id, assignment id, instant) without answer content. |

Proposed additions for results and CSV: the assignment's `deadline_at`,
`closed_at` and computed state. CSV columns are appended to
`QUIZ_RESULTS_EXPORT_COLUMNS` (`teacher.py:1185`), never inserted, so existing
consumers keep their positions.

### 8.2 Pending manual grading

The schema has no "pending" state (§2.7, gap 4). The definition is **not
settled** (§16, item 5). Proposed:

An attempt has **pending manual grading** if either

- it is submitted and has a `quiz_text_answers` row with `is_correct = 0` and
  `teacher_override = 0` (no automatic match and no teacher decision), or
- it has a `practical_submissions` row with at least one non-deleted file,
  **whether or not the attempt was submitted**. Uploads are accepted only while
  the attempt is unsubmitted (`practical.py:256-261`), so practical files can
  belong to an attempt that was later abandoned. Practical review has no
  persisted outcome today (§2.1), so such work can never be shown to be graded
  and is always pending until that is fixed (§14.4, §16 item 14).

An automatically matched open answer counts as graded. A teacher may still
override it; that is not pending work.

### 8.3 Pending grading and cleanup

- Pending grading **blocks cleanup of the whole assignment**. Cleanup never
  removes part of an assignment's detail.
- The 30 days are a minimum. Pending grading can only make retention longer.
- A blocked assignment is reported in every cleanup summary with its count of
  pending attempts, so it does not wait unnoticed.
- No automatic expiry of pending grading is proposed. Whether there should be a
  maximum hold is a product and legal question (§16, item 7).

### 8.4 After cleanup (PR #328)

- The teacher results page and CSV show the retained summary per attempt and
  state that the detailed answers were removed on a given date.
- The review POST is refused for a cleaned assignment (§11.6).
- The student result page shows the retained summary instead of the
  per-question breakdown. `quiz_result_context` must not present missing
  answer rows as unanswered questions.
- **The student's result URL keeps working and stays private.**
  `/quiz/attempt/<id>/result` and `/account/attempts` must resolve after
  cleanup with exactly the authorization of today: the owning account, or for a
  historical attempt the browser holding its session marker
  (`quiz_attempt_owner:<id>`, `web/attempt_access.py:24`). This requires one of
  the two designs in §9.6. Cleanup may not delete an attempt row until one of
  them is implemented and verified.
- All three views read inside one snapshot (§11.7).

### 8.5 Authorization is unchanged

No lifecycle route or state adds a way to reach an attempt. Attempt pages stay
behind `require_quiz_attempt_access`. Teacher pages stay behind
`_fetch_managed_assignment`. The history design (§9) must keep the same rule:
a student reads only their own history, a teacher only that of assignments they
own, an admin all.

---

## 9. Retention boundary and durable history

### 9.1 Two categories

**A. Detailed operational data. May be deleted after retention.**

- `quiz_answers` rows.
- `quiz_text_answers` rows, including raw answers and teacher notes.
- `practical_submissions`, `practical_submission_files` rows and the files
  under the upload root.
- The per-attempt question list, order and seed (`question_ids_json`, `seed`),
  if the history design does not need them.
- Vault attempt notes, subject to §10.7.

**B. Durable learning history. Must survive.**

- Which account took which assignment, or that the attempt has no account.
- Subject, section and topic context.
- Assignment identity: title, kind (plain or mixed), question count.
- `started_at`, `submitted_at`, or the fact that it was never submitted.
- Final score: multiple-choice score, open subtotal and combined score where
  the plan enabled it.
- Whether the final score was automatic or teacher-reviewed.
- Compact performance information, for example per-difficulty and per-topic
  correct/total counts.

Deleting category A is never a deletion of category B. Cleanup is defined as
"remove detail whose durable record is already complete and verified", not as
"remove old attempts".

### 9.2 What a history record must be able to say without category A

Each of these is derived from detail rows today and disappears with them:

| Fact | Derived from today |
|---|---|
| Open subtotal, combined score | `quiz_text_answers` (`quiz.py:839`, `:863`) |
| Teacher-reviewed or automatic | `quiz_text_answers.teacher_override` |
| Per-difficulty breakdown | `quiz_answers` joined to `questions` (`quiz.py:1707`) |
| Per-topic performance | `quiz_answers` joined to `question_topic_assignments`; not computed anywhere yet |
| Section, class, subject | join to `curriculum_sections` through `quiz_assignments.section_id` |

A history record therefore needs a **snapshot** of these values, not a
reference to rows that will be gone or to canonical rows that may be
reclassified.

### 9.3 Coverage: every attempt, not only submitted ones

A durable record is required for **every attempt whose row or details cleanup
will delete**. No category of attempt is exempt.

| Attempt | Required durable record |
|---|---|
| Submitted, account-owned | Full record: context, timestamps, scores, grading status, performance. |
| Abandoned (never submitted) | Participation record: context, `started_at`, the explicit fact "not submitted", no score. Never represented as a score of zero. |
| Historical, `user_id` NULL, submitted | Full record with no account link, the `student_name` snapshot, and a marker that it is browser-bound. |
| Historical, `user_id` NULL, abandoned | Participation record, same markers. |
| Any attempt with practical uploads, including an abandoned one | The record states that practical work was uploaded, per task, with file counts. A practical outcome is recorded only once practical grading is persisted (§14.4). |

Consequences:

- Ordinary cleanup never deletes an attempt whose required record is missing.
  "It was never submitted" and "it has no owner" are not reasons to skip the
  record.
- How a NULL-owner record is represented is open (§16, item 9). The option
  "delete historical attempts without a record" is **excluded**; the only
  alternatives are a record without an account, or never cleaning them.

### 9.4 Constraints on the future schema

Not a schema. These are requirements any history schema must meet:

1. History rows have no `ON DELETE CASCADE` path from `quiz_attempts`,
   `quiz_answers`, `quiz_text_answers` or `practical_submissions`. Deleting
   detail cannot reach history.
2. The account link is nullable (historical attempts) and, when set, is
   `REFERENCES users(id) ON DELETE RESTRICT`, matching 015.
3. One record per attempt, keyed by the attempt id, enforced by a unique key,
   so writing it is idempotent. Attempt ids are never reused: `quiz_attempts`
   is `AUTOINCREMENT` and migration 015 carried the sequence over.
4. **Two parts with different lifetimes.**
   - An **immutable context snapshot**, written once: account, `student_name`,
     assignment id and title, section slug and title, class, subject, topic
     context, plan size and kind, `started_at`. Later renames, topic
     reclassification or deletion of canonical content never change it.
   - A **mutable grading outcome**: scores, grading status, performance counts,
     last-updated instant. Refreshed by submission and by every teacher review,
     and frozen when the assignment's detail is cleaned.
5. A record carries its own classification (`recorded`, `reconstructed` or
   `unverifiable`), the evidence that classification rests on, the reason
   when it is unverifiable, and an inconsistency flag (§9.5). A record that
   cannot be trusted therefore exists, with everything that is known, without
   being mistaken for a verified one.
6. History is classified `RUNTIME` in `src/canonical_tables.py` and is included
   in backups like the rest of the database.

### 9.5 Completeness: the submission manifest

**What the code stores today** (verified):

| Case | Stored |
|---|---|
| Planned multiple-choice question that is renderable at submission | One `quiz_answers` row, `chosen_letter` NULL if unanswered (`quiz_submit_attempt`, `quiz.py:2185-2197`). |
| Planned open question, at least one of its fields present in the form | One `quiz_text_answers` row for **every** slot of the question, with an empty string for a slot left blank (`grade_quiz_text_answers`, `quiz.py:622`, iterates the union of submitted and expected slots). |
| Planned open question, none of its fields present in the form | **No rows** (`quiz_record_planned_open_text_answers`, `quiz.py:918`). |
| Planned question that is no longer renderable at submission | **No rows.** Only a count of skipped questions exists, at request time, and is not stored (`filter_renderable_attempt_question_ids`, `quiz.py:1512`). |
| Number of multiple-choice questions actually graded | `quiz_attempts.score_total`, overwritten at submission (`quiz.py:2183`, `:2207`). |
| Which slots were expected for an open question | Not stored separately; only implied by the rows that were written. |

So the absence of rows has four different meanings that stored data cannot
tell apart: the question was omitted as unrenderable; the question was
presented and none of its fields arrived; the rows were lost later (for
example through `quiz_text_answers.question_id … ON DELETE CASCADE`); or the
plan itself was malformed. These must not be equated, and a summary computed
from the rows cannot distinguish them, however carefully it is recomputed.

**Strict plan parsing.** The current parser is lenient: it turns unreadable
input into an empty plan and silently skips entries that are not integers
(`quiz_parse_attempt_question_plan`, `quiz.py:1348`; `int()` coercion in the
filter). History uses a strict parser instead. A plan is valid only if:

- it is a JSON list of question ids, or a JSON object with exactly the known
  keys (`question_ids`, `open_question_ids`, `mixed_open_enabled`,
  `include_open_answers_in_final_score`);
- every id is a JSON integer, positive, not a boolean, not a string, not a
  float;
- there are no duplicate ids, the list is not empty, and every open id is in
  the question list;
- the flags are booleans.

Anything else is a **malformed plan**. Nothing is skipped or repaired.

The strict parser reads the **raw stored text**, not a value that a lenient
JSON decoder has already tidied. Python's decoder accepts several things that
would hide damage: a repeated key (the last one wins), `NaN` and `Infinity`,
and numbers such as `1.0` or `1e0` that compare equal to an integer. The
strict parser rejects each of these, and also an empty value, a top-level
string, number or `null`, a nested list in place of an id, text
after the closing bracket, and an id too large to be a row id. A plan that
fails is kept byte for byte in the history record; it is never rewritten.

**The submission manifest (PR #327).** In the submission transaction, the
winning request (§7.5) records, durably and once, what was expected and what
arrived:

| Recorded | Meaning |
|---|---|
| The strictly parsed plan | Expected questions, in order, with their kind from the plan. |
| Per planned question, one disposition | `presented` or `omitted`, with the reason for an omission (not found, not eligible, no longer auto-gradable). |
| Per presented multiple-choice question | `answered` or `unanswered`. |
| Per presented open question | The expected slot numbers at that moment. |
| Per expected slot | `received_nonempty`, `received_empty` (the field arrived blank: an intentionally empty answer), or `not_received` (the field did not arrive at all). |
| Counts | Expected questions, presented, omitted, expected slots, slots received, slots answered. |

The manifest is part of the immutable side of the history record. It is never
recomputed from detail rows afterwards.

**Invariant between manifest and detail**, checked whenever history is written,
refreshed or verified:

- the dispositions account for every planned question exactly once;
- the `quiz_answers` rows are exactly the presented multiple-choice questions;
- for a presented open question with at least one received slot, the
  `quiz_text_answers` rows are exactly its expected slots;
- for a presented open question with no received slot, there are no rows, and
  the manifest says `not_received` for each slot;
- `score_total` equals the number of presented multiple-choice questions.

**Refresh after teacher grading preserves the invariant.** The review
transaction (§11.6) re-checks it before recomputing the mutable outcome. If the
detail no longer matches the manifest (rows have gone missing since
submission), the outcome is **not** overwritten with values computed from the
reduced data: the last verified outcome is kept, the record is marked
inconsistent, and the assignment is blocked from cleanup. The review itself
still commits.

**Attempts without a manifest** (submitted before PR #327) have no
submission-time record of what was expected. The backfill classifies them from
**independent evidence of completeness** only. "Independent" has a precise
meaning: the evidence that a set of rows is complete may not be that set of
rows.

- A surviving row proves that this row exists. It does not prove that the
  rows which belonged beside it are all still there.
- A backfill and a verifier that read the same incomplete rows and agree
  prove nothing. Agreement is never the test, and neither tool may treat the
  other's output as evidence.

**The counterexample this rule exists for.** An open question with two slots
was submitted and scored 1 of 2. One of its two rows is lost afterwards, by a
manual repair or a faulty data operation; the design does not need to know
how. An earlier draft accepted "at least one row" as proof that the question
was presented with the slots its rows show, and would have recorded 1 of 1 as
a reconstructed result. That rule is removed.

**What independent evidence the stored data offers**

| Part of the attempt | Independent evidence | Verdict |
|---|---|---|
| The plan | The raw stored text passes the strict parser. | Otherwise **unverifiable**. |
| Kind of each planned question | The question exists in canonical content. | Otherwise **unverifiable** (its kind and slots cannot be established). |
| Multiple-choice part | `quiz_attempts.score_total` and `score_correct`, written at submission on a different row: the number of `quiz_answers` rows equals `score_total`, every row's question is a planned multiple-choice question, and the sum of `is_correct` equals `score_correct`. | **Reconstructed**: the rows are the presented questions, and planned multiple-choice questions without a row were omitted. Any disagreement: **unverifiable**. |
| Open question with rows | **None is stored.** Each `quiz_text_answers` row carries its own slot number and its own points. Nothing records how many slots the question had at submission, and no count or total of open answers exists on the attempt. | **Unverifiable**, unless a slot-coverage witness is accepted (below) and the rows are exactly the slots it names. |
| Open question without rows | None. Omitted, not received and lost cannot be told apart. | **Unverifiable**. |
| Unsubmitted attempt with a valid plan | No answers were ever expected. | Participation record. |

**Slot-coverage witness.** An open question of a pre-manifest attempt counts
as reconstructed only if a witness establishes its **complete original slot
set**, and the attempt's rows for it are exactly that set, one row per slot.
Any witness must meet all of these:

1. It comes from a source other than `quiz_text_answers`.
2. It establishes the slots the question had **when the attempt was
   submitted**, not the slots it has today.
3. It is named in the history record (which witness, which version of it), so
   the verifier checks the rows against the witness and not against
   themselves.
4. If it is missing, ambiguous, or disagrees with the rows in either
   direction, the attempt is unverifiable.

What does **not** qualify:

| Candidate | Why it is not a witness |
|---|---|
| The surviving rows, in any number | They are the thing being checked. |
| Today's `fill_in_subquestions` for the question | Independent of the rows, but it describes the question now. Canonical content can change after a submission. Disagreement with it proves a problem; agreement proves nothing about the original. |
| A vault attempt note | Written best-effort after the commit, rewritten by later requests, and removable (§2.8). |
| A backup snapshot holding the same rows | A copy of the same source. It shows that nothing was lost since that snapshot, not that the set was complete at submission. |
| The backfill's own earlier output | The same source again, one step removed. |

One candidate could qualify: the canonical content revision that was deployed
when the attempt was submitted, if release records identify it reliably.
Whether that, or anything else, is accepted is decided in the PR #327 design
(§16.3, item 19). **Until then no witness is accepted**, and this document
assumes none: every pre-manifest attempt that has a planned open question is
unverifiable.

**An unverifiable attempt keeps its history.** Its record is still written and
holds everything that can be stated without guessing: the immutable context,
the raw plan text exactly as stored, the rows and counts as found, the scores
as found and marked as not verified, the classification `unverifiable` and the
reason (malformed plan, missing question, multiple-choice count mismatch, open
answers without rows, slot coverage unproven, slot coverage mismatch). Its
detail rows are left untouched. Nothing about it is repaired, inferred or
rounded up.

**Verification cannot upgrade a classification.** The verifier re-derives each
verdict from the evidence the record names: the manifest for a `recorded`
record; `score_total`, `score_correct` and the named witness for a
`reconstructed` one. It never promotes `unverifiable`, whatever the rows now
look like, and running the backfill or the verifier again changes nothing. A
restore does not upgrade a classification either (§13.4).

A record is `recorded` (manifest written at submission), `reconstructed`
(every part backed by independent evidence of completeness) or `unverifiable`.

**An unverifiable or inconsistent record is a cleanup blocker** (§10.2). The
attempt, and with it the whole assignment, keeps its detail. Such cases are
reported individually. Resolving them is a deliberate operator and product
action outside ordinary cleanup; this design defines no automatic way to
delete them. Whether `reconstructed` records are sufficient for cleanup, or
only `recorded` ones, is part of §16.3 item 11; the safe default is that both
qualify and unverifiable never does.

The practical consequence is stated plainly in §16.5: assignments with mixed
quizzes submitted before PR #327 are not cleaned by the ordinary tool.

### 9.6 Result access after attempt rows are deleted

Authorization today reads the `quiz_attempts` row
(`require_quiz_attempt_access`, `web/attempt_access.py:33`): `user_id` for
account-owned attempts, and for historical attempts the session marker keyed by
attempt id. If cleanup deletes the row, that information must live elsewhere.
PR #327 must choose and implement one of:

- **A. Retained ownership-bearing stub.** The `quiz_attempts` row stays, with
  `id`, `assignment_id`, `user_id`, `student_name`, timestamps and scores, and
  with `question_ids_json` and `seed` reduced to neutral values (both are
  `NOT NULL`). Authorization and URLs are unchanged by construction.
- **B. History-based authorization.** The history record carries the attempt
  id, the owning `user_id` or the fact that the attempt is browser-bound, and
  the access function reads it with identical rules. Requires a test proving
  that every outcome of `can_access_attempt` (`web/access.py:146`) is the same
  before and after deletion, for account-owned and browser-owned attempts.

Under both, `/quiz/attempt/<id>/result` stays stable, a foreign attempt stays
a 404, and a browser-owned historical attempt stays reachable only from the
browser holding its marker. Until one is implemented and verified, cleanup
deletes child rows only and leaves attempt rows in place.

### 9.7 When history is written

- At submission, in the submission transaction, by the winning request only
  (§7.5), together with the manifest (§9.5).
- On every teacher review, in the review transaction, after the invariant
  check (§9.5, §11.6).
- For abandoned attempts, when the assignment is closed and submissions have
  ended: lazily by the first request or tool that needs it, and in any case by
  the backfill and by the cleanup pre-check.
- For attempts that predate PR #327, by the backfill command (§12.3), which
  classifies and never guesses.

### 9.8 Unresolved schema decisions

- New table, or keep `quiz_attempts` as the durable row (§9.6 A).
- Granularity of performance data: per difficulty only, or per topic.
- Whether a question-level trace (question id and correctness, without the
  chosen answer) counts as history or as detail.
- Representation of NULL-owner attempts (§16, item 9).
- Whether practical results are part of history from the start (§16, item 14).
- The history work (PR #327) must start with its own design PR. This document
  fixes the dependency and the coverage: **cleanup cannot ship before it, and
  it must cover every attempt cleanup would touch**.

---

## 10. Monthly cleanup design

Separate phase (PR #328). Not implemented, not scheduled and not enabled by
this PR.

### 10.1 Gate and trusted time

Cleanup stays disabled until all of these hold. Each is checked by the tool at
run time; none is a convention.

1. The history schema exists (the tool checks the applied migrations and the
   expected columns).
2. An operator has enabled deletion explicitly. The tool is dry-run by default
   and deletes only with `--execute`; the systemd unit is not installed until
   PR #327 is deployed and verified in production.
3. **Trusted time is established for this run** (below).
4. The shared maintenance lock is held by this process (§10.11).
5. A verified recovery snapshot of **this** database and **this** upload root
   exists and is shown, by content, to hold what the run will delete (§10.8).
   This is a recoverability requirement. **It is not evidence about the
   clock.**
6. Pending unlinks from earlier runs have been retried (§10.6).

Without 1, 3, 4 or 5 the tool exits non-zero without opening a connection
that can perform cleanup writes. The only write a refused run may make is the
clock-fault bookkeeping update of §3.6, on a connection used for nothing else.
There is no flag that skips any of them.

**Nothing in this section exists before PR #328.** PR #326 and PR #327 contain
no deletion, no time-source query, no closure bound and no snapshot
verification. Cleanup is entirely disabled in both.

**Why the server's clock is not enough.** Take an assignment whose real age
since closure is 30 days minus 20 seconds. The server's clock steps forward by
30 seconds. Every check of §3.6 passes: the step is below the tolerance, the
operating system still reports the clock as synchronised, and a comparison
with external sources "within 120 seconds" agrees. A rule of the form
"server time >= E + 30 days" then deletes 20 seconds early. Tightening a
tolerance does not repair this; any tolerance admits an error of its own size.
The rule itself has to change: it must compare a **lower bound of the present**
with an **upper bound of the closure**, both taken from evidence outside the
host.

**Time fix.** A time fix `F` is an interval `[F.lo, F.hi]` of UTC, in whole
seconds, obtained by the cleanup tool during the run. Its claim: true UTC at
the moment the fix was completed lies inside it, provided the sources are
right within the error they state.

For each source `i` the tool records the source's time `s_i`, the error the
source states for itself `e_i`, and the round trip `rtt_i` measured with the
local monotonic clock:

```
lo_i = s_i - e_i
hi_i = s_i + e_i + (rtt_i + age_i) × (1 + ρ)

F.lo = floor( min over the sources used of lo_i )
F.hi = ceil ( max over the sources used of hi_i )
```

- `age_i` is the monotonic time from the arrival of that answer to the moment
  the bound is stated for; `ρ` is a bound on the drift of the monotonic clock
  (proposed 500 parts per million).
- **Sources.** At least three (proposed) sources operated independently of
  each other, whose intervals all overlap. If one answering source is
  inconsistent with the others, there is **no fix**; the tool does not vote.
  Which sources and which protocol is decided in PR #328 (§16.3, item 18).
- **Operator attestation.** An operator may supply the current UTC time, read
  from an independent device, for this run only. It counts as one source with
  a stated error that may not be set below a floor (proposed 60 seconds), and
  it can stand in for the network sources when they cannot be reached.
- A fix is used only by the process that obtained it, for at most a bounded
  time (proposed 15 minutes); after that the tool obtains a new one. A fix is
  never stored and read back later as evidence of the present.

**What a fix proves, and in which direction**

| Bound | Valid for | Rule |
|---|---|---|
| `F.lo`, a lower bound of the present | Everything that happens **after** the fix. Time does not run backwards, so the bound stays true as it ages. | It is **never advanced** for elapsed time. The tool takes no credit for the minutes that pass after the probe. |
| `F.hi`, an upper bound | Every fact that was **already true** when the fix was completed. | It is applied only to facts the tool read, as committed, **before** it started probing. |

**Requirements for every run that records a bound or deletes**

| # | Requirement | If it fails |
|---|---|---|
| a | `lifecycle_clock` row present and well-formed, `state = ok`. | Refuse. |
| b | The regression and boot-anchor checks of §3.6 pass. | Clock-fault bookkeeping (§3.6); refuse. |
| c | The operating system reports the clock as synchronised. Necessary; never sufficient. | Refuse. State unchanged. |
| d | A time fix is obtained. | No fix (sources unreachable, too few, or inconsistent with each other, and no attestation): refuse with a distinct status. State unchanged. Nothing recorded, nothing deleted. |
| e | The server's clock lies inside the fix, widened by a tolerance (proposed 60 seconds). | Network sources that agree with each other and not with the server: clock-fault bookkeeping; refuse. An attestation that disagrees with the server: refuse, state unchanged. |

All five are established at the start of the run, before the tool writes
anything else. Requirement (e) is then re-checked before every transaction,
with the upper side of the fix aged as in the formula. If it fails at that
point, because the clock was changed after verification, the run stops at
once: no further transaction, clock-fault bookkeeping, exit with the same
distinct status.

Requirement (e) keeps lifecycle enforcement from continuing on a clock that
visibly disagrees with the outside world. **It is not what protects
retention.** The 30-second step of the counterexample passes (e). Retention is
protected by the rule below, in which the server's clock supplies no evidence.

**Closure upper bound.** `closure_upper_bound_at` is a UTC instant that is not
earlier than the moment at which the assignment's closure was persisted. It is
recorded like this, in a confirmation run or at the start of an execute run:

1. **Read first.** In short writer transactions the tool applies ordinary
   observation (§6.3) and then collects every assignment that has no hold, a
   persisted closure (`closed_at` or `expiry_observed_at`) and no bound yet.
   These transactions commit.
2. **Probe afterwards.** Only then does the tool obtain the time fix `F` whose
   upper bound it will record. This is a new fix, not the one that satisfied
   requirements (d) and (e) at the start of the run.
3. **Record.** For each collected assignment, a conditional update stores
   `closure_upper_bound_at = F.hi`, restating that the bound is still NULL,
   that there is still no hold, and that the markers and `retention_base_at`
   are the values read in step 1.

Because the marker was committed before the probe began, the moment it became
true is not later than `F.hi`. From that moment the assignment is closed,
whatever any clock shows: no start is possible (§3.3). The bound therefore
needs no assumption about the server's clock at the time of the closure, and
none about how the clock behaved since.

The bound concerns the closure, not the grace window. In keeping with P1 and
§7.10, retention is counted from the closure and a submission during grace
moves nothing. The end of submissions is a separate condition: it must be
persisted before anything is deleted (§10.2, item 3).

An assignment whose bound is recorded in a run cannot satisfy the rule below
in that same run.

- The bound is written once and then used **as recorded**. A later run never
  recomputes it from stored local timestamps and never replaces it with a
  smaller value, however narrow its own fix is.
- It is cleared whenever a hold reason is added to the assignment, and it is
  disregarded and cleared if `retention_base_at` is later than it. It then has
  to be recorded again: after the hold is resolved or, when the one reason
  left is `restore_reclean`, by the operator path below.
- A restore clears it for every assignment (§13.4).

**One operator exception.** Step 1 collects only assignments without a hold,
so no ordinary run, scheduled or `--confirm-closures`, records a bound for a
held assignment. An assignment that was cleaned and then restored is held with
`restore_reclean` until it has been cleaned again, and it cannot be cleaned
again without a bound. For that case alone an operator may record the bound
while the hold is **exactly** `restore_reclean`. The order (read, then probe,
then record), requirements (a) to (e) and the retention rule below are the
same; only the hold condition differs, and the hold is not changed. §13.4
defines the conditions and the whole sequence.

**Trusted retention rule.** An assignment is old enough only if

```
F.lo  >=  max(closure_upper_bound_at, E, retention_base_at)  +  max(R, G)
```

`E` and `retention_base_at` are values of the server's clock. They enter the
maximum only as additional lower limits: they can delay cleanup and can never
bring it forward.

| Source of error | How the rule covers it |
|---|---|
| External probe uncertainty | Each source's stated error and its measured round trip widen its interval. `F.lo` is the lowest lower bound and the recorded bound the highest upper bound among the sources used. |
| Evidence age | The lower bound is never advanced. A fix is used by one process for a bounded time. The upper bound is applied only to facts read before the probe; where it is aged for check (e), it grows by the elapsed time times `1 + ρ`. |
| Timestamp precision | Lower bounds are rounded down and upper bounds rounded up to whole seconds. |
| Clock drift | `ρ` is charged on every interval measured with the local monotonic clock. |
| Previously recorded closure uncertainty | It is contained in `closure_upper_bound_at`, which already includes the error of the fix that produced it, and which is reused as recorded and never tightened. |
| Repeated small clock adjustments | No term of the rule is a difference between two readings of the server's wall clock, so adjustments cannot add up inside it. Within a boot they are also caught by the anchor (§3.6). |

Applied to the counterexample: whatever the server's clock shows, `F.lo` is
not later than the true present and the recorded bound is not earlier than the
true closure. With a real age of 30 days minus 20 seconds the rule is false,
and nothing is deleted.

**Consequences**

- **If sufficient evidence is unavailable, cleanup is skipped.** No fix means
  no bound is recorded and nothing is deleted. A host without outbound access
  to time sources cleans only when an operator attests the time for that run.
  That is the intended behaviour.
- Clock synchronisation and a recent backup are not accepted as proof of
  correct UTC anywhere in this rule. Taking a fresh backup after a clock jump
  changes nothing.
- The rule can only lengthen retention. Detail is kept for at least 30 days
  after the closure was confirmed, which is later than the closure itself by
  the delay of the confirming run and by the width of two fixes.
- **Cost.** If closures were confirmed only by the monthly run, detail could
  live for up to roughly three months. A confirmation run deletes nothing and
  may be scheduled more often than deletion; with daily confirmation and
  monthly deletion, detail lives between 30 and roughly 62 days. The cadence
  is an open decision (§16.3, item 18).
- After any `suspect` state, cleanup resumes only through §3.7. The clock
  looking correct again is not sufficient.
- The remaining assumption is the sources. If every source used, or the
  operator and the server's clock together, is wrong in the same direction by
  more than its stated error, the rule is wrong with them (§16.5).

**Confirmation runs.** `--confirm-closures` performs steps 1 to 3 above and
nothing else. It needs requirements (a) to (e) and the maintenance lock, not
the snapshot gate. It writes only `closure_upper_bound_at` and ordinary
observation markers, and deletes nothing. It records no bound for a held
assignment, whatever the reason.

### 10.2 Eligibility predicate

This is the only definition of eligibility. An assignment is eligible at
server time `now`, under the run's time fix `F`, only if **all** hold:

1. `E` is not NULL. Open assignments, including every no-deadline assignment
   that was never closed, are never eligible.
2. `lifecycle_hold` is NULL. No scheduled or ordinary run accepts any other
   value. In an operator re-clean that names the assignment, and only there,
   this item reads: `lifecycle_hold` is exactly `restore_reclean` (§13.4).
   Every other item applies to that run unchanged.
3. Its closure and the end of its submissions are persisted: `closed_at` or
   `expiry_observed_at` is not NULL, and `submissions_closed_at` is not NULL.
   The run's first step stamps either marker by ordinary observation when the
   server's clock shows it (§6.3). Once `submissions_closed_at` is set, no
   student write to the assignment is accepted, whatever any clock shows.
4. **Server-clock rule, necessary and never sufficient:**
   `now >= max(E, retention_base_at) + max(R, G)`.
5. **Trusted-time rule (§10.1):** `closure_upper_bound_at` is not NULL and is
   not earlier than `retention_base_at`, and
   `F.lo >= max(closure_upper_bound_at, E, retention_base_at) + max(R, G)`.
6. `details_cleaned_at` is NULL.
7. Every stored time of the assignment and of its attempts parses strictly
   (§3.4). No **factual event timestamp** and no **terminal marker** is later
   than `now` (§3.9). A `deadline_at` later than `now`, which a manual close
   leaves behind, is valid and blocks nothing.
8. No attempt of the assignment has pending manual grading (§8.2).
9. **Every attempt of the assignment**, whether submitted, abandoned,
   account-owned or NULL-owner, has a durable record that is not unverifiable
   and not inconsistent, and whose manifest invariant holds against the detail
   rows now (§9.3, §9.5).
10. Result access after deletion is implemented and verified (§9.6), or the
    run is configured to keep attempt rows.
11. The run's verified snapshot holds, with identical content, every row and
    every file this assignment's transaction would delete (§10.8).

A failure of 2, 7, 8, 9, 10 or 11 **blocks** the assignment and is reported
with its reason. Nothing of a blocked assignment is deleted. A failure of 3, 4
or 5 means "not yet": it is reported as closure not confirmed, or as not old
enough.

`R` is a floor. No setting may reduce `R` below 30 days; the tool rejects a
smaller value instead of clamping silently. How long detail actually lives
above that floor depends on how often closures are confirmed and how often
deletion runs (§10.1), and it grows whenever a run is skipped or refused.

### 10.3 What is deleted and what is kept

Per eligible assignment, in this order, in one transaction:

1. Verify every attempt (§10.4). Any failure: roll back, report, next
   assignment.
2. Confirm that the snapshot covers every row and file about to be removed
   (§10.8). Any difference: roll back, report, next assignment.
3. Record each practical file's `stored_path` in the pending-unlink ledger
   (§10.6).
4. Delete `quiz_answers` for its attempts. Must be explicit: this foreign key
   has no cascade.
5. Delete `quiz_text_answers` for its attempts. Explicit, not left to the
   cascade, so the count is known and reported.
6. Delete `practical_submission_files`, then `practical_submissions`.
7. Reduce or delete the attempt rows, as §9.6 decides.
8. Freeze the mutable grading outcome of each history record.
9. `UPDATE quiz_assignments SET details_cleaned_at = :now WHERE id = :id AND details_cleaned_at IS NULL AND submissions_closed_at IS NOT NULL`.

`submissions_closed_at` is already set when this transaction starts:
eligibility requires it (§10.2, item 3). Every student write to the assignment
has therefore been refused, without consulting the clock, since that marker
was written (§3.3), and none can arrive while the transaction runs.

`details_cleaned_at` records that step 9 committed, that is, that the database
deletion is complete. File removal happens afterwards and is tracked by the
ledger, not by this column.

Step 9 also restates the hold condition of §10.2, item 2:
`lifecycle_hold IS NULL` in an ordinary run, and
`lifecycle_hold = 'restore_reclean'` in an operator re-clean (§13.4). The
transaction never writes `lifecycle_hold`.

Kept:

- The `quiz_assignments` row, including `public_token`, `owner_user_id` and the
  lifecycle columns. It is small, it is the assignment's identity for history,
  and keeping it means a cleaned link still answers "closed" instead of 404.
  The token is never reused.
- All history rows.
- All canonical content: `questions`, `multiple_choice_options`,
  `fill_in_subquestions`, topics, sections, exams, assets. The tool contains no
  `DELETE` or `UPDATE` against any table classified `CANONICAL`. A test asserts
  it by statement inspection.
- `users`, `totp_*`, `login_throttle`, `lifecycle_clock`, `database_identity`.

### 10.4 Verification and foreign-key analysis

Verification inside the transaction, before any delete, for **every** attempt
of the assignment:

- a durable record exists;
- it is `recorded` or `reconstructed`, never unverifiable or inconsistent;
- the completeness evidence its classification names is present and still
  holds: the manifest for a `recorded` record; `score_total`, `score_correct`
  and the named slot-coverage witness for a `reconstructed` one (§9.5);
- the manifest invariant holds against the detail rows as they are now (§9.5);
- its mutable outcome equals the values recomputed from those rows.

The last point is necessary and never sufficient. A record that merely agrees
with the rows it was computed from has not been verified by that agreement.

Foreign keys:

- Cleanup deletes only from child tables of `quiz_attempts`, and possibly from
  `quiz_attempts` itself. Nothing references `quiz_answers`,
  `quiz_text_answers` or `practical_submission_files`, so no cascade starts
  from them.
- Deleting `practical_submissions` cascades to `practical_submission_files`;
  the tool deletes those first anyway.
- Deleting `quiz_attempts` cascades to `quiz_text_answers` and
  `practical_submissions`, already empty for these attempts by then.
- No cascade leads from any of these tables to `questions`, to `users`, or to a
  history table that satisfies §9.4.
- The history table's foreign keys must be reviewed against this list in its
  own PR. If history references `quiz_attempts(id)` with a foreign key,
  deleting attempt rows is impossible or destructive; that constrains §9.6.
- After each transaction and at the end of the run the tool runs
  `PRAGMA foreign_key_check` and fails on any row.
- Foreign keys stay **on** for the whole run. The tool never uses the
  migration runner's table-rebuild mode, which turns them off.

### 10.5 Transactions, batching, idempotence, interruption

- Unit of work: **one assignment per transaction**, `BEGIN IMMEDIATE`, with
  `now` sampled after the lock. An assignment is either fully cleaned and
  stamped in the database, or untouched.
- Bounded batch: at most `N` assignments per run (proposed default 50) and a
  wall-clock budget. The rest waits for the next run. An assignment with more
  attempts than a second bound (proposed 5 000) is skipped and reported for a
  manual run, not partially processed.
- Each transaction is short, so the web application's 5-second busy timeout
  (`web/db.py:29`) is not exhausted by cleanup holding the writer lock.
- Idempotent: the predicate excludes `details_cleaned_at IS NOT NULL`, and the
  stamp is a conditional update. A second run finds nothing to do for a cleaned
  assignment.
- Interrupted (SIGTERM, power loss) during a transaction: it rolls back.
  Committed assignments stay cleaned. The next run continues.
- Interrupted after a commit and before its files are unlinked: the ledger
  still lists them; the next run removes them (§10.6).
- A restore from an older snapshot brings detail back together with a NULL
  `details_cleaned_at`. Such assignments are held and are not cleaned by the
  scheduled run; the operator re-clean of §13.4 follows reconciliation, and
  each of its steps can be interrupted and run again. A restore also clears
  every closure bound, so no restored assignment is cleaned earlier than 30
  days after it has been confirmed again, by an ordinary run or by that path.

### 10.6 Files on disk and the pending-unlink ledger

Database rows and files cannot be deleted atomically together. Proposed
mechanism, added by PR #328: a small ledger table listing files whose rows
have been deleted and which still have to be removed (`stored_path`,
assignment id, recorded instant, attempt count, last error, and the id of the
snapshot verification record that covered the file, §10.8).

1. Inside the cleanup transaction: insert one ledger row per file, delete the
   practical rows, commit. The intent to unlink is now durable.
2. After commit, still under the maintenance lock: unlink each file, then
   delete its ledger row in a short transaction.
3. A file that is already missing counts as removed.
4. An unlink that fails (permissions, I/O error) leaves its ledger row, with
   the error and an incremented attempt count.

**Path safety.** A ledger path is unlinked only if all of these hold; otherwise
it is **never** unlinked, its ledger row is kept, and it is reported:

- it resolves through `practical_uploaded_file_safe_path`
  (`web/services/practical.py:311`) to a location strictly inside the upload
  root;
- the final component is a regular file, not a symbolic link and not a
  directory;
- no component of the path below the upload root is a symbolic link;
- no live `practical_submission_files` row references the same `stored_path`.

**Crash points**

| Crash | State afterwards | Next run |
|---|---|---|
| Before the cleanup commit | Nothing changed. | Starts over. |
| After the commit, before any unlink | Rows gone, files present, ledger lists them. | Ledger retried first; files removed. |
| After an unlink, before its ledger row is deleted | File gone, ledger row present. | File is missing, which counts as removed; ledger row deleted. |

Retry and reporting:

- Every run, dry or not, reports the number and age of ledger rows.
- Every execute run starts by retrying the ledger before doing anything else.
- A run that ends with ledger rows exits with status 1.
- The tool never scans the upload root and deletes "files without rows": a
  file whose upload is in progress looks exactly like that, because the upload
  writes the file before it inserts and commits the row
  (`web/blueprints/practical.py:308-325`).

Empty attempt directories (`<attempt>/<task>/<submission>/`,
`src/practical_uploads.py:166`) are removed only if empty.

### 10.7 Vault notes

- The cleanup tool **does not delete vault files** in its first version.
  `AGENTS.md` forbids deleting vault files unless explicitly asked, and decision
  E's requirement is that nothing in the vault disappears unexpectedly.
- This leaves a known gap: `vault/Generated/Quizzes/attempts/*.md` holds a copy
  of student answers that outlives the database detail. It is recorded as an
  open decision (§16, item 8), not solved silently.
- If note removal is approved later, it must be opt-in, limited to the exact
  path `quiz_attempt_row_note_path` (`quiz.py:229`) returns for each attempt
  (computed before the row is deleted), confined to
  `Generated/Quizzes/attempts/`, recorded in the same ledger as §10.6, and
  reported file by file in dry-run.
- Topic notes, generated exams and every other vault path are out of reach of
  the tool by construction.
- Independent of cleanup: `quiz_write_assignment_note` should write the
  computed state instead of the literal `"open"`.

### 10.8 Recovery snapshots and their provenance

A snapshot directory holding `questions.db` and `files.tar.gz` is not, by that
fact alone, a consistent pair, and it is not, by that fact alone, a snapshot
of **this** system. Today the database is copied first and the files are
archived afterwards with nothing excluding other writers
(`docs/DEPLOYMENT.md` §16), and nothing in a snapshot says which database or
which upload root it came from. Deleting data makes both gaps matter: the
snapshot that justifies a deletion must be the right one, intact, and must
actually contain what is deleted.

Everything in this section belongs to PR #328. Restores in PR #326 and PR #327
use the procedure that exists today (§13.4).

**Database identity.** Migration C adds a single-row table
`database_identity` holding a random `instance_id`. The row is written once
per environment by an explicit operator command, not by migration SQL and not
at start-up. A database restored from its own snapshot keeps its identity. A
copy made for another environment must be given a new one by the same command
before it is used. A missing or malformed row makes the cleanup tool refuse.

**Snapshot manifest.** `src.backup_application` writes `manifest.json` into
the staging directory before the snapshot is published, so a published
snapshot always has one:

| Field | Content |
|---|---|
| `database_instance_id` | Read from the snapshot's own database copy. |
| `source_database_path`, `source_upload_root` | The resolved absolute paths the backup run was given. |
| `database_sha256` | Digest of the snapshot's `questions.db`, byte for byte. |
| `archive_sha256` | Digest of the snapshot's `files.tar.gz`, byte for byte. |
| `stamp`, format version | Informational. |

**Verification record.** A verify command, proposed as part of
`src.backup_application` or a companion, checks one snapshot and writes
`verification.json` into it:

| Field | Content |
|---|---|
| Identity | `database_instance_id`, `source_database_path` and `source_upload_root`, copied from the manifest. |
| Digests | `database_sha256` and `archive_sha256`, **recomputed** from the files and equal to the manifest's. |
| File list | For every member of the archive under the upload tree: its stored path, size and SHA-256. |
| Consistency | Every non-deleted `practical_submission_files.stored_path` in the snapshot database is in the file list, with the recorded size where one is stored. Archived files without a row are listed and do not fail the check. |
| Database health | `PRAGMA integrity_check` of the snapshot copy, and its applied migrations. |
| Result and record id | Pass or fail, and the SHA-256 of the record's own content, which names it. |

**Gate at the start of an execute run.** The run uses one snapshot throughout:
the one named by the operator, or the newest that passes. All of these must
hold, or the run aborts before any cleanup write:

1. It is a complete `snapshot-*` directory, never a hidden staging directory,
   with a manifest and a verification record whose result is a pass.
2. **Intended database.** The record's `database_instance_id` equals the
   `instance_id` of the live database, and equals the one in the snapshot's
   own database copy.
3. **Intended upload root.** The record's `source_database_path` and
   `source_upload_root` equal the resolved `--db` and `--uploads` of this run.
4. **Exact artefacts.** The SHA-256 of `questions.db` and of `files.tar.gz`,
   recomputed now, equal the digests in the record. A database or an archive
   substituted after verification, and a record copied in from another
   snapshot, both fail here.
5. Its manifest is of the format written by the lock-holding backup tool, so
   it was taken under the shared lock (§10.11) and no cleanup deletion or
   unlink ran while it was being made. A snapshot without a manifest, which
   includes every snapshot taken before PR #328, does not pass.

A snapshot of another environment fails 2 or 3 however valid, recent and
verified it is.

**Coverage: the snapshot contains what is about to be deleted.** Identity and
digests show that the snapshot is the right one and intact. They do not show
that it holds this particular detail. That is established **by content**,
per assignment, inside the cleanup transaction and under the writer lock, so
nothing can change between the comparison and the delete (§10.3, step 2):

- Every row the transaction will delete or reduce, in `quiz_answers`,
  `quiz_text_answers`, `practical_submissions`, `practical_submission_files`
  and `quiz_attempts`, is present in the snapshot database with the same
  value in every column.
- Every file the transaction will put on the unlink ledger is in the record's
  file list, and the live file's size and SHA-256, computed now, equal the
  recorded ones.
- Any difference blocks that assignment (`blocked: backup does not cover
  current detail`) and nothing of it is deleted. A teacher review committed
  after the snapshot is the ordinary cause; the assignment is cleaned by a
  later run, after a later verified snapshot.

**No timestamp establishes coverage.** The snapshot's age, its stamp and the
detail's own timestamps are not compared for this purpose. A snapshot that
looks newer than the data but does not contain it fails the comparison; a
snapshot that contains it passes whatever its age.

The verification record id is written to the run's summary for every cleaned
assignment and stored with its ledger rows, so it stays known which snapshot
held the data.

**Snapshot age says nothing about the clock.** The snapshot's stamp and the
"current time" it would be compared with come from the same clock. Clock
correctness is established separately, by §10.1.

**Uploads during a snapshot.** Student uploads are not excluded by the lock.
An upload writes its file first and commits its row afterwards (§10.6), and
the snapshot copies the database first and archives files afterwards. So:

| Upload relative to the snapshot | In the snapshot | Effect on restore |
|---|---|---|
| Committed before the database copy | Row and file | Consistent. |
| Committed after the database copy, file written before the archive pass reached it | File without a row | Harmless surplus file; reported. |
| File written after the archive pass | Neither | Not in this snapshot; in the next. |

A row without its file cannot arise from an upload. It can arise only from a
deletion, which the lock excludes. This document makes no stronger claim of
point-in-time consistency for concurrent uploads.

Other prerequisites and limits:

- The tool does not create the backup itself, and never copies the live
  database file. Live databases are backed up only with `src.backup_sqlite`
  (`AGENTS.md`).
- A restore test from a snapshot (`docs/DEPLOYMENT.md` §17) must have been done
  after the history migration and before the first execute run.
- Snapshots contain the detail that cleanup removes, and nothing prunes them
  automatically (§16, item 15).
- The gate examines the snapshot on the server. Whether an off-server copy of
  it exists is not checked by the tool (§16.5).
- Two environments cloned from each other share an identity until one is
  re-keyed. Then check 2 cannot tell their snapshots apart, and it is the
  comparison by content that refuses the wrong one.

### 10.9 Dry run

Two different guarantees, stated separately because only the first can be
given for a live WAL database.

**Guarantee 1 — no logical database mutation.** The dry run issues no
statement that changes a row or the schema. The connection sets
`PRAGMA query_only = ON`, so SQLite refuses any attempt. Testable: a logical
dump of every table is identical before and after.

**Guarantee 2 — no filesystem change outside SQLite's own files.** The dry run
does not unlink, create, rename or touch any file in the upload root or the
vault, takes no ledger action and writes only to stdout. Testable: a listing of
both trees with sizes, hashes and modification times is identical before and
after.

**Not guaranteed on a live database: unchanged SQLite files.** `query_only`
governs SQL statements, not the files. Measured on a WAL database (SQLite
3.51, temporary files):

| Connection | Observed |
|---|---|
| `mode=ro` when a `-wal` file exists and `-shm` does not | The reader **created** the `-shm` file. |
| `mode=ro` in a directory it cannot write, with no sidecars present | The open **failed** ("unable to open database file"). |
| `mode=rw` with `query_only`, when it is the last connection to close | It **checkpointed** the WAL: the main database file's bytes changed and the WAL was truncated. No row changed. |
| Either mode while another connection keeps the database open | No file changed. |

So a dry run that opens the live database may create or resize `-wal` and
`-shm`, and may cause the main file to be rewritten by a checkpoint. That is
normal SQLite housekeeping and not a mutation of data, but it is a file
change, and the design does not promise otherwise.

`immutable=1` must **never** be used on the live database. It tells SQLite the
file cannot change, so it skips locking and change detection; against a
database that is being written it returns wrong results or errors.

**Safe strategy.**

- Default and scheduled dry runs read a **snapshot copy**, not the live
  database: the `questions.db` of a verified recovery snapshot (§10.8), or a
  fresh copy made with `src.backup_sqlite`. Such a copy is a standalone
  rollback-journal file (`backup_sqlite.py` sets `journal_mode=DELETE`), opened
  `mode=ro` with `query_only`. Reading it creates no sidecar. This is the same
  approach the admin sign-in preflight already uses (`docs/DEPLOYMENT.md` §23:
  it reads a `src.backup_sqlite` snapshot, never the live WAL database).
- **Guarantee 3**, for this mode only: the live database and its sidecars are
  not opened by the dry run at all. Testable: the dry run succeeds when given
  only the snapshot path, and file metadata of the live database is unchanged.
  Making the copy with `src.backup_sqlite` is a separate, already sanctioned
  step with its own file effects.
- A dry run against the live database is available for an operator who wants
  current data. It carries guarantees 1 and 2 only and says so in its output.
- The report states which source it read and that source's timestamp, so a
  day-old snapshot is not mistaken for the current state.

In every mode the dry run prints exactly what an execute run would do: per
assignment, the id, `E`, eligibility, the reason if blocked, row and file
counts, and the ledger state.

A dry run may query the time sources, since that writes nothing. It then
reports each assignment's recorded closure bound and whether the trusted-time
rule holds. Without a fix it reports that an execute run would be refused. A
dry run never records a closure bound.

### 10.10 Logging and audit summary

To stdout, captured by journald like the backup unit:

- Run mode, data source, `now`, each requirement of §10.1 with its result, the
  time fix (its bounds, the sources used and their stated errors, or the
  attestation), `R`, batch limits.
- The snapshot used: its path, database identity, both digests and the id of
  its verification record (§10.8).
- Per assignment: id, `E`, `closure_upper_bound_at`, outcome (`cleaned`,
  `closure bound recorded`, `not yet: closure not confirmed`,
  `not yet: not old enough`, `blocked: pending grading`,
  `blocked: history missing`, `blocked: history unverifiable`,
  `blocked: history inconsistent`, `blocked: on hold` with the reasons,
  `blocked: history mismatch`, `blocked: malformed timestamp`,
  `blocked: event timestamp in the future`,
  `blocked: backup does not cover current detail`, `skipped: too large`,
  `not eligible`), row counts per table, file count and bytes.
- For an operator re-clean (§13.4): the assignment named, the step reached
  (`re-clean: closure bound recorded`, `re-clean: cleaned`,
  `re-clean: verified and resolved`) or the condition that refused it
  (`re-clean refused: other hold reason present`,
  `re-clean refused: reconciliation incomplete`,
  `re-clean refused: no authoritative history`,
  `re-clean refused: history mismatch`), and the operator.
- Ledger: files removed this run, files still pending, oldest pending age,
  last error per pending file.
- Totals and exit status: 0 clean; 1 something was blocked, mismatched or left
  pending; 2 operator or database error, including a snapshot that fails the
  provenance gate; 3 lock held by another maintenance job, nothing done;
  4 trusted time not established or lost during the run, nothing further
  done.

Never logged: student names, answers, teacher notes, tokens.

Whether a persisted audit table is also wanted is open (§16, item 10).

### 10.11 Shared exclusion between backup and cleanup

Scheduling the two jobs at different times is not exclusion: a timer can be
delayed (`RandomizedDelaySec`, `Persistent=true` after downtime), a run can be
slow, and an operator can start either by hand.

**One advisory lock file** under the state directory (for example
`/var/lib/learnpilot/maintenance.lock`, which both units can already write
through `ReadWritePaths=/var/lib/learnpilot`), taken with `flock`, exclusive,
non-blocking:

| Holder | Held for |
|---|---|
| `src.backup_application` | From before the SQLite snapshot until the upload and vault archive is finished, the manifest is written and the snapshot is published. |
| The cleanup tool | The whole run: ledger retry, recording of closure bounds, every database deletion transaction, and every file unlink. A confirmation run holds it too. |

- The lock covers all four operations: SQLite snapshot, upload and vault
  archive, cleanup database deletion, cleanup file unlinking.
- A job that finds the lock held does nothing, logs it, and exits with a
  distinct status (3). It does not wait indefinitely and does not proceed
  without the lock.
- Retry is by the next timer firing or an operator rerun. Both jobs are
  idempotent, so a skipped run loses nothing.
- The lock is released by the kernel when the process exits, including on a
  crash, so there is no stale lock to clear by hand.
- Taking the lock in `src.backup_application` is a change to the existing
  backup tool. It belongs to PR #328, with its own tests, and must not weaken
  the tool's all-or-nothing contract.
- The lock does **not** cover the web application. Cleanup against requests is
  handled by transactions (§11.6, §11.7); uploads against backups remain as
  documented today.
- Correctness of the database work does not rest on the lock alone: each
  cleanup transaction re-checks its predicate under the SQLite writer lock.

### 10.12 Proposed CLI

Name and flags are proposals:

```
python3 -m src.cleanup_assignment_details --db-snapshot /var/backups/learnpilot/snapshot-<stamp>/questions.db --uploads "$DZI_UPLOAD_ROOT"     # dry run on a snapshot (default use)
python3 -m src.cleanup_assignment_details --db "$DZI_DB" --uploads "$DZI_UPLOAD_ROOT" --backup-dir /var/backups/learnpilot                     # dry run on the live database
python3 -m src.cleanup_assignment_details --db "$DZI_DB" --confirm-closures                                                                    # records closure bounds; deletes nothing
python3 -m src.cleanup_assignment_details --db "$DZI_DB" --uploads "$DZI_UPLOAD_ROOT" --backup-dir /var/backups/learnpilot --execute
python3 -m src.cleanup_assignment_details --db "$DZI_DB" --reclean-confirm <assignment id>                                                     # operator: bound for a restore_reclean hold; deletes nothing
python3 -m src.cleanup_assignment_details --db "$DZI_DB" --uploads "$DZI_UPLOAD_ROOT" --backup-dir /var/backups/learnpilot --reclean <assignment id> --execute   # operator: re-clean, verify, resolve
```

The database is named explicitly and never defaulted to `data/questions.db`,
never created and never migrated, following `manage_assignment_tokens`.
`--execute` with `--db-snapshot` is refused. `--confirm-closures` and
`--execute` both need a time fix (§10.1); an operator attestation is given
with its own option and is valid for that invocation only. `--execute` may
name the snapshot to use; otherwise it takes the newest that passes the
provenance gate (§10.8).

The two `--reclean` forms are the operator path of §13.4. They act only on
the assignments named, only when the hold is exactly `restore_reclean`, and
need the same time fix and, for the deletion, the same snapshot gate. They are
never placed in a timer. Run again after an interruption, each continues from
the stored state.

### 10.13 Reusable systemd pattern

`deploy/systemd/learnpilot-backup.service` and `.timer` are the template.
Nothing in `deploy/` is changed by this PR. A future
`learnpilot-cleanup.service` and `.timer` would reuse:

| From the backup units | Reused as |
|---|---|
| `Type=oneshot`, `User=learnpilot`, `Group=learnpilot` | same |
| `WorkingDirectory=/opt/learnpilot/app`, venv interpreter with `-m` | same |
| `EnvironmentFile=/etc/learnpilot/backup.env` (state paths only, no secrets) | same file; cleanup needs no secret |
| `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=strict`, `ProtectHome`, `UMask=0077` | same |
| `ReadWritePaths=/var/lib/learnpilot` | same; the backup directory only needs to be readable |
| `OnCalendar=`, `RandomizedDelaySec=`, `Persistent=true` | monthly, for example `*-*-01 04:30:00` |

The suggested time is after the daily backup only so that a fresh snapshot
usually exists; exclusion is provided by the lock of §10.11, not by the
schedule. `Persistent=true` runs a missed month at the next boot, which is safe
because the predicate decides what is deleted. If confirmation runs are
scheduled separately (§16.3, item 18), they get their own timer on the same
pattern; a host without outbound access to time sources cannot run either
unattended (§10.1). As with backups, no failure
notification exists (`docs/DEPLOYMENT.md` §16), so the operator checks
`systemctl --failed`.

The timer is installed only when the gate in §10.1 is satisfied.

---

## 11. Authorization and concurrency

### 11.1 Principles

1. **One writer transaction per decision.** Every lifecycle decision that leads
   to a write is taken inside a `BEGIN IMMEDIATE` transaction, on rows re-read
   inside that transaction. SQLite allows one writer at a time, so two such
   transactions are always fully ordered.
2. **Time is sampled after the lock.** `now` is read only once
   `BEGIN IMMEDIATE` has returned, and is used only if the clock-health check
   passes (§3.6). A request that waited for the lock across a boundary is judged at
   the instant it got the lock.
3. **The predicate is restated in SQL.** Each write carries its precondition in
   its `WHERE` clause and the handler acts on `rowcount`. A value read earlier
   in the request is never trusted for a write.
4. **Authorization first, state second.** The existing 404 for "not yours or
   missing" is decided before any lifecycle message is produced, so lifecycle
   responses never reveal the existence or state of someone else's row.
5. **Reads that span several statements use one read transaction**, so they see
   one snapshot (§11.7).

The open predicate, used everywhere below and written once here:

```sql
-- OPEN(:now)
closed_at IS NULL
AND expiry_observed_at IS NULL
AND submissions_closed_at IS NULL
AND (deadline_at IS NULL OR deadline_at > :now)
```

Every writer transaction begins with the same three steps, in this order
(§3.6): the clock row itself (missing, `suspect`, regression, step); then the
hold, where `lifecycle_hold IS NOT NULL` refuses the request with a 503
whatever the state (§3.8); then the factual timestamps and terminal markers of
the rows being acted on, compared with `now`. Only then is the predicate
evaluated. A scheduled deadline later than `now` is never a finding (§3.9).

Connection note for the implementing PR: `connect_db` (`web/db.py:32`) uses
Python's default transaction handling, where `BEGIN IMMEDIATE` is issued
explicitly by the caller (as `web/blueprints/practical.py:256` already does)
and the connection must not be inside an implicit transaction at that point.

Production uses WAL (`docs/DEPLOYMENT.md` §8). A reader never blocks the
writer and sees a consistent snapshot for the duration of a read transaction.

### 11.2 Attempt creation racing deadline expiry

Deadline expiry is not an event. The race is between the start request's check
and its insert.

Transaction in `quiz_start_or_resume_attempt`:

```sql
BEGIN IMMEDIATE;
-- clock-health check; :now sampled here, after the lock
SELECT id, submitted_at FROM quiz_attempts
 WHERE assignment_id = :aid AND user_id = :uid;      -- existing: return it, COMMIT
INSERT INTO quiz_attempts (assignment_id, user_id, student_name, seed,
                           question_ids_json, score_total, started_at)
SELECT :aid, :uid, :name, :seed, :plan, :total, :now
  FROM quiz_assignments
 WHERE id = :aid
   AND lifecycle_hold IS NULL
   AND closed_at IS NULL
   AND expiry_observed_at IS NULL
   AND submissions_closed_at IS NULL
   AND (deadline_at IS NULL OR deadline_at > :now);
-- rowcount 0: closed (or held: 503). Stamp expiry_observed_at if the deadline has passed (§6.3).
COMMIT;                                               -- commit also in the closed case, to keep the stamp
```

- The open check and the insert are one statement, so there is no gap between
  them.
- `started_at = :now` is the instant that satisfied `deadline_at > :now`, so
  every attempt created here has `started_at < deadline_at`, strictly. For a
  manual close that follows, see §11.3.
- A request that arrives at `E - 1 s` and obtains the lock at `E` is refused.
- The plan and seed are computed before the transaction, as today; they do not
  depend on time.
- `:uid` is the validated session principal. The form names no owner.

### 11.3 Attempt creation racing manual closure

The close is the transaction of §6.1. It and the start of §11.2 both run under
`BEGIN IMMEDIATE`, so one of them commits entirely before the other makes its
checks.

| Commit order | Result |
|---|---|
| close, then start | The start's `INSERT … SELECT` finds `closed_at` set and inserts nothing. |
| start, then close | The attempt row exists when the close runs. The close stores the sampled `now`. Lifecycle time never decreases (§3.6), so `started_at <= closed_at`. The attempt is in flight (§7.1) and has the whole grace window, counted from `closed_at`. |

The order is decided by the writer lock and enforced by the insert guard. It
is not inferred from timestamps, and no timestamp is altered to encode it:
`closed_at` is always the instant at which the close was sampled, also when a
start carries the same second, and also in the last second before a deadline.

**The counterexample from review.** `deadline_at = D`. An attempt starts at
`D - 1 s`, and the teacher closes in that same second, so `now = D - 1 s`. An
earlier draft stored `closed_at = D`, one second later than the clock, so that
`started_at < closed_at` would hold strictly. That value failed the draft's own
predicate `deadline_at > closed_at`: the update matched no row, and the
response still reported a closure. With §6.1 as it now stands,
`closed_at = D - 1 s`, one row is updated, `E = D - 1 s`, and the attempt is
in flight and may submit until `D - 1 s + 15 minutes`.

### 11.4 Submission racing the grace cutoff

```sql
BEGIN IMMEDIATE;
-- clock-health check; :now sampled here, after the lock
SELECT qt.submitted_at, qt.started_at, qt.user_id,
       qa.lifecycle_hold, qa.deadline_at, qa.closed_at,
       qa.expiry_observed_at, qa.submissions_closed_at
  FROM quiz_attempts qt JOIN quiz_assignments qa ON qa.id = qt.assignment_id
 WHERE qt.id = :attempt_id;

-- 0. lifecycle_hold IS NOT NULL            -> 503; ROLLBACK
-- 1. submitted_at IS NOT NULL              -> outcome already_submitted; ROLLBACK (nothing was written)
-- 2. submissions_closed_at IS NOT NULL     -> outcome closed (no clock involved); ROLLBACK
--    closed and attempt not in flight (§7.1) -> outcome closed (no clock involved); ROLLBACK
--    closed and :now >= E + G              -> outcome closed; stamp submissions_closed_at; COMMIT
-- 3. otherwise:
--      INSERT quiz_answers …, quiz_text_answers …
UPDATE quiz_attempts
   SET submitted_at = :now, score_correct = :score, score_total = :total
 WHERE id = :attempt_id AND submitted_at IS NULL;
--      rowcount must be 1 here; anything else is a bug: ROLLBACK and fail loudly
--      (from PR #327) write the manifest and the history record
COMMIT;                                     -- outcome submitted
```

From PR #328 the read also selects `qa.details_cleaned_at`, and a non-NULL
value is `closed` in step 2 without consulting the clock. Because cleanup acts
only on an assignment whose `submissions_closed_at` is already set (§10.2),
the PR #326 form above already rejects every write to a cleaned assignment.

An unhealthy clock is refused before step 0 (§11.1). The steps marked "no
clock involved" compare no times, but they are reached only by a request whose
clock check passed: a request made while the clock is detectably wrong gets a
503, not `closed`.

- The order of checks 1 and 2 is fixed (§7.5): an attempt that was already
  submitted is answered `already_submitted` even after the cutoff.
- A manual close cannot commit between the check and the update, because the
  submission holds the writer lock.
- The cutoff is exclusive: `now = E + G` is `closed`.
- A request received before the cutoff that obtains the lock after it is
  `closed`. Students are told "submit by" the cutoff, and the page shows it.
- Authorization is not repeated inside the transaction, but the row is: the
  route's `require_quiz_attempt_access` decided who may act, and the
  transaction re-reads the attempt it then acts on, by the same id.
- Grading reads canonical content (`quiz_correct_mc_letters_by_id`,
  `fill_in_subquestions`) inside the same transaction.
- Post-commit side effects (vault notes) run only for outcome `submitted`.

### 11.5 Concurrent double submission — implementation dependency

Today two POSTs for one attempt can both pass the route's early
`if attempt["submitted_at"]` read (`web/blueprints/quiz.py:103`), and
`quiz_submit_attempt` then updates `WHERE id = ?` with no state condition
(`web/services/quiz.py:2207-2213`). The second overwrites the first: answers
through the `ON CONFLICT … DO UPDATE` upsert, `submitted_at` itself, and the
vault note, which is rewritten after each.

This is a **prerequisite of the lifecycle work, not an observation**:

- With grace, a second POST inside the window would rewrite a finished
  submission and move `submitted_at`.
- Durable history (PR #327) records the final score at submission. A
  submission that can be overwritten makes that record wrong.

Required in PR #326:

1. The transaction and outcome order of §11.4.
2. With two concurrent POSTs, the second obtains the lock after the first
   committed, reads `submitted_at` set, and returns `already_submitted`
   without having written anything.
3. The `ON CONFLICT … DO UPDATE` upsert on `quiz_answers` becomes a plain
   insert. With the guard in place a conflict can only mean a bug, and it
   should fail the transaction, not overwrite.
4. The same re-check under the lock in `_store_upload`, which already does it
   for `submitted_at` (`web/blueprints/practical.py:256-261`) and gains the
   closure predicate.
5. Side effects are tied to the outcome: only `submitted` grades, refreshes
   history and writes notes.

### 11.6 Cleanup while a teacher is grading

Review today is one `UPDATE quiz_text_answers`
(`web/blueprints/teacher.py:1004-1012`) preceded by a lookup, outside a
transaction. The handler changes twice, once per phase. The first version
must not mention a column that does not exist yet.

**PR #327 — review with history. No cleanup marker.**

```sql
BEGIN IMMEDIATE;
SELECT qta.id, qta.attempt_id, qta.is_correct, qta.points_possible
  FROM quiz_text_answers qta
  JOIN quiz_attempts qt ON qt.id = qta.attempt_id
 WHERE qta.id = :answer_id AND qt.assignment_id = :assignment_id;
-- no row: ROLLBACK, 404 (as today)
UPDATE quiz_text_answers
   SET teacher_override = :override, teacher_override_status = :status,
       points_awarded = :points, teacher_note = :note
 WHERE id = :answer_id;
-- refresh the mutable grading outcome of that attempt's history record,
-- recomputed from the rows as they now are
COMMIT;
```

**PR #328 — the same transaction with the cleanup guard added.**

```sql
BEGIN IMMEDIATE;
SELECT qta.id, qta.attempt_id, qta.is_correct, qta.points_possible
  FROM quiz_text_answers qta
  JOIN quiz_attempts qt ON qt.id = qta.attempt_id
  JOIN quiz_assignments qa ON qa.id = qt.assignment_id
 WHERE qta.id = :answer_id AND qa.id = :assignment_id
   AND qa.details_cleaned_at IS NULL;
-- no row: ROLLBACK; if the assignment is cleaned, answer "details removed", otherwise 404
UPDATE quiz_text_answers SET … WHERE id = :answer_id;
-- refresh history as in PR #327
COMMIT;
```

| Commit order (PR #328) | Result |
|---|---|
| review, then cleanup | Cleanup reads the reviewed rows. History was refreshed in the review's own transaction, so verification passes and the frozen outcome is the reviewed one. If the review is newer than the run's snapshot, the comparison by content fails and the assignment waits for a later verified snapshot (§10.8). |
| cleanup, then review | The lookup finds no row. Nothing is updated; the teacher sees that the details were removed. |

A teacher who has the results page open while cleanup runs sees a page that is
out of date; the next request shows the cleaned state. No write can land on
deleted rows.

### 11.7 Cleanup while results are read or exported

Three views read attempt detail with several statements on one connection,
outside any transaction:

| View | Function |
|---|---|
| Student result | `quiz_result` (`web/blueprints/quiz.py:188`), `quiz_result_context` (`web/services/quiz.py:2219`) |
| Teacher results | `teacher_assignment_results` (`web/blueprints/teacher.py:945`) |
| CSV export | `teacher_assignment_results_export` (`web/blueprints/teacher.py:1244`) |

Each statement sees the latest committed state. If cleanup commits between two
of them, the view shows attempt rows with no answers: a student sees every
question as unanswered, a teacher downloads a file that looks valid and is
empty.

Required for **all three**, in PR #328 at the latest and recommended in
PR #326 so the pattern exists early:

- All reads of one response happen in one read transaction (`BEGIN` …
  `COMMIT`). Under WAL that is a snapshot: the response is entirely before or
  entirely after any cleanup commit. It takes no writer lock.
- The authorization read (`require_quiz_attempt_access`,
  `_fetch_managed_assignment`) is inside the same transaction as the data it
  guards, so access is decided on the same snapshot that is rendered.
- From PR #328, the view reads `details_cleaned_at` in that transaction and
  branches: full detail before, retained summary after, never a mixture.
- The response body is built before the transaction ends and sent after it.
  The CSV is already built in memory (`io.StringIO`); that stays a
  requirement.

After attempt rows are deleted, the student views authorize through the
mechanism of §9.6. A CSV already downloaded is outside the system (§16,
item 13).

### 11.8 Clock skew and authoritative time

- The only clock that decides anything is the application server's UTC clock
  through `quiz_current_timestamp()`, sampled as §3.4 says. The browser's
  clock, the timer value and `submission_reason` decide nothing.
- That clock is authoritative for serving, not infallible, and it is not
  evidence of UTC. §3.6 defines what is checked, what the checks cannot show,
  and what stops when the clock is in doubt. Deletion is decided by trusted
  time (§10.1), not by this clock.
- Lifecycle writes store the application's `now`, not SQLite's
  `CURRENT_TIMESTAMP`, so the checked instant and the stored instant are equal.
- Client display may drift. The attempt page shows the cutoff as an absolute
  local time rendered by the server, and any countdown is computed from a
  server-supplied remaining duration, as the existing timer does
  (`quiz_remaining_seconds`, `web/services/quiz.py:1733`).
- **Several workers or processes.** Clock-health state is one database row read
  and written under the writer lock, so every worker and every tool sees the
  same state, and one process detecting an anomaly stops all of them. No
  decision relies on per-process memory.
- **Application restart.** Nothing is lost: state, high-water mark and boot
  anchor are in the database. Within the same boot the comparison with the
  anchor continues across the restart, and it is cumulative.
- **Reboot.** The anchor is void. Lifecycle writes wait for synchronisation
  evidence and then write a new anchor (§3.6). That evidence is the host's own
  claim; cleanup needs a time fix of its own in any case (§10.1).
- With more than one application host, the boot-time comparison would not
  carry across hosts and all hosts would need synchronised clocks. Production
  is a single VPS; this is recorded as a risk (§16.5).

### 11.9 Account-owned attempt authorization

Unchanged and restated as a requirement on every new code path:

- Every attempt route calls `require_quiz_attempt_access`
  (`web/attempt_access.py:33`) before reading or writing anything of the
  attempt. A foreign or missing attempt is 404 before any closure notice.
- Starting requires `can_take_quizzes` (`web/access.py:131`) and uses only the
  session's account id. No form field names the owner.
- A teacher or admin has no student-side access to another account's attempt
  (`can_access_attempt`, `web/access.py:146`). Closure, grace and cleanup add
  no bypass.
- Atomicity: the principal is validated per request against the `users` row.
  The start and submit transactions do not need to re-validate the account
  under the writer lock, because disabling an account is not a lifecycle event
  and a request that was authorized when it began may complete. This matches
  current behaviour.
- Historical attempts (`user_id` NULL) stay reachable only through the browser
  marker. They can no longer be started (every new attempt is account-owned).
  A historical unsubmitted attempt follows the same submittable rule.
- History (PR #327) is readable by the owning account only on the student side.

### 11.10 Assignment owner and admin authorization

- Close, edit and duplicate use `@access_policy(ASSIGNMENT_OWNER)` and
  `_fetch_managed_assignment` (`web/blueprints/teacher.py:299`), which applies
  `can_manage_assignment` (`web/access.py:100`): the owner or an admin.
  Everyone else gets 404.
- The ownership check is a read before the write transaction. It does not need
  to be atomic with the close: `owner_user_id` is set at creation and no route
  changes it (`teacher.py:775`). If an ownership-transfer feature is ever
  added, the close and edit updates must add `AND owner_user_id = :uid` for
  non-admins.
- An admin can close any assignment. An admin cannot reopen any assignment;
  there is no such action for any role (§6.4).
- `closed_by_user_id` records the acting account, which may be an admin and not
  the owner.
- The cleanup tool runs as the service user on the server, outside the web
  authorization model. Access to it is server access.

### 11.11 CSRF for manual closure

- The close route is `POST` only. A `GET` to it matches no rule (405), so a
  link, image or prefetch cannot close an assignment.
- It carries `@integrity_policy(SAME_ORIGIN)` (`web/request_integrity.py:21`),
  the mechanism every existing state-changing teacher route uses: the `Origin`
  header, or `Referer` when `Origin` is absent, must match the host.
- Closure is irreversible, so the UI asks for confirmation. Confirmation is a
  usability measure; the integrity policy is the control.
- The close takes no parameters other than the assignment id in the path.
  There is no `closed_at` field, no `state` field and no `reopen` value to
  tamper with.
- The default-deny policy tests (`tests/test_access_policy.py`) must list the
  new endpoint with both policies, as they do for edit and duplicate.

### 11.12 Public-token links after closure

- The token keeps resolving after closure and after cleanup (§6.7, §10.3). It
  is never reused and never rotated; a correction gets a new assignment and a
  new token.
- An anonymous visitor to a closed link sees the closed notice and the
  assignment title, as an open link already shows the title. Nothing about
  attempts, results or students is shown.
- A signed-in account with an attempt is redirected to its own attempt or
  result. An account without one cannot create one.
- The QR route (`teacher_assignment_qr`, `teacher.py:691`) keeps working for a
  closed assignment; the link it encodes leads to the closed notice. Proposed:
  the teacher page states that the link no longer accepts attempts.
- A malformed or unknown token stays the generic 404 without a lookup
  (`quiz_fetch_assignment_by_public_token`, `web/services/quiz.py:294`).
- The retired numeric entry `/quiz/<id>` stays retired
  (`tests/test_numeric_quiz_entry_retired.py`). Closure adds no id-based
  student path.

### 11.13 Pending TOTP

- TOTP applies to admin accounts only (`016_admin_totp.sql`). Between the
  password step and the code step the session holds only the pending keys
  (`PENDING_SECOND_FACTOR_KEYS`, `web/blueprints/account.py:39`), valid for a
  bounded time (`_pending_second_factor`, `account.py:124`).
- A pending session has no principal with an account id
  (`can_take_quizzes` docstring, `web/access.py:131-144`). It is anonymous for
  every policy.
- Consequences, to be pinned by tests: a pending admin cannot close, edit,
  duplicate, review, export or view results, and cannot start or submit an
  attempt. The public entry page behaves as for an anonymous visitor.
- No lifecycle route may read `mfa_uid`. Only `current_principal()` identifies
  the actor.
- Teachers and students have no second factor. Whether irreversible actions
  such as closure should require recent re-authentication is not proposed here.

### 11.14 Other races

| Race | Resolution |
|---|---|
| Edit racing close | Both are conditional updates restating the open predicate (§6.6). If the close commits first the edit changes zero rows and returns 409. A deadline extension cannot land on a closed assignment. |
| Two deadline edits | Serialised by the writer lock. Each re-checks the open predicate at its own `now`; the later commit stores its value. Neither can act on a closed assignment. |
| Two closes | The second changes zero rows (§6.2). |
| Upload racing cutoff | `_store_upload` checks the submittable predicate under `BEGIN IMMEDIATE`, with `now` sampled after the lock. A file written to disk by a refused request is removed by the existing failure path (`practical.py:330`). |
| Upload by another account during grace | 404 from `require_quiz_attempt_access` before any lifecycle check or file handling (`practical.py:174-177`). |
| Cleanup racing a student | Not reachable: eligibility requires `submissions_closed_at` to be persisted, which refuses every student write without consulting the clock, and then both time rules of §10.2. |
| Start racing a manual close in the last second before the deadline | Same as any start racing a close (§11.3). The close stores its sampled time and updates one row. |
| Operator tooling racing a request | Hold reasons, retention floors and the clock state are written under the writer lock. A request sees the state before or after, never a partial one. |
| Cleanup racing backup | Excluded by the shared maintenance lock (§10.11). |
| Two cleanup processes | Excluded by the same lock; each transaction also re-checks its predicate. |
| Disabled account | Loses its session on the next request. Its unsubmitted attempt becomes abandoned at the cutoff. Its attempts and history stay. |

---

## 12. Migration proposal

### 12.1 Current sequence

Verified in the worktree and on `origin/main`: the last migration is
`017_assignment_public_token.sql`. The sequence is `000` to `017` without gaps.
`018` is the next free number at the time of writing. If another PR lands a
migration first, the lifecycle migration takes the next number; nothing in this
design depends on the number.

No migration file is created by this PR.

### 12.2 Migration A — lifecycle columns and clock (PR #326)

```sql
PRAGMA foreign_keys = ON;

ALTER TABLE quiz_assignments ADD COLUMN deadline_at TEXT;
ALTER TABLE quiz_assignments ADD COLUMN display_timezone TEXT;
ALTER TABLE quiz_assignments ADD COLUMN closed_at TEXT;
ALTER TABLE quiz_assignments ADD COLUMN closed_by_user_id INTEGER REFERENCES users(id);
ALTER TABLE quiz_assignments ADD COLUMN expiry_observed_at TEXT;
ALTER TABLE quiz_assignments ADD COLUMN submissions_closed_at TEXT;
ALTER TABLE quiz_assignments ADD COLUMN retention_base_at TEXT;
ALTER TABLE quiz_assignments ADD COLUMN lifecycle_hold TEXT;

CREATE TABLE IF NOT EXISTS lifecycle_clock (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    high_water_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'ok' CHECK (state IN ('ok', 'suspect')),
    state_reason TEXT,
    state_changed_at TEXT,
    ref_wall_at TEXT,
    ref_boot_id TEXT,
    ref_boottime_ms INTEGER
);
INSERT OR IGNORE INTO lifecycle_clock (id, high_water_at) VALUES (1, '1970-01-01 00:00:00');
```

Each addition answers a specific requirement:

| Addition | Needed for |
|---|---|
| `deadline_at`, `closed_at`, `closed_by_user_id` | Deadlines and manual closure. |
| `display_timezone` | Approved default in the teacher's zone (P2); shown for no-deadline assignments too. |
| `expiry_observed_at`, `submissions_closed_at` | Closure and the end of submissions staying terminal when the clock moves (§3.3). |
| `retention_base_at` | Retention never counted from an effective closure that could not be verified (§3.7, §13.4). |
| `lifecycle_hold` | Not serving and not cleaning an assignment whose state is unreconciled, with every unresolved reason kept (§3.8). One TEXT column holds the set, so no hold table is needed and every guard stays `lifecycle_hold IS NULL`. |
| `lifecycle_clock` | Clock-health state shared by all processes (§3.6). `ref_wall_at`, `ref_boot_id` and `ref_boottime_ms` are the boot anchor: written once per boot and by operator confirmation, not by ordinary writes. `state_reason` and `state_changed_at` describe the last clock fault and are carried through a restore (§13.4). |

The shape of this migration is unchanged by the present revision. The
corrections change how two of its elements are used, not what is created.

| Aspect | Proposal |
|---|---|
| Kind | Plain additive migration. Not a table rebuild. No `-- learnpilot-migration: table-rebuild` directive. |
| Nullability | All eight columns nullable, no default. SQLite requires a NULL default for an added `REFERENCES` column while foreign keys are on. |
| Historical rows | NULL in all eight: `OPEN`, no deadline, no zone, no hold. Identical to current behaviour. |
| Backfill | None. Existing assignments do **not** receive a 14-day deadline or a timezone. A retroactive deadline would close tests teachers believe are open, and for older rows would close them in the past. |
| `display_timezone` | IANA name. Validated in the application against the zone database; not constrained in SQL, because the valid set changes with zone data. |
| Constraints | Candidate shape check on the timestamp columns: `CHECK (deadline_at IS NULL OR deadline_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]')`. It guarantees the fixed width that string comparison relies on. It does **not** prove calendar validity (`9999-99-99 99:99:99` passes); the strict parser of §3.4 does. Migrations 014 and 017 add columns without checks; adopting this is a small decision for the implementing PR. |
| Not constrained in SQL | Write-once markers, "deadline in the future", the 180-day maximum, "retention base only rises", the content of `lifecycle_hold` (any non-NULL value holds, so a malformed one fails closed), and the pairing of `closed_at` with `closed_by_user_id`. These are enforced by the application's validated, conditional writes. Triggers could enforce write-once at database level; no migration uses triggers today, so that is an alternative, not a proposal (see also §13.2). |
| `lifecycle_clock` singleton | `CHECK (id = 1)` guarantees **at most** one row. It does not guarantee that the row exists: the seed can be skipped by a failed or partial migration, and the row can be absent after a restore or manual repair. Existence and well-formedness are therefore checked by the application at start-up and in every lifecycle transaction, and a missing or malformed row fails closed (§3.6). |
| Clock seed | A fixed sentinel, `1970-01-01 00:00:00`, not `CURRENT_TIMESTAMP`. Seeding from the real clock would make every test database that then runs under a patched application clock look like a clock regression, and would tie the migration's result to the moment it ran. The first lifecycle write raises the mark to real time. Until then nothing has been observed, so there is nothing a regression could undo. |
| Test fixtures | A test that patches `quiz_current_timestamp` sets the clock row through one helper in `tests/support.py`, consistently with the patched time and with a matching boot anchor. No test relies on the migration seed. |
| Classification | `lifecycle_clock` must be classified `RUNTIME` in `src/canonical_tables.py`. |
| Indexes | None. Request paths read one row by primary key or by the unique `public_token` index. The teacher list filters by owner and already has `idx_quiz_assignments_owner`. |
| `quiz_attempts` | No change. `started_at` keeps its default; new code passes an explicit value. |
| Baseline adoption | No change. `baseline_status` (`src/manage_migrations.py:429`) has rules only for `000` to `006`; every later migration, `007` to `017`, falls through to "manual review required: no baseline rule defined". Baseline adoption is a one-time step for databases that predate the runner (`docs/DEPLOYMENT.md` §9). The lifecycle migration needs no rule and is applied by the normal `--apply`. |
| Tests | `tests/schema_snapshot.py` and a migration SQL test in the style of `tests/test_assignment_public_token_migration_sql.py`. |
| Canonical data | `quiz_assignments` is `RUNTIME` (`src/canonical_tables.py:66`). Nothing is exported; `data/canonical/` is untouched. |

### 12.3 Migration B — durable history (PR #327)

Schema is deliberately not fixed here (§9.8). Constraints on it are in §9.4,
and it includes the submission manifest of §9.5.
It contains nothing related to cleanup: no `details_cleaned_at`, no ledger.

Backfill **is** required and must cover **every existing attempt**: submitted,
abandoned, account-owned and NULL-owner (§9.3). Following the precedent of
migration 017 and `src/manage_assignment_tokens.py`, the backfill is an
explicit operator command with a dry run, not SQL inside the migration and not
application startup code.

The backfill classifies each attempt from positive evidence only, as
`reconstructed` or `unverifiable` (§9.5). It never guesses and never repairs a
plan. Its report lists every unverifiable record. Unverifiable records are
legitimate output of the backfill and a blocker for cleanup.

### 12.4 Migration C — cleanup marker, closure bound, identity and ledger (PR #328)

```sql
ALTER TABLE quiz_assignments ADD COLUMN details_cleaned_at TEXT;
ALTER TABLE quiz_assignments ADD COLUMN closure_upper_bound_at TEXT;

CREATE TABLE IF NOT EXISTS database_identity (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    instance_id TEXT NOT NULL
);
-- plus the pending-unlink ledger table of §10.6 (shape to be fixed in PR #328)
```

Two nullable columns, no backfill, no index. `database_identity` is created
empty: its row is written once per environment by an explicit operator
command, following the precedent of migration 017 and
`src/manage_assignment_tokens.py`, so a copied database does not silently
share an identity that a migration generated (§10.8). All of it is kept out of
migrations A and B, so none of it can exist before the tool that writes it.
`database_identity` must be classified `RUNTIME` in `src/canonical_tables.py`.

### 12.5 Ordering

A, then B, then C. Each is its own file in its own PR. B must not be merged
before A is deployed. C must not be merged before B's backfill has been
verified in production.

### 12.6 Compatibility between schema and code

**New schema, old code — technically runnable, operationally prohibited.**
Checked against the current code:

- Assignment reads use `SELECT qa.*` (`_QUIZ_ASSIGNMENT_SELECT`,
  `web/services/quiz.py:279`) or explicit column lists. Extra columns are
  ignored.
- The only insert names its columns (`quiz_create_assignment`,
  `web/services/quiz.py:257`). New nullable columns take NULL.
- The only update names `title_bg` and `time_limit_minutes`
  (`web/blueprints/teacher.py:776`).
- Production start-up refuses only when a migration **file** is missing from
  `schema_migrations` (`pending_schema_migrations`, `web/production.py:134`).
  It does not object to an applied migration it has no file for.

So pre-lifecycle code starts and serves on the migrated schema without errors,
and **ignores every deadline and closure while doing so**. That it runs is a
hazard, not a rollback path. §13.2 prohibits it.

**Old schema, new code — refused.** New code fails on the missing columns, and
production refuses to start while a migration file is pending
(`create_production_app`, `web/production.py:186`).

**Proposed start-up guard (PR #326).** Production start-up additionally refuses
when `schema_migrations` lists a migration the running build does not ship.
From PR #326 onward this makes "older build on newer schema" fail closed
instead of running without enforcement. It cannot protect against
pre-lifecycle builds, which do not contain the guard; for those the
prohibition is procedural.

The same check applies to every lifecycle command-line tool: a tool from an
older build refuses a database that has a migration it does not ship. This is
what keeps a hold or recovery command from PR #326 from running against a
PR #328 database, where adding a hold reason must also clear the closure
bound (§3.8).

---

## 13. Deployment and rollback

### 13.1 Deployment sequence (each phase)

The standard procedure of `docs/DEPLOYMENT.md` §20 applies. No new service,
environment variable or secret is needed for PR #326.

1. Take a complete pre-deploy snapshot with `src.backup_application`
   (`docs/DEPLOYMENT.md` §16). Do not continue if it fails.
2. Stop the service. The unit's graceful stop lets in-flight requests finish
   (`deploy/systemd/learnpilot.service`, `TimeoutStopSec=40`). From here until
   step 7 no assignment traffic is served.
3. Deploy the code.
4. `python -m src.manage_migrations --db "$DZI_DB" --dry-run`, then `--apply`
   (`docs/DEPLOYMENT.md` §9).
5. `PRAGMA foreign_key_check` returns no rows.
6. Verify before opening traffic (§13.5).
7. Start the service; confirm the health check.

Additional steps per phase:

| Phase | Extra step |
|---|---|
| PR #326 | **Lifecycle activation.** From the moment step 7 completes, §13.2 applies permanently. Confirm the server has timezone data for `zoneinfo`. Announce to teachers that existing tests have no deadline, and that new ones are offered 23:59 local time 14 days ahead. |
| PR #327 | Run the history backfill in dry-run, review, run it, then run the verification report. Review every unverifiable record. Keep the reports. |
| PR #328 | Deploy with cleanup not scheduled. Create the database identity. Confirm on the host that the tool can obtain a time fix, or decide that runs will be attested. Take and verify a snapshot with a manifest, and perform a restore test from it. Run confirmation runs and dry runs for at least one full cycle. Only then install the timer and run the first `--execute` supervised. No detail can be deleted earlier than 30 days after its closure was first confirmed. |

### 13.2 After lifecycle activation: no pre-lifecycle code

**Rule.** Once PR #326 has served traffic in an environment, no build that
lacks lifecycle enforcement may serve assignment traffic in that environment
again. Assignment traffic means the student entry, attempt, submission, upload
and result routes and the teacher create, edit, duplicate, close, results and
export routes.

Running pre-lifecycle code would let students start and submit on closed
assignments and let teachers edit them. That is a reopening. Decision D
forbids it, and this document does not treat it as an acceptable side effect of
a rollback under any circumstances.

What to do instead when a release is bad:

| Step | Requirement |
|---|---|
| Maintenance first | Stop the service, or have Nginx answer a maintenance response for the application. Draining is the unit's graceful stop. Students in flight lose time while the service is down; that is preferable to unenforced access and is covered, within its bound, by the grace window. |
| Prefer a forward fix | Correct the defect and deploy a new build. This is the default. |
| Rollback build | If a rollback is unavoidable, the target must be a build that **retains lifecycle enforcement**: the current code with the faulty change reverted, shipping every migration file that has been applied. A build from before PR #326 is never a rollback target. |
| Schema stays | Additive migrations are not reverted (`docs/DEPLOYMENT.md` §21). Lifecycle columns, their values and `lifecycle_clock` are kept. Terminal markers are never cleared, by a migration or by hand. |
| Guard | The start-up guard of §12.6 makes an older lifecycle-aware build refuse a newer schema. Pre-lifecycle release artefacts are removed from the server's rollback options when PR #326 is deployed, and `docs/DEPLOYMENT.md` §21 is updated in that PR to say so. |
| Verify before reopening traffic | §13.5. |

Optional database-level backstop, not proposed by default: triggers on
`quiz_attempts` that abort an insert, or a first `submitted_at`, when the
assignment has a persisted closure marker. They would hold even against code
that knows nothing about the lifecycle. They cannot evaluate an unobserved
deadline or the grace window without embedding time logic in the schema, and
no migration uses triggers today. Listed as an alternative for the PR #326
review.

Per phase, with the rule applied:

| Phase | Rollback target | Schema | Data consequences |
|---|---|---|---|
| PR #326 | Forward fix, or a lifecycle-enforcing build. Never pre-#326 code. | Kept. | None for closures: they stay enforced. |
| PR #327 | A build that still enforces the lifecycle and ships migrations A and B. If such a build does not refresh history, history is stale for the interval: rerun backfill and verification before relying on it. | History kept. | No detail is lost. Cleanup is not yet installed. |
| PR #328 | Disable the timer first. Then a build that keeps the cleaned-assignment handling of results and review. | `details_cleaned_at` and the ledger kept. | **Deletion is not reversible by rollback.** Cleaned detail returns only through a restore (§13.4). |

### 13.3 Audit after accidental exposure to pre-lifecycle code

If non-enforcing code served traffic after activation, despite §13.2, that is
an incident. Stop the service, restore enforcing code, and before reopening
traffic produce a report covering **starts, submissions and edits**:

| What | How it is found |
|---|---|
| Attempts started after closure | By the rule of §7.1: `started_at >= deadline_at` for a deadline closure, `started_at > closed_at` for a manual one (old code stamps `started_at` with `CURRENT_TIMESTAMP`). |
| Attempts carrying the same second as a manual close | `started_at = closed_at`. Listed as **ambiguous**: stored values cannot order them, and under enforcing code they are valid (§7.1). They matter only if non-enforcing code was serving in that second. |
| Submissions accepted after the cutoff | `submitted_at >= E + G`. |
| Submissions inside the exposure interval on any closed assignment | `submitted_at` within the interval and the assignment closed at that time. Includes grace-window submissions that would have been valid, listed for completeness. |
| Submissions overwritten by a second POST | Not detectable from the live data alone: old code overwrites answers and `submitted_at`. Compare `submitted_at`, scores and answer rows with the last snapshot taken before the exposure. |
| Practical uploads after the cutoff | `practical_submission_files.uploaded_at >= E + G`. |
| Edits to closed assignments | Compare `title_bg` and `time_limit_minutes` with the pre-exposure snapshot. Old code cannot change lifecycle columns; it does not know them. |
| History affected (from PR #327) | Records of the attempts above; rerun verification for them. |

Nothing in the report is deleted or altered automatically. Attempts that were
started after the closure are not in flight (§7.1), cannot be submitted under
enforcing code, and are shown to the teacher as started after closure. What to
do with work submitted during the exposure is a decision for the teacher and
the operator, recorded with the incident.

### 13.4 Restore and reconciliation

A restore replaces the whole database (`docs/DEPLOYMENT.md` §17) with an older
copy. Everything about the lifecycle that changed after the snapshot is
missing from it, in **both** directions: closures are gone, and so are
deadline extensions. Neither direction is safe to serve:

- a lost closure reopens an assignment;
- a lost extension makes an assignment look closed earlier than it was, and
  retention would then be counted from the wrong instant.

Example. The snapshot holds a deadline of 5 January. Afterwards the teacher
extended it to 30 January and then closed the assignment manually on
10 January. The true effective closure is 10 January. The restored row alone
says 5 January. Reconciliation must end with `E` = 10 January, or with the
assignment held.

**Rule.** After a restore, no assignment is served and none is cleaned until
its lifecycle state has been reconciled from sufficient evidence **and every
other hold reason it carries has been resolved**. Traffic does not resume on
the assumption that the restored state is right.

**A restore weakens nothing.** The restored copy is older, and so is whatever
it says about holds, incidents and uncertainty. None of the following may be
overwritten by it, or by the restore procedure:

| State | Rule |
|---|---|
| Hold reasons | The set after the restore is the **union** of the restored row's reasons, the reasons the same assignment carried in the pre-restore database (when that is readable) and `restore_pending`. An existing `clock_review`, `restore_unreconciled` or `restore_reclean` is never replaced by `restore_pending`. |
| Suspect clock incident | The clock state is set to `suspect` after every restore, whatever the snapshot says. If the pre-restore database was `suspect`, its reason and reading are carried over and §3.7 has to be completed for that incident, including its `clock_review` holds. |
| Terminal markers | A marker present in the restored row is kept. A marker present in a pre-restore database that qualifies as evidence A (below) is taken over. If both have the same marker with different values, or the restored row has one the pre-restore database lacks, the evidence contradicts itself: the assignment gets `restore_unreconciled` and keeps the marker it has. No marker is ever cleared. |
| Authoritative lifecycle state | Comes only from sufficient evidence (below). The snapshot's values are not assumed to be right. |
| Retention floor | `retention_base_at` becomes the later of the two values. It is never lowered. |
| History uncertainty (from PR #327) | A record's classification is never upgraded by a restore. `unverifiable` and inconsistent records stay so, whichever database they are taken from. |
| Cleanup uncertainty (from PR #328) | `closure_upper_bound_at` is cleared for every assignment, so retention is confirmed again: after release, or for an assignment held only for `restore_reclean` by the operator path below. Ledger rows from both databases are kept. An assignment that had been cleaned gets `restore_reclean`. |

**What each phase requires of the snapshot.** The restore procedure of
PR #326 uses only what exists when PR #326 ships:

| Phase | Snapshot check before restoring |
|---|---|
| PR #326, PR #327 | The existing procedure of `docs/DEPLOYMENT.md` §17: `PRAGMA quick_check` on the snapshot database and a listing of the archive. Nothing from §10.8 is required, because it does not exist yet. |
| From PR #328 | In addition, the snapshot carries a passing verification record for this database identity and upload root, and its digests match (§10.8). |

**Procedure, in maintenance**

1. Stop the service. Restore the database and the files together, from one
   snapshot, checked as the table above requires for the deployed phase.
2. **Before the service is started**, run the restore-hold command. It adds
   `restore_pending` to every assignment, sets the clock state to `suspect`
   with the reason `restore`, and, when the pre-restore database is readable,
   merges its hold reasons and its clock incident as the table above says.
   From PR #328 it also clears every `closure_upper_bound_at` and the cleanup
   timer is disabled.
3. Complete the clock procedure of §3.7, for the restore and for any incident
   carried over, ending with the operator confirmation. Every assignment is
   held throughout, so a confirmed clock serves nothing yet.
4. Reconcile the authoritative lifecycle state per assignment from the best
   available evidence (table below). The state reconciled is the whole of it:
   `deadline_at` as last set (extensions and reductions alike),
   `display_timezone`, `closed_at` and `closed_by_user_id`,
   `expiry_observed_at`, `submissions_closed_at`, `retention_base_at`, and
   through them `E`.
5. Resolve `restore_pending` only for assignments whose reconciliation is
   complete. Resolve every other reason by its own condition (§3.8). An
   assignment is released when its set is empty, not before.
6. From PR #327, re-verify history. From PR #328, reconcile cleanup state.
   Both are described below.
7. Verify (§13.5).
8. Resume traffic. Only assignments with an empty hold set are served.

The application cannot tell by itself that its database was replaced. Step 2
is therefore part of the documented restore commands in `docs/DEPLOYMENT.md`,
immediately after the copy and before the start. A restore carried out
without it is a deviation from procedure and is listed as a risk (§16.5).

**Evidence, in order of preference**

| Evidence | Sufficient when | Result |
|---|---|---|
| A. The pre-restore database, which the restore procedure moves aside | It opens, passes `PRAGMA integrity_check`, its lifecycle columns parse strictly, and the reason for the restore does not put those columns in doubt (the operator records why). | It is the authoritative latest state. Its lifecycle columns replace the restored ones for every assignment present in both, in either direction, **subject to the table above**: hold reasons are merged, terminal markers are never cleared, the floor is never lowered. In the example: deadline 30 January, `closed_at` 10 January, `E` = 10 January. `restore_pending` is resolved; other reasons stay. |
| B. A complete lifecycle event log kept outside the database (proposed, see below) | It is contiguous from the last event contained in the snapshot to its end, and its last line is intact. | Replayed per assignment to the same authoritative state. An event that was logged but may not have committed makes that one assignment ambiguous: it gets `restore_unreconciled`. |
| C. Partial evidence: an incomplete log, application log lines, a person's recollection | Never sufficient to release an assignment as it stands. | See "when evidence is incomplete". |

Reconciliation changes `deadline_at` only on a restored row that carries no
terminal marker, and only to the value evidence A or B gives.

**When evidence is incomplete**

| Situation | Enforcement |
|---|---|
| The set of assignments that changed after the snapshot **can** be established (for example a complete list of ids from the log, with incomplete values) | `restore_pending` is resolved for the unaffected assignments. The affected ones get `restore_unreconciled`. Traffic resumes only for assignments whose set is then empty. |
| The set of affected assignments **cannot** be established | Every assignment stays held. That is assignment traffic in maintenance, enforced by the holds, until an operator resolves each one. |
| An individual held assignment, with a record of its state | An operator enters the state from a record the system produced at the time (an application log line or an event-log entry showing the event, its instant and its actor), and the record is kept with the resolution. The entry can add a closure or set a deadline on a row without a terminal marker; it cannot clear or change a marker. |
| An individual held assignment, without such a record | **It stays held.** A person's recollection is not evidence of a timestamp. No command ends it, closes it, stamps `submissions_closed_at` or sets a retention floor in order to release it. The teacher's remedy is a new assignment made on the normal creation form. |

Telling a teacher that an assignment's state was lost is not a resolution. The
assignment stays held until an operator has resolved every reason in the
system. Ending a held assignment without evidence is **not approved** and is
not part of any PR in this document (§16.3, item 17).

**Cleanup state (from PR #328)**

- An assignment that had been cleaned before the restore has its detail back,
  with `details_cleaned_at` NULL. That is honest about what the database now
  contains. Student writes stay impossible when `submissions_closed_at` is
  reconciled as set; until it is, the hold refuses them.
- Such assignments get `restore_reclean`, added to their other reasons.
  Ordinary scheduled cleanup does not touch them, and an ordinary
  confirmation run records no bound for them. The operator re-clean below is
  the only path, and it requires the authoritative history of the next point.
- No closure bound survives the restore. An assignment without
  `restore_reclean` has to be confirmed again after release, and is cleaned no
  earlier than 30 days after that (§10.1).
- Ledger rows in the restored database are retried by the next execute run.

**Operator re-clean of a restored, previously cleaned assignment (from
PR #328)**

Three rules, taken literally, never meet: `restore_reclean` is resolved only
by a completed re-clean; a re-clean needs a closure bound and the full
retention interval after it; and an ordinary run records a bound only for an
assignment without a hold. The path below is the one place where a closure is
confirmed, and detail deleted, while a hold is in place. It serves nothing:
it does not touch `lifecycle_hold` until its last step, and every serving
guard reads only that column.

It applies only when **all** of these hold:

1. **The hold is exactly `restore_reclean`.** The stored value is compared as
   a whole with that single reason. A set that contains any second reason, and
   a value that does not parse, are refused. If a reason is added later, the
   bound is cleared (§3.8) and the assignment is outside this path until that
   reason has been resolved.
2. **An operator runs it and names the assignment.** No timer and no
   scheduled run uses it; neither `--confirm-closures` nor a plain `--execute`
   reaches a held assignment.
3. **Lifecycle reconciliation is authoritative.** `restore_pending` was
   resolved from evidence A or B, which is the only way the set can have
   shrunk to this one reason. The reconciled row shows a persisted closure
   (`closed_at` or `expiry_observed_at`), a persisted end of submissions
   (`submissions_closed_at`), a NULL `details_cleaned_at`, and times that
   parse strictly, with no factual timestamp or terminal marker later than
   `now` (§3.9).
4. **History is verified complete.** Every attempt of the assignment has a
   **frozen** record that is `recorded` or `reconstructed`, neither
   unverifiable nor inconsistent. Only cleanup freezes a record (§9.4), so on
   a restored assignment a frozen record can only be the authoritative one
   imported from the pre-restore database. The attempts in the restored
   database are exactly the attempts that have such a record, and the
   verification of §10.4 passes against the restored detail.

If 3 or 4 fails, nothing is written and the assignment stays held. Two cases
are worth naming. An assignment restored without evidence A has no frozen
record. An assignment whose restored detail is older than its record, because
a review or a submission was made after the snapshot, fails §10.4. Both keep
`restore_reclean`. That is a blocked state and not a loop: the detail is
kept, the hold stands, and deleting it is a deliberate decision outside this
tooling, as it is for any unverifiable record (§9.5).

**Sequence.** Each step is a separate operator action or a separate
transaction, and each restates its precondition in its `WHERE` clause.

1. **Confirm the closure.** The run needs requirements (a) to (e) of §10.1
   and the maintenance lock. The order is that of every bound: the tool reads
   the row as committed and checks conditions 1 to 4; only then does it obtain
   a new time fix; then it records the bound.

   ```sql
   UPDATE quiz_assignments
      SET closure_upper_bound_at = :F_hi
    WHERE id = :id
      AND lifecycle_hold = 'restore_reclean'
      AND closure_upper_bound_at IS NULL
      AND details_cleaned_at IS NULL
      AND submissions_closed_at IS NOT NULL
      AND (closed_at IS NOT NULL OR expiry_observed_at IS NOT NULL);
   -- and the markers and retention_base_at equal the values read before the probe
   ```

   One column is written. No observation marker is stamped (§6.3 stamps none
   while an assignment is held), and no hold, marker, floor or history record
   is touched.
2. **Wait out the full retention interval.** Nothing shortens it. The re-clean
   run must find, with a fix of its own,
   `F.lo >= max(closure_upper_bound_at, E, retention_base_at) + max(R, G)`,
   and the server-clock rule as well (§10.2, items 4 and 5).
3. **Re-clean.** An operator runs the cleanup tool with `--execute` for the
   named assignment. Every gate of §10.1 applies, including the verified
   snapshot that holds the detail by content. Eligibility is §10.2 with item 2
   read as "exactly `restore_reclean`, named by the operator"; items 1 and 3
   to 11 are unchanged. The transaction is that of §10.3, and its last
   statement restates the hold. The hold stays in place and the frozen records
   are not modified.
4. **Verify, then resolve.** In a separate transaction after the deletion has
   committed, the tool checks that `details_cleaned_at` is set; that no row of
   its attempts is left in `quiz_answers`, `quiz_text_answers`,
   `practical_submissions` or `practical_submission_files`; that the attempt
   rows are in their post-cleanup form (§9.6); that every frozen record is
   still present and unchanged; that each of its files is unlinked or on the
   ledger; that `PRAGMA foreign_key_check` returns nothing; and that the hold
   is still exactly `restore_reclean`. Only then is the reason resolved,
   logged with the operator, the fix and the id of the snapshot's verification
   record. If any check fails, the hold stays.

| After | `lifecycle_hold` | `closure_upper_bound_at` | `details_cleaned_at` | Served |
|---|---|---|---|---|
| Restore-hold step | `restore_pending,restore_reclean`, plus any reason carried over | NULL | NULL | No |
| Reconciliation, and resolution of every other reason | `restore_reclean` | NULL | NULL | No |
| Step 1, operator confirmation | `restore_reclean` | recorded | NULL | No |
| Step 3, operator re-clean | `restore_reclean` | recorded | set | No |
| Step 4, verification and resolution | NULL | recorded | set | Yes, as a cleaned assignment (§8.4) |

**Failure and interruption.** At every point the assignment is still held, and
the next run continues from what is stored:

| Interrupted or failed | State left behind | Retry |
|---|---|---|
| Before the update of step 1 commits | Hold in place, no bound. | Run step 1 again. |
| No time fix, or requirement (e) fails | Nothing written. | Run step 1 again when the evidence is available. |
| Another reason is added after step 1 (a clock incident, a second restore) | Bound cleared; the set has two reasons. | The path refuses it. Resolve the other reason, run step 1 again, and wait a further full interval. |
| Step 3 attempted before the interval has passed | Nothing written. Reported as not old enough. | Run step 3 later. |
| During the transaction of step 3 | Rolled back: detail intact, hold and bound unchanged. | Run step 3 again. |
| After step 3 commits, before its files are unlinked | Cleaned in the database, hold in place, files on the ledger. | The next run retries the ledger first, finds `details_cleaned_at` set, skips the deletion and goes to step 4. |
| After step 3 commits, before step 4 | Cleaned, hold in place. | The same: step 4 only. |
| During the transaction of step 4 | Rolled back: hold in place. | Run step 4 again. |

No step takes effect twice: step 1 requires a NULL bound, step 3 a NULL
`details_cleaned_at`, and step 4 a set `details_cleaned_at` together with the
exact hold.

What the path never does:

- It never serves the assignment. Student starts, submissions and uploads,
  and teacher edits, closes, reviews and exports, get the "unavailable"
  response for as long as the hold is there. `submissions_closed_at` refuses
  student writes independently of it.
- It never reopens anything. It writes no `deadline_at`, clears no terminal
  marker and lowers no floor; `closed_at`, `expiry_observed_at`,
  `submissions_closed_at`, `retention_base_at` and the history records are as
  they were, and `details_cleaned_at` is only ever set.
- It never bypasses trusted time, the retention interval, the history
  verification or the snapshot gate. The only condition that differs from an
  ordinary run is the value `lifecycle_hold` must have.
- It never applies to another hold. `clock_review`, `restore_pending` and
  `restore_unreconciled` have no such path, alone or in combination.

**History state (from PR #327)**

- With evidence A, history records in the pre-restore database are newer than
  the restored ones. For assignments that had been cleaned, the pre-restore
  record is the only correct final outcome (it was verified at cleanup, and
  reviews made after the snapshot are in it and not in the restored detail).
  Those records are imported with the classification they had.
- For all other assignments, restored detail and restored history are
  consistent with each other but older. Reviews and submissions made after the
  snapshot are lost with the restore. History is re-verified against the
  manifest (§9.5) before `restore_pending` is resolved.
- Without evidence A, a previously cleaned assignment has restored detail that
  may lack later reviews, and no authoritative final record. It stays held and
  is never cleaned by the ordinary tool.

**Lifecycle event log (proposed, pending confirmation, §16.2 item I5).** An
append-only file under the state directory, outside the database. Each
lifecycle write appends one line, flushed to disk **before** its database
transaction commits: a sequence number, assignment id, event, old and new
values, instant, acting account id. The database stores the last sequence
number it contains, so a snapshot knows where its knowledge ends. Without this
log, evidence B does not exist: a restore whose pre-restore database is
unreadable leaves every assignment held until resolved from a record, one by
one. The log reduces that cost, and it would let start-up notice a database
older than the log. It is not needed for safety, which the holds provide, and
nothing in this document depends on it.

**What is lost regardless.** Assignments created, and attempts and submissions
made, after the snapshot are not in the restored database. That is inherent in
a restore and is stated to the people concerned.

### 13.5 Verification before traffic is reopened

After any deploy, rollback, incident or restore:

- `schema_migrations` matches the migration files of the build about to run.
- `PRAGMA foreign_key_check` returns no rows.
- Every stored lifecycle time parses strictly (§3.4).
- **No factual event timestamp and no terminal marker is later than lifecycle
  time** (§3.9). Exceptions are listed and held.
- **Scheduled deadlines in the future are expected** and are not exceptions.
  Only a deadline further ahead than validation could have accepted is
  reported, as a data anomaly (§3.9).
- Every non-NULL `lifecycle_hold` parses as a set of known reasons. One that
  does not is reported; it holds its assignment either way.
- No assignment has `closed_by_user_id` without `closed_at`.
- The `lifecycle_clock` row exists, is the only row, is well-formed, and its
  `state` is `ok`. A missing or malformed row is repaired only through the
  operator confirmation of §3.7, never by re-running the seed blindly.
- The operator has verified the clock against an independent source (§3.7).
  This is a manual check and is all that PR #326 and PR #327 have. It does not
  establish a time fix; only the cleanup tool does that, per run (§10.1).
- After a restore: the restore-hold step was run, no reason was resolved
  without its condition, and the number of held assignments is reported with
  their reasons.
- A closed assignment refuses a start and an edit in a smoke test; an open one
  accepts both; a held one refuses everything.
- The timezone database is available to the application: the deadline form
  lists zones and a known zone converts correctly.
- From PR #327: the history verification report shows no unexpected change,
  and no record's classification was upgraded.
- From PR #328: the cleanup timer is in the intended state, the ledger is
  reported, the database identity row exists, and directly after the
  restore-hold step no `closure_upper_bound_at` remains. Assignments held for
  `restore_reclean` are listed with the step of the operator path each has
  reached (§13.4).

---

## 14. Phased implementation roadmap

Planning targets. This document does not create or implement these PRs. The
phases are not to be combined.

### 14.1 PR #326 — lifecycle schema, deadline, closure, grace, submission race

The five product decisions this PR depended on are approved (§1.2). **It is
not fully unblocked:** implementation details I1 to I5 are proposed and must
be confirmed before merge (§16.2).

Contains:

- Migration A (§12.2): eight columns and `lifecycle_clock`.
- One pure lifecycle function (§5.7), the strict serialiser and parser (§3.4).
- Clock health (§3.6): the shared clock row with its boot anchor, the checks
  in every lifecycle writer transaction, the one narrowly scoped clock-fault
  bookkeeping write, the sticky `suspect` state, and the operator inspection,
  recovery and confirmation commands (§3.7). Recovery writes no closure
  marker.
- Timestamp classes (§3.9), applied in the clock checks and in deployment
  verification: a scheduled deadline in the future is valid.
- Terminal markers `expiry_observed_at` and `submissions_closed_at`, with
  best-effort observation stamping (§6.3).
- Holds as a set of reasons (§3.8), enforced on every assignment route; the
  restore-hold and reconciliation commands (§13.4), built on the restore
  procedure that exists today. No command ends a held assignment without
  evidence. The lifecycle event log only if I5 is confirmed.
- Timezone support (§5.3): the IANA selector on the create, duplicate and edit
  forms, server-side validation, the stored zone for every assignment, and the
  local-to-UTC and UTC-to-local helper pair. The server timezone is never used.
- Deadline on create and edit with the explicit "no deadline" choice, the
  23:59 default 14 calendar days ahead (P2) and the 180-day maximum (P3).
- Duplication with the same choice as creation (P5, §5.5).
- Manual close route with owner or admin authorization and same-origin
  integrity (§6.1, §11.11); audit fields from the principal only; the sampled
  closure time stored unchanged; ordering against starts by guarded
  transactions (§11.3); a zero-row update classified, never reported as a
  closure.
- Title, time limit and deadline frozen after closure (P4, §6.6), with the
  field whitelist of §6.4.
- No reopening, with its regression test (§6.4), and the start-up guard
  (§12.6), which also requires a well-formed clock row.
- Transactional start with the open predicate (§11.2).
- Transactional submit with the three outcomes and their order (§7.5, §11.4).
  **The double-submit fix (§11.5) is part of this PR and may not be deferred.**
- The same predicate in practical uploads.
- The 15-minute grace window (P1), from one named constant.
- Closed and grace notices; lifecycle state on teacher pages; the vault
  assignment note writes the computed state.
- Snapshot reads for student result, teacher results and CSV (§11.7),
  recommended here.
- `docs/DEPLOYMENT.md` updates for §13.2 and §13.4.

Does not contain: any history table, the submission manifest, any deletion,
`details_cleaned_at`, `closure_upper_bound_at`, `database_identity`, the
ledger, any timer or systemd unit, the maintenance lock, snapshot manifests or
snapshot verification, external time-source queries (those belong to the
cleanup tool), any command that ends a held assignment without evidence, a
per-account timezone preference, server-side enforcement of
`time_limit_minutes`. **Cleanup is entirely disabled in PR #326.**

### 14.2 PR #327 — durable student learning history

Preceded by its own design PR (§9.8).

Contains:

- Migration B (§12.3).
- A durable record for **every** attempt: submitted, abandoned, account-owned
  and NULL-owner, including attempts with practical uploads (§9.3).
- The immutable context snapshot and the mutable grading outcome as separate
  parts (§9.4), including the **final mixed-quiz score** and the **grading
  status**.
- Strict parsing of the raw plan text, the submission manifest written by the
  winning submission, the manifest-to-detail invariant, and the `recorded`,
  `reconstructed` and `unverifiable` classification from independent evidence
  of completeness (§9.5). An open question of a pre-manifest attempt is
  unverifiable unless a slot-coverage witness is accepted; an unverifiable
  attempt keeps its history.
- History written by the winning submission and refreshed inside the teacher
  review transaction, in the PR #327 form of §11.6, which has no cleanup
  column. A refresh never overwrites a verified outcome with values computed
  from detail that no longer matches the manifest.
- The result-access design of §9.6 (stub or history-based authorization),
  implemented and tested, though nothing is deleted yet.
- Backfill command with dry run, and the verification report.
- Student and teacher views that can render from history alone.

Does not contain: any deletion, `details_cleaned_at`,
`closure_upper_bound_at`, `database_identity`, the ledger, the cleanup tool,
any time-source query, any timer. **Cleanup is entirely disabled in
PR #327.**

Depends on: PR #326 deployed, in particular the submission outcomes. Blocked by
§16 items 5, 9, 11 and 19, and item 14 if practical results are part of
history from the start.

### 14.3 PR #328 — monthly cleanup

Contains:

- Migration C (§12.4): `details_cleaned_at`, `closure_upper_bound_at`,
  `database_identity` and the pending-unlink ledger.
- Trusted time (§10.1): the time fix from independent sources or operator
  attestation, the closure upper bound recorded only from a fix obtained after
  the closure was read, the trusted retention rule, and confirmation runs. If
  the evidence is unavailable the run is skipped.
- The cleanup tool of §10: dry-run on a snapshot by default, gate checks,
  per-assignment transactions, evidence-based verification of every attempt,
  file removal with ledger and retry, audit summary.
- The shared maintenance lock in the cleanup tool **and** in
  `src.backup_application` (§10.11); the snapshot manifest, the verification
  record, the provenance gate and the comparison of detail with the snapshot
  by content (§10.8).
- The hold tooling extended to clear the closure bound whenever a reason is
  added, the restore procedure extended to require a verified snapshot of
  this environment, and the `restore_reclean` reason (§3.8, §13.4).
- The operator re-clean path for an assignment held only for
  `restore_reclean`: confirmation of the closure with the hold in place,
  re-clean after the full retention interval, verification, and only then
  resolution of the reason; every step retryable (§13.4).
- The PR #328 form of the review transaction, and cleaned-state handling in
  student result, teacher results and CSV (§8.4, §11.6, §11.7).
- systemd unit and timer templates under `deploy/systemd/`, and the
  `docs/DEPLOYMENT.md` section. Installing them on the server is a deployment
  step, not part of the merge.

Depends on: PR #327 deployed, backfilled and verified in production; a restore
test. Blocked by §16 items 6, 7, 8, 10, 13, 15 and 18, and item 14 if not
settled earlier.

### 14.4 Separate prerequisite — persisted practical grading

Practical review has no stored outcome: no migration creates
`practical_submission_grades`, and no code moves
`practical_submissions.status` beyond `'draft'` (§2.1). Consequences:

- History can record that practical work was uploaded, but not whether it was
  graded or what it scored.
- Under §8.2 an assignment with practical uploads, on submitted or abandoned
  attempts, is always "pending" and is never cleaned.

This needs its own PR, with its own migration, between PR #326 and PR #328. It
is **not** part of PR #326, #327 or #328. It does not block PR #326. It blocks
PR #327 only if history is to include practical results from the start (§16,
item 14). Without it, PR #328 can still ship; assignments with practical
uploads then stay blocked and are reported on every run.

### 14.5 Dependency summary

```
PR #326 ──► PR #327 (design, then implementation, backfill, verification) ──► PR #328
   │                         ▲
   └──► practical grading ───┘ (needed for practical results in history and for
        persistence PR         cleaning assignments that have practical uploads)
```

---

## 15. Acceptance criteria and test matrix

Tests build temporary databases from migrations and canonical data
(`tests/support.py`) and never touch `data/questions.db`. Time is controlled by
patching `quiz_current_timestamp`, as `tests/test_quiz_attempt_render.py`
already does. Concurrency tests use two connections to one temporary database
file. `T0` is the creation instant; `E`, `G`, `R` as defined above. Case ids
carry the phase: `L` for PR #326, `H` for PR #327, `C` for PR #328.

### 15.1 PR #326

Timezone cases use `Europe/Sofia` (DST changes on the last Sundays of March
and October) and one zone with a different offset and DST calendar, with the
process timezone set to a third zone so that any use of the server zone fails
the test.

| # | Area | Case | Expected |
|---|---|---|---|
| L1 | Default deadline | Render `/teacher/new` | Deadline prefilled as "through 23:59" on today's local date + 14 calendar days in the preselected zone; the zone is shown and editable. |
| L2 | Default deadline (I4) | POST the prefilled value | `deadline_at` is the next local midnight (start of date + 15) converted to UTC, stored as `YYYY-MM-DD HH:MM:SS`; `display_timezone` is the selected name. |
| L3 | Default across DST | `Europe/Sofia`. Created 2026-03-20 (UTC+2), DST starting 2026-03-29; and created 2026-10-15 (UTC+3), DST ending 2026-10-25 | Displayed "through 23:59" on 2026-04-03 and 2026-10-29. Stored `2026-04-03 21:00:00` and `2026-10-29 22:00:00`: the offset of the **target** date is applied, not that of the creation date. No comparison with `now + 336 h` is made. |
| L4 | Default, calendar days | Default computed at 00:05 and at 23:55 local on the same date | Same displayed deadline and same stored cutoff both times. |
| L5 | UTC persistence | Same local deadline entered in two different zones | Two different UTC `deadline_at` values, each equal to the correct conversion; enforcement uses only the UTC value. |
| L6 | No server timezone | Process timezone set to a third zone | Defaults, conversions and displays are unchanged. |
| L7 | Timezone input | POST with an unknown zone name, or with no zone | 400; no row. |
| L8 | Explicit choice | POST with neither a deadline nor "no deadline" | 400; no row created. |
| L9 | No deadline | POST with "no deadline" and a zone | `deadline_at` NULL; `display_timezone` stored; state `OPEN`. |
| L10 | No deadline | Same assignment, clock moved 400 days ahead | Still `OPEN`; start and submit work. |
| L11 | Lower bound | Deadline equal to or before `now` | 400. |
| L12 | Maximum, at boundary | Displayed deadline "through 23:59" on local date today + 180 | Accepted; stored cutoff is local midnight starting today + 181. |
| L13 | Maximum, beyond | Displayed deadline at 00:00 on local date today + 181, and one a year ahead | 400 with a validation message; nothing stored. |
| L14 | Maximum across DST | Boundary date reached across one and across two DST changes | Decided by the local date of the displayed deadline, not by elapsed hours: today + 180 accepted, today + 181 rejected. |
| L15 | Maximum on edit (I3) | Edit an open assignment 30 days after creation | Boundary is 180 days from the edit date. |
| L16 | Maximum and no deadline | "No deadline" selected | Accepted; the maximum does not apply. |
| L17 | DST input | Typed local time in the spring gap | 400 with a validation message. |
| L18 | DST input | Typed local time in the autumn overlap | Stored as the earlier UTC instant. |
| L19 | Strict input | Deadline text with an offset, a `T`, fractional seconds, or `2026-02-30` | 400; nothing stored. |
| L20 | Duplication, default | Open the duplicate form | Same choice and zone selector as creation, prefilled with the 23:59 default 14 calendar days ahead. |
| L21 | Duplication, with deadline | Duplicate with the default deadline | Copy has that deadline and zone, and is `OPEN`. |
| L22 | Duplication, no deadline | Duplicate a source that has a deadline, choosing "no deadline" | Copy has `deadline_at` NULL. |
| L23 | Duplication, no silent copy | Duplicate sources that are open with a deadline, expired, and manually closed | The copy never carries the source's deadline, zone, `closed_at`, `closed_by_user_id` or `expiry_observed_at`. |
| L24 | Duplication, no implied deadline | Duplicate POST without a valid choice | 400; no copy created. |
| L25 | Duplication, identity | Any successful duplicate | New id, acting account as owner, a new `public_token` different from the source's; no attempts copied. |
| L26 | Duplication, limits | Duplicate with a deadline beyond 180 days | 400; no copy. |
| L27 | Manual close | Owner POSTs close on an `OPEN` assignment | One row updated. `closed_at` equals the sampled `now` exactly; `closed_by_user_id` = owner. |
| L28 | Manual close | Close again | No column changes; same redirect. |
| L29 | Manual close | Close when the deadline has already passed | `closed_at` stays NULL; `E` = `deadline_at`; `expiry_observed_at` stamped. |
| L30 | Manual close | Close before a later deadline | `E` = `closed_at`; `deadline_at` unchanged. |
| L31 | Forged audit fields | Close POST carrying `closed_at`, `closed_by_user_id`, `expiry_observed_at` | Ignored: stored values are the server's `now` and the session principal. |
| L32 | Field whitelist | Edit POST on an open assignment carrying a valid deadline plus forged closure, owner and token fields | Deadline, zone, title and time limit updated; the forged fields unchanged. |
| L33 | Title freeze, manual close | Direct edit POST changing the title after a manual close, as owner and as admin | 409; title, time limit and deadline unchanged. |
| L34 | Title freeze, expiry | Direct edit POST changing the title after the deadline passed, observed and not yet observed | 409; nothing changed; the observation is stamped. |
| L35 | Title freeze, authorization | Edit POST on a closed assignment by another teacher, a student, anonymous | Same 404, 403 or login redirect as on an open one; no lifecycle message for a foreign assignment. |
| L36 | No reopening | Edit POST on a closed assignment carrying a later deadline or "no deadline" | 409; no column changes. |
| L37 | No reopening | (a) Static check over `web/` and `src/`. (b) Behavioural: every lifecycle-writing function and every operator command is run against assignments in each closed state | (a) Every statement that assigns a lifecycle column is in the lifecycle module or the reconciliation command, and none sets a terminal marker to NULL. (b) No terminal marker is cleared or changed. `deadline_at` changes only through the edit statement with the open predicate, or through reconciliation from evidence A or B on a restored row that carries no terminal marker (§13.4). Neither check is accepted without the other. |
| L38 | Expiry without jobs | Create with a deadline, advance the clock past it, run nothing | Entry page shows closed; start refused. |
| L39 | Observation marker | First request after the deadline, a `GET` of the entry page | `expiry_observed_at` stamped once; a second request does not change it. |
| L40 | Observation marker | Marker stamped 30 days after the deadline | `E` and `retention_eligible_at` are still computed from `deadline_at`. |
| L41 | Deadline boundary | Start at `E - 1 s` | Attempt created, `started_at = E - 1 s`. |
| L42 | Deadline boundary | Start at `E` | Refused; no row. |
| L43 | Lock boundary | Start request issued at `E - 1 s` while another connection holds the writer lock until `E` | Refused: time is sampled after the lock. |
| L44 | Lock boundary | Submit request issued at `E + 15 min - 1 s`, lock obtained at `E + 15 min` | `closed`; nothing stored. |
| L45 | Deadline boundary | Edit deadline at `E - 1 s` to a later time | Accepted. |
| L46 | Deadline boundary | Edit deadline at `E` | 409. |
| L47 | Concurrent edits | Two deadline edits, both orders | Later commit's value stored; both validated against the open predicate. |
| L48 | Concurrent close and edit | Deadline extension or title change racing a manual close, both orders | Close first: edit 409, nothing changed, `E` = `closed_at`. Edit first: edit stored, then the close succeeds. |
| L49 | Grace | Attempt started before `E`, submit at `E + 1 s` | `submitted`; `submitted_at = E + 1 s`. |
| L50 | Grace boundary | Submit at `E + 15 min - 1 s` | `submitted`. |
| L51 | Grace boundary | Submit at exactly `E + 15 min` | `closed`; no answer rows; `submitted_at` NULL. |
| L52 | Grace eligibility | Attempts written directly in the test database and submitted inside the window: `started_at >= deadline_at` on an expired assignment; `started_at > closed_at` on a manually closed one; `started_at = closed_at` on a manually closed one | `closed`; `closed`; `submitted`. The first two are not in flight (§7.1) and are refused without comparing `now`. An equal second with a manual close is in flight. |
| L53 | Grace constant | Inspect the code | The 15 minutes come from one named constant; no other literal encodes the duration. |
| L54 | Grace and retention | Submission at `E + 14 min` | `retention_eligible_at` is still `E + R`. |
| L55 | After closure | GET entry, no attempt, anonymous and signed in | Closed notice; no start form. |
| L56 | After closure | POST entry, no attempt | 409; no row. |
| L57 | After closure | Entry with an existing unsubmitted attempt, in grace | Redirect to the attempt; form shown with the cutoff in the assignment's zone. |
| L58 | After closure | GET attempt after the cutoff | Closed notice; no questions rendered. |
| L59 | After closure | Entry or attempt with a submitted attempt | Redirect to the result. |
| L60 | After closure | Manual close while an attempt is open; next GET | Grace notice with cutoff `closed_at + 15 min`. |
| L61 | Concurrent start | Two starts for one account | One row. |
| L62 | Concurrent start | Start and close transactions, both orders, including in the same wall-clock second | Close first: no attempt. Start first: `closed_at` is the sampled time of the close, equal to `started_at` in the same-second case and never later than `now`; the attempt is in flight and submittable in grace. No stored time is adjusted. |
| L63 | Concurrent submit | Submit and close transactions, both orders | Both orders store the submission (the second as a grace submission). |
| L64 | Double submit | Two submits with different answers | First is `submitted`; second is `already_submitted`; answer rows, scores and `submitted_at` are the first's. |
| L65 | Double submit side effects | Same as L64, observing side effects | Grading ran once; the attempt note was written once; the second request wrote no row in any table. |
| L66 | Double submit after cutoff | Submitted attempt, second POST at `E + 15 min + 1 h` | `already_submitted`: redirect to the result, not the closed notice. |
| L67 | Failure before commit | Error raised after answers are inserted and before commit | No answer rows; `submitted_at` NULL; no note; a retry while permitted succeeds. |
| L68 | Upsert removed | Attempt to insert a second `quiz_answers` row for the same question | Transaction fails; nothing overwritten. |
| L69 | Grading during closure | Teacher review POST on a closed assignment | Accepted; points and status updated. |
| L70 | Historical assignments | Row created before migration A | All lifecycle columns NULL; `OPEN`; start, submit, edit, duplicate work. |
| L71 | Historical attempts | Unsubmitted `user_id` NULL attempt with the browser marker, assignment then closed | Same grace and cutoff rules. |
| L72 | Mixed scores | Mixed assignment submitted in grace | Multiple-choice score stored; open answers recorded; combined score derived as before. |
| L73 | Practical uploads | Upload while open, in grace, after cutoff | Accepted, accepted, refused with no row and no file left on disk. |
| L74 | Practical uploads | Upload to another account's attempt during grace | 404; no file written; no lifecycle message. |
| L75 | Clock regression | Manual close, then clock set to before `closed_at`, beyond the tolerance | Read pages show closed. A start gets 503 and the state becomes `suspect` through the bookkeeping update. After operator recovery: with a verified time at or after `closed_at`, a start is refused as closed by the marker; with a verified time before `closed_at`, the assignment is held (`clock_review`). It is startable in neither case. |
| L76 | Clock regression | Deadline passed and observed, then clock set to before the deadline, beyond the tolerance | As L75, with `expiry_observed_at` as the marker: start, edit and close get 503; after recovery a start is refused by the marker, or the assignment is held if the marker lies after the verified time. `expiry_observed_at` is unchanged and the assignment is startable in neither case. |
| L77 | Clock regression | Regression within the small tolerance | Lifecycle time equals the high-water mark; requests proceed; state stays `ok`. |
| L78 | Clock regression | Regression beyond tolerance | `state = suspect` is persisted by the one bookkeeping update. Every lifecycle write, including a submission for an in-flight attempt and an upload, gets 503 and writes nothing else. Health check degraded. |
| L79 | Clock recovery is not silent | After L78, the clock is set back to the correct time | Still `suspect`; writes still refused until the operator confirmation of §3.7. |
| L80 | Malformed timestamps | `deadline_at` with a non-calendar or mis-shaped value written directly in the test database | Assignment treated as closed for students and frozen for edits; reported. |
| L81 | Malformed timestamps | Attempt with malformed `started_at` on a closed assignment | Not submittable. |
| L82 | Authorization | Close as another teacher, as a student, anonymous | 404, 403 or redirect to login as for the edit route; no change. |
| L83 | Authorization | Close as admin on another teacher's assignment | Accepted; `closed_by_user_id` = admin. |
| L84 | Authorization | Any lifecycle route with a pending TOTP session | Treated as anonymous; no change. |
| L85 | Authorization | Another account requests an attempt in grace | 404, no closure notice. |
| L86 | Authorization | Duplicate as another teacher, as a student, anonymous | Refused as today; no copy. |
| L87 | CSRF | Close, edit and duplicate with a foreign `Origin`, and with no `Origin` and a foreign `Referer` | Refused by the integrity policy; no change. |
| L88 | CSRF | `GET` on the close URL | 405; no change. |
| L89 | Policy | Access-policy and integrity-policy tests | The close endpoint is registered with both. |
| L90 | Public token | Token of a closed assignment | 200 with closed notice; malformed token still 404. |
| L91 | Client input | Submit with forged `submission_reason` and timer values after the cutoff | `closed`. |
| L92 | Snapshot reads | Student result, teacher results and CSV | Each issues all its reads inside one read transaction. |
| L93 | Migration | Migration SQL test and schema snapshot | Eight nullable columns; `lifecycle_clock` with one row seeded with the fixed sentinel; no rebuild; `foreign_key_check` empty. |
| L94 | Compatibility | Lifecycle-aware build started on a schema with an unknown applied migration | Production start-up refuses. |
| L95 | Compatibility | New code on the old schema | Production start-up refuses (pending migration). |
| L96 | Compatibility | Pre-lifecycle code paths on the new schema, in a test only | They run and ignore closure. The test documents why §13.2 forbids it; it is not an accepted mode. |
| L97 | Exposure audit | Test database with attempts started after closure by each rule of §7.1, an attempt carrying the same second as a manual close, and submissions after `E + 15 min`, all written directly | The audit report lists every start after closure and every late submission, and lists the same-second attempt as ambiguous; nothing is modified. |
| L98 | Restore, evidence A | Snapshot with deadline 5 January; afterwards extended to 30 January and closed manually on 10 January; restore, with the pre-restore database readable | After reconciliation `deadline_at` is 30 January, `closed_at` 10 January, `E` = 10 January. Not 5 January. Its `restore_pending` reason is resolved; any other reason it carried is still there. |
| L99 | Exclusive cutoff (I4) | Start at 23:59:59 local on the displayed day, and at 00:00:00 the next day | Accepted; refused. |
| L100 | Display zone, no deadline | Manual close of a no-deadline assignment | Closure time and grace cutoff shown in the stored zone, named. |
| L101 | Display zone, historical | Assignment created before migration A | Times shown in the application default zone, named; editing asks for a zone; the server zone is not used. |
| L102 | Terminal submissions | `submissions_closed_at` set. (a) The clock then reads earlier than `E`, beyond the tolerance, with the clock row untouched. (b) Service level: the fixture sets a consistent clock row at a lifecycle time earlier than `E`, state `ok`, no hold | (a) 503: the regression is found before any outcome is evaluated; `suspect` by the bookkeeping update; nothing else written. (b) Submit and upload are `closed`, decided by the marker without comparing times; nothing written. |
| L103 | Grace exhaustion observed | A POST at `E + 15 min` stamps `submissions_closed_at`. Then (a) a regression beyond the tolerance brings the clock to `E + 1 s`; (b) a regression within the tolerance; (c) service level, fixture clock row at `E + 1 s`, state `ok` | (a) 503 and `suspect`. (b) Lifecycle time stays at the high-water mark: `closed`. (c) `closed`, decided by the marker. The submission is accepted in none of them. |
| L104 | Skipped observation | The observation stamp fails (lock unavailable) on a `GET` after the deadline | The response still shows closed; the next observer stamps it. |
| L105 | Stale high-water, new boot | No stamp ever written, high-water older than the deadline, new boot, synchronisation evidence absent | Lifecycle writes refused; after the bounded wait `state = suspect`. The test documents the stated limit: with evidence present and a wrong clock this case is not detectable. |
| L106 | Same-boot step | Wall clock stepped forwards, and backwards, by more than the tolerance relative to the boot anchor | `state = suspect` in both directions; writes refused. |
| L107 | Several processes | Process A detects an anomaly; process B has detected nothing | B's next lifecycle write is refused. After the operator confirms through the command-line tool, both proceed. |
| L108 | Application restart | Restart while `suspect` | Still `suspect`. |
| L109 | Clock singleton missing | `lifecycle_clock` has no row | Lifecycle writes get 503; production start-up refuses; the seed is not re-run automatically. |
| L110 | Clock singleton malformed | Row with an unparseable timestamp, an unknown state, or an inconsistent reference | Same as L109. |
| L111 | Migration seed and mocked clock | Freshly migrated database, application clock patched to a date in the past | No regression is detected, because the seed is the sentinel. The fixture helper sets the clock row consistently with the patched time. |
| L112 | Recovery manufactures no closure | High-water mark later than several deadlines and grace ends that were never stamped; some lie at or before the verified time, some after it; operator recovery lowers the mark | The recovery command writes no `closed_at`, `expiry_observed_at` or `submissions_closed_at`. Those after the verified time are held (`clock_review`) with every column unchanged. Those at or before it are stamped only later, by ordinary observation, with the instant of that observation and never with the old mark. |
| L113 | Recovery and persisted markers | Assignments closed by a persisted `closed_at` or `expiry_observed_at`, some also with `submissions_closed_at`, with marker values before, inside and after the suspect interval; then recovery with a lower high-water mark. Worked case: an attempt starts at 11:59; a manual close is stamped at 12:00; recovery verifies at 12:05; the old high-water mark was 12:06 and the declared suspect interval begins after 12:00, so no hold applies to the assignment; `submissions_closed_at` is NULL; grace ends at 12:15 | No marker is cleared or changed. **A. New starts** are refused for every one of them, at any time; none becomes startable, and there is no reopening. **B. A valid in-flight submission** is accepted inside the exclusive grace window when the attempt started before the closure (§7.1), no hold applies, `submissions_closed_at` is NULL, the clock state is `ok` and the other guards of §11.4 pass. The worked case uses two separate unsubmitted attempt fixtures, both started at 11:59: the first submits at 12:05 and the outcome is `submitted`; the second, still unsubmitted, submits at exactly 12:15 and is `closed`. A retry of the first attempt, at 12:05 or at any later time including after 12:15, is `already_submitted` (§7.5), not `closed`. **C. A submission is always refused** when a hold applies (503: a marker inside the suspect interval or after the verified time, or an `E + G` after the verified time and at or before the old mark), when `submissions_closed_at` is set (`closed`, without comparing times), when grace has expired (`closed`), and from PR #328 when the detail is cleaned (`closed`). |
| L114 | Recovery holds | Assignments with factual timestamps or terminal markers in the declared suspect interval or after the verified time; and an open assignment whose only future time is its scheduled deadline | The first are held with `clock_review` added to their existing reasons; the closed ones get `retention_base_at` = verified time. The last is not held. |
| L115 | Confirmation | Operator-supplied time disagreeing with the local clock | Rejected; state stays `suspect`. |
| L116 | Hold enforcement | Every assignment route for a held assignment, by its owner, its student and a stranger; with one reason, with two, and with a value that does not parse | Owner and student get the temporary-unavailable response in all three cases; the stranger gets the usual 404. Nothing is written. The unparseable value is reported. |
| L117 | Hold is operator-only | Any request carrying `lifecycle_hold` or `retention_base_at` | Ignored. |
| L118 | Duplicate retry | Submission commits, the response is lost, the client resends the POST | `already_submitted`: redirect to the result; no row written; no second note. |
| L119 | Restore, before reconciliation | Restored database, restore-hold step run, service started | Every assignment carries `restore_pending` in addition to any reason it already had; the clock state is `suspect`; no assignment route serves anything. |
| L120 | Restore, incomplete evidence | Affected set identifiable but values incomplete; and affected set not identifiable | First: `restore_pending` is resolved only for unaffected assignments, the affected get `restore_unreconciled`, and an assignment is served only if no reason remains. Second: all stay held. |
| L121 | No evidence-free termination | An operator tries to resolve a held assignment for which no record of its state exists | No command does it. The assignment stays held; no `closed_at`, `submissions_closed_at` or `retention_base_at` is written; its reasons are unchanged. |
| L122 | Reconciliation limits | Reconciliation given input that would clear or change a terminal marker, lower `retention_base_at`, change `deadline_at` on a row with a terminal marker, or remove a reason other than the one being resolved | Refused. |
| L123 | Manual close in the last second before the deadline | `deadline_at = D`; an attempt starts at `D - 1 s`; the owner closes in that same second | One row updated. `closed_at = D - 1 s`, not `D`; `E = closed_at`; `expiry_observed_at` stays NULL. The attempt is in flight and may submit until `D - 1 s + 15 min`; a submission at exactly that instant is `closed`. |
| L124 | Manual close, zero rows | The close update matches no row because the assignment is held; already closed manually; already expired with a marker; expired without one; and, by fault injection, although the row is still `OPEN` | 503; redirect with nothing written; redirect with nothing written; `expiry_observed_at` stamped, then redirect; server error with a rollback. In no case is a closure reported that was not persisted. |
| L125 | Stored closure time | Close in the same second as a start, then another request for the assignment in that same second | `closed_at = started_at = now`. No stored time is later than the clock, so the follow-up request raises no clock fault. |
| L126 | Future scheduled deadline, clock checks | Open assignment with a deadline 179 days ahead: start, submit, upload, edit, close | All proceed. The deadline is never compared with the clock for health; the state stays `ok`. |
| L127 | Future scheduled deadline after a manual close | Manually closed assignment whose stored `deadline_at` is still 100 days ahead: read pages, a grace submission, a review, deployment verification | No anomaly, no hold, no clock fault. `E` is `closed_at`. |
| L128 | Timestamp classes in verification | Deployment verification on a database with future deadlines, one `started_at` in the future, one `closed_at` in the future, and one deadline 400 days ahead | The future deadlines pass. The two factual timestamps are listed and their assignments held. The 400-day deadline is reported as a data anomaly; the clock state is not changed by it. |
| L129 | Forward error below the tolerance | Same boot; the wall clock is 30 s ahead of the boot anchor | State stays `ok` and writes proceed. A deadline takes effect 30 s early and the closure is terminal. The test documents the stated limit of §3.6; nothing in PR #326 deletes on the strength of this clock. |
| L130 | Repeated small clock steps | Same boot; forward steps of 30 s, with a lifecycle write after each | The first and second pass. The third brings the sum to 90 s, above the tolerance: `suspect`. The anchor is unchanged by the writes in between. |
| L131 | Clock-fault bookkeeping scope | Any detected fault, in a request and in a command-line tool | The only row changed in the whole database is `lifecycle_clock`: `state`, reason and reading. `high_water_at` and the anchor are unchanged; no `quiz_*` row, no observation marker and no hold is written. A second detection while `suspect` writes nothing. |
| L132 | Recovery of an unobserved expiry | Deadline after the verified time and at or before the old high-water mark, never stamped | Held with `clock_review`; no column changes. After the operator's review the reason is resolved, the assignment is `OPEN` until its stored deadline, and a start succeeds. |
| L133 | Hold after a future-dated marker | `closed_at` stamped an hour ahead of the verified time during an incident | Held. The reason cannot be resolved until lifecycle time has passed `closed_at`; until then every request for the assignment gets the unavailable response. After release the grace window is the ordinary one from `closed_at`, at most 15 minutes. |
| L134 | Restore with a pre-existing hold | The snapshot holds an assignment with `clock_review`; restore; reconciliation with evidence A | After the restore-hold step the set is `clock_review,restore_pending`. Reconciliation resolves `restore_pending` only. The assignment is still not served; it is released when `clock_review` is resolved too. |
| L135 | Restore, hold only in the pre-restore database | `clock_review` or `restore_unreconciled` was added after the snapshot was taken | Carried onto the restored row together with `restore_pending`. |
| L136 | Restore preserves a clock incident | Pre-restore clock state `suspect`, snapshot state `ok` | After the restore the state is `suspect`, with the earlier reason and reading carried over; lifecycle writes are refused until §3.7 is completed for it. |
| L137 | Restore, marker conflict | The restored row has a terminal marker the pre-restore database lacks; and both have `closed_at` with different values | `restore_unreconciled` in both cases; the restored marker is kept; nothing is cleared. |
| L138 | Restore, phase isolation | The PR #326 restore procedure and its commands, on a build and schema without `database_identity`, snapshot manifests, verification records or the ledger | They complete without them. No step requires functionality introduced in PR #328. |
| L139 | Hold set | Add a reason twice; add a second reason; resolve one of two; look for a tooling path that writes the column as a whole | Idempotent; both present in the fixed order; the other remains and the assignment stays held; no such path exists. |
| L140 | Phase isolation | Inspect every statement and call added by PR #326 | No `DELETE` on a detail table; no reference to `details_cleaned_at`, `closure_upper_bound_at`, `database_identity` or the ledger; no time-source query. |

### 15.2 PR #327

| # | Area | Case | Expected |
|---|---|---|---|
| H1 | History | Submit a plain attempt | One record: immutable context snapshot plus scores, status "automatic". |
| H2 | Mixed scores | Submit a mixed attempt with combined scoring | Record holds multiple-choice score, open subtotal and combined score equal to `quiz_combined_score_summary`. |
| H3 | Review | Teacher reviews an open answer | Outcome refreshed in the same transaction; status "teacher-reviewed"; combined score updated; context snapshot unchanged. |
| H4 | Phase isolation | Inspect every statement added by PR #327 | None references `details_cleaned_at`, `closure_upper_bound_at`, `database_identity` or the ledger; none deletes from a detail table; nothing queries a time source; the suite passes on a schema without them. |
| H5 | Pending review | Attempt with an unmatched, unreviewed open answer | Reported as pending by the definition adopted for §16, item 5. |
| H6 | Abandoned | Unsubmitted attempt on a closed assignment, past the cutoff | Participation record: "not submitted", no score, never zero. |
| H7 | NULL owner | Historical attempt, submitted and unsubmitted | Record with no account, `student_name` snapshot, browser-bound marker. |
| H8 | Practical | Abandoned attempt with practical uploads | Record states practical work was uploaded, with counts; no outcome invented. |
| H9 | Winner only | Double submit (L64) | History written once, by the `submitted` request. |
| H10 | Slot, empty answer, omitted question | Three attempts: an open question submitted with a blank field; an open question whose fields did not arrive; a question no longer renderable at submission | Three different manifest entries: `received_empty` with rows; `not_received` with no rows; `omitted` with its reason. None is reported as another. |
| H11 | Malformed plan, parsed values | Plans with a string id, a boolean, a float, a duplicate, an empty list, an unknown key, an open id not in the list | Each is rejected by the strict parser and classified unverifiable. Nothing is skipped or repaired. |
| H12 | Canonical content deleted | Planned question deleted from canonical content before the backfill | Unverifiable; the backfill does not guess the question's kind. |
| H13 | Evidence, not agreement | Pre-manifest attempts: multiple-choice rows equal to `score_total`; rows fewer than `score_total`; a planned open question with no rows; a planned open question with rows and no accepted witness | Reconstructed; unverifiable; unverifiable; unverifiable. The last three are unverifiable even though backfill and verifier compute identical summaries from the rows. |
| H14 | Context immutability | Rename the assignment, reclassify a question's topic, rename the section | Context snapshot unchanged; a later review still refreshes only the outcome. |
| H15 | Preservation | Delete the detail rows of an attempt in a test database | Record unchanged; student and teacher summary views still render. |
| H16 | Backfill | Run on a database with existing attempts of every kind, twice | One record per attempt; second run changes nothing; unverifiable records listed. |
| H17 | Verification | Tamper with one stored value | Report names that attempt and exits non-zero. |
| H18 | Result access | Every outcome of `can_access_attempt`, with the attempt row present and with it removed or reduced | Identical for account-owned and browser-owned attempts; URL unchanged. |
| H19 | Authorization | Student reads another account's history; teacher reads history of an unowned assignment | 404. |
| H20 | Foreign keys | Schema inspection | No cascade path from detail tables to history; account link nullable and `RESTRICT`. |
| H21 | Refresh after loss of detail | A `quiz_text_answers` row disappears after submission; the teacher then reviews another answer of that attempt | The review commits. The stored outcome is not overwritten; the record is marked inconsistent; the assignment is blocked from cleanup. |
| H22 | Manifest immutability | Review, rename, topic reclassification, change of a question's slots in canonical content | Manifest unchanged. |
| H23 | Manifest written once | Double submit | One manifest, from the `submitted` request. |
| H24 | Coverage counts | Attempt with answered, blank, not-received and omitted items | Counts of expected questions, presented, omitted, expected slots, received and answered each match the manifest entries. |
| H25 | Malformed plan, raw text | Stored plan text with a repeated key, `1.0`, `1e0`, `NaN`, text after the closing bracket, an empty string, a top-level number, a nested list, an id beyond the row-id range | Each is rejected on the raw text and classified unverifiable. The lenient decoder is not used. The stored text is kept byte for byte in the record. |
| H26 | Partial slot loss | Pre-manifest attempt; a two-slot open question scored 1 of 2; one of its two rows is deleted before the backfill | Unverifiable (slot coverage unproven). The record does not state 1 of 1 as a result; the surviving row is left in place; the assignment is blocked from cleanup. |
| H27 | Today's content is not a witness | Pre-manifest attempt whose open-answer rows match today's `fill_in_subquestions` exactly, with no accepted witness; then the same attempt after a slot is removed from canonical content | Unverifiable; still unverifiable, and now also reported as disagreeing with current content. |
| H28 | No laundering | Backfill, then verifier, then both again, on the attempt of H26; then a restore | The classification is unverifiable every time. Neither tool treats the other's output as evidence, and the restore does not upgrade it. |
| H29 | Unverifiable keeps its history | The attempts of H25 and H26 | Each has a record with the context snapshot, the raw plan, the rows and scores as found and marked not verified, and its reason. Student and teacher views render it as unverified. No detail row is changed. |
| H30 | Witness rules | With a witness accepted by the PR #327 design: rows equal to the witness's slots; rows a strict subset; rows a superset; no witness for the submission date | Reconstructed; unverifiable; unverifiable; unverifiable. Without an accepted witness the first is unverifiable too. |

### 15.3 PR #328

| # | Area | Case | Expected |
|---|---|---|---|
| C1 | Retention | `F.lo` = recorded closure bound + 29 days | Not eligible; zero changes. |
| C2 | Retention | `F.lo` = recorded closure bound + 30 days - 1 s | Not eligible. |
| C3 | Retention | `F.lo` = recorded closure bound + 30 days exactly, server-clock rule also satisfied | Eligible; cleaned. |
| C4 | Retention | `F.lo` = recorded closure bound + 31 days | Eligible; cleaned. |
| C5 | Retention | Student submitted on day 1, assignment closed on day 14, closure confirmed on day 15, cleanup on day 35 | Not eligible: neither rule runs from the submission. |
| C6 | Retention | Retention setting below 30 days | Tool refuses to run. |
| C7 | Open assignments | No-deadline assignment, never closed, any age | Never eligible; listed in the summary. |
| C8 | Grace | Retention forced equal to `G` in the test | Not eligible until both rules reach `max(R, G)`, and never before `submissions_closed_at` is persisted. |
| C9 | Pending review | Assignment with one pending attempt | Whole assignment blocked and reported; nothing deleted. |
| C10 | Practical | Assignment with practical files, before practical grading is persisted | Blocked and reported. |
| C11 | Practical | Abandoned attempt with practical uploads, after the prerequisite and with a complete record | Rows deleted in the transaction; files ledgered, then unlinked. |
| C12 | Abandoned | Assignment with an abandoned attempt that has no durable record | Blocked: history missing. |
| C13 | NULL owner | Assignment with a historical attempt that has no durable record | Blocked: history missing. |
| C14 | Missing invariant | Assignment whose detail rows no longer match one attempt's manifest | Blocked: history inconsistent; nothing deleted. |
| C15 | Malformed timestamps | Assignment or attempt with a malformed lifecycle timestamp | Blocked and reported. |
| C16 | Gate | History schema absent | Exit non-zero before any write connection is opened. |
| C17 | Gate, snapshot | No verified snapshot; one without a manifest; one whose verification record is a fail | `--execute` aborts with no cleanup write. |
| C18 | Gate, clock | Clock behind the high-water mark, or `state = suspect`, or clock row missing | Tool refuses to run. In the first case the only write is the bookkeeping update. |
| C19 | Gate | History mismatch for one attempt | That assignment rolled back and reported; others proceed. |
| C20 | Dry run, logical | Default invocation with eligible assignments | Logical dump of every table identical before and after. |
| C21 | Dry run, files | Same run | Upload and vault trees identical in names, sizes, hashes and modification times; no ledger action. |
| C22 | Dry run, snapshot source | Dry run given only a snapshot copy | Succeeds; live database files and sidecars have unchanged metadata; no sidecar created beside the snapshot. |
| C23 | Dry run, live source | Dry run on a live WAL database | Guarantees of C20 and C21 hold; the test does **not** assert unchanged `-wal`/`-shm` or main-file bytes, and the output states the weaker guarantee. |
| C24 | Dry run | Any write attempted on the dry-run connection | Refused by `query_only`. |
| C25 | Dry run | `--execute` together with a snapshot source | Refused. |
| C26 | Interruption | Kill the process during the second assignment's transaction | First assignment cleaned and stamped; second fully intact. |
| C27 | Crash after commit | Kill after the database commit and before any unlink | `details_cleaned_at` set; files present; ledger lists them. |
| C28 | Retry | Run again after C27 | Ledger retried first; files removed; ledger empty; exit 0. |
| C29 | Failed unlink | One file cannot be removed | Ledger row kept with error and attempt count; exit 1; reported on every later run. |
| C30 | Retry | Run twice with nothing new eligible | Second run performs zero writes. |
| C31 | Backup exclusion | Start cleanup while the backup holds the maintenance lock | Cleanup exits 3 having done nothing. |
| C32 | Backup exclusion | Start a backup while cleanup holds the lock, including during the unlink phase | Backup exits without publishing a snapshot. |
| C33 | Backup exclusion | Holder killed | Lock released; the next run proceeds. |
| C34 | Snapshot consistency | Snapshot whose database lists a file missing from the archive | Consistency check fails; cleanup gate refuses it. |
| C35 | Snapshot consistency | Snapshot with an archived file that has no row | Reported; check passes. |
| C36 | Restore | Restore a copy that contains ledger rows | The next execute run retries them first. |
| C37 | Restore | Restore a pre-cleanup copy, then the scheduled cleanup fires before reconciliation | Every assignment is held; nothing is deleted. |
| C38 | Foreign keys | `PRAGMA foreign_key_check` after each transaction and at the end | No rows. |
| C39 | Question bank | Row counts and content hash of every `CANONICAL` table before and after | Identical. |
| C40 | Question bank | Statement inspection of the tool | No `DELETE` or `UPDATE` on a `CANONICAL` table. |
| C41 | History | History rows before and after cleanup | Context snapshots identical; outcomes identical and marked frozen. |
| C42 | Vault | Vault tree before and after | Identical unless note removal was explicitly enabled (§16, item 8). |
| C43 | Batching | More eligible assignments than the batch limit | Exactly the limit cleaned; the rest on the next run. |
| C44 | Concurrency | Review and cleanup transactions, both orders | §11.6 table. |
| C45 | Concurrency | Student result read transaction spanning a cleanup commit | Entirely pre-cleanup or entirely post-cleanup; never questions shown as unanswered. |
| C46 | Concurrency | Teacher results and CSV read transactions spanning a cleanup commit | Same; never attempts with silently empty answers. |
| C47 | After cleanup | Owner account opens `/quiz/attempt/<id>/result` and `/account/attempts` | Summary shown; same URL. |
| C48 | After cleanup | Another account, and a browser without the marker, open the same URL | 404. |
| C49 | After cleanup | Browser holding the marker of a historical attempt | Summary shown. |
| C50 | After cleanup | Teacher review POST, results page, CSV | Review refused with "details removed"; pages and CSV show the summary and the removal date. |
| C51 | After cleanup | Public token of a cleaned assignment | Closed notice. |
| C52 | Marker meaning | `details_cleaned_at` set while ledger rows remain | Reported as "database cleaned, files pending", not as fully cleaned. |
| C53 | Logging | Summary output | Contains ids and counts; no names, answers, notes or tokens. |
| C54 | Forward jump across the threshold | Assignment really closed 10 days ago; clock jumps forward 25 days; cleanup runs | The network sources agree with each other and not with the server: `state = suspect`; nothing deleted. The trusted-time rule is false as well. |
| C55 | New backup after a jump | Same jump; a backup then runs and produces a fresh verified snapshot; cleanup runs | Still refused. Snapshot recency is not accepted as clock evidence. |
| C56 | Independent evidence unavailable | External sources unreachable, no attestation; then with a valid operator attestation | Refused with status 4, state unchanged, no bound recorded, nothing deleted; then the run proceeds under the trusted-time rule. |
| C57 | Attestation | Operator-attested time disagreeing with the server's clock | Refused; state unchanged. |
| C58 | No silent resumption | After `suspect`, the clock is corrected and the timer fires | Refused until the operator confirmation of §3.7. |
| C59 | Retention floor | `retention_base_at` 20 days after `E`; closure bound recorded after the floor; `F.lo` at `E` + 30 days and at the bound + 30 days | Not eligible; eligible. |
| C60 | Hold | Otherwise eligible assignment with `lifecycle_hold` set: one reason of each kind, two reasons, and a value that does not parse; in a scheduled `--execute` and in `--confirm-closures` | Never cleaned and no closure bound recorded, in every case, `restore_reclean` included; reported with its reasons. The one operator path is C67. |
| C61 | Cleaned is terminal | Cleaned assignment with a retained abandoned stub (`submitted_at` NULL). (a) The clock then reads earlier than `E`, beyond the tolerance. (b) Service level, fixture clock row earlier than `E`, state `ok` | (a) 503 and `suspect`; no row and no file written. (b) Submit and upload are `closed`, decided by `submissions_closed_at`, which eligibility required before cleanup; no row and no file written. |
| C62 | Unverifiable history | Assignment with one unverifiable or inconsistent record, including one that is unverifiable only because slot coverage is unproven | Blocked with the reason; nothing deleted; the record and the detail rows are unchanged. |
| C63 | Upload during a snapshot | Uploads committing before the database copy, between copy and archive, and after the archive pass | Row and file; file without row (reported, check passes); neither. Never a row without its file. |
| C64 | Crash after unlink | Kill after a file is unlinked and before its ledger row is deleted | Next run finds the file missing, counts it removed, deletes the ledger row. |
| C65 | Unsafe unlink paths | Ledger paths that resolve outside the upload root, end in a symbolic link, pass through a symbolic-link directory, name a directory, or are still referenced by a live row | None is unlinked; each is reported; ledger rows kept. |
| C66 | Two cleanup processes | Second cleanup started while the first holds the maintenance lock, during deletion and during unlinking | Second exits with status 3 having written nothing. |
| C67 | Restore of a cleaned assignment, end to end | An assignment cleaned before the restore, restored from a snapshot taken after its last detail change, with the pre-restore database as evidence A. Followed through: (a) the restore-hold step; (b) reconciliation; (c) a scheduled `--execute` and a `--confirm-closures` run; (d) operator confirmation; (e) operator re-clean before, and then after, the full retention interval; (f) verification and resolution. At every step before (f): a student start, submission and upload, and a teacher edit, close and review. Variants at (d): a second reason present (`clock_review`, `restore_pending` or `restore_unreconciled`); an unparseable hold; no frozen record (no evidence A); restored detail older than the record; `submissions_closed_at` NULL | (a) `restore_pending,restore_reclean`; closure bound NULL. (b) `restore_reclean` only; frozen records imported with the classification they had. (c) Both skip it: nothing deleted, no bound recorded. (d) `closure_upper_bound_at` recorded from a fix obtained after the read; the hold and every other column unchanged. (e) Before the interval: refused as not old enough, nothing written. After it: detail deleted in one transaction, `details_cleaned_at` set, hold still `restore_reclean`. (f) Post-conditions verified, then `restore_reclean` resolved; `lifecycle_hold` is NULL and the assignment is served as cleaned. Every student and teacher request before (f) gets the unavailable response; none is accepted as a start, a submission, an upload, an edit or a reopening. No terminal marker, floor or history record changes, apart from `details_cleaned_at` being set. Each variant is refused at (d): nothing written, still held, no bound. |
| C68 | Forward error below every tolerance | True time since closure is 30 days - 20 s; the server's clock is 30 s ahead; the step passed §3.6 and requirement (e) | Not eligible: `F.lo` is below the recorded bound + 30 days. Nothing deleted, although the server-clock rule is satisfied. |
| C69 | Repeated small clock steps | The server's clock has gained two days through steps that were each below the tolerance, over several reboots; and the same with a total of 50 s | First: the sources disagree with the server, `suspect`, nothing deleted. Second: requirement (e) passes and the trusted-time rule decides; nothing is deleted early. |
| C70 | Time changes after verification | A fix is obtained; then the wall clock is stepped forward 40 days before the next transaction; and, separately, by 30 s | First: requirement (e) fails on the re-check; the run stops with status 4 and the bookkeeping update; nothing further is deleted. Second: eligibility is unchanged, because it is computed from `F.lo`. |
| C71 | Closure not yet confirmed | Closed 60 days ago by the server's clock; `closure_upper_bound_at` NULL | The run records the bound and deletes nothing of this assignment. A later run whose `F.lo` is at least 30 days above that bound cleans it. |
| C72 | Bound recorded only after the fact | An assignment is closed between the tool's read and its probe | No bound is recorded for it in this run. A bound is only ever the upper side of a fix obtained after the closure marker was read as committed. |
| C73 | Bound invalidated | A hold reason is added after the bound was recorded; and `retention_base_at` is raised above it | The bound is cleared in both cases and must be recorded again after release; cleanup then waits a further 30 days. |
| C74 | Insufficient time evidence | Fewer sources than required; sources that disagree with each other; the operating system reports the clock as unsynchronised | No fix in any of them: no bound recorded, nothing deleted, status 4, state unchanged. |
| C75 | Fix arithmetic | Sources with different stated errors and round trips; a fix that has aged | `F.lo` is the lowest lower bound, rounded down, and is not advanced with age. The recorded bound is the highest upper bound, rounded up. A wide interval only delays cleanup. |
| C76 | Recorded bound is kept | A later run has a narrower fix | `closure_upper_bound_at` is not lowered and is not recomputed from stored timestamps. |
| C77 | Both rules required | The trusted-time rule holds and the server-clock rule does not; and the reverse | Not eligible in either case. |
| C78 | Future scheduled deadline at cleanup | Manually closed assignment, otherwise eligible, whose stored `deadline_at` is still 100 days ahead | Cleaned. It is not blocked as a timestamp in the future. |
| C79 | Event timestamp in the future | Otherwise eligible assignment with an attempt whose `submitted_at` is later than `now` | Blocked and reported; nothing deleted. |
| C80 | Wrong-environment backup | The backup directory holds a verified, recent snapshot of another database; and one of this database taken from another upload root | Refused at the provenance gate before any cleanup write. |
| C81 | Cloned-environment backup | A verified snapshot with the same `instance_id` and different content | The identity check passes; the comparison by content fails; every affected assignment is blocked; nothing deleted. |
| C82 | Substituted database | The snapshot's `questions.db` is replaced after verification | Digest mismatch; refused before any cleanup write. |
| C83 | Substituted archive | The snapshot's `files.tar.gz` is replaced after verification; and a verification record is copied in from another snapshot | Digest mismatch in both cases; refused before any cleanup write. |
| C84 | Coverage by content, rows | A teacher review commits after the snapshot; the snapshot's stamp and the row timestamps are manipulated so that the snapshot looks newer | That assignment is blocked (`backup does not cover current detail`); others proceed. No timestamp comparison lets it through. |
| C85 | Coverage by content, files | A practical file whose bytes differ from the recorded digest; and one absent from the record's file list | The assignment is blocked; no ledger row, no deletion. |
| C86 | Bookkeeping from the tool | The sources agree with each other and disagree with the server's clock | The only database change is the `lifecycle_clock` row. No connection that can perform cleanup writes is opened. |
| C87 | Confirmation run | `--confirm-closures` with eligible and ineligible assignments | Writes only `closure_upper_bound_at` and ordinary observation markers; deletes nothing; does not need the snapshot gate. |
| C88 | Restore clears bounds; re-confirmation, interruption and retry | Restore of a database whose assignments had recorded bounds, one of them a previously cleaned assignment on the path of C67. For that one, each of these in turn, followed by a rerun: the process is killed before the confirmation update commits; a `clock_review` reason is added after confirmation; the process is killed during the re-clean transaction; after its commit and before the files are unlinked; after its commit and before resolution; during the resolution transaction | Every bound is NULL after the restore. Ordinary assignments: nothing is cleaned until the holds are resolved, each closure has been confirmed again, and that confirmation is at least 30 days old. The re-clean assignment is still held and not served after every interruption, and: no bound, so the rerun confirms; reason added, so the bound is cleared and the path refuses it until `clock_review` is resolved, then it is confirmed again and waits a further 30 days; killed in the re-clean, so it is rolled back with detail intact and the rerun cleans; killed after the commit, so the rerun retries the ledger, skips the deletion, verifies and resolves; killed in the resolution, so the hold is intact and the rerun resolves. The completing run ends with `lifecycle_hold` NULL, `details_cleaned_at` set and the assignment served as cleaned. No step takes effect twice, and nothing is deleted earlier than 30 days after the bound that was in force. |
| C89 | Restore, snapshot provenance | From PR #328: a restore attempted from a snapshot without a passing verification record, or with one that names another database identity | The restore procedure refuses it. |

Total: 140 + 30 + 89 = 259 cases. The revision before the third review
listed 213 (122 + 24 + 67). Eight cases that review named were wrong and are
corrected in place (L37, L62, L102, L103, L112, L113, L121, C61); 30 further
cases were reworded to match the corrected design; 46 cases are new (L123 to
L140, H25 to H30, C68 to C89). The follow-up review changed no count: L113
was corrected again, C67 and C88 were extended to the complete re-clean path
with interruption and retry, and L76 and C60 were reworded to agree with
them. No case was kept with an expectation the design no longer supports.

---

## 16. Open decisions and risks

### 16.1 Resolved decisions

Approved by the product owner. They are no longer open. The item numbers are
kept so earlier references stay meaningful.

| # | Question | **APPROVED** | Specified in |
|---|---|---|---|
| 1 | What is the grace duration? | **15 minutes**, from effective closure; a submission at exactly `E + 15 minutes` is rejected; no effect on retention. | §1.2 P1, §7 |
| 3 | What is the default deadline's time of day? | **23:59 local time, 14 calendar days ahead**, in the teacher's selected IANA timezone, stored in UTC. | §1.2 P2, §5.1–§5.3 |
| 4 | Is there a maximum deadline distance? | **180 days**, validated server-side; "no deadline" stays available. | §1.2 P3, §5.4 |
| 12 | Can the title be edited after closure? | **No.** Frozen after manual closure and after expiry. | §1.2 P4, §6.6 |
| 16 | What deadline does a duplicated assignment get? | **The teacher chooses**, on the same deadline or no-deadline form as creation, prefilled with the default. Nothing is copied from the source and nothing is imposed. | §1.2 P5, §5.5 |

### 16.2 Implementation details proposed for PR #326, pending confirmation

These follow from the approvals but were **not** themselves approved. PR #326
cannot be merged until each is confirmed or replaced. The document specifies
the recommended option; the alternative is stated so the choice is real.

| # | Proposed | Alternative | Affects |
|---|---|---|---|
| I1 | Timezone selected and stored **per assignment**. | A per-account preference: one setting for all of a teacher's assignments, but a new `users` column and a settings page. | §5.3, migration A |
| I2 | `Europe/Sofia` as the **editable preselection** of the selector. | No preselection (the teacher must pick every time), or a deployment setting. | §5.3 |
| I3 | The 180-day maximum counted **from the date the deadline is set or edited**. | From the assignment's creation date, which caps the total life of an assignment with a deadline at 180 days. | §5.4 |
| I4 | **Exclusive cutoff at the next local midnight**, displayed as "through 23:59". | Store 23:59:00 as the cutoff; the last displayed minute is then not usable. | §5.1, §5.4 |
| I5 | An append-only **lifecycle event log** outside the database, as restore evidence. | No log: after a restore without a readable pre-restore database, every assignment stays held until an operator resolves it from a record, one by one, and those without a record stay held. Safe either way; the log only reduces that cost. | §13.4 |

None of I1 to I5 is approved by this revision, and no part of the corrected
design depends on any of them being approved in the proposed form.

### 16.3 Open decisions

None of these is approved by this document. "Recommended" is a proposal.
"Settle before" names the PR that cannot be merged without the answer. None of
these product decisions blocks PR #326; the pending implementation details of
§16.2 do.

| # | Question | Recommended default | Alternatives and trade-offs | Blocks | Settle before |
|---|---|---|---|---|---|
| 2 | Should `time_limit_minutes` be enforced on the server? | Separate PR; the lifecycle work does not change it. | Enforce together with the lifecycle: one consistent cutoff, but changes existing behaviour for every timed test. Never enforce: the timer stays advisory and grace is the only hard bound. | Nothing in #326–#328 | Not blocking; decide independently |
| 5 | What exactly is "pending manual grading"? **Unresolved.** | An unmatched open answer with no teacher decision (`is_correct = 0` and `teacher_override = 0`) on a submitted attempt, or any practical submission with files on any attempt (§8.2). | (a) Every open answer without a teacher decision, including automatic matches: safest, but every mixed quiz waits for a full manual pass. (b) An explicit "grading finished" action per assignment: unambiguous and auditable, but adds a column and a teacher step. (c) Never pending: simplest, but cleanup may freeze an unreviewed automatic zero as final. | History status field; cleanup eligibility | PR #327 |
| 6 | Is indefinite retention acceptable for assignments with no deadline that are never closed? **Unresolved.** | Yes for now; list them in every cleanup summary; no automatic closure. | Automatic closure after inactivity (for example 180 days without a new attempt): bounds retention, but is a deadline the teacher did not choose and conflicts with "teachers may choose no deadline". Mandatory deadline: contradicts decision A. Reminder to the teacher: no enforcement. | Cleanup scope | PR #328 |
| 7 | Is there a maximum time pending grading may hold cleanup? | None; report only. | A hard limit (for example 180 days) after which automatic results become final: bounds retention, but finalises grades without the teacher. An escalation to the admin: needs a notification channel that does not exist. | Cleanup eligibility | PR #328 |
| 8 | Are vault attempt notes subject to the same cleanup policy? **Unresolved.** | Cleanup does not delete vault files until this is explicitly approved. | (a) Delete each attempt's note with its detail: consistent retention, but is automated vault deletion, which `AGENTS.md` forbids without explicit request. (b) Stop writing answers into notes from now on: shrinks the problem going forward, leaves existing notes. (c) Keep notes as they are: the copy of student answers outlives the database detail and the retention statement must say so. | Cleanup scope; retention wording | PR #328 |
| 9 | How are historical attempts (`user_id` NULL) represented durably? | A record without an account, with the name snapshot and a browser-bound marker. | Never clean them: no schema question, but their detail stays indefinitely. **Excluded, not an option:** deleting them without a durable record (§9.3). | History scope; cleanup scope | PR #327 |
| 10 | Is a persisted cleanup audit table needed? | No; journald output plus `details_cleaned_at`. The pending-unlink ledger (§10.6) is operational state, not an audit log, and is proposed regardless. | An audit table: durable, queryable evidence of what was deleted and when, survives log rotation; costs a table and a migration. | Cleanup tool | PR #328 |
| 11 | What is the history schema, including how result access survives (§9.6) and whether `reconstructed` records qualify for cleanup or only `recorded` ones (§9.5)? | Its own design PR. | New table versus keeping `quiz_attempts` as an ownership-bearing stub; per-difficulty versus per-topic performance; whether a question-level trace is history or detail (§9.8). | Everything in #327 | PR #327 |
| 13 | What is the user-facing retention wording? | Product and legal. | Must cover the 30-day minimum, the fact that the 30 days are counted from a confirmed closure and can therefore be noticeably longer (item 18), the up-to-monthly schedule, backup lifetime (item 15), exported CSV files and vault notes. | First execute run | PR #328 |
| 14 | How is completion of practical grading represented? **Unresolved.** | A separate prerequisite PR that persists a review outcome (§14.4). Until then practical uploads always block cleanup. | (a) Use the existing `practical_submissions.status` values `'submitted'` and `'reviewed'`: no new table, but no score. (b) Create the `practical_submission_grades` table the code already probes for: holds score and note, needs a migration and review UI. (c) Treat practical work as never graded in-app: history records only that work was uploaded, and such assignments are never cleaned. | Practical results in history; cleaning assignments with uploads | PR #327 if history includes practical results from the start; otherwise PR #328 |
| 15 | How does backup expiry relate to cleanup? **Unresolved.** | Treat them as two separate mechanisms and state both to users. | Shorten backup retention: detail disappears sooner, recovery window shrinks. Automate pruning: predictable lifetime, but automated deletion of backups is its own risk. Scrub snapshots: not practical for SQLite files and tar archives and defeats their purpose. Exclude detail from backups: makes the live database unrecoverable. | Retention wording | PR #328 |
| 17 | May an operator end a held assignment for which no evidence of its state exists? **Not approved.** | No. It stays held; the teacher makes a new assignment. No command for it is part of PR #326, #327 or #328. | (a) "End as of now": the operator closes it and counts retention from that moment. It restores access to results, but it is a termination without evidence, and an earlier draft that made it the default was rejected in review. (b) Allow duplicating a held assignment, so the teacher can continue without the operator; it copies no lifecycle state. (c) Keep it held indefinitely, as proposed: safe, at the cost of results that stay unavailable. | Nothing: the default is complete and safe | Not blocking; only if the product owner wants (a) or (b) |
| 18 | Which time sources does cleanup use, how many, and how often are closures confirmed? **Unresolved.** | At least three independently operated sources, or an operator attestation per run; daily confirmation runs and monthly deletion. | Fewer sources: easier to reach, weaker against one wrong source. Attestation only: no outbound access needed, but every run needs a person. Confirmation only in the monthly run: one timer, but detail then lives up to about three months instead of about two. | Cleanup schedule; retention wording | PR #328 |
| 19 | Is any slot-coverage witness accepted for open answers submitted before PR #327? **Unresolved.** | None. Every such attempt with a planned open question is unverifiable and its assignment is not cleaned by the ordinary tool. | The canonical content revision deployed at submission, if release records identify it reliably: makes some old mixed attempts verifiable, at the cost of building and trusting that record. A one-off operator and product decision for old assignments, outside ordinary cleanup. | History classification; how many old assignments can ever be cleaned | PR #327 |

Explanation for item 15. Cleanup removes detail from the **live** database and
the upload root. It does not touch snapshots. A snapshot taken before a cleanup
run still contains the removed rows and files, and stays restorable for as
long as that snapshot exists.

- **Nothing prunes snapshots automatically.** `docs/DEPLOYMENT.md` §16 states
  that no automatic deletion is installed. The documented policy, 14 days of
  local snapshots plus 7 daily and 4 weekly copies off-server, is something an
  operator carries out by hand.
- So detail is unavailable to the application from the cleanup run onward, and
  remains recoverable by an operator until the last snapshot containing it is
  deleted. If the policy is followed that is roughly four weeks. If pruning is
  skipped it is indefinite.
- The two clocks are independent. Cleanup is driven by the confirmed closure
  plus `R`; a snapshot's life by when someone deletes it.
- Restoring such a snapshot brings the detail back into the live database,
  held, until an operator re-clean after reconciliation (§13.4).
- The provenance gate of §10.8 needs a snapshot that still holds the detail at
  the moment of deletion. It does not require that the snapshot be kept
  afterwards.

### 16.4 Decisions grouped by blocking phase

| Must be settled before | Items |
|---|---|
| PR #326 | No product decision: items 1, 3, 4, 12 and 16 are approved (§16.1). **Implementation details I1 to I5 are pending confirmation (§16.2)** and must be confirmed before merge. |
| PR #327 | 5, 9, 11, 19, and 14 if practical results are part of history from the start |
| PR #328 | 6, 7, 8, 10, 13, 15, 18, and 14 if not settled earlier |
| Independent | 2, and 17 (not approved; the design is complete without it) |

### 16.5 Risks

| Risk | Where | Mitigation |
|---|---|---|
| Pre-lifecycle code serves traffic after activation and ignores closure. | §13.2, §13.3 | Prohibited. Maintenance, forward fix, enforcing rollback builds, start-up guard; if it happens it is an incident with a full audit of starts, submissions and edits. |
| A restore removes closures or deadline extensions. | §13.4 | Every assignment is held after a restore and released only when reconciled from sufficient evidence and free of every other hold reason. Without evidence it stays held. Cost: availability; a held assignment's results are unreachable until an operator resolves it. |
| A restore is carried out without the restore-hold step. | §13.4 | **Not detected by the application**, which cannot tell that its database was replaced. Mitigated only by procedure: the step is part of the documented restore commands, before the service is started. The event log of I5, if approved, would let start-up notice. |
| An assignment stays held indefinitely because no evidence of its state exists. | §3.8, §13.4 | Accepted. The teacher makes a new assignment. Ending it without evidence is not approved (item 17). |
| A restored, previously cleaned assignment cannot be re-cleaned: it has no authoritative history (no evidence A), or its restored detail is older than that history. | §13.4 | It stays held with `restore_reclean` and its detail is kept. A blocked state, not a loop; deleting such detail is a decision outside this tooling. When the conditions are met, re-clean still needs an operator twice and a further full retention interval, during which the assignment's results are unreachable. |
| An expiry or grace-window end that no writer ever observed is recomputed as open after an undetected clock regression. | §3.6 | Conditional guarantee, stated as such. Mitigated by observation stamping from every observer, the boot-anchor check and synchronisation evidence after a reboot. Recovery no longer stamps such assignments; it holds them. Not eliminable without a trusted time source. |
| The server's clock is wrong by less than the tolerance, or was wrong when a boot was anchored. | §3.6 | **Not detected.** Deadlines and grace windows take effect early or late by that error, and closures stamped that way are terminal; the remedy is a new assignment. No deletion can follow from it. |
| Clock jumps forward: assignments close early, irreversibly, and retention could be cut short. | §3.6, §10.1 | Closures are terminal by design; the remedy is a new assignment. Retention is not cut short: deletion needs a lower bound of the present and a recorded upper bound of the closure, both from outside the host, and the server's clock supplies no evidence in that rule. |
| Every time source the cleanup tool uses, or the attesting operator together with the server's clock, is wrong in the same direction by more than the stated error. | §10.1 | **Not eliminated.** The rule is as good as its sources. Mitigated by requiring several independent sources that agree, by refusing on any disagreement, and by the server-clock rule as a second necessary condition. |
| A clock anomaly stops all lifecycle writes until an operator intervenes. | §3.6, §3.7 | Deliberate fail-closed behaviour. Students in flight cannot submit during the incident; their browser draft remains. |
| Cleanup never runs unattended if the host cannot reach external time sources. | §10.1 | Intended: an operator attests the time for the run. Retention is lengthened, never shortened. |
| Retention is longer than 30 days by the delay until a closure is confirmed. | §10.1 | Accepted as the price of not trusting the server's clock. Bounded by how often confirmation runs happen (item 18); stated in the retention wording (item 13). |
| A closure stamped under a wrong clock that was not yet detected. | §3.7 | Held on recovery; `retention_base_at` raised to verified time; from PR #328 the closure bound is cleared and recorded again after release. An assignment whose marker lies in the future stays held until lifecycle time has passed it. |
| A row written by non-enforcing code in the very second of a manual close passes the in-flight check. | §7.1, §13.3 | Reachable only during a prohibited exposure. Listed as ambiguous by the audit. An exact record would need another column and is not proposed. |
| A teacher selects the wrong timezone and the deadline falls hours from what they meant. | §5.3 | The zone is always visible on the form and beside the displayed deadline; it can be corrected while the assignment is open. After closure the remedy is a new assignment. |
| Zone data changes (a government alters DST rules) after a deadline was stored. | §5.2 | The stored UTC instant is authoritative and does not move; only its local display could differ from what the teacher saw. |
| History is wrong or incomplete and cleanup deletes the only correct source. | §9.5, §10.4 | Submission-time manifest; completeness judged only from evidence independent of the rows; invariant checked against detail for every attempt inside the cleanup transaction; unverifiable and inconsistent records block; supervised first run; restore test. |
| **Every** attempt submitted before PR #327 that has a planned open question is unverifiable, so assignments with mixed quizzes from before PR #327 are not cleaned by the ordinary tool. | §9.5 | Accepted: no stored data shows how many slots an open question had at submission. Reported by the backfill before cleanup exists, so the scale is known. Their detail is retained, which is the safe direction. Changing it needs an accepted witness (item 19). |
| The multiple-choice part of an old attempt is reconstructed from `score_total` and `score_correct`, which sit in the same database as the rows. | §9.5 | They are on a different row and were written at submission, so the loss of an answer row shows as a mismatch. A fault that altered both consistently would not show. From PR #327 the manifest replaces this for new attempts. |
| Assignments blocked from cleanup accumulate (incomplete history, pending grading, practical uploads, no deadline). | §10.2 | Reported on every run. Their detail is retained, which is the safe direction; bounding it needs items 6, 7 and 14. |
| A snapshot is inconsistent with its file archive. | §10.8, §10.11 | Shared lock against cleanup; consistency check. Residual: concurrent student uploads are not excluded and are only reported. |
| The snapshot offered to cleanup belongs to another environment, or was altered after verification. | §10.8 | Database identity, upload root and both digests are checked at the gate, and the detail to be deleted is compared with the snapshot by content. Residual: the gate looks at the snapshot on the server and does not check that an off-server copy exists; two environments cloned from each other share an identity until one is re-keyed, and then only the comparison by content separates them. |
| Files remain after their rows are deleted. | §10.6 | Ledger, retry at the start of every run, non-zero exit while any remain. |
| A dry run changes SQLite files of the live database. | §10.9 | Dry runs read a snapshot copy by default; the live mode states its weaker guarantee. |
| Detail lives on in vault notes, exported CSV files and backups after cleanup. | §10.7, §11.7, item 15 | Stated in the retention wording; vault policy decided under item 8; backup pruning is manual. |
| A missed monthly run, or a run refused for lack of time evidence or of a covering snapshot, lengthens retention. | §10.1, §10.2 | `Persistent=true`; the operator checks `systemctl --failed`; no notification channel exists. |
| Deleting a canonical question cascades into `quiz_text_answers` and makes old attempts unverifiable. | §2.2, §9.5 | Existing hazard. History snapshots context so it cannot reach history; affected records are marked incomplete and block cleanup. |
| More than one application worker or host. | §2.7, §11.8 | All checks are transactional and do not rely on the single worker; clocks must be synchronised if hosts are added. |

`docs/ACCOUNTS_DESIGN.md` defers student-data retention and erasure as a
product and legal decision. Decision C sets a minimum retention for detailed
attempt data. It does not settle account retention or erasure requests, which
stay deferred.
