# Subject identity and content ownership

`src/subject_content.py` defines the content identities `it` and `physics`.
Stored `IT`, `informatika_it`, and `Информационни технологии` resolve to
`it`; the existing `/subjects/information-technologies` slug also resolves to
`it`. `informatika_it` is legacy IT/DZI data, not the future Informatics
placeholder. `physics` is the canonical key for future Physics content.
Unknown values have no fallback. Other subjects shown in navigation are
placeholders, not registered content identities in this contract.

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

Subject-scoped browsing and counts remain F2; assignment/question pools remain
F3; reviewed non-DZI import tooling remains F4. This contract adds no Physics
questions, migrations, tables, or curriculum decisions.
