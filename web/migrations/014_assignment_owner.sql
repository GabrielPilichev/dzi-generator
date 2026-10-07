PRAGMA foreign_keys = ON;

ALTER TABLE quiz_assignments
    ADD COLUMN owner_user_id INTEGER REFERENCES users(id);

CREATE INDEX IF NOT EXISTS idx_quiz_assignments_owner
    ON quiz_assignments(owner_user_id, created_at, id);
