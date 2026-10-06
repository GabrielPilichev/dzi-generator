# Biology Wave 2 — R2 independent review

Reviewed 2026-10-06 (Opus 5.5 High, plan section I review R2). Review only: no
candidate JSON, plan, structure file, tracked repository file or database was
changed; nothing was approved, rejected, exported or imported. Wave 3 was not
started. The digestive run-1 file was not reviewed (deferred to CP1).

All 93 candidates were read in full (stem, four options, key, explanation,
`research` block). Collector self-reports were treated as leads only.

| # | Dataset (`data/question_candidates/biology/…`) | Candidate id = prefix + `-` + id below | Candidates |
|---|---|---|---|
| X | `grade-8/biology-g8-excretory-system-run-1.json` | `biology-g8-excretory-system-run-1` | 34 |
| E | `grade-8/biology-g8-embryonic-and-postembryonic-development-run-1.json` | `biology-g8-embryonic-and-postembryonic-development-run-1` | 27 |
| S | `grade-9/biology-g9-supramolecular-complexes-run-1.json` | `biology-g9-supramolecular-complexes-run-1` | 32 |

Files reviewed (copies in `/mnt/project-files/biology/wave-2/candidates/`, unchanged before and after this review):

- X `214f181caf2b519edc5642c85fbf80506fef517a78d057ca42d9032b421e671c`
- E `9d62419ce3a14e732b8122ffb559761fed21017b88b0b8b81fb51d66f6645e79`
- S `e63b9adbb08e9d1a799af366410a0ffc16a0af970ab62dff89b8a73e4e3c9fd2`

## 1. Overall verdict

**PASS WITH REQUIRED REVISIONS.**

- No wrong key was found in any dataset, and no unsourced clinical claim. Every
  health, prevention and numeric claim that a key depends on was checked
  against a fetched authoritative page (section 5). The programme-level stop
  rule (wrong key or unsourced clinical claim in more than 5 % of a dataset) is
  not triggered.
- 15 candidates need a bounded correction: a scientific contradiction
  (S-07), an ambiguous distractor (X-005), non-school terminology (X-033),
  stem give-aways (E-012, S-15, S-04), a re-angled key (X-027), weak
  distractors (X-032), unnecessary numbers and option-length cues. 11 are
  deleted as re-angled duplicates, trivia or out-of-scope detail. No dataset
  exceeds the one-fifth re-angled duplicate limit (X 3/34: 003, 013 and 027
  before revision; E 4/27; S 3/32).
- **Embryonic is not a complete topic bank.** After review it holds 23 items
  against a 28–34 target, with post-embryonic life stages (D5) covered by one
  newborn item and infection/medicine/drug risks (D6) absent. The individual
  items are sound; the topic cannot be marked COMPLETE until the owner decides
  on a bounded supplement (section 8).
- **Reasoning floors are missed after recalibration** in excretory (27 % vs
  ≥ 33 %) and embryonic (26 % vs ≥ 30 %). Collector difficulty and skill
  labels were inflated in both, as in Wave 1. Supramolecular meets its floor
  (48 %).

## 2. Summary

| Dataset | Total | KEEP | REVISE | DELETE | Difficulty changes (survivors) | Major coverage gaps |
|---|---|---|---|---|---|---|
| X G8 excretory | 34 | 21 | 9 | 4 | 10 (9 medium→easy, 1 hard→medium) | No gross kidney structure (cortex/medulla/pelvis); no urine-substance item (urea) until X-033 is revised; no excretion-as-a-process item (lungs, skin, kidneys); D6 water balance has no prediction item; reasoning 27 % < 33 % floor |
| E G8 embryonic and postembryonic | 27 | 20 | 3 | 4 | 9 (all medium→easy) | **D5 post-embryonic periods: 1 item (newborn only)**; **D6 infections, medicines, drugs: 0 items**; D4 birth 2 items; responsibility for offspring absent; D6 is 8/23 alcohol + smoking; reasoning 26 % < 30 % floor |
| S G9 supramolecular complexes | 32 | 26 | 3 | 3 | 5 (4 medium→easy, 1 hard→medium) | No definition item until S-04 is revised; D1 examples (ribosome, chromatin) unverified against the programme; virus forms thin (no plant virus or phage-structure item); lysis as a release mode untested |
| **Total** | **93** | **67** | **15** | **11** | **24** | |

Survivors after review: X 30 (target 30–36), E 23 (target 28–34), S 29
(target 30–36).

| Dataset | Collector E/M/H (all) | Reviewed E/M/H (survivors) | Reasoning share (survivors, non-easy) | Floor |
|---|---|---|---|---|
| X | 14/19/1 | 22/8/0 | 8/30 = 27 % | ≥ 33 % — **missed** |
| E | 8/19/0 | 17/6/0 | 6/23 = 26 % | ≥ 30 % — **missed** |
| S | 13/17/2 | 15/13/1 | 14/29 = 48 % | ≥ 33 % — met |

Collector reasoning claims were 59 % (X), 63 % (E) and 59 % (S). The gap comes
from `explain` and `apply-health` items whose answer is one recalled fact or
one rule with three plainly unsafe alternatives (plan B6: a scenario wrapped
around one fact is recall). The embryonic collector's own warning that some
`explain` items test one fact is confirmed: E-002, E-008, E-018, E-019 and E-023
are single-fact items labelled `explain`.

## 3. Candidate-level decisions

Difficulty column: the collector's label, then the recommended final label in
bold where it changes. For deleted items the recommended label is shown for
completeness only.

### 3.1 X — G8 excretory system (34)

| ID | Decision | Collector → recommended difficulty | Reason / action |
|---|---|---|---|
| 001 | KEEP | easy | Bladder stores urine (NIDDK). Clean recall. |
| 002 | KEEP | easy | Path kidney → ureter → bladder → urethra. Canonical D1 order item; its distractors already separate ureter from urethra. |
| 003 | **DELETE** | medium | Re-angle of 002 (B5): which organ links kidney–bladder vs bladder–outside is fully discriminated by 002's swapped-order distractors. |
| 004 | **DELETE** | easy | Count trivia (two kidneys, two ureters); the same structural fact is the premise of 014, which applies it. Keep 014 instead. |
| 005 | **REVISE** | easy | Distractor D „В горната част на корема, под стомаха“ is not unambiguously wrong (kidneys lie in the upper abdomen, behind and below the stomach level). Replace D with a clearly wrong site; shorten key (63 chars vs ≤39) e.g. „От двете страни на гръбначния стълб, под ребрата“. |
| 006 | KEEP | easy | Main kidney function; distractors are real confusions (lungs, digestion, bladder). |
| 007 | KEEP | easy | Nephron = glomerulus + tubule (NIDDK). |
| 008 | KEEP | medium → **easy** | Correct (NIDDK: thin walls pass small molecules; proteins and cells stay). Downgrade: the stem states the size observation and the key restates it; one fact. |
| 009 | KEEP | medium | Glomerulus vs tubule roles, reversed-pair distractor. Two facts compared; counts as reasoning. |
| 010 | KEEP | easy | Renal artery in, renal vein out (NIDDK). |
| 011 | KEEP | medium | Filtration → return → bladder → out. Real sequence logic (nothing returns before it is filtered). |
| 012 | KEEP | medium | Large filtrate, little urine → tubules return almost all water (NIDDK). Links filtration and reabsorption; also serves D6 water balance. |
| 013 | **DELETE** | medium | Same label as 012 (tubule returns a needed filtered substance), re-angled to glucose; its explanation repeats 017's diabetes threshold. 012 + 017 keep both ideas. |
| 014 | KEEP | hard → **medium** | Unilateral ureter block: right side keeps draining. Downgrade from hard: one structural fact (each kidney has its own ureter) applied once. |
| 015 | **REVISE** | medium | Key correct (OpenStax 25.1: only traces of protein normally; excess usually means glomerular damage). Explanation „в урината има най-много следи от тях“ reads as „the most traces“; change to „в урината има само следи от белтък“. |
| 016 | KEEP | medium | Leukocytes + bacteria + burning → UTI (OpenStax 25.1, NIDDK). Good discriminating distractors. |
| 017 | **REVISE** | medium → **easy** | Correct (OpenStax 25.1). Downgrade: single association. Key length cue (49 vs ≤28): shorten to „Захарен диабет“ or lengthen distractors equally. |
| 018 | KEEP | easy | Cells not normally in urine (OpenStax 25.1). |
| 019 | KEEP | medium → **easy** | Dark urine → dehydration (NHS stones, OpenStax 25.1). Downgrade: one recognised association. |
| 020 | KEEP | medium → **easy** | Blood in urine → see a doctor (NHS kidney infection). No distractor is a real-world acceptable action. Downgrade: answer is obvious once any health rule is known. |
| 021 | KEEP | easy | Bacteria are the usual cause of bladder infection (NIDDK). |
| 022 | KEEP | easy | Untreated bladder infection can reach the kidneys (NIDDK). |
| 023 | KEEP | medium → **easy** | Fluids, water best, help prevent bladder infection (NIDDK). Downgrade: one rule. |
| 024 | KEEP | medium → **easy** | Do not hold urine; empty fully (NHS kidney infection, verified). Downgrade: one rule. |
| 025 | **REVISE** | medium | Correct (NHS stones: pain starts in side or lower back, comes and goes; cloudy pee and frequent peeing occur in both; NIDDK: burning = UTI). Strong length cue: key 56 chars vs 11–20. Balance option lengths. |
| 026 | **REVISE** | easy | Correct (NHS: build-up of chemicals in pee forms hard lumps). Key 77 chars vs ≤42: shorten key or lengthen distractors. |
| 027 | **REVISE** | medium → **easy** | Key is pickable from the water fact alone, the same label as 023. Make salt the discriminating fact: e.g. stem „Освен достатъчно вода, кой навик намалява риска от бъбречни камъни?“ with key „Да не прекалява със солта“ (NHS stones, verified) and three non-acceptable habits. |
| 028 | KEEP | easy | Diabetes and high blood pressure are the commonest CKD causes (NIDDK, NHS). |
| 029 | KEEP | medium → **easy** | No early CKD symptoms; found by blood/urine tests (NHS, verified). Downgrade: one fact. |
| 030 | KEEP | medium → **easy** | At-risk parent: get tested, control blood pressure (NIDDK). Downgrade: the other three options are plainly unsafe. |
| 031 | **REVISE** | medium | Correct (OpenStax 25.2). Remove „около 4 cm“ from the explanation (B7: unnecessary number) and balance key length (80 vs ≤42). |
| 032 | **REVISE** | easy | Concept is worthwhile (D6, kidneys keep water–salt balance) but distractors A–C are implausible and the key (64 vs ≤46) is a paraphrased NIDDK sentence. Key → постоянство на вътрешната среда (water, salts) as the reason; three plausible wrong reasons. |
| 033 | **REVISE** | easy | „киселината, образувана от клетките“ is a lay translation of NIDDK, not school terminology, and invites confusion with пикочна киселина. Key → end products of protein breakdown such as урея (OpenStax 25.1 mentions urea from protein breakdown) plus excess salts; balance length (65 vs ≤43). |
| 034 | **DELETE** | medium | Dialysis is treatment detail beyond the appendix §4 limit (cause – signs – prevention); fourth CKD item; key is the only option naming the kidney after a kidney-disease stem (83 vs ≤54 chars). |

### 3.2 E — G8 embryonic and postembryonic development (27)

| ID | Decision | Collector → recommended difficulty | Reason / action |
|---|---|---|---|
| 001 | KEEP | easy | Zygote (OpenStax 28.1). |
| 002 | KEEP | medium → **easy** | Half the genetic material from each parent (OpenStax 28.1). Downgrade: labelled explain but tests one fact. |
| 003 | KEEP | medium | Fertilisation in the uterine tube because the unfertilised egg does not survive the journey (OpenStax 28.1, verified). Needs two facts (short egg life; slow passage). |
| 004 | KEEP | medium | Fertilisation → divisions → implantation → placenta feeds. Genuine ordering; cleavage named only, no G10 detail. |
| 005 | KEEP | easy | зигота – зародиш – плод – новородено; correct embryo/fetus terminology. |
| 006 | KEEP | medium | Embryonic period lays down organ systems, fetal period grows and matures them (OpenStax 28.2/28.3). Distractors A and D both test order already in 005; acceptable. |
| 007 | **REVISE** | easy | Explanation gives „около края на първата седмица“ (plan D: NO week-by-week milestones); remove. Key „вгнездяване (имплантация)“ is 25 chars vs ≤9 and echoes „вграждането“ in the stem: drop the parenthetical or give every option one. |
| 008 | KEEP | medium → **easy** | No implantation → no maternal supply (OpenStax 28.2). Downgrade: one inference; the collector's own flag on nominal explain items applies here. |
| 009 | KEEP | easy | Amniotic fluid protects from trauma and temperature change (OpenStax 28.2, verified). Within G8 scope. |
| 010 | KEEP | easy | Placenta = exchange organ (OpenStax 28.2). Kept as the single placenta-exchange recall item. |
| 011 | **DELETE** | medium → **easy** | Same label as 010 (placenta exchanges substances), restated for oxygen (B5). The stem's „lungs do not work“ leaves one option. |
| 012 | **REVISE** | medium | Fact correct (OpenStax 28.2: bloodstreams never mix; blood cells cannot cross). The stem already says „не се смесват“, which eliminates A and C. Remove that clause so non-mixing is tested. |
| 013 | KEEP | medium | Placenta (exchange) vs umbilical cord (vessels to the placenta); second fact added, compare tag. Correct. |
| 014 | **DELETE** | medium | Same label as 010 (placental exchange) restated for wastes and CO₂; labelled recall yet rated medium. |
| 015 | KEEP | easy | Uterine contractions push the fetus through the birth canal (OpenStax 28.5). |
| 016 | KEEP | easy | First breath inflates the lungs (OpenStax 28.5, verified). |
| 017 | KEEP | medium | Newborn heat loss from higher surface-to-volume ratio (OpenStax 28.5, verified). Only D5 item; scope edge noted by collector. Keep, but check against the programme's post-embryonic expected results before approval. |
| 018 | KEEP | medium → **easy** | Alcohol crosses the placenta (NHS, OpenStax 28.2, verified). Downgrade: one fact. |
| 019 | KEEP | medium → **easy** | Fetal liver not fully developed, cannot process alcohol (NHS, verified). Distinct fact from 018. Downgrade. |
| 020 | KEEP | easy | FASD: long-term learning, behaviour and development problems (NHS, WHO). |
| 021 | **DELETE** | medium | Same label as 022 (no safe amount of alcohol): all three distractors are rejected by that fact alone; the „talk to a midwife“ part is never discriminated. |
| 022 | **REVISE** | medium → **easy** | Correct (CDC). Key 97 chars vs ≤61 is a length cue; balance options. Downgrade: one principle (no safe amount, time or type). |
| 023 | KEEP | medium → **easy** | Smoking restricts oxygen to the baby (NHS, verified). Downgrade: one fact. |
| 024 | KEEP | medium → **easy** | Preterm birth and low birth weight (NHS, verified). Labelled recall but rated medium; downgrade. |
| 025 | KEEP | medium → **easy** | Second-hand smoke can reduce birth weight (NHS, verified). Downgrade: distractors are absolute statements. |
| 026 | KEEP | medium → **easy** | Stopping even in the last weeks helps (NHS, verified). Low-tar distractor is a real misconception. Downgrade. |
| 027 | **DELETE** | medium | After-birth bronchitis/pneumonia risk is infant respiratory health, not a risk to development (D6); overlaps the G8 respiratory smoking items and repeats 025's passive-smoking label. |

### 3.3 S — G9 supramolecular complexes (32)

Ids are as in the file (two digits, non-contiguous).

| ID | Decision | Collector → recommended difficulty | Reason / action |
|---|---|---|---|
| 01 | KEEP | easy | Ribosome = RNA + proteins (OpenStax 4.3). Scientifically correct; confirm ribosome is a programme example of a supramolecular complex before approval (collector could not check). |
| 02 | KEEP | easy | Chromatin = DNA + proteins (OpenStax 4.3). Same programme check as 01. |
| 03 | KEEP | medium | Virus, ribosome and chromosome share nucleic acid + protein. Integrates three facts; good D1 item. |
| 04 | **REVISE** | easy | Cued: the stem says „а не отделна молекула“ and three options contain „молекула“. No item defines a supramolecular complex, so re-target to the definition (several macromolecules joined into one functional structure) with plausible wrong definitions. |
| 05 | KEEP | easy | Virion = nucleic acid + capsid (OpenStax 21.1, verified). |
| 06 | KEEP | easy | Envelope derived from the host membrane (OpenStax 21.1, verified). |
| 07 | **REVISE** | easy | Key says viruses lack „мембрана“, which contradicts 06 (enveloped viruses carry a lipid membrane). OpenStax 21.1 says they lack the plasma membrane, organelles and ribosomes. Change to „нямат собствена плазмена мембрана, цитоплазма, органели и рибозоми“ and align the explanation. |
| 08 | KEEP | medium | Cells use DNA; viruses DNA or RNA (OpenStax 21.1, verified). Two-fact comparison. |
| 09 | KEEP | medium | Host-cell specificity from receptors (OpenStax 21.2, verified). |
| 11 | KEEP | medium | Attachment → penetration → synthesis → assembly → release (OpenStax 21.2). Uncoating omitted deliberately; 18 covers it. |
| 12 | **DELETE** | medium | Blocked receptors stop attachment: same fact as 09 and as sample 3 of 20, which is the stronger integration item. |
| 13 | KEEP | medium | Virus supplies only the genetic information; the host supplies ribosomes, organelles and energy (OpenStax 21.1/21.2). |
| 14 | KEEP | easy | Phage nucleic acid enters naked, capsid stays outside (OpenStax 21.2, verified). |
| 15 | **REVISE** | medium | Fact correct (OpenStax 21.2: budding, cell not immediately killed). The stem clause „и не я разрушават веднага“ is restated by key D. Remove the clause from the stem. |
| 18 | KEEP | medium → **easy** | Capsid breakdown releases the nucleic acid (OpenStax 21.2). Downgrade: one step. |
| 20 | KEEP | hard | Three-sample experiment needing two independent requirements (living cell; matching receptor). Genuine two-step discrimination. |
| 21 | **DELETE** | easy | Same label as 23 (flu spreads by droplets); 31 also tests the flu route. |
| 22 | KEEP | easy | Flu signs (WHO, verified); distractors are hepatitis, measles and HIV signs. |
| 23 | KEEP | medium | Covering coughs works because flu spreads by droplets (WHO, CDC). Links route to a prevention measure; kept as the flu-route item. |
| 24 | KEEP | easy | Measles: fever, cough, runny nose, red watery eyes, then rash starting on the face and spreading down (WHO measles, verified). |
| 25 | **DELETE** | easy | Hepatitis A route is re-tested by 41 (whose stem points straight to the food/water option) and grouped again in 31. |
| 27 | KEEP | medium → **easy** | Hepatitis B via shared needles, tattooing and piercing (WHO, verified). Downgrade: B and D are the same unsafe practice. |
| 28 | KEEP | easy | HIV not spread by kisses, hugs, sharing food (WHO, verified). Capitalised НЕ is correct use. |
| 29 | KEEP | medium | HIV targets white blood cells, weakening defence (WHO, verified). Links two facts. |
| 30 | KEEP | medium → **easy** | Symptoms from cell damage and the body's response (OpenStax 21.2, verified). Downgrade: one statement. |
| 31 | KEEP | medium | Grouping diseases by route; needs the routes of four diseases. |
| 34 | KEEP | easy | Vaccine prepares the body in advance without causing disease (CDC, WHO). |
| 36 | KEEP | medium | Yearly flu vaccine because surface molecules change; measles vaccine stable (OpenStax 21.3, verified). |
| 37 | KEEP | medium | Birth dose because transmission at birth and infant infection often becomes chronic (WHO hep B, verified; WHO says within 24 hours, so „първите часове“ is acceptable). |
| 38 | KEEP | medium | Community protection for someone who cannot be vaccinated (WHO). Prophylaxis level, no immunology. |
| 39 | KEEP | medium → **easy** | Antibiotics do not work on viruses (CDC). Good misconception distractors. Downgrade: one fact, diagnosis given in the stem. |
| 41 | KEEP | hard → **medium** | Hepatitis A recognition, route and prevention (WHO, verified). Downgrade from hard: the stem's food/water exposure selects the only food/water option in one step. |

## 4. Duplicate and redundancy findings

Within each dataset, sorted by `research.concept` and read side by side. Plan
rule B5 (one named fact, one candidate; reverse, "why" and scenario forms of the
same fact are the same label) was applied.

| Dataset | Cluster | Items | Resolution |
|---|---|---|---|
| X | Urinary path and organ links | 002, 003 | Keep 002; delete 003 (002's distractors already swap ureter and urethra). |
| X | "Each kidney has its own ureter" | 004, 014 | Keep 014 (applies the fact); delete 004 (count trivia). |
| X | Tubular reabsorption of a needed substance | 009, 011, 012, 013, 017 | Keep 009 (roles), 011 (order), 012 (water), 017 (glucose in diabetes); delete 013 (glucose re-angle of 012 whose explanation repeats 017). |
| X | "Drink enough water" as prevention | 023, 027 | Keep 023 for UTI; revise 027 so salt is the discriminating fact. |
| X | Chronic kidney disease | 028, 029, 030, 034 | Keep 028 (causes), 029 (silent early), 030 (prevention); delete 034 (treatment detail). |
| X | Glomerulus retains proteins and cells | 008, 015, 018 | Kept: normal function (008), pathology reading (015), normal urine composition (018) need different second facts. |
| E | Placenta exchanges substances | 010, 011, 014 (+013, 018) | Keep 010; delete 011 (oxygen) and 014 (wastes). 013 adds the cord (compare), 018 adds alcohol crossing; both kept. |
| E | Implantation | 007, 008 | Both kept: term (007) vs consequence of failure (008, easy). |
| E | Embryo → fetus order | 005, 006 | Both kept; 006's distractors A and D overlap 005's order, acceptable. |
| E | No safe alcohol | 021, 022 | Keep 022 (revised for length); delete 021 (all its distractors fall to 022's fact). |
| E | Passive smoking | 025, 027 | Keep 025 (pregnancy); delete 027 (after birth, respiratory). |
| E | Alcohol and smoking overall | 018–027 | 10 of 27 raw items, 8 of 23 survivors. Over-represented relative to the rest of D6 (section 6). |
| S | Attachment needs a receptor | 09, 12, 20 | Keep 09 (specificity) and 20 (integration); delete 12. |
| S | Flu spreads by droplets | 21, 23, 31 | Keep 23 (route → measure) and 31 (grouping); delete 21. |
| S | Hepatitis A by food and water | 25, 31, 41 | Keep 41 (signs – route – prevention) and 31; delete 25. |
| S | Virus lacks ribosomes and metabolism | 07, 13, 20 | All kept: why not a cell (07), what the virus supplies (13), conditions for replication (20). |

Re-angled duplicate share (plan K topic-level stop: more than one fifth): X
3/34 (9 %), E 4/27 (15 %), S 3/32 (9 %). Not triggered.

Cross-dataset: no semantic duplicate between X and E. X has no sweat or skin
item, so it does not collide with Wave 1 skin items 11/15/22; the Wave 1
cell-and-tissues item on kidneys and bladder as one system is a different fact
from X-001/002. S has no overlap with the Wave 1 chemical-composition bank (no
nucleic-acid or ribosome items there). E-023 (smoking limits oxygen to the
fetus) will need a `--compare` against G8 respiratory when that topic is
collected.

## 5. Scientific and source findings

No key is wrong. The findings below need correction before approval.

| Id | Finding | Evidence |
|---|---|---|
| S-07 | Key says viruses lack „мембрана“. That contradicts S-06 (enveloped viruses carry a lipid membrane) and is not what the source says: viruses lack organelles, ribosomes and **the plasma membrane**, while an envelope is derived from host membranes. | OpenStax *Biology 2e* 21.1 |
| X-005 | Distractor D („в горната част на корема, под стомаха“) is not unambiguously wrong: the kidneys do lie in the upper abdomen, behind and partly below the stomach. | NIDDK *Your Kidneys & How They Work* ("just below the rib cage, one on each side of your spine") |
| X-033 | „киселината, образувана от клетките“ is a literal translation of NIDDK's lay sentence. It is true but not school terminology and invites confusion with пикочна киселина. Use end products of protein breakdown (урея) and excess salts. | NIDDK kidneys page; OpenStax *A&P 2e* 25.1 (urea from protein breakdown) |
| X-034 | Dialysis is treatment, beyond the appendix §4 limit „no clinical detail beyond cause – signs – prevention“. Deleted. | Plan appendix §4 |
| E-027 | Postnatal bronchitis and pneumonia risk is correct (NHS) but is infant respiratory health, not a risk to development. Deleted. | NHS *Stop smoking in pregnancy* |
| E-012 | Correct fact, but the stem says the bloods do not mix, which eliminates two distractors. | OpenStax *A&P 2e* 28.2 ("maternal and fetal bloodstreams never mix directly"; "blood cells cannot move across the placenta") |
| E-007, X-031 | Unnecessary numbers in explanations: "end of the first week" (plan D: NO week-by-week milestones) and "about 4 cm" (B7). Both true per OpenStax 28.2 / 25.2; remove them. | Plan B7, D |
| S-37 | Stem says the birth dose is given „в първите часове“. WHO says "as soon as possible after birth (within 24 hours)". Acceptable as worded; do not add a number. | WHO hepatitis B fact sheet |
| E-017 | Newborn heat loss via surface-to-volume ratio is correct, but its place in the G8 programme could not be confirmed (collector flagged it too). Kept pending the programme check in section 8. | OpenStax *A&P 2e* 28.5 |
| S-01, S-02, S-03 | Ribosome = RNA + protein and chromatin = DNA + protein are correct. The tracked materials define D1 only as "definition and examples", so whether these are the programme's examples is unverified (mon.bg not readable). Kept pending the programme check. | OpenStax *Biology 2e* 4.3; plan D.3 |

**Claims verified by this review** (pages fetched in this session on
2026-10-06, full text read, not search snippets):

- NIDDK *Your Kidneys & How They Work*: location; nephron = glomerulus +
  tubule; thin glomerular walls pass small molecules and water, proteins and
  cells stay; tubule returns needed substances; ureters, bladder storage; acid,
  water, salt and mineral balance (X-001, 002, 005–012, 032, 033).
- NIDDK *Bladder Infection (UTI) in Adults*: bacteria as the usual cause;
  untreated infection can reach the kidneys; burning, frequent urges, cloudy
  urine; liquids and loose clothing (X-016, 021–023, 025).
- NHS *Kidney stones*: pain starting in the side or lower back that comes and
  goes; cloudy pee and needing to pee often; build-up of chemicals; prevention
  by enough water, not too much salt, dark pee means dehydration (X-019,
  025–027).
- NHS *Kidney infection*: blood in pee warrants urgent medical advice; do not
  hold pee in; empty the bladder fully (X-020, 024).
- NHS *Chronic kidney disease*: no early symptoms, found by blood or urine
  tests (X-029). NIDDK CKD causes as cited by the collector were not
  re-fetched; NHS confirms the same causes.
- OpenStax *A&P 2e* 25.1: traces of protein only, excess usually means
  glomerular damage; cells not normally present, leukocytes suggest UTI;
  glucose in urine in diabetes mellitus; urochrome; urea (X-013, 015–018, 033).
  25.2: short female urethra explains more UTIs (X-031).
- OpenStax *A&P 2e* 28.1, 28.2, 28.5: zygote with half the genetic material
  from each parent; fertilisation in the uterine tube because the oocyte cannot
  survive the journey; implantation; amniotic fluid protects from trauma and
  temperature change; placenta exchange without mixing; placenta permeable to
  alcohol and nicotine; contractions; first breath; newborn surface-to-volume
  heat loss (E-001–017).
- NHS *Drinking alcohol while pregnant*: alcohol crosses the placenta; the
  baby's liver is not fully developed; FASD (E-018–020). NHS *Stop smoking in
  pregnancy*: restricted oxygen; preterm birth and low birth weight; stopping
  even in the last weeks helps; second-hand smoke reduces birth weight;
  bronchitis and pneumonia after birth (E-023–027). CDC alcohol page (E-022)
  was not re-fetched; the claim matches NHS.
- OpenStax *Biology 2e* 21.1, 21.2, 21.3: noncellular; lack organelles,
  ribosomes and plasma membrane; capsid; host-derived envelope; DNA or RNA
  genome; receptor specificity; attachment, penetration, uncoating,
  replication, assembly, release; phage nucleic acid enters naked; budding
  without immediate cell death; symptoms from cell damage and immune response;
  yearly flu vaccine vs stable measles vaccine (S-03, 05–09, 11–20, 30, 36).
- WHO fact sheets: influenza (droplets, sudden fever, dry cough, muscle and
  joint pain, tissue and handwashing), measles (fever, cough, runny nose, red
  watery eyes, rash from the face downward, respiratory spread), hepatitis A
  (faecal–oral, dark urine, jaundice, safe water and hygiene), hepatitis B
  (mother to child at birth, needles, tattooing, piercing; infant infection
  usually chronic; vaccine within 24 hours of birth), HIV (white blood cells;
  blood, breast milk, semen, vaginal fluids, mother to baby; not kisses, hugs or
  shared food) (S-21–31, 37, 41).

Not re-fetched (collector citations judged sufficient and consistent with the
pages above): CDC flu, CDC vaccine basics, WHO *How do vaccines work*, CDC
antibiotic use, WHO alcohol fact sheet, NIDDK CKD. mon.bg and NCBI were not
used, as instructed; their unavailability is not a review failure.

**Source vs content.** The collectors' sources are adequate for every kept
claim. Source weakness appears only where the programme text itself is needed
(S-01–03, E-017, Bulgarian terminology for urine formation); those are scope
checks, not content errors. One NHS page (alcohol in pregnancy) is past its
next-review date (13 March 2026); its claims match CDC and WHO, so no change.

## 6. Coverage findings

Dimensions from plan D.2/D.3 and the appendix coverage matrix. Counts are
survivors after this review.

**X G8 excretory (30).** D1 organs and order 3; D2 kidney 5; D3 urine
formation 3 (011, 012, 014); D4 urine composition 6; D5 diseases 11; D6 blood
and water balance 2 (032, 033 after revision).

- Over-represented: D5 diseases (UTI 5 items, stones 3, CKD 3) relative to D6.
- Thin or missing:
  - kidney gross structure at school level (cortex, medulla, renal pelvis):
    none; partly image-dependent, so only text-describable items are possible;
  - substances removed in urine (урея, salts): none until X-033 is revised;
  - excretion as a process across organs (lungs remove CO₂, skin sweat, kidneys
    urine): none; the Wave 1 skin bank covers only skin vs kidneys;
  - D6 prediction items (more water drunk or more sweating → urine volume and
    colour): none, and this is the natural source of the missing reasoning
    items;
  - Bulgarian school terminology for urine formation (първична урина /
    вторична урина, филтрация / обратно всмукване) is absent; the items use
    NIDDK lay terms. Whether the programme lists these terms as new concepts
    must be checked (section 8).

**E G8 embryonic and postembryonic (23).** D1 fertilisation 3; D2 embryonic
stages 6; D3 placenta and cord 3; D4 birth 2; D5 post-embryonic periods 1; D6
risks 8.

- **Life-stage concern confirmed and material.** The topic title is half
  „следзародишно развитие“, and the appendix lists post-embryonic periods and
  their features as a main dimension („Main stages of embryonic and
  post-embryonic human development … responsibility for offspring“). The bank
  has one D5 item (E-017, newborn heat loss), no item on the sequence or
  features of the post-natal periods (infancy, childhood, adolescence and
  puberty, adulthood, ageing), none on growth after birth, and none on
  responsibility for offspring. The collector's explanation (8-page budget
  spent on alcohol and smoking pages) is accurate; the budget, not the
  material, caused the gap.
- **D6 is narrow.** All 8 survivors are alcohol or smoking. Infections in
  pregnancy, medicines taken without medical advice and drugs, all named in
  plan F ("risks in pregnancy (alcohol, smoking, infections)"), have no item.
- D4 birth has 2 items; adequate for a narrow topic only if D5 is filled.
- Stage-order items are not over-repeated (004, 005, 006 test different
  orders or criteria).

**S G9 supramolecular complexes (29).** D1 definition and examples 4; D2 virus
structure, why not a cell 5; D3 reproduction 6; D4 viral diseases 8; D5
prophylaxis 6.

- No item asks for the definition of a supramolecular complex; the revised
  S-04 should supply it.
- Virus forms are thin: no plant virus example and no bacteriophage structure
  (head, tail) item; S-14 covers phage entry only. Lysis as a release mode is
  mentioned only in explanations.
- Diseases cover influenza, measles, hepatitis A and B and HIV. That is
  adequate; the plan warns against repeating prevention facts across more
  viruses.
- Vaccine items stay at prophylaxis level (no antibodies or memory cells), as
  the plan's G8 ownership of immunity requires. S-38 (community protection)
  is at that boundary and acceptable.
- Differences between viruses and cellular organisms are well covered (07,
  08, 13, 20, 39).

## 7. Process deviations

Recorded as process facts. None is treated as a content defect, and no
candidate was deleted for them.

| Dataset | Deviation | Effect on content |
|---|---|---|
| X | 15 source pages were read against the 8-page budget. The file records `pages_inspected: 8` and cites 8 pages, so the recorded value understates what was read. | None found: every kept claim traces to a cited page that this review re-fetched. |
| X | Every source's `retrieved_at` (07:12:00Z) is later than the run's `finished_at` (07:09:21Z). | Provenance timestamps are inconsistent; content unaffected. |
| S | 14 source pages were read against the 8-page budget. The run was restarted with `--max-pages 14`, so `limits.max_pages` now records 14, not the prescribed 8. | None found; 13 cited pages, claims verified (section 5). |
| S | `started_at`/`finished_at` span one second (restart artefact), and sources are dated before `started_at`. Candidate ids run 01–41 with nine gaps (10, 16, 17, 19, 26, 32, 33, 35, 40), i.e. drafts dropped before the final file. | Traceability only. |
| E | Stayed within 8 counted pages, plus three probe fetches it did not count (NHS 404, empty OpenStax 28.4, CDC index). `finished_at` (07:40:00Z) is later than the collector's own final report (07:10Z). | The budget choice caused the D5/D6 gaps (section 6); timestamps are inconsistent. |
| All | Curriculum scope taken from the tracked structure review and plan, not the МОН PDF (mon.bg blocked), as instructed. | Three scope questions remain open (section 8). |

## 8. Next actions required before approval

1. **Correction pass** (plan section I: deterministic correction, Sonnet 5.5
   Medium), from this review only:
   - apply the 15 REVISE actions exactly as stated in section 3 (X-005, 015,
     017, 025, 026, 027, 031, 032, 033; E-007, 012, 022; S-04, 07, 15);
   - remove the 11 DELETE items (X-003, 004, 013, 034; E-011, 014, 021, 027;
     S-12, 21, 25);
   - set every surviving item's `difficulty` to the recommended value (24
     changes) and retag as `recall` the 21 survivors reviewed as single-fact
     that carry a reasoning tag: X-008, 017, 019, 020, 023, 024, 027, 029, 030;
     E-002, 008, 018, 019, 022, 023, 025, 026; S-18, 27, 30, 39;
   - cite the source for each revised claim in `research.notes` (X-027 NHS
     stones on salt; X-033 OpenStax 25.1 on urea).
2. **Post-correction verification** against this review, then local
   validation with `--db` and the G8 `--compare` pairs from the execution
   sheet (X vs E; the digestive file stays untouched until CP1).
3. **Programme check before approval** (the owner can open mon.bg in a
   browser; tracked materials do not settle these):
   - G9 theme 2 examples: are ribosomes and chromatin/chromosomes the
     programme's examples of supramolecular complexes? If not, delete S-01 and
     S-02 and revise S-03.
   - G8 4.2 expected results for post-embryonic development: confirm the
     periods to be named, and whether newborn thermoregulation (E-017) is in
     scope.
   - G8 2.3 new concepts: if „първична урина“ / „вторична урина“ (or
     филтрация / обратно всмукване) are listed, the correction pass should
     use them in X-009, 011 and 012.
4. **Owner decision on the embryonic gap** (blocks marking the topic
   COMPLETE, not the approval of the 23 corrected items). R2 recommends one
   bounded supplement run of at most 12 raw, Sonnet 5.5 Medium, its own 8-page
   budget, missions D5 (post-natal periods in order and their features,
   puberty, growth, responsibility for offspring) and D6 (infections, medicines
   and drugs in pregnancy, from WHO/NHS/CDC pages), with `--compare` against
   this file. It needs a plan amendment, so it is not started here.
5. **Reasoning floors.** Excretory (27 %) and embryonic (26 %) are below
   their floors after recalibration. Either record a per-topic justification
   in plan D (B6 permits it) or include 3–4 D6 prediction items (water intake,
   sweating → urine volume and colour) in a supplement for excretory. The
   embryonic supplement in step 4 should aim its sequence and compare items at
   D5.
6. **Provenance.** At export, record the true page counts (X 15, S 14
   against a budget of 8) and note the inconsistent run and source timestamps
   in the provenance file, rather than editing the candidate files'
   `run` blocks.
