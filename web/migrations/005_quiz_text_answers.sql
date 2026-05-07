PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS quiz_text_answers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id INTEGER NOT NULL,
    question_id INTEGER NOT NULL,
    subquestion_id INTEGER,
    subquestion_number INTEGER NOT NULL,
    response_order INTEGER,
    raw_answer TEXT NOT NULL DEFAULT '',
    normalized_answer TEXT NOT NULL DEFAULT '',
    grading_mode TEXT NOT NULL DEFAULT 'ordered'
        CHECK (grading_mode IN ('ordered', 'order_independent')),
    accepted_answers_json TEXT NOT NULL DEFAULT '[]',
    matched_answer TEXT,
    is_correct INTEGER NOT NULL DEFAULT 0 CHECK (is_correct IN (0, 1)),
    points_awarded REAL NOT NULL DEFAULT 0,
    points_possible REAL NOT NULL DEFAULT 1,
    graded_at TEXT DEFAULT CURRENT_TIMESTAMP,
    grader_version TEXT,
    teacher_override INTEGER NOT NULL DEFAULT 0 CHECK (teacher_override IN (0, 1)),
    teacher_note TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (attempt_id) REFERENCES quiz_attempts(id) ON DELETE CASCADE,
    FOREIGN KEY (question_id) REFERENCES questions(id) ON DELETE CASCADE,
    FOREIGN KEY (subquestion_id) REFERENCES fill_in_subquestions(id) ON DELETE SET NULL,
    UNIQUE (attempt_id, question_id, subquestion_number)
);

CREATE INDEX IF NOT EXISTS idx_quiz_text_answers_attempt
    ON quiz_text_answers(attempt_id);

CREATE INDEX IF NOT EXISTS idx_quiz_text_answers_question
    ON quiz_text_answers(question_id);

CREATE INDEX IF NOT EXISTS idx_quiz_text_answers_attempt_question
    ON quiz_text_answers(attempt_id, question_id);

CREATE INDEX IF NOT EXISTS idx_quiz_text_answers_correct
    ON quiz_text_answers(attempt_id, is_correct);
