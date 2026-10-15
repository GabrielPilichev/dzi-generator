PRAGMA foreign_keys = ON;

-- The "profile required" mark of an account (docs/reviews/EMAIL_REGISTRATION_DESIGN.md,
-- sections 6.1, 7.3 and 7.4, decision D11; registration PR 6). Runtime state: users is
-- never exported to data/canonical.
--
-- users.profile_required_at is NULL when no profile is asked of the account, and the time
-- the mark was written otherwise. The application writes it in exactly one place: in the
-- transaction that creates a student through Google sign-in, and only while the build's
-- profile gate (the profile service's PRODUCTION_ACTIVATION, or its development setting)
-- is on. A mark never grants, removes or reinterprets a role, a credential, a session or
-- a profile, and it decides nothing about access to data: it can only send a student
-- without a profile to the profile form before a NEW quiz attempt is created. The
-- reading rule is profiles.needs_profile.
--
-- Existing rows: the column is added NULL and no row is written. Every account that
-- exists when this migration runs is unmarked, which makes "nothing retroactive" a
-- property of the data and not of a condition in code. No account is backfilled and no
-- operator command marks an existing one. A mark written while the gate was on stays
-- stored, and is ignored while the gate is off.
--
-- One nullable TEXT column, added in place: no table is rebuilt, no index is created and
-- no default or constraint is attached. Code that predates this migration names its
-- columns and runs unchanged against the new schema, so a rollback of the code needs no
-- schema change.
ALTER TABLE users ADD COLUMN profile_required_at TEXT;
