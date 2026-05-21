PRAGMA foreign_keys = ON;

ALTER TABLE quiz_text_answers
ADD COLUMN teacher_override_status TEXT
    CHECK (
        teacher_override_status IS NULL
        OR teacher_override_status IN ('correct', 'partially_correct', 'incorrect')
    );
