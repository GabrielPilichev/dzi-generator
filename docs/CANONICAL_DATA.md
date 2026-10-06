# Canonical content (G1)

`data/canonical/` is the **database reconstruction representation** of LearnPilot's
durable content: curriculum, questions, options and the DZI structures. It exists so
that the content in `data/questions.db` can be rebuilt from tracked, reviewable files
**with stable ids**. The reviewed structure/batch JSON files stay the *human authoring*
inputs; they are not an id authority.

Status: G1 only. `data/questions.db` is **still tracked** and still what the tests and
the local app copy. Replacing it (G2) and untracking it (H) are separate PRs
(`docs/REFACTOR_ACCOUNTS_ROADMAP.md`).

## Classification

Every table is classified exactly once in `src/canonical_tables.py`
(`TABLE_CLASSIFICATION`); a table without an entry fails
`tests/test_canonical_content.py`.

| Class | Meaning | Tables |
| --- | --- | --- |
| canonical | durable content, rebuilt from tracked files | `curricula`, `curriculum_*`, `topic_*`, `lessons`, `questions`, `multiple_choice_options`, `fill_in_subquestions`, `question_topic_assignments`, `exams`, `exam_tasks`, `exam_task_questions`, `official_exam_sources`, `practical_tasks`, `practical_task_resources`, `assets`, `asset_links`, `dzi_blueprints`, `dzi_blueprint_slots`, `assessment_events`, `assessment_event_sections` (27 tables) |
| runtime | user/session state; a content rebuild never writes or replaces it | `quiz_assignments`, `quiz_attempts`, `quiz_answers`, `quiz_text_answers`, `practical_submissions`, `practical_submission_files` |
| operational | caches, vault mirror, logs, bookkeeping | `obsidian_notes`, `note_question_links`, `generated_exams`, `scrape_log`, `schema_migrations`, `sqlite_sequence` |

The seed's smoke-test runtime rows (2 assignments, 4 attempts, 19 answers) and its
operational rows are **not** in the canonical data and are not deleted from the seed.
No `student_name` or other user data is exported.

## Source precedence (the one-time reconciliation)

| Content | Values | Ids |
| --- | --- | --- |
| Legacy (IT curriculum, 832 questions, DZI) | tracked seed | tracked seed |
| Physics/Biology (6 curricula, 20 sections, 53 topics, 846 questions, 3384 options) | tracked structures and reviewed batches, run through the real importers on a scratch copy of the seed | a read-only export of the author's local database (identity fields only), matched by natural key |

Natural keys: curriculum `(subject, grade, education_type)`, section/topic slug, question
`(source_exam, source_number)`, option `(question_id, option_letter)`. Position in a file
is never identity. `src/canonical_reconcile.py` stops on any missing, duplicate,
unexpected or conflicting match, on an id that would recycle a historical id, and when
the seed or the identity export is not the audited one. Production holds only the
seed-era rows, so it is not the authority for the newer ids.

Intentional differences from the local database (also in `manifest.json` → `sources`):

* `created_at` is `NULL` on the rows added from tracked inputs (topics, questions,
  topic/question assignments); the local timestamps are not tracked anywhere.
* `topic_section_assignments` and `question_topic_assignments` ids of those rows are
  derived: no foreign key or stored reference points at these tables, so they have no
  external identity. They are assigned consecutively above the seed sequence in
  ascending parent-id order, the order a sequential import produces.
* `sqlite_sequence` is `max(seed sequence, max id)`; the seed's high-water marks
  (ids of deleted historical rows) are preserved, so they are never reused.

The raw identity export is not tracked: the canonical files are its durable form.
`LEARNPILOT_G1_IDENTITY_MANIFEST=<path>` makes one test re-derive the dataset from it and
compare it byte for byte with the tracked files.

## Stable-id contract

Once an id enters canonical data it never changes and is never reused. Question ids are
embedded without foreign keys in `quiz_attempts.question_ids_json`,
`quiz_assignments.question_plan_json`, `quiz_answers.question_id`,
`generated_exams.question_ids`, `docs/reviews/confirmed_dzi_image_question_mappings.json`
(`db_question_id`) and tracked vault notes (`question_ids:`); `questions.topic_id` and
`asset_links.owner_id` are likewise FK-less. `src/canonical_parity.py` validates all of
them against the canonical ids.

New canonical rows must take an id above **both** the canonical maximum/sequence and the
target database's maximum/sequence (`canonical_tables.next_free_id`). Production must
eventually consume the explicit canonical ids rather than assign its own; G1 provides no
production mechanism.

## Format

`data/canonical/<table>.jsonl`, one file per canonical table: UTF-8, LF, final newline,
one compact JSON object per row, every column in schema order, explicit `null`,
`ensure_ascii=False`, rows in primary-key order, ids explicit. Integer and real values
keep their JSON form (`1` vs `1.0`). No DDL, no blobs; schema stays
`000_baseline` + `001`–`010` + later migrations.

`data/canonical/manifest.json`: format/version, schema migration level, the
classification and exclusions, the FK-topological load order, and per table the row
count, columns, primary key, `max_id`, expected `sqlite_sequence`, natural key and
`sha256`, plus the `sources` reconciliation record. The hash is over `line + "\n"` of
every normalized row; exporter, manifest, loader checks and comparator all use
`canonical_tables.iter_table_lines`/`hash_lines`.

## Tools

| Command | Purpose |
| --- | --- |
| `python3 -m src.canonical_export --db PATH --out DIR` | export a database's canonical tables to an empty directory (never the tracked one) |
| `python3 -m src.canonical_export --db PATH --replace-canonical` | replace `data/canonical` after the identity checks |
| `python3 -m src.canonical_export --check` | verify the tracked files against the manifest, classification and schema |
| `python3 -m src.canonical_reconcile --identity-manifest PATH --out DIR` | the one-time construction described above |

`--replace-canonical` refuses (and leaves the files untouched) when it would remove a
row, drop a row count, change the id of a natural key or reassign an id, change an
option letter, lower a sequence, or include a runtime/operational table. New rows and
value changes are allowed. There is deliberately no override flag: a reviewed retirement
of an id needs its own change.

`src/canonical_load.py` is the minimal explicit-ID loader that proves the round trip
(`rebuild_for_parity`: empty database → migrations 000–010 → canonical load). It loads in
FK order inside one transaction with `foreign_keys=ON`, fails on conflicting existing
rows, writes only canonical tables and `sqlite_sequence`, never lowers a sequence and
never remaps an id. It is not the G2 developer builder.

`src/canonical_parity.py` compares a database with the canonical files: schema against a
freshly migrated database, row counts, normalized hashes, explicit ids, the FK graph,
`sqlite_sequence`, the non-FK references above, and that runtime/operational tables are
empty. SQLite file bytes are never compared.

## Remaining work

* **G2** — `tests/support.py` still copies the seed; the deterministic builder, importer
  explicit-id modes and DZI subquestion hardening are not part of G1.
* **H** — untrack `data/questions.db` and document the production content-update
  workflow; production still receives nothing from `data/canonical`.
