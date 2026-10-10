PRAGMA foreign_keys = ON;

-- External sign-in identities (docs/ACCOUNTS_DESIGN.md, sections 5 and 13). Runtime
-- account state: never exported to data/canonical.
--
-- Storage only. Nothing reads or writes this table yet: provider sign-in (Google and
-- Microsoft OIDC), linking and registration arrive with later PRs. The schema is the
-- approved one of section 5, unchanged; users is not touched and no row is written.
--
-- An identity is the exact pair (issuer, subject) of a verified ID token. Both are
-- compared byte for byte (SQLite's default BINARY collation): no case folding, no
-- trimming. UNIQUE (issuer, subject) maps one identity to at most one user; a user
-- may have several identities. The same subject under another issuer is another
-- identity.
--
-- email_at_link is what the provider reported when the identity was linked. It is
-- metadata only, deliberately neither unique nor indexed: an email never selects a
-- user, signs anyone in or links anything.
--
-- user_id has no ON DELETE action: an account that has identities cannot be deleted
-- from under them, and no identity is removed or moved to another account by a
-- cascade. That the keys are well-formed (never empty) is enforced by the storage
-- service (web/services/identities.py), not by SQL constraints.
CREATE TABLE IF NOT EXISTS user_identities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    issuer TEXT NOT NULL,
    subject TEXT NOT NULL,
    provider TEXT NOT NULL CHECK (provider IN ('google', 'microsoft')),
    email_at_link TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_login_at TEXT,
    UNIQUE (issuer, subject)
);

CREATE INDEX IF NOT EXISTS idx_user_identities_user ON user_identities(user_id);
