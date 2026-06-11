-- ДЗИ Generator — Schema v2
-- SQLite 3
-- Промени спрямо v1:
--   * exams таблица — източниците стават first-class entities
--   * questions: добавени subject, level, year, session, variant, format_version
--   * question_type CHECK разширен с true_false, matching, free_response, practical
--   * Foreign key questions.exam_id -> exams.id
--   * Запазен е source_exam като denormalized текст (за обратна съвместимост)

-- ============================================================
-- exams: източници (един ред = един изпит вариант)
-- ============================================================
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

-- ============================================================
-- questions: въпроси (включва нови мета-полета)
-- ============================================================
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
    quality_score REAL,               -- 0.0-1.0, optional, попълнено от quality checker
    
    FOREIGN KEY (exam_id) REFERENCES exams(id) ON DELETE SET NULL
);

-- ============================================================
-- multiple_choice_options
-- ============================================================
CREATE TABLE IF NOT EXISTS multiple_choice_options (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id INTEGER NOT NULL,
    option_letter TEXT NOT NULL,
    option_text TEXT NOT NULL,
    is_correct INTEGER DEFAULT 0,
    
    FOREIGN KEY (question_id) REFERENCES questions(id) ON DELETE CASCADE,
    UNIQUE (question_id, option_letter)
);

-- ============================================================
-- fill_in_subquestions
-- ============================================================
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

-- ============================================================
-- generated_exams
-- ============================================================
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

-- ============================================================
-- scrape_log: история на скрейпването (за rate limiting & dedup)
-- ============================================================
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

-- ============================================================
-- Индекси
-- ============================================================
CREATE INDEX IF NOT EXISTS idx_questions_topic ON questions(topic);
CREATE INDEX IF NOT EXISTS idx_questions_type ON questions(question_type);
CREATE INDEX IF NOT EXISTS idx_questions_source ON questions(source_exam);
CREATE INDEX IF NOT EXISTS idx_questions_subject ON questions(subject);
CREATE INDEX IF NOT EXISTS idx_questions_level ON questions(level);
CREATE INDEX IF NOT EXISTS idx_questions_exam ON questions(exam_id);
CREATE INDEX IF NOT EXISTS idx_options_question ON multiple_choice_options(question_id);
CREATE INDEX IF NOT EXISTS idx_subquestions_question ON fill_in_subquestions(question_id);
CREATE INDEX IF NOT EXISTS idx_exams_subject_year ON exams(subject, year);
CREATE INDEX IF NOT EXISTS idx_scrape_url ON scrape_log(url);
