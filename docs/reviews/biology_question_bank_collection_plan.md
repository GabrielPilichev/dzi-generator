# Biology, Grades 8–10 — question-bank collection plan

**Post-pilot recalibration, 2026-10-06.** Sections A–K below are the plan in
force for every Biology collection run from Wave 2 on. They replace the
pre-pilot targets, run splits, wave schedule and model table of 2026-10-05,
which are kept unchanged as a historical appendix at the end of this file
(„Appendix — pre-pilot plan“). Where the appendix is still referred to (its
dimension catalogue D1…, its cross-topic boundaries, its image policy and its
distractor guidance), this is said explicitly.

A plan only: no question was collected, no candidate dataset was created,
nothing was approved, exported or imported, and no code, test, structure file or
database was changed.

Evidence used: `docs/reviews/biology_wave1_pilot_independent_review.md`,
`docs/reviews/biology_wave1_pilot_resolution.md` (both historical, not edited),
the four reviewed import batches under `data/import_batches/subjects/biology/`,
`docs/QUESTION_COLLECTION_PIPELINE.md` (current rules) and
`docs/reviews/biology_curriculum_structure_review.md`.

**Curriculum authority.** The content boundary is the official МОН programme of
each grade, as distilled in the tracked structure review and the structure files
under `data/subject_structures/biology/`:

- Grade 8: Приложение №18. Grade 8 Biology is framework-plan dependent; the
  structure review's treatment of that is authoritative and is not
  reinterpreted here.
- Grade 9: the standard 36-hour programme, Приложение №11. The 90-hour
  intensive-language variant (Grade 8 and 9 combined) is **not** used to
  broaden any Grade 9 bank.
- Grade 10: Приложение №7.
- Grades 11–12 (profiled preparation): out of scope.

`mon.bg` and the external science sites were blocked by the egress proxy of the
environment that prepared this recalibration, so the programmes were not
re-fetched; the tracked structure review was used instead. Collector
environments may differ, which is why rule B8 (source preflight) exists.

## A. Post-pilot lessons

Wave 1 collected 159 raw candidates in four narrow-to-medium topics. The
independent review kept 109, revised 35 and deleted 15; 144 were imported
(83 easy / 56 medium / 5 hard). What that changes:

1. **Yield held, kind did not.** 91 % of raw candidates survived, better than the
   75–85 % the old plan assumed, but 109 of the 144 survivors are recall and the
   collectors' own difficulty labels were inflated in every dataset (49/86/24
   claimed, 83/56/5 after review). The shortfall is in cognitive variety, not in
   count. Ceilings are therefore now derived from target ÷ 0.92 instead of
   target ÷ 0.75–0.85, and every topic carries an explicit reasoning floor.
2. **Narrow topics saturate at 32–37.** A 40 cap filled to the cap produced 8, 3
   and 4 re-angled duplicates. Narrow topics now target 28–36 final, and every
   ceiling is a maximum with an operational early stop (section K).
3. **The same fact is asked four ways.** Definition, reverse definition, "why"
   form and scenario form of one fact are now one fact (B5), recorded in a
   `research.concept` label that carries the dimension id.
4. **Unnecessary numbers are fragile.** P3-015 rested on figures an
   authoritative source contradicts. Numbers are now classified per topic
   (section D) and the default is no exact number.
5. **Model memory is not a source.** Three of the four pilot runs cited no
   external source at all, and P3-031 had a key that is wrong as worded. Health,
   safety and numeric claims now need a fetched authoritative source (B7, F).
6. **Plain text only.** Table-dependent prompts do not render; data are given in
   sentences.
7. **Skill tags were inconsistent.** Each topic now has a fixed 5–8 tag
   vocabulary (section E).
8. **Broad topics are untested.** Only one half of one broad topic (chemical
   composition, run-1) was collected. Section I makes the first broad two-run
   topic a blocking checkpoint (CP1) before any other broad topic is collected.
9. **Pilot topic status.** Cell and tissues, skin and human evolution are
   complete; chemical composition needs a real second run (section D.1).

## B. Collection rules now in force

The current pipeline document governs; these are the Biology-specific
applications of it. MUST/SHOULD as in the pipeline.

**B1. One run = one topic = one dataset = one session.** Dataset
`data/question_candidates/biology/grade-<n>/<topic slug>-run-<k>.json`, run id
`<topic slug>-run-<k>`. No two sessions ever own the same file. Run k of a topic
starts only after run k−1 is finished and, where section I says so, reviewed.

**B2. Ceilings, not quotas (MUST).** Every number in sections C and H is a
maximum. A run that stops below it with `stop_reason: topic_complete` and a
stated reason is the expected outcome, not a failure. No run exceeds 40.

**B3. Run mission.** A run writes only for its own dimensions (section D). A
later run reads every earlier run of its topic first and lists the
concepts already covered in `run.notes`.

**B4. Difficulty by cognitive demand (MUST).** easy = direct recall or
recognition; medium = one connection, comparison, interpretation or
application; hard = multi-step reasoning or careful discrimination inside the
programme. Unfamiliar wording, a number, a long stem or a rare term never makes
an item hard. No E/M/H quota is imposed. As a flexible expectation, a healthy
bank lands roughly at 35–50 % easy, 40–50 % medium and 5–15 % hard; the
reviewer, not the collector, sets the final label.

**B5. One named fact, one candidate (MUST).** Before writing, name the fact or
relation in `research.concept` as `D<n> · <short Bulgarian label>` (for example
`D2 · алвеоли – газова обмяна`). A second item on the same label is allowed only
if it uses a different reasoning tag (E) *and* needs a second fact. The reverse
question, the "why" question and a scenario whose answer is the same fact are
the same label and are rejected.

**B6. Reasoning floor (SHOULD).** Each topic's floor is in the master table
(≥ 30–50 %). An item counts as reasoning only if it needs more than one recalled
fact; a scenario wrapped around one fact is recall. The run report gives the
recall/reasoning split by skill tag. Deviations below one third are justified
per topic in section D.

**B7. Numbers and health claims (MUST).** No exact percentage, count, date,
volume or concentration unless section D lists it as CURRICULUM-REQUIRED or
PEDAGOGICALLY USEFUL; anything listed as UNNECESSARY PRECISION is not written.
Data needed for reasoning are supplied in the stem. Any figure that affects
correctness, and any disease, prevention, first-aid, nutrition, substance or
medical claim, is checked against a fetched source from section F, named with
page and what it confirms in `research.notes`. A distractor must never be a
real-world acceptable action.

**B8. Source preflight (MUST for HIGH-risk topics).** At the start of a run the
collector fetches one page from each source the topic requires (section F). If a
required source is unreachable, the collector does not fall back to model
memory for the claims that source would verify: those items are not written,
and the report lists the blocked source and the dimensions left uncovered.

**B9. Duplicate control (MUST).** Validate with `--db` and with `--compare`
against every earlier run of the topic and every collected neighbour listed in
section G. Neighbour checks are read-only; a run never edits another dataset.

**B10. Plain-text prompts and option hygiene (MUST).** No table-dependent stems,
no Markdown tables, no „всички изброени“ / „нито едно от изброените“, four
options of similar length and grammar, the key not cued by length or wording.
Distractors follow the appendix's „Distractors“ guidance. Image-dependent items
are not written (appendix §5).

**B11. Scope.** The appendix's §4 boundary table and its out-of-scope list stay
in force (no amitosis, no „висша нервна дейност“, no psychosexual stages, no
race-related results removed in 2023, nothing from Grades 11–12).

**B12. Collectors do not** approve, export or import; touch tracked application
files, structure files, the database or the vault; or stage or commit.

Command template (one run):

```
python3 -B -m src.start_question_collection \
  --subject biology --grade <n> --topics biology-<topic slug> \
  --structure data/subject_structures/biology/grade-<n>.json \
  --max-pages 8 --max-candidates <run max> --max-per-source <run max> \
  --max-topics 1 --max-runtime-minutes 60 \
  --run-id biology-<topic slug>-run-<k> --skills "<section E vocabulary>" \
  --output data/question_candidates/biology/grade-<n>/biology-<topic slug>-run-<k>.json
```

## C. Master table

Breadth: NARROW / NORMAL / BROAD / VERY_BROAD. "Target final range" is the
topic's total bank after review; "additional final expected" is what the
remaining runs should add. Raw ceiling = sum of run maxima = ⌈upper target ÷
0.92⌉ (the pilot's survival rate). Review checkpoints are defined in section I.

| Grade | Section | Topic | Status | Breadth | Scientific risk | Current final | Target final range | Additional final expected | Raw ceiling | Runs (max each) | Reasoning target | Collector model | Review checkpoint |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 8 | Структурна организация на човешкия организъм | Клетка и тъкани | **COMPLETE** | NARROW | LOW | 32 | 32 | 0 | 0 | — | — | — | Pilot review (done) |
| 8 | Обмяна на веществата | Храносмилателна система | **TO COLLECT** | BROAD | HIGH | 0 | 48–58 | 48–58 | 64 | r1 (34) + r2 (30) | ≥ 35 % | Sonnet High | CP1 broad checkpoint (blocking) |
| 8 | Обмяна на веществата | Дихателна система | **TO COLLECT** | NORMAL | MEDIUM | 0 | 36–42 | 36–42 | 46 | r1 (26) + r2 (20) | ≥ 35 % | Sonnet Medium | r1 in CP1; bank R5 |
| 8 | Обмяна на веществата | Отделителна система | **TO COLLECT** | NARROW | MEDIUM | 0 | 30–36 | 30–36 | 40 | r1 (40) | ≥ 33 % | Sonnet Medium | R2 |
| 8 | Обмяна на веществата | Сърдечно-съдова система | **TO COLLECT** | VERY_BROAD | HIGH | 0 | 62–72 | 62–72 | 79 | r1 (28) + r2 (26) + r3 (25) | ≥ 35 % | Sonnet High | r1 CP2 science gate; bank R7 |
| 8 | Движение и опора на тялото | Опорно-двигателна система – скелет и скелетни мускули | **TO COLLECT** | BROAD | MEDIUM | 0 | 46–54 | 46–54 | 59 | r1 (30) + r2 (29) | ≥ 35 % | Sonnet Medium | r1 R8; bank R9 |
| 8 | Размножаване, растеж и развитие | Полова система | **TO COLLECT** | NORMAL | HIGH | 0 | 36–42 | 36–42 | 46 | r1 (26) + r2 (20) | ≥ 33 % | Sonnet High | r1 R5 science gate; bank R8 |
| 8 | Размножаване, растеж и развитие | Зародишно и следзародишно развитие | **TO COLLECT** | NARROW | MEDIUM | 0 | 28–34 | 28–34 | 37 | r1 (37; collected 27) + r2 (≤ 10 supplement) | ≥ 30 % | Sonnet Medium | R2; r2 R2b |
| 8 | Регулация и хомеостаза | Нервна система | **TO COLLECT** | BROAD | HIGH | 0 | 48–58 | 48–58 | 64 | r1 (34) + r2 (30) | ≥ 35 % | Sonnet High | r1 CP2 science gate; bank R6 |
| 8 | Регулация и хомеостаза | Ендокринна система | **TO COLLECT** | NORMAL | HIGH | 0 | 36–44 | 36–44 | 48 | r1 (26) + r2 (22) | ≥ 40 % | Sonnet High | r1 R8 science gate; bank R9 |
| 8 | Регулация и хомеостаза | Сетивни системи | **TO COLLECT** | BROAD | MEDIUM | 0 | 42–50 | 42–50 | 55 | r1 (28) + r2 (27) | ≥ 35 % | Sonnet Medium | r1 R9; bank R10 |
| 8 | Регулация и хомеостаза | Кожа | **COMPLETE** | NARROW | MEDIUM | 37 | 37 | 0 | 0 | — | — | — | Pilot review (done) |
| 9 | Химичен състав на живата материя | Химичен състав на живата материя | **SUPPLEMENT PLANNED** | BROAD | MEDIUM | 39 | 61–66 | 22–27 | 30 | r2 (30) | ≥ 45 % | Sonnet High | CP1 |
| 9 | Надмолекулни комплекси | Надмолекулни комплекси | **TO COLLECT** | NARROW | HIGH | 0 | 30–36 | 30–36 | 40 | r1 (40) | ≥ 33 % | Sonnet High | R2 |
| 9 | Структура и процеси в клетката | Структура и процеси в клетката | **TO COLLECT** | VERY_BROAD | HIGH | 0 | 68–80 | 68–80 | 87 | r1 (30) + r2 (29) + r3 (28) | ≥ 40 % | Sonnet High | r1 CP2 science gate; bank R7 |
| 9 | Възпроизводство на клетката | Възпроизводство на клетката | **TO COLLECT** | NORMAL | HIGH | 0 | 40–48 | 40–48 | 53 | r1 (28) + r2 (25) | ≥ 45 % | Sonnet High | r1 R5 science gate; bank R8 |
| 9 | Приложение на знанията за клетката | Приложение на знанията за клетката | **TO COLLECT** | NARROW | MEDIUM | 0 | 22–30 | 22–30 | 33 | r1 (33) | ≥ 30 % | Sonnet Medium | R11 |
| 10 | Многоклетъчен организъм | Наследственост и изменчивост | **TO COLLECT** | VERY_BROAD | HIGH | 0 | 68–80 | 68–80 | 87 | r1 (30) + r2 (29) + r3 (28) | ≥ 50 % | Sonnet High | r1 CP2 science gate; bank R7 |
| 10 | Многоклетъчен организъм | Размножаване, растеж и индивидуално развитие | **TO COLLECT** | NORMAL | MEDIUM | 0 | 42–50 | 42–50 | 55 | r1 (28) + r2 (27) | ≥ 40 % | Sonnet Medium | r1 R10; bank R11 |
| 10 | Биосфера | Популация, биоценоза, екосистема | **TO COLLECT** | BROAD | MEDIUM | 0 | 50–60 | 50–60 | 66 | r1 (34) + r2 (32) | ≥ 40 % | Sonnet Medium | r1 R10; bank R11 |
| 10 | Биосфера | Екологични фактори | **TO COLLECT** | NORMAL | MEDIUM | 0 | 36–44 | 36–44 | 48 | r1 (26) + r2 (22) | ≥ 40 % | Sonnet Medium | r1 in CP1; bank R5 |
| 10 | Биологична еволюция | Произход и развитие на живата материя | **TO COLLECT** | BROAD | HIGH | 0 | 50–60 | 50–60 | 66 | r1 (34) + r2 (32) | ≥ 40 % | Sonnet High | r1 R10 science gate; bank R11 |
| 10 | Биологична еволюция | Произход и еволюция на човека | **COMPLETE** | NARROW | HIGH | 36 | 36 | 0 | 0 | — | — | — | Pilot review (done) |
| 10 | Биологична еволюция | Доказателства за биологичната еволюция | **TO COLLECT** | NORMAL | MEDIUM | 0 | 36–44 | 36–44 | 48 | r1 (26) + r2 (22) | ≥ 40 % | Sonnet Medium | r1 R7; bank R9 |

Collector models: Sonnet 5.5 High on 23 runs (every HIGH-risk topic, plus the
chemical-composition supplement because its first run produced the pilot's
wrong key and contradicted figure); Sonnet 5.5 Medium on 17 runs. No collection
on Opus 5.5; Opus 5.5 High does every review in section I and adjudicates
ambiguous high-risk content.

## D. Per-topic notes

Fields A–G are in the master table, H in section E, K in section F and L in the
master table and section I. Dimension ids D1… refer to the appendix's coverage
matrix (§2); where a run mission differs from that split, it is restated here.
Numbers: **REQ** = curriculum-required, **USE** = pedagogically useful (supplied
in the stem or checked), **NO** = unnecessary precision (not written).

### D.1 Pilot topics

**G8 Клетка и тъкани — COMPLETE (32).** Inside the narrow range and the review's
32–36. The review found only 4–6 further distinct items, all image-dependent
(tissue recognition), which the plan defers. Reasoning is 8/32 (25 %), below a
third; accepted because the sub-theme is recognition-centred and its reasoning
gap can only be closed by the later image pass. No run. Reopen only with that
pass.

**G8 Кожа — COMPLETE (37).** Inside the review's recommended 36–40. The review
named 3–5 further distinct items (hair and nails as derivatives, harder
thermoregulation items); one run, its comparison and its review cost more than
those items add, and the review itself recorded "none needed". Reasoning 10/37
(27 %). Not scheduled. If the final Grade 8 review (section I) finds a real
coverage gap, the only permitted supplement is: maximum 8 raw, dimensions
„производни на кожата“ and thermoregulation prediction, Sonnet Medium,
`--compare` against the skin and cell-and-tissues banks, stop at the first
candidate that repeats a named fact or needs a first-aid claim without a fetched
source.

**G9 Химичен състав на живата материя — SUPPLEMENT PLANNED (substantial
addition), 39 → 61–66.** The first run covered D1–D5 only; D6–D9 (enzymes,
nucleic acids, grouping by monomers, living vs non-living) are part of the
programme's expected results and have almost no items. The review recommended
25–30 more raw, capped near 32, with a reasoning quota.

- Run-2: maximum **30** raw, Sonnet 5.5 High, expected +22–27 final.
- Mission: D6 enzymes (nature, specificity, effect of temperature and pH as
  prediction, not values); D7 DNA vs RNA (composition, structure, location,
  function: comparison, not molecular mechanism, which is G9 theme 3); D8
  grouping biopolymers by monomer and comparing groups; D9 only as reasoning
  from described data (005/006 already cover the recall form).
- Reasoning floor ≥ 45 % of the run (P3 had 6/39 = 15 %, the pilot's lowest);
  the whole bank then reaches at least about 26 %, accepted because run-1's correct
  recall items stay.
- Numbers: REQ none; USE element groups named without percentages; NO
  percentages of elements or water, molecular masses, temperature or pH optima.
- Early stop: when each of D6–D9 has its items, or the next candidate names a
  monomer, a definition or a function already labelled in run-1, or needs
  replication/transcription detail (G9 theme 3). `--compare` against run-1 and
  `g8-digestive-system` run-1 (nutrients).

**G10 Произход и еволюция на човека — COMPLETE (36), saturated.** The collector
reported saturation and the review confirmed it: 4 of 40 were re-angled
duplicates and D5 has only three independent arguments. The 4–6 items the review
could still imagine (fire, art, burial per stage) would mostly re-test stage
features already covered. Reasoning 11/36 (31 %). No run, and none may be added.
Later evolution runs compare against this bank instead.

### D.2 Grade 8

**Храносмилателна система** (CP1 topic)
- r1 (34): D1–D4 — organs in order, structure ↔ function, mechanical vs chemical
  processing, digestion → absorption as a sequence, nutrients. r2 (30): D5–D7 —
  diseases (cause – signs – prevention), alcohol / energy drinks / diets /
  overeating, applied data (menu, BMI, energy balance from given values).
- Reasoning ≥ 35 %: predict the effect of a blocked or missing organ function,
  compare digestion sites, judge a described menu.
- Numbers: REQ BMI only as the lab calculation with values supplied; USE
  energy values supplied in the stem; NO intestine lengths, enzyme pH values,
  daily-intake norms or calorie figures to memorise.
- Risks: organ → function and "where is X digested" re-angled; diet items
  drifting into general advice without biology; nutrient definitions repeating
  chemical composition.

**Дихателна система**
- r1 (26): D1–D3 airways and lungs, breathing movements, gas exchange,
  structure ↔ function. r2 (20): D4–D6 air composition and its effect, diseases,
  breathing-rate and vital-capacity readings.
- Numbers: USE inhaled vs exhaled air composition only when given in the stem;
  vital-capacity values supplied; NO alveolus counts, lung surface areas.
- Risks: airway order and "where gas exchange happens" rewritten; smoking items
  repeat; gas transport by blood belongs to cardiovascular.

**Отделителна система** (single run, narrow)
- r1 (40): D1–D6 — organs and order, kidney at school depth, formation and
  removal of urine, urine composition norm vs pathology as described data,
  diseases, link with blood and water balance. Expected early stop near 34–36:
  six dimensions, small organ set.
- **Final bank (R2 resolution): 30 candidates; genuine reasoning 8/30 (~27 %)
  against the planned ≥ 33 % signal. No supplement is justified, and no
  excretory run-2 is created.** All six dimensions are covered and the topic is
  inside its 30–36 final target; the remaining apparent reasoning opportunities
  are re-angles of existing facts or belong to skin, endocrine or other topics.
  Accepted coverage-driven exception under the plan's quality/early-stop
  philosophy.
- One run of up to 40 rather than the 20–30 suggested for narrow topics: the
  whole bank fits one mission and the pilot showed narrow topics saturating at
  32–37; splitting would double comparison overhead for no new coverage.
- Numbers: USE urine values supplied as data; NO daily volumes, nephron counts.
- Risks: urine-formation steps asked repeatedly; sweat as excretion overlaps the
  skin bank.

**Сърдечно-съдова система** (VERY_BROAD, CP2 gate)
- r1 (28): D1–D3 heart, vessels, two circulation loops as a sequence.
  r2 (26): D4–D6 blood and formed elements, blood groups and transfusion
  compatibility, immunity, vaccines, immunisation. r3 (25): D7–D8 diseases and
  blood-borne infection, personal responsibility, pulse and blood pressure at
  rest and under load as data; integration items across r1–r2.
- Reasoning ≥ 35 %: trace a blood path, predict compatibility, interpret
  measurements.
- Numbers: REQ none to memorise; USE pulse and pressure values supplied in the
  stem; NO blood-cell counts, blood volume, heart-rate norms as recall.
- Risks: four fact families (vessels, loops, blood cells, groups) each easy to
  over-sample; HIV and hepatitis overlap supramolecular complexes and the
  reproductive topic; immunity and vaccines are owned here.

**Опорно-двигателна система – скелет и скелетни мускули**
- r1 (30): D1–D4 skeleton parts and functions, bone connections and joints,
  muscle structure, work and fatigue, lever system. r2 (29): D5–D7 comparison
  with the mammal skeleton, injuries and diseases, posture, first aid for
  trauma.
- Numbers: NO bone counts, muscle counts.
- Risks: bone-naming lists; bone and muscle tissue already in cell and tissues;
  first aid for burns stays with skin.

**Полова система** (R5 gate)
- r1 (26): D1–D3 male and female organs, gametes, ovulation and the menstrual
  cycle as a sequence. r2 (20): D4–D6 reproductive health, sexually transmitted
  infections, applying responsible-behaviour rules to a situation.
- Numbers: USE "about mid-cycle" for ovulation at most; NO cycle-day
  arithmetic, sperm counts or contraceptive effectiveness figures.
- Risks: STI items repeating one prevention fact; age-appropriate wording; no
  psychosexual stages (removed 2023); hormones belong to endocrine.

**Зародишно и следзародишно развитие** (single run, narrow)
- r1 (37): D1–D6 fertilisation, embryonic stages in order, placenta and
  umbilical cord, birth, post-embryonic periods, risks to development.
- Reasoning ≥ 30 %, below a third: a stage-order and recognition sub-theme;
  reasoning comes from risk-factor and placenta-function items.
- **r2 supplement** `biology-g8-embryonic-and-postembryonic-development-run-2`
  (ceiling 10, not a quota; early stop expected): targeted at the D5/D6
  coverage gaps shown by run-1 (27 collected, 22 after R2 resolution). The
  original allocation was 37 raw, so run-1 (27) + run-2 (10) stays inside it and
  the overall Biology raw ceiling of 1,151 is unchanged. Boundary: Grade 8
  topic 4.2 only.
  - D5: postembryonic development that the official Grade 8 4.2 wording
    supports (comparison, sequence, growth, responsibility for offspring). The
    programme names only the MAIN stages and gives no canonical list of
    postnatal periods, so no detailed period names unless justified by
    curriculum-aligned source evidence; otherwise no unsupported vocabulary.
  - D6 maxima (not quotas): infection in pregnancy and prevention 2; medicine
    without medical advice 1; drugs 1; at most one cause/effect item on timing of
    harmful exposure relative to the organ-forming period, if scientifically and
    curriculum-supported.
  - Exclude: week-by-week detail; gametogenesis/cleavage (owned elsewhere);
    endocrine/hormone detail; treatment detail; duplicated alcohol, smoking,
    placenta and first-breath content; newborn thermoregulation/heat loss;
    re-angles of run-1 facts.
  - Skills: recall, sequence, explain, compare, apply-health. Reasoning target
    ≥ 45 % genuine reasoning among survivors; no scenario-wrapped recall.
  - Sources for pregnancy/health/developmental-risk claims: fetched content
    from WHO, CDC, NHS, MedlinePlus or OpenStax; search snippets do not count;
    an unsupported claim means the candidate is dropped.
  - Compare against embryonic run-1 and, where relevant, excretory.
  - Review R2b (Opus 5.5 High mini-review) reads run-1 and run-2 side by side;
    the topic is not COMPLETE until R2b and the correction/validation pass.
  - **R2b outcome: TOPIC COMPLETE; no run-3.** Final pending survivors: run-1
    22, run-2 5 (8 collected against the ceiling of 10), combined 27 against
    the projected 28–34. The numerical shortfall is accepted because coverage is
    complete. Genuine reasoning after independent review: run-2 0/5, combined
    5/27 (~19 %) against the ≥ 30 % floor. Accepted under B6 and the
    coverage-driven early-stop policy: R2b found that raising reasoning would
    need duplicate re-angles, unsupported postnatal terminology, or content
    owned by the reproductive/endocrine topics. Ceilings are not quotas, so the
    overall raw ceiling (1,151) and the remaining run count (41) are unchanged.
- Numbers: USE about nine months / 40 weeks; NO week-by-week milestones.
- Risks: stage-order items repeat; gametogenesis and cleavage detail are G10.

**Нервна система** (CP2 gate)
- r1 (34): D1–D4 neuron and nervous tissue, CNS parts and functions, PNS, reflex
  arc as a sequence. r2 (30): D5–D8 unconditioned vs conditioned reflexes,
  nervous regulation and homeostasis from the nervous side, diseases and
  injuries, alcohol and psychoactive substances.
- Numbers: NO neuron counts, impulse speeds.
- Risks: brain part → function lists; substance items repeating one warning;
  nervous vs hormonal comparison is owned by endocrine.

**Ендокринна система** (R8 gate)
- r1 (26): D1–D4 glands and location, hormones and effects, feedback regulation,
  nervous vs hormonal comparison. r2 (22): D5–D7 homeostasis of the whole
  organism, too much / too little hormone, hormonal preparations at
  discussion level.
- Reasoning ≥ 40 %: feedback and disorder prediction are reasoning by nature.
- Numbers: USE blood-glucose values only when supplied; NO hormone
  concentrations.
- Risks: gland → hormone → effect triples rewritten; sex hormones and the cycle
  overlap the reproductive topic; pancreas overlaps digestive.

**Сетивни системи**
- r1 (28): D1–D4 receptor – pathway – centre, eye parts, path of light and
  signal, common defects, ear, hearing and balance. r2 (27): D5–D7 taste, smell,
  skin senses, disorders and prevention, visual-system observations.
- Numbers: USE noise levels only if supplied; NO rod and cone counts.
- Risks: eye-part and ear-part lists; skin receptors are already in the skin
  bank; receptor → pathway overlaps nervous.

### D.3 Grade 9

**Надмолекулни комплекси** (single run, narrow)
- r1 (40): D1–D5 — definition and examples, virus composition and why it is not
  a cell, virus reproduction stages, viral diseases (route – signs –
  prevention), prophylaxis applied to a situation. Expected early stop near
  34–36.
- Numbers: NO virus sizes, incubation periods unless a WHO page is fetched and
  the period is the point of the item.
- Risks: one prevention fact repeated across viruses; vaccines stay at
  prophylaxis level (immunity is G8); bacteria are theme 3.

**Структура и процеси в клетката** (VERY_BROAD, CP2 gate)
- r1 (30): D1–D4 cell theory, prokaryote vs eukaryote, membrane, nucleus and
  cytoplasm. r2 (29): D5–D8 organelles structure ↔ function, plant vs animal
  cell, bacteria and bacterial diseases, passive vs active transport, osmosis,
  cytoses. r3 (28): D9–D12 metabolism (anabolic vs catabolic, photosynthesis and
  respiration at programme depth), replication – transcription – translation
  (what, where, order), process ↔ structure integration, described
  observations.
- Reasoning ≥ 40 %: the programme asks to link processes to structures.
- Numbers: NO ATP yields, codon counts, organelle sizes.
- Risks: organelle → function is the main trap; nucleic-acid composition is
  owned by chemical composition; chromosomes and division are owned by cell
  reproduction.

**Възпроизводство на клетката** (R5 gate)
- r1 (28): D1–D3 life and mitotic cycle, interphase, mitosis phases, meiosis as
  two divisions. r2 (25): D4–D6 mitosis vs meiosis, chromosome number before and
  after division, significance, differentiation.
- Reasoning ≥ 45 %: chromosome tracing and comparison.
- Numbers: REQ n / 2n logic; USE human 46 / 23; NO phase durations, DNA amounts
  in picograms.
- Risks: phase-order items; chromosome arithmetic with changed numbers only;
  amitosis removed in 2023; inheritance is G10.

**Приложение на знанията за клетката** (single run, recall-dominated)
- r1 (33): D1–D5 biotechnology, medicine, agriculture, environmental protection,
  each application tied to the cell process it uses.
- Target 22–30, below the narrow range, and reasoning ≥ 30 %: the programme
  only asks to describe and illustrate. Every item must link an application to
  a different process from themes 1–4; collected last in Grade 9 so it can
  compare against all four.
- Numbers: NO.
- Risks: field → example pairs; every G9 topic and G8 vaccines.

### D.4 Grade 10

**Наследственост и изменчивост** (VERY_BROAD, CP2 gate)
- r1 (30): D1–D4 core terms (each asked once), Mendel's laws, monohybrid and
  dihybrid crosses, allelic interactions. r2 (29): D5–D8 non-allelic
  interactions, variation, modifications vs mutations, human-genetics methods,
  hereditary diseases, pedigrees, sex determination. r3 (28): D9–D10 reading
  schemes and karyograms described in text, factors of variation, and
  multi-step problems combining r1–r2 ideas.
- Reasoning ≥ 50 %: the programme is problem-centred.
- Numbers: REQ the segregation ratios 3:1, 1:2:1, 1:1, 9:3:3:1 and the
  programme's interaction ratios (for example 9:7, 9:3:4, 12:3:1, 15:1, 2:1),
  each checked against a fetched source; NO disease prevalence figures.
- Risks: crosses with swapped traits are the same exercise (budget by cross type
  and interaction type); meiosis is G9 and is used, not re-tested.

**Размножаване, растеж и индивидуално развитие**
- r1 (28): D1–D4 asexual vs sexual reproduction, gametogenesis phases,
  spermatogenesis vs oogenesis, external vs internal fertilisation, embryonic
  stages. r2 (27): D5–D7 direct vs indirect development, growth and
  differentiation, factors of human development.
- Numbers: NO.
- Risks: overlaps G8 embryonic development and G8 reproductive organs; meiosis
  events are G9.

**Популация, биоценоза, екосистема**
- r1 (34): D1–D4 levels in order, population structure and distribution,
  biocenosis and niche, ecosystem components, food chains and webs. r2 (32):
  D5–D8 energy flow and pyramids, cycles of matter, succession and productivity,
  biosphere.
- Reasoning ≥ 40 %.
- Numbers: USE the roughly 10 % transfer rule, checked, with values supplied
  for calculations; NO biome productivity figures.
- Risks: food-chain items with swapped organisms; biotic relations overlap
  ecological factors.

**Екологични фактори**
- r1 (26): D1–D3 classifying factors, abiotic factors and tolerance, biotic
  relations as factors. r2 (22): D4–D6 adaptations and their relative nature,
  assessing a described anthropogenic impact, predicting change from data.
- Numbers: data only when supplied.
- Risks: factor → example classification; adaptation as the result of selection
  belongs to origin and development.

**Произход и развитие на живата материя** (R10 gate)
- r1 (34): D1–D4 hypotheses for the origin of life (as hypotheses), Darwin,
  modern theory, Darwinian vs modern factors. r2 (32): D5–D8 species and its
  criteria, speciation and microevolution, macroevolution and its directions,
  applying a factor to a described case.
- Numbers: USE the age of the Earth at "about 4.5 billion years" at most; NO
  dates of experiments or geological periods to memorise.
- Risks: the two classic comparisons get rewritten; evidence items belong to
  3.3; human evolution is saturated and is only compared against.

**Доказателства за биологичната еволюция**
- r1 (26): D1–D4 groups of evidence, homologous vs analogous, rudimentary
  organs, comparative embryology and physiology. r2 (22): D5–D7 fossils, index
  fossils, transitional forms, phylogenetic series, choosing evidence for a
  stated claim.
- Numbers: NO fossil ages.
- Risks: homologous / analogous classification with a new organ each time is
  one item type, capped; human fossils belong to 3.2.

## E. Skill vocabularies

`research.skill` holds one tag; the dimension is in `research.concept` (B5).
Only `recall` counts as recall; every other tag claims that the item needs more
than one fact, which the reviewer audits.

| Tag | Meaning |
|---|---|
| `recall` | one fact, term, name, single structure → function or a memorised order |
| `explain` | why or how, linking two facts |
| `compare` | two named items on a stated criterion |
| `classify` | place an unfamiliar example in a category |
| `sequence` | reconstruct or check a process from its logic, not a memorised list |
| `structure-function` | infer function from structure or the reverse for an unfamiliar case |
| `predict` | consequence of a change or disorder |
| `apply-health` | apply a verified health, prevention or first-aid rule to a situation |
| `interpret-data` | read and conclude from data supplied in the stem |
| `calculate` | a short calculation with supplied values |
| `link-process` | tie an application to the cell process it rests on |
| `cross` / `pedigree` / `interaction` | heredity problem types |
| `argue` | choose the evidence or argument that supports a stated claim |

| Topic | Vocabulary (declared with `--skills`) |
|---|---|
| G8 digestive | recall, explain, compare, sequence, predict, apply-health, interpret-data |
| G8 respiratory | recall, explain, compare, sequence, predict, apply-health, interpret-data |
| G8 excretory | recall, explain, sequence, compare, apply-health, interpret-data |
| G8 cardiovascular | recall, explain, compare, sequence, predict, apply-health, interpret-data, calculate |
| G8 musculoskeletal | recall, structure-function, compare, classify, explain, apply-health |
| G8 reproductive | recall, explain, compare, sequence, predict, apply-health |
| G8 embryonic and postembryonic | recall, sequence, explain, compare, apply-health |
| G8 nervous | recall, structure-function, sequence, compare, explain, predict, apply-health |
| G8 endocrine | recall, explain, compare, predict, interpret-data, apply-health |
| G8 sensory | recall, structure-function, sequence, compare, predict, apply-health |
| G8 skin (conditional only) | recall, explain, predict, apply-health |
| G9 chemical composition r2 | recall, compare, classify, explain, predict, interpret-data |
| G9 supramolecular | recall, explain, sequence, compare, apply-health |
| G9 cell structure | recall, structure-function, compare, sequence, explain, predict, interpret-data, link-process |
| G9 cell reproduction | recall, sequence, compare, calculate, predict, explain |
| G9 application | recall, link-process, compare, explain, classify |
| G10 heredity | recall, cross, interaction, pedigree, classify, explain, interpret-data, predict |
| G10 reproduction and development | recall, compare, sequence, explain, classify, predict |
| G10 population–ecosystem | recall, classify, explain, compare, calculate, predict, interpret-data |
| G10 ecological factors | recall, classify, explain, predict, interpret-data, compare |
| G10 origin and development | recall, compare, explain, classify, predict, argue |
| G10 evidence | recall, classify, explain, compare, argue |

## F. Source requirements

Scope comes from the programme; correctness from an authoritative source. A
deeper source never widens the bank. Stable textbook facts do not need a web
fetch; the items below do (B7). Third-party pages are research only; questions
are written new.

| Topic | Must be verified from a fetched source | Sources |
|---|---|---|
| G8 digestive | diet and nutrition advice, BMI categories, alcohol and energy-drink effects, disease prevention | WHO healthy-diet and BMI pages; NHS; NIH NIDDK; OpenStax *Anatomy and Physiology 2e* |
| G8 respiratory | smoking effects, respiratory disease prevention, air-quality effects | WHO; NHS; OpenStax A&P |
| G8 excretory | kidney and urinary disease causes and prevention, urine findings | NIH NIDDK; NHS; OpenStax A&P |
| G8 cardiovascular | transfusion compatibility, vaccine and immunity claims, blood-borne infections, cardiovascular risk factors, first aid for bleeding | WHO; CDC; NHS Blood and Transplant; Red Cross; OpenStax A&P |
| G8 musculoskeletal | first aid for fractures, sprains and dislocations; posture and bone-health claims | Red Cross / Bulgarian Red Cross; NHS; OpenStax A&P |
| G8 reproductive | STI transmission and prevention, HPV vaccination, cycle hormone sequence | WHO STI fact sheets; CDC; OpenStax A&P |
| G8 embryonic and postembryonic | risks in pregnancy (alcohol, smoking, infections) | WHO; CDC; NHS |
| G8 nervous | effects of alcohol and psychoactive substances, injuries, disease prevention | WHO; NIH NIDA; NHS; OpenStax A&P |
| G8 endocrine | diabetes, thyroid and iodine claims, hormonal preparations | WHO; NIH NIDDK; NHS |
| G8 sensory | vision defects and correction, noise-induced hearing loss, eye first aid | NIH NEI; WHO; NHS |
| G9 chemical composition r2 | enzyme properties, DNA vs RNA composition | OpenStax *Biology 2e*; NCBI Bookshelf |
| G9 supramolecular | virus structure and life cycle, viral disease routes and prevention, vaccines | OpenStax *Biology 2e*; WHO fact sheets; CDC |
| G9 cell structure | organelle functions, transport, where each genetic process occurs, bacterial diseases | OpenStax *Biology 2e*; NCBI Bookshelf; WHO / CDC |
| G9 cell reproduction | phase events, chromosome vs chromatid counts | OpenStax *Biology 2e*; NCBI Bookshelf |
| G9 application | each named application's biology (for example insulin from bacteria, antibiotics, vaccines) | OpenStax *Biology 2e*; NIH; WHO |
| G10 heredity | every ratio, interaction definitions, sex-linked inheritance, hereditary diseases | OpenStax *Biology 2e*; NIH MedlinePlus Genetics; NHGRI |
| G10 reproduction and development | gametogenesis phases, development types | OpenStax *Biology 2e* |
| G10 population–ecosystem | transfer rule, pyramid types, succession | OpenStax *Biology 2e*; Nature Scitable |
| G10 ecological factors | anthropogenic examples, tolerance | OpenStax; EEA / UNEP for examples only |
| G10 origin and development | origin-of-life hypotheses, Darwinian vs modern factors, species criteria | Understanding Evolution (UC Berkeley); OpenStax *Biology 2e* |
| G10 evidence | homologous / analogous / rudimentary examples, transitional forms | Understanding Evolution; Smithsonian Human Origins; OpenStax |

HIGH-risk topics (B8 preflight is mandatory): digestive, cardiovascular,
reproductive, nervous, endocrine, supramolecular complexes, cell structure and
processes, cell reproduction, heredity and variation, origin and development of
living matter (and the completed human-evolution bank). Health and first-aid
claims anywhere in Grade 8 also need a fetched source, whatever the topic's
risk level. Bulgarian terminology follows the programme's wording.

## G. Duplication-risk map

| Pair | What repeats | Control |
|---|---|---|
| G8 nervous ↔ G8 endocrine | regulation comparison, homeostasis | endocrine owns the comparison; endocrine r1 runs after nervous r2 and compares against both nervous runs |
| G8 nervous ↔ G8 sensory | receptors, pathways, brain centres | sensory runs after nervous and compares against it |
| G8 reproductive ↔ G8 embryonic | fertilisation, placenta | never in the same wave; reproductive compares against embryonic |
| G8 embryonic ↔ G10 reproduction and development | embryonic stages | G10 compares against G8; G10 owns gametogenesis and cleavage |
| G8 reproductive ↔ G9 supramolecular ↔ G8 cardiovascular | HIV, hepatitis, HPV, vaccines | immunity and vaccines owned by cardiovascular; viruses by G9; STI behaviour by reproductive; each compares against the earlier ones |
| G8 respiratory ↔ G8 cardiovascular | gas exchange vs gas transport | not in the same wave; cardiovascular compares against respiratory r1 |
| G8 digestive ↔ G9 chemical composition | nutrients vs biomolecules | chemical composition r2 runs after digestive r1 and compares against it |
| G8 skin ↔ G8 cell and tissues ↔ G8 excretory | epithelium, sweat as excretion | excretory and any skin supplement compare against both banks |
| G8 musculoskeletal ↔ G8 cell and tissues | bone and muscle tissue | musculoskeletal compares against cell and tissues |
| G9 chemical composition ↔ G9 cell structure | nucleic acids, proteins, enzymes | composition r2 runs first; cell structure compares against it |
| G9 cell structure ↔ G9 supramolecular | bacteria vs viruses | cell structure compares against supramolecular |
| G9 cell reproduction ↔ G10 heredity | meiosis, chromosomes | not in the same wave; heredity uses meiosis without re-testing it |
| G9 cell reproduction ↔ G10 reproduction and development | meiosis vs gametogenesis | G10 runs later and compares |
| G9 application ↔ all G9 topics | the same processes as examples | collected last and compares against all four |
| G10 population–ecosystem ↔ G10 ecological factors | biotic relations, adaptation | never in the same wave; population compares against factors |
| G10 origin ↔ G10 evidence ↔ G10 human evolution | evidence placement, human fossils | never in the same wave; both compare against the human-evolution bank |

The verifier checks that none of these pairs shares a wave (the digestive ↔
chemical-composition pair is checked at run level, because only digestive run-1
overlaps).

## H. Wave schedule

Wave 1 was the pilot. Waves 2–11 hold the 41 remaining runs (including the embryonic run-2 supplement), at most four
parallel sessions each, with the reviews of section I between them.

| Wave | Session 1 | Session 2 | Session 3 | Session 4 | Ceiling |
|---|---|---|---|---|---|
| W2 | `g8-digestive-system` r1 (34, S-High) | `g8-excretory-system` r1 (40, S-Medium) | `g9-supramolecular-complexes` r1 (40, S-High) | `g8-embryonic-and-postembryonic-development` r1 (37, S-Medium) | 151 |
| **R2** | Opus 5.5 High review: `g8-excretory-system` bank, `g9-supramolecular-complexes` bank, `g8-embryonic-and-postembryonic-development` bank | | | | blocks: none |
| W2b | `g8-embryonic-and-postembryonic-development` r2 (≤ 10 supplement, S-Medium) | | | | ≤ 10, inside the original 37 for the topic; after R2 resolution |
| **R2b** | Opus 5.5 High mini-review: embryonic run-1 and run-2 side by side | | | | blocks: embryonic COMPLETE |
| W3 | `g8-digestive-system` r2 (30, S-High) | `g9-chemical-composition-of-living-matter` r2 (30, S-High) | `g8-respiratory-system` r1 (26, S-Medium) | `g10-ecological-factors` r1 (26, S-Medium) | 112 |
| **CP1** | Opus 5.5 High review: `g8-digestive-system` bank, `g9-chemical-composition-of-living-matter` bank, `g8-respiratory-system` r1, `g10-ecological-factors` r1 | | | | blocks: all of W4 onward |
| W4 | `g8-cardiovascular-system` r1 (28, S-High) | `g9-cell-structure-and-processes` r1 (30, S-High) | `g10-heredity-and-variation` r1 (30, S-High) | `g8-nervous-system` r1 (34, S-High) | 122 |
| **CP2** | Opus 5.5 High review: `g8-cardiovascular-system` r1, `g9-cell-structure-and-processes` r1, `g10-heredity-and-variation` r1, `g8-nervous-system` r1 | | | | blocks: run-2 of the four gated topics (W6) |
| W5 | `g8-respiratory-system` r2 (20, S-Medium) | `g10-ecological-factors` r2 (22, S-Medium) | `g8-reproductive-system` r1 (26, S-High) | `g9-cell-reproduction` r1 (28, S-High) | 96 |
| **R5** | Opus 5.5 High review: `g8-respiratory-system` bank, `g10-ecological-factors` bank, `g8-reproductive-system` r1, `g9-cell-reproduction` r1 | | | | blocks: W8 runs of the two gated topics |
| W6 | `g8-cardiovascular-system` r2 (26, S-High) | `g9-cell-structure-and-processes` r2 (29, S-High) | `g10-heredity-and-variation` r2 (29, S-High) | `g8-nervous-system` r2 (30, S-High) | 114 |
| **R6** | Opus 5.5 High review: `g8-nervous-system` bank | | | | blocks: none |
| W7 | `g8-cardiovascular-system` r3 (25, S-High) | `g9-cell-structure-and-processes` r3 (28, S-High) | `g10-heredity-and-variation` r3 (28, S-High) | `g10-evidence-for-biological-evolution` r1 (26, S-Medium) | 107 |
| **R7** | Opus 5.5 High review: `g8-cardiovascular-system` bank, `g9-cell-structure-and-processes` bank, `g10-heredity-and-variation` bank, `g10-evidence-for-biological-evolution` r1 | | | | blocks: none |
| W8 | `g8-endocrine-system` r1 (26, S-High) | `g8-musculoskeletal-system` r1 (30, S-Medium) | `g9-cell-reproduction` r2 (25, S-High) | `g8-reproductive-system` r2 (20, S-High) | 101 |
| **R8** | Opus 5.5 High review: `g8-reproductive-system` bank, `g9-cell-reproduction` bank, `g8-endocrine-system` r1, `g8-musculoskeletal-system` r1 | | | | blocks: endocrine run-2 (W9) |
| W9 | `g8-endocrine-system` r2 (22, S-High) | `g8-musculoskeletal-system` r2 (29, S-Medium) | `g8-sensory-systems` r1 (28, S-Medium) | `g10-evidence-for-biological-evolution` r2 (22, S-Medium) | 101 |
| **R9** | Opus 5.5 High review: `g8-endocrine-system` bank, `g8-musculoskeletal-system` bank, `g10-evidence-for-biological-evolution` bank, `g8-sensory-systems` r1 | | | | blocks: none |
| W10 | `g8-sensory-systems` r2 (27, S-Medium) | `g10-population-biocenosis-ecosystem` r1 (34, S-Medium) | `g10-origin-and-development-of-living-matter` r1 (34, S-High) | `g10-reproduction-growth-and-individual-development` r1 (28, S-Medium) | 123 |
| **R10** | Opus 5.5 High review: `g8-sensory-systems` bank, `g10-population-biocenosis-ecosystem` r1, `g10-origin-and-development-of-living-matter` r1, `g10-reproduction-growth-and-individual-development` r1 | | | | blocks: origin run-2 (W11) |
| W11 | `g10-population-biocenosis-ecosystem` r2 (32, S-Medium) | `g10-origin-and-development-of-living-matter` r2 (32, S-High) | `g10-reproduction-growth-and-individual-development` r2 (27, S-Medium) | `g9-application-of-cell-knowledge` r1 (33, S-Medium) | 124 |
| **R11** | Opus 5.5 High review: `g10-population-biocenosis-ecosystem` bank, `g10-origin-and-development-of-living-matter` bank, `g10-reproduction-growth-and-individual-development` bank, `g9-application-of-cell-knowledge` bank | | | | blocks: none |

Why this order: narrow single-run topics fill the first wave beside the
checkpoint topic, so CP1 sees a broad topic early without leaving sessions idle;
the three VERY_BROAD topics and nervous start only after CP1 and are gated by
CP2 before their second runs; W5 keeps four sessions busy while CP2 runs;
Grade 10 evolution and ecology go last so they can compare against the
completed evidence and factor banks; application of cell knowledge closes
Grade 9.

## I. Checkpoint and review schedule

All reviews are independent (Opus 5.5 High, a reviewer who did not collect),
produce a correction specification, and are followed by deterministic
correction (Sonnet 5.5 Medium) and verification before approval, export and
import with the existing tools.

| Review | After | Reviews | Blocks |
|---|---|---|---|
| R1 | Wave 1 | pilot (done) | — |
| R2 | W2 | excretory, supramolecular, embryonic banks | nothing; runs during W3 |
| **CP1** | W3 | **digestive (broad checkpoint)**, chemical-composition run-2, respiratory r1, ecological factors r1 | **all of W4 onward** |
| **CP2** | W4 | science gate on r1 of cardiovascular, cell structure, heredity, nervous | their run-2 (W6); W5 proceeds |
| R5 | W5 | respiratory and ecological-factors banks; science gate on reproductive r1 and cell reproduction r1 | their run-2 (W8) |
| R6 | W6 | nervous bank | nothing |
| R7 | W7 | cardiovascular, cell structure, heredity banks; evidence r1 | nothing |
| R8 | W8 | reproductive and cell-reproduction banks; science gate on endocrine r1; musculoskeletal r1 | endocrine run-2 (W9) |
| R9 | W9 | endocrine, musculoskeletal, evidence banks; sensory r1 | nothing |
| R10 | W10 | sensory bank; population r1, reproduction-and-development r1; science gate on origin r1 | origin run-2 (W11) |
| R11 | W11 | population, origin, reproduction-and-development, application banks | nothing |
| Final | after R11 | one cross-file duplicate pass per grade, sorted by `research.concept` | release of the grade |

**Why digestive is the broad checkpoint.** It is the first broad two-run topic
the schedule can reach without waiting on anything, and it is the template for
the other Grade 8 organ-system topics (organs, process, diseases,
applied data). It also carries every pilot failure mode at once: health and
nutrition claims, an optional numeric lab (BMI), organ → function re-angling and
a nutrient overlap with chemical composition. Its two runs have distinct
missions (structure and process vs health and data), so CP1 can judge whether
differentiated runs really add coverage. Heredity or cell structure would be
more demanding, but they are three-run topics whose first run alone cannot
answer that question, and a failure there would be harder to separate from
subject difficulty.

**CP1 decides:** whether digestive landed inside 48–58 with ≥ 35 % reasoning
and no padding; whether run-2 added new labels rather than re-angled ones; the
real survival rate on a broad topic; whether chemical-composition run-2 met its
45 % floor. Outcomes: GO (W4 as planned); ADJUST (move BROAD and VERY_BROAD
ceilings in proportion to what digestive showed, never above a topic's
pre-pilot raw target, or tighten rules; recorded as an amendment to this file
before W4); STOP (escalate to the owner).

**Science gates** (CP2, R5, R8, R10) check the high-risk facts of a topic's
first run before its later runs copy the same errors: wrong keys, unsourced
health claims, wrong ratios or phase events, and scope leaks.

## J. Recalculated totals

| Grade | Topics | Completed final | Additional final expected | Expected grade total | Remaining raw ceiling | Remaining runs | Original raw | Original final |
|---|---|---|---|---|---|---|---|---|
| 8 | 12 | 69 | 412–490 | 481–559 | 538 | 20 | 692 | 519–612 |
| 9 | 5 | 39 | 182–221 | 221–260 | 243 | 8 | 319 | 236–276 |
| 10 | 7 | 36 | 282–338 | 318–374 | 370 | 13 | 452 | 338–394 |
| **Total** | **24** | **144** | **876–1049** | **1020–1193** | **1151** | **41** | **1463** | **1093–1282** |

- Completed final: **144** (4 topics). Expected additional final: **876–1,049**
  from 41 runs. Expected Biology total: **1,020–1,193**.
- Remaining raw ceiling **1,151** in **41 runs** over **10 waves**, against the
  original remaining 1,304 raw (1,463 − 159) in 43 runs over 11 waves.
- The expected total is about 7 % below the original 1,093–1,282, and the
  remaining raw ceiling about 12 % below the original remaining raw. Three
  causes: (1) three pilot topics are complete at 32–37, and chemical
  composition needs 30 raw instead of 39; (2) the one-fact rule (B5) removes the
  re-angled variants that made up 8–20 % of each pilot dataset, so the same
  curriculum coverage yields fewer distinct items: most remaining topics drop
  by 2–8 at each end of their range (evidence for evolution is unchanged, and
  cell structure and heredity keep their upper bound of 80); (3) ceilings follow
  the observed 91 % survival instead of an assumed 15–25 % loss. CP1 may raise
  any broad ceiling back up to its pre-pilot raw target if digestive shows more
  distinct material than expected.
- Physics (619 = 195 + 128 + 296) is context only. Biology comes out at about
  1.6–1.9 × Physics because its programmes list many more separately assessable
  organ systems, processes and comparisons (24 topics, ten of them broad or
  very broad), not because a total was aimed at. If CP1 shows broad topics
  saturating early, the total falls and that is acceptable.

## K. Stop conditions

**Run level (collector).** Stop with `topic_complete` and say which condition
applied when any of these is true:

1. Every dimension of the run's mission has at least one item and the reasoning
   floor is met.
2. Three consecutive drafted candidates are rejected under B5 (same label as an
   earlier item of this or an earlier run).
3. The next candidate would need a fact outside the programme or from another
   topic's ownership (appendix §4).
4. The next candidate's only new element is trivia: a name, number or detail
   listed as NO in section D.
5. A reasoning item can be written only by wrapping a recall fact in an
   artificial scenario.
6. Two consecutive drafts cannot get three plausible distractors that are real
   confusions.
7. A required source for the next claim is unreachable (B8).
8. `--compare` against a neighbour bank flags a duplicate, or the reviewer list
   of neighbour labels already holds the next fact.

**Topic level.** A topic stops early, and its later runs are cancelled, when a
review finds that a run added fewer than 10 new labels, or that more than one
fifth of its candidates were re-angled duplicates. Cancelled runs are recorded
here as an amendment; totals in J are not back-filled.

**Programme level.** Stop and escalate to the owner when CP1 or CP2 returns STOP;
when any review finds a wrong key or unsourced clinical claim in more than 5 %
of a dataset; or when a gate review finds the same systematic error in two
topics of one wave.

---

## Appendix — pre-pilot plan (2026-10-05, historical)

Kept unchanged for the record. Superseded: its §1 targets and totals, its §2 run
splits (the dimension lists D1… remain the reference catalogue), its §6 run and
wave schedule, its §7 model table and its §9 open points. Still in force: §3
„Distractors“ and the duplicate guards as tightened by B5 and B9, the §4 boundary
table, the §5 image policy, and §8 as background to section F.

**Original title:** Biology, Grades 8–10 — question-bank collection plan

Prepared 2026-10-05. A plan only: no question was collected, no candidate
dataset was created, nothing was imported or changed in the database.

Authority for scope: the three МОН programmes recorded in
`docs/reviews/biology_curriculum_structure_review.md` (amended texts of
15.11.2023) and the structure files under `data/subject_structures/biology/`.
Targets were set from each programme's „Учебно съдържание“ table — expected
results and new concepts — and its example laboratory topics, not from topic
titles. Physics banks were looked at for the pipeline only; no count was copied.

**Reading note.** In Grades 8 and 10 the programme lists expected results per
theme, not per sub-theme. Where a result names its organs or concepts, it is
assigned to the matching topic; results that apply to every sub-theme (for
example „изброява и описва по алгоритъм заболявания“) are applied to each. That
assignment is this plan's reading, not the programme's own layout.

### 1. Targets per topic

Scope class: narrow / normal / broad / exceptionally broad. "Raw" is what to
collect; "final" is the expected range after independent review. Both are
ranges of what the curriculum supports, not quotas: a collector who runs out of
distinct, in-scope material stops below the raw target and says so.
Difficulty intent is easy/medium/hard in percent. Diagram priority is for a
later image pass; every bank must stand on its own as text.

| Grade | Section | Topic | Slug | Scope class | Raw | Final | Difficulty E/M/H | Diagrams | Duplicate-risk notes |
|---|---|---|---|---|---|---|---|---|---|
| 8 | Структурна организация на човешкия организъм | Клетка и тъкани | `biology-g8-cell-and-tissues` | narrow | 40 | 30–35 | 40/50/10 | important | Four tissue types × (structure, function, location) invites the same fact three ways; cap at one item per tissue-property pair. Keep cell detail out (G9). |
| 8 | Обмяна на веществата | Храносмилателна система | `biology-g8-digestive-system` | broad | 68 | 50–60 | 35/50/15 | important | Organ → function pairs and “where is X digested” repeat easily; diet items drift into general health advice with no biology. |
| 8 | Обмяна на веществата | Дихателна система | `biology-g8-respiratory-system` | normal | 55 | 40–48 | 35/50/15 | useful | Airway order and “where does gas exchange happen” are the two facts most likely to be rewritten; smoking items repeat. |
| 8 | Обмяна на веществата | Отделителна система | `biology-g8-excretory-system` | normal | 48 | 35–42 | 35/50/15 | useful | Small organ set; urine-formation steps asked repeatedly. Do not go to nephron transport mechanisms beyond the school description. |
| 8 | Обмяна на веществата | Сърдечно-съдова система | `biology-g8-cardiovascular-system` | exceptionally broad | 88 | 65–75 | 30/50/20 | important | Highest in the grade: vessel types, circulation loops, blood cells and blood groups are four separate fact families, each easy to over-sample. Immunity/vaccines belong here only. |
| 8 | Движение и опора на тялото | Опорно-двигателна система – скелет и скелетни мускули | `biology-g8-musculoskeletal-system` | broad | 66 | 50–58 | 35/50/15 | important | Bone-naming items multiply without adding coverage; limit named bones to those the programme needs for regions and joints. |
| 8 | Размножаване, растеж и развитие | Полова система | `biology-g8-reproductive-system` | normal | 56 | 42–50 | 35/50/15 | important | Organ → function pairs for two systems; cycle-day arithmetic can repeat. STI items repeat the same prevention fact. |
| 8 | Размножаване, растеж и развитие | Зародишно и следзародишно развитие | `biology-g8-embryonic-and-postembryonic-development` | narrow | 37 | 32–38 | 40/50/10 | useful | Few stages, so stage-order items repeat. Gametogenesis and cleavage detail belong to G10. |
| 8 | Регулация и хомеостаза | Нервна система | `biology-g8-nervous-system` | broad | 74 | 55–65 | 30/50/20 | important | Reflex-arc element order and brain-part → function are over-sampled easily; psychoactive-substance items repeat the same warning. |
| 8 | Регулация и хомеостаза | Ендокринна система | `biology-g8-endocrine-system` | normal | 56 | 42–50 | 35/50/15 | useful | Gland → hormone → effect triples are the classic rewrite trap; one item per triple, then regulation and disorders. |
| 8 | Регулация и хомеостаза | Сетивни системи | `biology-g8-sensory-systems` | broad | 64 | 48–56 | 35/50/15 | important | Eye-part and ear-part → function lists; keep to one item per structure and spend the rest on the path of the signal and on disorders. |
| 8 | Регулация и хомеостаза | Кожа | `biology-g8-skin` | narrow | 40 | 30–35 | 40/50/10 | useful | Three layers and four functions; thermoregulation is the only process. First-aid items overlap with the musculoskeletal topic (trauma) — burns, frostbite, heat and sunstroke stay here. |
| 9 | Химичен състав на живата материя | Химичен състав на живата материя | `biology-g9-chemical-composition-of-living-matter` | broad | 78 | 58–68 | 30/50/20 | useful | Definition items for five compound groups plus “monomer of X” are easy to duplicate; comparison items must compare different pairs. |
| 9 | Надмолекулни комплекси | Надмолекулни комплекси | `biology-g9-supramolecular-complexes` | narrow | 48 | 35–42 | 35/50/15 | useful | Small concept set (definition, virus structure, reproduction stages); disease items repeat the same prevention fact across viruses. |
| 9 | Структура и процеси в клетката | Структура и процеси в клетката | `biology-g9-cell-structure-and-processes` | exceptionally broad | 95 | 70–80 | 30/50/20 | important | Largest bank. Organelle → function is the main trap; membrane transport, metabolism and the genetic processes are three separate process families and each needs its own budget. Bacteria diseases are here, viral ones are not. |
| 9 | Възпроизводство на клетката | Възпроизводство на клетката | `biology-g9-cell-reproduction` | normal | 60 | 45–52 | 30/50/20 | important | Phase-order items for mitosis and meiosis repeat; chromosome-count arithmetic repeats with changed numbers (the validator flags those as near duplicates). |
| 9 | Приложение на знанията за клетката | Приложение на знанията за клетката | `biology-g9-application-of-cell-knowledge` | narrow | 38 | 28–34 | 45/45/10 | not needed | RECALL-DOMINATED: the programme asks only to describe and illustrate applications. Field → example pairs will be rewritten unless every item is tied to a different cell process from themes 1–4. |
| 10 | Многоклетъчен организъм | Наследственост и изменчивост | `biology-g10-heredity-and-variation` | exceptionally broad | 96 | 70–80 | 25/50/25 | important | Cross problems with changed traits are the same exercise; budget them per cross type and per interaction type. Definitions (gene, allele, genotype, phenotype) must not be asked more than once each. |
| 10 | Многоклетъчен организъм | Размножаване, растеж и индивидуално развитие | `biology-g10-reproduction-growth-and-individual-development` | broad | 66 | 50–58 | 30/50/20 | important | Four explicit comparison pairs; each pair supports several distinct criteria but not ten. Human organs and the menstrual cycle are G8. |
| 10 | Биосфера | Популация, биоценоза, екосистема | `biology-g10-population-biocenosis-ecosystem` | broad | 74 | 55–65 | 30/50/20 | important | Definition items for five levels; food-chain and pyramid items repeat with swapped organisms. Factors and adaptations belong to the next topic. |
| 10 | Биосфера | Екологични фактори | `biology-g10-ecological-factors` | normal | 56 | 42–50 | 30/50/20 | useful | Factor → example classification is the trap; adaptation items need different organisms and different factors. |
| 10 | Биологична еволюция | Произход и развитие на живата материя | `biology-g10-origin-and-development-of-living-matter` | broad | 72 | 55–62 | 30/50/20 | useful | Darwinian vs modern factors, and micro- vs macroevolution, are the two comparisons that get rewritten. Evidence items belong to 3.3, human evolution to 3.2. |
| 10 | Биологична еволюция | Произход и еволюция на човека | `biology-g10-origin-and-evolution-of-humans` | narrow | 40 | 30–35 | 40/50/10 | useful | Stage-order items repeat. Sensitive content: follow the amended programme exactly (the term „раса“ as a new concept and the discussion of racial belonging were removed in 2023). |
| 10 | Биологична еволюция | Доказателства за биологичната еволюция | `biology-g10-evidence-for-biological-evolution` | normal | 48 | 36–44 | 35/50/15 | important | Homologous/analogous/rudimentary classification with a different organ each time is one item type; cap it and cover fossils, series and embryology. |

#### Totals

| Grade | Topics | Raw target | Expected final range | Collection runs |
|---|---|---|---|---|
| 8 | 12 | 692 | 519–612 | 23 |
| 9 | 5 | 319 | 236–276 | 10 |
| 10 | 7 | 452 | 338–394 | 14 |
| **Total** | **24** | **1463** | **1093–1282** | **47** |

Raw targets assume that roughly 15–25 % of candidates are deleted or merged in
review. Biology allows many surface variants of one fact, so the loss is
expected to be higher than the 5–9 % seen in the Physics banks.

#### Why each topic has its target

- **8 · Клетка и тъкани** (narrow, raw 40): One sub-theme under one theme: levels of organisation in the human body and the kinds of tissue, recognised on images.
- **8 · Храносмилателна система** (broad, raw 68): Organs and functions, the digestion process, diseases by the cause–signs–prevention algorithm, food composition and eating regime, alcohol/energy drinks/diets/overeating, and two lab topics (menu, nutrient tests, BMI and energy use).
- **8 · Дихателна система** (normal, raw 55): Organs and functions, breathing and gas exchange, diseases, the significance of air composition, and a lab on breathing rate and vital capacity.
- **8 · Отделителна система** (normal, raw 48): Organs and functions, excretion as a process, diseases, and reading urine composition in norm and pathology.
- **8 · Сърдечно-съдова система** (exceptionally broad, raw 88): The sub-theme carries the heart, vessels and circulation, the formed elements of blood, blood groups, immunity, vaccines and immunisation, infection through blood, and blood-composition data, plus a pulse and blood-pressure lab.
- **8 · Опорно-двигателна система – скелет и скелетни мускули** (broad, raw 66): Skeleton and skeletal muscles, three kinds of bone connection, joints, comparison with mammals, injuries and diseases, risk factors, first aid for trauma, and tolerance toward people with disabilities.
- **8 · Полова система** (normal, raw 56): Male and female organs and functions, ovulation and the menstrual cycle, sexual and reproductive health, sexually transmitted diseases, risks of early sexual contact.
- **8 · Зародишно и следзародишно развитие** (narrow, raw 37): Main stages of embryonic and post-embryonic human development, named and recognised on an image; responsibility for offspring.
- **8 · Нервна система** (broad, raw 74): Structure and functions of the nervous system, reflex and reflex arc, unconditioned vs conditioned reflexes, nervous regulation and its link with hormonal regulation, diseases and injuries, alcohol and narcotic substances, mental-health prophylaxis.
- **8 · Ендокринна система** (normal, raw 56): Glands and hormones, hormonal regulation, examples of nervous–hormonal links, homeostasis, disorders, hormonal preparations.
- **8 · Сетивни системи** (broad, raw 64): Sense organs as parts of sensory systems: eye and vision, ear, hearing and balance, other senses; disorders and prevention; a lab on the visual system.
- **8 · Кожа** (narrow, raw 40): Structure and functions of the skin, thermoregulation, diseases and injuries, first aid for burns, frostbite, heat and sunstroke.
- **9 · Химичен състав на живата материя** (broad, raw 78): Groups of chemical elements, inorganic and organic compounds by the composition–properties–functions algorithm, carbohydrates, lipids, proteins, enzymes, nucleic acids, grouping by monomers, comparisons, living vs non-living matter, reading charts.
- **9 · Надмолекулни комплекси** (narrow, raw 48): Definition of supramolecular complexes, viruses, stages of virus reproduction, viral diseases by infection route – signs – prevention, prophylaxis.
- **9 · Структура и процеси в клетката** (exceptionally broad, raw 95): About a third of the programme: cell theory, prokaryotic and eukaryotic cells, membrane, nucleus, cytoplasm, organelles, bacterial diseases, membrane transport and cytoses, metabolism (anabolic and catabolic processes), genetic processes (replication, transcription, translation), linking processes to structures, and five of the nine lab topics.
- **9 · Възпроизводство на клетката** (normal, raw 60): Mitosis, meiosis, mitotic cycle and life cycle of the cell, kinds of division and their stages, following them on a diagram, significance of division and differentiation. Amitosis was removed in 2023.
- **9 · Приложение на знанията за клетката** (narrow, raw 38): Applications of cell knowledge in science, biotechnology, medicine, agriculture and environmental protection; project work.
- **10 · Наследственост и изменчивост** (exceptionally broad, raw 96): Eight definitions, Mendel's laws, mono- and dihybrid crosses, four allelic and four non-allelic interactions, kinds of variation, mutations and modifications, methods of human genetics, hereditary diseases, pedigrees, chromosomal sex determination, reading genetic schemes and karyograms.
- **10 · Размножаване, растеж и индивидуално развитие** (broad, raw 66): Phases of gametogenesis, stages of embryonic development in animals and humans, direct and indirect development, four comparisons, significance of gametogenesis, fertilisation and differentiation, factors of human development.
- **10 · Популация, биоценоза, екосистема** (broad, raw 74): Hierarchy of supra-organism levels, five definitions with examples, structure and composition of population, biocenosis, ecosystem and biosphere, development and productivity of ecosystems, cycles of matter, energy flow, ecological pyramid, and four field/lab topics.
- **10 · Екологични фактори** (normal, raw 56): Abiotic, biotic and anthropogenic factors with examples, adaptations and their relative nature, assessing the state of a population or ecosystem, predicting change under human impact, and two lab topics.
- **10 · Произход и развитие на живата материя** (broad, raw 72): Hypotheses for the origin of life, Darwin's theory and contribution, the modern theory, factors of evolution in both, criteria for species, speciation, micro- vs macroevolution, directions of the evolutionary process, paths of biological progress.
- **10 · Произход и еволюция на човека** (narrow, raw 40): Anthropogenesis, main stages of human evolution recognised in text or image, causes for the appearance of human races, one species.
- **10 · Доказателства за биологичната еволюция** (normal, raw 48): Four groups of evidence with examples, and seven defined terms: analogous, homologous and rudimentary organs, phylogenetic series, fossils, index fossils, transitional forms.

### 2. Coverage matrix

Each topic lists its assessable dimensions, taken from the programme. A topic
collected in several runs splits its dimensions between the runs; a run writes
questions only for its own dimensions.

#### Grade 8

**Клетка и тъкани** — `biology-g8-cell-and-tissues`, raw 40 in 1 run
- run-1 (max 40): D1 Levels of organisation: cell → tissue → organ → system → organism, and their order; D2 Kinds of tissue on a structural and functional basis; D3 Tissue ↔ function ↔ where it is found; D4 Recognising a tissue from a description (image-dependent items deferred); D5 Assigning an organ to a system by function or structure

**Храносмилателна система** — `biology-g8-digestive-system`, raw 68 in 2 runs
- run-1 (max 34): D1 Organs of the tract and glands: order and structure; D2 Function of each organ; mechanical vs chemical processing; D3 Digestion and absorption as a sequence; D4 Nutrients and food composition; healthy regime
- run-2 (max 34): D5 Diseases: cause – signs – prevention; D6 Alcohol, energy drinks, diets, overeating: effect on the organism; D7 Applied data: menu, BMI, basal metabolism, nutrient tests

**Дихателна система** — `biology-g8-respiratory-system`, raw 55 in 2 runs
- run-1 (max 28): D1 Airways and lungs: order and structure; D2 Breathing movements and gas exchange; D3 Structure–function links (alveoli, cilia, etc.)
- run-2 (max 27): D4 Air composition and its effect; D5 Diseases: cause – signs – prevention; D6 Breathing rate and vital capacity: reading measurements

**Отделителна система** — `biology-g8-excretory-system`, raw 48 in 2 runs
- run-1 (max 24): D1 Organs of the system and their order; D2 Kidney structure at school level and its function; D3 Formation and removal of urine as a sequence
- run-2 (max 24): D4 Urine composition: norm vs pathology (table data); D5 Diseases: cause – signs – prevention; D6 Link with the blood and water balance

**Сърдечно-съдова система** — `biology-g8-cardiovascular-system`, raw 88 in 3 runs
- run-1 (max 30): D1 Heart: structure and work; D2 Vessels: kinds, structure–function; D3 Circulation: the two loops as a sequence
- run-2 (max 29): D4 Blood: plasma and formed elements, functions; D5 Blood groups and transfusion compatibility; D6 Immunity: kinds, vaccines, vaccination, immunisation
- run-3 (max 29): D7 Diseases and blood-borne infection: cause – signs – prevention, personal responsibility; D8 Data: blood composition in norm and pathology; pulse and arterial pressure at rest and under load

**Опорно-двигателна система – скелет и скелетни мускули** — `biology-g8-musculoskeletal-system`, raw 66 in 2 runs
- run-1 (max 33): D1 Parts of the skeleton and their functions; D2 Bone connections: fixed, semi-movable, movable; joint structure; D3 Skeletal muscles: structure, work, fatigue; D4 Bone and muscle as a lever system: structure–function
- run-2 (max 33): D5 Comparison with the mammal skeleton; D6 Injuries and diseases: cause – signs – prevention; posture; D7 First aid for trauma

**Полова система** — `biology-g8-reproductive-system`, raw 56 in 2 runs
- run-1 (max 28): D1 Male reproductive organs: structure and function; D2 Female reproductive organs: structure and function; D3 Gametes; ovulation and the menstrual cycle as a sequence
- run-2 (max 28): D4 Sexual and reproductive health; D5 Sexually transmitted diseases: cause – signs – prevention; D6 Responsible behaviour: applying the rules to a situation

**Зародишно и следзародишно развитие** — `biology-g8-embryonic-and-postembryonic-development`, raw 37 in 2 runs (run-1 + a targeted run-2 supplement)
- run-1 (max 37; stopped early at 27 collected, 22 surviving after R2 and curriculum correction): D1 Fertilisation and the start of development; D2 Stages of embryonic development in order; D3 Placenta and umbilical cord: role; D4 Birth (as covered)
- run-2 (max 10, a ceiling and not a quota; early stop applies; 27 + 10 = the 37 raw allocation; R2b required): D5 Post-embryonic development, D6 Factors that put development at risk, within the limits of section D

**Нервна система** — `biology-g8-nervous-system`, raw 74 in 2 runs
- run-1 (max 37): D1 Neuron and nervous tissue in the system; D2 Central nervous system: parts and functions; D3 Peripheral nervous system; D4 Reflex and reflex arc as a sequence
- run-2 (max 37): D5 Unconditioned vs conditioned reflexes: comparison; D6 Nervous regulation and homeostasis; link with hormonal regulation; D7 Diseases and injuries: cause – signs – prevention; D8 Alcohol, narcotic and psychoactive substances: effect on person and society

**Ендокринна система** — `biology-g8-endocrine-system`, raw 56 in 2 runs
- run-1 (max 28): D1 Endocrine glands and where they are; D2 Hormones and their effects; D3 Hormonal regulation as feedback; D4 Nervous vs hormonal regulation: comparison and links
- run-2 (max 28): D5 Homeostasis: the organism as a whole; D6 Disorders of too much or too little hormone; D7 Hormonal preparations: discussion-level facts

**Сетивни системи** — `biology-g8-sensory-systems`, raw 64 in 2 runs
- run-1 (max 32): D1 Sensory system: receptor – pathway – brain centre; D2 Eye: structure and function of parts; D3 Vision: path of light and of the signal; common defects; D4 Ear: structure; hearing and balance
- run-2 (max 32): D5 Taste, smell, skin senses; D6 Disorders and injuries: cause – signs – prevention; D7 Observations on the visual system

**Кожа** — `biology-g8-skin`, raw 40 in 1 run
- run-1 (max 40): D1 Layers of the skin and what they contain; D2 Functions of the skin; D3 Thermoregulation as a mechanism; D4 Diseases and injuries; hygiene; D5 First aid: burns, frostbite, heat and sunstroke

#### Grade 9

**Химичен състав на живата материя** — `biology-g9-chemical-composition-of-living-matter`, raw 78 in 2 runs
- run-1 (max 39): D1 Chemical elements by share; examples of significance; D2 Water and mineral salts; D3 Carbohydrates: composition – properties – functions; D4 Lipids: composition – properties – functions; D5 Proteins: structure levels at school depth, functions
- run-2 (max 39): D6 Enzymes: nature, specificity, conditions; D7 Nucleic acids: DNA and RNA, comparison; D8 Grouping by monomers; comparing biopolymer groups; D9 Living vs non-living matter; reading composition charts

**Надмолекулни комплекси** — `biology-g9-supramolecular-complexes`, raw 48 in 2 runs
- run-1 (max 24): D1 Supramolecular complexes: definition and examples; D2 Viruses: composition and structure; why they are not cells; D3 Reproduction of a virus as a sequence of stages
- run-2 (max 24): D4 Viral diseases: route of infection – signs – prevention; D5 Prophylaxis: vaccines and behaviour, applied to a situation

**Структура и процеси в клетката** — `biology-g9-cell-structure-and-processes`, raw 95 in 3 runs
- run-1 (max 32): D1 Cell theory: main statements; D2 Prokaryotic vs eukaryotic cell: comparison; D3 Cell membrane: structure and properties; D4 Nucleus and cytoplasm
- run-2 (max 32): D5 Organelles: structure ↔ function; plant vs animal cell; D6 Bacteria and bacterial diseases: route – signs – prevention; D7 Membrane transport: passive vs active; osmosis; D8 Endocytosis and exocytosis
- run-3 (max 31): D9 Metabolism: anabolic vs catabolic; photosynthesis and respiration at programme depth; D10 Replication, transcription, translation: what, where, in what order; D11 Process ↔ structure that provides it; D12 Observations: microscope preparations, osmotic behaviour, pigments

**Възпроизводство на клетката** — `biology-g9-cell-reproduction`, raw 60 in 2 runs
- run-1 (max 30): D1 Life cycle and mitotic cycle of the cell; interphase; D2 Mitosis: phases and events in order; D3 Meiosis: two divisions, what changes
- run-2 (max 30): D4 Mitosis vs meiosis: comparison; D5 Chromosome number before and after division; D6 Significance of division; cell differentiation

**Приложение на знанията за клетката** — `biology-g9-application-of-cell-knowledge`, raw 38 in 1 run — **recall-dominated, see section 3**
- run-1 (max 38): D1 Biotechnological production: which cell process is used; D2 Medicine: diagnostics, antibiotics, vaccines as applications; D3 Agriculture; D4 Environmental protection; D5 Matching an application to the knowledge it rests on

#### Grade 10

**Наследственост и изменчивост** — `biology-g10-heredity-and-variation`, raw 96 in 3 runs
- run-1 (max 32): D1 Core terms: heredity, variation, gene, allele, genotype, phenotype; D2 Mendel's laws; monohybrid cross; D3 Dihybrid cross; D4 Allelic interactions: complete and incomplete dominance, codominance, lethal
- run-2 (max 32): D5 Non-allelic interactions: complementary, epistatic, polymeric; pleiotropy; D6 Kinds of variation; modifications vs mutations; D7 Methods of human genetics; hereditary diseases; D8 Pedigree and sex determination in humans
- run-3 (max 32): D9 Reading genetic schemes, karyograms, tables; D10 Factors causing modifications and mutations

**Размножаване, растеж и индивидуално развитие** — `biology-g10-reproduction-growth-and-individual-development`, raw 66 in 2 runs
- run-1 (max 33): D1 Asexual vs sexual reproduction; D2 Gametogenesis: phases; spermatogenesis vs oogenesis; D3 Fertilisation: external vs internal; D4 Embryonic development: stages in order
- run-2 (max 33): D5 Post-embryonic development: direct vs indirect; D6 Growth and cell differentiation; D7 External and internal factors of human development

**Популация, биоценоза, екосистема** — `biology-g10-population-biocenosis-ecosystem`, raw 74 in 2 runs
- run-1 (max 37): D1 Supra-organism levels in order; D2 Population: structure and characteristics; spatial distribution; D3 Biocenosis: composition and relations; ecological niche; D4 Ecosystem: components, food chains and webs
- run-2 (max 37): D5 Energy flow and ecological pyramids; D6 Cycles of matter; D7 Development (succession) and productivity; D8 Biosphere

**Екологични фактори** — `biology-g10-ecological-factors`, raw 56 in 2 runs
- run-1 (max 28): D1 Classifying factors: abiotic, biotic, anthropogenic; D2 Abiotic factors and organism responses; limits of tolerance at programme depth; D3 Biotic relations as factors
- run-2 (max 28): D4 Adaptations ↔ factors; relative adaptation; D5 Anthropogenic impact: assessing a described state; D6 Predicting change from given data

**Произход и развитие на живата материя** — `biology-g10-origin-and-development-of-living-matter`, raw 72 in 2 runs
- run-1 (max 36): D1 Hypotheses for the origin of life; D2 Darwin: struggle for existence, natural and artificial selection; D3 Modern theory: population as the unit; factors of evolution; D4 Darwinian vs modern factors: comparison
- run-2 (max 36): D5 Biological species and its criteria; D6 Speciation; microevolution; D7 Macroevolution; directions and paths of biological progress; D8 Applying a factor to a described case

**Произход и еволюция на човека** — `biology-g10-origin-and-evolution-of-humans`, raw 40 in 1 run
- run-1 (max 40): D1 Anthropogenesis: definition and driving factors; D2 Stages of human evolution in order and their features; D3 Human vs other primates: what changed; D4 Causes for the appearance of human populations/races; D5 All humans as one species: the argument

**Доказателства за биологичната еволюция** — `biology-g10-evidence-for-biological-evolution`, raw 48 in 2 runs
- run-1 (max 24): D1 Groups of evidence and what each shows; D2 Homologous vs analogous organs; D3 Rudimentary organs; D4 Comparative embryology and physiology
- run-2 (max 24): D5 Fossils, index fossils, transitional forms; D6 Phylogenetic series; D7 Choosing evidence that supports a stated claim

### 3. Coverage principles

**Mixture, not vocabulary.** Across an ordinary topic the bank should hold all
of these where the programme supports them:

| Kind | What it tests | Typical share |
|---|---|---|
| A | Terms and recognition | at most a quarter |
| B | Understanding and explanation | a fifth to a quarter |
| C | Comparison and classification | about a fifth |
| D | Process, sequence and mechanism | about a fifth |
| E | Application to a described situation (health, first aid, an organism, a case) | a tenth to a fifth |
| F | Reading a table, graph or described experiment | up to a tenth, where the programme has data or lab work |

No share is forced where a topic lacks the material. The programmes ask for
„сравнява“, „проследява“, „обосновава“, „аргументира“, „анализира данни“ far
more often than „дефинира“, so a recall-heavy bank misrepresents them.

**Flagged as recall-dominated:** Grade 9 „Приложение на знанията за клетката“.
The programme only asks to describe and illustrate applications. Its target is
the lowest, and every item must tie an application to a cell process from the
other four themes. Also watch Grade 8 „Клетка и тъкани“ and „Кожа“, and Grade
10 „Произход и еволюция на човека“: small concept sets where kind A can crowd
out the rest.

**Difficulty** uses the existing model.

- easy: recognise or recall one fact, or apply it in one obvious step;
- medium: explain, compare on a named criterion, order a process, classify an
  unfamiliar example, read one value from data;
- hard: combine two ideas from the topic, predict the result of a change,
  interpret data and draw a conclusion, solve a cross with a non-trivial ratio.

Baseline 30–40 / 45–55 / 10–20. Topics with real multi-step reasoning carry
more hard items (heredity 25 %, cardiovascular, nervous, cell processes, cell
division, ecology and evolution 20 %); small descriptive topics carry 10 %.
Hard never means obscure: no named enzyme lists, no rare syndromes, no figures
to memorise, nothing from profiled XI–XII or university courses.

**Distractors.** Each wrong option should be a real confusion: a neighbouring
structure, a reversed cause and effect, a swapped process step, the other
member of a compared pair, a wrong classification, a similar term (митоза /
мейоза, транскрипция / транслация, хомоложни / аналогни, артерия / вена). Four
options of similar length and grammar. No „всички изброени“, no „нито едно от
изброените“, no joke options, no negative stems unless the negation is the
point and is capitalised.

**Duplicate guards.** The validator finds only exact and near-identical
wording, inside one file and against stored questions; it does not see two
files of the same topic and does not see a reworded fact.

1. One item per fact. A second item on the same structure, step or definition
   is allowed only if it tests a different kind (A–F) of knowledge.
2. Every candidate records `research.concept` as a short, specific fact label
   (for example „алвеоли – газова обмяна“), so reviewers can sort by concept.
3. A later run of a topic reads the earlier run's file first and lists the
   concepts already covered in its notes; it never edits that file.
4. Comparison items must use different pairs or different criteria.
5. Calculation and cross items differ in structure, not only in numbers or
   trait names.
6. The reviewer compares all files of a grade together (section 6).

### 4. Cross-grade and cross-topic boundaries

| Area | Where it belongs | What must not leak |
|---|---|---|
| Cell | G8 „Клетка и тъкани“: the cell only as the unit of tissues. G9: composition, structure, processes, division. | No organelles, membrane transport or division in G8. |
| Reproduction and development | G8: human organs, cycle, health, human stages. G10: gametogenesis phases, comparisons across organisms, direct and indirect development. | No gametogenesis or cleavage detail in G8; no human anatomy or STI items in G10. |
| Meiosis | G9 „Възпроизводство на клетката“. G10 uses it as known. | G10 items do not re-test phases of meiosis; G9 items do not test inheritance. |
| DNA and genetic processes | G9 „Структура и процеси в клетката“ (replication, transcription, translation). G10 heredity works with genes and alleles. | No genetic code tables, operons or mutation mechanisms at molecular level. |
| Viruses, bacteria, immunity | Viruses: G9 theme 2. Bacteria: G9 theme 3. Immunity and vaccines: G8 cardiovascular. | Vaccine items in G9 stay at prophylaxis level; G8 does not test virus reproduction. |
| Diseases | Each system's diseases stay with that system. First aid: trauma with the musculoskeletal topic, burns/frostbite/heat with skin, bleeding with cardiovascular. | No clinical detail beyond cause – signs – prevention. |
| Regulation | Nervous vs hormonal comparison is tested once, in the endocrine topic; the nervous topic covers the link from its side only. | |
| Ecology vs evolution | Adaptation as a response to factors: G10 ecological factors. Adaptation as a result of selection: G10 3.1. | |
| Evolution topics | Mechanisms 3.1, humans 3.2, evidence 3.3. | Evidence items are not placed in 3.1; human fossils are 3.2. |

Out of scope everywhere: profiled XI–XII modules, plant and animal systematics
from earlier grades, amitosis, „висша нервна дейност“, psychosexual development
stages and the removed race-related results (all taken out in 2023).

### 5. Images and diagrams

Priority per topic is in the main table. **Important** means the programme
itself says „означава върху схема, изображение, модел“ or „разчита“ for the
core of the topic (tissues, organ systems, the cell, division phases, genetic
schemes and karyograms, pyramids and food webs, homologous organs). **Useful**
means diagrams would add items but text covers the topic. Nothing is collected
now: a question that needs a picture is not written, and a described table or
described experiment is used instead where that works.

### 6. Pipeline limits and collection runs

| Limit | Ceiling | Plan |
|---|---|---|
| Subject and grade per run | 1 and 1 | 1 and 1 |
| `max_topics` | 10 | 1 |
| `max_candidates` | 250 | at most 40 |
| `max_candidates_per_source` | 50 | equal to the run's `max_candidates` (at most 40) |
| `max_pages` | 100 | 8 |
| `max_runtime_minutes` | 60 | 60 |

The binding limits are runtime and the per-source cap. Physics runs produced
about 23 candidates in 12–30 minutes, so 40 is the most one 60-minute run
should attempt; most Biology candidates will cite one official source, so a run
could not exceed 50 in any case. Topics above 40 are therefore collected in two
or three runs. No limit is changed.

- Dataset: `data/question_candidates/biology/grade-<n>/<topic slug>-run-<k>.json`
  (ignored by Git), run id `<topic slug>-run-<k>`, started with
  `src.start_question_collection` against the grade's structure file.
- One run = one topic = one file = one session. No two sessions share a file.
- Run k of a topic starts only after run k−1 is finished.

**47 runs in 12 waves of up to 4 simultaneous sessions.** Waves are balanced by candidates, not topics.

| Wave | Runs (topic slug without `biology-`, run, max candidates) | Candidates |
|---|---|---|
| 1 | `g8-cell-and-tissues` run-1 (40); `g8-skin` run-1 (40); `g10-origin-and-evolution-of-humans` run-1 (40); `g9-chemical-composition-of-living-matter` run-1 (39) | 159 |
| 2 | `g9-application-of-cell-knowledge` run-1 (38); `g8-nervous-system` run-1 (37); `g10-population-biocenosis-ecosystem` run-1 (37); `g10-origin-and-development-of-living-matter` run-1 (36) | 148 |
| 3 | `g8-digestive-system` run-1 (34); `g8-musculoskeletal-system` run-1 (33); `g10-reproduction-growth-and-individual-development` run-1 (33); `g8-sensory-systems` run-1 (32) | 132 |
| 4 | `g9-cell-structure-and-processes` run-1 (32); `g10-heredity-and-variation` run-1 (32); `g8-cardiovascular-system` run-1 (30); `g9-cell-reproduction` run-1 (30) | 124 |
| 5 | `g8-respiratory-system` run-1 (28); `g8-reproductive-system` run-1 (28); `g8-endocrine-system` run-1 (28); `g10-ecological-factors` run-1 (28) | 112 |
| 6 | `g8-excretory-system` run-1 (24); `g9-supramolecular-complexes` run-1 (24); `g10-evidence-for-biological-evolution` run-1 (24); `g8-embryonic-and-postembryonic-development` run-1 (37, stopped at 27) | 109 |
| 7 | `g9-chemical-composition-of-living-matter` run-2 (39); `g8-nervous-system` run-2 (37); `g10-population-biocenosis-ecosystem` run-2 (37); `g10-origin-and-development-of-living-matter` run-2 (36) | 149 |
| 8 | `g8-digestive-system` run-2 (34); `g8-musculoskeletal-system` run-2 (33); `g10-reproduction-growth-and-individual-development` run-2 (33); `g8-sensory-systems` run-2 (32) | 132 |
| 9 | `g9-cell-structure-and-processes` run-2 (32); `g10-heredity-and-variation` run-2 (32); `g9-cell-reproduction` run-2 (30); `g8-cardiovascular-system` run-2 (29) | 123 |
| 10 | `g8-reproductive-system` run-2 (28); `g8-endocrine-system` run-2 (28); `g10-ecological-factors` run-2 (28); `g8-respiratory-system` run-2 (27) | 111 |
| 11 | `g8-excretory-system` run-2 (24); `g9-supramolecular-complexes` run-2 (24); `g10-evidence-for-biological-evolution` run-2 (24); `g8-embryonic-and-postembryonic-development` run-2 (10, supplement) | 82 |
| 12 | `g10-heredity-and-variation` run-3 (32); `g9-cell-structure-and-processes` run-3 (31); `g8-cardiovascular-system` run-3 (29) | 92 |

Four simultaneous sessions is the recommendation: each is independent, and four
keeps usage and the review backlog manageable. Six is possible in waves 1–6
(all first runs); more than that adds nothing because later runs wait on
earlier ones. **Run wave 1 as a pilot** and review one of its files before
starting wave 2, so that wording, depth and the concept labels are corrected
once instead of in 47 files.

Collectors do not approve, export or import; do not touch tracked application
files, the structure files, the database or the vault; do not stage or commit.

#### After collection

1. Validate every file (`src.validate_question_candidates`, with `--db`, never
   `--annotate` onto the original).
2. Independent review per grade, in section-sized batches, plus one
   cross-file duplicate pass per grade sorted by `research.concept`:
   Grade 8 in five batches (one per section), Grade 9 in three (theme 3 alone),
   Grade 10 in three. Output: one correction specification per grade.
3. Deterministic correction from the specification.
4. Post-correction verification, then approval by a separate reviewer identity,
   export and import with the existing tools.

### 7. Model and effort

| Phase | Model and effort | Why |
|---|---|---|
| Planning (this document) | Opus 5.5 High | Scope judgement. |
| Collection, most topics | Sonnet 5.5 Medium | Bounded writing against a stated dimension list. |
| Collection, six topics | Sonnet 5.5 High | Accuracy-critical or reasoning-heavy: G9 „Структура и процеси в клетката“, „Възпроизводство на клетката“, „Химичен състав на живата материя“; G10 „Наследственост и изменчивост“, „Произход и развитие на живата материя“; G8 „Сърдечно-съдова система“. |
| Independent review | Opus 5.5 High | Scientific errors, scope and semantic duplicates are the expensive failures. |
| Deterministic correction | Sonnet 5.5 Medium | Applies an exact specification. |
| Post-correction verification | Sonnet 5.5 Medium or High | Mechanical comparison against the specification. |
| Validation, export, import | No reasoning model unless a step fails | Existing scripts. |

No collection on Opus. If the pilot shows Sonnet Medium writing mostly recall
items, raise those topics to High before buying more review.

### 8. Scientific verification priorities

Verification is for facts, not for scope: an authoritative source that goes
deeper does not widen the bank.

| Priority | Topics | Typical risks | Suitable sources |
|---|---|---|---|
| Highest | G10 heredity and variation | Ratios for interaction types, definitions of codominance vs incomplete dominance, epistasis and polymery examples, sex-linked inheritance, pedigree logic | OpenStax *Biology 2e* and *Concepts of Biology*; NCBI Bookshelf; NIH MedlinePlus Genetics and NHGRI for hereditary diseases |
| Highest | G9 cell structure and processes; cell reproduction | Organelle functions, passive vs active transport, where each genetic process happens, phase events, chromosome numbers through meiosis | OpenStax *Biology 2e*; NCBI Bookshelf (*Molecular Biology of the Cell* for checking only); Nature Scitable |
| High | G9 chemical composition; supramolecular complexes | Monomers and bonds named correctly, enzyme properties, virus life cycle, transmission routes | OpenStax; NCBI Bookshelf; WHO fact sheets for viral diseases |
| High | G8 cardiovascular (blood groups, immunity, vaccines), nervous, endocrine, sensory, reproductive | Transfusion compatibility, hormone–gland pairs, cycle timing, STI facts, substance effects | OpenStax *Anatomy and Physiology 2e*; WHO; NIH/MedlinePlus; Bulgarian Ministry of Health material for the national immunisation calendar if it is mentioned at all |
| High | G10 evolution topics | Darwinian vs modern factors, species criteria, hominin stages and dates, homologous vs analogous examples | Understanding Evolution (UC Berkeley); Smithsonian Human Origins; OpenStax |
| Medium | G10 ecology topics | Pyramid types, energy transfer, succession, niche vs habitat | OpenStax; Nature Scitable; EEA or IPBES only for anthropogenic examples |
| Medium | Remaining G8 systems; G8 first aid | First-aid steps change over time | OpenStax *Anatomy and Physiology 2e*; Red Cross / Bulgarian Red Cross first-aid guidance |

Bulgarian terminology follows the programme's wording; where a textbook term
differs from the programme, the programme wins. Third-party pages are research
only: questions are newly written (`original_from_research`), never copied.

### 9. Open points

1. Raw targets are estimates. The pilot wave should be used to recalibrate
   them, especially for the two largest banks.
2. The split of dimensions between runs is contiguous and approximate; a
   collector may report that a dimension is exhausted and stop early.
3. The Grade 9 structure has one topic per section, so its five banks are also
   section banks; „Структура и процеси в клетката“ will be a 70–80 question
   section and may deserve a later decision about how tests draw from it.
4. Image-based questions are deferred; topics marked "important" will be
   under-covered on recognition until an image pass exists.
