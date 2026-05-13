PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS practical_task_resources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exam_task_id INTEGER NOT NULL,
    resource_path TEXT NOT NULL CHECK (length(resource_path) > 0),
    original_filename TEXT,
    label_bg TEXT,
    file_size_bytes INTEGER CHECK (file_size_bytes IS NULL OR file_size_bytes >= 0),
    sha256 TEXT CHECK (sha256 IS NULL OR length(sha256) = 64),
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (exam_task_id) REFERENCES exam_tasks(id) ON DELETE CASCADE,
    UNIQUE (exam_task_id, resource_path)
);

CREATE INDEX IF NOT EXISTS idx_practical_task_resources_exam_task
    ON practical_task_resources(exam_task_id);
