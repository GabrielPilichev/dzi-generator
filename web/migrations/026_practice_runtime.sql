PRAGMA foreign_keys = ON;

-- Student practice runtime storage (C2.1; docs/STUDENT_PRACTICE_DESIGN.md, section 2).
-- Storage only: no route, service, UI or activation reads or writes these tables yet.
-- Runtime state, never canonical: a fresh build leaves both tables empty and the
-- canonical export leaves them out (src/canonical_tables.py).
--
-- Additive: two new tables and two indexes. No existing table is rebuilt or altered,
-- no row is seeded and no legacy attempt is backfilled. Code that predates this
-- migration never names these tables and runs unchanged.
--
-- practice_sessions: one practice run of one account over one curriculum scope.
-- user_id comes only from the validated principal. The foreign keys are RESTRICT,
-- never CASCADE or SET NULL: while a session references an account, section or
-- topic, SQLite refuses to delete that parent, and no deletion silently rewrites
-- or orphans a session. That is all RESTRICT does: it states no lifetime or
-- retention policy for any row. A later, explicitly scoped cleanup could remove
-- dependants first (answers, then sessions); none is implemented here. The session
-- id is positive like every other id. topic_id is NULL for
-- a section-wide session; a stored topic id is positive, which is what lets the
-- open-scope key below use 0 as its NULL stand-in without ever colliding with a
-- real topic. The plan (question_ids_json) and seed are required and stored as
-- text, never as a blob; no name and no question text is stored.
--
-- The CHECK constraints are row-local invariants only: a session is not both
-- submitted and abandoned, an unsubmitted session has no score, and a submitted one
-- has a whole, nonnegative score no larger than a positive total. The schema does NOT
-- implement who may start or read a session, the shape of the JSON plan, whether a
-- topic belongs to its section, question eligibility, scoring or state transitions:
-- those are the service's job (a later C2 slice) and are not proven by these tables.
-- There is no trigger, timer or retention rule.
CREATE TABLE practice_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT
        CHECK (typeof(id) = 'integer' AND id > 0),
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT
        CHECK (typeof(user_id) = 'integer' AND user_id > 0),
    section_id INTEGER NOT NULL REFERENCES curriculum_sections(id) ON DELETE RESTRICT
        CHECK (typeof(section_id) = 'integer' AND section_id > 0),
    topic_id INTEGER REFERENCES curriculum_topics(id) ON DELETE RESTRICT
        CHECK (topic_id IS NULL OR (typeof(topic_id) = 'integer' AND topic_id > 0)),
    seed TEXT NOT NULL
        CHECK (typeof(seed) = 'text'),
    question_ids_json TEXT NOT NULL
        CHECK (typeof(question_ids_json) = 'text'),
    started_at TEXT NOT NULL
        CHECK (typeof(started_at) = 'text'),
    submitted_at TEXT
        CHECK (submitted_at IS NULL OR typeof(submitted_at) = 'text'),
    abandoned_at TEXT
        CHECK (abandoned_at IS NULL OR typeof(abandoned_at) = 'text'),
    score_correct INTEGER,
    score_total INTEGER,
    CHECK (submitted_at IS NULL OR abandoned_at IS NULL),
    CHECK (submitted_at IS NOT NULL OR (score_correct IS NULL AND score_total IS NULL)),
    CHECK (submitted_at IS NULL
           OR (typeof(score_correct) = 'integer' AND typeof(score_total) = 'integer'
               AND score_correct >= 0 AND score_total > 0 AND score_correct <= score_total))
);

-- At most one open session (neither terminal timestamp set) per account and scope.
-- COALESCE(topic_id, 0) makes the NULL topic of a section-wide session collide with
-- another one (plain NULLs never do in a unique index); topic ids are positive, so 0
-- cannot be a real topic. A submitted or abandoned session is outside the index, so
-- a fresh open session may follow it; another account or another scope is a
-- different key. This is the only guarantee here: the start service still decides
-- resume, abandonment and replacement inside one write transaction.
CREATE UNIQUE INDEX uniq_practice_sessions_open_scope
    ON practice_sessions(user_id, section_id, COALESCE(topic_id, 0))
    WHERE submitted_at IS NULL AND abandoned_at IS NULL;

-- "My practice" per scope: one account's sessions of a section or topic.
CREATE INDEX idx_practice_sessions_user_scope
    ON practice_sessions(user_id, section_id, topic_id);

-- practice_answers: the stored verdict per question of a session. The primary key is
-- the uniqueness rule (one answer per session and question) and there is no
-- surrogate id. chosen_letter reuses the existing letter representation of
-- quiz_answers.chosen_letter (text or NULL); no A-D restriction is invented here,
-- because the existing eligibility checks four options without such a letter
-- contract. NULL is a verdict-bearing "no choice": the verdict itself is required
-- and Boolean. Both references are RESTRICT: while an answer references a session or
-- a question, SQLite refuses to delete that session or question. That establishes
-- no lifetime or retention policy and no implicit cleanup; a future, explicitly
-- scoped cleanup could remove dependants first. None is implemented here.
CREATE TABLE practice_answers (
    session_id INTEGER NOT NULL REFERENCES practice_sessions(id) ON DELETE RESTRICT
        CHECK (typeof(session_id) = 'integer' AND session_id > 0),
    question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE RESTRICT
        CHECK (typeof(question_id) = 'integer' AND question_id > 0),
    chosen_letter TEXT
        CHECK (chosen_letter IS NULL OR typeof(chosen_letter) = 'text'),
    is_correct INTEGER NOT NULL
        CHECK (typeof(is_correct) = 'integer' AND is_correct IN (0, 1)),
    PRIMARY KEY (session_id, question_id)
);
