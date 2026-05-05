PRAGMA foreign_keys = ON;

-- 003_dzi_tasks_assets_blueprint.sql
-- Additive support for official DZI task structure, file assets, practical
-- task metadata, and blueprint-driven generation/validation.
--
-- Existing tables reused:
--   exams               official exam identity
--   questions           reusable question bank
--   curriculum_topics   topic linkage
--   curriculum_sections curriculum/section linkage

CREATE TABLE IF NOT EXISTS official_exam_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exam_id INTEGER REFERENCES exams(id) ON DELETE CASCADE,
    authority TEXT NOT NULL DEFAULT 'MON',
    source_kind TEXT NOT NULL,
    source_url TEXT,
    local_path TEXT,
    sha256 TEXT,
    published_at TEXT,
    notes TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (exam_id, source_kind, source_url, local_path)
);

CREATE TABLE IF NOT EXISTS exam_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exam_id INTEGER NOT NULL REFERENCES exams(id) ON DELETE CASCADE,
    task_number INTEGER NOT NULL,
    exam_part INTEGER NOT NULL,
    task_kind TEXT NOT NULL,
    points INTEGER NOT NULL,
    prompt TEXT,
    rubric TEXT,
    topic_id INTEGER REFERENCES curriculum_topics(id) ON DELETE SET NULL,
    section_id INTEGER REFERENCES curriculum_sections(id) ON DELETE SET NULL,
    source_page_start INTEGER,
    source_page_end INTEGER,
    has_assets INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (exam_id, task_number)
);

CREATE TABLE IF NOT EXISTS exam_task_questions (
    task_id INTEGER NOT NULL REFERENCES exam_tasks(id) ON DELETE CASCADE,
    question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    role TEXT NOT NULL DEFAULT 'primary',
    sub_number INTEGER,
    PRIMARY KEY (task_id, question_id, role)
);

CREATE TABLE IF NOT EXISTS assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_type TEXT NOT NULL,
    original_filename TEXT,
    local_path TEXT NOT NULL UNIQUE,
    source_url TEXT,
    sha256 TEXT,
    mime_type TEXT,
    file_size INTEGER,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS asset_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    owner_type TEXT NOT NULL,
    owner_id INTEGER NOT NULL,
    role TEXT,
    display_order INTEGER DEFAULT 0,
    caption_bg TEXT,
    source_page INTEGER,
    source_bbox_json TEXT,
    UNIQUE (asset_id, owner_type, owner_id, role, display_order)
);

CREATE TABLE IF NOT EXISTS practical_tasks (
    task_id INTEGER PRIMARY KEY REFERENCES exam_tasks(id) ON DELETE CASCADE,
    work_environment TEXT NOT NULL,
    expected_outputs_json TEXT,
    grading_criteria_json TEXT,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS dzi_blueprints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    blueprint_slug TEXT NOT NULL UNIQUE,
    title_bg TEXT NOT NULL,
    total_points INTEGER NOT NULL DEFAULT 100,
    part1_minutes INTEGER NOT NULL DEFAULT 90,
    part2_minutes INTEGER NOT NULL DEFAULT 150,
    is_active INTEGER DEFAULT 1,
    notes TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS dzi_blueprint_slots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    blueprint_id INTEGER NOT NULL REFERENCES dzi_blueprints(id) ON DELETE CASCADE,
    slot_number INTEGER NOT NULL,
    exam_part INTEGER NOT NULL,
    task_kind TEXT NOT NULL,
    points INTEGER NOT NULL,
    topic_area TEXT,
    topic_id INTEGER REFERENCES curriculum_topics(id) ON DELETE SET NULL,
    section_id INTEGER REFERENCES curriculum_sections(id) ON DELETE SET NULL,
    required_asset_type TEXT,
    notes TEXT,
    UNIQUE (blueprint_id, slot_number)
);

CREATE INDEX IF NOT EXISTS idx_official_exam_sources_exam
    ON official_exam_sources(exam_id);

CREATE INDEX IF NOT EXISTS idx_exam_tasks_exam
    ON exam_tasks(exam_id);

CREATE INDEX IF NOT EXISTS idx_exam_tasks_kind
    ON exam_tasks(task_kind);

CREATE INDEX IF NOT EXISTS idx_exam_tasks_topic
    ON exam_tasks(topic_id);

CREATE INDEX IF NOT EXISTS idx_exam_tasks_section
    ON exam_tasks(section_id);

CREATE INDEX IF NOT EXISTS idx_exam_task_questions_question
    ON exam_task_questions(question_id);

CREATE INDEX IF NOT EXISTS idx_assets_type
    ON assets(asset_type);

CREATE INDEX IF NOT EXISTS idx_asset_links_owner
    ON asset_links(owner_type, owner_id);

CREATE INDEX IF NOT EXISTS idx_asset_links_asset
    ON asset_links(asset_id);

CREATE INDEX IF NOT EXISTS idx_practical_tasks_environment
    ON practical_tasks(work_environment);

CREATE INDEX IF NOT EXISTS idx_dzi_blueprint_slots_blueprint
    ON dzi_blueprint_slots(blueprint_id);

CREATE INDEX IF NOT EXISTS idx_dzi_blueprint_slots_kind
    ON dzi_blueprint_slots(task_kind);
