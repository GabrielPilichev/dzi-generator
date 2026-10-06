# Curriculum structure import

`python3 -m src.import_subject_structure` loads the **structure** of one
curriculum — its ordered sections and their ordered topics — without any
question. It is how a registered subject gets real sections and topics before,
or independently of, its question bank.

It is a controlled import of a reviewed file, not an editor: it adds rows and
sets their order. It never deletes, never renames, and never touches questions.
The example below is synthetic; reviewed files for real curricula are listed
under "Structure files in the repository".

## Registry versus structure

- **Subject registry** (`src/subject_registry.py`, code): which subjects exist,
  their route slug, Bulgarian title and declared grades. A subject must be
  registered, and the grade declared, before a structure can be imported.
- **Curriculum structure** (database): what a subject contains in a grade —
  `curricula`, `curriculum_sections`, `curriculum_topics`,
  `topic_section_assignments`, `topic_classes`. This importer writes only these
  five tables.

Registering a subject adds no structure, and importing a structure never changes
the registry. See `docs/SUBJECT_CONTENT_CONTRACT.md` for ownership rules.

## Input format

One JSON file describes one curriculum: one subject, one grade, one education
type. Use one file per grade (and per track, if a subject has several).

```json
{
  "format_version": 1,
  "subject": "physics",
  "grade": 8,
  "curriculum": {
    "education_type": "synthetic-example",
    "title": "Synthetic physics curriculum, grade 8",
    "source_url": "https://example.invalid/official-programme.pdf"
  },
  "sections": [
    {
      "slug": "physics-g8-first-section",
      "title": "Synthetic first section",
      "topics": [
        {"slug": "physics-g8-first-topic", "title": "Synthetic first topic"},
        {"slug": "physics-g8-second-topic", "title": "Synthetic second topic",
         "description": "One sentence of plain prose shown on the topic page."}
      ]
    },
    {
      "slug": "physics-g8-second-section",
      "title": "Synthetic second section",
      "topics": []
    }
  ]
}
```

| Field | Required | Rule |
|---|---|---|
| `format_version` | yes | The integer `1`. |
| `subject` | yes | A registered subject (canonical id, or an identifier the registry resolves; `informatika_it` is IT, never Informatics). Stored as the canonical id. |
| `grade` | yes | An integer among that subject's declared grades. |
| `curriculum.education_type` | yes | Trimmed text. With subject and grade it identifies the curriculum (`curricula` is unique on the three). |
| `curriculum.title` | yes | Trimmed text. |
| `curriculum.source_url` | no | Where the authoritative programme is published. |
| `sections` | yes | Non-empty array. **Array order is the section order.** |
| `sections[].slug` | yes | Stored slug, namespaced (see "Slugs"); unique in the file. |
| `sections[].title` | yes | Trimmed text. |
| `sections[].topics` | yes | Array, possibly empty. **Array order is the topic order.** |
| `topics[].slug` | yes | Stored slug, namespaced; unique in the whole file (a topic belongs to one section). |
| `topics[].title` | yes | Trimmed text. |
| `topics[].description` | no | Plain prose; no vault link syntax such as `[[...]]`. |

Any other key is rejected, including `display_order`, `order` or `position`:
order cannot be given as a number, only as position in the array. Modules,
questions and cross-listing a topic in a second section are not part of the
format.

## Slugs

Stored slugs use the same namespace as the reviewed question importer:
`<canonical subject id>-g<grade>-<name>`, lowercase letters, digits and single
hyphens (`physics-g8-first-section`). Section and topic slugs are globally
unique in the database, and the namespace keeps subjects and grades apart.

Public URLs drop the namespace: `physics-g8-first-section` is served at
`/subjects/physics/8/first-section`, its topic at
`/subjects/physics/8/first-section/first-topic`. A slug is rejected if a stored
section or topic of the same subject and grade already uses the bare name
(`first-section`), because the two would be indistinguishable in a URL.

## Order

- Sections: position in `sections`, stored in
  `curriculum_sections.display_order` (1, 2, 3, ...).
- Topics: position in a section's `topics`, stored in
  `topic_section_assignments.display_order` (migration 010).

Browsing lists topics with an explicit order first, in that order, then topics
without one in the previous order (by title). Questions on a section page follow
the same topic order. Content that was never imported this way — all existing IT
content — has no explicit topic order and is listed exactly as before.

Because the file defines the order, it must be complete for its curriculum: a
stored section of that curriculum, or a stored topic of a listed section, that
the file does not list is a conflict rather than something to place by guesswork.

## Dry run (default)

```sh
python3 -B -m src.import_subject_structure --db path/to/database.db --input structure.json
```

Nothing is written. The file is validated, compared with the database, and the
resulting writes are rehearsed on an in-memory copy. The printed plan names the
subject, grade and curriculum, lists every section and topic in order with
`[create]`, `[update]` (and what changes) or `[unchanged]`, gives totals, and
ends with whether `--apply` would write anything. A rejected file prints every
problem found and exits with status 1.

## Apply

```sh
python3 -B -m src.import_subject_structure --db path/to/database.db --input structure.json --apply
```

`--apply` is the only writing mode. It re-validates and re-plans inside one
transaction; any conflict or error rolls the whole import back, so a file is
imported completely or not at all. New rows are marked
`source_type = 'subject_structure_import'`.

The database must already have migration 010; otherwise the importer stops and
asks for the pending migrations to be applied (`src.manage_migrations`).

## Idempotency

Importing the same file again creates nothing and changes nothing: every item is
reported `[unchanged]` and the result line says the database already matches.
A dry run after an apply reports the same.

A later version of the file may:

- append sections or topics (created);
- reorder sections or topics (order updated, ids and content kept);
- add a `description` to a topic, or `source_url` to the curriculum, where the
  stored value is empty (filled in).

## Conflicts

Existing text is never overwritten and nothing is guessed. These are rejected,
all of them reported together, with the database unchanged:

- unknown subject, or a grade the registry does not declare for it;
- malformed, un-namespaced or duplicate slugs; a topic listed twice;
- a stored curriculum for the same subject, grade and education type with a
  different title or a different non-empty `source_url`, or several stored
  curricula matching it;
- a section slug that already belongs to another curriculum (another subject,
  grade or education type), or is stored with another grade or section type;
- a topic slug whose stored primary section is not the section the file puts it
  in, or that belongs to another subject;
- a stored title or non-empty description that differs from the file;
- a stored topic that is not the primary `covers` member of its section, is
  recorded for other grades, or has inconsistent subject ownership;
- stored sections or topics the file does not list (see "Order");
- a slug that would shadow a bare stored slug in URLs.

To rename something, change the stored row deliberately and then the file; the
importer will not do it. Legacy IT curricula cannot be taken over this way: their
existing sections use un-namespaced slugs that a structure file cannot list.

## Structure-only subjects

After a structure import and before any question, the subject is browsable:
its grade pages list the sections, section pages list the topics, topic pages
show the description. The homepage and subject pages say that questions are in
preparation, and no review tools or test action are shown
(`docs/SUBJECT_CONTENT_CONTRACT.md`, "Placeholders and status"). Physics'
navigation-only placeholder groups retire for a grade as soon as it has stored
sections.

## Importing questions afterwards

Questions are a separate, later step with `src.import_reviewed_subject_questions`
(`docs/REVIEWED_SUBJECT_IMPORT.md`). A question batch names one curriculum,
section and topic; when they equal the imported structure — same education type
and curriculum title, same section slug and title, same topic slug and title —
the batch reuses those rows and only adds questions. The structure and its order
are not changed by a question import, and a structure re-import is not affected
by questions.

The opposite order also works: a structure file may list a section and topic
that a question batch created earlier; the import then only sets their order.

## Structure files in the repository

Reviewed structure files are tracked under
`data/subject_structures/<subject id>/grade-<n>.json`, one file per curriculum
(add the education type to the name if a grade ever has more than one). Each has
a review record under `docs/reviews/` that names the official source, says which
wording is official and which is LearnPilot's own, and records the provenance
this format cannot hold (it stores only the curriculum `source_url`).

| File | Curriculum | Review record |
|---|---|---|
| `data/subject_structures/physics/grade-8.json` | Physics, grade 8, general education | `docs/reviews/physics_g8_curriculum_structure_review.md` |
| `data/subject_structures/physics/grade-9.json` | Physics, grade 9, general education | `docs/reviews/physics_g9_curriculum_structure_review.md` |
| `data/subject_structures/biology/grade-8.json` | Biology, grade 8, general education | `docs/reviews/biology_curriculum_structure_review.md` |
| `data/subject_structures/biology/grade-9.json` | Biology, grade 9, general education | `docs/reviews/biology_curriculum_structure_review.md` |
| `data/subject_structures/biology/grade-10.json` | Biology, grade 10, general education | `docs/reviews/biology_curriculum_structure_review.md` |

A tracked file is not an imported one. Neither the seed database nor production
changes until someone runs the importer with `--apply` on that database.

## Production

Importing into the production database is a separate, deliberate operational
action. It is never part of a deploy and nothing runs it automatically.

1. Review the structure file and commit it to the repository first.
2. Take a fresh application snapshot and confirm it succeeded
   (`docs/DEPLOYMENT.md`, sections 16 and 20).
3. Dry-run against the production database and read the whole plan.
4. Only then run the same command with `--apply`, as the service user.
5. Check the subject's pages.

The browsing cache follows database changes, so no restart is needed. The seed
database tracked in the repository is not the production database; importing
into one does not change the other.
