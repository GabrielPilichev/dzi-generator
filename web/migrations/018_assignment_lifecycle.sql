PRAGMA foreign_keys = ON;

-- Assignment lifecycle (docs/ASSIGNMENT_LIFECYCLE_DESIGN.md, migration A, PR #326).
-- Runtime state: never exported to data/canonical.
--
-- Additive on purpose: quiz_assignments is not rebuilt, all eight columns are
-- nullable without a default and nothing is backfilled. A row that exists before
-- this migration keeps NULL in every one of them and therefore stays OPEN with no
-- deadline, no zone and no hold, exactly as it behaved before.
--
--   deadline_at            exclusive cutoff, UTC 'YYYY-MM-DD HH:MM:SS'; NULL = no deadline
--   display_timezone       IANA name the teacher selected; display and audit only
--   closed_at              instant of the manual close, as sampled; write-once, terminal
--   closed_by_user_id      who closed it; always the server-side principal
--   expiry_observed_at     first writer transaction that saw the deadline passed; terminal
--   submissions_closed_at  first writer transaction that saw the grace window ended; terminal
--   retention_base_at      retention floor, raised only by operator tooling
--   lifecycle_hold         set of unresolved hold reasons; any non-NULL value holds
--
-- Write-once markers, "deadline in the future", the 180-day maximum and the content
-- of lifecycle_hold are enforced by the application's validated, conditional writes
-- (web/services/lifecycle.py), not by SQL constraints.
ALTER TABLE quiz_assignments ADD COLUMN deadline_at TEXT;
ALTER TABLE quiz_assignments ADD COLUMN display_timezone TEXT;
ALTER TABLE quiz_assignments ADD COLUMN closed_at TEXT;
ALTER TABLE quiz_assignments ADD COLUMN closed_by_user_id INTEGER REFERENCES users(id);
ALTER TABLE quiz_assignments ADD COLUMN expiry_observed_at TEXT;
ALTER TABLE quiz_assignments ADD COLUMN submissions_closed_at TEXT;
ALTER TABLE quiz_assignments ADD COLUMN retention_base_at TEXT;
ALTER TABLE quiz_assignments ADD COLUMN lifecycle_hold TEXT;

-- Clock-health state shared by every process. CHECK (id = 1) allows at most one
-- row; it does not make the row exist, so the application checks for it at
-- start-up and in every lifecycle transaction and fails closed without it.
--
-- The seed is a fixed sentinel, not CURRENT_TIMESTAMP: nothing has been observed
-- yet, so there is nothing a clock regression could undo, and the result of the
-- migration does not depend on the moment it ran. The first lifecycle write
-- raises the mark to real time. ref_* is the boot anchor (NULL until then).
CREATE TABLE IF NOT EXISTS lifecycle_clock (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    high_water_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'ok' CHECK (state IN ('ok', 'suspect')),
    state_reason TEXT,
    state_changed_at TEXT,
    ref_wall_at TEXT,
    ref_boot_id TEXT,
    ref_boottime_ms INTEGER
);
INSERT OR IGNORE INTO lifecycle_clock (id, high_water_at) VALUES (1, '1970-01-01 00:00:00');
