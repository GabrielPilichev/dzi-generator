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
copy, display order, supported grades, the educational paths of each grade,
legacy aliases, and the `dzi` flag. Homepage cards, navigation, search text,
subject routes, the grade overview, the teacher form and the reviewed importer
all derive from it.

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

The registry never describes curriculum content. It does record which grades a
subject is officially studied in and on which path (see "Grade applicability"),
and Physics' navigation-only `placeholder_topic_groups` (see "Placeholders and
status").

## Grade applicability

Three separate questions are answered in three separate places:

| Question | Answered by |
|---|---|
| Which educational paths does the subject have in this grade? | `SubjectDefinition.grade_paths` (registry) |
| Does LearnPilot support the grade (routes, imports), and on which path? | `SubjectDefinition.grades` and `supported_path()` (registry, product scope) |
| Is there content for it? | The database (`subject_content_grades`), never the registry |

`grade_paths` maps a grade to a tuple of paths: `general` (общообразователна
подготовка), `profiled` (профилирана подготовка), or both. A grade 8-12 with no
entry is one where the subject has neither path. `grades` must be a subset of
the grades that have a path.

A supported grade with one path is supported on that path. Where a supported
grade has both, `supported_paths` names the one LearnPilot covers; the supported
path may therefore be a subset of the applicable paths. It is declared only for
those grades and nowhere else.

`framework_plan_dependent_grades` lists grades whose general education exists
only under some framework plans (marked `G*` below). It is a qualification of
the general path, not another path.

`validate_registry` rejects a supported grade without a path, a supported
grade with two paths and no declared one, a supported path that the grade does
not have, and a framework-plan-dependent grade without a general path.

| id | 8 | 9 | 10 | 11 | 12 | Supported grades | Supported path |
|---|---|---|---|---|---|---|---|
| `it` | G | G | G | P | P | 8-12 | general 8-10, profiled 11-12 |
| `physics` | G* | G | G | P | P | 8-10 | general |
| `informatics` | G* | — | — | P | P | 8 | general |
| `biology` | G* | G | G | P | P | 8-10 | general |
| `mathematics` | G | G | G | G+P | G+P | 8-12 | general (also in 11-12) |
| `chemistry` | G* | G | G | P | P | 8-10 | general |

Notes on the matrix:

- **`G*`, grade 8.** Physics, Biology, Chemistry and Informatics have general
  education in grade 8 only under some framework plans. No single framework
  plan describes every subject: Informatics has general-education hours only in
  the plan with intensive foreign-language study and the mathematics/science
  profiles, while that same family of plans gives Physics, Biology and
  Chemistry no grade 8 hours.
- **Informatics.** Grade 8 is valid but not universal; grades 9-10 have no
  general and no profiled programme; 11-12 are profiled.
- **Mathematics 11-12** have both paths. LearnPilot supports the general one;
  the profiled one is outside its scope.
- **IT 11-12** are profiled and supported: the existing ДЗИ content lives there.

How each case is presented on the subject page (`subject.html`,
`_subject_states.html`), from registry values only:

| Case | Presentation | Route |
|---|---|---|
| Supported, general path | Grade card, `subject-class-card--general` (primary orange) | Grade page |
| Supported, profiled path | Grade card, `subject-class-card--profiled` (success green), label „Профилирана подготовка“ | Grade page |
| Framework-plan dependent | The same card plus the neutral label „Изучава се в зависимост от учебния план“; the subject's grade page says „Предметът се изучава в N. клас само при определени учебни планове.“ | Grade page |
| Profiled path not supported | One muted green note („11.–12. клас — профилирана подготовка“), never a link or card | None; 404 unless the grade is supported on its general path |
| No path | Omitted | 404 |

Colour means path and nothing else: orange is general, green is profiled. It
never depends on the grade number and never on whether content exists. Content
state is the badge („В подготовка“, „N въпроса“), which is neutral; nothing
else in the subject and grade pages uses green. A grade has one card and one
route whatever its paths: Mathematics 11 is one general card plus the note.

### Importer contract

The reviewed importers check `subject_supports_grade`, i.e. `grades` only. They
do not compare a batch's or structure file's `education_type` with
`grade_paths` or with the supported path. `grades` controls what may be
imported; the path metadata drives curriculum semantics and the UI. Keeping an
imported curriculum on the supported path is the reviewer's responsibility.

### Sources for the grade paths

Recorded 2026-10-05; the matrix above is the result of an independent review
of that research. The Physics content itself follows the programmes for plans
without intensive foreign-language study
(`docs/reviews/physics_g9_curriculum_structure_review.md`); the matrix does not
assume that variant for every subject.

- **Наредба № 5 от 30.11.2015 г. за общообразователната подготовка**, МОН
  consolidated text of 30.01.2026
  (<https://mon.bg/nfs/2026/01/nrdb5-2015_oop_izm012026_30012026.pdf>). Its
  per-subject appendices define results by stage: Математика (Приложение № 3)
  has both „първи гимназиален“ (VIII-X) and „втори гимназиален“ (XI-XII);
  Информатика (№ 6), Информационни технологии (№ 7), Биология и здравно
  образование (№ 15), Физика и астрономия (№ 16) and Химия и опазване на
  околната среда (№ 17) end at „първи гимназиален“. Mathematics is also a
  profiling subject (Наредба № 7), hence both paths in XI-XII. Чл. 4, ал. 2 makes
  Informatics in that stage an exception whose presence depends on the
  framework plan.
- **Наредба № 7 от 11.08.2016 г. за профилираната подготовка**
  (<https://www.mon.bg/regulation/naredba-№-7-ot-11-avgust-2016-g-za-profiliranata-podgotovka/>;
  text read from a mirror, the МОН page blocks automated requests). Чл. 2:
  profiled preparation is acquired in the second gymnasium stage; all six
  subjects are profiling subjects; чл. 3, ал. 4 gives the hours for XI and XII
  клас. МОН's profiled Informatics programme
  (<https://www.mon.bg/nfs/2018/12/profil-informatika.pdf>) confirms modules
  per XI/XII клас.
- **МОН draft amending orders of 27.07.2023** for the general-education
  programmes of VIII, IX and X клас
  (<https://www.mon.bg/nfs/2023/07/pr_8-zapup8_27072023.pdf>,
  `pr_9-zapup9_27072023.pdf`, `pr_10-zapup10_27072023.pdf`). They go through
  every appendix of the base orders: Informatics is Приложение № 15 for VIII
  клас only; the IX and X клас orders have Information Technologies, Biology,
  Physics, Chemistry and Mathematics but no Informatics programme.
- **Наредба № 4 от 30.11.2015 г. за учебния план** (framework plans; read from
  a mirror, extracted tables are not clean). Informatics has general-education
  hours (72) in one plan only, Приложение № 3 (profiled education with
  intensive foreign-language study, profiles „Математически“, „Софтуерни и
  хардуерни науки“, „Икономическо развитие“, „Природни науки“). In the
  intensive-language plans Biology, Physics and Chemistry have no hours in
  VIII клас and more in IX клас.

Decisions that are product scope, not quotations:

- Which grades and which path LearnPilot supports. Only IT has profiled
  content today; every other profiled path is outside scope.
- Vocational programmes are not modelled.

Not verified: МОН's live programme listing pages and the signed orders (the
pages return HTTP 403 to automated requests).

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

Each subject declares the grades it supports (see "Grade applicability"). A
supported grade without content is an intentional preparation page; an
unsupported grade is a 404, and the importer rejects it.

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
| `/subjects/<subject>` | Subject: its supported grades, and a note for profiled grades outside scope |
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

Grade overview:

- `/grade/<n>` (8-12) lists the subjects LearnPilot supports in that grade:
  every registry subject whose `grades` contain it, in registry order, as the
  same path-coloured cards as the subject page, each linking to
  `/subjects/<subject>/<n>`. A supported subject without content is listed as
  „В подготовка“. Subjects whose profiled path in that grade is outside scope
  are named in one note and never linked. The page shows no sections and
  belongs to no subject; the navigation entry „Класове“ leads here.

Legacy URLs:

- `/section/<slug>` remains the IT-only section page (it still permits the
  known curriculum-less section without assigning it an IT owner). It links
  back to `/subjects/information-technologies/<n>`. IT class links everywhere
  use the canonical subject URLs; until this change `/grade/<n>` was the IT
  grade page.
- `/physics` and `/physics/grade/<n>` are permanent (301) redirects to
  `/subjects/physics` and `/subjects/physics/<n>`; an unsupported grade
  (including 11 and 12) is a 404.

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
- `_subject_states.html`: preparation hero, status panel, empty note and the
  profiled-grades note.

The toolbar's bookmark key is the stored section slug on section pages (the IT
legacy page and the generic page of one section share bookmarks) and
`<section slug>/<topic slug>` on topic pages.

A topic page filters its section's cached topics and questions; it runs no
query of its own. A topic description is shown only when it is prose; vault
link residue such as `- [[moc]]` is never displayed.

## Assessment context and DZI open questions

Ordinary curriculum assessments are section-scoped: the selected section fixes
subject and grade, and MC selection follows its owned topic memberships.
The "Създай тест от раздела" action on section and topic pages opens
`/teacher/new?subject=<route slug>&section=<stored section slug>`. Tests created
from a topic page still draw from its section; assignments have no topic scope.

All current open/fill-in questions are IT **DZI assessment content**, not ordinary
Grade 8–12 question pools. Shared topics describe content taxonomy, not DZI
ownership. Neither topic overlap, a school grade, nor the subject registry's DZI
capability flag authorizes DZI open questions in an ordinary curriculum test.
No school-grade remapping of DZI questions is needed for this boundary.

`AssessmentScope` reloads the section from the database. For `/teacher/new`, only
an IT section with stored `section_type=dzi_training` authorizes DZI opens;
submitted mode, source, grade or slug cannot grant permission. Other sections
have zero DZI open candidates. The form hides the open panel in ordinary
contexts and restores it when an explicit DZI-training section is selected.
Positive open counts in an ordinary context are rejected server-side.

DZI practical tasks 26–28 and their exam resources follow the same stored
assessment context (`AssessmentScope.allows_dzi_content`): topic overlap does
not expose them in ordinary curriculum sections. The teacher form mounts their
panel only for an explicit DZI-training section; dedicated DZI catalogue,
practical-source and download routes retain their existing access semantics.

`src/dzi_open_questions.py:fetch_dzi_open_questions` is the shared generation
pool. It requires an open question with IT/DZI metadata, a matching linked
IT/DZI exam identity/year, stored accepted answers for every subquestion, and
the existing practical-task and visual-dependent generation safeguards.
Curriculum topic membership is not required for DZI opens. Optional source
filtering only narrows this pool. The form's counts, represented source options,
mixed-plan generation and plan validation all use that same eligibility rule;
answerless-only sources cannot become generation options. A requested count
above the narrowed pool is rejected. Validated plans are persisted as snapshots.

The legacy DZI DOCX exam builder uses the same ready-open pool; ordinary
topical/class worksheets do not select DZI opens.

The dedicated `/teacher/dzi-training` route is explicitly DZI. Its artifact
generator applies the same DZI open readiness helper while retaining the
existing class mix, memberships and MC/foundation composition. Unsupported open
types, answerless questions and generation-ineligible visual questions cannot
enter its pool. Duplication revalidates a mixed plan against its stored target
section and current DZI readiness before copying; invalid plans are rejected,
not silently trimmed. Duplication does not support a submitted target section.
Metadata-only title/time editing and started attempt snapshots are unchanged.

DZI ownership and generation readiness are separate from catalogue display.
`/dzi` and `/dzi/source/<source_slug>` retain legitimate exam/task displays,
including questions without accepted answers. The admin open-candidate audit
continues to show generation-ready candidates and their represented sources.
Those display routes do not use the generation filter to hide exam records.
Only `it` carries the registry's `dzi` capability flag for DZI navigation and
practical-task UI; this flag is not curriculum-form assessment authorization.

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
   route slug, title, description, search terms, `grade_paths` from the
   official programmes, the supported `grades`, `supported_paths` where a
   supported grade has both paths, and any `framework_plan_dependent_grades`).
   Record the sources above.
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

This architecture makes no curriculum decisions. Curricula come from reviewed
structure files tracked under `data/subject_structures/`, each with a review
record in `docs/reviews/` (`docs/SUBJECT_STRUCTURE_IMPORT.md`, "Structure files
in the repository"). A tracked file changes no database until it is imported.
