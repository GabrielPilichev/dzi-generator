# Reviewed subject MC import (F4)

`python3 -m src.import_reviewed_subject_questions` accepts one reviewed JSON
batch with one curriculum, one section, one topic, and one or more text-only
multiple-choice questions. This is a deliberately small authoring format, not
a DZI importer or a statement of approved Physics curriculum.

```json
{
  "format_version": 1,
  "subject": "physics",
  "grade": 8,
  "source": "reviewed:physics:synthetic-example-v1",
  "curriculum": {
    "subject": "physics", "grade": 8,
    "education_type": "synthetic-example", "title": "Synthetic curriculum"
  },
  "section": {
    "slug": "physics-g8-synthetic-section", "title": "Synthetic section", "grade": 8
  },
  "topic": {
    "slug": "physics-g8-synthetic-topic", "title": "Synthetic topic", "grade": 8
  },
  "questions": [{
    "number": 1, "subject": "physics", "grade": 8,
    "topic_slug": "physics-g8-synthetic-topic", "type": "multiple_choice",
    "prompt": "Which letter is first?",
    "options": {"A": "A", "B": "B", "C": "C", "D": "D"},
    "correct": "A"
  }]
}
```

Save a *reviewed* batch outside the seed DB. Both arguments are required:

```sh
python3 -B -m src.import_reviewed_subject_questions --input path/to/batch.json --db path/to/database.db
python3 -B -m src.import_reviewed_subject_questions --input path/to/batch.json --db path/to/database.db --apply
```

The first command is a dry run: it copies the database into memory, validates
and simulates the entire import, and reports inserted/reused counts without
writing the target. `--apply` is the only writing mode; it uses one transaction
and rolls back on any failure. Test/apply to a temporary DB before considering
any separately authorized content PR. F4 does not apply to `data/questions.db`.

`subject` may be any registered canonical subject (`it`, `physics`,
`informatics`, `biology`, `mathematics`, `chemistry`) and `grade` must be one of
that subject's declared grades in `src/subject_registry.py`; a subject or grade
outside the registry is rejected. Registration alone adds no content.

The section and topic slugs must be stable and namespaced
`<canonical-subject>-g<grade>-...` (the same rule as the structure importer;
public URLs drop the namespace). The `source` is a stable
`reviewed:<canonical-subject>:<batch>` identifier. Each positive `number` is
unique within that source; the pair maps to the existing
`questions(source_exam, source_number)` unique index. `source_exam` is a
**non-DZI provenance identifier**, not an official exam; `exam_id` remains
unset. F1 resolves legacy IT aliases, including `informatika_it`, to `it`,
never to a future Informatics subject. New rows store the canonical key.

The importer rejects unknown subjects; mismatched subjects or grades at any
level; slug collisions; conflicting existing content or relationships; missing
provenance; invalid MC options/answers; and F1-inconsistent ownership. Exactly
four nonempty A–D options and one correct letter match the current MC engine.
Exact re-import reuses existing rows without changing them. Content under an
existing stable identity that differs from the batch fails rather than being
overwritten. The importer populates primary topic-section membership,
topic-class mapping, question topic ID, question-topic assignment, and MC
options so F2 browsing and F3 assessment can consume the batch.

Images, lessons, open/fill-in answers, numerical/unit/symbolic grading,
practical tasks, DZI metadata, and multi-section/multi-topic batches are not
supported in F4. No curriculum or real Physics content is implied by the
synthetic example.

## Structure first, questions afterwards

Curriculum structure — every section and topic of a grade, in order, without
questions — is imported separately with `src.import_subject_structure`
(`docs/SUBJECT_STRUCTURE_IMPORT.md`). The normal sequence for a subject is:

1. import the structure of a grade;
2. import question batches into its sections and topics with this tool.

A batch then *reuses* the imported rows instead of creating them. For that, its
`curriculum.education_type` and `curriculum.title`, its `section.slug` and
`section.title`, and its `topic.slug` and `topic.title` must equal the imported
structure exactly; any difference is rejected as a conflict, as before. The
dry-run summary shows `reused: 1` for the curriculum, section and topic.

This tool never sets or changes section or topic order. A topic it creates itself
(one that is not in the structure yet) has no explicit order and is listed after
the ordered topics; add it to the structure file and re-import the structure to
place it. The structure importer treats a stored topic that its file does not
list as a conflict, so the structure file stays the complete, ordered record.

## Batches exported from reviewed candidates

Questions collected from the web do not reach this importer directly. They are
first candidates in a collection-run dataset, are validated and reviewed there,
and only the approved ones are exported as batches in the format above by
`src.export_approved_question_candidates`
(`docs/QUESTION_COLLECTION_PIPELINE.md`). The format and this importer are
unchanged; it remains the only tool that writes such questions to a database.

An exported batch has the `source`
`reviewed:<canonical-subject>:<run id>.<topic>`. Its questions' source URL,
source type, transformation, review record and other provenance cannot be
stored by this format; they are kept in the `<run id>.provenance.json` file
written beside the batch, keyed by the same `source` and question `number`.
Keep the two files together.

Importing into production is a separate, deliberate operational action: take a
fresh application snapshot first (`docs/DEPLOYMENT.md`), dry-run, read the
summary, and only then `--apply`.
