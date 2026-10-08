-- learnpilot-migration: table-rebuild
PRAGMA foreign_keys = ON;

-- Account-owned quiz attempts (Accounts phase 2). Runtime state: never exported to
-- data/canonical.
--
-- quiz_attempts gains user_id and loses its table-level
-- UNIQUE (assignment_id, student_name), which only a rebuild can remove. The runner
-- executes this file in its own transaction with foreign keys off and checks them
-- before committing (docs/DEPLOYMENT.md, "Table-rebuild migrations").
--
-- user_id is the account that owns the attempt. NULL is the historical state: an
-- attempt started before accounts, bound to the browser that started it. Every
-- existing row is copied with user_id NULL; nothing is linked to an account by name.
-- The foreign key is ON DELETE RESTRICT, never SET NULL: an account-owned attempt
-- must not silently become a historical one. Accounts are disabled, not deleted.
CREATE TABLE quiz_attempts_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    assignment_id INTEGER NOT NULL REFERENCES quiz_assignments(id),
    student_name TEXT NOT NULL,
    seed TEXT NOT NULL,
    question_ids_json TEXT NOT NULL,
    started_at TEXT DEFAULT CURRENT_TIMESTAMP,
    submitted_at TEXT,
    score_correct INTEGER,
    score_total INTEGER,
    user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT
);

INSERT INTO quiz_attempts_new (
    id, assignment_id, student_name, seed, question_ids_json,
    started_at, submitted_at, score_correct, score_total, user_id
)
SELECT
    id, assignment_id, student_name, seed, question_ids_json,
    started_at, submitted_at, score_correct, score_total, NULL
FROM quiz_attempts
ORDER BY id;

-- Carry the AUTOINCREMENT high-water mark over, so the id of a deleted attempt is
-- never reused. The copy above left MAX(id) (or no row, for an empty table) under
-- the new name; replace it with the old table's value. A table that never held a
-- row has no sqlite_sequence row and gets none. The rename below moves the row to
-- the final name.
DELETE FROM sqlite_sequence WHERE name = 'quiz_attempts_new';
INSERT INTO sqlite_sequence (name, seq)
SELECT 'quiz_attempts_new', MAX(seq)
FROM sqlite_sequence
WHERE name = 'quiz_attempts'
HAVING MAX(seq) IS NOT NULL;

DROP TABLE quiz_attempts;
ALTER TABLE quiz_attempts_new RENAME TO quiz_attempts;

CREATE INDEX idx_quiz_attempts_assignment ON quiz_attempts(assignment_id);

-- One attempt per account and assignment; the display name is not part of it.
CREATE UNIQUE INDEX uniq_quiz_attempts_user
    ON quiz_attempts(assignment_id, user_id)
    WHERE user_id IS NOT NULL;

-- Historical attempts keep the old one-per-name rule among themselves.
CREATE UNIQUE INDEX uniq_quiz_attempts_legacy_name
    ON quiz_attempts(assignment_id, student_name)
    WHERE user_id IS NULL;

-- "My attempts": one account's attempts, newest first.
CREATE INDEX idx_quiz_attempts_user
    ON quiz_attempts(user_id, started_at, id)
    WHERE user_id IS NOT NULL;
