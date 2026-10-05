# Question collection and review pipeline

How questions found on the web become reviewed LearnPilot questions. This is
the local data, validation and review side. **Nothing here fetches from the
web**, and nothing here writes a database: the only database boundary is the
existing reviewed importer (`docs/REVIEWED_SUBJECT_IMPORT.md`).

```
web discovery            (a later, bounded collection run — not in this repository yet)
  -> source record
  -> candidate            src.start_question_collection, src.question_candidates
  -> validation, flags    src.validate_question_candidates
  -> review file          a person sets each candidate's review status
  -> approved export      src.export_approved_question_candidates   (JSON files only)
  -> reviewed import      src.import_reviewed_subject_questions     (dry run, then --apply)
```

Collection is never approval, and approval is never import. Each arrow after
"validation" is a separate, deliberate human step.

## Commands

```sh
# 1. Start a bounded run: writes an empty candidate dataset. No network, no database.
python3 -B -m src.start_question_collection \
  --subject physics --grade 8 \
  --topics physics-g8-first-topic,physics-g8-second-topic \
  --structure path/to/physics-g8-structure.json \
  --max-pages 40 --max-candidates 100 --max-per-source 20 \
  --output data/question_candidates/physics/grade-8/physics-g8-20260110.json

# 2. Validate (read-only). Exit status 1 if there is any error.
python3 -B -m src.validate_question_candidates --input RUN.json
python3 -B -m src.validate_question_candidates --input RUN.json --db path/to/database.db
python3 -B -m src.validate_question_candidates --input RUN.json --annotate RUN.review.json

# 3. After human review: export the approved candidates as reviewed import batches.
python3 -B -m src.export_approved_question_candidates \
  --input RUN.review.json --output-dir data/import_batches/subjects/physics/grade-8

# 4. Import with the existing importer: dry run first, then a deliberate --apply.
python3 -B -m src.import_reviewed_subject_questions --input BATCH.json --db path/to/database.db
```

`--subject`, `--grade`, `--topics`, `--max-pages`, `--max-candidates` and
`--max-per-source` are required by the start command; there are no defaults for
them on the command line. `--structure` is the reviewed structure file of
`docs/SUBJECT_STRUCTURE_IMPORT.md`: the run copies the exact section and topic
titles from it, because the reviewed importer later requires them to match.

None of these commands has an `--apply`, `--apply-to-db` or import option.

## Hard limits

One run covers **one subject and one grade**. That is structural: the dataset
has a single `subject` and a single `grade`. Every other limit is a positive
integer stored in `run.limits`; all five are required.

| Limit | Default | Absolute ceiling |
|---|---|---|
| `max_topics` (target topics) | 3 | 10 |
| `max_pages` (pages inspected) | 40 | 100 |
| `max_candidates` | 100 | 250 |
| `max_candidates_per_source` | 20 | 50 |
| `max_runtime_minutes` | 30 | 60 |

The ceilings live in `src/question_collection_limits.py`
(`ABSOLUTE_CEILINGS`) and are enforced by `CollectionLimits` itself, so a
command-line flag, a dataset file or a caller cannot raise them. Zero, negative,
missing, `null`, boolean, fractional and text values are rejected: there is no
value that means "unlimited". `max_candidates_per_source` may not exceed
`max_candidates`.

The validator checks a dataset against its own limits: the number of target
topics, `pages_inspected`, the number of candidates and the number of candidates
per source. The number of source records may not exceed `pages_inspected`, since
every source is an inspected page, and one URL is one source.

`CollectionBudget` is the live counter for a future collector: `record_page()`
before each fetch and `record_candidate(source_id)` before keeping a candidate.
It raises `CollectionLimitReached` with the stop reason (`page_limit`,
`candidate_limit`, `runtime_limit`) and stays stopped. Runtime is enforced there;
in a stored dataset a run that lasted longer than `max_runtime_minutes` is
flagged (`runtime_limit_exceeded`), because it is recorded after the fact.

## Dataset format (version 1)

One JSON file per run: the manifest, the sources, the candidates. Unknown keys
are rejected everywhere. A complete synthetic example is tracked at
`docs/examples/question_candidates.example.json`.

```json
{
  "format_version": 1,
  "kind": "question_candidates",
  "run": { "...": "manifest" },
  "sources": [ { "...": "one inspected page" } ],
  "candidates": [ { "...": "one question" } ]
}
```

### Run manifest (`run`)

| Field | Rule |
|---|---|
| `id` | Lowercase hyphenated slug, at most 80 characters. Prefix of every candidate id. |
| `started_at`, `finished_at` | ISO 8601 with a UTC offset (`Z` allowed). `finished_at` is `null` while the run is open. |
| `collector` | Optional: who or what collected (a person, a model name). |
| `subject` | Canonical subject id from `src/subject_registry.py` (aliases are not accepted here). |
| `grade` | An integer among that subject's declared grades. |
| `curriculum` | `education_type` and `title`, exactly as in the structure. |
| `targets` | Sections with their targeted topics: `{"section": {"slug", "title"}, "topics": [{"slug", "title"}]}`. Slugs are namespaced `<subject>-g<grade>-...`. |
| `limits` | The five limits above. |
| `pages_inspected` | Pages fetched so far. |
| `stop_reason` | `null` while open, else one of `candidate_limit`, `page_limit`, `topic_complete`, `runtime_limit`, `manual_stop`, `no_more_sources`. Set together with `finished_at`. |
| `notes` | Optional text. |

The sources used and the candidate count are the `sources` and `candidates`
arrays themselves; they are not repeated in the manifest.

### Source record (`sources[]`)

| Field | Rule |
|---|---|
| `id` | Lowercase hyphenated slug, unique in the dataset. |
| `type` | A source category: `official` or `third_party`. |
| `url` | Absolute `http`/`https` URL. One URL is one source. |
| `title` | Title of the page or document. |
| `retrieved_at` | ISO 8601 with a UTC offset. |
| `publisher`, `notes` | Optional text. |

### Candidate (`candidates[]`)

| Field | Rule |
|---|---|
| `id` | Stable slug starting with `<run id>-`, unique in the dataset. |
| `section_slug`, `topic_slug` | A target topic of the run and its section. |
| `source_id` | One of the source records. |
| `transformation` | `extracted` (source wording), `adapted` (edited source wording) or `original_from_research` (newly written). |
| `source_ref` | Optional locator in the source: any of `document_title`, `exam_session`, `question_number`, `page`. |
| `research` | What a newly written question is based on: `concept` (required), `skill`, `notes`. |
| `reuse_exception` | Only for the exception described under "Source policy": `basis` and `approved_by`. |
| `type` | `multiple_choice`, `true_false` or `short_answer`. |
| `prompt` | Trimmed question text. |
| `options`, `correct` | Multiple choice: an object of consecutive Latin letters from `A` (2 to 6) and the correct letter. True/false: `correct` is `true` or `false`. |
| `accepted_answers` | Short answer: non-empty array of accepted texts. |
| `explanation` | Optional rationale. |
| `difficulty` | Optional: `easy`, `medium` or `hard`. |
| `review` | `status`, `reviewer`, `reviewed_at`, `notes` (all four keys; see "Review states"). |
| `flags` | Optional `[{code, message}]` written by `--annotate`. Informational only: validation and export always recompute flags and never trust stored ones. |

The subject and grade of a candidate are those of its run.

## Source policy

These are conservative provenance and review controls. They are not legal
advice and make no claim about what the law permits.

| Source type | `extracted` | `adapted` | `original_from_research` |
|---|---|---|---|
| `official` (public-authority material such as exam papers and curricula) | accepted; flagged `extract_without_locator` if there is no `source_ref` | accepted | accepted |
| `third_party` (educational or commercial sites, textbooks, question banks) | **rejected** (`third_party_verbatim`) | accepted, always flagged `third_party_adapted` | accepted; `research.concept` is required |

Third-party material is research input. The candidate stores the concept, skill
and notes about what the source covers, and the question itself is newly written
for LearnPilot. The dataset has no field for third-party source text, and
`research.notes` longer than 500 characters is flagged (`research_notes_long`):
notes describe the concept, they do not hold copied wording. The validator
cannot detect copying; the reviewer compares against the source URL.

Narrow exception: an `extracted` third-party candidate is accepted only when it
carries `reuse_exception` with a `basis` (for example a written permission on
file) and `approved_by` (a named person). It is then flagged
`third_party_reuse_exception` on every validation and export. The exception is
per candidate and is rejected anywhere else.

A new source category is one entry in `SOURCE_TYPES`
(`src/question_candidates.py`) stating whether verbatim wording is allowed and
whether research metadata is required. The file format does not change.

## Review states

`pending` → `approved` | `rejected` | `needs_revision`.

- A collector can only submit `pending` candidates (`add_candidate` refuses
  anything else).
- `approved` requires a `reviewer` and a `reviewed_at` that is not earlier than
  the start of the run, and the reviewer may not be the run's `collector`.
- An approved candidate may not carry a blocking flag (`duplicate_exact`,
  `duplicate_existing_exact`); validation reports `approved_blocked`.
- Only `approved` candidates are exported. `pending`, `needs_revision` and
  `rejected` candidates stay in the candidate file and are listed in the export
  report with the reason.

Reviewing means editing `review` in the dataset by hand and re-running the
validator. There is no automatic approval anywhere.

## Validation

`src.validate_question_candidates` prints a JSON report with `errors` (the
dataset is not usable) and `flags` (a reviewer must look). It never changes its
input. Errors cover: format version and kind; unknown subject; unsupported
grade; slug shape and topic membership; missing or malformed provenance; unknown
source type or transformation; the source policy; malformed questions
(unsupported type, missing or invalid correct answer, bad or duplicate answer
choices, placeholder choices, untrimmed text); invalid review state; every hard
limit; duplicate candidate ids.

With `--db` the database is opened strictly read-only
(`mode=ro&immutable=1`, `query_only`), so not even SQLite sidecar files are
touched. Two checks are added:

- ownership: each target section and topic that exists must belong to the
  run's subject, grade and curriculum with the same title (`db_conflict` is an
  error); a target that is not stored yet is flagged `target_not_in_db` — import
  the structure first;
- duplicates of stored questions of the same subject (below).

Because the connection is immutable it cannot see changes still waiting in a
write-ahead log; that is flagged `db_wal_pending`. For a current result on a
live database, validate against a `src.backup_sqlite` copy. Do not point these
tools at production; use a copy.

`--annotate OUTPUT` writes a copy of the dataset with the computed flags stored
on each candidate, as the file to review. It refuses to replace an existing file.

### Quality flags

| Flag | Meaning |
|---|---|
| `prompt_too_short` | Fewer than 12 characters or 3 words after normalization. |
| `prompt_too_long` | More than 1500 characters. |
| `html_residue` | An HTML tag, an entity such as `&nbsp;`, or `javascript:` in any text. |
| `answer_leak` | The correct choice, and no other choice, appears in the prompt; or an accepted short answer does. |
| `mc_option_count` | Not exactly four choices (the reviewed importer accepts only four). |
| `extract_without_locator`, `third_party_adapted`, `third_party_reuse_exception`, `research_notes_long` | See "Source policy". |
| `runtime_limit_exceeded`, `target_not_in_db`, `db_wal_pending` | Dataset-level notes described above. |

These are deterministic text rules. They do not show that a question is
correct, unambiguous, at the right level or on the right topic. A dataset with
no flags is not a reviewed dataset.

### Duplicate detection

Text is compared after normalization: Unicode NFKC, case folding, sentence
punctuation and quotes replaced by spaces, whitespace collapsed. Operators and
signs (`+ - * / = < >`) are kept, so `5 + 3` and `5 - 3` stay different.

- **Exact** (`duplicate_exact`): equal normalized prompts and equal sets of
  normalized answer choices. The same wording with different choices is a
  different question.
- **Near** (`duplicate_near`): prompt similarity of at least 0.90
  (`difflib.SequenceMatcher` ratio) and, when both questions have choices, at
  least half of the choices in common.
- With `--db`, the same comparison against stored questions of the same subject:
  `duplicate_existing_exact`, `duplicate_existing_near`. Questions already
  imported from this same run are not counted.

Duplicates are flagged on both candidates and never removed. Rejected candidates
are left out of the comparison, so rejecting one of a pair clears the flag on
the other. Limitations: questions that differ only in numbers are reported as
near duplicates; reworded questions about the same fact are not detected; images
and formulas written differently are not understood; the comparison is quadratic,
which is fine at the 250-candidate ceiling.

## Export and the importer boundary

`src.export_approved_question_candidates` re-validates the dataset, refuses it
if there is any error or if the run is not finished, and writes into
`--output-dir`:

- `<run id>.<topic>.json` — one reviewed import batch per topic, in the
  unchanged format of `docs/REVIEWED_SUBJECT_IMPORT.md`, checked with the
  importer's own `validate_batch`. Its `source` is
  `reviewed:<subject>:<run id>.<topic>`; questions are numbered in candidate
  order.
- `<run id>.provenance.json` — for every exported question number: the
  candidate id, the full source record, transformation, `source_ref`, research,
  explanation, difficulty, review and flags; plus the run manifest and the list
  of candidates that were not exported and why.

Existing files are never replaced. The command writes JSON only.

**What reaches the database.** The reviewed import format, and the `questions`
table behind it, hold the batch `source` identifier (as `source_exam`) and the
question number. They have no place for the source URL, title, publisher,
retrieval time, source type, transformation, locator, research notes,
explanation, difficulty or review record. The export report lists these under
`provenance_not_stored_in_database`, and the provenance file keeps them: a
stored question's `source_exam` and `source_number` identify exactly one entry
in it. Keep the provenance file with the batch. Storing this in the database
would need a schema change, which this pipeline deliberately does not make.

**What the importer accepts.** Only multiple-choice questions with exactly the
choices A–D. Approved `true_false`, `short_answer` and other-sized
multiple-choice candidates are valid candidates but are not exported; the report
names each one.

**Finish the review before exporting.** Once a source has been imported, the
importer rejects a later batch for the same source with a different set of
question numbers. Export therefore refuses a topic that still has `pending` or
`needs_revision` candidates. `--allow-undecided` overrides this for a topic you
will not export again from this run; revised questions then belong in a new run.

## Where files live

| What | Where | Git |
|---|---|---|
| Format documentation, synthetic example | `docs/QUESTION_COLLECTION_PIPELINE.md`, `docs/examples/` | tracked |
| Reviewed curriculum structure files (the `--structure` input) | `data/subject_structures/<subject>/grade-<n>.json` | tracked (`docs/SUBJECT_STRUCTURE_IMPORT.md`) |
| Collection-run datasets (candidates under collection or review) | `data/question_candidates/<subject>/grade-<n>/` | ignored (`.gitignore`) |
| Exported reviewed batches and their provenance file | `data/import_batches/subjects/<subject>/grade-<n>/` | tracked when a content PR intentionally adds them, like the other reviewed batches in `data/import_batches/` |

Run output is bulky, unreviewed and may contain rejected material, so it is not
committed by default. An approved export is the reviewed artefact and follows the
existing convention for reviewed batches.

## Rules for a future collection agent

A web-enabled collector is not part of this repository yet. When one is run
(by a person or by an agent), these rules apply.

1. Start the run with `src.start_question_collection`; never hand-write limits.
   Stop when `CollectionBudget` stops, and record the `stop_reason`.
2. One subject, one grade, the listed target topics. Nothing else.
3. Respect `robots.txt` and any site terms or restrictions that apply.
4. Respect access controls: no login, paywall or CAPTCHA bypass, no
   circumvention of rate limits or blocks.
5. Request politely: low request rates, an identifying user agent, back off on
   errors. `src/scrape/http.py` is the existing polite fetcher.
6. No whole-site crawling: no recursive link following without a page budget,
   no sitemap-wide fetching, no "everything on this domain" mode. Fetch the pages
   needed for the target topics, within `max_pages`.
7. Never copy a third-party question bank. Third-party pages are research: record
   the concept and skill, then write a new question. Do not store their question
   wording in the dataset.
8. Official public-authority material may keep its wording, with exact
   provenance: URL, title, retrieval time and a `source_ref` locator.
9. Record a source for every page a candidate came from, and submit candidates
   with `add_source` / `add_candidate` (`src/question_candidates.py`), which
   enforce the limits and the `pending` state.
10. Never approve, export or import. A person reviews every candidate; a person
    runs the export and the importer.
11. Never write to `data/questions.db` or any production database.

## What is deliberately not here

No web fetching, no crawler, no browser automation, no AI API calls, no
background jobs, no review UI, no database migration, no second database
importer, and no automatic approval or import.
