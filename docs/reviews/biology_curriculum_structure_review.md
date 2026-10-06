# Biology, Grades 8–10 — curriculum structure review

Prepared 2026-10-05. Research and structure only: no question was collected,
nothing was imported, `data/questions.db` was not written. The three structure
files under `data/subject_structures/biology/` were dry-run only.

## 1. Scope

| Grade | Path | In scope |
|---|---|---|
| 8 | General education, framework-plan dependent | yes |
| 9 | General education | yes |
| 10 | General education | yes |
| 11–12 | Profiled preparation only | **no** — no file, no structure |

Subject: `biology` (registry title „Биология“; official subject name „Биология и
здравно образование“).

## 2. Official sources

All four programmes were retrieved from МОН's own file store on 2026-10-05.
Each PDF was created on 4–5.12.2023; the server reports `Last-Modified`
2024-02-28 for all four. None prints a date or version of its own.

| Grade | Official title | Marked | URL | Pages | SHA-256 |
|---|---|---|---|---|---|
| VIII | Учебна програма по биология и здравно образование за VIII клас (общообразователна подготовка) | Приложение № 18 към т. 18 | <https://www.mon.bg/nfs/2023/12/up_viii_bio.pdf> | 11 | `9375b41a…260319f` |
| IX | Учебна програма по биология и здравно образование за IX клас (общообразователна подготовка по рамкови учебни планове по чл. 12, ал. 2, т. 5–14, т. 16–18, т. 20–21, т. 23–24 от Наредба № 4 от 30 ноември 2015 г. за учебния план) | Приложение № 11 към т. 11 | <https://www.mon.bg/nfs/2023/12/up_ix_bio_pril11-36.pdf> | 10 | `b8a256e1…a0a24f2f` |
| IX (variant, not used) | Same title, „…по чл. 12, ал. 2, т. 2, 3, 4, 22…“ | Приложение № 12 към т. 12 | <https://www.mon.bg/nfs/2023/12/up_ix_bio_pril12-90.pdf> | 16 | `e19f5030…fc61e871` |
| X | Учебна програма по биология и здравно образование за X клас (общообразователна подготовка) | Приложение № 7 | <https://www.mon.bg/nfs/2023/12/up_x_bio.pdf> | 14 | `760697ba…7960de800` |

### Approval and amendment

| Grade | Base order | Amending order | How verified |
|---|---|---|---|
| VIII | Заповед № РД 09-301 от 17.03.2016 г. (Biology = Приложение № 18) | Заповед № РД09-3712/15.11.2023 г. (публ. 27.11.2023 г.) | Base order and appendix number: МОН's draft amending order, section XVIII (<https://www.mon.bg/nfs/2023/07/pr_8-zapup8_27072023.pdf>). Amending order number: МОН's „VIII клас“ programmes page, as recorded in `physics_g8_curriculum_structure_review.md` (read in a browser by the project owner). |
| IX | Заповед № РД 09-1092 от 25.01.2017 г. (Biology = Приложения № 11 and № 12) | Заповед № РД09-3716/15.11.2023 г. (публ. 28.11.2023 г.) | Draft amending order, sections XI and XII (<https://www.mon.bg/nfs/2023/07/pr_9-zapup9_27072023.pdf>); order number as recorded in `physics_g9_curriculum_structure_review.md`. |
| X | Заповед № РД 09-30 от 11.01.2018 г. (Biology = Приложение № 7) | Заповед № РД09-3717/15.11.2023 г. (публ. 29.11.2023 г.) | Draft amending order, section VII (<https://www.mon.bg/nfs/2023/07/pr_10-zapup10_27072023.pdf>); order number as recorded in `physics_g10_curriculum_structure_review.md` (archived МОН page). |

Effective from school year 2024/2025 according to the archived МОН Grade 10 page
quoted in the Physics Grade 10 review; not re-verified here for Grades 8 and 9.

**The retrieved PDFs are the amended texts.** Each contains what the draft order
introduces and lacks what it removes:

- VIII: has „Описва същността и значението на сексуалното и репродуктивното
  здраве.“ and the sentence that annual hours follow the framework plan; no
  „висша нервна дейност“.
- IX (Приложение № 11): no „амитоза“; hours sentence present.
- X: „Дефинира антропогенеза.“ without „раса“; no „Дискутира проблеми, свързани
  с расовата принадлежност“; hours sentence present.

**The 2023 amendment changes no theme or sub-theme title** in any of the four
programmes. It removes or rewords expected results and new concepts and replaces
the fixed annual hours (previously VIII 54, IX 36 or 90, X 72).

Not verified: the signed orders themselves, and МОН's live programme listing
pages (HTTP 403 to automated requests). The draft orders are МОН publications,
not the signed text.

## 3. Programme variants and the Grade 8 framework-plan nuance

Source-derived facts:

- **Grade 9 has two general-education programmes**, chosen by the school's
  framework plan (the titles above name the points of чл. 12, ал. 2 of Наредба
  № 4). Points 2, 3, 4 and 22 are the plans with intensive foreign-language
  study (as established in the Physics Grade 9 review from МОН's framework-plan
  page).
- **Приложение № 12 (90 hours) is Grade 8 plus Grade 9 content.** Its ten
  themes are themes 1–5 of the VIII клас programme, with the same sub-themes,
  followed by themes 1–5 of Приложение № 11 renumbered 6–10. A line-by-line
  comparison finds 115 of 135 long lines of the VIII programme and 89 of 114 of
  Приложение № 11 verbatim in it; the rest are line-break differences.
- **Grades 8 and 10 have one programme each.** Neither title names framework
  plans.
- **Hours by framework plan** (Наредба № 4, read from a mirror of the act,
  <https://www.prirodninauki.bg/wp-content/uploads/2020/09/naredba--4-ot-30-noemvry-2015-h--za-uchebnyja-plan.pdf>;
  its tables do not extract cleanly, so this is supporting evidence only):
  plans without intensive language study give Biology VIII 54, IX 36, X 72;
  the intensive-language plans (Приложения № 2, 3, 4, 22) give no VIII hours
  and IX 90, X 72.

So Biology is general education in both cases. In intensive-language plans it
is simply not taught in Grade 8: the Grade 8 content moves into Grade 9. **The
curriculum content does not differ between the plans; only its placement and
hours do.** Grade 8 Biology is not profiled preparation.

LearnPilot decision: the canonical structures are Приложение № 18 for Grade 8
and Приложение № 11 for Grade 9, the same choice as for Physics. Приложение
№ 12 is not stored: a pupil on an intensive-language plan finds the first half
of their Grade 9 year under LearnPilot Grade 8. The registry already marks
Grade 8 as framework-plan dependent.

## 4. Mapping methodology

Each programme has one content table, „Учебно съдържание“, with the columns
„Теми“, „Компетентности като очаквани резултати от обучението“ and „Нови
понятия“. The first column holds numbered themes (1., 2., …) and, in Grades 8
and 10, numbered sub-themes (1.1, 2.1, …).

Rule, applied to all three grades:

1. **Numbered theme → LearnPilot section.**
2. **Numbered sub-theme → LearnPilot topic**, in programme order.
3. **A theme without sub-themes is its own single topic**, with the theme's
   title. This applies to all five Grade 9 themes (section 6, ambiguity 1).

Expected results, new concepts and the example laboratory work are not topics.
Titles are the programme's wording; nothing is renamed. No topic has a
description.

## 5. Structures

Official numbering in brackets. Slugs are `biology-g<grade>-<name>`.

### Grade 8 — 5 sections, 12 topics

| # | Section (theme) | Topics (sub-themes) |
|---|---|---|
| 1 | Структурна организация на човешкия организъм [1] `structural-organisation-of-the-human-body` | Клетка и тъкани [1.1] `cell-and-tissues` |
| 2 | Обмяна на веществата [2] `metabolism` | Храносмилателна система [2.1] `digestive-system`; Дихателна система [2.2] `respiratory-system`; Отделителна система [2.3] `excretory-system`; Сърдечно-съдова система [2.4] `cardiovascular-system` |
| 3 | Движение и опора на тялото [3] `movement-and-support-of-the-body` | Опорно-двигателна система – скелет и скелетни мускули [3.1] `musculoskeletal-system` |
| 4 | Размножаване, растеж и развитие [4] `reproduction-growth-and-development` | Полова система [4.1] `reproductive-system`; Зародишно и следзародишно развитие [4.2] `embryonic-and-postembryonic-development` |
| 5 | Регулация и хомеостаза [5] `regulation-and-homeostasis` | Нервна система [5.1] `nervous-system`; Ендокринна система [5.2] `endocrine-system`; Сетивни системи [5.3] `sensory-systems`; Кожа [5.4] `skin` |

### Grade 9 — 5 sections, 5 topics

The programme has five themes and no sub-themes. Each section holds one topic
with the same title and the same slug name.

| # | Section and its single topic (theme) | Slug name |
|---|---|---|
| 1 | Химичен състав на живата материя [1] | `chemical-composition-of-living-matter` |
| 2 | Надмолекулни комплекси [2] | `supramolecular-complexes` |
| 3 | Структура и процеси в клетката [3] | `cell-structure-and-processes` |
| 4 | Възпроизводство на клетката [4] | `cell-reproduction` |
| 5 | Приложение на знанията за клетката [5] | `application-of-cell-knowledge` |

### Grade 10 — 3 sections, 7 topics

| # | Section (theme) | Topics (sub-themes) |
|---|---|---|
| 1 | Многоклетъчен организъм [1] `multicellular-organism` | Наследственост и изменчивост [1.1] `heredity-and-variation`; Размножаване, растеж и индивидуално развитие [1.2] `reproduction-growth-and-individual-development` |
| 2 | Биосфера [2] `biosphere` | Популация, биоценоза, екосистема [2.1] `population-biocenosis-ecosystem`; Екологични фактори [2.2] `ecological-factors` |
| 3 | Биологична еволюция [3] `biological-evolution` | Произход и развитие на живата материя [3.1] `origin-and-development-of-living-matter`; Произход и еволюция на човека [3.2] `origin-and-evolution-of-humans`; Доказателства за биологичната еволюция [3.3] `evidence-for-biological-evolution` |

| Grade | Sections | Topics | Programme variant |
|---|---|---|---|
| 8 | 5 | 12 | Приложение № 18 (single programme) |
| 9 | 5 | 5 | Приложение № 11 (plans without intensive language study) |
| 10 | 3 | 7 | Приложение № 7 (single programme) |
| **Total** | **13** | **24** | |

### Cross-grade boundaries

Related material stays in the grade whose programme lists it: „Клетка и тъкани“
(Grade 8, human tissues) is not merged with the Grade 9 cell themes;
„Размножаване, растеж и развитие“ (Grade 8, human) and „Размножаване, растеж и
индивидуално развитие“ (Grade 10, multicellular organisms) are separate units
with different official titles and different slugs. Genetics, ecology and
evolution appear only in Grade 10.

### Wording normalised from the PDFs

| In the PDF | Stored | Why |
|---|---|---|
| „5.2 Ендокринна сиситема“ | Ендокринна система | Typo in the table (also in Приложение № 12). |
| „2.1 Храносмила-телна система“ | Храносмилателна система | Line-break hyphen. |
| „2.4 Сърдечно - съдова система“ | Сърдечно-съдова система | Spacing; the expected results write „сърдечно-съдовата“. |
| „3. 1. Опорно-двигателна система - скелет и скелетни мускули“ | Опорно-двигателна система – скелет и скелетни мускули | Dash as in Приложение № 12. |

No shortened display titles are proposed.

## 6. Ambiguities

1. **Grade 9 has no sub-themes.** The rule above gives five sections with one
   same-titled topic each, so section and topic pages repeat a title and the
   topic URL repeats the slug name
   (`/subjects/biology/9/cell-reproduction/cell-reproduction`). The importer
   accepts this (section and topic slugs are separate namespaces). The
   alternative — one section holding the five themes as topics — would need a
   section title that the programme's content table does not contain. The first
   option invents nothing and keeps "theme = section" in all three grades, so it
   is used; the reviewer should confirm before import, because the importer
   never renames or moves stored rows.
2. **Grade 8, theme 1 has two official wordings.** The introduction lists
   „Равнища на организация в човешкото тяло“; the content table has „Структурна
   организация на човешкия организъм“. The content table is followed.
3. **Uneven topic sizes.** „Структура и процеси в клетката“ (Grade 9) carries
   about a third of that programme's expected results in one topic; „Клетка и
   тъкани“ and „Кожа“ are small. This follows the programme and was not
   rebalanced.
4. **Order numbers** for the amendments come from earlier reviews in this
   repository, not from a fresh reading of МОН's pages.

## 7. Repository state

Read-only inspection of the local database and tracked files: no Biology
curriculum, section, topic or question exists; no tracked Biology file existed;
the registry has no Biology placeholder groups. No stored slug conflicts with
the proposed ones.

## 8. Validation

`python3 -B -m src.import_subject_structure --db <copy> --input <file>` (dry run
only, against a scratch copy of the local database) for each file: exit 0, no
problem reported; curricula 1/1/1, sections 5/5/3 and topics 12/5/7 to create,
in file order. Subject registered, grades 8–10 supported, education type
`general`. All 13 section slugs and 24 topic slugs are unique within their kind
across the three files. `--apply` was not run, so the "[unchanged]" second-run
behaviour was not exercised. No importer or application code was changed.

## 9. Recommendation

Import the three files as they are, after the reviewer confirms ambiguity 1.
Collect questions per topic afterwards, against the expected results of each
programme. Do not create Grade 11–12 structure.
