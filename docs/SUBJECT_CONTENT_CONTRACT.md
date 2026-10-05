# Subject identity, content ownership and the generic subject experience

Every subject uses the same routes, templates, styles and controller code:

```
Subject   src/subject_registry.py
 └ Grade    curricula.grade = curriculum_sections.class
    └ Section  curriculum_sections (subject through its curriculum)
       └ Topic   curriculum_topics.section_id + topic_section_assignments
          └ Questions  questions.topic_id, questions.subject
```

Adding a subject is a registry entry plus data (see "Adding a subject").

## Canonical registry: what a subject is

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

The registry never describes curriculum. The one exception is Physics'
navigation-only `placeholder_topic_groups` (see "Placeholders and status").

## Database: what a subject contains

Curriculum structure and questions live only in the database, in the existing
tables; there is no `subjects` table and no per-subject schema.

- `curricula` declares a subject and a grade (several `education_type` rows per
  subject and grade are allowed).
- `curriculum_sections` belongs to one curriculum; `section_slug` is globally
  unique and `display_order` orders sections in a grade.
- `curriculum_modules` is optional. A section may have no module, and a subject
  needs none.
- `curriculum_topics.section_id` is a topic's primary section;
  `topic_section_assignments` lists every section it appears in.
  `topic_slug` is globally unique.
- `topic_section_assignments.display_order` (migration 010) is a topic's
  explicit position in a section. It is optional: topics with an order come
  first, in that order; topics without one follow in the earlier order, by
  title. Questions on a section page follow the same topic order. No existing
  IT row has an explicit order, so IT content is listed exactly as before.
- `questions.topic_id` and `questions.subject` attach a question.

Each subject declares the grades it supports (currently 8-12 for all six). A
declared grade without content is an intentional preparation page; an
undeclared grade is a 404, and the importer rejects it.

## Ownership inheritance

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
as before. The IT-only question generator rejects topics outside `it` before
generating or saving an IT/DZI-labelled question.

`src/subject_browsing.py` selects sections, topics, questions, counts and
homepage search titles through these rules, and `src/subject_browse_cache.py`
caches the detached results per database version. A record that is not
consistently owned by the requested subject is never shown.

## Canonical subject URLs

| URL | Page |
|---|---|
| `/subjects/<subject>` | Subject: its declared grades |
| `/subjects/<subject>/<grade>` | Grade: sections, grouped by module when a grade has several |
| `/subjects/<subject>/<grade>/<section>` | Section: topics, review questions, test action |
| `/subjects/<subject>/<grade>/<section>/<topic>` | Topic: description, its questions, test action |

`<subject>` is a registry route slug. Every level is checked: the subject must
be registered, the grade declared, the section owned by that subject in that
grade, and the topic an owned member of that section. Anything else is a 404,
including another subject's section or topic and a topic with a cross-subject
membership. Subject browsing is public.

The section endpoint is still named `subject_topic_group_view` (its URL argument
`topic_group_slug`): the same URL serves a stored section or, for a grade
without stored sections, a Physics placeholder group.

Legacy URLs:

- `/grade/<n>` and `/section/<slug>` remain the IT-only pages and are where IT
  class links point (`legacy_grade_routes`). `/section/<slug>` still permits
  the known curriculum-less section without assigning it an IT owner.
- `/physics` and `/physics/grade/<n>` are permanent (301) redirects to
  `/subjects/physics` and `/subjects/physics/<n>`; an undeclared grade is a 404.

### Section and topic slugs in URLs

The reviewed importer namespaces new section and topic slugs as
`<subject id>-g<grade>-...`. URLs do not repeat what they already say:

- Links are built with `public_slug`: a stored slug that starts with the
  namespace of the URL's own subject and grade is shortened
  (`physics-g8-mechanics` → `/subjects/physics/8/mechanics`); any other slug
  is used as stored (all existing IT slugs, and a topic listed in a section of
  another grade).
- Lookups use `stored_slug_candidates`: the namespaced form first, then the
  exact slug. Full stored slugs therefore stay valid URLs, existing IT slugs
  are unaffected, and a Physics placeholder URL such as
  `/subjects/physics/8/mechanics` keeps working when a stored section with the
  slug `physics-g8-mechanics` replaces the placeholder.
- A subject must not own both `<namespace>x` and `x` in one grade; `x` would be
  shadowed. The importer's namespace rule prevents this for new content.

## Shared page components

There are no per-subject templates or styles. The generic pages are
`subject.html`, `subject_grade.html`, `subject_section.html` and
`subject_topic.html`. Three partials are shared with the legacy IT section
page (`section.html`), so review features exist once:

- `_question_cards.html`: the read-only question list.
- `_section_toolbar.html`: the review banner with the test action, the
  search/filter toolbar and the `section-tools.js` include.
- `_subject_states.html`: preparation hero, status panel and empty note.

The toolbar's bookmark key is the stored section slug on section pages (the IT
legacy page and the generic page of one section share bookmarks) and
`<section slug>/<topic slug>` on topic pages.

A topic page filters its section's cached topics and questions; it runs no
query of its own. A topic description is shown only when it is prose; vault
link residue such as `- [[moc]]` is never displayed.

## Assessments are section-scoped

`src/subject_assessment.py` scopes new assignment selection. The selected
section supplies the subject; its MC and accepted-text open candidates must
have consistent question ownership and membership in that section. New mixed
plans are validated before persistence. The teacher form defaults to IT for old
links and can select any registered subject (buttons appear for subjects with
sections); unknown or mismatched subject requests are rejected.

The "Създай тест от раздела" action on section and topic pages opens
`/teacher/new?subject=<route slug>&section=<stored section slug>`. A test
created from a topic page draws from the whole section: `quiz_assignments`
references a section only and there are no topic-scoped assignments.

Existing persisted plans, metadata-only edits, duplication's verbatim snapshot
copy, and started attempt snapshots are not regenerated. The IT-only DZI
training generator excludes non-IT and unclassified questions, even when class
membership matches. Only `it` carries the `dzi` flag, which gates the DZI
homepage entry, practical-task panel and "ДЗИ източник" wording.

## Placeholders and status

Being registered does not imply content. Availability is derived, not stored:
a subject is `active` when it owns at least one section, in a declared grade,
whose class equals its curriculum grade (`subject_content_grades`); otherwise it
is `in_preparation`. Navigation and the teacher subject buttons use this one
cheap, section-based status. If the database cannot be read, every subject
degrades to `in_preparation`.

Pages that already know question counts distinguish three presentations:

| Stored content | Homepage card | Subject and grade pages |
|---|---|---|
| No sections | "Бъдещ предмет", inactive | Preparation shells for every declared grade |
| Sections, no questions | "Предмет в подготовка", class links, "Въпросите са в подготовка" | Browsable sections and topics; no review tools or test action |
| Sections and questions | "Активен предмет", counts | Full review and test creation |

Physics keeps navigation-only placeholder topic groups in the registry. They
are shown for a grade only while that grade has no stored sections, and retire
per grade once it does. Placeholder groups have no topics and no topic pages.
No other subject has placeholders, and new ones should not be added: stored
sections are the structure.

## Adding a subject

1. Add one `SubjectDefinition` to `SUBJECTS` in `src/subject_registry.py` (id,
   route slug, title, description, search terms, declared grades).
2. Import its curriculum structure for each grade from a reviewed file:
   `src/import_subject_structure.py` (`docs/SUBJECT_STRUCTURE_IMPORT.md`). The
   subject is then browsable as structure, with questions shown as in preparation.
3. Import reviewed questions into that structure:
   `src/import_reviewed_subject_questions.py` (`docs/REVIEWED_SUBJECT_IMPORT.md`).

No route, template, stylesheet or controller change is needed;
`tests/test_subject_generic_flow.py` registers a synthetic subject and walks it
through the homepage, navigation, subject, grade, section and topic pages and
the teacher flow to keep that true.

The registry says which subjects exist; the database says what they contain.
The structure importer writes sections and topics in file order and never
creates questions; the question importer adds text-only multiple-choice
questions to one section and topic per batch and never changes the order. Both
are dry-run by default, reject conflicts instead of overwriting, and are run
against production only as a deliberate step after a fresh backup.

No real curriculum is included: the repository ships no structure file, and no
Physics or other curriculum decisions are made by this architecture.
