-- 002_curriculum_section_provenance.sql
-- Adds provenance fields for curriculum section source tracking.
-- NOTE:
-- SQLite does not support ALTER TABLE ADD COLUMN IF NOT EXISTS
-- in all versions, so this migration is documentary/replay reference.
-- Existing project DB was updated using an idempotent Python migration.

ALTER TABLE curriculum_sections ADD COLUMN source_url TEXT;
ALTER TABLE curriculum_sections ADD COLUMN source_title TEXT;
ALTER TABLE curriculum_sections ADD COLUMN source_authority TEXT;
ALTER TABLE curriculum_sections ADD COLUMN dzi_relevance_verified INTEGER NOT NULL DEFAULT 0;
ALTER TABLE curriculum_sections ADD COLUMN dzi_relevance_notes TEXT;
