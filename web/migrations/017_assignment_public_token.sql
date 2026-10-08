PRAGMA foreign_keys = ON;

-- Unguessable student-facing assignment link (/quiz/s/<public_token>). Runtime state:
-- never exported to data/canonical.
--
-- Additive on purpose: quiz_assignments is not rebuilt and the column stays nullable.
-- Rows that exist before this migration keep NULL until the operator runs
-- `python3 -m src.manage_assignment_tokens --db <database>`; SQLite's randomblob() is
-- not used for these security-sensitive values. A NULL token matches no link, so such
-- an assignment simply has no public link yet. The unique index allows any number of
-- NULLs and guarantees that a token resolves to exactly one assignment.
ALTER TABLE quiz_assignments
    ADD COLUMN public_token TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_quiz_assignments_public_token
    ON quiz_assignments(public_token);
