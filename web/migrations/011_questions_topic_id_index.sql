PRAGMA foreign_keys = ON;

-- Index the questions -> curriculum_topics join column. idx_questions_topic covers
-- the legacy TEXT column questions.topic, not topic_id. Index only; no row changes.
CREATE INDEX IF NOT EXISTS idx_questions_topic_id ON questions(topic_id);
