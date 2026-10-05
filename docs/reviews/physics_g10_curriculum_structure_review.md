# Physics, Grade 10 — curriculum structure review

Review record for `data/subject_structures/physics/grade-10.json`: where the
structure comes from, what is official wording and what is LearnPilot's own, and
what the structure file cannot hold. Prepared on 2026-10-05.

**Status: proposed.** The file has been validated with an importer dry run on a
temporary database copy only. It has not been imported into `data/questions.db`
or any other real database, and `--apply` was not run anywhere. Read this before
the first import and before any question is collected for these topics.

Two labels are used throughout: **OFFICIAL** is what the МОН programme prints;
**MAPPING DECISION** is how LearnPilot represents it.

## 1. Official source

| | |
|---|---|
| Document title | Учебна програма по физика и астрономия за X клас (общообразователна подготовка) |
| Marked on the document | Приложение № 8 (the amending order calls it „Приложение № 8 към т. 8“) |
| Issuing authority | Министерство на образованието и науката (МОН) |
| Subject | Физика и астрономия |
| Grade | X клас, първи гимназиален етап на средната образователна степен |
| Education type | Общообразователна подготовка |
| Framework-plan applicability | None stated. Unlike the two IX клас programmes, the title names no framework curricula. |
| Annual hours | „определеният годишен брой часове в прилагания в училището рамков учебен план“; before the 2023 amendment: 72 (draft amending order, VIII.3) |
| URL | <https://www.mon.bg/nfs/2023/12/up_x_fa.pdf> |
| Retrieved | 2026-10-05, 18 pages, 429 496 bytes |
| SHA-256 | `3cc865919949dc643e164003d71e6e6f21ce6f709f26e30606eaf6f89e193de1` |
| Document date | PDF created 2023-12-11; server `Last-Modified` 2024-02-28. The document prints no date or version of its own. |

Approval — what was verified and how:

| Fact | Level of verification |
|---|---|
| Base order: **Заповед № РД 09-30 от 11.01.2018 г.** „за утвърждаване на учебните програми за общообразователната подготовка в X клас“; Physics and Astronomy is its Приложение № 8 към т. 8 | Verified from МОН's own **draft** amending order (<https://www.mon.bg/nfs/2023/07/pr_10-zapup10_27072023.pdf>, section VIII; SHA-256 `ef508877…d9b30f`). The signed 2018 order was not retrieved. |
| Amendment: **Заповед № РД09-3717/15.11.2023 г.** (публ. 29.11.2023 г.) | Verified from an **archived copy of the official МОН page** „X клас от учебната 2024/2025“ (Wayback Machine snapshot 2024-04-23 of <https://www.mon.bg/obshto-obrazovanie/uchebni-planove-i-programi-2/uchebni-programi-v-sila-ot-uchebnata-2024-2025-godina/x-klas-ot-uchebnata-2024-2025/>): „Учебни програми за X клас, утвърдени със Заповед РД09-30 /11.01.2018 г., изменена и допълнена със Заповед № РД09-3717/15.11.2023 г. на министъра на образованието и науката и влизат в сила от учебната 2024/2025 година (публ. 29.11.2023 г.)“. The signed order was not retrieved. |
| The PDF is the one МОН links | Same archived page: the single link „Физика и астрономия“ points to `/nfs/2023/12/up_x_fa.pdf`. |
| The PDF is the amended text | Verified from the PDF itself: it contains the new part „Основни формули за физични величини и закономерности в учебната програма“, the sentence that annual hours follow the framework curriculum, and the reworded results in 1.1, 1.2, 3.1 and 3.2 that section VIII of the draft order introduces. |
| Current live МОН page | **Not read.** <https://www.mon.bg/obshto-obrazovanie/uchebni-planove-i-programi-2/uchebni-programi/uchebni-programi-za-obsthoobrazovatelna-podgotovka/x-klas/> returns HTTP 403 (browser challenge) to automated requests and has no archived copy. It should be opened in a browser to confirm that no order later than РД09-3717 is listed (section 8, point 1). |
| Cross-check (third party, not an authority) | <https://www.prirodninauki.bg/archives/20136> lists one X клас programme against two for IX клас (36 and 90 hours); its copy of `up_x_fa.pdf` is byte-identical to МОН's. |

Per the draft order the amendment changes no theme or sub-theme title. It moves
formulas out of „Нови понятия“ into the new formulas part, rewords results in
1.1, 1.2, 1.4, 3.1 and 3.2, removes one result from 1.2 (magnetic domains) and
replaces the fixed 72 annual hours. The 2018 text of Приложение № 8 was not
retrieved.

## 2. Programme variants

**OFFICIAL — one programme.** Grade 10 has a single general-education Physics
and Astronomy programme. Evidence:

- the draft amending order goes through every appendix of Заповед № РД 09-30
  (№ 1–12); only section VIII (Приложение № 8) is Physics and Astronomy, where
  the IX клас order has two (Приложения № 9 and № 10);
- the archived official page lists „Физика и астрономия“ once, with no
  „годишно – N часа“ qualifier;
- the programme title carries no framework-plan qualifier, and after the
  amendment it defers the hours to whichever framework curriculum the school
  applies. Different framework plans therefore change the **hours**, not the
  content.

| Candidate variant | Finding |
|---|---|
| Standard vs intensive foreign-language programme | Does not exist for X клас. The intensive-language case is absorbed in IX клас (90-hour Приложение № 10 = Grade 8 + Grade 9 content); by X клас both tracks use this document. |
| Framework-plan variants | Hours only (see above). The hours per framework plan were not retrieved; the framework-plan page is behind the same browser challenge. |
| Приложение № 12 of РД 09-30, revoked by the draft order (XII) | Not identified from the draft. The archived page lists Физическо възпитание и спорт as separately approved (Заповед № РД09-2242/05.09.2023 г.), which is the likely explanation — an inference, not verified. Nothing suggests it is a Physics programme. |
| Профилирана подготовка | XI–XII клас only; not Grade 10. |

**MAPPING DECISION.** Приложение № 8 is the canonical and only Grade 10
structure. Nothing was merged and no variant content from another grade is
involved.

## 3. How the programme is organised

| Part of the document | What it is | Used for |
|---|---|---|
| Кратко представяне на учебната програма | introduction | nothing |
| Очаквани резултати … в края на класа | five **области на компетентност**: Електричество и магнетизъм; Трептения и вълни; Светлина; От атома до Космоса; Наблюдение, експеримент и изследване | not structure |
| **Учебно съдържание** — column „Теми“ | three numbered themes with 5 + 3 + 5 numbered sub-themes | **sections and topics** |
| Учебно съдържание — „Компетентности като очаквани резултати от обучението“, „Нови понятия“ | expected results and new concepts per sub-theme | topic scope (appendix) |
| Основни формули за физични величини и закономерности | formulas per sub-theme | topic scope (appendix) |
| Препоръчителни уроци за практически дейности (лабораторни работи) | seven recommended lab lessons | collector guidance only |
| Препоръчително процентно разпределение на задължителните учебни часове | share of hours by lesson type; **no hours per theme** | nothing |
| Оценяване; дейности за ключовите компетентности; междупредметни връзки | methodological guidance | nothing |

Only the „Теми“ column defines the hierarchy. It is an explicit two-level
structure, mapped exactly as in Grades 8 and 9: each numbered theme is a
**section**, each numbered sub-theme is a **topic**, in the programme's order.

The competence areas are not themes. Two of them share a name with a theme
(„Светлина“, „От атома до Космоса“); the first theme „Електромагнитни явления“
spans two areas („Електричество и магнетизъм“ for 1.1–1.4, „Трептения и вълни“
for 1.5). The theme names are used because they are what „Учебно съдържание“
prints. No textbook table of contents or lesson plan was used.

## 4. Mapping

| Official no. | OFFICIAL heading (verbatim) | MAPPING DECISION | Stored slug |
|---|---|---|---|
| 1. | Електромагнитни явления | section 1 | `physics-g10-electromagnetic-phenomena` |
| 1.1. | Електрично поле | topic 1 | `physics-g10-electric-field` |
| 1.2. | Магнитно поле | topic 2 | `physics-g10-magnetic-field` |
| 1.3. | Движение на заредени частици в електрично и в магнитно поле | topic 3 | `physics-g10-charged-particle-motion` |
| 1.4. | Електромагнитна индукция. Променлив ток | topic 4 | `physics-g10-electromagnetic-induction-alternating-current` |
| 1.5. | Електромагнитни вълни | topic 5 | `physics-g10-electromagnetic-waves` |
| 2. | Светлина | section 2 | `physics-g10-light` |
| 2.1. | Разпространение на светлината | topic 1 | `physics-g10-light-propagation` |
| 2.2. | Вълнови свойства на светлината | topic 2 | `physics-g10-wave-properties-of-light` |
| 2.3. | Квантови свойства на светлината | topic 3 | `physics-g10-quantum-properties-of-light` |
| 3. | От атома до Космоса | section 3 | `physics-g10-from-atom-to-cosmos` |
| 3.1. | Атоми и атомни преходи | topic 1 | `physics-g10-atoms-and-atomic-transitions` |
| 3.2. | Атомно ядро | topic 2 | `physics-g10-atomic-nucleus` |
| 3.3. | Ядрени реакции | topic 3 | `physics-g10-nuclear-reactions` |
| 3.4. | Частици | topic 4 | `physics-g10-particles` |
| 3.5. | Звезди, галактики, Вселена | topic 5 | `physics-g10-stars-galaxies-universe` |

Totals: 3 sections, 13 topics.

**Official wording.** All 16 titles are the official headings. Nine occur as
continuous strings in the extracted PDF text. Seven are wrapped over several
lines inside their table cells in both tables (1.3, 1.4, 2.1, 2.2, 2.3, 3.1,
3.5) and were confirmed by reading the cells; 1.4 and 3.1 are also quoted
continuously in the draft order. Numbering is not part of the title: the
importer takes order from position in the file.

**LearnPilot's own, not official:**

| Item | Value | Why |
|---|---|---|
| Subject id | `physics` | Registry id; the official subject is „Физика и астрономия“. |
| `education_type` | `general` | The value stored curricula already use for общообразователна подготовка. |
| Curriculum title | „Физика и астрономия, 10. клас — общообразователна подготовка“ | Same pattern as Grades 8 and 9. The official document title is in section 1. |
| Slugs | table above | English, semantic, namespaced `physics-g10-`. Two are shortened from the heading: `charged-particle-motion` drops „в електрично и в магнитно поле“, `light-propagation` follows English word order. |

**No extra pedagogical decomposition was invented.** There are exactly as many
sections as numbered themes and topics as numbered sub-themes; no expected
result, concept, formula or lab lesson was promoted to a topic.

## 5. Anomalies in the official PDF

None affects a heading.

- The header reads „Приложение № 8“, without „към т. 8“.
- 1.1, second result, is cut off: „…и пресмята интензите.“ The draft order
  gives the intended text: „…и пресмята интензитета на електрично поле на
  точков заряд.“
- 1.4, first result: the PDF has „Описва и провежда различни групи опити за
  получаване на индуциран ток“, the draft „Описва различни групи опити…“. The
  signed order evidently differs from the draft here; the PDF is followed.
- The formulas table has no rows for 1.3, 3.3 and 3.4 (no formulas), so its
  numbering skips them. Unlike the Grade 9 PDF, the numbering matches
  „Учебно съдържание“.
- Typos: „Итензитет“, „Препоръчитерни уроци“, an unclosed bracket in 2.3
  („фотони с точно определена енергия.“), a missing full stop in 3.2. In the
  extracted text several formulas lose symbols (φ, λ, ν); they were read with
  the draft order alongside.
- Entry into force: the draft says from 2025–2026 for pupils entering X клас;
  the archived official page says from 2024/2025. Not relevant to structure.

## 6. Continuity with Grades 8 and 9

Read against `grade-8.json` (Механика; Топлинни явления) and `grade-9.json`
(Електричен ток; Механично движение). Neither file was changed.

| Grade 10 | Relation | Note |
|---|---|---|
| 1.1 Електрично поле, 1.2 Магнитно поле, 1.3 | Legitimate continuation | The programme: „тук се изучава за първи път полевата форма на материята“. Grade 9 treats current and circuits without fields. 1.1 reuses напрежение, 1.2/1.3 reuse force and energy from Grade 8. |
| 1.4 Електромагнитна индукция. Променлив ток | Continuation | Builds on Grade 9 „Електрична енергия“ (ЕДН, power) and on period/frequency/amplitude from Grade 9 „Трептения“. Effective values, generator, transformer are new. |
| 1.5 Електромагнитни вълни | Intentional deepening | Explicitly contrasted with Grade 9 „Механични вълни“; the relation c = λν repeats u = λν for a new kind of wave. Questions on mechanical waves stay in Grade 9. |
| 2. Светлина; 3. От атома до Космоса | Intentional revisiting of основна степен | The programme says light, the structure of the atom and nucleus, and some astronomical objects were met „на по-ниско равнище още в основната степен“ and are deepened here. That is below Grade 8, outside LearnPilot's current Physics range. |

No Grade 10 heading repeats a Grade 8 or Grade 9 heading, and no apparent
duplication was found. No content belongs to a programme variant.

Existing Grade 10 placeholders in `src/subject_registry.py` (`mechanics`,
`thermal-processes`, `electric-phenomena`, `magnetic-phenomena`) do not match
the programme: mechanics and thermal phenomena are Grade 8, and electric and
magnetic phenomena are one theme here with light and atomic/nuclear physics
missing altogether. As for Grade 9, placeholders retire once the grade has
stored sections; no placeholder slug equals a new bare section slug, so all four
URLs would become 404 after import. This was not rehearsed (no `--apply`).

## 7. What the structure file cannot hold

The importer stores the curriculum title, `education_type` and `source_url`.
Order numbers, appendix number, authority, hours, document date, retrieval date
and hash have no field in the format and are recorded only here. Deliberately
not in the file: topic descriptions, topic scope, competence areas, lab lessons.

Recommended lab lessons (the lessons are OFFICIAL; the assignment to topics is a
MAPPING DECISION):

| # | Препоръчителен урок (verbatim, typo in the heading aside) | Topic |
|---|---|---|
| 1 | Изследване на явлението електромагнитна индукция | 1.4 |
| 2 | Изследване на трансформатор | 1.4 |
| 3 | Определяне на показателя на пречупване на вода | 2.1 |
| 4 | Определяне на показателя на пречупване на прозрачна пластина чрез пълно вътрешно отражение | 2.1 |
| 5 | Определяне на дължината на светлинна вълна с дифракционна решетка | 2.2 |
| 6 | Наблюдаване и изследване на спектри | 3.1 (also 2.1/2.2) |
| 7 | Регистриране на йонизиращи лъчения | 3.2 |

## 8. Validation performed

`data/questions.db` was only read. The importer opens its target read-write even
for a dry run, so the dry run was made on a temporary copy taken with SQLite
`.backup` through a read-only connection. SHA-256 of `data/questions.db`
(`5d01b031…fb2e84`) and `vault/.obsidian/graph.json` (`2fc6d662…f7bdc6`) were
identical before and after.

Command: `python3 -B -m src.import_subject_structure --db COPY --input
data/subject_structures/physics/grade-10.json` (no `--apply`).

| Check | Result |
|---|---|
| File accepted by the importer's validation | pass |
| Dry run on a copy of the developer database | Curricula: 1 to create, 0 to update, 0 unchanged. Sections: 3 / 0 / 0. Topics: 13 / 0 / 0. „Questions: none are created or changed by this importer“. No conflicts. |
| Copy after the dry run | byte-identical; still 7 curricula, 51 sections, 143 topics, 1155 questions, no `physics-g10-*` row |
| Grades 8 and 9 | not in the plan; untouched |

Existing tests: `tests.test_import_subject_structure`,
`tests.test_subject_registry` and `tests.test_subject_topic_pages` — 82 tests,
4 failures and 1 error, the same set recorded in the Grade 9 review. They build
fixtures from `data/questions.db`, whose local state already holds Physics rows
(`UNIQUE constraint failed: curriculum_sections.section_slug`, „seed database
has content for IT only“). No test reads the Grade 10 file. Not re-run against a
clean seed, because restoring the database was out of scope.

## 9. Open points for the reviewer

1. **Current МОН page not read.** Approval and amendment come from the archived
   2024-04-23 copy of the official page and from the draft order. Open the live
   „X клас“ page in a browser and confirm РД09-3717/15.11.2023 г. is still the
   latest amendment and the Physics link is still `up_x_fa.pdf`.
2. **Signed orders not retrieved** (РД 09-30 and РД09-3717). The draft differs
   from the published programme in at least one result (1.4).
3. **Hours per framework plan not retrieved.** They do not affect structure.
4. **Topic granularity.** Thirteen topics of uneven size: 1.3 and 3.4 are small
   (four and three results, no formulas); 1.1 and 3.5 are broad. Decide before
   questions are imported: the importer appends topics but never renames or
   moves them.
5. **`docs/SUBJECT_STRUCTURE_IMPORT.md`** lists the tracked structure files and
   has rows for Grades 8 and 9. A Grade 10 row was not added, because this task
   was scoped to two files.

## 10. Files changed

- `data/subject_structures/physics/grade-10.json` (new)
- `docs/reviews/physics_g10_curriculum_structure_review.md` (new, this file)

## Appendix: official scope of each topic

Condensed from the programme's „Учебно съдържание“ and „Основни формули“
tables, for question collectors: a question belongs to a topic only if it tests
one of these results at this level. Formulas are transcribed as plain text.

### 1. Електромагнитни явления

**1.1. Електрично поле**

- Формулира и прилага закона на Кулон (само за два точкови заряда).
- Определя електричното поле като посредник на електричното взаимодействие и пресмята интензитета на електрично поле на точков заряд (PDF text cut off, see section 5).
- Дефинира интензитета и потенциала като основни характеристики на електростатичното поле.
- Определя напрежението като разлика в потенциалите на две точки от полето.
- Изразява силата, действаща на точков заряд, и потенциалната енергия на заряда чрез интензитета и потенциала на полето.
- Онагледява полето чрез силови линии; чертае силови линии на полето на точков заряд и на еднородно поле.
- Прилага връзката между интензитет и напрежение в еднородно поле (за две точки на една и съща силова линия).
- Описва качествено електростатична индукция и електростатично екраниране, с приложения.
- Определя кондензаторите като устройства за временно съхраняване на заряди и енергия, пресмята капацитет, дава примери за приложение.
- Обяснява качествено поляризацията на диелектриците, с приложения.

Нови понятия: точков заряд; закон на Кулон; пробен заряд; електрично поле; интензитет; интензитет на електрично поле на точков заряд; потенциал; напрежение; силови линии; еднородно поле; електростатична индукция; капацитет на кондензатор; фарад; поляризация.
Формули: F = k·q1·q2/r²; E = F/q0; φ = W/q0; E = k·q/r²; U = φ2 – φ1; E = U/d; C = q/U.

**1.2. Магнитно поле**

- Разпознава на схема и определя посоката на индукционните линии на полето на прав магнит.
- Дефинира магнитната индукция чрез максималната магнитна сила, действаща на движещ се пробен заряд.
- Определя големината и посоката на максималната магнитна сила върху движеща се заредена частица и върху праволинеен проводник с ток в еднородно магнитно поле.
- Описва принципа на действие на електромотора.
- Обяснява опита на Оерстед.
- Обяснява (без формули) от какво зависи магнитното поле на проводници с ток.
- Разпознава на схема индукционните линии на дълъг праволинеен проводник, кръгов проводник и соленоид и определя посоката им.
- Класифицира материалите на диа-, пара- и феромагнитни; знае, че феромагнитните над определена температура стават парамагнитни; посочва приложения на феромагнитните материали.

Нови понятия: магнитно поле; индукционни линии; магнитна индукция; тесла; магнитна сила; електродвигател; диа-, пара- и феромагнетизъм.
Формули: Fmax = qvB; Fmax = IℓB.

**1.3. Движение на заредени частици в електрично и в магнитно поле**

- Описва енергетично движението на заредени частици в електрично поле.
- Описва качествено движението на заредени частици в еднородно магнитно поле.
- Посочва приложения на снопове ускорени заредени частици в техниката – ускорители и др.
- Разбира значението на земното магнитно поле за предпазване на Земята от космически лъчения.

Нови понятия: електронволт (eV).
Формули: няма.

**1.4. Електромагнитна индукция. Променлив ток**

- Описва и провежда различни групи опити за получаване на индуциран ток.
- Дава определение на явлението електромагнитна индукция.
- Формулира качествено закона на Фарадей (без правилото на Ленц).
- Описва действието на генератор на променливо напрежение.
- Определя по графика период, честота и амплитуда на променливото напрежение (ток).
- Пресмята ефективни стойности и средна мощност на променлив ток.
- Описва действието на повишаващ и понижаващ трансформатор.
- Разбира защо на големи разстояния електричната енергия се пренася при високо напрежение.
- Обяснява по схема електропреносната мрежа.

Нови понятия: електромагнитна индукция; индуциран ток и ЕДН; електрически генератор; променлив ток (напрежение); ефективни стойности на ток (напрежение); трансформатор.
Формули: I = Imax/√2; U = Umax/√2; U1/U2 = N1/N2; I2/I1 = N1/N2.

**1.5. Електромагнитни вълни**

- Знае, че променливите електрични и магнитни полета са свързани и могат да се пораждат взаимно.
- Описва основни свойства на плоска електромагнитна вълна – напречен характер, скорост на разпространение.
- Характеризира вълната с честота (период) и дължина на вълната и прилага връзката между тях.
- Знае, че електромагнитните вълни, за разлика от механичните, се разпространяват и във вакуум, където скоростта им не зависи от честотата и е максималната скорост в природата.
- Изброява основните диапазони от електромагнитния спектър и характерни източници.
- Дава примери за съвременни приложения на радио- и микровълните.

Нови понятия: електромагнитно поле; електромагнитна вълна; радиовълни; микровълни; инфрачервени лъчи; ултравиолетови лъчи; рентгенови лъчи; гама-лъчи.
Формули: c = λν = λ/T.

### 2. Светлина

**2.1. Разпространение на светлината**

- Определя светлината като електромагнитна вълна в определен интервал от дължини на вълната.
- Знае, че скоростта на светлината в материална среда е по-малка от тази във вакуум, и дефинира показател на пречупване.
- Разбира, че при преминаване от една среда в друга честотата не се променя, а дължината на вълната се променя.
- Формулира и прилага законите за отражение и пречупване на светлината.
- Посочва условията за пълно вътрешно отражение и дава примери за приложението му.
- Дава определение за дисперсия на светлината и посочва примери (разлагане с призма, небесна дъга).

Нови понятия: показател на пречупване; пълно вътрешно отражение; закон на Снелиус; граничен ъгъл; оптично влакно; дисперсия на светлината.
Формули: n = c/u; α1 = α1′; n1·sinα1 = n2·sinα2.

**2.2. Вълнови свойства на светлината**

- Дава определения за интерференция и дифракция на светлината.
- Разбира, че дифракция се наблюдава при всички видове вълни, когато размерът на преградите или процепите е съпоставим с дължината на вълната.
- Обяснява дифракцията чрез принципа на Хюйгенс.
- Описва по схема опита на Юнг.
- Формулира условията за интерференчен максимум и минимум (без понятието кохерентност).
- Описва принципа на действие на дифракционна решетка (без формула) и нейни приложения.

Нови понятия: интерференция; условия за интерференчен максимум и минимум; дифракция; дифракционна решетка; дифракционен спектър.
Формули: Δr = mλ; Δr = (m + 1/2)λ.

**2.3. Квантови свойства на светлината**

- Описва графиката на спектъра на топлинно излъчване на абсолютно черно тяло при различни температури.
- Формулира и прилага законите на Стефан и на Вин.
- Описва основните закономерности при фотоефекта и дава примери за приложението му.
- Знае, че светлината се излъчва, разпространява и поглъща на кванти (фотони) с точно определена енергия.
- Прилага уравнението на Айнщайн за фотоефекта.

Нови понятия: абсолютно черно тяло; закон на Стефан; закон на Вин; светлинни кванти (фотони); константа на Планк; енергия на фотон; отделителна работа; уравнение на Айнщайн.
Формули: P = σST⁴; λmax·T = const; E = hν; hν = Ae + Ek,max.

### 3. От атома до Космоса

**3.1. Атоми и атомни преходи**

- Характеризира микрочастиците с вълни на Дьо Бройл; дава примери за вълнови свойства на частиците (дифракция на електрони).
- Описва на качествено равнище строежа на електронната обвивка на атома.
- Определя енергията на излъчения (погълнатия) фотон като разлика от енергиите на началното и крайното състояние на атома.
- Обяснява линейния спектър на водородния атом и останалите химични елементи с атомните преходи.
- Дава примери за луминесцентно излъчване и приложенията му.
- Описва най-важните условия за генериране на лазерно лъчение и дава примери за приложения на лазерите.
- Описва качествено излъчването на рентгенови лъчи и техни приложения.

Нови понятия: вълна на Дьо Бройл; квантови състояния (енергетични нива) на атомите; луминесценция; спонтанно и стимулирано излъчване; инверсна населеност; рентгенова тръба; спирачно и характеристично рентгеново лъчение.
Формули: λ = h/(mv).

**3.2. Атомно ядро**

- Описва качествено основни характеристики на атомните ядра и на ядрените сили.
- Характеризира ядрата с енергия на връзката и с масов дефект.
- Разграничава радиоактивните ядра по период на полуразпадане и определя стойността му от графика (без формулата на закона за радиоактивното разпадане).
- Описва биологичното действие на йонизиращите лъчения и разграничава погълната доза (Gy) и еквивалентна доза (Sv).
- Описва измененията в ядрата при алфа-, бета- и гама-разпадане.
- Дава примери за използване на радиоактивни изотопи (медицина, датиране).

Нови понятия: енергия на връзката (ΔE); специфична енергия на връзката; връзка между енергия и маса; масов дефект (Δm); период на полуразпадане; неутрино и антинеутрино.
Формули: ΔE = Δm·c².

**3.3. Ядрени реакции**

- Дава примери за ядрени реакции; записва реакцията на делене на урана.
- Обяснява по схема принципа на действие на ядрения реактор.
- Изброява мерки за радиационна защита на ядрените централи.
- Обяснява защо при ядрен синтез на леки ядра се отделя енергия.
- Изброява най-важните условия за управляем термоядрен синтез.
- Дискутира проблеми и перспективи пред термоядрената енергетика.

Нови понятия: обогатен уран; критична маса; активна зона на реактор; термоядрен синтез.
Формули: няма.

**3.4. Частици**

- Знае, че елементарните частици се разделят на лептони и кварки, а всяка частица има античастица.
- Дава примери за частици, изградени от кварки (адрони – бариони и мезони).
- Изброява фундаменталните взаимодействия и техните носители и ги подрежда по сила.

Нови понятия: лептони; кварки; античастици; адрони, бариони и мезони; силно взаимодействие; слабо взаимодействие.
Формули: няма.

**3.5. Звезди, галактики, Вселена**

- Описва по схема ядрения синтез в звездите (протон-протонен цикъл).
- Разграничава видове звезди според положението им върху диаграмата „спектър – светимост“ и определя светимост на звезда.
- Обяснява как протича еволюцията на звездите в зависимост от масата им.
- Описва на елементарно равнище структурата на Вселената.
- Изброява основни факти за Вселената (разширяване, еднородност, фоново лъчение, тъмна материя и тъмна енергия).
- Определя разстояние до галактики по закона на Хъбъл.
- Описва на елементарно равнище съвременната теория за възникването и развитието на Вселената (Голям взрив).

Нови понятия: светимост; главна последователност; протозвезда; звезди гиганти, свръхгиганти и джуджета; бели джуджета; неутронни звезди; черни дупки; закон на Хъбъл; фоново (реликтово) лъчение.
Формули: L = 4πR²σT⁴; v = Hr.
