# Physics, Grade 8 — curriculum structure review

Review record for `data/subject_structures/physics/grade-8.json`: where the
structure comes from, what is official wording and what is LearnPilot's own, and
what the structure file cannot hold. Prepared on 2026-10-05.

**Status: proposed.** The file has been validated and rehearsed on temporary
database copies only. It has not been imported into `data/questions.db` or any
other real database. Read this before the first import and before any question
is collected for these topics.

## 1. Official source

| | |
|---|---|
| Document title | Учебна програма по физика и астрономия за VIII клас (общообразователна подготовка) |
| Marked on the document | Приложение № 17 към т. 17 |
| Issuing authority | Министерство на образованието и науката (МОН) |
| Subject | Физика и астрономия |
| Grade | VIII клас, първи гимназиален етап на средната образователна степен |
| Education type | Общообразователна подготовка |
| Stated applicability | „Прилага се и като учебна програма за IX клас (I част) за учениците, които се обучават по рамкови учебни планове с интензивно изучаване на чужд език“ |
| URL | <https://www.mon.bg/nfs/2023/12/up_viii_fa.pdf> |
| Retrieved | 2026-10-05, 13 pages, 261 274 bytes |
| SHA-256 | `9bebc77dcaeaa8b99d22a1dfbdcce5b60fee9275560665af45e78087148b7d29` |
| Document date | PDF created 2023-12-11; server `Last-Modified` 2024-02-28. The document prints no date or version of its own. |

Approval:

- **Заповед № РД 09-301 от 17.03.2016 г.** of the Minister of Education and
  Science approves the Grade 8 general-education programmes; Physics and
  Astronomy is its Приложение № 17 към т. 17. Confirmed in МОН's own draft
  amending order, section XVII
  (<https://mon.bg/nfs/2023/07/pr_8-zapup8_27072023.pdf>).
- **Amended by Заповед № РД09-3712/15.11.2023 г. (публ. 27.11.2023 г.).**
  Verified from МОН's current "VIII клас" programmes page, which states:
  „Учебните програми са утвърдени със Заповед № РД09-301 от 17.03.2016 г.,
  изменена и допълнена със Заповед № РД09-3712/15.11.2023 г. (публ. 27.11.2023
  г.)“
  (<https://www.mon.bg/obshto-obrazovanie/uchebni-planove-i-programi-2/uchebni-programi/uchebni-programi-za-obsthoobrazovatelna-podgotovka/viii-klas/>).
  The page was read in a browser by the project owner; it is behind a browser
  challenge for automated requests. The text of the signed order itself was not
  retrieved.
- The retrieved PDF is the amended text: it contains the part „Основни формули
  за физични величини и закономерности в учебната програма“ and the sentence
  about the annual hours following the framework curriculum, both of which the
  2023 amendment introduces.
- The amendment does not change any theme or sub-theme title. Per the draft
  order it moves formulas out of the „Нови понятия“ column into the new formulas
  part, adds one concept to 1.1, changes one expected result in 2.5 and replaces
  the fixed annual hours. The 2016 text was not retrieved.

Other programmes considered and not used:

- IX and X клас have their own programmes; profiled preparation starts in XI
  клас. None of them is combined into this structure.
- There is one general-education programme for Grade 8. The intensive
  foreign-language case above is the same document taught a year later, not a
  second programme. LearnPilot files it under grade 8 only.

## 2. How the programme is organised

| Part of the document | What it is | Used for |
|---|---|---|
| Кратко представяне на учебната програма | introduction | nothing |
| Очаквани резултати … в края на класа | three **области на компетентност**: Механика; Топлинни явления; Наблюдение, експеримент и изследване | not structure (see below) |
| **Учебно съдържание** — column „Теми“ | two numbered themes, each with five numbered sub-themes | **sections and topics** |
| Учебно съдържание — „Компетентности като очаквани резултати от обучението“, „Нови понятия“ | expected results and new concepts per sub-theme | topic scope (appendix) |
| Основни формули за физични величини и закономерности | formulas per sub-theme | topic scope (appendix) |
| Препоръчителни уроци за практически дейности (лабораторни работи) | eight recommended lab lessons | collector guidance only |
| Препоръчително процентно разпределение на задължителните учебни часове | share of hours by lesson type; **no hours per theme** | nothing |
| Оценяване; дейности за ключовите компетентности; междупредметни връзки | methodological guidance | nothing |

Only the „Теми“ column defines the hierarchy. It is an explicit two-level
structure, so nothing had to be decomposed or invented:

- each numbered theme (`1.`, `2.`) is a LearnPilot **section**;
- each numbered sub-theme (`1.1.` … `2.5.`) is a LearnPilot **topic**;
- order is the programme's order.

„Наблюдение, експеримент и изследване“ is a competence area, not a theme: it has
no row in „Учебно съдържание“ and cuts across both themes. It is therefore not a
section. No textbook table of contents or lesson plan was used anywhere.

## 3. Mapping

```
Official programme (Учебно съдържание, „Теми“)      LearnPilot section               LearnPilot topics
1. Механика                                      →  physics-g8-mechanics          →  1.1 – 1.5
2. Топлинни явления                              →  physics-g8-thermal-phenomena  →  2.1 – 2.5
```

| Official no. | Official heading (verbatim) | LearnPilot | Stored slug |
|---|---|---|---|
| 1. | Механика | section 1 | `physics-g8-mechanics` |
| 1.1. | Неравномерно движение | topic 1 | `physics-g8-non-uniform-motion` |
| 1.2. | Принципи на механиката | topic 2 | `physics-g8-principles-of-mechanics` |
| 1.3. | Равновесие на телата | topic 3 | `physics-g8-equilibrium-of-bodies` |
| 1.4. | Механична работа, мощност и енергия | topic 4 | `physics-g8-work-power-energy` |
| 1.5. | Механика на течности и газове | topic 5 | `physics-g8-fluid-mechanics` |
| 2. | Топлинни явления | section 2 | `physics-g8-thermal-phenomena` |
| 2.1. | Топлинно движение | topic 1 | `physics-g8-thermal-motion` |
| 2.2. | Топлообмен. Преходи между състоянията на веществата | topic 2 | `physics-g8-heat-exchange-phase-transitions` |
| 2.3. | Първи принцип на термодинамиката | topic 3 | `physics-g8-first-law-of-thermodynamics` |
| 2.4. | Процеси с идеален газ | topic 4 | `physics-g8-ideal-gas-processes` |
| 2.5. | Топлинни машини | topic 5 | `physics-g8-heat-engines` |

**Official wording.** All 12 titles in the structure file are the official
headings, character for character, compared mechanically with the cells of the
programme's table. The only change is that the numbering is not part of the
title: the importer takes order from position in the file. (The programme prints
`1. 2.` with a stray space; it is sub-theme 1.2.)

**LearnPilot's own, not official:**

| Item | Value | Why |
|---|---|---|
| Subject id | `physics` | Registry id (`src/subject_registry.py`); the official subject is „Физика и астрономия“. |
| `education_type` | `general` | The value the stored IT curricula already use for общообразователна подготовка. |
| Curriculum title | „Физика и астрономия, 8. клас — общообразователна подготовка“ | Same pattern as the stored IT curriculum titles. The official document title is in section 1. |
| Slugs | table above | English, semantic, namespaced `physics-g8-`. They follow the official wording rather than textbook habits: `principles-of-mechanics`, not "Newton's laws". `first-law-of-thermodynamics` is the usual English name of „първи принцип на термодинамиката“; `fluid-mechanics` stands for „течности и газове“. |

No topic title is derived, so there is no decomposition to justify.

## 4. The old placeholder groups

The registry's navigation-only groups for grade 8 were never authoritative
(`docs/SUBJECT_CONTENT_CONTRACT.md`, "Placeholders and status").

| Placeholder | Against the official programme | After import |
|---|---|---|
| Механика | Matches theme 1 exactly. | Replaced by the stored section; the URL `/subjects/physics/8/mechanics` keeps working. |
| Топлинни явления | Matches theme 2 exactly. | Replaced by the stored section; the URL `/subjects/physics/8/thermal-phenomena` keeps working. |
| Налягане | Too narrow, and not a theme. Hydrostatic and atmospheric pressure are part of sub-theme 1.5 „Механика на течности и газове“. | Disappears; `/subjects/physics/8/pressure` becomes a 404. |
| Електричество | Not in the Grade 8 programme at all. | Disappears; `/subjects/physics/8/electricity` becomes a 404. |

No official theme is missing from the placeholders at section level; what they
lacked entirely is the ten sub-themes. Placeholders retire per grade as soon as
the grade has stored sections; no registry change is needed, and grades 9–12
keep theirs. Whether to delete the grade 8 entry from the registry afterwards is
a separate code decision.

## 5. What the structure file cannot hold

The importer stores the curriculum title, `education_type` and `source_url`, and
marks new rows `source_type = 'subject_structure_import'`. Everything else in
section 1 — order numbers, appendix number, authority, document date, retrieval
date, hash, the intensive-language applicability note — has no field in the
format and is recorded only here. The schema has `curricula.notes` and
section-level `source_title` / `source_authority` columns, but the structure
importer does not write them, and no migration or code change was made for this.

Also deliberately not in the file:

- **Topic descriptions.** The format allows an optional `description` per topic.
  None is set, so that every stored text is official wording. The importer fills
  an empty description later but never overwrites a stored one, so adding them
  remains possible and reversing them would not be.
- **Topic scope** (expected results, new concepts, formulas): see the appendix.
- **The third competence area and the lab lessons**: see below.

Recommended lab lessons, with the topic each one belongs to. The lessons are the
programme's; the assignment to topics is LearnPilot's reading of them.

| # | Препоръчителен урок (verbatim) | Topic |
|---|---|---|
| 1 | Основни насоки при провеждане на физично изследване | none (general) |
| 2 | Опитно определяне на ускорението при равноускорително движение | 1.1 |
| 3 | Експериментална проверка на втория принцип на механиката | 1.2 |
| 4 | Определяне центъра на тежестта на плоски пластинки | 1.3 |
| 5 | Експериментално изследване на закона на Архимед | 1.5 |
| 6 | Изследване на процес (изохорен или изобарен) при идеален газ | 2.4 |
| 7 | Определяне на специфичния топлинен капацитет на твърдо тяло | 2.2 |
| 8 | Определяне на специфичната топлина на топене на лед | 2.2 |

## 6. Validation performed

All on temporary copies, removed afterwards. `data/questions.db` was only read
as a file to make the copies.

- `validate_structure` accepts the file: registered subject, declared grade,
  namespaced unique ASCII slugs.
- Dry run (`python3 -B -m src.import_subject_structure --db COPY --input
  data/subject_structures/physics/grade-8.json`) on a copy of the developer
  database and on a migrated copy of the committed seed: 1 curriculum, 2
  sections, 10 topics to create; no conflicts; the copy is unchanged.
- `--apply` on the copies, then a second dry run and a second `--apply`: all 13
  items `[unchanged]`, nothing written.
- Stored result: sections and topics carry `display_order` 1…n in programme
  order; every section and topic is owned by `physics`; `topic_classes` is 8; no
  question row exists; IT curricula untouched; foreign keys consistent.
- The importer rejects damaged variants of the file (duplicate slug, wrong or
  missing namespace, undeclared grade, unknown subject, numeric order key,
  changed stored title, omitted stored topic), and treats a reordered file as an
  order update.
- With the structure applied to a temporary copy, the application's pages
  render: grade page with both sections in order, both section pages with their
  five topics in order, all ten topic pages, questions shown as in preparation.
- `src.start_question_collection.targets_from_structure` resolves topics from
  this file, so a later collection run can use it as `--structure`.

## 7. Open points for the reviewer

1. **Amending order.** Resolved: Заповед № РД09-3712/15.11.2023 г. is confirmed
   on МОН's page (section 1).
2. **Topic granularity.** The ten topics are exactly the programme's sub-themes.
   Some are broad: 1.2 covers inertia, mass, adding forces, action and reaction,
   weight, normal force, friction and the second principle. The programme gives
   no lesson titles, so any finer split would be LearnPilot's own. Decide before
   questions are imported: the importer appends topics but never renames or moves
   them.
3. **Subject name.** LearnPilot shows the subject as „Физика“; the official
   subject is „Физика и астрономия“. The Grade 8 programme has no astronomy
   content.
4. **Intensive foreign-language classes** study this programme in grade 9
   (I част). LearnPilot lists it under grade 8 only.
5. **Descriptions.** Add topic descriptions later, or keep titles only.

## Appendix: official scope of each topic

Quoted from the programme's „Учебно съдържание“ and „Основни формули“ tables, for
question collectors: a question belongs to a topic only if it tests one of these
results at this level. Limits the programme states in brackets are part of the
scope. Symbols and formulas are transcribed as plain text.

### 1. Механика

**1.1. Неравномерно движение**

- Разбира относителния характер на движението.
- Характеризира неравномерното движение с величините средна скорост, моментна скорост и ускорение.
- Прилага законите за скоростта и за пътя при праволинейно равноускорително и равнозакъснително движение с примери от транспорта (спирачен път) и свободното падане (без движение на тяло, хвърлено вертикално нагоре).
- Разчита графиката на закона за скоростта при праволинейни равнопроменливи движения.

Нови понятия: отправно тяло; средна скорост; моментна скорост (v); ускорение (a); земно ускорение; праволинейни равнопроменливи движения.
Формули: v = Δs/Δt; a = Δv/Δt; v = v0 ± at; s = v0t ± at²/2.

**1.2. Принципи на механиката**

- Разбира, че телата запазват състоянието си на покой или на праволинейно и равномерно движение по инерция, когато не взаимодействат с други тела.
- Дефинира и илюстрира с примери от всекидневието, че масата на телата е мярка за тяхната инертност.
- Събира сили с еднакви или противоположни посоки.
- Илюстрира с примери, че при взаимодействието на телата винаги възникват две равни по големина и противоположни по посока сили – на действие и противодействие (реактивно движение).
- Разграничава и пресмята сила на тежестта, сила на реакция на опората, тегло и сила на триене.
- Прилага втория принцип на механиката за праволинейно движение на тяло – без триене и с триене (без разлагане на сили).

Нови понятия: равнодействаща сила (F); сила на тежестта (G); тегло (P); сила на реакция на опората (N); коефициент на триене (k); сила на триене (f); сила на съпротивление на въздуха.
Формули: G = mg; F = kN (сила на триене); F = ma.

**1.3. Равновесие на телата**

- Описва видовете равновесие на телата (устойчиво и неустойчиво) в зависимост от положението на центъра на тежестта.
- Обяснява равновесието на тяло върху опора и различни приложения (подреждане на товари върху платформи, безопасност на движението по наклонен участък от пътя).

Нови понятия: център на тежестта.
Формули: няма.

**1.4. Механична работа, мощност и енергия**

- Пресмята работата на сила, успоредна на направлението на движение.
- Определя мощността на механизъм като работа, извършена за единица време.
- Дефинира и пресмята кинетична и потенциална енергия на тяло.
- Формулира и прилага закона за запазване на механичната енергия.
- Обобщава запазването на енергията като основен природен закон.

Нови понятия: механична работа (A); механична мощност (P); кинетична енергия (Eк); потенциална енергия (Eп); механична енергия.
Формули: A = Fs и A = –Fs; P = A/t; Eк = mv²/2; Eп = mgh.

**1.5. Механика на течности и газове**

- Описва общите свойства на течности и газове.
- Използва закона на Паскал и формулата за хидростатичното налягане за обясняване на действието на хидравличните машини, скачените съдове, водния манометър и живачния барометър и дава примери за тяхното приложение.
- Формулира и прилага закона на Архимед.

Нови понятия: хидростатично налягане (p); изтласкваща сила (FA).
Формули: p = ρgh; FA = ρVg.

### 2. Топлинни явления

**2.1. Топлинно движение**

- Описва топлинното движение на градивните частици като хаотично, масово и непрекъснато.
- Характеризира топлинното равновесие с температура.
- Свързва вътрешната енергия и температурата с топлинното движение на градивните частици.
- Прилага връзката между температурните скали на Целзий и на Келвин.

Нови понятия: вътрешна енергия; абсолютна температура (T); келвин (K).
Формули: T = t + 273,15.

**2.2. Топлообмен. Преходи между състоянията на веществата**

- Пресмята количество топлина при топлообмен.
- Описва преходите между състоянията на веществото.
- Пресмята обмененото при преходите количество топлина, като използва специфичен топлинен капацитет, специфична топлина на топене и специфична топлина на изпарение.
- Дава примери за приложения на преходите между състоянията на веществата в природата, бита и техниката.

Нови понятия: количество топлина (Q); специфичен топлинен капацитет (c); специфична топлина на топене (λ); специфична топлина на изпарение (r).
Формули: c = Q/(mΔt); λ = Q/m; r = Q/m.

**2.3. Първи принцип на термодинамиката**

- Определя работата при изменение на обема на газ.
- Обяснява, че вътрешната енергия може да се измени чрез работа и топлообмен.
- Формулира първия принцип на термодинамиката като закон за запазване на енергията при топлинните процеси.
- Обосновава невъзможността на вечния двигател.

Нови понятия: работа на външните сили за свиване или разширяване на газ.
Формули: A = pΔV и A = −pΔV; ΔU = Q + A.

**2.4. Процеси с идеален газ**

- Определя чрез примери от всекидневието адиабатния процес като процес, при който не се извършва топлообмен (без графики и формули).
- Описва въз основа на опита изотермен, изохорен и изобарен процес.
- Прилага законите за трите изопроцеса при решаване на задачи.
- Описва модела идеален газ.
- Анализира енергетично адиабатния и изохорния процес чрез първия принцип на термодинамиката.

Нови понятия: адиабатен процес; изотермен процес; изохорен процес; изобарен процес.
Формули: изотермен процес pV = const, p1V1 = p2V2; изохорен процес p/T = const, p1/T1 = p2/T2; изобарен процес V/T = const, V1/T1 = V2/T2.

**2.5. Топлинни машини**

- Описва по схема принципа на действие на топлинна машина.
- Дава примери за топлинни машини и пресмята коефициент на полезно действие (КПД).
- Проследява по схема действието на четиритактовия двигател с вътрешно горене (двигател на Ото).
- Дискутира екологични проблеми, предизвикани от отработените газове на двигателите с вътрешно горене, и такива, свързани с топлинното замърсяване на околната среда.

Нови понятия: КПД на топлинна машина.
Формули: η = 1 − Q2/Q1.
