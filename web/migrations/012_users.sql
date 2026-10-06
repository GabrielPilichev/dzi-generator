PRAGMA foreign_keys = ON;

-- Local accounts (Accounts PR 1). Runtime state: never exported to data/canonical.
-- email is stored normalized (NFKC, trimmed, lower-case); the CHECK is only the basic
-- invariant, the accounts service applies the stricter validation.
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE
        CHECK (email IS NULL OR (length(email) BETWEEN 3 AND 254
               AND email = lower(trim(email)) AND instr(email, '@') > 1)),
    display_name TEXT NOT NULL
        CHECK (length(trim(display_name)) BETWEEN 2 AND 80),
    role TEXT NOT NULL DEFAULT 'student'
        CHECK (role IN ('student', 'teacher', 'admin')),
    password_hash TEXT
        CHECK (password_hash IS NULL OR (length(password_hash) > 0 AND email IS NOT NULL)),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    session_key TEXT NOT NULL CHECK (length(session_key) >= 32),
    password_changed_at TEXT,
    last_login_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
