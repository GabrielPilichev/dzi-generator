# Biology Grade 10 — ecological factors, run 1: review and integration record

Durable record of how the 14 questions of `biology-g10-ecological-factors-run-1`
were collected, reviewed, corrected, approved and integrated (PR #352,
<https://github.com/GabrielPilichev/dzi-generator/pull/352>). It complements the
scope sheet `biology-g10-ecological-factors-run-1-scope.md`. Hashes are SHA-256;
the candidate, review and correction files were scratch files, so the hashes are
the durable identity of what was reviewed.

## Outcome

The empty topic `biology-g10-ecological-factors` (id 178, „Екологични фактори“,
section 66 `biology-g10-biosphere` „Биосфера“, curriculum 11) gains **14
text-only multiple-choice questions**, all browseable and quiz-eligible. This is
the ecological-factors run-1 component of CP1 only (dimensions D1–D3). It is not
the whole topic bank, not CP1 completion, and run 2 (D4–D6) and W4 are not
authorized.

| | Before | After |
|---|---|---|
| Topic 178 (visible and quiz-eligible) | 0 | 14 |
| Section 66 | 0 | 14 |
| Biology Grade 10 | 36 | 50 |
| Biology overall | 244 | 258 |
| `questions` / `multiple_choice_options` | 1695 / 6343 | 1709 / 6399 |
| `question_topic_assignments` | 2049 | 2063 |

New ids come from the stable-id allocator: questions 1701–1714, options
6740–6795, question-topic assignments 2050–2063. `sqlite_sequence` is 1714, 6795
and 2063. Batch numbers 1–14 are ids 1701–1714 in order. Keys are
`C A D D C B A D B A D B C D` (A 3, B 3, C 3, D 5).

Of the 16 collected candidates, 14 are approved and exported; candidates 004 and
011 were rejected and are deliberately omitted (see "Rejected candidates").

## Retained set

Reviewed labels (these live in the provenance file, not in the database):

| No. | Candidate | Dim. | Fact (`research.concept`) | Tag | Difficulty |
|---|---|---|---|---|---|
| 1 | -001 | D1 | абиотичен фактор – неживата среда | recall | easy |
| 2 | -002 | D1 | антропогенни фактори – дейност на човека | recall | easy |
| 3 | -003 | D1 | биотични фактори – влияние на живи организми | recall | easy |
| 4 | -005 | D2 | диапазон на поносимост – долна и горна граница | recall | easy |
| 5 | -006 | D2 | оптимум – най-благоприятни стойности на фактора | recall | easy |
| 6 | -007 | D2 | над горната граница – нарушаване на ензимите (бактерия) | recall | medium |
| 7 | -008 | D2 | данни от опит – оптимум и граници на поносимост | interpret-data | medium |
| 8 | -009 | D2 | поносимостта е специфична за фактор – широк и тесен диапазон | predict | medium |
| 9 | -010 | D2 | групиране по зададена поносимост – тесен диапазон | recall | easy |
| 10 | -012 | D3 | хищничество – убиване и изяждане на плячка | recall | easy |
| 11 | -013 | D3 | паразитизъм срещу хищничество – разлика | recall | easy |
| 12 | -014 | D3 | мутуализъм – полза и за двата вида | recall | easy |
| 13 | -015 | D3 | коменсализъм – полза за единия, без ефект за другия | recall | easy |
| 14 | -016 | D3 | конкуренция за ограничен ресурс | recall | easy |

Coverage D1 3, D2 6, D3 5. Skills: recall 12, interpret-data 1, predict 1.
Difficulty: 11 easy, 3 medium (-007, -008, -009), 0 hard. Strict reasoning
(items needing more than one recalled fact): **2 of 14 (14.3 %)**, -008 and -009.
-009 and -010 do not repeat each other: -009 tests the consequence of a narrow
range, -010 what „тесен“ means. Numbers appear only as hypothetical data supplied
in the stem (-008, -009, -010). Not covered: the effect of light, water, soil,
salinity and pH; further anthropogenic examples; intraspecific/interspecific
competition and symbiosis as a general term; D4–D6 and the population–ecosystem
topic.

## Sources

- Scope: МОН Приложение № 7, theme 2.2 „Екологични фактори“,
  <https://www.mon.bg/nfs/2023/12/up_x_bio.pdf> (official; scope only, type
  `official`). Not a fresh retrieval: a cached PDF (SHA-256
  `760697ba4c9290ea29541d062ad41e3e67706ae856a77e09876f7b07960de800`, fetched
  by the coordinator 2026-11-10T10:14:13Z) was re-read locally and counted
  conservatively as one inspected page.
- Research, newly written questions only (`original_from_research`), retrieved
  2026-11-11 (18:49:10–18:50:35Z):
  - OpenStax *Biology 2e* 44.1 <https://openstax.org/books/biology-2e/pages/44-1-the-scope-of-ecology>
  - OpenStax *Biology 2e* 45.6 <https://openstax.org/books/biology-2e/pages/45-6-community-ecology>
  - OpenStax *Biology 2e* 47.3 <https://openstax.org/books/biology-2e/pages/47-3-threats-to-biodiversity>
  - OpenStax *Biology 2e* 6.5 <https://openstax.org/books/biology-2e/pages/6-5-enzymes>
  - LibreTexts 1.5 „The Biosphere“ <https://bio.libretexts.org/Bookshelves/Ecology/AP_Environmental_Science/01:_Chapters/1.05:_The_Biosphere>
- Collector: `claude-sonnet-5-5 (Sonnet 5.5) / High`; run started
  2026-11-11T18:49:02Z, finished 2026-11-11T18:58:04Z; 8 of 8 pages, 16 of a
  ceiling of 26 candidates.

### Page ledger and the corrected stop reason

The collector's ledger counts the 8 pages as: 1 cached programme (18:49:09Z), 2
OpenStax `robots.txt` (18:49:09Z), 3 OpenStax 44.1 (18:49:10Z), 4 LibreTexts
`robots.txt` (18:49:44Z), 5 LibreTexts 1.5 (18:49:44–45Z), 6 OpenStax 45.6
(18:49:59Z), 7 OpenStax 47.3 (18:49:59Z), 8 OpenStax 6.5 (18:50:35Z). The run was
closed with `stop_reason: no_more_sources` at 18:58:04Z. That was not accurate:
all 8 page units were used while in-scope facts (light, water, soil, salinity,
pH) were still unsourced. The independent review corrected it to `page_limit`;
`started_at`, `finished_at`, `pages_inspected`, `limits` and `sources` were not
changed, and the correction is disclosed in `run.notes`.

Request-spacing defects, disclosed rather than hidden:

- Collection: a script defect let requests to one host go out less than 1.5 s
  apart. The OpenStax `robots.txt` and 44.1 were about 1 s apart; 45.6 and 47.3
  went out in the same second; the `bio.libretexts.org` `robots.txt` and page
  1.5 were retrieved in the same second although that host declares
  `Crawl-delay: 5`. No block, no 429, `max_retries=1`. A neutral user agent was
  used because the standard one contains personal contact data.
- Review verification: four requests to `bio.libretexts.org` about 4 s apart
  against the declared `Crawl-delay: 5` (the verifier's limiter used 3 s). No
  block or 429; four of the eight allowed requests were not used, and the
  verifier's ledger was not rewritten. The final recheck made no requests.

### Review-verification pages (not collection sources)

These three pages were read by the independent reviewer only to verify specific
claims. They are **not** in `sources[]` of the dataset and are **not** counted in
`pages_inspected`: the validator rejects more source records than inspected
pages, and adding them would misdescribe the collection. The corrected
`research.notes` cite them as „проверка при рецензията“. User agent
`LearnPilotReviewVerifier/0.1 (educational content review; low-rate, robots.txt
respected)`; the `bio.libretexts.org` `robots.txt` (retrieved 2026-11-11T20:30:51Z,
SHA-256 `f0430ed375b29e339b6fb5954e31129828b8ac4b8806f78ef3c9f8603456b2b2`)
allowed all three.

1. **kappus-5-01** — „5.1: Tolerance Ranges of Species - Biology LibreTexts“
   - URL: <https://bio.libretexts.org/Courses/Evergreen_Valley_College/Introduction_to_Ecology_(Kappus)/05:_Adaptations_to_the_Environment/5.01:_Tolerance_Ranges_of_Species>
   - Retrieved 2026-11-11T20:30:55Z; SHA-256
     `c999120cfb3a14e6a91fa3d254e7631445b52d0390f16f0ad357b493f3b7cd30`.
   - Licence observed: page tag `license:ccby v4.0`; the attribution names
     Wikipedia „Shelford's law of tolerance“ (CC BY-SA 3.0) and an Open Library
     chapter (CC BY-NC 4.0).
   - Supports: -006 line 6 (conditions within which an organism can best
     survive and reproduce, i.e. the optimum); -005 line 9; -008 line 9; -009
     line 6 (tolerance is set factor by factor).
2. **vce-gardener-05** — „5: Abiotic Stress Effects on Plant Growth and
   Development - Biology LibreTexts“ (Virginia Cooperative Extension Gardener
   Handbook)
   - URL: <https://bio.libretexts.org/Bookshelves/Botany/Virginia_Cooperative_Extension_Gardener_Handbook/05:_Abiotic_Stress_Effects_on_Plant_Growth_and_Development>
   - Retrieved 2026-11-11T20:30:59Z; SHA-256
     `f7f5030569899b877e758aed7fee0d2ccafdae0ad3a9cc00e9286d3b144d85fb`.
   - Licence observed: page tag `license:ccbyncsa v4.0`.
   - Supports: -008 line 318 („All plants have an optimum temperature for good
     growth“, the growth criterion for the best tested temperature). The same
     line shows that above the optimum plant damage runs mainly through stomatal
     closure, reduced photosynthesis and membrane oxidation, which is why -007
     was narrowed to a bacterium.
3. **pcc-micro-8-4** — „8.4: Temperature and Microbial Growth - Biology
   LibreTexts“ (Portland Community College, Cascade Microbiology)
   - URL: <https://bio.libretexts.org/Courses/Portland_Community_College/Cascade_Microbiology/08:_Microbial_Growth/8.4:_Temperature_and_Microbial_Growth>
   - Retrieved 2026-11-11T20:31:03Z; SHA-256
     `52b320f07a1d2dfdbec704e7db8b4c0b8b678f77b57d5cbc1b57f0aa4ffa4028`.
   - Licence observed: page tag `license:ccby`; contributor line 79 says
     „Original content via Openstax (CC BY 4.0)“. It is an OpenStax
     *Microbiology* derivative hosted by LibreTexts, not a new OpenStax
     retrieval; no LLM notice appears in the saved copy.
   - Supports: -007 lines 9 and 15–16 (organism-level support for heat damage
     in microbes).

## Review and correction chain

| Step | Reviewer / author | Result | SHA-256 |
|---|---|---|---|
| Raw candidates | collector, Sonnet 5.5 / High | 16 pending; author claimed 6/16 strict reasoning | `4496d7c5430394c888c6723da53472d324de54eb570bca24f98c6bde1a1230c4` |
| Independent review | Opus 5.5 High | NEEDS CORRECTIONS: 14 retained, 004 and 011 recommended for rejection; strict reasoning 2/14 | `REVIEW.md` `7bd5d927aaeacd0cb87e28c8b5a16d66f73c892072d896940eedb6450646985d` |
| Correction specification | same reviewer | 39 changed leaves: 37 candidate leaves and 2 run leaves | `CORRECTIONS.json` `96a1e6147558e46460ab1fb2a1340ff38c83d5b1dd2659268b61dc00aad31b67` |
| Corrections v1 | correction author | specification applied | `516f359455ddf2082f8bd17a85c4f37035e309f3b5e9e6c0545a0513ddd5535b` |
| Verification of v1 | Opus 5.5 High | one further required fix: the 008 explanation claimed more than five tested temperatures show | `VERIFICATION.md` `cac14401acfd2dcb728f5840d5136b672394f587103b72ca27a360827850ba98`; handoff `3d07aa68a0aa5f8a0bc5d561241083b0d945e894d0a9468f80d60ea82e03c873` |
| One-field amendment | amendment author | `008:explanation` replaced by the verifier's exact 315-character text | `AMENDMENT.json` `1814b7233dc584a689ba692fb730f5d4f08de3ffa14b89cc61c58aad71920b31` |
| Corrected candidates v2 and recheck | amendment author, then Opus 5.5 High | **PASS** for the 14 recommended retained items | v2 `658b0a8b5bac2f285e96970123f4b9c9e1987f2f22e629d1c4583da830224da3`; `RECHECK.md` `a48ddeca401f93563b96df613284c7828d43f2c93a46877d3bdbf7df432f409a`; handoff `REVIEW_HANDOFF.final.md` `0c6ea574cbd1fd2ce849a116195a79567e3b98a769fc6472ff0aff5a3884715f` |

The amendment is a documented departure from `CORRECTIONS.json`: the original
specification's `new` text for `008:explanation` said the optimum lies close to
22 °C, which five tested temperatures do not establish. All other 38 changed
leaves equal the specification exactly. The final text states only that 22 °C is
the best of the tested temperatures, that the exact optimum cannot be located
from five samples, and that survival at 32 °C and death at 42 °C put the upper
limit of tolerance between them.

What the reviewers changed (per the review and specification): -001 terminology
(„популация“ replaced by „растенията на една ливада“) and recall tag; -006
source support and a key of comparable length („Зона на оптимума на фактора“);
-007 narrowed to „една бактерия“ with the four options adapted, recall tag;
-008 wording „От изпитаните температури …“ and „кълнове“; recall tags for -010 and
-012 to -016 (and easy for -010 and -013); -014 locator corrected to „ред 63 (+ надпис ред 66)“;
stale `research.notes` removed from every affected item; `run.stop_reason` and
`run.notes` corrected as above.

The final recheck ran the real candidate validator against an independently
built baseline and all ten neighbour datasets: 0 errors, 0 flags, 16 pending.
The current `quiz_prompt_needs_visual` helper flags 0 of 16 stems. Optional,
non-blocking suggestions the reviewer left **unapplied** by instruction: the -006
`explanation` and -009 `research.notes` wording. The candidate PASS is
scientific and correction verification only; it is not owner approval,
source-use clearance, export authorization or approval of this integrated PR.

## Rejected candidates and their explicit omission

Candidates 004 and 011 were rejected at the approval step with the final
review's reasons, and are **not** exported, not imported and not in canonical
data. They remain in the (ignored) candidate dataset and are listed under
`not_exported` in the provenance file.

- **-004** (classifying a forest fire by its cause): no fetched support, as the
  author's own notes admitted; fire is widely treated as an abiotic factor
  whatever its cause, so the key is contestable and option C is defensible;
  options C and D share „не зависи от причината“, which lets a student
  eliminate them; under B5 it adds no tested fact beyond -001 (abiotic) and -002
  (anthropogenic).
- **-011** (predicting a temperature response from a given optimum and limit):
  under B5 it is the reverse of -008; its enzyme explanation repeats -007; its
  key and explanation say survival is impossible *at* the upper limit, which
  contradicts LibreTexts 1.5 line 16 and -006; and the general claim that
  warming from 15 to 25 °C intensifies life processes is not specified to any
  organism and is false for endotherms. The key was not judged wrong, but fixing
  the item would still leave a B5 repeat.

## Reasoning (B6) decision and the R5 limit

Strict reasoning for the retained set is 2 of 14 (14.3 %), below the topic's
40 % SHOULD signal and below one third. The reviewer recommended, and the
coordinator accepted (`COORDINATOR_DECISION.md`, SHA-256
`bbc78a06a533dce1dcca920898f77ca3f438422ecb98194c2f1472e534226f7d`), recording
this as a **run-level deviation for run 1 only**: run 1 is confined to D1–D3,
whose assessed items are mostly definitions and classification; the
reasoning-centred dimensions (D4 adaptation explanations, D5 impact assessment,
D6 forecasts) are deliberately reserved for run 2. That acceptance applied to
the 14 recommended items once their corrections passed independent verification,
which the final recheck confirmed. The whole-topic 40 % target stays for review
R5 and is not an automatic gate. Padding run 1, relabelling one-fact items as
reasoning and widening the mission were all declined, and the respiratory run's
accepted exception was **not** transferred. For information only: with 14
items holding 2 reasoning, the topic bank reaches 40 % only if run 2 adds about
12 items at roughly 70 % reasoning, or at least 6 items if all of them are
reasoning. Difficulty is 11 easy of 14 (79 %), outside B4's flexible 35–50 %
easy expectation; labels follow actual demand and were not inflated.

## Owner decisions and source-use disclosure

Gabriel (the owner), after the final corrected-hash PASS, wrote: „Approve 14 and authorize export with the source disclosure“
(observed 2026-11-12T06:32:13Z). That approves candidates 001–003, 005–010 and
012–016 (batch numbers 1–14) and the rejection of 004 and 011, and authorizes
export and integration on PR #352 with the original source disclosures
preserved. It does **not** establish permission from any publisher or legal
clearance, and this record makes no such claim.

The disclosures kept in the provenance file and in `run.notes`:

- Each of the four saved OpenStax pages states that the book „may not be used in
  the training of large language models or otherwise be ingested into large
  language models or generative AI offerings without OpenStax's prior written
  permission“ (44.1 line 53, 45.6 line 118, 47.3 line 63, 6.5 line 71 of the
  saved text), followed two lines later by the CC BY-NC-SA statement. The
  OpenStax `robots.txt` disallows `/books/` for the user agent `GPTBot` only; the
  collector used a neutral agent. The reviewer found no copied question text and
  no copied English sentences; the questions are newly written in Bulgarian.
- The closest uses of source wording are translated paraphrases of OpenStax
  examples: the -014 stem (45.6 line 63, the lichen), the -015 options C and A
  (45.6 lines 58 and 69) and the -013 key (45.6 line 68). Attribution in the
  source records and notes is accurate; the original OpenStax sourcing is
  unedited.
- PCC 8.4 is an OpenStax *Microbiology* derivative (CC BY 4.0) hosted by
  LibreTexts; the pages from LibreTexts (1.5, Kappus 5.1, VCE chapter 5) carry
  CC BY, CC BY-NC-SA or derivative licences as listed above.

## Approval copy and export

The exporter refuses pending candidates, so the review fields were set in a copy
of the PASSed v2 file (SHA-256
`b7fd2179ac5d67e337429b3d4a73476224d88c2918e131ee53dd48883da583c5`). It differs
from v2 in exactly 65 leaves: the four review fields of each of the 16
candidates (`status`, `reviewer`, `reviewed_at`, `notes`) and one disclosure
paragraph appended to `run.notes` (the original notes are preserved as history
before it). 14 candidates are `approved`, 2 (-004, -011) `rejected`, none
`pending` or `needs_revision`; reviewer
`biology-g10-ecological-factors-run-1-opus-5-5-independent-review-and-owner-approval`;
`reviewed_at` 2026-11-12T06:36:44Z. Candidate text, options, keys, labels,
sources, order and collection timestamps are identical to v2. The paragraph
records the final verified hash, the 14/2 split, the coordinator's B6 decision,
the owner's authorization and the absence of any publisher-permission or legal
claim.

The approval copy was revalidated against a fresh scratch baseline and the ten
observed neighbour datasets: ok, 16 candidates, 6 sources, 0 errors, 0 flags.
Export: 14 exported, 2 not exported (rejected status), 0 undecided, 0 flags.
Batch source `reviewed:biology:biology-g10-ecological-factors-run-1.ecological-factors`;
numbers 1–14 follow the approved candidate order, mapped in the provenance file
to candidates -001, -002, -003, -005, -006, -007, -008, -009, -010, -012, -013,
-014, -015, -016. Re-exporting reproduces both files byte for byte.

| File | SHA-256 |
|---|---|
| `data/import_batches/subjects/biology/grade-10/biology-g10-ecological-factors-run-1.ecological-factors.json` | `e625b9440947d6b2cbe641051f558c64e6a56db7068ba9868633ffbe88000d38` |
| `data/import_batches/subjects/biology/grade-10/biology-g10-ecological-factors-run-1.provenance.json` | `c5b88ba5dd59d95cd040d5bcbea578b7d35196b4ec9283a58713b1f703e4a378` |

Neighbour datasets used for duplicate checks (read-only, unchanged): human
evolution r1 `ccaa8260ebf7c6836343cf7f5904237a34d9ab3c88a44fc05d0cd4facdeb1ebb`,
Grade 8 cell-and-tissues r1 `a4acbed688c23694377ae629fcaa1c6978f2b8aed855704c2a6133146ba439ca`,
digestive r1 `10b9bab49f2a75ca460f13a95c2f9a7964a0ceba416e320c0f0f220286c9267a`,
embryonic r1 `b9e050fc25ab4b7ee0a4af8f4583c341cdd078457e9c7459f1d081307e447b1e`,
embryonic r2 `8340e7f6b8966ebba9bace83b97b624c85e4344526dc41488af96f74618ac613`,
excretory r1 `84d2b8bdd24eed16a1e47466793dec78ac15b21d4e55fe9869848fb787af606b`,
respiratory r1 `451e2bc36d0da58533fec84a77bf98322d646dd993ca41da9993738bbc4922b1`,
skin r1 `2f76ea833fc473979b60c514aecb424f54ed6e7b482b5d009e346029e3b1728a`,
Grade 9 chemical-composition r1 `f49266777239fd18662e34f241ed494077c9d47e452e6dbb4125af69e0fb4aac`,
supramolecular-complexes r1 `1ea37b000e5a950fa9b050a1406930cb6b8f8290d3a96b12fd630db2302128e1`.

## Import and canonical refresh

Everything was done on disposable databases built by `src.build_database`; no
real `data/questions.db`, vault or provider was used.

- Reviewed importer dry run on a fresh baseline: curriculum 11, section 66 and
  topic 178 reused (nothing created), 14 questions inserted; the baseline's rows
  and sequences were unchanged afterwards. `--apply` (on a copy) inserted 14
  questions, 56 options and 14 question-topic assignments. An exact re-import
  reused all 14 and changed nothing.
- Every old canonical row, id, value, mapping and order was byte-identical
  before and after, in all 27 canonical tables; only `questions`,
  `multiple_choice_options` and `question_topic_assignments` gained rows, and
  only those three sequences moved (1700→1714, 6739→6795, 2049→2063).
- `canonical_export --replace-canonical` (identity guards passed) then
  `--check` changed exactly `questions.jsonl` (+14), `multiple_choice_options.jsonl`
  (+56), `question_topic_assignments.jsonl` (+14, pure additions) and
  `manifest.json` (row counts, max ids, sequences, three hashes). Manifest
  `sources` history is unchanged. New table hashes: questions
  `802b5ac4c8ed49cef370b6497405d9e9904bc407cb80be6f295d5fb8cb2490df`, options
  `b0328297d959d3f35db213de5878c756294acbef3ec57b951332ad7d64d29570`,
  assignments `c3fc076339b6600c2213eb34464bead38134158cacc293d6801340ebd69d94ca`.
- A second fresh build from the updated canonical files equals the applied
  scratch database (rows of all 27 tables, sequences, logical fingerprint),
  passes the builder's validation, and has an empty foreign-key check and
  `integrity_check` `ok`.
- The importer stamps new rows with the wall-clock `created_at`. The reviewed
  rows and assignments that earlier runs added carry `created_at = NULL`
  (`docs/CANONICAL_DATA.md`), so the 14 question rows and 14 assignment rows
  (28 values, no others) were set to `NULL` on the scratch database before the
  export, to keep the canonical bytes independent of when the refresh ran.
- Pinned expectations updated, with unchanged assertion strength, only in
  `tests/test_build_database.py` and `tests/test_canonical_content.py`:
  counts 1709 / 6399, CLI output `questions: 1709`, question sequence 1714,
  next free id 1715, `(MIN, MAX, COUNT)` = `(1, 1714, 1709)`, the joined count
  for `q.id >= 838` 877, and the parity fixture's `6398 rows` (6399 minus the
  deleted option). Fixed historical references (option 6671, question 1683)
  stay.

## Validation

Affected suites, run after the final assertion changes with the
content-visibility venv: import 12, stable ids 35, build database 38, canonical
content 42, subject topic pages 24, auth guard 42, quiz attempt render 86 — 279
tests, no failures, errors or skips. Compile checks, a fresh foreign-key and
`integrity_check` pass, and `audit_dzi_state` on a fresh scratch database reports
`may_2025_v2` as `PART1_IMPORTED` with the pool-health helper at 25 imported, 15
MC-ready, 10 unsupported short-answer and 0 broken MC. The audit command exits 1
because a fresh checkout lacks the untracked official `may_2025_v2_exam.pdf`
(`missing_asset_files: 1`); the baseline produces identical output and status,
so this is disclosed, not a pass and not caused by this change.

A scratch audit probe (not a committed test) used `tests.support` isolation, a
canonical-built database and fixture accounts. It checked that topic 178 holds
exactly ids 1701–1714, that they are quiz-eligible and not visual, that the
section 66 quiz pool is exactly those 14, and that every other topic's question
set equals the baseline; that the anonymous grade, section and topic pages link
to each other and render exactly the 14 ids (and none of the rejected prompts);
and that a teacher-created section-wide assignment was entered by a fixture
student through its public token, rendered all 14 questions, was answered with
10 correct and 4 wrong answers by the stored keys, and scored 10/14 with the
expected stored answers. The numeric `/quiz/<assignment_id>` entry stayed the
same generic 404 as an unknown page (23 requests, 125 assertions).

## Limits

- The database and importer store only the source identifier, question number,
  prompt, options and key. Explanations, reviewed difficulty, source URLs and
  the review record live in the provenance file. The stored `difficulty` of the
  14 rows is the column default `medium` (the page therefore shows every
  question as medium), not the reviewed 11 easy / 3 medium. The application and
  schema are unchanged by this PR.
- Quiz assignments are section-wide; there is no topic-level selection. Section
  66 contains only these 14 questions, so a section quiz uses exactly them.
  `has_section_test` of the section stays 0 and is not changed here.
- Reasoning is 2 of 14; recall dominates by design, with the B6 deviation and
  the R5 carry-over above. Tolerance support comes from open university and
  college texts, not a Bulgarian textbook; Kappus 5.1 is itself a derivative of
  a Wikipedia article. The bacterial specification in -007 rests on
  microbiology evidence and is deliberately not generalized. Heat denaturation
  of enzymes is Grade 9 content: no canonical question or observed neighbour
  tests it, and the Grade 9 chemical-composition run 2 should compare against
  item -007 (batch number 6, id 1706).
- No source was fetched for the effects of light, water, soil, salinity or pH;
  those dimensions stay uncovered and nothing was added to fill them.
- CP1 stays open. W4 and ecological run 2 are not authorized, and the digestive
  bank and chemical-composition supplement keep their existing status.
- No production import, deployment or feature gate was touched; there is no
  production content update. A later production release must validate identity
  safety, high-water marks and references separately. The integrated PR needs
  its own independent review of the exact pushed commit before merge.
