PRAGMA foreign_keys = ON;

-- Explicit topic order within a section. NULL keeps the legacy order (title, id),
-- so existing rows are unchanged.
ALTER TABLE topic_section_assignments
    ADD COLUMN display_order INTEGER NULL;
