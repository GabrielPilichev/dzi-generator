# Physics, Grade 10 — question bank independent review

Review of the 13 pending run-1 candidate datasets under
`data/question_candidates/physics/grade-10/`. Prepared on 2026-10-05 by Claude
Opus 5.5 (collector of all 13 datasets, per run metadata: `claude-sonnet-5-5-collector`).
**Review only:** no candidate file, database or other file was changed; nothing
was approved, imported or exported. This document is the correction
specification for a different agent.

## 1. Scope and method

- Scope authority: МОН programme for X клас (общообразователна подготовка), as
  recorded in `data/subject_structures/physics/grade-10.json` and the appendix of
  `docs/reviews/physics_g10_curriculum_structure_review.md`. A question belongs
  to a topic only if it tests one of that topic's listed results at that level.
- All 311 candidates were read in full (prompt, four options, key, explanation,
  difficulty). Collector notes were not used as evidence.
- Every numerical item was recomputed by script (formula, substitution, units,
  rounding, origin of distractors); every direction item was re-derived with
  v × B / right-hand rule and the sign of the charge; every nuclear equation was
  checked for A and Z conservation.
- All 311 prompts + keys were compared pairwise across topics (token overlap and
  sequence similarity), and every pair above threshold was read.
- Key-length cue: an option was treated as a cue when the key is the strictly
  longest option and at least 1,3 times the next longest (text options only).
- IDs: short form `XX-NN` = topic code + candidate number. Full ID =
  `physics-g10-<topic>-run-1-<number>`; **electric-field and
  wave-properties-of-light use two digits (`-01`), all other topics three
  (`-001`)**. Full IDs are listed in sections 9–12.

| Code | Topic slug (`physics-g10-…`) |
|---|---|
| EF | electric-field |
| MF | magnetic-field |
| CP | charged-particle-motion |
| IN | electromagnetic-induction-alternating-current |
| EW | electromagnetic-waves |
| LP | light-propagation |
| WP | wave-properties-of-light |
| QL | quantum-properties-of-light |
| AT | atoms-and-atomic-transitions |
| NU | atomic-nucleus |
| NR | nuclear-reactions |
| PA | particles |
| SG | stars-galaxies-universe |

## 2. Material external sources

None. No web lookup was made and no external source changed a decision. Facts
and constants were checked against standard reference values known to the
reviewer (e.g. T_Curie(Fe) ≈ 770 °C; He lines 447/502/668 nm; binding energies
⁴He 28,3 MeV, ¹⁶O 127,6 MeV, ⁷Li 5,6 MeV/нуклон, maximum ≈ 8,8 MeV/нуклон near
A ≈ 56–62; D+T → 17,6 MeV; pp-chain 4p → ⁴He + 2e⁺ + 2ν; hydrogen levels
−13,6/n² eV; T_CMB ≈ 2,7 K; age ≈ 13,8 Gyr; quark content and charges). All agree
with the candidates. See caveat 1 in section 19.

## 3. Raw-bank summary

13 datasets, **311** candidates, all `multiple_choice`, all `pending`, four options A–D each, no image/asset fields.
Raw difficulty: easy 127 | medium 156 | hard 28. Raw keys: A 77 | B 79 | C 82 | D 73.

| Topic | Raw | Keep | Revise | Delete | Move out | Move in | Final |
|---|---|---|---|---|---|---|---|
| EF | 24 | 16 | 6 | 2 | 0 | 0 | 22 |
| MF | 24 | 19 | 4 | 1 | 0 | 1 | 24 |
| CP | 23 | 17 | 4 | 1 | 1 | 0 | 21 |
| IN | 26 | 17 | 9 | 0 | 0 | 0 | 26 |
| EW | 22 | 11 | 8 | 3 | 0 | 0 | 19 |
| LP | 25 | 18 | 6 | 1 | 0 | 0 | 24 |
| WP | 23 | 22 | 0 | 1 | 0 | 0 | 22 |
| QL | 23 | 21 | 2 | 0 | 0 | 0 | 23 |
| AT | 25 | 20 | 3 | 2 | 0 | 0 | 23 |
| NU | 24 | 13 | 10 | 1 | 0 | 0 | 23 |
| NR | 24 | 21 | 2 | 1 | 0 | 0 | 23 |
| PA | 22 | 19 | 2 | 1 | 0 | 0 | 21 |
| SG | 26 | 23 | 2 | 1 | 0 | 0 | 25 |
| **Total** | **311** | **237** | **58** | **15** | **1** | **1** | **296** |

**No wrong answer key and no wrong number was found.** No candidate changes its
correct letter and no options are reordered. Three scientific/ambiguity fixes are
required (section 5).

## 4. Candidate-level specification

KEEP = unchanged. For REVISE, `CONTENT REVISION` replaces the named field(s)
with exactly the text given; every field not named stays byte-for-byte. Final
key and final difficulty are stated for every non-KEEP survivor. `review.*`
fields are not to be touched by the correction agent.

### EF — electric-field (raw 24 → final 22)

KEEP: 01, 02, 04, 05, 06, 08, 09, 11, 12, 13, 16, 17, 18, 20, 21, 24

- **EF-03 — REVISE, CONTENT REVISION.** Final key **C**, final difficulty **easy** (unchanged).
  - `options.C` — Key is conspicuously the longest / most detailed option (length cue).
    New value: «всеки заряд създава електрично поле, което действа със сила на другия заряд»
- **EF-07 — REVISE, DIFFICULTY-ONLY:** medium → **easy**. Same one-step inverse-square proportion as EF-01 (easy), applied to E. Final key **A**.
- **EF-10 — DELETE.** Within-topic redundancy: direction of the force on a negative charge is fully re-assessed by EF-11 (magnitude + direction) and reused in CP-06.
- **EF-14 — REVISE, DIFFICULTY-ONLY:** medium → **easy**. One-step W = qφ with one prefix conversion (same level as EF-04). Final key **B**.
- **EF-15 — REVISE, DIFFICULTY-ONLY:** medium → **easy**. One-step E = U/d with cm→m. Final key **D**.
- **EF-19 — REVISE, DIFFICULTY-ONLY:** medium → **easy**. One-step C = q/U; prefixes cancel. Final key **D**.
- **EF-22 — REVISE, CONTENT REVISION.** Final key **D**, final difficulty **medium** (unchanged).
  - `options.D` — Key is conspicuously the longest / most detailed option (length cue).
    New value: «Хартията се поляризира и ближната ѝ страна се привлича по-силно, отколкото далечната се отблъсква»
- **EF-23 — DELETE.** Scope: zero-field point between two charges needs superposition of two fields and solving a ratio equation; the programme limits 1.1 to the field of one point charge and Coulomb's law "само за два точкови заряда". Not in the new-concept list.

### MF — magnetic-field (raw 24 → final 24)

KEEP: 01, 02, 03, 04, 06, 07, 08, 09, 11, 12, 13, 15, 17, 18, 19, 20, 21, 23, 24

- **MF-05 — REVISE, DIFFICULTY-ONLY:** medium → **easy**. Qualitative recall of two dependences. Final key **A**.
- **MF-10 — REVISE, CONTENT REVISION.** Final key **C**, difficulty medium → **easy**.
  - `options.C` — Key is conspicuously the longest / most detailed option (length cue).
    New value: «Като отношението на максималната магнитна сила върху движещ се пробен заряд към произведението qv: B = Fmax/(qv)»
- **MF-14 — REVISE, CONTENT REVISION.** Final key **B**, final difficulty **medium** (unchanged).
  - `prompt` — The bracketed sentence tells the pupil the method and the sign-reversal step, which is what the item assesses.
    New value: «Електрон се движи надясно в равнината на чертежа. Еднородно магнитно поле е перпендикулярно на чертежа и насочено от наблюдателя към чертежа. Накъде е насочена магнитната сила, действаща на електрона?»
- **MF-16 — DELETE.** Within-topic redundancy: B = F/(Iℓ) with cm→m is the same computation as MF-17 (data-table version); MF-15 and MF-18 already cover F = IℓB.
- **MF-22 — REVISE, CONTENT REVISION.** Final key **B**, final difficulty **medium** (unchanged).
  - `options.B` — Key is conspicuously the longest / most detailed option (length cue).
    New value: «Сменя посоката на тока в рамката на всеки половин оборот»

### CP — charged-particle-motion (raw 23 → final 21)

KEEP: 01, 02, 05, 06, 07, 08, 09, 10, 11, 12, 13, 16, 18, 19, 20, 22, 23

- **CP-03 — REVISE, CONTENT REVISION.** Final key **A**, final difficulty **easy** (unchanged).
  - `options.A` — Key is conspicuously the longest / most detailed option (length cue).
    New value: «Не се променя, защото магнитната сила не извършва работа»
- **CP-04 — REVISE, CONTENT REVISION.** Final key **D**, final difficulty **easy** (unchanged).
  - `options.D` — Only the key carries a justification („защото …“) and it is the longest option.
    New value: «Праволинейно и равномерно, без да се отклонява»
- **CP-14 — MOVE → `physics-g10-magnetic-field`.** Direction of the magnetic force on a positive charge (left-hand rule) is the 1.2 result „определя … посоката на максималната магнитна сила върху движеща се заредена частица“. Change `topic_slug` to `physics-g10-magnetic-field` (section_slug unchanged: `physics-g10-electromagnetic-phenomena`). No text change. It complements MF-14 (electron) as the positive-charge base case. Final key **A**, final difficulty **medium**.
- **CP-15 — DELETE.** Cross-topic: direction of the magnetic force on an electron (left-hand rule + sign reversal) is a 1.2 result and duplicates MF-14 / MF-19.
- **CP-17 — REVISE, CONTENT REVISION.** Final key **D**, final difficulty **medium** (unchanged).
  - `prompt` — Unnatural Bulgarian („Какво е сравнението на радиусите…“).
    New value: «Две еднакви частици с еднакъв заряд навлизат в еднородно магнитно поле перпендикулярно на индукционните линии, но с различни скорости. Как се отнасят радиусите на окръжностите, по които се движат частиците?»
- **CP-21 — REVISE, CONTENT REVISION.** Final key **A**, final difficulty **easy** (unchanged).
  - `options.A` — Key is conspicuously the longest / most detailed option (length cue).
    New value: «То отклонява голяма част от заредените космически частици»

### IN — electromagnetic-induction-alternating-current (raw 26 → final 26)

KEEP: 02, 03, 04, 05, 06, 07, 08, 09, 10, 13, 17, 18, 19, 20, 21, 22, 25

- **IN-01 — REVISE, CONTENT REVISION.** Final key **C**, final difficulty **easy** (unchanged).
  - `options.C` — Key is conspicuously the longest / most detailed option (length cue).
    New value: «Възникване на ЕДН в проводящ контур при промяна на магнитното поле, което го пронизва»
- **IN-11 — REVISE, CONTENT REVISION.** Final key **A**, final difficulty **medium** (unchanged).
  - `options.A` — Key is conspicuously the longest / most detailed option (length cue).
    New value: «Тя е равна на постоянния ток, който отделя в същия резистор същата топлина за същото време»
- **IN-12 — REVISE, DIFFICULTY-ONLY:** medium → **easy**. One-step Umax = U·√2. Final key **B**.
- **IN-14 — REVISE, DIFFICULTY-ONLY:** medium → **easy**. One-step I = P/U with effective values. Final key **C**.
- **IN-15 — REVISE, CONTENT REVISION.** Final key **C**, final difficulty **medium** (unchanged).
  - `options.C` — Key is conspicuously the longest / most detailed option (length cue).
    New value: «Мощността I²R не става отрицателна при никоя посока на тока, затова средната ѝ стойност не е нула»
- **IN-16 — REVISE, CONTENT REVISION.** Final key **A**, final difficulty **easy** (unchanged).
  - `options.A` — Key is conspicuously the longest / most detailed option (length cue).
    New value: «Постоянният ток създава магнитно поле, което не се променя с времето»
- **IN-23 — REVISE, CONTENT REVISION.** Final key **C**, final difficulty **medium** (unchanged).
  - `options.B` — SCIENTIFIC/AMBIGUITY: for this step-down transformer (N1 = 200, N2 = 100) „I2 > I1“ is a true statement, so the item had a true distractor that is merely not the cause.
    New value: «Вторичната намотка има повече навивки от първичната»
  - `options.C` — Key is conspicuously the longest / most detailed option (length cue). (ratio 2,0)
    New value: «В намотките и в сърцевината на реалния трансформатор има загуби на енергия»
- **IN-24 — REVISE, DIFFICULTY-ONLY:** hard → **medium**. Two-step proportion (U2 ∝ N2, P ∝ U²); not multi-concept hard. Final key **D**.
- **IN-26 — REVISE, DIFFICULTY-ONLY:** hard → **medium**. Direct qualitative Faraday law; the option set makes D the only choice with rising amplitude and doubled frequency. Final key **D**.

### EW — electromagnetic-waves (raw 22 → final 19)

KEEP: 02, 03, 05, 06, 07, 10, 14, 15, 16, 17, 20

- **EW-01 — REVISE, CONTENT REVISION.** Final key **A**, final difficulty **easy** (unchanged).
  - `options.A` — Key is conspicuously the longest / most detailed option (length cue).
    New value: «Променливо електрично поле поражда променливо магнитно поле и обратно, и така вълната се разпространява»
- **EW-04 — REVISE, CONTENT REVISION.** Final key **B**, final difficulty **easy** (unchanged).
  - `options.B` — Key is conspicuously the longest / most detailed option (length cue).
    New value: «Скоростите са равни, защото във вакуум не зависят от честотата»
- **EW-08 — DELETE.** Answer cue: the stem asks for a source of „рентгенови лъчи“ and only the key contains „Рентгенова тръба“; source matching is covered by EW-09 and EW-13, X-ray production by AT-21.
- **EW-09 — REVISE, CONTENT REVISION.** Final key **A**, final difficulty **easy** (unchanged).
  - `options.A` — Key is 2,5 times longer than any distractor and the only one with a numeric detail.
    New value: «Нагрят радиатор в стая»
- **EW-11 — REVISE, CONTENT REVISION.** Final key **D**, final difficulty **easy** (unchanged).
  - `options.D` — Key is conspicuously the longest / most detailed option (length cue).
    New value: «Микровълните се поглъщат най-вече от водните молекули в храната и я нагряват»
- **EW-12 — REVISE, CONTENT REVISION.** Final key **A**, final difficulty **easy** (unchanged).
  - `options.A` — Key is conspicuously the longest / most detailed option (length cue).
    New value: «Измерва времето между излъчването на импулса и приемането на отразения сигнал»
- **EW-13 — REVISE, DIFFICULTY-ONLY:** medium → **easy**. Recognition of one out-of-band device. Final key **C**.
- **EW-18 — DELETE.** Changed-values duplicate of EW-16 (λ = c/ν with a prefix conversion); EW-17 and EW-19 cover the inverse and period forms.
- **EW-19 — REVISE, DIFFICULTY-ONLY:** medium → **easy**. One-step λ = cT with one prefix conversion. Final key **D**.
- **EW-21 — DELETE.** Scope + cross-topic duplicate: wavelength change on entering a slower medium is a 2.1 result (1.5 covers c = λν in vacuum only) and repeats the reasoning of LP-06 / LP-22.
- **EW-22 — REVISE, DIFFICULTY-ONLY:** hard → **medium**. One-step ratio λ ∝ v at equal frequency; large exponents are arithmetic, not reasoning. Final key **D**.

### LP — light-propagation (raw 25 → final 24)

KEEP: 01, 02, 03, 04, 05, 06, 07, 08, 09, 10, 11, 12, 13, 16, 18, 20, 24, 25

- **LP-14 — REVISE, CONTENT REVISION.** Final key **D**, final difficulty **medium** (unchanged).
  - `prompt` — Unclear wording („пада върху повърхността ѝ с въздуха отдолу“).
    New value: «Лъч светлина се разпространява във вода (n = 1,33) и пада отдолу върху границата вода – въздух под ъгъл на падане 45°. Кое твърдение е вярно?»
- **LP-15 — DELETE.** Within-topic redundancy: sin α_гр = n₂/n₁ for two media is the first step of LP-18, which also requires the decision; LP-13, LP-14 and LP-24 already test the critical angle.
- **LP-17 — REVISE, CONTENT REVISION.** Final key **A**, final difficulty **medium** (unchanged).
  - `options.A` — Key is conspicuously the longest / most detailed option (length cue).
    New value: «За да е възможно пълно вътрешно отражение на границата между сърцевината и обвивката»
- **LP-19 — REVISE, CONTENT REVISION.** Final key **A**, final difficulty **easy** (unchanged).
  - `options.A` — Key is conspicuously the longest / most detailed option (length cue).
    New value: «Показателят на пречупване на стъклото е различен за светлина с различна дължина на вълната»
- **LP-21 — REVISE, CONTENT REVISION.** Final key **A**, final difficulty **easy** (unchanged).
  - `options.A` — Key is conspicuously the longest / most detailed option (length cue).
    New value: «Слънчевата светлина се пречупва и отразява във водните капки и поради дисперсия се разлага на цветове»
- **LP-22 — REVISE, DIFFICULTY-ONLY:** hard → **medium**. Single proportion λ₁n₁ = λ₂n₂; same level as LP-06 (medium). Final key **B**.
- **LP-23 — REVISE, CONTENT REVISION.** Final key **B**, final difficulty **hard** (unchanged).
  - `prompt` — Second sentence has no subject.
    New value: «Лъч светлина във въздух пада под ъгъл 40° върху плоскопаралелна стъклена пластина (n = 1,5). Лъчът излиза във въздуха през срещуположната стена на пластината. Колко е ъгълът между излезлия лъч и нормалата към стената?»

### WP — wave-properties-of-light (raw 23 → final 22)

KEEP: 01, 02, 03, 04, 05, 07, 08, 09, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23

- **WP-06 — DELETE.** Same numbers as WP-04 run backwards (Δr = 1500 nm, m = 3, λ = 500 nm); either item gives away the other.

### QL — quantum-properties-of-light (raw 23 → final 23)

KEEP: 01, 02, 03, 04, 05, 06, 07, 09, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 21, 22, 23

- **QL-08 — REVISE, DIFFICULTY-ONLY:** easy → **medium**. Two steps (ν = c/λ, then E = hν) with three powers of ten; harder than the other easy items. Final key **C**.
- **QL-20 — REVISE, CONTENT REVISION.** Final key **C**, final difficulty **medium** (unchanged).
  - `prompt` — Stem („Кое твърдение за броя фотони…“) does not match the options, which name a beam.
    New value: «Червен и син лазерен лъч имат еднаква мощност. През напречното сечение на кой лъч преминават повече фотони за една секунда?»
  - `options.B` — Must fit the new stem.
    New value: «През двата лъча преминава еднакъв брой, защото мощностите им са равни»

### AT — atoms-and-atomic-transitions (raw 25 → final 23)

KEEP: 02, 03, 04, 05, 06, 07, 08, 10, 11, 12, 13, 14, 15, 16, 17, 19, 21, 22, 24, 25

- **AT-01 — REVISE, DIFFICULTY-ONLY:** medium → **easy**. One-step inference from λ = h/(mv). Final key **A**.
- **AT-09 — REVISE, CONTENT REVISION.** Final key **B**, final difficulty **medium** (unchanged).
  - `options.D` — SCIENTIFIC/AMBIGUITY: the stem says unnamed higher states lie between −0,85 eV and 0, and the real 1→5 transition is 13,06 eV, so „13,0 eV“ cannot be excluded from the given data. 12,5 eV lies between the listed 12,09 and 12,75 eV and is decidable (do not use a value above 13,6 eV: photo-ionisation would absorb it).
    New value: «12,5 eV»
  - `explanation` — Last sentence must match the new option D.
    New value: «Атомът поглъща фотон само ако енергията му е равна на разлика между две енергетични нива. От основното състояние са възможни преходи с енергии 10,2 eV (до n = 2), 12,09 eV (до n = 3), 12,75 eV (до n = 4) и още по-големи към по-високите състояния. От предложените стойности само 10,2 eV съвпада с такава разлика; 12,5 eV попада между 12,09 eV и 12,75 eV и не отговаря на никой преход.»
- **AT-18 — REVISE, CONTENT REVISION.** Final key **D**, final difficulty **medium** (unchanged).
  - `options.D` — Form cue: three options start with „За да…“, only the key is a two-clause statement.
    New value: «За да преобладава стимулираното излъчване над поглъщането на фотоните»
- **AT-20 — DELETE.** Cross-topic duplicate of QL-09 (photons per second from laser power and wavelength, changed values); it assesses E = hν of 2.3, not an atomic-transition result.
- **AT-23 — DELETE.** Scope: quantitative short-wavelength limit λmin = hc/(eU); the programme asks only for a qualitative description of X-ray emission and gives no formula. AT-22 keeps the qualitative version.

### NU — atomic-nucleus (raw 24 → final 23)

KEEP: 01, 02, 03, 07, 09, 12, 15, 16, 18, 20, 21, 23, 24

- **NU-04 — REVISE, CONTENT REVISION.** Final key **A**, final difficulty **medium** (unchanged).
  - `options.A` — Key is conspicuously the longest / most detailed option (length cue).
    New value: «Между нуклоните действат ядрени сили на привличане, по-силни от електричното отблъскване.»
- **NU-05 — REVISE, CONTENT REVISION.** Final key **C**, final difficulty **medium** (unchanged).
  - `options.C` — Key is conspicuously the longest / most detailed option (length cue). (ratio 2,2)
    New value: «Неутроните участват в ядреното привличане, без да добавят електрично отблъскване.»
- **NU-06 — REVISE, CONTENT REVISION.** Final key **B**, final difficulty **hard** (unchanged).
  - `options.B` — Key is conspicuously the longest / most detailed option (length cue).
    New value: «Енергията на връзката расте приблизително пропорционално на A, а специфичната е почти постоянна.»
- **NU-08 — DELETE.** Within/cross-topic redundancy: ΔE = Δm·931,5 MeV is the first step of NU-09 and the whole of NR-19.
- **NU-10 — REVISE, CONTENT REVISION.** Final key **B**, final difficulty **easy** (unchanged).
  - `options.B` — Key is conspicuously the longest / most detailed option (length cue). (ratio 2,6; only option with numbers)
    New value: «Расте при леките ядра, има максимум около желязото и намалява при тежките.»
  - `options.C` — Lengthened to reduce the length gap.
    New value: «Намалява монотонно и е най-малка при най-тежките ядра.»
- **NU-11 — REVISE, CONTENT REVISION.** Final key **A**, final difficulty **medium** (unchanged).
  - `options.A` — Key is conspicuously the longest / most detailed option (length cue).
    New value: «⁷₃Li има по-голяма обща енергия на връзката, но ⁴₂He е по-здраво свързано ядро.»
- **NU-13 — REVISE, CONTENT REVISION.** Final key **B**, final difficulty **medium** (unchanged).
  - `explanation` — SCIENTIFIC precision: β⁺ decay is not „the reverse process“ of β⁻ decay.
    New value: «При β⁻-разпадане масовото число не се променя, а зарядовото нараства с 1: Z = 6 + 1 = 7 (азот), A = 14. Излъчват се електрон и антинеутрино; неутрино се излъчва при β⁺-разпадане, заедно с позитрон.»
- **NU-14 — REVISE, CONTENT REVISION.** Final key **C**, final difficulty **easy** (unchanged).
  - `options.C` — Key is conspicuously the longest / most detailed option (length cue).
    New value: «Нито масовото, нито зарядовото число се променя.»
- **NU-17 — REVISE, CONTENT REVISION.** Final key **B**, final difficulty **easy** (unchanged).
  - `options.B` — Key is conspicuously the longest / most detailed option (length cue).
    New value: «Йонизират атоми и молекули в клетките и могат да увредят ДНК.»
- **NU-19 — REVISE, CONTENT REVISION.** Final key **A**, final difficulty **medium** (unchanged).
  - `options.A` — Key is conspicuously the longest / most detailed option (length cue).
    New value: «Еквивалентната доза е по-голяма при α-частиците, защото биологичното им действие е по-силно.»
- **NU-22 — REVISE, DIFFICULTY-ONLY:** hard → **medium**. Two half-lives plus one conceptual trap (mass stays in the sample); not multi-concept hard. Final key **C**.

### NR — nuclear-reactions (raw 24 → final 23)

KEEP: 01, 02, 03, 04, 05, 07, 08, 09, 10, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 23, 24

- **NR-06 — DELETE.** Within-topic redundancy: identifying the light particle X from A and Z conservation is the same task as NR-02; NR-03, NR-04 and NR-05 also test balancing.
- **NR-11 — REVISE, CONTENT REVISION.** Final key **D**, final difficulty **medium** (unchanged).
  - `options.D` — Key is conspicuously the longest / most detailed option (length cue).
    New value: «При допир със стена плазмата би се охладила и би я повредила; магнитното поле я държи встрани»
- **NR-22 — REVISE, CONTENT REVISION.** Final key **A**, final difficulty **hard** (unchanged).
  - `options.A` — Options not parallel (B–C say „повече енергия“).
    New value: «Около 4 пъти повече енергия при синтезното гориво»

### PA — particles (raw 22 → final 21)

KEEP: 01, 02, 03, 04, 05, 06, 07, 08, 09, 10, 11, 12, 13, 14, 15, 16, 17, 18, 22

- **PA-19 — REVISE, DIFFICULTY-ONLY:** hard → **medium**. Recognition that the antineutron consists of antiquarks; single concept. Final key **C**.
- **PA-20 — REVISE, DIFFICULTY-ONLY:** hard → **medium**. Choosing an explanation among clearly false alternatives; no calculation or chained inference. Final key **B**.
- **PA-21 — DELETE.** Cross-topic near-duplicate of NU-04 (same stem, same distractors: gravity, shell electrons); the item belongs to 3.2.

### SG — stars-galaxies-universe (raw 26 → final 25)

KEEP: 01, 03, 04, 05, 07, 08, 09, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 22, 23, 24, 25, 26

- **SG-02 — REVISE, CONTENT REVISION.** Final key **D**, final difficulty **medium** (unchanged).
  - `explanation` — Grammar: „Две от протоните“ → „Два от протоните“.
    New value: «Replace only the words „Две от протоните се превръщат“ with „Два от протоните се превръщат“; rest of the explanation unchanged.»
- **SG-06 — DELETE.** Changed-values duplicate of SG-07 (radius ratio from L ∝ R²T⁴); SG-04 and SG-05 cover the direct use.
- **SG-21 — REVISE, DIFFICULTY-ONLY:** hard → **medium**. The model t = r/v is given in the stem; what remains is substituting v = Hr and two unit conversions. Final key **C**.

## 5. Scientific corrections

Keys, formulas, substitutions, units and rounding are correct in all 311
candidates. Three items need a scientific or ambiguity fix (all in section 4):

| ID | Problem | Fix |
|---|---|---|
| AT-09 | Distractor 13,0 eV cannot be excluded: the stem leaves the states above n = 4 unspecified and the real 1→5 transition is 13,06 eV. | Option D → 12,5 eV; explanation rewritten. |
| IN-23 | Distractor B („I2 > I1“) is a true statement for the given 200:100 transformer. | Option B replaced by a false statement. |
| NU-13 | Explanation calls β⁺ decay „the reverse process“ of β⁻ decay. | Explanation sentence replaced. |

Verified and left as they are (school-level simplifications that are acceptable):
SG-18 explains the cosmological redshift as a Doppler effect; SG-17 equates
look-back time with distance in light-years („около“); SG-21 uses t = 1/H with the
assumption stated in the stem; PA-07 orders the interactions by the conventional
school ranking; NR-11 distractor B starts from a true clause (plasma is
quasi-neutral) but draws a false conclusion; IN-16 ignores the switch-on
transient („след включването“).

## 6. Curriculum-scope findings

Removed or moved for scope (7):

| ID | Decision | Finding |
|---|---|---|
| EF-23 | DELETE | Superposition of two fields; beyond 1.1. |
| AT-23 | DELETE | Quantitative λmin = hc/(eU); 3.1 is qualitative for X-rays. |
| EW-21 | DELETE | Wavelength in a medium is 2.1; also duplicates LP-06/LP-22. |
| CP-14 | MOVE → magnetic-field | Force direction on a moving charge is a 1.2 result. |
| CP-15 | DELETE | Same 1.2 result; duplicates MF-14/MF-19. |
| AT-20 | DELETE | Photon counting is 2.3; duplicates QL-09. |
| PA-21 | DELETE | Nuclear stability is 3.2; duplicates NU-04. |

Edge cases reviewed and kept in place:

- **EF-24** (charge sharing + Coulomb): two point charges, charge conservation
  is prior knowledge; in scope, hard.
- **MF-09** (compass inside solenoid): recognises solenoid field direction; the
  inside-the-coil trap is legitimate; hard. **MF-19** (electron near a wire):
  combines two 1.2 results (field direction of a straight wire, force on a moving
  charge); checked against „antiparallel currents repel“; hard.
- **CP-09/10/11/13**: use eU = mv²/2 with constants supplied — an energetic
  description, allowed by 1.3 although the topic has no formulas.
- **CP-17/CP-18** (r grows with v; helix): qualitative motion in a uniform
  magnetic field; in scope.
- **WP-19**: uses Δr = mλ between neighbouring slits qualitatively, no grating
  formula; in scope. **WP-23**: λ = λ₀/n is prerequisite context from 2.1, the
  assessed step is the interference condition; in scope, hard.
- **QL-18**: stopping voltage is not a programme term, but eU = Ek,max is given
  in the stem and the task is Einstein's equation in eV; kept.
- **QL-01…06, QL-23**: black body, Wien and Stefan are explicit 2.3 results;
  QL-06 is the only hard one.
- **AT-22** (qualitative shift of the short-wave limit) kept; **AT-25**
  (excitation by electron impact) kept as hard — the stem states that the atom
  can take part of the electron's energy.
- **PA-15/PA-18** (quark charges): charges are supplied in the stem/explanation
  and the sum is one line; kept. **PA-10** (β⁻ decay as weak interaction) is an
  example of a listed concept; kept. **PA-20** kept, relabelled medium.
- **SG-01/SG-02/SG-11** (stellar fusion) belong to 3.5 (pp cycle); they do not
  repeat NR items. **SG-04/05/07/09** use L = 4πR²σT⁴, a 3.5 formula; QL-06 uses
  Wien + Stefan and stays in 2.3. **SG-21** (1/H) kept as medium.
- **NR-19, NR-23** use ΔE = Δm·c² / specific binding energy from 3.2 as
  prerequisite to „обяснява защо се отделя енергия“; kept in 3.3.
- **NR-21** needs the mole; all data are supplied; kept.

## 7. Cross-topic duplicate and redundancy findings

Cross-topic (deleted or moved): CP-15 ↔ MF-14/MF-19; CP-14 → MF; EW-21 ↔
LP-06/LP-22; AT-20 ↔ QL-09; PA-21 ↔ NU-04; NU-08 ↔ NR-19 (and NU-09).

Within-topic (deleted): EF-10 ⊂ EF-11; MF-16 ≈ MF-17; EW-18 ≈ EW-16; LP-15 ⊂
LP-18; WP-06 = WP-04 reversed; NR-06 ≈ NR-02; SG-06 ≈ SG-07; EW-08 (cue, and
covered by EW-09/EW-13/AT-21).

Pairs examined and both kept (related, not duplicate):

| Pair | Why both stay |
|---|---|
| CP-07 / CP-08 | Proton vs electron: the sign reasoning is the point. |
| CP-10 / AT-04 | Same set-up, different target (speed vs de Broglie wavelength). |
| CP-10 / CP-11 | CP-11 adds the charge ratio. |
| CP-04 / CP-05 / CP-18 | Three distinct cases (parallel, perpendicular, oblique). |
| MF-08 / MF-09 | Pole identification vs needle inside the coil. |
| MF-12 / MF-20, MF-15 / MF-17 / MF-18 | Direct, data-table and equilibrium uses. |
| IN-05 / IN-16 | Faraday experiment vs why a transformer needs AC. |
| EW-06 / EW-07, EW-12 / EW-20, EW-15 / EW-19 | Concept + calculation pairs. |
| EW-04 / LP-19 distractor, EW-10, LP-01 | Prerequisite overlap only. |
| WP-04 / WP-05 / WP-22 | Rising difficulty on the max/min condition. |
| QL-02 / QL-23, QL-04 / QL-05, QL-13 / QL-14 | Different tasks on one law. |
| QL-06 / SG-07 | Wien + Stefan vs luminosity ratio. |
| NU-09 / NR-19 / NR-23 | Binding energy per nucleon vs reaction energy. |
| NU-13 / NU-16 / PA-10 | Products, mechanism, interaction. |
| NU-20 / NU-21 / NU-22 / NU-24 | Table, graph points, mass trap, dating. |
| NR-02 / NR-03 / NR-04 / NR-05 | Different unknowns in the balance. |
| NR-09 / SG-11, NR-08 / SG-01 | Reactor vs star context. |
| PA-08 / PA-09 / PA-11 | Three different interaction–carrier pairs. |
| SG-19 / SG-20 | Direct use vs determining H from data. |

## 8. Difficulty changes

20 candidates change difficulty (19 difficulty-only, 1 inside a content revision):

- medium → easy (11): EF-07, EF-14, EF-15, EF-19, MF-05, MF-10, IN-12, IN-14, EW-13, EW-19, AT-01
- easy → medium (1): QL-08
- hard → medium (8): IN-24, IN-26, EW-22, LP-22, NU-22, PA-19, PA-20, SG-21

Ten of the collectors' 28 hard labels do not survive: eight are relabelled
medium, two (EF-23, EW-21) are deleted. No candidate is raised to hard. Labels
were set on merit, not to a quota.

## 9. Exact DELETE IDs (15)

- `physics-g10-electric-field-run-1-10`
- `physics-g10-electric-field-run-1-23`
- `physics-g10-magnetic-field-run-1-016`
- `physics-g10-charged-particle-motion-run-1-015`
- `physics-g10-electromagnetic-waves-run-1-008`
- `physics-g10-electromagnetic-waves-run-1-018`
- `physics-g10-electromagnetic-waves-run-1-021`
- `physics-g10-light-propagation-run-1-015`
- `physics-g10-wave-properties-of-light-run-1-06`
- `physics-g10-atoms-and-atomic-transitions-run-1-020`
- `physics-g10-atoms-and-atomic-transitions-run-1-023`
- `physics-g10-atomic-nucleus-run-1-008`
- `physics-g10-nuclear-reactions-run-1-006`
- `physics-g10-particles-run-1-021`
- `physics-g10-stars-galaxies-universe-run-1-006`

## 10. Exact MOVE IDs and destinations (1)

- `physics-g10-charged-particle-motion-run-1-014` → `physics-g10-magnetic-field` (key A, medium)

## 11. Exact REVISE IDs (58)

`EF-03`, `EF-07`, `EF-14`, `EF-15`, `EF-19`, `EF-22`, `MF-05`, `MF-10`, `MF-14`, `MF-22`, `CP-03`, `CP-04`, `CP-17`, `CP-21`, `IN-01`, `IN-11`, `IN-12`, `IN-14`, `IN-15`, `IN-16`, `IN-23`, `IN-24`, `IN-26`, `EW-01`, `EW-04`, `EW-09`, `EW-11`, `EW-12`, `EW-13`, `EW-19`, `EW-22`, `LP-14`, `LP-17`, `LP-19`, `LP-21`, `LP-22`, `LP-23`, `QL-08`, `QL-20`, `AT-01`, `AT-09`, `AT-18`, `NU-04`, `NU-05`, `NU-06`, `NU-10`, `NU-11`, `NU-13`, `NU-14`, `NU-17`, `NU-19`, `NU-22`, `NR-11`, `NR-22`, `PA-19`, `PA-20`, `SG-02`, `SG-21`

## 12. REVISE split

### Content revisions (39)

- `physics-g10-electric-field-run-1-03`
- `physics-g10-electric-field-run-1-22`
- `physics-g10-magnetic-field-run-1-010` (also difficulty medium → easy)
- `physics-g10-magnetic-field-run-1-014`
- `physics-g10-magnetic-field-run-1-022`
- `physics-g10-charged-particle-motion-run-1-003`
- `physics-g10-charged-particle-motion-run-1-004`
- `physics-g10-charged-particle-motion-run-1-017`
- `physics-g10-charged-particle-motion-run-1-021`
- `physics-g10-electromagnetic-induction-alternating-current-run-1-001`
- `physics-g10-electromagnetic-induction-alternating-current-run-1-011`
- `physics-g10-electromagnetic-induction-alternating-current-run-1-015`
- `physics-g10-electromagnetic-induction-alternating-current-run-1-016`
- `physics-g10-electromagnetic-induction-alternating-current-run-1-023`
- `physics-g10-electromagnetic-waves-run-1-001`
- `physics-g10-electromagnetic-waves-run-1-004`
- `physics-g10-electromagnetic-waves-run-1-009`
- `physics-g10-electromagnetic-waves-run-1-011`
- `physics-g10-electromagnetic-waves-run-1-012`
- `physics-g10-light-propagation-run-1-014`
- `physics-g10-light-propagation-run-1-017`
- `physics-g10-light-propagation-run-1-019`
- `physics-g10-light-propagation-run-1-021`
- `physics-g10-light-propagation-run-1-023`
- `physics-g10-quantum-properties-of-light-run-1-020`
- `physics-g10-atoms-and-atomic-transitions-run-1-009`
- `physics-g10-atoms-and-atomic-transitions-run-1-018`
- `physics-g10-atomic-nucleus-run-1-004`
- `physics-g10-atomic-nucleus-run-1-005`
- `physics-g10-atomic-nucleus-run-1-006`
- `physics-g10-atomic-nucleus-run-1-010`
- `physics-g10-atomic-nucleus-run-1-011`
- `physics-g10-atomic-nucleus-run-1-013`
- `physics-g10-atomic-nucleus-run-1-014`
- `physics-g10-atomic-nucleus-run-1-017`
- `physics-g10-atomic-nucleus-run-1-019`
- `physics-g10-nuclear-reactions-run-1-011`
- `physics-g10-nuclear-reactions-run-1-022`
- `physics-g10-stars-galaxies-universe-run-1-002`

### Difficulty-only revisions (19)

- `physics-g10-electric-field-run-1-07`: medium → easy
- `physics-g10-electric-field-run-1-14`: medium → easy
- `physics-g10-electric-field-run-1-15`: medium → easy
- `physics-g10-electric-field-run-1-19`: medium → easy
- `physics-g10-magnetic-field-run-1-005`: medium → easy
- `physics-g10-electromagnetic-induction-alternating-current-run-1-012`: medium → easy
- `physics-g10-electromagnetic-induction-alternating-current-run-1-014`: medium → easy
- `physics-g10-electromagnetic-induction-alternating-current-run-1-024`: hard → medium
- `physics-g10-electromagnetic-induction-alternating-current-run-1-026`: hard → medium
- `physics-g10-electromagnetic-waves-run-1-013`: medium → easy
- `physics-g10-electromagnetic-waves-run-1-019`: medium → easy
- `physics-g10-electromagnetic-waves-run-1-022`: hard → medium
- `physics-g10-light-propagation-run-1-022`: hard → medium
- `physics-g10-quantum-properties-of-light-run-1-008`: easy → medium
- `physics-g10-atoms-and-atomic-transitions-run-1-001`: medium → easy
- `physics-g10-atomic-nucleus-run-1-022`: hard → medium
- `physics-g10-particles-run-1-019`: hard → medium
- `physics-g10-particles-run-1-020`: hard → medium
- `physics-g10-stars-galaxies-universe-run-1-021`: hard → medium

## 13. Final expected count per topic, with difficulty and keys (also sections 15 and 18)

| Topic | Final | easy | medium | hard | A | B | C | D |
|---|---|---|---|---|---|---|---|---|
| EF | 22 | 13 | 8 | 1 | 6 | 4 | 6 | 6 |
| MF | 24 | 11 | 11 | 2 | 6 | 6 | 6 | 6 |
| CP | 21 | 9 | 10 | 2 | 5 | 4 | 7 | 5 |
| IN | 26 | 12 | 13 | 1 | 7 | 6 | 7 | 6 |
| EW | 19 | 12 | 7 | 0 | 5 | 5 | 5 | 4 |
| LP | 24 | 11 | 11 | 2 | 6 | 7 | 6 | 5 |
| WP | 22 | 10 | 10 | 2 | 6 | 6 | 5 | 5 |
| QL | 23 | 8 | 13 | 2 | 6 | 6 | 6 | 5 |
| AT | 23 | 11 | 10 | 2 | 5 | 5 | 7 | 6 |
| NU | 23 | 10 | 12 | 1 | 6 | 6 | 6 | 5 |
| NR | 23 | 9 | 12 | 2 | 6 | 6 | 5 | 6 |
| PA | 21 | 9 | 12 | 0 | 5 | 5 | 5 | 6 |
| SG | 25 | 10 | 14 | 1 | 6 | 6 | 7 | 6 |
| **Total** | **296** | **135** | **143** | **18** | **75** | **72** | **78** | **71** |

MF counts include the moved CP-14; CP counts exclude it.

## 14. Final expected total

**296** = 311 − 15 deleted. The move does not change the total.

## 15. Final expected difficulty

easy **135** (45.6 %) | medium **143** (48.3 %) | hard **18** (6.1 %).

## 16. Expected hard candidate IDs (18)

EF-24, MF-09, MF-19, CP-11, CP-13, IN-25, LP-23, LP-24, WP-22, WP-23, QL-06, QL-21, AT-04, AT-25, NU-06, NR-22, NR-23, SG-07

- `physics-g10-electric-field-run-1-24`
- `physics-g10-magnetic-field-run-1-009`
- `physics-g10-magnetic-field-run-1-019`
- `physics-g10-charged-particle-motion-run-1-011`
- `physics-g10-charged-particle-motion-run-1-013`
- `physics-g10-electromagnetic-induction-alternating-current-run-1-025`
- `physics-g10-light-propagation-run-1-023`
- `physics-g10-light-propagation-run-1-024`
- `physics-g10-wave-properties-of-light-run-1-22`
- `physics-g10-wave-properties-of-light-run-1-23`
- `physics-g10-quantum-properties-of-light-run-1-006`
- `physics-g10-quantum-properties-of-light-run-1-021`
- `physics-g10-atoms-and-atomic-transitions-run-1-004`
- `physics-g10-atoms-and-atomic-transitions-run-1-025`
- `physics-g10-atomic-nucleus-run-1-006`
- `physics-g10-nuclear-reactions-run-1-022`
- `physics-g10-nuclear-reactions-run-1-023`
- `physics-g10-stars-galaxies-universe-run-1-007`

No hard candidate in EW and PA after review.

## 17. Final expected keys, global

A **75** | B **72** | C **78** | D **71** (sum 296). Balanced; no positional pattern found.

## 18. Final expected keys per topic

See the table in section 13. Smallest cell is 4 (EF–B, CP–B, EW–D), largest 7;
no topic has a cue-level imbalance, so no option is reordered.

Arithmetic checks (run programmatically on the decision table that produced this
document): KEEP 237 + REVISE 58 + DELETE 15 + MOVE 1 = 311; final total 296 =
311 − 15; topic counts, difficulty counts, global keys and per-topic keys each
sum to 296; every survivor counted once; all DELETE/REVISE/MOVE IDs exist; no ID
in two decision groups; content 39 + difficulty-only 19 = 58.

## 19. Remaining caveats

1. **No external source was consulted.** Nuclear, particle and cosmology facts
   were checked from the reviewer's own knowledge of standard values, not
   against a cited text. An approver who wants cited verification should
   spot-check NU-09/NU-10/NU-11, NR-19/NR-23, PA-08/PA-18 and SG-13/SG-21/SG-25.
2. **The candidate validator was not run** (`src.validate_question_candidates`),
   to avoid any chance of touching `data/questions.db`; duplicates against
   questions already stored in the database were therefore not checked.
3. **EF-23** is deleted on a strict reading of the programme (superposition is
   not listed). Bulgarian textbooks do treat the field of two charges; if the
   approver reads 1.1 more broadly, the item is scientifically correct (key B)
   and could be restored as hard.
4. **Length cues below the 1,3 threshold were left** (e.g. EF-05, CP-22, IN-06,
   NU-03, PA-20, WP-19, WP-21). In AT the key is the strictly longest option in
   11 of 25 items but never by 1,3 or more, so no AT item was revised for
   length; WP and NU show a similar mild tendency.
5. After the revisions some keys are still the longest option by a small margin
   (IN-11, IN-23, EW-12, NU-10, NU-19); the gap is reduced, not removed.
6. The moved CP-14 says „читателя“ where MF items say „наблюдателя“; harmless,
   not revised. Its new ID in the magnetic-field dataset is for the correction
   agent to assign under the pipeline's rules.
7. Collector and reviewer are different models, but the approving reviewer must
   still be a separate identity; all candidates remain `pending`.

