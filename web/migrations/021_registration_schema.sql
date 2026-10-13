PRAGMA foreign_keys = ON;

-- Verified email/password registration, storage only (docs/reviews/EMAIL_REGISTRATION_DESIGN.md,
-- sections 6.1 and 9, PR 1). Every table is runtime state: never exported to
-- data/canonical (src/canonical_tables.py).
--
-- Nothing reads or writes any of this yet except the profile service
-- (web/services/profiles.py), which has no caller. There is no route, no mail and no
-- activation: the registration gates stay off. Later PRs add the services that fill
-- pending_registrations, registration_sends and registration_control.
--
-- Not here, by design: users.profile_required_at belongs to PR 6 (D11), and a
-- needs-profile mark would be meaningless before the profile gate exists. Teacher
-- quotas (PR 7) and password recovery (PR 8) add their own migrations.
--
-- One change to an existing table: users gains email_verified_at, NULL by default.
-- Every account that exists when this migration runs has it NULL ("never verified
-- here"), which is what makes "nothing retroactive" a property of the data. No
-- existing row is written and no table is rebuilt. Code that predates this migration
-- names its columns, so it runs unchanged against the new schema.
ALTER TABLE users ADD COLUMN email_verified_at TEXT;

-- What a person declared at registration, apart from users so a profile can later be
-- erased while the users row that attempts and assignments reference stays. This is a
-- declaration, never authorization: users.role is the only authority and no access
-- decision reads this table. 'admin' cannot be stored as an account_type.
--
-- grade is the school grade only (D1), 8 to 12, with the date it was set (D2); no
-- automatic advancement. typeof() is checked because an INTEGER column keeps a value
-- such as 9.5 as a real, which BETWEEN would accept.
CREATE TABLE user_profiles (
    user_id INTEGER PRIMARY KEY REFERENCES users(id),
    account_type TEXT NOT NULL CHECK (account_type IN ('student', 'teacher')),
    first_name TEXT NOT NULL CHECK (length(first_name) BETWEEN 1 AND 39),
    last_name TEXT NOT NULL CHECK (length(last_name) BETWEEN 1 AND 39),
    grade INTEGER CHECK (grade IS NULL OR (typeof(grade) = 'integer' AND grade BETWEEN 8 AND 12)),
    grade_set_at TEXT,
    school_name TEXT CHECK (school_name IS NULL OR length(school_name) BETWEEN 3 AND 120),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK ((account_type = 'student' AND grade IS NOT NULL AND grade_set_at IS NOT NULL
            AND school_name IS NULL)
        OR (account_type = 'teacher' AND grade IS NULL AND grade_set_at IS NULL
            AND school_name IS NOT NULL))
);

-- One to three subjects of a teacher profile (D3). subject_id is an id of
-- src.subject_registry, or NULL for „Друг предмет“ (label then holds the person's text).
-- That an id exists in the registry is checked by the profile service: the registry is
-- Python data and SQL cannot know it. The partial unique indexes keep a subject, and the
-- one free-text entry, from being stored twice for a profile.
CREATE TABLE teacher_subjects (
    user_id INTEGER NOT NULL REFERENCES user_profiles(user_id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK (typeof(position) = 'integer' AND position BETWEEN 1 AND 3),
    subject_id TEXT CHECK (subject_id IS NULL OR length(subject_id) BETWEEN 1 AND 64),
    label TEXT NOT NULL CHECK (length(label) BETWEEN 2 AND 60),
    PRIMARY KEY (user_id, position)
);

CREATE UNIQUE INDEX idx_teacher_subjects_subject
    ON teacher_subjects(user_id, subject_id) WHERE subject_id IS NOT NULL;
CREATE UNIQUE INDEX idx_teacher_subjects_other
    ON teacher_subjects(user_id) WHERE subject_id IS NULL;

-- A registration that is not an account (design, 2.1 and 2.4). Not a users row: it has
-- no principal and no privilege, reserves no email, and any number may exist for one
-- address. It holds personal data and a credential hash while it is pending, so
-- every column that carries either is NULL in any other state, which the table CHECK
-- enforces column by column.
--
-- The keyed hashes (binding_hash, code_hash) are 64 bytes of lower-case hexadecimal;
-- a code or token is never stored. The same four conditions as migration 020 are
-- needed together: length() and GLOB both stop at a NUL byte, and
-- length(CAST(.. AS BLOB)) alone counts bytes but lets GLOB miss what follows a NUL.
-- password_hash is a Werkzeug scrypt hash of the exact password and is not covered by
-- that rule: the accounts service decides whether a hash is usable.
CREATE TABLE pending_registrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    binding_hash TEXT NOT NULL UNIQUE
        CHECK (typeof(binding_hash) = 'text'
               AND length(binding_hash) = 64
               AND length(CAST(binding_hash AS BLOB)) = 64
               AND binding_hash NOT GLOB '*[^0-9a-f]*'),
    account_type TEXT NOT NULL CHECK (account_type IN ('student', 'teacher')),
    state TEXT NOT NULL CHECK (state IN ('pending', 'activated', 'void', 'expired')),
    is_shadow INTEGER NOT NULL CHECK (is_shadow IN (0, 1)),
    email TEXT,
    first_name TEXT,
    last_name TEXT,
    grade INTEGER,
    school_name TEXT,
    subjects_json TEXT,
    password_hash TEXT,
    code_hash TEXT
        CHECK (code_hash IS NULL
               OR (typeof(code_hash) = 'text'
                   AND length(code_hash) = 64
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
    CHECK (state = 'pending' OR (email IS NULL AND first_name IS NULL AND last_name IS NULL
        AND grade IS NULL AND school_name IS NULL AND subjects_json IS NULL
        AND password_hash IS NULL AND code_hash IS NULL AND code_expires_at IS NULL))
);

CREATE INDEX idx_pending_registrations_state ON pending_registrations(state, expires_at);

-- The send ledger: one row per issuance, code or notice, keyed by the address's keyed
-- hash (no address is stored). id is the send generation and is never reused, hence
-- AUTOINCREMENT. pending_id has no foreign key: the pending row is deleted first.
CREATE TABLE registration_sends (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pending_id INTEGER,
    email_hash TEXT NOT NULL
        CHECK (typeof(email_hash) = 'text'
               AND length(email_hash) = 64
               AND length(CAST(email_hash AS BLOB)) = 64
               AND email_hash NOT GLOB '*[^0-9a-f]*'),
    kind TEXT NOT NULL CHECK (kind IN ('code', 'notice')),
    attempted_at TEXT NOT NULL,
    outcome TEXT NOT NULL DEFAULT 'started'
        CHECK (outcome IN ('started', 'accepted', 'not_sent', 'refused', 'unknown')),
    settled_at TEXT
);

CREATE INDEX idx_registration_sends_email ON registration_sends(email_hash, attempted_at);
CREATE INDEX idx_registration_sends_time ON registration_sends(attempted_at);

-- At most one row, absent until the first issuance or cleanup: an absent row means no
-- hold and no high-water mark. hold_reason only ever names a restore.
CREATE TABLE registration_control (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    high_water_at TEXT NOT NULL,
    hold_reason TEXT CHECK (hold_reason IS NULL OR hold_reason IN ('restore')),
    hold_set_at TEXT
);
