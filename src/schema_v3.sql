-- ДЗИ Generator — Schema v3
-- SQLite 3
-- Добавя curriculum + Obsidian sync таблиците към v2.
--
-- Промени спрямо v2:
--   * curriculum_areas — тематични области (spreadsheets, databases, ...)
--   * curriculum_modules — 4-те модула за ПП (11-12 клас)
--   * curriculum_topics — атомарни концепти (SUMIF, VLOOKUP, GPS, ...)
--   * topic_concepts — sub-concepts вътре в един topic
--   * topic_classes — many-to-many: topic ↔ class (един topic може да е в 2-3 класа)
--   * topic_prerequisites — DAG на предусловия
--   * obsidian_notes — track на vault-а
--   * note_question_links — въпрос ↔ topic note (за RAG)
--   * questions.topic_id — FK (NEW); запазваме стария text 'topic' като legacy
--
-- Backward compatibility: всички v2 заявки продължават да работят.

-- ============================================================
-- curriculum_areas: тематични области
-- ============================================================
CREATE TABLE IF NOT EXISTS curriculum_areas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    area_id TEXT UNIQUE NOT NULL,         -- 'spreadsheets', 'databases', ...
    title_bg TEXT NOT NULL,               -- 'Електронни таблици'
    moc_filename TEXT,                    -- 'spreadsheets-moc.md'
    description TEXT
);

-- ============================================================
-- curriculum_modules: ПП модули за 11-12 клас
-- ============================================================
CREATE TABLE IF NOT EXISTS curriculum_modules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    module_number INTEGER NOT NULL,       -- 1, 2, 3, 4
    title_bg TEXT NOT NULL,
    class INTEGER NOT NULL,               -- 11 or 12
    hours_per_year INTEGER,
    moc_filename TEXT,
    description TEXT,
    UNIQUE (module_number)
);

-- ============================================================
-- curriculum_topics: атомарни концепти (eq на Topics/ in vault)
-- ============================================================
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
    
    FOREIGN KEY (area_id) REFERENCES curriculum_areas(id) ON DELETE SET NULL,
    FOREIGN KEY (module_id) REFERENCES curriculum_modules(id) ON DELETE SET NULL
);

-- ============================================================
-- topic_classes: many-to-many topic ↔ class (един topic в няколко класа)
-- ============================================================
CREATE TABLE IF NOT EXISTS topic_classes (
    topic_id INTEGER NOT NULL,
    class INTEGER NOT NULL,               -- 8, 9, 10, 11, 12
    
    PRIMARY KEY (topic_id, class),
    FOREIGN KEY (topic_id) REFERENCES curriculum_topics(id) ON DELETE CASCADE
);

-- ============================================================
-- topic_concepts: подконцепти вътре в един topic
-- ============================================================
CREATE TABLE IF NOT EXISTS topic_concepts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id INTEGER NOT NULL,
    concept TEXT NOT NULL,                -- 'syntax', 'examples_numbers', 'common_errors', ...
    description TEXT,
    
    FOREIGN KEY (topic_id) REFERENCES curriculum_topics(id) ON DELETE CASCADE,
    UNIQUE (topic_id, concept)
);

-- ============================================================
-- topic_prerequisites: DAG на предусловия (topic A изисква B)
-- ============================================================
CREATE TABLE IF NOT EXISTS topic_prerequisites (
    topic_id INTEGER NOT NULL,
    requires_topic_id INTEGER NOT NULL,
    
    PRIMARY KEY (topic_id, requires_topic_id),
    FOREIGN KEY (topic_id) REFERENCES curriculum_topics(id) ON DELETE CASCADE,
    FOREIGN KEY (requires_topic_id) REFERENCES curriculum_topics(id) ON DELETE CASCADE,
    CHECK (topic_id != requires_topic_id)
);

-- ============================================================
-- obsidian_notes: track на всеки .md файл в vault-а
-- ============================================================
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

-- ============================================================
-- note_question_links: question ↔ note връзки (за RAG)
-- ============================================================
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

-- ============================================================
-- ALTER questions: добавяме topic_id (nullable за обратна съвместимост)
-- (executed by migration script — не може CHECK тук)
-- ============================================================
-- ALTER TABLE questions ADD COLUMN topic_id INTEGER;
-- ALTER TABLE questions ADD COLUMN legacy_topic TEXT; (стария 'topic' се копира тук)

-- ============================================================
-- Индекси
-- ============================================================
CREATE INDEX IF NOT EXISTS idx_topics_area ON curriculum_topics(area_id);
CREATE INDEX IF NOT EXISTS idx_topics_module ON curriculum_topics(module_id);
CREATE INDEX IF NOT EXISTS idx_topics_slug ON curriculum_topics(topic_slug);
CREATE INDEX IF NOT EXISTS idx_topic_classes_topic ON topic_classes(topic_id);
CREATE INDEX IF NOT EXISTS idx_topic_classes_class ON topic_classes(class);
CREATE INDEX IF NOT EXISTS idx_obsidian_path ON obsidian_notes(file_path);
CREATE INDEX IF NOT EXISTS idx_obsidian_type ON obsidian_notes(note_type);
CREATE INDEX IF NOT EXISTS idx_obsidian_topic ON obsidian_notes(topic_id);
CREATE INDEX IF NOT EXISTS idx_links_question ON note_question_links(question_id);
CREATE INDEX IF NOT EXISTS idx_links_note ON note_question_links(note_id);

-- ============================================================
-- Seed: тематични области (от learning programs)
-- ============================================================
INSERT OR IGNORE INTO curriculum_areas (area_id, title_bg, moc_filename, description) VALUES
    ('spreadsheets', 'Електронни таблици', 'spreadsheets-moc.md',
     'Excel-style ЕТ. От 9 клас (основи) до 11 клас Модул 1 (advanced).'),
    ('databases', 'Бази данни', 'databases-moc.md',
     'Релационни БД, SQL, проектиране. Главно 11 клас Модул 1.'),
    ('web', 'Уеб технологии', 'web-moc.md',
     'HTML, CSS, дизайн, CMS, сигурност. 8 клас (въведение) и 12 клас Модул 3 (професионално).'),
    ('graphics', 'Графика и обработка на изображения', 'graphics-moc.md',
     'Растер и вектор. 11 клас Модул 2.'),
    ('video_audio', 'Видео и аудио', 'video-audio-moc.md',
     'Цифровизация, монтаж, кодеци. 11 клас Модул 2.'),
    ('info_systems', 'Информационни системи', 'info-systems-moc.md',
     'SDLC, проектиране на ИС, управление на проекти. 11-12 клас.'),
    ('ai_programming', 'AI и програмиране', 'ai-programming-moc.md',
     'Скриптови езици, алгоритми, ML основи. Прогресия 8-9-10 клас.'),
    ('networks', 'Компютърни мрежи и услуги', NULL,
     'Мрежи, протоколи, сигурност. 8-9 клас.'),
    ('applications', 'Приложни програми', NULL,
     'Текстообработка, шаблони, циркулярни писма. 9 клас.');

-- ============================================================
-- Seed: ПП модули
-- ============================================================
INSERT OR IGNORE INTO curriculum_modules (module_number, title_bg, class, hours_per_year, moc_filename, description) VALUES
    (1, 'Обработка и анализ на данни', 11, 72, 'module-1-data-moc.md',
     'ЕТ за големи обеми, въведение в ИС, проектиране на БД'),
    (2, 'Мултимедия', 11, 72, 'module-2-multimedia-moc.md',
     'Цифровизация, графика, звук, видео, мултимедийни продукти'),
    (3, 'Уеб дизайн', 12, 62, 'module-3-web-moc.md',
     'HTML/CSS, CMS, сигурност в уеб'),
    (4, 'Решаване на проблеми с ИКТ', 12, 62, 'module-4-ict-moc.md',
     'Управление на проекти, компютърни системи, авторски права');
