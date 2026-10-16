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
-- created_at is the lifecycle writer clock of the creating transaction, read after the write
-- lock was taken, in the lifecycle layout 'YYYY-MM-DD HH:MM:SS' (UTC) so that the window is a
-- text comparison. The CHECK is the four-part form of migrations 020 and 021: length() and GLOB
-- both stop at a NUL byte, so the byte length is checked too.
--
-- Existing assignments (no retroactive denial): one row is copied for each assignment that has
-- an owner, with the owner and the created_at it already has, so a creation made shortly before
-- this migration still counts in its window. Nothing is closed, hidden or changed. An assignment
-- with no owner (owner_user_id IS NULL) belongs to no account and gets no row. A created_at that
-- is not in the layout (the column is nullable and was never constrained) is recorded as the
-- epoch, which is outside every window.
--
-- Additive only: a new table and one index; quiz_assignments and users are not touched. Code
-- that predates this migration names its columns and ignores the table, so a rollback of the
-- code needs no schema change.
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

INSERT INTO assignment_creations (owner_user_id, assignment_id, created_at)
SELECT owner_user_id, id,
       CASE WHEN typeof(created_at) = 'text'
                 AND length(created_at) = 19
                 AND length(CAST(created_at AS BLOB)) = 19
                 AND created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]'
            THEN created_at
            ELSE '1970-01-01 00:00:00' END
FROM quiz_assignments
WHERE owner_user_id IS NOT NULL
ORDER BY id;
