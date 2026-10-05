# Physics, Grade 9 — curriculum structure review

Review record for `data/subject_structures/physics/grade-9.json`: where the
structure comes from, what is official wording and what is LearnPilot's own, and
what the structure file cannot hold. Prepared on 2026-10-05.

**Status: proposed.** The file has been validated and rehearsed on temporary
database copies only. It has not been imported into `data/questions.db` or any
other real database. Read this before the first import and before any question
is collected for these topics. МОН publishes **two** Grade 9 programmes; the
mapping to the 36-hour one (Приложение № 9) as the default is accepted (section
1, "Applicability").

## 1. Official source

| | |
|---|---|
| Document title | Учебна програма по физика и астрономия за IX клас (общообразователна подготовка по рамкови учебни планове по чл. 12, ал. 2, т. 5 – 14, т. 16 – 18, т. 20 – 21, т. 23 – 24 от Наредба № 4 от 30 ноември 2015 г. за учебния план) |
| Marked on the document | Приложение № 9 към т. 9 |
| Issuing authority | Министерство на образованието и науката (МОН) |
| Subject | Физика и астрономия |
| Grade | IX клас, първи гимназиален етап на средната образователна степен |
| Education type | Общообразователна подготовка |
| URL | <https://www.mon.bg/nfs/2023/12/up_ix_fa_pril9-36.pdf> |
| Retrieved | 2026-10-05, 13 pages, 254 601 bytes |
| SHA-256 | `47fb3cf38c818114636f508d980f68545aed46cb3dc5ebc19d43a681bde1dc34` |
| Document date | PDF created 2023-12-11; server `Last-Modified` 2024-02-28. The document prints no date or version of its own. |

Approval — what was verified and how:

| Fact | Level of verification |
|---|---|
| Base order: **Заповед № РД 09-1092 от 25.01.2017 г.**; Physics and Astronomy is its Приложение № 9 към т. 9 (and № 10 към т. 10, see below) | Verified from МОН's own **draft** amending order for the IX клас programmes (<https://mon.bg/nfs/2023/07/pr_9-zapup9_27072023.pdf>, sections IX and X). The signed 2017 order was not retrieved. |
| The programme has been amended (2023 review) | Verified from the retrieved PDF itself: it contains every change section IX of the draft order makes — the new part „Основни формули за физични величини и закономерности в учебната програма“, the sentence that the annual hours follow the framework curriculum, formulas removed from „Нови понятия“, and the result „Представя графично плоски и сферични вълни…“ removed from 2.2. |
| Current approval and amendment | **Verified from the current official МОН page.** МОН's current „IX клас“ programmes page (<https://www.mon.bg/obshto-obrazovanie/uchebni-planove-i-programi-2/uchebni-programi/uchebni-programi-za-obsthoobrazovatelna-podgotovka/ix-klas/>) states that the IX клас programmes were approved with **Заповед № РД 09-1092 от 25.01.2017 г.** and amended and supplemented with **Заповед № РД09-3716/15.11.2023 г. (публ. 28.11.2023 г.)**. The page is behind a browser challenge for automated requests (HTTP 403), so it was read in a browser, not retrieved by script; the signed orders themselves were not retrieved ("signed order independently retrieved": no). The 2023 draft order is unnumbered and undated („№ …/…2023 г.“); the number above comes from the page, not from the draft. |
| Both Physics programmes are listed on the current page | **Verified from the current official МОН page:** it lists Физика и астрономия (годишно – 36 часа) and Физика и астрономия (годишно – 90 часа). |
| Framework-plan points т. 2, 3, 4, 22 | **Verified from the current official МОН framework-plan page** (<https://www.mon.bg/obshto-obrazovanie/uchebni-planove-i-programi-2/uchebni-planove/ramkovi-uchebni-planove/>): чл. 12, ал. 2, т. 2, 3, 4 and 22 of Наредба № 4 correspond to plans with **интензивно** изучаване на чужд език. Same reading method as above. |

The exact link behind „Физика и астрономия (годишно – 36 часа)“ was not
compared with the PDF used here; the file sits in МОН's own `nfs/2023/12/`
directory next to the amended Grade 8 and Grade 10 files.

The amendment changes no theme or sub-theme title. The 2017 text of Приложение
№ 9 was not retrieved.

### Applicability and the second Grade 9 programme

МОН publishes two general-education Physics and Astronomy programmes for IX
клас. Which one a school uses depends on its framework curriculum:

| | Приложение № 9 към т. 9 (**used here**) | Приложение № 10 към т. 10 (not used) |
|---|---|---|
| Framework curricula (чл. 12, ал. 2 of Наредба № 4) | т. 5–14, 16–18, 20–21, 23–24 | т. 2, 3, 4, 22 (интензивно изучаване на чужд език) |
| Annual hours before the 2023 amendment (draft order) | 36 | 90 |
| Themes in „Учебно съдържание“ | 1. Електричен ток; 2. Механично движение | 1. Механика; 2. Топлинни явления; 3. Електричен ток; 4. Механично движение |
| Amended file | `up_ix_fa_pril9-36.pdf` | <https://www.mon.bg/nfs/2023/12/up_ix_fa_pril10-90.pdf> (18 pages, SHA-256 `fb975e64…67ea78`) |

Verified by comparing the documents: themes 1–2 of Приложение № 10 carry the
same theme and sub-theme titles as the Grade 8 programme (already stored as
LearnPilot grade 8), and its themes 3–4 carry the same titles as themes 1–2 of
Приложение № 9. Приложение № 10 is therefore Grade 8 plus Приложение № 9 in one
year, not different content.

Intensive foreign-language study: the Grade 8 programme states that it „прилага
се и като учебна програма за IX клас (I част) за учениците, които се обучават по
рамкови учебни планове с интензивно изучаване на чужд език“. The Grade 9
programme used here says nothing about intensive foreign-language study. The
current МОН framework-plan page establishes that т. 2, 3, 4 and 22 are the plans
with intensive foreign-language study, so the 90-hour programme (Приложение
№ 10) is the **intensive-language variant**.

**LearnPilot decision.** The 36-hour programme (Приложение № 9) is the
canonical/default Grade 9 structure. The 90-hour intensive-language variant is
intentionally **not** merged into it. Support for that alternative curriculum
can be added separately later, if LearnPilot needs curriculum variants.

Not used: the X клас programme (its own document) and profiled preparation
(XI–XII клас).

## 2. How the programme is organised

| Part of the document | What it is | Used for |
|---|---|---|
| Кратко представяне на учебната програма | introduction | nothing |
| Очаквани резултати … в края на класа | three **области на компетентност**: Електричество и магнетизъм; Трептения и вълни; Наблюдение, експеримент и изследване | not structure (see below) |
| **Учебно съдържание** — column „Теми“ | two numbered themes, each with three numbered sub-themes | **sections and topics** |
| Учебно съдържание — „Компетентности като очаквани резултати от обучението“, „Нови понятия“ | expected results and new concepts per sub-theme | topic scope (appendix) |
| Препоръчителни уроци за практически дейности (лабораторни работи) | six recommended lab lessons | collector guidance only |
| Основни формули за физични величини и закономерности | formulas per sub-theme | topic scope (appendix) |
| Препоръчително процентно разпределение на задължителните учебни часове | share of hours by lesson type; **no hours per theme** | nothing |
| Оценяване; дейности за ключовите компетентности; междупредметни връзки | methodological guidance | nothing |

Only the „Теми“ column defines the hierarchy. It is an explicit two-level
structure, mapped exactly as in Grade 8:

- each numbered theme (`1.`, `2.`) is a LearnPilot **section**;
- each numbered sub-theme (`1.1.` … `2.3.`) is a LearnPilot **topic**;
- order is the programme's order.

The competence areas are not themes and are not sections. Their names differ
from the theme names („Електричество и магнетизъм“ / „Трептения и вълни“ versus
„Електричен ток“ / „Механично движение“); the theme names are used because they
are what „Учебно съдържание“ prints. No textbook table of contents or lesson
plan was used anywhere.

## 3. Mapping

| Official no. | Official heading (verbatim) | LearnPilot | Stored slug |
|---|---|---|---|
| 1. | Електричен ток | section 1 | `physics-g9-electric-current` |
| 1.1. | Електрически вериги | topic 1 | `physics-g9-electric-circuits` |
| 1.2. | Електрична енергия | topic 2 | `physics-g9-electrical-energy` |
| 1.3. | Ток в метали и полупроводници | topic 3 | `physics-g9-current-in-metals-and-semiconductors` |
| 2. | Механично движение | section 2 | `physics-g9-mechanical-motion` |
| 2.1. | Трептения | topic 1 | `physics-g9-oscillations` |
| 2.2. | Механични вълни | topic 2 | `physics-g9-mechanical-waves` |
| 2.3. | Звук | topic 3 | `physics-g9-sound` |

Totals: 2 sections, 6 topics.

**Official wording.** All 8 titles are the official headings, compared
mechanically with the extracted text of the PDF. Seven match as continuous
strings; „Ток в метали и полупроводници“ is wrapped over two lines inside its
table cell and was confirmed by reading the cell (and the identical heading 3.3
in Приложение № 10). Numbering is not part of the title: the importer takes
order from position in the file.

**LearnPilot's own, not official:**

| Item | Value | Why |
|---|---|---|
| Subject id | `physics` | Registry id (`src/subject_registry.py`); the official subject is „Физика и астрономия“. |
| `education_type` | `general` | The value the stored curricula already use for общообразователна подготовка. |
| Curriculum title | „Физика и астрономия, 9. клас — общообразователна подготовка“ | Same pattern as the stored Grade 8 title. The official document title is in section 1. |
| Slugs | table above | English, semantic, namespaced `physics-g9-`, following the official wording: `mechanical-motion` for „Механично движение“, not "oscillations and waves". |

**No extra pedagogical decomposition was invented.** There are exactly as many
sections as numbered themes and exactly as many topics as numbered sub-themes;
no expected result, concept, formula or lab lesson was promoted to a topic, and
no topic title is derived.

## 4. Existing Grade 9 state and the old placeholder groups

Read-only inspection of `data/questions.db` (developer copy, locally modified)
and of the committed seed (`git show HEAD:data/questions.db`):

- no `physics` curriculum for grade 9, no `physics-g9-*` section or topic, and
  therefore no Grade 9 Physics question, in either database;
- the developer copy already holds the Grade 8 Physics structure; the committed
  seed holds no Physics rows at all;
- Grade 9 Physics currently exists only as four navigation-only placeholder
  groups in `src/subject_registry.py`. They were never authoritative
  (`docs/SUBJECT_CONTENT_CONTRACT.md`, "Placeholders and status").

| Placeholder | Against the official programme | After import |
|---|---|---|
| Механика (`mechanics`) | Not a Grade 9 theme. Theme 2 is „Механично движение“ and contains only oscillations, waves and sound; mechanics proper is Grade 8. | Disappears; `/subjects/physics/9/mechanics` becomes a 404. |
| Трептения и вълни (`oscillations-and-waves`) | The name of a competence area, not a theme. Its content is sub-themes 2.1 and 2.2. | Disappears; URL becomes a 404. |
| Звук (`sound`) | A sub-theme (2.3), not a theme. | The section-level URL `/subjects/physics/9/sound` becomes a 404; the topic lives at `/subjects/physics/9/mechanical-motion/sound`. |
| Светлина (`light`) | Not in the Grade 9 programme („Светлина“ is a competence area of the X клас programme). | Disappears; URL becomes a 404. |

Theme 1 „Електричен ток“ has no placeholder at all. Unlike Grade 8, no
placeholder URL survives the import, because no placeholder slug equals a stored
section's bare slug. Placeholders retire per grade as soon as the grade has
stored sections; no registry change is needed, and grades 10–12 keep theirs.
All of this was observed on a temporary copy (section 6).

## 5. What the structure file cannot hold

The importer stores the curriculum title, `education_type` and `source_url`, and
marks new rows `source_type = 'subject_structure_import'`. Everything else in
section 1 — order numbers, appendix number, authority, framework-plan
applicability, document date, retrieval date, hash — has no field in the format
and is recorded only here.

Deliberately not in the file: topic descriptions (so that every stored text is
official wording; they can be added later but never overwritten), topic scope
(appendix), the competence areas and the lab lessons.

Recommended lab lessons, with the topic each belongs to. The lessons are the
programme's; the assignment to topics is LearnPilot's reading of them.

| # | Препоръчителен урок (verbatim) | Topic |
|---|---|---|
| 1 | Експериментална проверка на закона на Ом | 1.1 |
| 2 | Експериментално определяне на специфично съпротивление на метален проводник | 1.1 |
| 3 | Изучаване на успоредно и последователно свързване на резистори | 1.1 |
| 4 | Изследване на електрически вериги с полупроводникови елементи | 1.3 |
| 5 | Определяне на земното ускорение с математично махало | 2.1 |
| 6 | Изследване на пружинно махало | 2.1 |

## 6. Validation performed

All on temporary copies outside the repository. `data/questions.db` was only
read (SQLite backup through a read-only connection); its SHA-256 and that of
`vault/.obsidian/graph.json` were identical before and after.

Command: `python3 -B -m src.import_subject_structure --db COPY --input
data/subject_structures/physics/grade-9.json [--apply]`.

| Check | Result |
|---|---|
| File accepted by the importer's validation (registered subject, declared grade, namespaced unique slugs, no unknown keys) | pass |
| Dry run on a copy of the developer database | 1 curriculum, 2 sections, 6 topics `[create]`; no conflicts; copy byte-identical afterwards |
| Dry run on a copy of the committed seed (no pending migrations) | same plan; no conflicts |
| `--apply` on both copies | „changes written“ |
| Second dry run, second `--apply` | all 9 items `[unchanged]`; „no changes; the database already matches this structure“; copy byte-identical |
| Stored result (developer copy) | curricula 6 → 7, sections 49 → 51, topics 137 → 143; `display_order` 1…n in programme order; `topic_classes` = 9 for all six topics |
| Questions | 1027 before and after (seed copy: 832 / 832); `question_topic_assignments` and `multiple_choice_options` unchanged |
| Grade 8 Physics on the copy | still 2 sections, 10 topics; page still renders |
| `pragma_foreign_key_check` on the copies | 0 rows |
| Pages, Flask test client against the applied copy (`DZI_DB=COPY`, no server started) | 200 for `/subjects/physics`, `/subjects/physics/9`, both section pages and all six topic pages; sections and topics appear in programme order |
| Placeholder URLs on the applied copy | `mechanics`, `oscillations-and-waves`, `sound`, `light` → 404 (all 200 before import) |

Existing tests: `tests.test_import_subject_structure`,
`tests.test_subject_topic_pages` and `tests.test_subject_registry` report 4
failures and 1 error in this working tree. They build their fixtures from
`data/questions.db`, whose local modification already contains Grade 8 Physics
rows (`UNIQUE constraint failed: curriculum_sections.section_slug`, „seed
database has content for IT only“). None of them reads the Grade 9 file. This
was not re-run against a clean checkout, because restoring the database was out
of scope.

## 7. Open points for the reviewer

1. **Which Grade 9 programme.** Decided: this file follows Приложение № 9 (two
   themes, 36 hours) as the default. Schools on the intensive-language framework
   curricula of Приложение № 10 (90 hours) study Grade 8's two
   themes and these two themes in grade 9. LearnPilot already files Механика and
   Топлинни явления under grade 8, so those pupils find the first half of their
   year there. Listing them under grade 9 as well would need a second curriculum
   (another `education_type`) and decisions the importer cannot make by itself;
   nothing of the kind was created.
2. **Amending order.** Resolved from the current МОН page: Заповед
   № РД09-3716/15.11.2023 г. (публ. 28.11.2023 г.), base order № РД 09-1092
   от 25.01.2017 г. (section 1). Signed orders not independently retrieved.
3. **Framework-plan points.** Resolved from the current МОН framework-plan page:
   т. 2, 3, 4, 22 are the intensive foreign-language plans (section 1).
4. **Published formulas table.** In the retrieved PDF the part „Основни
   формули…“ numbers the same themes `3.`/`4.` and sub-themes `3.1.`–`4.3.`,
   where the draft order has `1.`/`2.`. This looks like numbering carried over
   from Приложение № 10; the titles are identical, and „Учебно съдържание“
   (`1.`/`2.`) is taken as authoritative. Sub-theme 2.3 also carries a stray
   closing quotation mark after its first expected result.
5. **Section title „Механично движение“.** Official, but it describes
   oscillations, waves and sound only. It is kept verbatim.
6. **Topic granularity.** Six topics, some broad (1.2 covers work and power of
   the current, sources, EMF, internal resistance and Ohm's law for the whole
   circuit). Decide before questions are imported: the importer appends topics
   but never renames or moves them.

## 8. Files changed

- `data/subject_structures/physics/grade-9.json` (new)
- `docs/reviews/physics_g9_curriculum_structure_review.md` (new, this file)
- `docs/SUBJECT_STRUCTURE_IMPORT.md` (one row added to "Structure files in the
  repository")

## Appendix: official scope of each topic

Condensed from the programme's „Учебно съдържание“ and „Основни формули“ tables,
for question collectors: a question belongs to a topic only if it tests one of
these results at this level. Formulas are transcribed as plain text.

### 1. Електричен ток

**1.1. Електрически вериги**

- Формулира закона на Ом за част от веригата.
- Анализира графиката на зависимостта на тока от напрежението за омов проводник.
- Знае, че специфичното съпротивление е характеристика на материала, от който е изработен проводникът.
- Пресмята съпротивление и специфично съпротивление.
- Разпознава по схема последователно, успоредно и смесено свързване на консуматори.
- Анализира разпределението на токовете и на напреженията при различни видове свързване на консуматори.
- Пресмята еквивалентно съпротивление при последователно, успоредно и смесено свързване на консуматори.

Нови понятия: закон на Ом; специфично съпротивление; еквивалентно съпротивление.
Формули: I = U/R; ρ = RS/l; R = R1 + R2 + …; 1/R = 1/R1 + 1/R2 + ….

**1.2. Електрична енергия**

- Дефинира работа на електричния ток като произведение от пренесения през консуматора заряд и напрежението върху консуматора.
- Разбира и дава примери, че работата на тока е мярка за преобразуваната електрична енергия, а мощността на консуматора зависи от условията, при които той работи.
- Пресмята работа и мощност на тока в електрически вериги с два консуматора, свързани успоредно или последователно.
- Описва източниците на напрежение от гледна точка на трансформациите на енергия в електрическата верига.
- Дефинира електродвижещо напрежение и вътрешно съпротивление на източник.
- Формулира и прилага закона на Ом за цялата верига.
- Чертае и разчита схеми на електрически вериги.

Нови понятия: работа на електричния ток; странични сили; електродвижещо напрежение; вътрешно съпротивление.
Формули: A = qU = UIt; ε = Aстр/q; I = ε/(R + r).

**1.3. Ток в метали и полупроводници**

- Разграничава метали и полупроводници (германий, силиций) според специфичното им съпротивление и неговата зависимост от температурата.
- Посочва токовите носители в металите и полупроводниците.
- Илюстрира с примери как електричните свойства на полупроводниците се управляват чрез целенасочено внасяне на примеси (легиране).
- Знае, че специфичното съпротивление на полупроводниците намалява при повишаване на температурата и при осветяване и посочва полупроводникови елементи (термистор, фоторезистор), които притежават това свойство.
- Описва качествено действието на полупроводников диод.
- Изброява други полупроводникови устройства с p-n преходи и техни съвременни приложения (светодиоди, транзистори, интегрални схеми).

Нови понятия: свръхпроводимост; електрони и дупки; донори и акцептори; p-n преход.
Формули: няма.

### 2. Механично движение

**2.1. Трептения**

- Описва хармоничното трептене като често срещано периодично движение, породено от действието на връщащата сила.
- Разпознава графиката на хармоничното трептене.
- Използва основни характеристики на трептенията и връзката между период и честота.
- Описва качествено и количествено хармоничното трептене на пружинно махало и на математично махало.
- Характеризира трептенията с кинетична и потенциална енергия.
- Проследява качествено преобразуването и запазването на енергията при незатихващите трептения.
- Посочва причината за затихване на трептенията в реалните системи.
- Разграничава чрез примери принудените от собствените трептения на една система (по честота).
- Описва чрез примери явлението резонанс и някои последствия от него (полезен и вреден резонанс).

Нови понятия: отклонение (x); връщаща сила; коефициент на еластичност (k); период на пружинно и математично махало; потенциална енергия на деформирана пружина; собствени, затихващи и принудени трептения; резонанс.
Формули: F = kx; T = 2π√(m/k); T = 2π√(l/g); W = kx²/2.

**2.2. Механични вълни**

- Описва вълновия процес като разпространение на механични трептения в материална среда.
- Разбира, че вълните пренасят енергия, а не пренасят вещество.
- Разграничава скоростта на вълната от скоростта на движение на частиците.
- Описва проста хармонична вълна чрез величините скорост (u), честота (ν) и дължина на вълната (λ).
- Прилага връзката между скорост, честота и дължина на вълната.
- Разграничава надлъжни и напречни вълни.
- Описва качествено създаването и разпространението на механични вълни в различни среди.
- Знае основни правила на поведение при земетресение.

Нови понятия: скорост (u), период (T), честота (ν), амплитуда (A) и дължина (λ) на хармонична вълна; надлъжни и напречни вълни; вълнов фронт; лъч; сеизмични вълни.
Формули: u = λν.

**2.3. Звук**

- Разграничава обективни и субективни характеристики на звука – интензитет и ниво на интензитета (dB), от субективните характеристики – височина и тембър на звука.
- Описва източници на звук, разпространението на звука и възприемането му от човешкото ухо.
- Дава примери за естествени и създадени от човека източници на ултразвук и инфразвук.
- Описва хигиенни правила за предпазване от вредното влияние на шума и силните звукове.
- Посочва съвременни приложения на ултразвука.

Нови понятия: скорост на звука (u); интензитет на звукова вълна; ниво на интензитета; височина и тембър на звука; инфразвук; ултразвук.
Формули: I = E/(St).
