PRAGMA foreign_keys = ON;

-- Assignment creation ledger (docs/reviews/EMAIL_REGISTRATION_PR7_SCOPE.md; registration PR 7,
-- decision D5). Runtime state: never exported to data/canonical.
--
-- One row per assignment an account created, written by the same statement sequence and the
-- same writer transaction that inserts the assignment (web/services/assignment_quota.py,
-- called from quiz_create_assignment). It is what the "20 creations in a rolling 24 hours"
-- limit counts. The "10 open assignments" limit counts quiz_assignments itself, because open
-- is a state of the assignment and closing one must free the slot.
--
-- Why a table of its own and not quiz_assignments.created_at: a creation must stay counted
-- after the assignment is closed, and after the assignment row is deleted (nothing deletes
-- one today; the retention cleanup of docs/ASSIGNMENT_LIFECYCLE_DESIGN.md will). So
-- assignment_id is a plain integer with NO foreign key: no deletion of an assignment, and no
-- cascade, can touch this table, and the UNIQUE constraint keeps one assignment from being
-- counted twice. owner_user_id is RESTRICT: an account that has history is never deleted
-- (accounts are disabled, not deleted, in any case).
--
-- created_at is the lifecycle writer clock of the creating transaction, read after the write lock
-- was taken (or, for a reconciled row, the normalized age of the assignment), in the lifecycle layout
-- 'YYYY-MM-DD HH:MM:SS' (UTC) so that the window is a text comparison. The CHECK is the four-part form of migrations 020 and 021: length() and GLOB
-- both stop at a NUL byte, so the byte length is checked too.
--
-- Existing assignments (no retroactive denial) and history this table does not have: the migration
-- writes NO rows. Every owned assignment that has no ledger row is recorded, from the created_at it
-- carries, by the first admission of its owner's next creation (web/services/assignment_quota.py,
-- reconcile), inside the same writer transaction and by one set of timestamp rules
-- (normalize_legacy_timestamp, written in Python only: a copy in SQL would be a second set of rules).
-- That also covers assignments created later by code that does not know this table (a rollback or
-- recovery build): they are recorded at the next admission, and an account whose data cannot be placed
-- with confidence (NULL, malformed or implausible created_at) is refused until an operator corrects it.
-- An assignment row deleted before it was ever recorded cannot be reconstructed. Nothing is closed,
-- hidden or changed. An assignment with no owner (owner_user_id IS NULL) belongs to no account.
--
-- Additive only: a new table and one index; quiz_assignments and users are not touched. Code that
-- predates this migration names its columns and still runs against the schema, but it does not
-- record creations and does not enforce the limits: a build that keeps this migration must keep
-- web/services/assignment_quota.py and its call in quiz_create_assignment (docs/DEPLOYMENT.md, section 36).
CREATE TABLE assignment_creations (
    id INTEGER PRIMARY KEY,
    owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    assignment_id INTEGER NOT NULL UNIQUE
        CHECK (typeof(assignment_id) = 'integer' AND assignment_id > 0),
    created_at TEXT NOT NULL
        CHECK (typeof(created_at) = 'text'
               AND length(created_at) = 19
               AND length(CAST(created_at AS BLOB)) = 19
               AND created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]')
);

CREATE INDEX idx_assignment_creations_owner
    ON assignment_creations(owner_user_id, created_at);
