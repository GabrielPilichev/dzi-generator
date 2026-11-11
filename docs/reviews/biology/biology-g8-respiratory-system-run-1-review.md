# Biology Grade 8 — respiratory system, run 1: review and integration record

Durable record of how the 17 questions of `biology-g8-respiratory-system-run-1`
were reviewed, corrected, approved and integrated (PR #351,
<https://github.com/GabrielPilichev/dzi-generator/pull/351>). It complements the
scope sheet `biology-g8-respiratory-system-run-1-scope.md`. Hashes are SHA-256;
the review and correction files were scratch files, so the hashes are the
durable identity of what was reviewed.

## Outcome

The empty topic `biology-g8-respiratory-system` (id 160, section 56
`biology-g8-metabolism`, curriculum 9) gains **17 text-only multiple-choice
questions**, all browseable and quiz-eligible. This is the respiratory run-1
component of CP1 only. It is not the whole respiratory bank, not CP1
completion, and run 2 (D4–D6) is still pending.

| | Before | After |
|---|---|---|
| Topic 160 (visible and quiz-eligible) | 0 | 17 |
| Section 56 | 30 | 47 |
| Biology Grade 8 | 126 | 143 |
| Biology overall | 227 | 244 |
| `questions` / `multiple_choice_options` | 1678 / 6275 | 1695 / 6343 |
| `question_topic_assignments` | 2032 | 2049 |

New ids come from the stable-id allocator: questions 1684–1700, options
6672–6739, question-topic assignments 2033–2049. `sqlite_sequence` is 1700,
6739 and 2049. Keys are `C A B D B C D A B C D A C B A D C`. Skills are 15
recall, 1 explain, 1 compare; reviewed difficulty is 15 easy and 2 medium.

## Sources

- Scope: МОН Приложение № 18, theme 2.2 „Дихателна система“,
  <https://www.mon.bg/nfs/2023/12/up_viii_bio.pdf> (official; scope only, not a
  fresh retrieval: the PDF saved by an earlier audit was re-read).
- Research, newly written questions only (`original_from_research`), OpenStax
  *Anatomy and Physiology 2e*, retrieved 2026-11-10 (15:10:22–15:10:33Z):
  - 22.1 <https://openstax.org/books/anatomy-and-physiology-2e/pages/22-1-organs-and-structures-of-the-respiratory-system>
  - 22.2 <https://openstax.org/books/anatomy-and-physiology-2e/pages/22-2-the-lungs>
  - 22.3 <https://openstax.org/books/anatomy-and-physiology-2e/pages/22-3-the-process-of-breathing>
  - 22.4 <https://openstax.org/books/anatomy-and-physiology-2e/pages/22-4-gas-exchange>
- Collector: `claude-sonnet-5-5 / High`; run started 2026-11-10T15:09:57Z, 6 of 8
  pages, 17 of a ceiling of 20 candidates, `stop_reason: topic_complete`.
- Review retrievals outside the collected sources: a Bulgarian Wikipedia page on
  the larynx, used only as terminology-usage evidence, and an NCBI page that was
  blocked by reCAPTCHA, was not bypassed and supplied no fact. No Bulgarian
  school textbook was read; textbook wording, including „парциално налягане“,
  stays unverified.

## Review and correction chain

| Step | Reviewer / author | Result | SHA-256 |
|---|---|---|---|
| Raw candidates | collector | 17 pending | `451e2bc36d0da58533fec84a77bf98322d646dd993ca41da9993738bbc4922b1` |
| Independent review | Opus 5.5 High | NEEDS CORRECTIONS: all 17 keys correct; wording fixes (gas gradient as „съдържание“ in -014/-015, a false premise in -013, option and explanation fixes in -002, -004, -008, -010, -016, -017); labels downgraded to recall/easy for -004, -008, -010, -013, -015; reasoning 2/17 (11.8 %) | report `79ce2a328a8ee928aa20a924ff7a1619abf275c1229cb85103ede392824f076a` |
| Corrections v1 | Sonnet High | applied | `7daac5885fc1f3bb0451bf284a2301842ba945a1dc45dbef272fd89b39009855` |
| Corrections v2 and pupil-facing verification | Sonnet High, then Opus 5.5 High | every prompt, option, key, explanation and label accepted for all 17; only four `research.notes` provenance findings remained | v2 `25521fc3d8d256864b68e87c5eaca69b4b339e3b63929e04d681642aad3721e1`; report `7ea5034e83b588332b9378d19b5e96abd956be8f12a9f8df13c82eccb5e84c7f` |
| Notes corrections v3 and verification | Sonnet High, then Opus 5.5 High | **PASS** for the exact v3 content; v3 differs from v2 in exactly 4 `research.notes` leaves (-004, -008, -010, -017) | v3 `e481c0cc99e04498f1aa0b83fe5b489572e70ba3ba2fc23e6f6f99a7e1087dcb`; PASS report `89da0a19d57d1b49140b98e4c366793bc42a42fcc853a173ace07bfa7a1fe572` |

The PASS review ran the real candidate validator against the independently
built canonical baseline from the same commit, which it reused read-only (the
PASS reviewer did not rebuild it), and the six neighbour datasets: 0 errors,
0 flags. The current
`quiz_prompt_needs_visual` helper flags 0 of 17 stems. Optional, non-blocking
suggestions left unapplied: a locator tweak for -015, labelling -017's last
citation as context, and wording of -004 option D.

## Owner decisions

Gabriel approved the candidate set and explicitly chose to keep all 17 and to
accept the documented reasoning exception: strict reasoning is 2 of 17 (-011,
-016), below the plan's 35 % SHOULD signal, because further D1–D3 reasoning
items would be recall wrapped in an artificial scenario (stop conditions K5 and
K3). The deficit is carried to review R5 as information, not as a quota. Run 2
(D4–D6), the digestive and chemical-composition work, CP1 and W4 remain with
their existing plan boundaries.

## Approval copy and export

The exporter refuses pending candidates, so the review fields were set in a copy
of the PASSed v3 file (SHA-256
`3f611c87f06a40e6c5f259bc67d4eff6abb090ff73708ac3deed0d651df78846`). It differs
from v3 in exactly 69 leaves: the four review fields of each of the 17
candidates (`status: approved`; reviewer
`biology-g8-respiratory-run-1-opus-5-5-independent-review-and-owner-approval`;
`reviewed_at` set at integration, 2026-11-11T09:11:58Z; a note naming the PASS
hash and the owner's approval), and one metadata-only disclosure appended to
`run.notes`. Pupil-facing fields, labels, ids, sources and collection
timestamps are identical.

Disclosure kept in the provenance `run.notes`: the collector ledger's own
`finished_at` is 2026-11-10T15:14:59Z, the manifest's is 2026-11-10T15:16:38Z.
The later value follows the disclosed option-length revision of 11 candidates
(file times about 15:16:23Z; prompts, keys, facts, limits and pages unchanged).

Export, validated against the baseline and six neighbours: 17 exported, 0 not
exported, 0 flags. Batch source
`reviewed:biology:biology-g8-respiratory-system-run-1.respiratory-system`;
question numbers 1–17 map to candidates `…-run-1-001` … `-017` in order.

| File | SHA-256 |
|---|---|
| `data/import_batches/subjects/biology/grade-8/biology-g8-respiratory-system-run-1.respiratory-system.json` | `a53620b802e634d6f9a1ff8c17571d4de02da4f183a2a3bf5f42c205dceb643b` |
| `data/import_batches/subjects/biology/grade-8/biology-g8-respiratory-system-run-1.provenance.json` | `a17e053bff7bb1fbf8962ac6b1a8f9a78a5327084a066ea408bd813dda4913c1` |

Neighbour datasets used for duplicate checks (read-only): cell-and-tissues r1
`a4acbed688c23694377ae629fcaa1c6978f2b8aed855704c2a6133146ba439ca`, digestive r1
`10b9bab49f2a75ca460f13a95c2f9a7964a0ceba416e320c0f0f220286c9267a`, embryonic r1
`b9e050fc25ab4b7ee0a4af8f4583c341cdd078457e9c7459f1d081307e447b1e`, embryonic r2
`8340e7f6b8966ebba9bace83b97b624c85e4344526dc41488af96f74618ac613`, excretory r1
`84d2b8bdd24eed16a1e47466793dec78ac15b21d4e55fe9869848fb787af606b`, skin r1
`2f76ea833fc473979b60c514aecb424f54ed6e7b482b5d009e346029e3b1728a`.

## Import and canonical refresh

Everything was done on disposable databases built by `src.build_database`; no
real `data/questions.db`, vault or provider was used.

- Reviewed importer dry run: curriculum, section and topic reused (9 / 56 /
  160, nothing created), 17 questions inserted; the dry run left rows and
  sequences unchanged. `--apply` inserted 17 questions, 68 options and 17
  question-topic assignments. An exact re-import reused all 17 and changed
  nothing.
- Every old canonical row, id, value, mapping and order was byte-identical
  before and after; only `questions`, `multiple_choice_options` and
  `question_topic_assignments` gained rows, and only those three sequences
  moved.
- `canonical_export --replace-canonical` (identity guards passed) then
  `--check` changed exactly `questions.jsonl` (+17), `multiple_choice_options.jsonl`
  (+68), `question_topic_assignments.jsonl` (+17) and `manifest.json` (row
  counts, max ids, sequences, three hashes). Manifest `sources` history is
  unchanged. New table hashes: questions
  `4d91755a98e7d1ff9af0103c8eefdc3f397e53a9c672e6842572db53938b323a`, options
  `e55bef1e85cc54047389ace45e402fefa7d9679f7a5f9b93d3d18665902f7b87`,
  assignments `f4afcb3c5af205baa6e9f9380b429a87c85144ecedede8203576315ea7e34177`.
- A second fresh build from the updated canonical files equals the applied
  scratch database, passes the builder's validation, and has an empty
  foreign-key check and `integrity_check` `ok`.
- The importer stamps new rows with the wall-clock `created_at`. All 846
  earlier reviewed rows and assignments carry `created_at = NULL`
  (`docs/CANONICAL_DATA.md`), so the 17 question rows and 17 assignment rows
  were set to `NULL` on the scratch database before the export, to keep the
  canonical bytes independent of when the refresh ran.
- Pinned expectations updated, with unchanged assertion strength, only in
  `tests/test_build_database.py` and `tests/test_canonical_content.py`:
  counts 1695 / 6343, CLI output `questions: 1695`, question sequence 1700,
  next free id 1701, `(MIN, MAX, COUNT)` = `(1, 1700, 1695)`, the joined count
  for `q.id >= 838` 863, and the parity fixture's `6342 rows` (6343 minus the
  deleted option). Fixed historical references (option 6671, question 1683)
  stay.

## Validation

Affected suites, run with the content-visibility venv: import 12, stable ids
35, build database 38, canonical content 42, subject topic pages 24, auth guard
42, quiz attempt render 86 — 279 tests, all passing. Compile checks and a fresh
foreign-key and `integrity_check` pass; `audit_dzi_state` on a fresh scratch
database reports `may_2025_v2` as `PART1_IMPORTED`, and the pool-health helper
keeps 25 imported, 15 MC-ready, 10 unsupported short-answer and 0 broken MC.

A scratch audit probe (not a committed test) used `tests.support` isolation and
fixture accounts. It checked, on the canonical-built database, that topic 160
holds exactly the 17 ids and that they are quiz-eligible and not visual; that
the anonymous topic page renders exactly those ids; that grade and section
pages link to it and show 47, 143 and 244; and that all other topic id sets
equal the baseline. A teacher created a section-wide assignment; a student
entered through the public token, rendered the attempt with all 17 questions,
submitted 12 correct and 5 wrong answers, and the result showed 12/47 with the
expected stored answers. The numeric `/quiz/<assignment_id>` entry stayed a
generic 404 (24 requests, 244 assertions).

## Limits

- The database and importer store only the source identifier, question number,
  prompt, options and key. Explanations, reviewed difficulty, source URLs and
  the review record live in the provenance file. The stored `difficulty` of the
  17 rows is the column default `medium` (the page therefore shows every
  question as medium), not the reviewed 15 easy / 2 medium.
- Quiz assignments are section-wide; there is no topic-level selection, so the
  17 questions mix with the 30 existing questions of the section.
- Reasoning is 2 of 17; recall dominates by design. Facts rest on OpenStax and
  the programme; Bulgarian textbook wording and the NCBI vocal-cord point stay
  unverified. Image-dependent, control-of-breathing, air-composition, disease
  and gas-transport content is not covered.
- No production import, deployment or feature gate was touched. A later
  production release must validate identity safety, high-water marks and
  references separately. The integrated PR needs its own independent review of
  the exact pushed commit before merge.
