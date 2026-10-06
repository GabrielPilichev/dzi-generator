# Canonical content (G1)

`data/canonical/` is the **database reconstruction representation** of LearnPilot's
durable content: curriculum, questions, options and the DZI structures. It exists so
that the content in `data/questions.db` can be rebuilt from tracked, reviewable files
**with stable ids**. The reviewed structure/batch JSON files stay the *human authoring*
inputs; they are not an id authority.

Status: G1 (canonical data) and G2 (builder, test databases, importer identity).
`data/questions.db` is **still tracked** as the transitional seed/dev DB, but nothing
needs it to *construct* a database any more: `python3 -m src.build_database` and the test
suite build databases from migrations + `data/canonical`. Untracking the seed is H
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
| `python3 -m src.build_database --output PATH` | G2: fresh database from migrations + canonical content (see below) |

`--replace-canonical` refuses (and leaves the files untouched) when it would remove a
row, drop a row count, change the id of a natural key or reassign an id, change an
option letter, lower a sequence, or include a runtime/operational table. New rows and
value changes are allowed. There is deliberately no override flag: a reviewed retirement
of an id needs its own change.

`src/canonical_load.py` is the explicit-ID loader (`rebuild_for_parity`: empty database →
migrations → canonical load). It loads in FK order inside one transaction with
`foreign_keys=ON`, fails on conflicting existing rows, writes only canonical tables and
`sqlite_sequence`, never lowers a sequence and never remaps an id. `src/build_database.py`
(below) wraps it with validation and an atomic move.

`src/canonical_parity.py` compares a database with the canonical files: schema against a
freshly migrated database, row counts, normalized hashes, explicit ids, the FK graph,
`sqlite_sequence`, the non-FK references above, and that runtime/operational tables are
empty. SQLite file bytes are never compared.

## Building a database (G2)

```sh
python3 -m src.build_database --output /path/to/new.db
```

```
empty temp file -> migrations 000..current (manage_migrations.apply_migrations)
                -> explicit-id canonical load (canonical_load.load_canonical)
                -> validation -> atomic move to --output
```

* `--output` is required and must not exist (file, directory, symlink or stale `-wal`/`-shm`/
  `-journal`): the builder never overwrites. A missing parent directory is created.
* It builds at a hidden temporary sibling path and links it into place only after validation,
  so a failure (migration error, missing/corrupt canonical file, hash mismatch, load conflict,
  FK/parity failure, an existing destination) exits non-zero and leaves **no** database and no
  temporary files behind. A pre-existing path is never removed.
* It never reads or copies `data/questions.db`, and application start-up never calls it.
* Validation (`build_database.validation_problems`): schema parity with a freshly migrated
  database, applied migrations == repository migrations == manifest level, per-table counts and
  normalized hashes (which include every explicit id), `sqlite_sequence` equal to the manifest
  and without entries for non-canonical tables, FK graph, non-FK references, runtime and
  operational tables empty, `foreign_keys` enableable, `integrity_check`, rollback-journal
  mode and no sidecar files.
* Reusable API: `build_database(output, canonical_dir=..., out=None) -> BuildResult`,
  `validation_problems(path)`, and `logical_fingerprint(path)` (schema, migration filenames,
  canonical hashes, sequences, non-canonical row counts, FK violations: two builds from the same
  checkout have equal fingerprints; SQLite file bytes and `applied_at` timestamps are not compared).
* A build takes about 1.5 s.

The result holds the canonical content with its stable ids and **empty runtime tables**
(`quiz_*`, `practical_submission*`) and empty operational tables. Tests and tools create the
runtime state they need explicitly (`tests/runtime_fixtures.py`, `docs/TESTING.md`).

## Content update workflow (developers)

```
reviewed source (structure JSON / reviewed batch / DZI JSON)
  -> import on a controlled working database
       (a build_database output or a temporary copy; never data/questions.db)
  -> stored rows keep their ids; only genuinely new rows are inserted, with safe ids
  -> python3 -m src.canonical_export --db WORKING.db --replace-canonical   (identity-guarded)
  -> python3 -m src.canonical_export --check  +  tests
  -> commit data/canonical
```

`data/canonical` is the durable identity contract; the seed is no longer the identity source.

### Importer identity rules

| Importer | Natural key (id kept) | New rows | Explicit ids |
| --- | --- | --- | --- |
| `import_subject_structure` | curriculum `(subject, grade, education_type)`, section slug, topic slug | `stable_ids.allocate_id` | `explicit_ids={"curricula": id, "sections": {slug: id}, "topics": {slug: id}}` |
| `import_reviewed_subject_questions` | curriculum/section/topic as above, question `(source_exam, source_number)`, option `(question_id, letter)` | `stable_ids.allocate_id` | `explicit_ids={..., "questions": {number: id}}` |
| `import_dzi_questions_json` | question `(source_exam, source_number)`, option `(question_id, letter)`, subquestion `(question_id, number)` | `stable_ids.allocate_id` | none (the DZI JSON has no id field) |
| `import_dzi_exam_skeleton`, `seed_dzi_blueprint`, `import_practical_task_resources` | upsert by natural key / `ON CONFLICT DO UPDATE`; never delete | SQLite `AUTOINCREMENT` (above the sequence of a built database) | none, unchanged in G2 |

Rules (`src/stable_ids.py`): an existing natural key keeps its stored id; a new row takes an id
above the canonical maximum/sequence **and** the target's maximum/sequence, so it can never
collide with a canonical row the target lacks or recycle a deleted id; a supplied explicit id
must be free, and an id at or below a recorded high-water mark is accepted only to restore the
canonical row that owns it (same natural key); anything else, or a different id for an
existing row, raises `IdConflict`/`StructureImportError` before anything is written and the
transaction rolls back. Nothing silently remaps.

Not changed, deliberately: obsolete one-shot scripts (`parse*.py`, `migrate_to_v2.py`,
`import_classroom_tests.py`, topic/question generators) are not part of the current content
workflow and are listed as historical in `docs/SCRIPT_INVENTORY.md`.

### DZI option/subquestion identity

The DZI importer used to `DELETE` a question's options/subquestions and re-insert them, giving
every re-imported row a new id. Now the payload is matched to stored rows by natural key:
a matching option letter / subquestion number is **updated in place** (same id); a missing one
is inserted with a safe new id; a stored letter/number the payload no longer lists is **stale
and deleted deliberately** (the payload is the complete set for the task; the deleted id is
never reused). Switching a task between multiple-choice and short-answer removes the other
type's rows for the same reason. Tests: `tests/test_importer_stable_ids.py`.

## Remaining seed references (G2 inventory)

Classification: **A** migrated to the builder/canonical in G2; **B** intentionally retained
until H; **C** production/runtime default path, not seed construction.

| Reference | Class |
| --- | --- |
| `tests/support.py` `isolated_app()`, `subject_fixtures.apply_pending_migrations`, and the seven test modules that copied the seed (`test_dzi_training_production_paths`, `test_export_approved_question_candidates`, `test_import_reviewed_subject_questions`, `test_import_subject_structure`, `test_question_candidates`, `test_subject_registry`, `test_production_deployment`) | A: now `build_test_database()` |
| `tests/test_canonical_content.py` (legacy-seed equivalence, seed immutability), `tests/test_schema_baseline.py` (000 + migrations == seed schema), `tests/test_production_deployment.py` baseline-adoption startup test, `tests/test_seed_database_copy.py` / `tests/test_isolation.py` (the byte-copy helper itself), `tests/support.copy_seed_database`, `tests/test_audit_dzi_state_seed_safety.py`, `src/canonical_reconcile.py` and the manifest's `sources` | B (pinned by `tests/test_seed_transition.py`) |
| `tests/seed_guard.py` tripwire and `tests/test_seed_guard.py` | B: remove with the seed in H |
| `DEFAULT_DB_PATH = data/questions.db` in `src/*.py` CLIs, `web/app.py`, `web/production.py`, `web/services/browsing.py`, README/DEPLOYMENT/AGENTS, and tests asserting those defaults or refusing to touch the checkout DB (`test_web_db_integration`, `test_production_deployment`, `test_migration_runner_dry_run`, `test_validate_*`, `test_question_image_review_suggestions`) | C |

## Remaining work

* **H** — untrack `data/questions.db`, retire the tripwire and the transitional seed-equivalence
  tests, and document the production content-update workflow; production still receives nothing
  from `data/canonical`, and G2 changes no production behavior.
