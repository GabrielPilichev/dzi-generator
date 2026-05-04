-- ДЗИ Generator — Database Schema
-- SQLite 3
-- Версия 1.0

-- ============================================================
-- Таблица: questions
-- Съхранява всички въпроси (multiple choice и fill-in)
-- ============================================================
CREATE TABLE IF NOT EXISTS questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    
    -- Метаданни за източника
    source_exam TEXT NOT NULL,        -- напр. "may_2025_v2"
    source_number INTEGER,             -- оригинален № в изпита (1-25)
    
    -- Тип и категоризация
    question_type TEXT NOT NULL CHECK (
        question_type IN ('multiple_choice', 'fill_in')
    ),
    topic TEXT,                        -- "spreadsheets", "databases", "web", "graphics", "hardware", "security", "info_systems", "video_audio"
    difficulty TEXT DEFAULT 'medium',  -- "easy", "medium", "hard"
    points INTEGER NOT NULL,           -- 1 за multiple_choice, 3 за fill_in
    
    -- Съдържание на въпроса
    prompt TEXT NOT NULL,              -- Текстът на въпроса
    has_image INTEGER DEFAULT 0,       -- 0 или 1, дали има прикачено изображение
    image_path TEXT,                   -- Път до изображение (ако има)
    
    -- Метаданни
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    is_ai_generated INTEGER DEFAULT 0  -- 0 = от истински изпит, 1 = AI генерирано
);

-- ============================================================
-- Таблица: multiple_choice_options
-- Опции за multiple choice въпроси
-- ============================================================
CREATE TABLE IF NOT EXISTS multiple_choice_options (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id INTEGER NOT NULL,
    option_letter TEXT NOT NULL,       -- "А", "Б", "В", "Г"
    option_text TEXT NOT NULL,
    is_correct INTEGER DEFAULT 0,      -- 1 за правилния отговор
    
    FOREIGN KEY (question_id) REFERENCES questions(id) ON DELETE CASCADE,
    UNIQUE (question_id, option_letter)
);

-- ============================================================
-- Таблица: fill_in_subquestions
-- Подзадачи за fill-in въпросите (обикновено 3 на въпрос)
-- ============================================================
CREATE TABLE IF NOT EXISTS fill_in_subquestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id INTEGER NOT NULL,
    subquestion_number INTEGER NOT NULL,  -- 1, 2, 3
    subquestion_text TEXT,                -- Контекст преди празното поле (ако има)
    correct_answer TEXT NOT NULL,         -- Правилният отговор
    answer_alternatives TEXT,             -- JSON списък от алтернативни приемливи отговори
    points INTEGER DEFAULT 1,
    
    FOREIGN KEY (question_id) REFERENCES questions(id) ON DELETE CASCADE,
    UNIQUE (question_id, subquestion_number)
);

-- ============================================================
-- Таблица: generated_exams
-- История на генерирани изпити (за справка)
-- ============================================================
CREATE TABLE IF NOT EXISTS generated_exams (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exam_name TEXT,
    generated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    question_ids TEXT NOT NULL,        -- JSON масив от question.id
    output_file_path TEXT
);

-- ============================================================
-- Индекси за по-бързи заявки
-- ============================================================
CREATE INDEX IF NOT EXISTS idx_questions_topic ON questions(topic);
CREATE INDEX IF NOT EXISTS idx_questions_type ON questions(question_type);
CREATE INDEX IF NOT EXISTS idx_questions_source ON questions(source_exam);
CREATE INDEX IF NOT EXISTS idx_options_question ON multiple_choice_options(question_id);
CREATE INDEX IF NOT EXISTS idx_subquestions_question ON fill_in_subquestions(question_id);
