# Biology Wave 1 pilot — independent review

Reviewed 2026-10-06. Review only: no candidate file, plan, structure file or
database was changed, nothing was approved, exported or imported.

All 159 candidates were read in full (prompt, four options, key, explanation).
Collector self-reports were treated as leads only.

| # | Dataset (`data/question_candidates/biology/…`) | Run id (candidate id = run id + `-NNN`) | Candidates |
|---|---|---|---|
| P1 | `grade-8/biology-g8-cell-and-tissues-run-1.json` | `biology-g8-cell-and-tissues-run-1` | 40 |
| P2 | `grade-8/biology-g8-skin-run-1.json` | `biology-g8-skin-run-1` | 40 |
| P3 | `grade-9/biology-g9-chemical-composition-of-living-matter-run-1.json` | `biology-g9-chemical-composition-of-living-matter-run-1` | 39 |
| P4 | `grade-10/biology-g10-origin-and-evolution-of-humans-run-1.json` | `biology-g10-origin-and-evolution-of-humans-run-1` | 40 |

## 1. Summary

| Dataset | Total | KEEP | REVISE | DELETE | Collector E/M/H | Reviewed E/M/H (survivors) | Recall / reasoning (survivors) |
|---|---|---|---|---|---|---|---|
| P1 cell and tissues | 40 | 27 | 5 | 8 | 16/20/4 | 22/9/1 | 24 / 8 |
| P2 skin | 40 | 26 | 11 | 3 | 7/25/8 | 20/16/1 | 27 / 10 |
| P3 chemical composition | 39 | 28 | 11 | 0 | 10/21/8 | 24/14/1 | 33 / 6 |
| P4 human evolution | 40 | 28 | 8 | 4 | 16/20/4 | 17/17/2 | 25 / 11 |
| **Total** | **159** | **109** | **35** | **15** | 49/86/24 | 83/56/5 | 109 / 35 |

Verdict: the datasets are usable after a correction pass, but the strategy is
**not ready to scale unchanged**. One key is wrong as worded (P3-031), one
data item rests on numbers an authoritative source contradicts (P3-015),
difficulty labels are inflated in every dataset, and narrow topics pad to the
cap with re-angled facts. Section 8 lists the rules to add before Wave 2.

## 2. Findings that affect correctness

| Id | Finding | Evidence |
|---|---|---|
| P3-031 | **Key wrong as worded.** „Усвояват се по-добре, когато храната съдържа малко мазнини“ reads as "when the food is low in fat", the opposite of the explanation. | Explanation in the same candidate; fat-soluble vitamins are absorbed with dietary fat. |
| P3-015 | Tissue water figures (brain 85 %, bone 22 %, enamel 3 %) are not supported by the source checked: brain 73 %, muscle 79 %, bone 31 %; no enamel figure. The conclusion is still right, the numbers are fragile and the programme does not require them. | USGS Water Science School, citing Mitchell et al. 1945 (J. Biol. Chem. 158). |
| P3-004 | Iron is given as a microelement. Bulgarian textbooks do not agree on where iron belongs (macro- vs microelement), so the example makes the item textbook-dependent. | Classification differs between school sources; not verified against one textbook. |
| P4-018 | „Неандерталците са странично разклонение, не пряк предшественик на нас“ is too strong: Neanderthals and modern humans share a common ancestor **and** interbred with non-African modern humans. | Smithsonian Human Origins, *Homo neanderthalensis*. |
| P4-038 | ">99 % identical genomes" is used as proof of one species. The figure is right (about 0.1 % difference) but the inference is not: human–chimpanzee similarity is about 98.8 %, so a percentage does not define a species. | Smithsonian Human Origins, *Genetics*. |
| P4-029 | Stem claims one advantage of bipedalism is "best substantiated". Carrying is one of several hypotheses; the option is true, the ranking is not established. | General state of the field; not a single source. |
| P4-006 | Keyed option says labour "encourages the brain" — reads as inheritance of acquired traits, the same error distractor B is meant to catch. | Internal consistency. |
| P2-039 | Distractor C (immediate very cold bath) is not safely wrong: cooling with cold water is recommended for heatstroke. | NHS, heat exhaustion and heatstroke. |
| P2-038 | Sunstroke vs heatstroke is a Bulgarian school distinction; the medical source checked does not separate them. Key is acceptable as curriculum terminology; the explanation should say both are overheating with similar first aid. | NHS (term "sunstroke" not used). |
| P2-028 | Keyed option relies on "DNA damage"; DNA is Grade 9 content. | Plan: cell detail is Grade 9. |
| P2-034 / P2-039 | „хладка“ / „хладък“ means tepid; the guidance is **cool** running water for 20 minutes and cool sponging. | NHS, burns and scalds; NHS, heat exhaustion. |

Verified and found correct: P3-001 (C, H, O, N "over 95 %" — the usual figure
is about 96 %), P3-002 (oxygen largest by mass), P3-006 (crust O 47 / Si 28 /
C 0.02 / N < 0.01; body O 65 / C 18 / N 3 — standard reference values, not
re-fetched), P3-017 sucrose = glucose + fructose, maltose = 2 glucose, lactose
= glucose + galactose, P3-014 Ca²⁺ in clotting and muscle contraction, P3-012
Na⁺/K⁺/Cl⁻ as dissolved ions, P2-021 humidity and evaporation, P2-022
goosebumps (hair-follicle muscles), P2-036/037 frostbite, P4-009 (*H. habilis*
"thought to represent the first maker of stone tools" — the candidate's wording
"associated with" is safe), P4-010, P4-011 (Neanderthal brain as large as ours
or larger), P4-026/027 (common ancestor 6–8 million years ago), P4-036 (46 vs
48 chromosomes), P4-031/034 (pigmentation and UV; no racial typology anywhere
in P4, the term „раса“ is absent).

Sources used: Smithsonian Human Origins (species overview, *H. neanderthalensis*,
*H. habilis*, genetics); USGS Water Science School (Mitchell 1945); NHS (heat
exhaustion and heatstroke; burns and scalds); OpenStax *Anatomy and Physiology
2e* §2.1 (confirms the four main elements; its percentages are in a figure that
could not be read). The МОН programme PDFs were **not** re-read: scope was
judged from the local plan and structure review.

## 3. Scope

- P1: no Grade 9 cell detail found. Organ-to-system items (016, 026) are the
  plan's D5 and stay, but they will be duplicated by the system topics.
- P2: 028 (DNA) is the only leak. 031 (changing mole) is health education at
  the edge of the topic; kept.
- P3: 029 (saturated vs unsaturated fatty acids), 030 (steroid) and 034
  (amino-acid radical) are at the upper edge of "school depth". Kept, but check
  them against the programme text before approval.
- P4: follows the 2023 amendment. 011 and 016 depend on brain-volume numbers
  (acceptable as ranges; do not add more numeric items).

## 4. Difficulty and recall

| Dataset | Collector E/M/H | Reviewed E/M/H | Bias |
|---|---|---|---|
| P1 | 16/20/4 | 22/9/1 | "medium" used for one-fact items with longer wording |
| P2 | 7/25/8 | 20/16/1 | strongest inflation: "hard" given to less familiar facts (008 melanin cells, 031 mole, 040 when to call 112) |
| P3 | 10/21/8 | 24/14/1 | "hard" given to facts that feel advanced (029, 021, 022, 005) |
| P4 | 16/20/4 | 17/17/2 | closest; "hard" still recall of a textbook chain (007) |

Systematic bias: collectors rate **fact familiarity and wording length**, not
cognitive demand. Only 5 of 144 survivors are hard by the plan's own definition
(combine two ideas, predict, interpret). The plan's 10–20 % hard share is not
being met anywhere.

Recall dominates: 109 of 144 survivors are recall or recognition, including
scenario items that wrap one memorised fact (P2-010, P2-011, P2-025, P4-039).
P3 is the weakest (6 reasoning items of 39).

## 5. Distractors, length and stems

- **Length.** Correct option strictly longest: P1 15/40, P2 12/40, P3 15/39,
  P4 10/40 (chance is 10). The collector's "20/39" for P3 counts ties. It is a
  usable cue only where the gap is large: P1-019, 029, 031 (deleted), P1-017,
  025, P3-005, 006, 021 (revised). Elsewhere it is not exploitable.
- **Wording cues.** Three distractors with „само“ against a key without it:
  P1-033, P3-012, P3-035. Key is the only option with a different opening:
  P1-034. Only the key fits the stem grammatically: P2-013.
- **Category mismatch.** P3-030 (three non-lipids against "which lipid"),
  P2-040 option C (a cut, in a burn question), P4-037 option C.
- **Stems.** No template dominates. P2 has six stems that end without
  punctuation before the options.
- **Skill tags.** P1 uses 20 different `research.skill` values, the others 5–8.
  P3 tags only "D1"–"D5". Neither is useful for a coverage audit.
- **Rendering.** P3-006 carries a pipe-and-newline table inside `prompt`;
  confirm the quiz page renders it before approval.

## 6. Duplicates and clusters

Within datasets:

| Cluster | Items | Decision |
|---|---|---|
| P1 skeletal muscle is voluntary | 009, 021 | delete 021 |
| P1 cardiac + smooth are involuntary | 028, 038 | delete 028 |
| P1 organ = several tissues | 003, 013, 019 | delete 019 |
| P1 hierarchy cell→tissue→organ | 001, 029 | delete 029 |
| P1 outer skin layer is epithelium | 015, 030 | delete 030 |
| P1 "X is connective tissue" | 004, 008, 035, 036, 037 | delete 036 |
| P1 tissue = similar structure and function | 002, 033 | delete 033 |
| P1 muscle cluster after deletions | 009, 010, 020, 038, 039 | distinct enough |
| P2 epidermis has no vessels | 001, 004, 005 | delete 001 |
| P2 skin receptors | 013, 016 | delete 016 |
| P2 subcutaneous fat insulates | 009, 024 | delete 024 |
| P2 burns | 033, 034, 035, 040 | distinct (degree, first step, blisters, when to call) |
| P2 frostbite 036/037, acne 026/030, melanin 008/028, functions 010/015 | | distinct, keep |
| P3 element–role pairs recycled as each other's distractors | 003, 013, 014 | keep; answers leak across items in one quiz |
| P3 cellulose | 019, 021, 022 | distinct |
| P4 recognise social factors | 002, 003 | delete 003 |
| P4 bipedalism came first | 007, 008, 014, 019 | delete 014 |
| P4 Neanderthal vs Cro-Magnon features | 011, 012, 015 | delete 015 |
| P4 fertile offspring = one species | 035, 037, 039 | delete 035 |
| P4 pigmentation | 031, 034 | distinct reasoning (selection vs convergence), keep |

Across datasets:

- P2-019 and P3-010 are the same question (why sweating cools). P3-010 is
  re-framed as a property of water.
- "Fat insulates and stores energy": P1-011, P2-009, P3-027 (and deleted
  P2-024). Different grades or topics; acceptable, but the fact is now covered.
- "Outer skin layer is epithelium": P1-015 and P2-004.

## 7. Bank size and saturation

| Topic | Survivors | Still missing | Further distinct items without padding | Recommended final size | Next run |
|---|---|---|---|---|---|
| g8-cell-and-tissues | 32 | image-based tissue recognition (deferred by the plan) | about 4–6, all image-dependent | 32–36 | none planned; none needed |
| g8-skin | 37 | skin derivatives (hair, nails), more hard items | about 3–5 | 36–40 | none planned; none needed |
| g9-chemical-composition | 39 | D6–D9 (enzymes, nucleic acids, monomer grouping, living vs non-living) | 25–30 in run-2; D9 is already partly covered by 005/006 | 60–66 | run-2 should proceed, capped near 32, with a reasoning quota |
| g10-origin-and-evolution-of-humans | 36 | fossil and cultural evidence per stage (fire, art, burial), one or two anatomy comparisons | about 4–6 | 34–38 | none planned; do **not** add one — the topic is saturated |

The collector's saturation warning for human evolution is confirmed: 4 of 40
were re-angled duplicates and D5 has three independent arguments in total.

Pilot evidence on the 1,470 raw target: **RECALIBRATE**, not a flat cut.

- Narrow topics reach saturation at 32–37 distinct questions; a 40 cap filled
  to the cap produced 8, 3 and 4 redundant items. Treat every run maximum as a
  ceiling with an explicit early stop, and expect about 36 raw for narrow topics.
- The plan's yield assumption (74–87 %) held numerically (91 % survive), but
  the survivors are 76 % recall and 58 % easy. The shortfall is in kind, not in
  count.
- Not shown by this pilot: whether broad topics (66–96 raw) hold up. Only one
  half of one broad topic was collected. Keep their targets for now and review
  the first broad two-run topic before Wave 7.

Remaining 20 topics: KEEP the raw targets as ceilings; REDUCE narrow-topic
expectations by about 10 %; do not INCREASE anything.

## 8. Rules for Waves 2–12

Keep as standard: A/B/C/D balancing; the answer-length audit; stem-diversity
audit; concept-cluster audit; "scope from the programme, facts from an
authoritative source".

Change or add:

1. **Skill tags from a fixed list**: `D<n>` plus one of `recall`, `compare`,
   `explain`, `apply`, `interpret`. No free text.
2. **Difficulty by demand**: hard only if two ideas are combined, a change is
   predicted or data are interpreted. A less familiar fact is not hard.
   Collectors must report how many hard items meet this.
3. **Reasoning floor**: at least a third of each run must need more than one
   recalled fact; a scenario wrapping one fact counts as recall.
4. **One item per fact**: before adding, name the single fact or relation
   tested; reject a second item with the same one, including its reverse,
   its "why" form and its scenario form.
5. **Early stop**: when the next item would repeat a named fact, stop with
   `topic_complete` below the cap. Reaching the cap is not a goal.
6. **Numbers**: no percentages, dates or volumes unless the programme requires
   them; any that remain cite a fetched authoritative source in `research.notes`.
7. **Verification is not optional**: P1–P3 used no external source at all.
   Every health, first-aid and numeric claim needs one fetched source.
8. **Option hygiene**: no „само/винаги/никога“ in distractors only; all options
   the same grammatical form and fitting the stem; all options from the
   category the stem names; no distractor that is true in the real world.
9. **Key wording check**: read the keyed option alone and confirm it cannot be
   read as its opposite (P3-031).
10. **Cross-topic check**: read sibling datasets of the same grade for the
    facts already used (fat insulation, sweating, epithelium).
11. **No tables in `prompt`** until rendering is confirmed.

## 9. Candidate decisions

Columns: decision; collector difficulty → reviewed difficulty; R = recall or
recognition, S = reasoning or application; action. E/M/H = easy/medium/hard.
Revisions quote the exact replacement text.

### P1 — `biology-g8-cell-and-tissues-run-1-NNN`

| NNN | Decision | Diff | Kind | Reason / action |
|---|---|---|---|---|
| 001 | KEEP | E→E | R | |
| 002 | KEEP | E→E | R | |
| 003 | KEEP | E→E | R | |
| 004 | KEEP | E→E | R | |
| 005 | KEEP | E→E | R | |
| 006 | KEEP | E→E | R | |
| 007 | KEEP | E→E | R | |
| 008 | KEEP | E→E | R | |
| 009 | KEEP | E→E | R | |
| 010 | KEEP | E→E | R | |
| 011 | KEEP | E→E | R | |
| 012 | KEEP | E→E | R | |
| 013 | KEEP | E→E | R | |
| 014 | KEEP | E→E | R | |
| 015 | KEEP | E→E | R | |
| 016 | KEEP | E→E | R | |
| 017 | REVISE | M→M | R | Length cue. Option D → „между клетките ѝ има много междуклетъчно вещество“. |
| 018 | KEEP | M→E | R | |
| 019 | DELETE | M | | Same fact as 003 (organ = several tissues) in "why" form; key is the longest option. |
| 020 | KEEP | M→M | R | |
| 021 | DELETE | M | | Same fact as 009, whose stem already states it. |
| 022 | REVISE | M→E | R | Weak distractors. B → „свързват костите една с друга в ставите“; C → „покриват ставните повърхности на костите“. |
| 023 | KEEP | M→M | S | |
| 024 | KEEP | M→E | R | |
| 025 | REVISE | M→M | S | Length cue. Option B → „в основата ѝ е еластична хрущялна тъкан“. |
| 026 | KEEP | M→E | R | |
| 027 | KEEP | M→E | R | |
| 028 | DELETE | M | | Same fact as 038 (cardiac and smooth are involuntary). |
| 029 | DELETE | M | | Same hierarchy as 001 in sentence form; key is the longest option. |
| 030 | DELETE | M | | Same fact as 015 inside a scenario. |
| 031 | DELETE | M | | Truism with absurd distractors; not worth rewriting. |
| 032 | KEEP | M→E | R | |
| 033 | DELETE | M | | Restates the definition in 002; three „само“ distractors. |
| 034 | REVISE | M→M | S | Key is the only option not starting with „така“. Options → A „за да останат тъканите живи с години“; B „за да се съкращават клетките и да се виждат по-добре“; C „за да се премахне междуклетъчното вещество“; D „за да преминава светлината през препарата“. |
| 035 | KEEP | M→M | S | |
| 036 | DELETE | M | | Fourth "X is connective tissue" item; mismatched pairs make it trivial. |
| 037 | KEEP | H→M | S | |
| 038 | KEEP | H→M | S | |
| 039 | KEEP | H→M | S | |
| 040 | REVISE | H→H | S | Grammar in option D: „една“ → „едно – всички са клетки“. |

### P2 — `biology-g8-skin-run-1-NNN`

| NNN | Decision | Diff | Kind | Reason / action |
|---|---|---|---|---|
| 001 | DELETE | E | | "Epidermis has no vessels" is covered by 004 and 005; the stem gives the answer away. |
| 002 | KEEP | E→E | R | |
| 003 | KEEP | M→E | R | |
| 004 | KEEP | M→M | R | |
| 005 | KEEP | M→M | S | |
| 006 | KEEP | M→E | R | |
| 007 | KEEP | M→M | S | |
| 008 | KEEP | H→E | R | One recalled fact; not hard. |
| 009 | KEEP | M→M | S | |
| 010 | KEEP | M→E | R | |
| 011 | KEEP | M→E | R | |
| 012 | REVISE | E→E | R | Stem has no end punctuation: add „:“. |
| 013 | REVISE | E→E | R | Only the key fits the stem. Add „:“ to the stem; B → „мастни жлези, които реагират на натиск върху кожата“; C → „пигментни клетки, които реагират на температура“; D → „рогови люспи, които предават усещането за болка“. |
| 014 | KEEP | M→M | R | |
| 015 | KEEP | M→E | R | |
| 016 | DELETE | M | | Trivial (colour vision); receptors are covered by 013. |
| 017 | KEEP | H→M | R | |
| 018 | KEEP | M→M | S | |
| 019 | KEEP | M→M | R | |
| 020 | KEEP | E→E | R | |
| 021 | KEEP | H→M | S | Correct; one-step application, not hard. |
| 022 | REVISE | M→E | R | Correct. Add „:“ to the stem. |
| 023 | KEEP | H→M | S | |
| 024 | DELETE | M | | Same fact as 009 (and P1-011). |
| 025 | KEEP | M→E | R | |
| 026 | REVISE | E→E | R | Add „:“ to the stem. |
| 027 | KEEP | M→E | R | |
| 028 | REVISE | M→E | R | DNA is Grade 9. Option D → „Увреждане на клетките и повишен риск от рак на кожата“; explanation → „Ултравиолетовите лъчи увреждат кожните клетки; многократните изгаряния повишават риска от рак на кожата.“ |
| 029 | REVISE | M→M | S | Add „:“ to the stem. |
| 030 | KEEP | M→E | R | |
| 031 | KEEP | H→E | R | Edge of scope (health education). |
| 032 | REVISE | M→M | R | Add „:“ to the stem. |
| 033 | KEEP | M→M | S | |
| 034 | REVISE | E→E | R | „хладка“ → „хладна“ in option D and in the explanation. |
| 035 | KEEP | M→E | R | |
| 036 | KEEP | M→E | R | |
| 037 | KEEP | H→M | S | |
| 038 | REVISE | H→M | R | Key stands as curriculum terminology. Append to the explanation: „И двете състояния са прегряване и първата помощ е сходна: хладно място, охлаждане и течности.“ |
| 039 | REVISE | M→H | S | Option A: „хладък“ → „хладен“. Option C is not safely wrong → „Продължава играта, но вече с шапка на главата“. |
| 040 | REVISE | H→M | R | Option C is not a burn → „При малък мехур на върха на един пръст“. |

### P3 — `biology-g9-chemical-composition-of-living-matter-run-1-NNN`

| NNN | Decision | Diff | Kind | Reason / action |
|---|---|---|---|---|
| 001 | KEEP | E→E | R | |
| 002 | KEEP | E→E | R | |
| 003 | KEEP | M→E | R | |
| 004 | REVISE | M→E | R | Iron's class is textbook-dependent. Stem examples → „(например йод, цинк, мед)“; explanation → „…липсата им нарушава процеси като синтеза на хормони (I).“ |
| 005 | REVISE | H→M | R | Length cue. Option B → „Живите организми съдържат същите елементи като неживата природа, но в различно съотношение“. |
| 006 | REVISE | H→M | S | Data verified. Confirm the table renders, otherwise give the eight values as a sentence. Option A → „Кислородът е с най-голям дял и в двете, а въглеродът е много повече в тялото“. |
| 007 | KEEP | M→M | R | |
| 008 | KEEP | E→E | R | |
| 009 | REVISE | M→M | S | Grammar in option D → „Безцветна и прозрачна е и пропуска светлината през клетките“. |
| 010 | REVISE | M→M | R | Same question as P2-019. Stem → „Изпотяването охлажда тялото. С кое свойство на водата е свързано това?“; A → „добър разтворител на соли“; B (key) → „висока топлина на изпарение – при изпаряване отнема много топлина“; C → „най-голяма плътност при 4 °C“; D → „прозрачност за светлината“. |
| 011 | KEEP | M→M | R | |
| 012 | REVISE | M→E | R | „Само“ cue. A → „Като разтворени йони – неразтворими соли не се срещат“; B → „Като неразтворими соли, отложени в тъканите като кристали“; D → „Като съединения, свързани с белтъците, а не като свободни йони“. |
| 013 | KEEP | M→E | R | |
| 014 | KEEP | M→E | R | |
| 015 | REVISE | H→M | S | Numbers not supported. Stem → „Съдържанието на вода в мозъка и в мускулите е около 75–80%, а в костите – около 20–30%. Кой извод е най-добре подкрепен от данните?“; in option D remove „и зъбният емайл“; explanation → „Мозъкът и мускулите (активна обмяна) съдържат много повече вода от костите (твърди, минерализирани).“ |
| 016 | KEEP | E→E | R | |
| 017 | KEEP | M→E | R | |
| 018 | KEEP | M→E | R | |
| 019 | KEEP | M→E | R | |
| 020 | KEEP | M→E | R | |
| 021 | REVISE | H→M | R | Length cue. Option A → „Човекът няма ензим, който да разгражда целулозата“. |
| 022 | KEEP | H→M | R | |
| 023 | KEEP | E→E | R | |
| 024 | KEEP | E→E | R | |
| 025 | KEEP | E→E | R | |
| 026 | KEEP | M→M | R | |
| 027 | KEEP | M→E | R | |
| 028 | KEEP | M→M | R | |
| 029 | KEEP | H→M | R | Check against the programme text before approval. |
| 030 | REVISE | M→E | R | Distractors are not lipids. A → „триглицеридът“; B → „фосфолипидът“; C → „восъкът“; explanation → „Холестеролът е стероид. Триглицеридите са запасни мазнини, фосфолипидите изграждат мембраните, а восъците са защитни липиди.“ |
| 031 | REVISE | H→M | S | **Key ambiguous.** Option C → „Усвояват се по-добре, когато се приемат с храна, съдържаща мазнини“. |
| 032 | KEEP | E→E | R | |
| 033 | KEEP | E→E | R | |
| 034 | KEEP | M→E | R | |
| 035 | REVISE | M→M | S | „само“ cue. A → „кватернерната структура“; C → „вторичната структура“. |
| 036 | KEEP | M→E | R | |
| 037 | KEEP | M→E | R | |
| 038 | KEEP | H→H | S | |
| 039 | KEEP | E→E | R | |

### P4 — `biology-g10-origin-and-evolution-of-humans-run-1-NNN`

| NNN | Decision | Diff | Kind | Reason / action |
|---|---|---|---|---|
| 001 | KEEP | E→E | R | |
| 002 | KEEP | E→E | R | |
| 003 | DELETE | M | | Same recognition as 002 inside a scenario; option categories are not parallel. |
| 004 | KEEP | M→M | R | |
| 005 | KEEP | M→M | S | |
| 006 | REVISE | M→M | R | Key reads as inherited acquired traits. Option D → „създава предимство за индивидите с по-развит мозък и общуване“; explanation → „Трудът и общуването не променят гените; те създават условия, при които отборът благоприятства по-развит мозък и реч.“ |
| 007 | KEEP | H→M | S | Simplified school chain; stem already says "main tendency". |
| 008 | KEEP | E→E | R | |
| 009 | KEEP | E→E | R | |
| 010 | KEEP | E→E | R | |
| 011 | KEEP | M→M | R | |
| 012 | KEEP | M→M | R | |
| 013 | KEEP | E→E | R | |
| 014 | DELETE | M | | "Bipedalism came first" is tested by 007, 008 and 019. |
| 015 | DELETE | M | | Combines the features already tested in 011 and 012. |
| 016 | KEEP | E→E | R | Number-dependent; acceptable as a range. |
| 017 | KEEP | E→E | R | |
| 018 | REVISE | H→H | S | Overstates the separation. Option A → „Двете групи имат общ предшественик и са съществували едновременно“; explanation → „Неандерталците и съвременните хора произлизат от общ предшественик, живели са по едно и също време и са се кръстосвали; това не е права стълбица.“ |
| 019 | KEEP | M→M | R | |
| 020 | KEEP | E→E | R | |
| 021 | KEEP | E→E | R | |
| 022 | KEEP | E→E | R | |
| 023 | KEEP | M→M | S | |
| 024 | KEEP | M→E | R | |
| 025 | KEEP | M→M | R | |
| 026 | KEEP | E→E | R | |
| 027 | KEEP | M→M | S | |
| 028 | KEEP | E→E | R | |
| 029 | REVISE | M→M | S | "Best substantiated" overclaims. Stem → „Кое от изброените е действително предимство на изправената походка в открита местност?“ |
| 030 | KEEP | E→E | R | |
| 031 | REVISE | M→M | S | Wording. Option B → „отбор в полза на пигмента, който предпазва от ултравиолетовите лъчи“. |
| 032 | REVISE | M→M | R | Stem ("long isolated groups") gives the answer. Stem → „Кой фактор, освен естествения отбор, допринася за натрупването на различия между човешките популации?“; C → „географската изолация, която ограничава обмена на гени“. |
| 033 | KEEP | M→M | S | |
| 034 | REVISE | H→H | S | Awkward distractors. A → „близък общ произход на двете популации“; C → „постоянен обмен на гени между двете популации“. |
| 035 | DELETE | E | | Same argument as 037 (and reversed in 039). |
| 036 | KEEP | E→E | R | Recall of the karyotype; weak as an "argument" item. |
| 037 | REVISE | M→M | S | Option C is nonsense → „различията в цвета изчезват още в следващото поколение“. |
| 038 | REVISE | M→M | R | A percentage does not prove one species. Stem → „Какво показват генетичните изследвания за различията между хората от различни популации?“; A → „различията между всеки двама души са много малки“; B → „всеки човек има напълно различни гени от останалите“; C → „хората от различни континенти се различават в повечето си гени“; explanation → „Геномите на всеки двама души се различават с около 0,1%.“ |
| 039 | KEEP | M→E | R | |
| 040 | KEEP | H→M | S | |
