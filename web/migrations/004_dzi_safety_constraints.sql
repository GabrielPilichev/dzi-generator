PRAGMA foreign_keys = ON;

CREATE UNIQUE INDEX IF NOT EXISTS uniq_questions_source_exam_number
ON questions(source_exam, source_number)
WHERE source_exam IS NOT NULL AND source_number IS NOT NULL;
