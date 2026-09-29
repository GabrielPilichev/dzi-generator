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

The section and topic slugs must be stable and namespaced
`<canonical-subject>-g<grade>-...`. The `source` is a stable
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
synthetic example. Actual academic structure and content review belong to F5.
