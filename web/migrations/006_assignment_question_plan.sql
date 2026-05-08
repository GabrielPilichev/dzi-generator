PRAGMA foreign_keys = ON;

ALTER TABLE quiz_assignments
    ADD COLUMN question_plan_json TEXT NULL;
