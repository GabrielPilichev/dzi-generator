PRAGMA foreign_keys = ON;

-- Admin second factor (Accounts PR T). Runtime account state: never exported to
-- data/canonical. 014 and 015 are reserved for assignment ownership and the phase-2
-- quiz-attempt rebuild.
--
-- secret_ciphertext is the base32 TOTP secret encrypted with Fernet under
-- DZI_TOTP_KEY; the plaintext secret is never stored. A credential counts only once
-- confirmed_at is set (the operator verified a code from the authenticator).
-- last_used_step is the highest TOTP time step accepted so far: a step at or below it
-- is refused, so a code cannot be replayed. last_verified_at is the application-clock
-- time of the latest completed second factor; a pending sign-in started before it is
-- spent.
CREATE TABLE IF NOT EXISTS totp_credentials (
    user_id INTEGER PRIMARY KEY REFERENCES users(id),
    secret_ciphertext TEXT NOT NULL
        CHECK (length(secret_ciphertext) > 0),
    confirmed_at TEXT,
    last_used_step INTEGER
        CHECK (last_used_step IS NULL OR last_used_step >= 0),
    last_verified_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Single-use recovery codes of a credential, stored only as scrypt hashes. They are
-- removed with their credential.
CREATE TABLE IF NOT EXISTS totp_recovery_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL
        REFERENCES totp_credentials(user_id) ON DELETE CASCADE,
    code_hash TEXT NOT NULL
        CHECK (length(code_hash) > 0),
    used_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_totp_recovery_codes_user
    ON totp_recovery_codes(user_id, used_at);

-- Admin sessions issued before this migration were never checked against a second
-- factor. Rotating the admin session keys ends them; the next admin session can only
-- come from a completed TOTP sign-in (or the CLI, which rotates the key again).
UPDATE users
SET session_key = lower(hex(randomblob(32))),
    updated_at = CURRENT_TIMESTAMP
WHERE role = 'admin';
