PRAGMA foreign_keys = ON;

-- Per-identifier login throttle (Accounts PR 4). Operational state: never exported to
-- data/canonical. identifier_hash is HMAC-SHA256(SECRET_KEY, normalized email) in hex,
-- so an attempted email is never stored in clear.
CREATE TABLE IF NOT EXISTS login_throttle (
    identifier_hash TEXT PRIMARY KEY
        CHECK (length(identifier_hash) = 64),
    failure_count INTEGER NOT NULL DEFAULT 0
        CHECK (failure_count >= 0),
    window_started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    blocked_until TEXT
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_login_throttle_window
    ON login_throttle(window_started_at);
