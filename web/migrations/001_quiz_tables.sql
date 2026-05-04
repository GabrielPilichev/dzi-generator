PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS quiz_assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    section_id INTEGER NOT NULL REFERENCES curriculum_sections(id),
    title_bg TEXT NOT NULL,
    question_count INTEGER NOT NULL,
    time_limit_minutes INTEGER,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS quiz_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    assignment_id INTEGER NOT NULL REFERENCES quiz_assignments(id),
    student_name TEXT NOT NULL,
    seed TEXT NOT NULL,
    question_ids_json TEXT NOT NULL,
    started_at TEXT DEFAULT CURRENT_TIMESTAMP,
    submitted_at TEXT,
    score_correct INTEGER,
    score_total INTEGER,
    UNIQUE (assignment_id, student_name)
);

CREATE TABLE IF NOT EXISTS quiz_answers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id INTEGER NOT NULL REFERENCES quiz_attempts(id),
    question_id INTEGER NOT NULL,
    chosen_letter TEXT,
    is_correct INTEGER,
    UNIQUE (attempt_id, question_id)
);

CREATE INDEX IF NOT EXISTS idx_quiz_assignments_section ON quiz_assignments(section_id);
CREATE INDEX IF NOT EXISTS idx_quiz_attempts_assignment ON quiz_attempts(assignment_id);
CREATE INDEX IF NOT EXISTS idx_quiz_answers_attempt ON quiz_answers(attempt_id);
