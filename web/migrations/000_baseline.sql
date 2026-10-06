PRAGMA foreign_keys = ON;

-- 000_baseline.sql
-- Schema-only baseline: the core tables that existed before migration 001.
--
-- A fresh database runs this file first, then 001-010. An existing database
-- already contains this schema and records 000 without running it:
--   python3 -m src.manage_migrations --db <db> --baseline-dry-run
--   python3 -m src.manage_migrations --db <db> --baseline-apply
--
-- Scope rules:
--   * No data. Canonical content is separate from the schema.
--   * Nothing owned by a numbered migration is created here. In particular the
--     columns added by 002 (curriculum_sections provenance), 006
--     (quiz_assignments.question_plan_json), 009 (quiz_text_answers
--     .teacher_override_status) and 010 (topic_section_assignments
--     .display_order) are deliberately absent, as are the tables and indexes
--     created by 001, 003, 004, 005, 007 and 008.
--   * Column order matters: the columns that older tools appended to
--     curriculum_modules, curriculum_topics and questions are listed in their
--     final position.

-- Curriculum structure ------------------------------------------------------

CREATE TABLE IF NOT EXISTS curricula (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    grade INTEGER NOT NULL,
    subject TEXT NOT NULL DEFAULT 'Информационни технологии',
    education_type TEXT NOT NULL,
    title_bg TEXT NOT NULL,
    source_type TEXT,
    source_url TEXT,
    notes TEXT,
    UNIQUE (grade, subject, education_type)
);

CREATE TABLE IF NOT EXISTS curriculum_areas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    area_id TEXT UNIQUE NOT NULL,         -- 'spreadsheets', 'databases', ...
    title_bg TEXT NOT NULL,               -- 'Електронни таблици'
    moc_filename TEXT,                    -- 'spreadsheets-moc.md'
    description TEXT
);

CREATE TABLE IF NOT EXISTS curriculum_modules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    module_number INTEGER NOT NULL,       -- 1, 2, 3, 4
    title_bg TEXT NOT NULL,
    class INTEGER NOT NULL,               -- 11 or 12
    hours_per_year INTEGER,
    moc_filename TEXT,
    description TEXT,
    curriculum_id INTEGER REFERENCES curricula(id),
    module_slug TEXT,
    module_type TEXT,
    is_dzi_relevant INTEGER DEFAULT 0,
    display_order INTEGER,
    UNIQUE (module_number)
);

CREATE TABLE IF NOT EXISTS curriculum_sections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    curriculum_id INTEGER REFERENCES curricula(id),
    module_id INTEGER REFERENCES curriculum_modules(id),
    section_slug TEXT UNIQUE NOT NULL,
    title_bg TEXT NOT NULL,
    class INTEGER NOT NULL,
    section_type TEXT NOT NULL,
    display_order INTEGER,
    has_section_test INTEGER DEFAULT 0,
    is_entry_check_scope INTEGER DEFAULT 0,
    is_exit_check_scope INTEGER DEFAULT 0,
    is_dzi_relevant INTEGER DEFAULT 0,
    source_type TEXT,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    section_id INTEGER NOT NULL REFERENCES curriculum_sections(id) ON DELETE CASCADE,
    lesson_number INTEGER,
    title_bg TEXT NOT NULL,
    lesson_type TEXT NOT NULL,
    display_order INTEGER,
    source_type TEXT,
    notes TEXT,
    UNIQUE (section_id, title_bg)
);

CREATE TABLE IF NOT EXISTS curriculum_topics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_slug TEXT UNIQUE NOT NULL,      -- 'sumif', 'vlookup', etc. (matches filename)
    title_bg TEXT NOT NULL,               -- 'SUMIF'

    -- Категоризация
    area_id INTEGER,                      -- FK to curriculum_areas
    module_id INTEGER,                    -- FK to curriculum_modules (nullable)

    -- Метаданни
    bloom_level TEXT,                     -- 'knowledge', 'comprehension', 'application', 'analysis', 'synthesis', 'evaluation'
    difficulty TEXT DEFAULT 'medium',
    exam_relevance TEXT,                  -- JSON array: ["DZI", "NVO_10"]

    -- Vault sync
    note_path TEXT,                       -- 'Topics/sumif.md'
    note_hash TEXT,                       -- SHA256 на frontmatter+body
    last_synced TEXT,

    -- Описание (от MD body — auto-extracted)
    description TEXT,

    -- Audit
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,

    -- Added after the original topic table, kept in their final position
    section_id INTEGER REFERENCES curriculum_sections(id),
    lesson_id INTEGER REFERENCES lessons(id),
    topic_level TEXT,
    assessment_weight REAL,

    FOREIGN KEY (area_id) REFERENCES curriculum_areas(id) ON DELETE SET NULL,
    FOREIGN KEY (module_id) REFERENCES curriculum_modules(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS topic_classes (
    topic_id INTEGER NOT NULL,
    class INTEGER NOT NULL,               -- 8, 9, 10, 11, 12

    PRIMARY KEY (topic_id, class),
    FOREIGN KEY (topic_id) REFERENCES curriculum_topics(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS topic_concepts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id INTEGER NOT NULL,
    concept TEXT NOT NULL,                -- 'syntax', 'examples_numbers', 'common_errors', ...
    description TEXT,

    FOREIGN KEY (topic_id) REFERENCES curriculum_topics(id) ON DELETE CASCADE,
    UNIQUE (topic_id, concept)
);

CREATE TABLE IF NOT EXISTS topic_prerequisites (
    topic_id INTEGER NOT NULL,
    requires_topic_id INTEGER NOT NULL,

    PRIMARY KEY (topic_id, requires_topic_id),
    FOREIGN KEY (topic_id) REFERENCES curriculum_topics(id) ON DELETE CASCADE,
    FOREIGN KEY (requires_topic_id) REFERENCES curriculum_topics(id) ON DELETE CASCADE,
    CHECK (topic_id != requires_topic_id)
);

CREATE TABLE IF NOT EXISTS topic_aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alias_slug TEXT NOT NULL UNIQUE,
    topic_id INTEGER REFERENCES curriculum_topics(id) ON DELETE SET NULL,
    action TEXT NOT NULL,
    reason TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS topic_section_assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id INTEGER NOT NULL REFERENCES curriculum_topics(id) ON DELETE CASCADE,
    section_id INTEGER NOT NULL REFERENCES curriculum_sections(id) ON DELETE CASCADE,
    relationship_type TEXT NOT NULL DEFAULT 'covers',
    is_primary INTEGER DEFAULT 0,
    confidence REAL DEFAULT 1.0,
    source_type TEXT DEFAULT 'manual_mapping',
    notes TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (topic_id, section_id)
);

CREATE TABLE IF NOT EXISTS assessment_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    curriculum_id INTEGER REFERENCES curricula(id),
    module_id INTEGER REFERENCES curriculum_modules(id),
    section_id INTEGER REFERENCES curriculum_sections(id),
    event_slug TEXT UNIQUE NOT NULL,
    title_bg TEXT NOT NULL,
    class INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    display_order INTEGER,
    is_national INTEGER DEFAULT 0,
    is_dzi INTEGER DEFAULT 0,
    source_type TEXT,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS assessment_event_sections (
    assessment_event_id INTEGER NOT NULL REFERENCES assessment_events(id) ON DELETE CASCADE,
    section_id INTEGER NOT NULL REFERENCES curriculum_sections(id) ON DELETE CASCADE,
    weight REAL,
    PRIMARY KEY (assessment_event_id, section_id)
);

-- Exams and questions --------------------------------------------------------

CREATE TABLE IF NOT EXISTS exams (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    -- Категоризация (parser-ът извлича всичко това)
    subject TEXT NOT NULL,            -- 'informatika_it', 'matematika', 'bel', 'angliyski', 'istoriya'
    level TEXT NOT NULL,              -- 'DZI', 'NVO_7', 'NVO_10'
    year INTEGER NOT NULL,            -- 2025
    session TEXT,                     -- 'may', 'august', 'june' (NVO)
    variant INTEGER DEFAULT 1,        -- 1, 2 (когато има две варианта)
    format_version TEXT,              -- 'modern_2023', 'legacy_pre2023', etc.

    -- Метаданни за източника
    source_url TEXT,                  -- от къде е свален
    source_file TEXT NOT NULL,        -- път до PDF в reference/
    sha256 TEXT UNIQUE,               -- за дедупликация

    -- Времеви маркери
    downloaded_at TEXT,
    parsed_at TEXT DEFAULT CURRENT_TIMESTAMP,
    parser_version TEXT,              -- кой parser е ползван

    UNIQUE (subject, level, year, session, variant)
);

CREATE TABLE IF NOT EXISTS questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    -- Връзка към източник (нов)
    exam_id INTEGER,

    -- Стари метаданни (запазени за обратна съвместимост и debug)
    source_exam TEXT NOT NULL,
    source_number INTEGER,

    -- Нови мета-полета (denormalized от exams за бързи queries)
    subject TEXT,
    level TEXT,
    year INTEGER,

    -- Тип и категоризация (CHECK разширен)
    question_type TEXT NOT NULL CHECK (
        question_type IN (
            'multiple_choice',
            'fill_in',
            'true_false',
            'matching',
            'free_response',
            'practical',
            'short_answer'
        )
    ),
    topic TEXT,
    difficulty TEXT DEFAULT 'medium',
    points INTEGER NOT NULL,

    -- Съдържание
    prompt TEXT NOT NULL,
    has_image INTEGER DEFAULT 0,
    image_path TEXT,

    -- Метаданни
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    is_ai_generated INTEGER DEFAULT 0,
    quality_score REAL,                   -- 0.0-1.0, optional, попълнено от quality checker

    -- Added after the original question table, kept in their final position
    topic_id INTEGER,
    legacy_topic TEXT,

    FOREIGN KEY (exam_id) REFERENCES exams(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS multiple_choice_options (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id INTEGER NOT NULL,
    option_letter TEXT NOT NULL,
    option_text TEXT NOT NULL,
    is_correct INTEGER DEFAULT 0,

    FOREIGN KEY (question_id) REFERENCES questions(id) ON DELETE CASCADE,
    UNIQUE (question_id, option_letter)
);

CREATE TABLE IF NOT EXISTS fill_in_subquestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id INTEGER NOT NULL,
    subquestion_number INTEGER NOT NULL,
    subquestion_text TEXT,
    correct_answer TEXT NOT NULL,
    answer_alternatives TEXT,
    points INTEGER DEFAULT 1,

    FOREIGN KEY (question_id) REFERENCES questions(id) ON DELETE CASCADE,
    UNIQUE (question_id, subquestion_number)
);

CREATE TABLE IF NOT EXISTS question_topic_assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    topic_id INTEGER REFERENCES curriculum_topics(id) ON DELETE SET NULL,
    section_id INTEGER REFERENCES curriculum_sections(id) ON DELETE SET NULL,
    assignment_type TEXT NOT NULL,
    confidence REAL,
    margin REAL,
    method TEXT,
    model TEXT,
    is_active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS generated_exams (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exam_name TEXT,
    generated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    question_ids TEXT NOT NULL,
    output_file_path TEXT,

    -- Нови: filter параметрите, използвани при генерация
    subject TEXT,
    level TEXT,
    target_points INTEGER
);

-- Vault and scraping ---------------------------------------------------------

CREATE TABLE IF NOT EXISTS obsidian_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT UNIQUE NOT NULL,       -- 'Topics/sumif.md', 'MOCs/klas-9-moc.md', ...
    note_type TEXT NOT NULL,              -- 'topic', 'moc', 'daily', 'lesson', 'home'
    title TEXT,                           -- от frontmatter

    -- Frontmatter полета (denormalized за бързи queries)
    frontmatter_json TEXT,                -- пълен YAML като JSON
    tags TEXT,                            -- comma-separated (за LIKE queries)
    classes TEXT,                         -- comma-separated, напр. '9,11'

    -- Body
    body_text TEXT,                       -- markdown body без frontmatter
    body_hash TEXT,                       -- SHA256 на body

    -- File metadata
    file_size INTEGER,
    file_mtime TEXT,                      -- file modification time (ISO)

    -- Audit
    last_synced TEXT DEFAULT CURRENT_TIMESTAMP,

    -- Връзка към topic ако е "topic" type
    topic_id INTEGER,
    FOREIGN KEY (topic_id) REFERENCES curriculum_topics(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS note_question_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id INTEGER NOT NULL,
    note_id INTEGER NOT NULL,
    relevance_score REAL DEFAULT 1.0,     -- 0.0-1.0, попълнено от similarity / manual
    link_type TEXT DEFAULT 'manual',      -- 'manual', 'auto_keyword', 'auto_embedding'

    FOREIGN KEY (question_id) REFERENCES questions(id) ON DELETE CASCADE,
    FOREIGN KEY (note_id) REFERENCES obsidian_notes(id) ON DELETE CASCADE,
    UNIQUE (question_id, note_id)
);

CREATE TABLE IF NOT EXISTS scrape_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,             -- 'zamatura.eu', 'mon.bg', etc.
    url TEXT NOT NULL,
    fetched_at TEXT DEFAULT CURRENT_TIMESTAMP,
    status TEXT NOT NULL,             -- 'success', 'skipped_dup', 'http_error', 'parse_error'
    file_path TEXT,                   -- къде е свален PDF (ако)
    notes TEXT,

    UNIQUE (source, url)
);

-- Indexes --------------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_assessment_events_class ON assessment_events(class);
CREATE INDEX IF NOT EXISTS idx_assessment_events_slug ON assessment_events(event_slug);
CREATE INDEX IF NOT EXISTS idx_assessment_sections_event ON assessment_event_sections(assessment_event_id);
CREATE INDEX IF NOT EXISTS idx_assessment_sections_section ON assessment_event_sections(section_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_curriculum_modules_slug
ON curriculum_modules(module_slug)
WHERE module_slug IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_exams_subject_year ON exams(subject, year);
CREATE INDEX IF NOT EXISTS idx_lessons_section ON lessons(section_id);
CREATE INDEX IF NOT EXISTS idx_links_note ON note_question_links(note_id);
CREATE INDEX IF NOT EXISTS idx_links_question ON note_question_links(question_id);
CREATE INDEX IF NOT EXISTS idx_obsidian_path ON obsidian_notes(file_path);
CREATE INDEX IF NOT EXISTS idx_obsidian_topic ON obsidian_notes(topic_id);
CREATE INDEX IF NOT EXISTS idx_obsidian_type ON obsidian_notes(note_type);
CREATE INDEX IF NOT EXISTS idx_options_question ON multiple_choice_options(question_id);
CREATE INDEX IF NOT EXISTS idx_qta_active ON question_topic_assignments(is_active);
CREATE INDEX IF NOT EXISTS idx_qta_question ON question_topic_assignments(question_id);
CREATE INDEX IF NOT EXISTS idx_qta_section ON question_topic_assignments(section_id);
CREATE INDEX IF NOT EXISTS idx_qta_topic ON question_topic_assignments(topic_id);
CREATE INDEX IF NOT EXISTS idx_questions_exam ON questions(exam_id);
CREATE INDEX IF NOT EXISTS idx_questions_level ON questions(level);
CREATE INDEX IF NOT EXISTS idx_questions_source ON questions(source_exam);
CREATE INDEX IF NOT EXISTS idx_questions_subject ON questions(subject);
CREATE INDEX IF NOT EXISTS idx_questions_topic ON questions(topic);
CREATE INDEX IF NOT EXISTS idx_questions_type ON questions(question_type);
CREATE INDEX IF NOT EXISTS idx_scrape_url ON scrape_log(url);
CREATE INDEX IF NOT EXISTS idx_sections_class ON curriculum_sections(class);
CREATE INDEX IF NOT EXISTS idx_sections_module ON curriculum_sections(module_id);
CREATE INDEX IF NOT EXISTS idx_sections_slug ON curriculum_sections(section_slug);
CREATE INDEX IF NOT EXISTS idx_subquestions_question ON fill_in_subquestions(question_id);
CREATE INDEX IF NOT EXISTS idx_topic_aliases_alias ON topic_aliases(alias_slug);
CREATE INDEX IF NOT EXISTS idx_topic_classes_class ON topic_classes(class);
CREATE INDEX IF NOT EXISTS idx_topic_classes_topic ON topic_classes(topic_id);
CREATE INDEX IF NOT EXISTS idx_topics_area ON curriculum_topics(area_id);
CREATE INDEX IF NOT EXISTS idx_topics_lesson ON curriculum_topics(lesson_id);
CREATE INDEX IF NOT EXISTS idx_topics_module ON curriculum_topics(module_id);
CREATE INDEX IF NOT EXISTS idx_topics_section ON curriculum_topics(section_id);
CREATE INDEX IF NOT EXISTS idx_topics_slug ON curriculum_topics(topic_slug);
CREATE INDEX IF NOT EXISTS idx_tsa_primary ON topic_section_assignments(is_primary);
CREATE INDEX IF NOT EXISTS idx_tsa_section ON topic_section_assignments(section_id);
CREATE INDEX IF NOT EXISTS idx_tsa_topic ON topic_section_assignments(topic_id);
