PRAGMA foreign_keys = ON;

-- DZI Part 1 simulation runtime storage (C3.1; docs/DZI_PART1_SIMULATION_DESIGN.md, sections 5, 7, 9-11).
-- Storage only: no route, service, pool, timer, clock integration, grader or activation reads or
-- writes these tables yet. Runtime state, never canonical: a fresh build leaves both tables empty
-- and the canonical export leaves them out (src/canonical_tables.py).
--
-- Additive: two new tables and two indexes (plus the two automatic indexes of the answer table's
-- unique keys). No existing table is rebuilt or altered, no row is seeded and nothing is
-- backfilled: the shapes and rows of every existing table stay exactly as they were. That is the
-- whole of the compatibility claim. It does not mean a build that predates this migration can run
-- against a database that has it applied: production start-up from a build that does not ship an
-- applied migration refuses that database (web/production.py), so rolling the code back is not an
-- off-switch (design section 5.5).
--
-- dzi_part1_attempts: one timed Part 1 attempt of one account. user_id comes only from the
-- validated principal. Every foreign key is RESTRICT, never CASCADE or SET NULL: while an
-- attempt references an account or a blueprint (provenance only: nothing reads the blueprint's
-- slots after the start), SQLite refuses to delete that parent. That is all RESTRICT does: it
-- states no lifetime or retention policy for any row. Ids are positive like every other id.
--
-- The start stores everything that later decides the attempt's fate, so a changed constant or a
-- re-seeded blueprint cannot change an attempt in flight: duration_seconds, the three lifecycle
-- timestamps, max_points_planned and slot_format_json. started_at, deadline_at and
-- submit_cutoff_at are written once, by the start, and every later state decision reads the
-- stored cutoff (the grace constant is read only to compute it for a new attempt).
--   * question_ids_json is a flat array of distinct positive question ids in slot order and
--     nothing else: the updater's reference gate and the parity check both read it (registered in
--     src/update_canonical_content.py and src/canonical_parity.py), and they read a nested
--     document differently.
--   * slot_format_json is a separate array of {"kind", "points"} objects in slot order. It holds
--     no question id and is registered in NEITHER reference validator; its points are plain
--     integers that would otherwise be read as question ids.
-- Both JSON columns and the seed are required text, never a blob.
--
-- Timestamps use the lifecycle layout 'YYYY-MM-DD HH:MM:SS' (UTC, web/services/lifecycle.py).
-- The orderings below compare text, which equals time order only for that fixed-width shape, so
-- every timestamp column carries the four-part shape CHECK of migrations 020, 021 and 024:
-- length() and GLOB both stop at a NUL byte, so the byte length is checked too.
--
-- The CHECK constraints are row-local invariants only: started_at < deadline_at < submit_cutoff_at;
-- at most one of submitted_at, expired_at and abandoned_at is set; a submitted attempt was
-- submitted at or after its start and before the stored cutoff; an expired attempt was marked at
-- or after the stored cutoff; a score exists if and only if the attempt is submitted, and is a
-- whole number with 0 <= score_points <= score_max and score_max > 0. abandoned_at has NO ordering
-- CHECK: a row stamped later than lifecycle time can be abandoned, by a start or by clock
-- recovery, at a time earlier than its own started_at (design section 7.4).
--
-- The schema does NOT implement: that deadline_at and submit_cutoff_at follow the arithmetic
-- above, the shape of either JSON column, the slot count, who may start, read or submit,
-- question eligibility, scoring or any state transition; those are the service's job (later C3
-- slices) and are not proven by these tables. There is no trigger, timer, lease or retention
-- rule, and no storage for accepted answers or correct options.
CREATE TABLE dzi_part1_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT
        CHECK (typeof(id) = 'integer' AND id > 0),
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT
        CHECK (typeof(user_id) = 'integer' AND user_id > 0),
    blueprint_id INTEGER NOT NULL REFERENCES dzi_blueprints(id) ON DELETE RESTRICT
        CHECK (typeof(blueprint_id) = 'integer' AND blueprint_id > 0),
    seed TEXT NOT NULL
        CHECK (typeof(seed) = 'text'),
    question_ids_json TEXT NOT NULL
        CHECK (typeof(question_ids_json) = 'text'),
    slot_format_json TEXT NOT NULL
        CHECK (typeof(slot_format_json) = 'text'),
    max_points_planned INTEGER NOT NULL
        CHECK (typeof(max_points_planned) = 'integer' AND max_points_planned > 0),
    duration_seconds INTEGER NOT NULL
        CHECK (typeof(duration_seconds) = 'integer' AND duration_seconds > 0),
    started_at TEXT NOT NULL
        CHECK (typeof(started_at) = 'text'
               AND length(started_at) = 19
               AND length(CAST(started_at AS BLOB)) = 19
               AND started_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]'),
    deadline_at TEXT NOT NULL
        CHECK (typeof(deadline_at) = 'text'
               AND length(deadline_at) = 19
               AND length(CAST(deadline_at AS BLOB)) = 19
               AND deadline_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]'),
    submit_cutoff_at TEXT NOT NULL
        CHECK (typeof(submit_cutoff_at) = 'text'
               AND length(submit_cutoff_at) = 19
               AND length(CAST(submit_cutoff_at AS BLOB)) = 19
               AND submit_cutoff_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]'),
    submitted_at TEXT
        CHECK (submitted_at IS NULL
               OR (typeof(submitted_at) = 'text'
                   AND length(submitted_at) = 19
                   AND length(CAST(submitted_at AS BLOB)) = 19
                   AND submitted_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]')),
    expired_at TEXT
        CHECK (expired_at IS NULL
               OR (typeof(expired_at) = 'text'
                   AND length(expired_at) = 19
                   AND length(CAST(expired_at AS BLOB)) = 19
                   AND expired_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]')),
    abandoned_at TEXT
        CHECK (abandoned_at IS NULL
               OR (typeof(abandoned_at) = 'text'
                   AND length(abandoned_at) = 19
                   AND length(CAST(abandoned_at AS BLOB)) = 19
                   AND abandoned_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]')),
    score_points INTEGER,
    score_max INTEGER,
    CHECK (started_at < deadline_at AND deadline_at < submit_cutoff_at),
    CHECK ((submitted_at IS NOT NULL) + (expired_at IS NOT NULL) + (abandoned_at IS NOT NULL) <= 1),
    CHECK (submitted_at IS NULL OR (started_at <= submitted_at AND submitted_at < submit_cutoff_at)),
    CHECK (expired_at IS NULL OR expired_at >= submit_cutoff_at),
    CHECK (submitted_at IS NOT NULL OR (score_points IS NULL AND score_max IS NULL)),
    CHECK (submitted_at IS NULL
           OR (typeof(score_points) = 'integer' AND typeof(score_max) = 'integer'
               AND score_points >= 0 AND score_max > 0 AND score_points <= score_max))
);

-- At most one open attempt (none of the three terminal timestamps set) per account. A submitted,
-- expired or abandoned attempt is outside the index, so a fresh open attempt may follow any
-- terminal state; another account is a different key. This is the only guarantee here: the start
-- service still decides resume, expiry, abandonment and replacement inside one write transaction.
CREATE UNIQUE INDEX uniq_dzi_part1_attempts_open_user
    ON dzi_part1_attempts(user_id)
    WHERE submitted_at IS NULL AND expired_at IS NULL AND abandoned_at IS NULL;

-- Owner-scoped history reads: one account's attempts in start order.
CREATE INDEX idx_dzi_part1_attempts_user_started
    ON dzi_part1_attempts(user_id, started_at);

-- dzi_part1_answers: the stored verdict per answered part of an attempt. Rows exist only after
-- the submit, so per-row points cannot protect an attempt in progress. The primary key (attempt,
-- slot, part) makes each slot part unique within an attempt and there is no surrogate id. Both
-- references are RESTRICT: while an answer references an attempt or a question, SQLite refuses to
-- delete that attempt or question. That states no lifetime or retention policy and no implicit
-- cleanup; a future, explicitly scoped cleanup could remove dependants first. None is implemented.
--
-- Row-local checks only: positive ids; kind in (multiple_choice, short_answer);
-- subquestion_number is 0 for a multiple-choice row and 1 or more for a short-answer row;
-- chosen_letter (the stored option key, text or NULL for "no choice") belongs to a multiple-choice
-- row only and raw_answer (text or NULL for "no answer") to a short-answer row only; is_correct is
-- 0 or 1; points_possible is a positive whole number and points_awarded a whole number that is 0 or
-- points_possible, and it is points_possible exactly when the verdict is 1 (no partial credit
-- inside one part); grader_version is non-empty text. raw_answer has NO length CHECK: its bound is
-- the service's, so changing the bound later needs no table rebuild. No question text, accepted
-- answer, blob, name or client timestamp is stored.
--
-- Uniqueness, stated accurately. The second unique key (attempt, question, part) prevents only a
-- duplicate of the SAME question part within an attempt. It does NOT keep one question out of two
-- slots: (slot 16, question Q, part 1) together with (slot 17, question Q, part 2) is accepted by
-- this table. That guarantee is a service invariant, not a table one: every row the service writes
-- has question_id equal to the stored plan entry of its slot, kind equal to that slot's
-- slot_format_json entry, points_possible equal to the slot's points (multiple choice) or to a
-- stored part weight (short answer), and a subquestion_number that is a stored part of the
-- question. The schema does not hold the plan and cannot check any of that.
CREATE TABLE dzi_part1_answers (
    attempt_id INTEGER NOT NULL REFERENCES dzi_part1_attempts(id) ON DELETE RESTRICT
        CHECK (typeof(attempt_id) = 'integer' AND attempt_id > 0),
    slot_number INTEGER NOT NULL
        CHECK (typeof(slot_number) = 'integer' AND slot_number > 0),
    subquestion_number INTEGER NOT NULL
        CHECK (typeof(subquestion_number) = 'integer' AND subquestion_number >= 0),
    question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE RESTRICT
        CHECK (typeof(question_id) = 'integer' AND question_id > 0),
    kind TEXT NOT NULL
        CHECK (typeof(kind) = 'text' AND kind IN ('multiple_choice', 'short_answer')),
    chosen_letter TEXT
        CHECK (chosen_letter IS NULL OR typeof(chosen_letter) = 'text'),
    raw_answer TEXT
        CHECK (raw_answer IS NULL OR typeof(raw_answer) = 'text'),
    is_correct INTEGER NOT NULL
        CHECK (typeof(is_correct) = 'integer' AND is_correct IN (0, 1)),
    points_possible INTEGER NOT NULL
        CHECK (typeof(points_possible) = 'integer' AND points_possible > 0),
    points_awarded INTEGER NOT NULL
        CHECK (typeof(points_awarded) = 'integer' AND points_awarded >= 0),
    grader_version TEXT NOT NULL
        CHECK (typeof(grader_version) = 'text' AND grader_version <> ''),
    PRIMARY KEY (attempt_id, slot_number, subquestion_number),
    UNIQUE (attempt_id, question_id, subquestion_number),
    CHECK (kind <> 'multiple_choice' OR (subquestion_number = 0 AND raw_answer IS NULL)),
    CHECK (kind <> 'short_answer' OR (subquestion_number >= 1 AND chosen_letter IS NULL)),
    CHECK (points_awarded = CASE WHEN is_correct = 1 THEN points_possible ELSE 0 END)
);
