# Subject identity and content ownership

## Canonical registry

`src/subject_registry.py` is the single list of LearnPilot subjects. It holds
static product metadata only: canonical id, URL route slug, Bulgarian title and
copy, display order, declared grades, legacy aliases, and the `dzi` and
`legacy_grade_routes` flags. Homepage cards, navigation, search text, subject
routes, the teacher form and the reviewed importer all derive from it.

| id | route slug | title |
|---|---|---|
| `it` | `information-technologies` | Информационни технологии |
| `physics` | `physics` | Физика |
| `informatics` | `informatics` | Информатика |
| `biology` | `biology` | Биология |
| `mathematics` | `mathematics` | Математика |
| `chemistry` | `chemistry` | Химия |

`src/subject_content.py` resolves stored values, keys and slugs through the
registry. Stored `IT`, `informatika_it`, and `Информационни технологии` resolve
to `it`; the `information-technologies` route slug also resolves to `it`.
`informatika_it` is legacy IT/DZI data and never resolves to `informatics`;
the bare names `informatika` and `Информатика` are deliberately not aliases.
Unknown values have no fallback. URLs accept route slugs only, never ids or
legacy aliases.

Being registered does not imply content. Availability is derived, not stored:
a subject is `active` when it owns at least one section, in a declared grade,
whose class equals its curriculum grade (`subject_content_grades`); otherwise it
is `in_preparation` and shows the existing "В ПОДГОТОВКА" presentation. Teacher
subject buttons and homepage/navigation status follow the same derivation, so
importing reviewed content activates a subject without route changes. If the
database cannot be read, every subject degrades to `in_preparation`.

Each subject declares the grades it supports (currently 8-12 for all six). A
declared grade without content is an intentional preparation page; an
undeclared grade is a 404, and the importer rejects it. Only `it` carries the
`dzi` flag, which gates the DZI homepage entry, practical-task panel and
"ДЗИ източник" wording. Physics keeps navigation-only placeholder topic groups
that retire per grade once that grade has database content.

Ownership is read through the existing records:

1. A curriculum declares its subject in `curricula.subject`.
2. A section inherits from `curriculum_sections.curriculum_id`.
3. A topic inherits from `curriculum_topics.section_id`, its primary section.
   That section must also occur as its primary `topic_section_assignments`
   entry. Every additional section membership must have the same subject;
   other grades and curricula within that subject are valid.
4. A question's `questions.subject` must resolve to the same subject as its
   `questions.topic_id` and that topic's section memberships.

`topic_classes.class` and `curriculum_sections.class` identify grades, never
subjects. Resolvers return `owned`, `unknown`, `unclassified`, `conflict`, or
`missing`, with a reason and no inferred subject for unresolved records.
Authoring code can use `require_topic_subject` to reject a mismatched topic.
These helpers only read the database; they do not repair or hide old rows.

Current seed exceptions: one unclassified section has no curriculum, and ten
questions have no `topic_id`. Existing IT/DZI routes continue to handle these
as before. The IT-only question generator now rejects topics outside `it`
before generating or saving an IT/DZI-labelled question.

F2 browsing uses `src/subject_browsing.py` to select sections, topics,
questions, counts, and homepage search titles through these ownership rules.
The legacy `/grade/<n>` and `/section/<slug>` paths remain IT-only; the latter
still permits the known curriculum-less section URL without assigning it an IT
owner. A populated generic subject/class page uses its own database sections,
while an empty Physics class keeps its provisional cards.

F3 new assignment selection uses `src/subject_assessment.py`. The selected
section supplies the subject; its MC and accepted-text open candidates must
have F1-consistent question ownership and membership in that section. New
mixed plans are validated before persistence. The teacher form defaults to IT
for old links and can select any registered subject (buttons appear for subjects
with content); unknown or mismatched subject requests are rejected. Existing persisted plans, metadata-only edits,
duplication's verbatim snapshot copy, and started attempt snapshots are not
regenerated. The IT-only DZI training generator excludes non-IT and
unclassified questions, even when class membership matches. Reviewed non-DZI
import tooling remains F4. No Physics questions, migrations, tables, or
curriculum decisions are added.
