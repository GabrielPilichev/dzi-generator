# Biology Wave 1 pilot — resolution record

Resolved 2026-10-06, after the correction pass. Follows
`biology_wave1_pilot_independent_review.md`; historical review decisions are
not rewritten. Nothing was approved, exported or imported; no database write.
All 144 survivors remain `pending`. (Candidate datasets are git-ignored, so
the dataset edits below do not appear in `git status`.)

## G9 Q6 — table dependency

`biology-g9-chemical-composition-of-living-matter-run-1-006` carried a
pipe-and-newline table in `prompt`. Candidate prompts render as plain text, so
the table was replaced by the same eight values as sentences (oxygen 47/65,
silicon 28/<0.01, carbon 0.02/18, nitrogen <0.01/3). Options, key and
explanation are unchanged; no number added.

## G9 Q29 / Q30 / Q34 — scope against the official programme

Authority: МОН Grade 9 Biology, 36-hour programme, Приложение № 11
(`up_ix_bio_pril11-36.pdf`). Topic 1 „Химичен състав на живата материя“
expects the student to: describe organic compounds „по алгоритъм (състав –
свойства – функции)“; define „въглехидрати, липиди, белтъци, ензими,
нуклеинови киселини“; group organic compounds „въз основа на броя и вида на
мономерните им единици“; compare biopolymer groups „по състав и функции“. The
programme names no sub-classes (fatty-acid saturation, steroids, amino-acid
side chains).

| Candidate | Classification | Basis | Action |
|---|---|---|---|
| 029 saturated vs unsaturated fatty acids | BORDERLINE_BUT_SUPPORTED | Lipid „състав – свойства“ (liquid oils vs solid fats); depth beyond the programme wording | unchanged |
| 030 cholesterol as steroid lipid | BORDERLINE_BUT_SUPPORTED | Lipids and their functions; „steroid“ is not named by the programme, the item is recognition of one lipid | unchanged |
| 034 variable part of amino acids | BORDERLINE_BUT_SUPPORTED | Proteins by „състав“ and „вид на мономерните единици“; the side chain is the only detail beyond the wording | unchanged |

None is OUT_OF_SCOPE. The reviewer may still prefer to drop 030 as the most
peripheral of the three; that is an approval-time judgement, not a blocker.

## Difficulty metadata

The 41 surviving KEEP candidates whose collector difficulty differed from the
independent-review difficulty (P1 8, P2 16, P3 13, P4 4) now carry the reviewed
difficulty. Only the `difficulty` field changed. REVISE candidates already
matched the review.

| Dataset | Easy | Medium | Hard | Total |
|---|---|---|---|---|
| Cell and tissues | 22 | 9 | 1 | 32 |
| Skin | 20 | 16 | 1 | 37 |
| Chemical composition | 24 | 14 | 1 | 39 |
| Human evolution | 17 | 17 | 2 | 36 |
| **Total** | **83** | **56** | **5** | **144** |

Integrity: reverting the 41 difficulty values and the Q6 prompt reproduces the
pre-task SHA-1 of every dataset exactly, so nothing else changed.

## Validation

`validate_question_candidates` on all four datasets (with `--db`, without
`--annotate`): 4/4 pass, 0 errors, 0 flags. Run again with `--compare` against
the other three datasets: 0 errors, 0 flags (no cross-dataset duplicate
signals). Audit: 144 unique IDs, 0 duplicate stems, 0 malformed options or
unresolved keys. One exact option set is shared by P4-008 and P4-012 (hominin
identification from different skeleton descriptions); the stems test different
facts, so it is noted, not acted on.

## Remaining approval blockers

None from this gate. Open judgement items carried from the review: the
length-cue and recall-heavy findings (not blocking), and the optional
reconsideration of 030 above.
