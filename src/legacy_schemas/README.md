# Legacy Schema Snapshots

This directory contains historical SQLite schema snapshots from earlier
LearnPilot/DZI Generator development:

- `schema.sql`
- `schema_v2.sql`
- `schema_v3.sql`

These files are not the source of truth for current schema changes.
Use `web/migrations/` for live migrations and new schema work.

Do not add new schema changes here, and do not use these snapshots to migrate
the current application database. They are kept only as historical references.
