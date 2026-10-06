# LearnPilot Script Inventory

This inventory covers tracked files under `src/`. It intentionally excludes local
artifacts that may appear in a working tree, such as `.DS_Store` files or
`src/data/`.

Status is conservative:

- **Active / currently used** means the file is imported by the app or tests, or
  is referenced by current review/import documentation. Some active scripts still
  write to the database or filesystem and should only be run through their
  documented workflow.
- **Review before use** means the script may still be useful, but it is
  environment-specific, mutating, AI-assisted, scraper-related, or not clearly
  part of the current app path.
- **Historical / one-shot** means the file appears to be an old migration,
  bootstrap, cleanup, schema snapshot, or superseded parser/generator. Keep it
  for archaeology unless a separate cleanup PR proves it is safe to remove.

## Active / currently used

| File | Purpose |
| --- | --- |
| `src/practical_uploads.py` | Runtime safety helpers for practical-task student uploads; imported by `web/app.py` and covered by tests. |
| `src/manage_migrations.py` | Numbered SQLite migration planner/runner with dry-run and baseline support (`--baseline-dry-run`/`--baseline-apply` adopt `000_baseline.sql` on existing databases); covered by migration tests. It is also the only production schema-change step (`docs/DEPLOYMENT.md`). |
| `src/schema_introspection.py` | Read-only SQLite table-shape helpers (columns, keys, indexes, CHECKs); used by baseline detection in `manage_migrations.py` and the schema-parity test. |
| `src/backup_sqlite.py` | WAL-safe online SQLite backup with `PRAGMA quick_check` verification; explicit source/destination, never overwrites or writes inside the checkout. Covered by `tests/test_backup_sqlite.py`. |
| `src/import_subject_structure.py` | Imports a reviewed curriculum structure (ordered sections and topics, no questions) for a registered subject; dry run by default, atomic `--apply`, rejects conflicts. See `docs/SUBJECT_STRUCTURE_IMPORT.md`; covered by `tests/test_import_subject_structure.py`. |
| `src/question_collection_limits.py` | Hard limits and absolute ceilings of one question-collection run, plus the page/candidate/runtime budget counter; no network or database access. See `docs/QUESTION_COLLECTION_PIPELINE.md`; covered by `tests/test_question_candidates.py`. |
| `src/question_candidates.py` | Question-candidate dataset format: validation, source policy, review states, duplicate and quality flags, optional strictly read-only database checks. Never writes a database. |
| `src/start_question_collection.py` | Writes the empty, bounded candidate dataset for a collection run from explicit subject, grade, topics and limits; creates one JSON file only. |
| `src/validate_question_candidates.py` | Read-only validator for a candidate dataset; optional `--db` (read-only) and `--annotate` copy with flags. |
| `src/export_approved_question_candidates.py` | Converts only approved candidates into reviewed import batches plus a provenance file; JSON output only, the reviewed importer stays the database boundary. Covered by `tests/test_export_approved_question_candidates.py`. |
| `src/validate_question_batch.py` | Read-only dry-run validator for prepared question batch JSON files before import. |
| `src/import_dzi_questions_json.py` | Imports reviewed official DZI Part 1 question JSON into the current schema; used by the validator path. |
| `src/validate_practical_task_batch.py` | Dry-run validator for reviewed DZI practical-task batch JSON and resource references. |
| `src/import_practical_task_resources.py` | Imports reviewed practical-task resource metadata after validation and schema checks. |
| `src/audit_dzi_state.py` | Read-only readiness audit for official DZI sources and Part 1 import state. |
| `src/audit_exam_provenance.py` | Read-only audit for duplicate, stale, or ambiguous DZI exam provenance. |
| `src/audit_dzi_assets.py` | Read-only per-task audit of DZI visual asset coverage. |
| `src/audit_rendered_question_images.py` | Read-only audit that checks linked question images and template rendering expectations. |
| `src/audit_dzi_question_image_assets.py` | Read-only QA audit for DZI question image asset files, dimensions, and mapping status. |
| `src/audit_question_image_dependencies.py` | Read-only audit that finds questions likely to need visual assets and can produce review worksheets. |
| `src/audit_open_question_readiness.py` | Read-only audit for future auto-gradable open/fill-in question readiness. |
| `src/audit_open_answer_variants.py` | Read-only audit for accepted-answer variant coverage on open/fill-in questions. |
| `src/map_confirmed_dzi_image_questions.py` | Maps manually confirmed DZI image-dependent task numbers to database question IDs. |
| `src/validate_confirmed_dzi_image_mappings.py` | Validates confirmed DZI image-question mapping JSON without database writes. |
| `src/validate_confirmed_dzi_image_files.py` | Checks expected PNG files for confirmed DZI image-question mappings. |
| `src/validate_confirmed_dzi_asset_files.py` | Compatibility entry point for the confirmed DZI image file validator. |
| `src/extract_dzi_image_candidates.py` | Plans/extracts candidate PNG crops from official PDFs for confirmed image-dependent questions. |
| `src/apply_confirmed_dzi_image_mappings.py` | Applies confirmed DZI image mappings to asset tables after validation; supports a read-only `--check` first. |
| `src/validate_question_image_review.py` | Validates manual question-image review Markdown files and asset filename decisions. |

## Review before use

| File | Purpose |
| --- | --- |
| `src/agents/__init__.py` | Package marker for local AI/Ollama helper modules. |
| `src/agents/ollama_client.py` | Minimal Ollama HTTP client used by AI-assisted classification/generation tools. |
| `src/build_worksheet.py` | Generates topical DOCX worksheets from approved questions. |
| `src/generate_dzi_training_set.py` | Builds DZI training question sets from configured class mixes and approved pools. |
| `src/generate_questions.py` | BgGPT-assisted multiple-choice question generator with dry-run and audit logging. |
| `src/predict_answers.py` | BgGPT-assisted answer predictor for unanswered multiple-choice imports. |
| `src/reclassify_topics.py` | Hybrid embedding/BgGPT topic reclassifier that can update topic assignments after confidence checks. |
| `src/topic_classifier.py` | Older BgGPT classifier for assigning orphan questions to known topic slugs. |
| `src/test_bggpt.py` | Local smoke test for BgGPT/Ollama suitability and JSON output behavior. |
| `src/review_export.py` | Exports pending AI-generated questions to Obsidian Markdown for human review. |
| `src/review_import.py` | Imports human review decisions from Obsidian Markdown back into the database. |
| `src/sync_vault.py` | Synchronizes Obsidian vault Markdown/frontmatter into SQLite curriculum and note tables. |
| `src/watch_vault.py` | Watches the vault and debounces calls to `sync_vault.py`; depends on local watchdog setup. |
| `src/install_watcher.sh` | Installs, uninstalls, and checks the macOS launchd vault watcher. |
| `src/dzi-generator-watcher.plist` | launchd template used by `install_watcher.sh` to run the vault watcher. |
| `src/generate_topic_stubs.py` | Generates Obsidian topic stub notes for curriculum topics missing note files. |
| `src/report_links.py` | Query/report utility for curriculum graph links, generated exams, topic coverage, and orphan questions. |
| `src/audit_stub_topics.py` | Audits approved-empty curriculum topics and can write a Markdown report. |
| `src/audit_empty_sections_fill_plan.py` | Reports curriculum sections with no approved questions and suggests fill priorities. |
| `src/audit_cross_grade_mappings.py` | Reports suspicious mappings where topics from later grades appear in earlier-grade sections. |
| `src/audit_dzi_relevance.py` | Prints section-level DZI relevance/provenance fields for manual review. |
| `src/audit_dzi_training_pool.py` | Writes DZI training-pool coverage reports by class and section. |
| `src/export_dzi_relevance_audit.py` | Exports DZI relevance/provenance audit data to CSV and Markdown. |
| `src/export_filled_section_questions_for_review.py` | Exports questions from selected filled sections for manual review. |
| `src/export_to_vault.py` | Exports generated exams or custom question sets back into the Obsidian vault. |
| `src/fill_structural_sections.py` | Suggests or applies topic assignments for structural review/summary/project sections; inspect arguments before use. |
| `src/inventory_dzi_sources.py` | Inventories official DZI source files and can register source/assets metadata. |
| `src/import_dzi_exam_skeleton.py` | Creates an official DZI exam row and task skeleton from the active blueprint. |
| `src/seed_dzi_blueprint.py` | Seeds the DZI IT profiled-preparation blueprint and task slots. |
| `src/import_classroom_tests.py` | Imports questions from a merged classroom-test PDF as AI/pending-review rows. |
| `src/run_tunnel_dev.py` | Starts the local Flask app behind a trusted Cloudflare tunnel proxy when required env vars are set. |
| `src/parsers/__init__.py` | Public exports for the parser package. |
| `src/parsers/base.py` | Dataclasses and base class for exam parsers. |
| `src/parsers/registry.py` | Parser registry and format auto-detection helper. |
| `src/parsers/dzi_it_modern.py` | Parser implementation for modern DZI IT PDF formats. |
| `src/parsers/dzi_it_legacy.py` | Parser implementation/stub for legacy pre-2023 DZI IT PDF formats. |
| `src/parsers/parse_pdf.py` | CLI for parser-registry based PDF parsing into the database. |
| `src/scrape/__init__.py` | Package marker for scraping helpers. |
| `src/scrape/http.py` | Polite HTTP fetcher with rate limiting, retries, and a declared user agent. |
| `src/scrape/mon.py` | Scraper for official MON DZI material pages. |
| `src/scrape/zamatura.py` | Scraper for zamatura.eu DZI PDF listings. |
| `src/scrape/run_scraper.py` | CLI orchestrator for MON/zamatura scraping and optional post-download parsing. |

## Historical / one-shot

| File | Purpose |
| --- | --- |
| `src/legacy_schemas/README.md` | Explains that legacy schema snapshots are historical and that `web/migrations/` is the source of truth. |
| `src/legacy_schemas/schema.sql` | Original v1 SQLite schema snapshot. |
| `src/legacy_schemas/schema_v2.sql` | v2 schema snapshot introducing exams/source metadata. |
| `src/legacy_schemas/schema_v3.sql` | v3 schema snapshot introducing curriculum and vault sync tables. |
| `src/migrate_to_v2.py` | One-off migration from v1 to v2 schema with backup/copy behavior. |
| `src/migrate_v2_to_v3.py` | One-off migration from v2 to v3 schema with backup and curriculum tables. |
| `src/parse.py` | Original DZI PDF parser for tasks 1-25. |
| `src/parse_v2.py` | Second-generation DZI PDF parser, superseded by the parser package and later JSON import workflow. |
| `src/build_exam.py` | Early DOCX DZI exam generator from database question pools. |
| `src/setup_vault.py` | Initial Obsidian vault bootstrap script for folders, MOCs, templates, and home page. |
| `src/populate_curriculum.py` | One-off population of Obsidian MOCs/topics from IT curriculum sources. |
| `src/expand_topics.py` | One-off addition of hardware/legal and related topic stubs for orphan coverage. |
| `src/expand_topics_v2.py` | Second one-off topic-stub expansion for remaining orphan patterns. |
| `src/link_questions_to_topics.py` | One-shot linker from legacy topic text to curriculum topic IDs after vault sync. |
| `src/apply_curriculum_section_provenance.py` | One-off schema/data patch adding provenance fields to curriculum sections. |
| `src/cleanup_sumif_renamed.py` | One-off cleanup for the duplicate `sumif-renamed` topic and related vault note. |
| `src/cleanup_legacy_dzi_question_slugs.py` | Conservative cleanup tool for known legacy DZI `source_exam` slugs; keep for audit trail. |
