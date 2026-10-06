# Canonical content (G1)

`data/canonical/` is the **database reconstruction representation** of LearnPilot's
durable content: curriculum, questions, options and the DZI structures. It exists so
that the content in `data/questions.db` can be rebuilt from tracked, reviewable files
**with stable ids**. The reviewed structure/batch JSON files stay the *human authoring*
inputs; they are not an id authority.

Status: G1 (canonical data), G2 (builder, test databases, importer identity) and H
(the tracked binary seed is retired).

## Sources of truth (after H)

| What | Source of truth |
| --- | --- |
| Schema | `web/migrations/` |
| Durable content | `data/canonical/` |
| Runtime database | ordinary SQLite, generated/provisioned from those two |
| Production | external persistent database, never stored in Git |
| Tests | temporary databases built from migrations + canonical |

`data/questions.db` is **no longer tracked**; it is ignored runtime state (with its
`-wal`/`-shm`/`-journal` sidecars). A fresh clone builds one explicitly:

```bash
python3 -m src.build_database --output data/questions.db
```

The application never builds the database at startup and never reads `data/canonical` while
serving requests. An existing local or production database is kept and migrated normally, never
replaced by a rebuild. The parity contract is: migrations + canonical -> a deterministic logical
database, and canonical export/check -> no drift.

## Classification

Every table is classified exactly once in `src/canonical_tables.py`
(`TABLE_CLASSIFICATION`); a table without an entry fails
`tests/test_canonical_content.py`.

| Class | Meaning | Tables |
| --- | --- | --- |
| canonical | durable content, rebuilt from tracked files | `curricula`, `curriculum_*`, `topic_*`, `lessons`, `questions`, `multiple_choice_options`, `fill_in_subquestions`, `question_topic_assignments`, `exams`, `exam_tasks`, `exam_task_questions`, `official_exam_sources`, `practical_tasks`, `practical_task_resources`, `assets`, `asset_links`, `dzi_blueprints`, `dzi_blueprint_slots`, `assessment_events`, `assessment_event_sections` (27 tables) |
| runtime | user/session state; a content rebuild never writes or replaces it | `quiz_assignments`, `quiz_attempts`, `quiz_answers`, `quiz_text_answers`, `practical_submissions`, `practical_submission_files` |
| operational | caches, vault mirror, logs, bookkeeping | `obsidian_notes`, `note_question_links`, `generated_exams`, `scrape_log`, `schema_migrations`, `sqlite_sequence` |

The retired seed's smoke-test runtime rows (2 assignments, 4 attempts, 19 answers) and its
operational rows were never part of the canonical data.
No `student_name` or other user data is exported.

## Provenance (the one-time G1 reconciliation)

`data/canonical` was constructed once from the then-tracked legacy seed (values and ids of the
legacy rows), the tracked Physics/Biology structures and reviewed batches (values) and a
read-only identity export of the author's local database (ids), matched by natural key
(curriculum `(subject, grade, education_type)`, section/topic slug, question
`(source_exam, source_number)`, option `(question_id, option_letter)`), never by position.
`manifest.json` → `sources` records that provenance (including the retired seed's SHA-256) as
history. The one-off reconciliation tool and the seed it read were removed in H; the tracked
canonical files are now the only authority, and changes go through
`canonical_export --replace-canonical`.

Properties of the dataset that stay true:

* `created_at` is `NULL` on the rows added from tracked inputs (topics, questions,
  topic/question assignments); those local timestamps are not tracked anywhere.
* `topic_section_assignments` and `question_topic_assignments` ids of those rows are derived:
  no foreign key or stored reference points at these tables.
* `sqlite_sequence` is `max(historical high-water mark, max id)`; the high-water marks (ids of
  deleted historical rows) are preserved, so ids are never reused.

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
`000_baseline` + `001`–`011` + later migrations.

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

`data/canonical` is the durable identity contract.

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

## Remaining `data/questions.db` references (after H)

| Reference | Class |
| --- | --- |
| `DEFAULT_DB_PATH = data/questions.db` in `src/*.py` CLIs, `web/app.py` and `web/services/browsing.py`: the local runtime default (the file must be built first; `audit_dzi_state` says so when it is absent) | runtime default |
| README, AGENTS, this file, `docs/TESTING.md`: the builder output path and the "ignored runtime state" explanation | documentation |
| `docs/DEPLOYMENT.md`, `web/production.py`, `deploy/*.example`: the production path `/var/lib/learnpilot/data/questions.db` and the refusal of state inside the checkout | production runtime path |
| tests that assert those defaults, refuse to touch the checkout DB or check it is ignored (`test_web_db_integration`, `test_production_deployment`, `test_isolation`, `test_build_database`, `test_database_test_support`, `test_seed_retirement`) | runtime tooling / contract tests |

## Remaining work

* The production content-update workflow (applying reviewed canonical seeds/importers to the
  production database) is part of the later consolidated deployment; H changes no production
  behavior and production receives nothing from `data/canonical` yet.
