# Physics, Grade 9 — question bank final content review

Review of the six pending candidate datasets under
`data/question_candidates/physics/grade-9/`. Prepared on 2026-10-05 by Claude
Opus 5.5. **Review only:** no candidate file, database or other file was
changed, nothing was approved, exported or imported. This document is the
specification for a later correction agent.

**Independence caveat.** Two of the six datasets (`electric-circuits`,
`oscillations`) were collected earlier in the same working session that produced
this review, under a different model. They were audited here with the same
checks as the other four, but a reviewer who did not see them being written
would be more independent. All 141 candidates stay `pending`; the approving
reviewer must be someone other than the collector identity.

Scope authority: МОН programme for IX клас, Приложение № 9 (36 hours), as
recorded in `docs/reviews/physics_g9_curriculum_structure_review.md` and
`data/subject_structures/physics/grade-9.json`. The 90-hour variant was not used.

## 1. Result

| Topic | Initial | Delete | Move | Revise | Keep unchanged | Final |
|---|---|---|---|---|---|---|
| electric-circuits (EC) | 24 | 2 | 0 | 4 | 18 | 22 |
| electrical-energy (EE) | 24 | 2 | 0 | 6 | 16 | 22 |
| current-in-metals-and-semiconductors (SC) | 23 | 3 | 0 | 1 | 19 | 20 |
| oscillations (OS) | 24 | 1 | 0 | 7 | 16 | 23 |
| mechanical-waves (MW) | 23 | 3 | 0 | 5 | 15 | 20 |
| sound (SO) | 23 | 2 | 0 | 9 | 12 | 21 |
| **Total** | **141** | **13** | **0** | **32** | **96** | **128** |

REVISE means any required change, including a difficulty-only relabel (20 of the
32 are difficulty-only; 12 change text). No candidate changes its correct letter.

Final difficulty: **easy 64 | medium 57 | hard 7**.

| Topic | easy | medium | hard | A | B | C | D |
|---|---|---|---|---|---|---|---|
| EC | 9 | 11 | 2 | 6 | 5 | 6 | 5 |
| EE | 11 | 9 | 2 | 6 | 6 | 5 | 5 |
| SC | 10 | 8 | 2 | 5 | 4 | 6 | 5 |
| OS | 11 | 11 | 1 | 6 | 5 | 6 | 6 |
| MW | 10 | 10 | 0 | 5 | 6 | 4 | 5 |
| SO | 13 | 8 | 0 | 6 | 4 | 6 | 5 |
| **Total** | **64** | **57** | **7** | **34** | **30** | **33** | **31** |

Hard candidates (final): EC-20, EC-24, EE-15, EE-17, SC-22, SC-23, OS-23.

The collectors labelled 14 candidates hard; seven of those are routine two-step
calculations or one-step proportions and are relabelled medium (EC-15, OS-04,
OS-24, MW-21, MW-22, SO-22, SO-23). Thirteen medium labels on one-step recall or
substitution are relabelled easy. The bank is easier than the collection
percentages suggested; that is what the questions are, and no question was
relabelled to reach a quota.

## 2. Method and automatic results

- All 141 candidates were read in full: prompt, four options, key, explanation,
  difficulty, source.
- Every numerical item was recomputed independently, including where the
  distractors come from. **No wrong key and no wrong number was found.**
- Validator, each of the six files, `python3 -B -m src.validate_question_candidates
  --input FILE --db data/questions.db` (read-only): `ok: true`, **0 errors,
  0 flags**, all candidates `pending`, in all six.
- Database comparison (195 stored Physics Grade 8 questions): best prompt
  similarity for any candidate is 0.62, and every pair above 0.5 is a shared
  stem pattern („Кое твърдение за … е вярно?“, „Коя от изброените …“), not shared
  content. **No exact, near or changed-numbers duplicate of a stored question.**
  The Grade 8 bank has no question on electricity, oscillations, waves or sound.
  EE-03 (meaning of a power rating) repeats the idea of a Grade 8 item on
  mechanical power in an electrical context; that is prerequisite reinforcement
  and it is kept.
- Cross-dataset comparison of all 141 prompts (pairwise similarity, then read):
  the validator cannot see across files, and five cross-topic redundancies were
  found this way (section 3).

Two scientific corrections are required (SO-14, SO-18); see section 5.

## 3. Deletions (13)

IDs are `physics-g9-<topic>-run-1-<NN>`; the short form below is topic code +
`NN`.

Cross-topic redundancy (5):

| ID | Reason |
|---|---|
| EE-18 | Ammeter in series / voltmeter in parallel: same question as EC-08 with a bulb instead of a resistor. Instrument connection belongs to circuits. |
| SC-21 | ρ = RS/l calculation with unit conversion: same task as EC-16; it tests the circuits formula, not a semiconductor result. |
| MW-05 | Why a mechanical wave cannot travel in vacuum: same as SO-01 (similarity 0.85, same key wording). Kept once, in sound; MW-01 already covers „материална среда“. |
| SO-10 | A sound wave carries energy, not matter: same idea as MW-02, where it belongs. |
| SO-12 | Sound passing from air to water, frequency constant and λ larger: same scenario and numbers as MW-22 and same concept as MW-12. |

Within-topic redundancy (6):

| ID | Reason |
|---|---|
| EC-02 | Equal voltage, smaller current → larger resistance: same reasoning as EC-17, which adds the graph. |
| EC-09 | 10 Ω + 30 Ω in series: trivial sum, fully covered by EC-12 and EC-19. |
| EE-13 | Series lamps, larger R → larger power: EE-15 tests the same idea and adds R = U²/P. |
| SC-16 | Thermistor in the hand: fourth item on "semiconductor resistance falls when heated" (SC-03, SC-14, SC-20, SC-22), with the same key wording as SC-03. |
| OS-22 | Comparing two pendulums by oscillations per minute: same skill as OS-03. |
| MW-17 | Second earthquake-behaviour item; the programme asks for basic rules once (MW-10). One distractor also relies on tsunami knowledge outside the topic. |

Scope or weak coverage (2):

| ID | Reason |
|---|---|
| SC-19 | Number of electrons from I·t/e: not an expected result of 1.3 (which has no formulas); two distractors with 10⁻¹⁹ electrons are not plausible. |
| MW-20 | Tsunami travel time t = s/u: Grade 8 kinematics with a wave word in it; tests no wave idea. MW-15 covers travel times properly. |

No candidate needs to move to another topic.

## 4. Decision for every candidate

`K` keep unchanged, `R` revise (see section 5), `D` delete. Difficulty shown is
the **final** one; `←` marks a relabel.

### electric-circuits (prefix `physics-g9-electric-circuits-run-1-`)

| NN | Key | Decision | Final difficulty | Note |
|---|---|---|---|---|
| 01 | B | K | easy | |
| 02 | D | D | – | redundant with 17 |
| 03 | A | K | easy | U = 10 V verified |
| 04 | C | K | easy | |
| 05 | C | K | easy | |
| 06 | A | K | easy | |
| 07 | D | K | easy | |
| 08 | B | R | easy ← medium | recall |
| 09 | B | D | – | trivial, covered |
| 10 | A | K | medium | 3 Ω verified |
| 11 | D | K | medium | ≈ 90,9 Ω verified |
| 12 | C | K | medium | 30 V verified |
| 13 | A | K | medium | 0,6 A verified |
| 14 | C | R | easy ← medium | one-step proportion |
| 15 | D | R | medium ← hard | 0,5 Ω verified; two steps, not hard |
| 16 | B | K | medium | 0,2 Ω·mm²/m verified |
| 17 | A | K | medium | |
| 18 | D | K | medium | 20 Ω verified |
| 19 | C | K | medium | 25 Ω verified |
| 20 | B | R | hard | 1,2 A verified; prompt reworded |
| 21 | A | K | medium | |
| 22 | C | K | easy | |
| 23 | D | K | medium | |
| 24 | B | K | hard | R₂ = R₁/2 verified |

### electrical-energy (prefix `physics-g9-electrical-energy-run-1-`)

| NN | Key | Decision | Final difficulty | Note |
|---|---|---|---|---|
| 01 | D | K | easy | |
| 02 | D | K | easy | 24 J verified |
| 03 | A | K | easy | |
| 04 | D | K | easy | 1150 W verified |
| 05 | C | K | medium | 1,8 MJ verified |
| 06 | A | K | easy | |
| 07 | A | R | easy | option cue |
| 08 | B | R | easy | option cue |
| 09 | A | R | easy ← medium | 2 A verified; one step |
| 10 | D | K | medium | |
| 11 | C | K | medium | |
| 12 | B | K | medium | 8 V verified |
| 13 | C | D | – | redundant with 15 |
| 14 | D | K | medium | 36 W verified |
| 15 | B | K | hard | 807 Ω / 484 Ω verified |
| 16 | B | K | medium | 2 W verified |
| 17 | C | K | hard | r = 1 Ω, ε = 6 V verified |
| 18 | D | D | – | duplicate of EC-08 |
| 19 | C | R | easy ← medium | 360 J verified |
| 20 | B | K | medium | 3680 W limit; only 4000 W exceeds it |
| 21 | A | K | easy | |
| 22 | A | K | medium | 25 W verified |
| 23 | C | R | easy ← medium | one-step proportion |
| 24 | B | R | medium | option cue |

### current-in-metals-and-semiconductors (prefix `physics-g9-current-in-metals-and-semiconductors-run-1-`)

| NN | Key | Decision | Final difficulty | Note |
|---|---|---|---|---|
| 01 | B | K | easy | |
| 02 | D | K | easy | |
| 03 | A | K | easy | |
| 04 | C | K | easy | |
| 05 | C | K | easy | |
| 06 | A | K | easy | |
| 07 | D | K | easy | |
| 08 | B | K | easy | |
| 09 | A | K | easy | |
| 10 | C | K | medium | |
| 11 | B | K | medium | |
| 12 | D | K | medium | |
| 13 | C | K | medium | |
| 14 | A | K | medium | |
| 15 | D | K | medium | orders of magnitude are right for metal / pure Si / insulator |
| 16 | B | D | – | redundant |
| 17 | A | K | medium | |
| 18 | C | K | medium | |
| 19 | D | D | – | outside 1.3; 5·10¹⁸ itself is correct |
| 20 | B | R | easy ← medium | 200 Ω verified; one step |
| 21 | A | D | – | duplicate of EC-16; 0,025 Ω·m itself is correct |
| 22 | C | K | hard | |
| 23 | D | K | hard | |

No band theory or other university-level detail was found. p-type and n-type
(SC-11) go slightly beyond the programme's own words („донори и акцептори“,
„електрони и дупки“, „p-n преход“) but are the standard names for exactly those
concepts and are kept.

### oscillations (prefix `physics-g9-oscillations-run-1-`)

| NN | Key | Decision | Final difficulty | Note |
|---|---|---|---|---|
| 01 | C | K | easy | |
| 02 | A | K | easy | |
| 03 | D | K | easy | 2 Hz, 0,5 s verified |
| 04 | B | R | medium ← hard | one-step proportion |
| 05 | B | R | easy | option cue |
| 06 | D | K | medium | A = 4 cm, T = 2 s verified |
| 07 | A | K | easy | |
| 08 | C | R | medium | option cue |
| 09 | C | K | medium | 0,628 s verified |
| 10 | B | K | medium | |
| 11 | A | K | easy | small angle is stated |
| 12 | D | K | medium | |
| 13 | D | K | medium | 1,99 s verified |
| 14 | A | K | medium | 9,86 m/s² verified |
| 15 | B | K | medium | 0,25 J verified |
| 16 | C | R | easy ← medium | recall |
| 17 | A | K | easy | |
| 18 | C | R | easy ← medium | recall |
| 19 | B | K | easy | |
| 20 | D | R | medium | option cue |
| 21 | D | K | easy | |
| 22 | B | D | – | redundant with 03 |
| 23 | C | K | hard | spring period independent of g also for a vertical spring |
| 24 | A | R | medium ← hard | option style cue; one ratio |

### mechanical-waves (prefix `physics-g9-mechanical-waves-run-1-`)

| NN | Key | Decision | Final difficulty | Note |
|---|---|---|---|---|
| 01 | C | K | easy | |
| 02 | A | K | easy | |
| 03 | D | K | easy | |
| 04 | B | K | easy | |
| 05 | A | D | – | duplicate of SO-01 |
| 06 | D | K | easy | 10 m/s verified |
| 07 | C | R | easy | option cue |
| 08 | B | K | easy | |
| 09 | D | K | easy | |
| 10 | C | R | easy | option cue |
| 11 | B | K | medium | |
| 12 | A | K | medium | |
| 13 | A | K | medium | |
| 14 | B | K | medium | 0,6 m/s verified |
| 15 | B | K | medium | 30 s verified |
| 16 | A | K | medium | вълнов фронт and лъч are still listed concepts after the 2023 amendment |
| 17 | C | D | – | second earthquake-rules item |
| 18 | D | K | medium | |
| 19 | A | R | easy ← medium | definition-level relation |
| 20 | C | D | – | kinematics, not waves; 50 min itself is correct |
| 21 | B | R | medium ← hard | 4 m/s, 2 m verified; two routine steps |
| 22 | C | R | medium ← hard | 3 m verified; one step with unused data |
| 23 | D | K | medium | |

### sound (prefix `physics-g9-sound-run-1-`)

| NN | Key | Decision | Final difficulty | Note |
|---|---|---|---|---|
| 01 | C | K | easy | |
| 02 | A | K | easy | |
| 03 | D | K | easy | |
| 04 | B | K | easy | |
| 05 | A | K | easy | |
| 06 | C | K | easy | |
| 07 | B | K | easy | |
| 08 | D | K | easy | |
| 09 | A | K | easy | |
| 10 | B | D | – | same idea as MW-02 |
| 11 | C | R | easy ← medium | 0,5 m verified; one step |
| 12 | D | D | – | duplicate of MW-12 / MW-22 |
| 13 | A | R | medium | grammar of the key |
| 14 | B | R | medium | **scientific correction** |
| 15 | D | R | easy ← medium | recognition |
| 16 | C | K | medium | |
| 17 | A | R | easy ← medium | recognition; no logarithm needed |
| 18 | C | R | medium | **inaccurate prompt** |
| 19 | D | K | medium | 600 m verified |
| 20 | B | R | easy ← medium | recall |
| 21 | A | K | medium | |
| 22 | D | R | medium ← hard | 0,93 s verified; two routine steps |
| 23 | C | R | medium ← hard | 1·10⁻⁷ J verified; substitution with a unit conversion |

No candidate asks for a logarithmic decibel calculation. SO-22 and MW-15 use the
same "difference of two travel times" calculation in different media; both are
kept because each is the only such item in its topic.

## 5. Revision specification

Apply exactly these changes. Anything not mentioned stays as it is, including
the `correct` letter, `source_id`, `research` and `review` (which stays
`pending`).

### 5.1 Difficulty only (20)

| Set `difficulty` to | IDs |
|---|---|
| `easy` | EC-08, EC-14, EE-09, EE-19, EE-23, SC-20, OS-16, OS-18, MW-19, SO-11, SO-15, SO-17, SO-20 |
| `medium` | EC-15, OS-04, MW-21, MW-22, SO-22, SO-23 |

(OS-24 also becomes `medium`; it is listed in 5.2 because its options change too.)

### 5.2 Text changes (12)

**EC-20** — clearer prompt. Replace `prompt` with:
„Резистор 4 Ω е свързан последователно с два успоредно свързани резистора по
12 Ω. Цялата връзка е включена към източник с напрежение 24 V. Какъв ток
протича през всеки от резисторите по 12 Ω?“
Options, key (B, 1,2 A), explanation and difficulty (hard) unchanged.

**EE-07** — the key is the only precise, long option. Replace option `A` with:
„Работата на страничните сили за пренасяне на единица заряд през източника“.

**EE-08** — only the key has a qualification. Replace option `B` with:
„Химична енергия → електрична енергия → механична енергия“.

**EE-24** — only the key has a parenthesis. Replace option `B` with:
„Токът намалява, а напрежението между изводите се увеличава“.

**OS-05** — key is conspicuously detailed. Replace option `B` with:
„Синусоида – плавна периодична крива“.

**OS-08** — key is much longer than the distractors. Replace option `C` with:
„Не се променя – механичната енергия се запазва“.

**OS-20** — only the key carries „в такт с люлеенето“. Replace option `D` with:
„Леко, периодично, през 2 s“.

**OS-24** — only the key has words. Replace option `A` with „T“ and set
`difficulty` to `medium`. (B „2T“, C „T/2“, D „4T“ unchanged.)

**MW-07** — only the key has a parenthesis. Replace option `C` with:
„Разстоянието между два съседни гребена“.

**MW-10** — key is by far the longest. Replace option `C` with:
„Скривате се под здрава маса и пазите главата си с ръце“.

**SO-13** — the key does not answer „Какво ще чуе…“ grammatically. Replace
option `A` with:
„Двата звука едновременно, защото скоростта на звука във въздуха не зависи от
височината и силата му“.

**SO-14** — scientific correction. The key says equal curve height means equal
intensity, but at equal amplitude a sound of twice the frequency carries more
energy (intensity grows with both amplitude and frequency), so „интензитетът им
е един и същ“ is not correct; the prompt also uses „височина“ for the curve and
for the tone. Ask about pitch only:

- `prompt`: „Микрофон е свързан с осцилоскоп. Звук 1 дава крива с 2 пълни
  трептения на едно деление по оста на времето, а звук 2 – крива с 4 пълни
  трептения на едно деление. Как се различават двата звука по височина на тона?“
- `options`: A „Двата звука са с еднаква височина на тона“; B „Звук 2 е с
  по-висок тон“; C „Звук 2 е с по-нисък тон“; D „По тези криви височината на
  тона не може да се сравни“
- `correct`: B (unchanged)
- `explanation`: „Повече трептения за едно и също време означават по-голяма
  честота, а по-голямата честота се възприема като по-висок тон.“
- `research.concept`: change to "pitch is determined by frequency, read from an
  oscilloscope trace"
- difficulty `medium` unchanged.

**SO-18** — inaccurate prompt: the eardrum and the ossicles are not in the inner
ear. Replace `prompt` with:
„Коя последователност правилно описва предаването на звуковите трептения в
човешкото ухо?“
Options, key (C) and explanation unchanged.

## 6. Coverage after correction

| Topic | Covered | Thin or absent |
|---|---|---|
| Електрически вериги | Ohm's law, I–U graph and measured data, resistivity and R = ρl/S, series, parallel, mixed, instrument connection, household wiring | recognising a connection type from a drawn scheme (no images in the format) |
| Електрична енергия | A = qU = UIt, power, energy conversions and sources, EMF, internal resistance, whole-circuit law, short circuit, terminal voltage, power in series and parallel, fuse | drawing and reading schemes |
| Ток в метали и полупроводници | carriers in metals and semiconductors, temperature dependence, doping, holes, p-n junction, diode, LED, transistor, thermistor, photoresistor, superconductivity | integrated circuits are mentioned only inside an explanation |
| Трептения | restoring force, T and ν, x(t) graph, spring and mathematical pendulum, energy, damping, forced oscillations, resonance, g from a pendulum | spring-pendulum lab |
| Механични вълни | definition, energy not matter, transverse and longitudinal, λ, amplitude, u = λν, speed set by the medium, wavefront and ray, seismic P and S waves, earthquake rule | |
| Звук | sources, medium and speed, hearing range, infra- and ultrasound, pitch, timbre, intensity and its unit, level in dB, the ear, noise protection, echo sounding, bats, resonance box | subjective loudness against objective intensity is tested only indirectly (SO-17) |

Circuits versus energy: after EE-18 is removed the two sets do not share a
question. EE-13/14/15 use series and parallel rules, but for power, which is the
energy topic's own expected result.

## 7. Originality and provenance

All 141 candidates are `original_from_research`; none is `extracted` or
`adapted`. Nothing reads like translated textbook wording, and the numerical
items use their own values. The sources were not re-fetched for this review, so
closeness to source wording was judged from the questions themselves, not by
comparing with the pages. **No blocker.**

Observations, none requiring an edit:

- Four datasets cite OpenStax *College Physics* chapters as research. That is a
  university-level text; the questions written from it stay at Grade 9 level.
- `mechanical-waves` records the Nesebar municipality earthquake leaflet as
  `official`. It is public-authority material, so the type is acceptable; after
  MW-17 is deleted it supports one candidate (MW-10).
- `electric-circuits` and `oscillations` say in their run notes that the scope
  was read from locally extracted programme text and that one web summary of the
  PDF was discarded; this matches what the questions cover.
- SO-17's explanation mentions harm above about 85 dB; this is the commonly cited
  occupational threshold and is not tested by the key.

## 8. Checks for the correction agent

After applying sections 3 and 5 the six files must show:

- counts 22 / 22 / 20 / 23 / 20 / 21, total 128, all `pending`;
- difficulty and A/B/C/D exactly as in the table in section 1;
- validator: 0 errors on each file. The shortened options were checked against
  the `answer_leak` rule by reading (none of the new keys appears in its prompt),
  not by running the validator on edited files.

Deleting candidates leaves gaps in the `NN` sequence; ids must not be renumbered.

## 9. Files

Created: `docs/reviews/physics_g9_question_bank_independent_review.md` (this
file). Nothing else was created or modified; `data/questions.db`,
`vault/.obsidian/graph.json` and the six candidate files have the same SHA-256
before and after the review.
