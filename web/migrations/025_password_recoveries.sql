PRAGMA foreign_keys = ON;

-- Password recovery scope 8.1: storage and operations only. No production writer
-- creates recoveries. Runtime data, never canonical. Additive: no existing row
-- changes; legacy sends (including purpose-less rows) keep recovery_id NULL.
CREATE TABLE password_recoveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    binding_hash TEXT NOT NULL UNIQUE
        CHECK (typeof(binding_hash) = 'text' AND length(binding_hash) = 64
               AND length(CAST(binding_hash AS BLOB)) = 64
               AND binding_hash NOT GLOB '*[^0-9a-f]*'),
    state TEXT NOT NULL CHECK (state IN ('pending', 'redeemed', 'void', 'expired')),
    is_shadow INTEGER NOT NULL CHECK (is_shadow IN (0, 1)),
    email TEXT,
    user_id INTEGER REFERENCES users(id),
    session_key_digest TEXT
        CHECK (session_key_digest IS NULL OR
               (typeof(session_key_digest) = 'text' AND length(session_key_digest) = 64
                AND length(CAST(session_key_digest AS BLOB)) = 64
                AND session_key_digest NOT GLOB '*[^0-9a-f]*')),
    code_hash TEXT
        CHECK (code_hash IS NULL OR
               (typeof(code_hash) = 'text' AND length(code_hash) = 64
                AND length(CAST(code_hash AS BLOB)) = 64
                AND code_hash NOT GLOB '*[^0-9a-f]*')),
    code_expires_at TEXT,
    code_send_id INTEGER,
    code_attempts INTEGER NOT NULL DEFAULT 0
        CHECK (typeof(code_attempts) = 'integer' AND code_attempts >= 0),
    send_count INTEGER NOT NULL DEFAULT 0
        CHECK (typeof(send_count) = 'integer' AND send_count >= 0),
    resend_after TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    closed_at TEXT,
    CHECK (state = 'pending' OR (email IS NULL AND user_id IS NULL
           AND session_key_digest IS NULL AND code_hash IS NULL AND code_expires_at IS NULL)),
    CHECK (state != 'pending' OR email IS NOT NULL),
    CHECK (state != 'pending'
           OR (is_shadow = 1 AND user_id IS NULL AND session_key_digest IS NULL)
           OR (is_shadow = 0 AND user_id IS NOT NULL AND session_key_digest IS NOT NULL))
);
CREATE INDEX idx_password_recoveries_state ON password_recoveries(state, expires_at);

-- No FK: final recovery rows are deleted before their send history. At most one
-- purpose, deliberately not exactly one (supported legacy ledger inserts).
ALTER TABLE registration_sends ADD COLUMN recovery_id INTEGER
    CHECK (recovery_id IS NULL
           OR (typeof(recovery_id) = 'integer' AND recovery_id > 0 AND pending_id IS NULL));
CREATE INDEX idx_registration_sends_recovery
    ON registration_sends(recovery_id) WHERE recovery_id IS NOT NULL;
