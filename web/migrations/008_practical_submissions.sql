PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS practical_submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    quiz_attempt_id INTEGER NOT NULL,
    exam_task_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'submitted', 'reviewed')),
    submitted_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (quiz_attempt_id) REFERENCES quiz_attempts(id) ON DELETE CASCADE,
    FOREIGN KEY (exam_task_id) REFERENCES exam_tasks(id) ON DELETE CASCADE,
    UNIQUE (quiz_attempt_id, exam_task_id)
);

CREATE TABLE IF NOT EXISTS practical_submission_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    practical_submission_id INTEGER NOT NULL,
    stored_path TEXT NOT NULL UNIQUE CHECK (length(stored_path) > 0),
    original_filename TEXT NOT NULL CHECK (length(original_filename) > 0),
    size_bytes INTEGER CHECK (size_bytes IS NULL OR size_bytes >= 0),
    mime_type TEXT,
    uploaded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    is_deleted INTEGER NOT NULL DEFAULT 0 CHECK (is_deleted IN (0, 1)),
    FOREIGN KEY (practical_submission_id) REFERENCES practical_submissions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_practical_submissions_attempt
    ON practical_submissions(quiz_attempt_id);

CREATE INDEX IF NOT EXISTS idx_practical_submissions_exam_task
    ON practical_submissions(exam_task_id);

CREATE INDEX IF NOT EXISTS idx_practical_submission_files_submission
    ON practical_submission_files(practical_submission_id);
