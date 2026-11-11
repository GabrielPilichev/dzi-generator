# Biology Grade 10 — ecological factors, run 1 scope

This execution sheet prepares one content batch under the existing Biology
collection plan. Collection starts after the dedicated branch has a draft PR.

## Target and current baseline

- Subject `biology`, Grade 10, general education, using the reviewed structure
  `data/subject_structures/biology/grade-10.json`.
- Curriculum 11; section `biology-g10-biosphere` (id 66, „Биосфера“);
  topic `biology-g10-ecological-factors` (id 178, „Екологични фактори“).
- Public route: `/subjects/biology/10/biosphere/ecological-factors`.
- Base `25364811792f69e89ac9ae7848eb5bdcf4932d68`, the squash merge of PR #351.
  Canonical topic 178 has zero questions and zero active question assignments;
  section 66 has zero questions. Grade 10 Biology has 36 questions, Biology
  overall 244, and the database reconstruction input has 1,695 questions and
  6,343 multiple-choice options.
- Scope reference: МОН Приложение № 7, Grade 10 general-education programme,
  theme 2.2 „Екологични фактори“,
  <https://www.mon.bg/nfs/2023/12/up_x_bio.pdf>. The saved programme document
  was inspected for its title, appendix and topic text. It supplies curriculum
  scope; the proposed questions are original research-based material.

## Run mission and size

Follow sections A–K of
`docs/reviews/biology_question_bank_collection_plan.md`, especially B, D.4, E,
F, G, H and K. The historical appendix supplies dimension definitions where
the current plan refers to them; current master-table and run limits prevail.

Run 1 covers D1–D3 only:

- D1: classify abiotic, biotic and anthropogenic factors.
- D2: abiotic factors and how a change in one factor's intensity affects an
  organism's life processes. Tolerance covers one factor's range between lower
  and upper limits, an optimum, and wide versus narrow ranges. Theme 2.2 does
  not name tolerance itself; this depth comes from the plan's D2 catalogue,
  supported by programme example lab 4. Every tolerance claim needs fetched
  support. Use plain descriptions; named laws and extra terminology must stay
  within Grade 10 general education under B11 and never become standalone
  eponym-recall items. Grouping organisms by supplied tolerance to one factor
  is permitted, without explaining adaptive features.
- D3: types of biotic relations, such as competition, predation, parasitism,
  mutualism and commensalism, considered as factors acting on an organism or
  population.

Explaining a structural, physiological or behavioural feature as an
adaptation to a factor, including its relative nature, is D4 (run 2), even if
the stem calls it a response. Run 2 also owns assessment of a described
anthropogenic impact (D5) and forecasts of population/community/ecosystem
change from data (D6). Factor classification in D1 and supplied data for a D2
tolerance exercise stay within their run-1 missions. Biocenosis composition
and structure, ecological niche, supra-organism levels, population size,
structure or dynamics, food webs, pyramids, cycles and succession follow the
separate population–ecosystem mission. Evolutionary selection belongs to its
own topic.

The ceiling is **26 raw candidates**, as in the current plan. It is a maximum,
not a target to fill. Stop when distinct in-scope facts are exhausted and
record the dimensions left uncovered. Any positive count that survives review
can be integrated. Report a thin result honestly; do not top up or re-angle
items to reach a number or declare the whole topic bank complete.

The reasoning signal is at least **40 percent**, assessed under B6. A scenario,
new organism name or `classify` tag does not establish multi-fact reasoning.
The reviewer audits each claim and justifies deviations under the plan. The
respiratory run's accepted exception belongs to that run. Difficulty labels
follow actual cognitive demand, with no forced easy/medium/hard distribution.

## Collection output and source controls

Use a fresh Sonnet / High collector, followed by a separate Opus / High review.
Record the actual model and effort. The current user preference governs the
older plan table's Medium suggestion.

Before creation, check the exact output path and run id across the known
worktrees and candidate directories. Candidate files are gitignored. Preserve
and report an existing matching run; never overwrite it or start a competing
run 1. Claims about other environments remain bounded by what was inspected.

Start the dataset with the existing CLI:

```sh
python3 -B -m src.start_question_collection --subject biology --grade 10 \
  --topics biology-g10-ecological-factors \
  --structure data/subject_structures/biology/grade-10.json \
  --max-pages 8 --max-candidates 26 --max-per-source 26 --max-topics 1 \
  --max-runtime-minutes 60 --run-id biology-g10-ecological-factors-run-1 \
  --skills "recall,classify,explain,predict,interpret-data,compare" \
  --output data/question_candidates/biology/grade-10/biology-g10-ecological-factors-run-1.json
```

Account for inspected pages, requests and runtime through the existing budget
helper. Use polite fetching and respect access blocks. Record actual retrieved
pages, source identities, timestamps and fact locators. A cached programme
re-read must be identified as cached evidence, with its original record or
local inspection time explained accurately.

Use authoritative science research, starting with relevant OpenStax *Biology
2e* sections selected from their actual content. Plan F requires fetched
support for **tolerance** and for anthropogenic examples. EEA/UNEP material is
for examples where relevant, within the run-1 boundary. MEDIUM risk makes B8's
HIGH-risk preflight optional; it does not waive the fact-specific requirements.
Record gaps when a necessary source cannot be read. Numbers are used only when
supplied in the stem; clearly distinguish hypothetical exercise data from
empirical biological values and verify the rule supporting the inference.

Write new Bulgarian text-only MC questions, four distinct nonblank A–D options
and exactly one correct key, with concise explanations. Keep classification
`original_from_research`. Every `research.concept` names a distinct fact or
relation as `D<n> · <Bulgarian label>`. Third-party material supplies research,
and the programme supplies scope. Do not copy a question bank, invent citations
or substitute unverified model memory for required source evidence.

Use B5's factual duplicate rule, including factor-to-example re-angles. Keep
options grammatically parallel and plausible, with no key-length or qualifier
cue. Avoid tables, image dependencies and all/none-of-the-above choices. Check
each stem with the actual current `quiz_prompt_needs_visual` helper.

The collector writes only pending candidates and scratch evidence. It does not
approve, export, import, edit tracked files or commit.

## Duplicates, review and checkpoint boundaries

Validate against a fresh disposable canonical database to compare with all
244 existing Biology questions, including the completed human-evolution bank.
Use `--compare` only with actual observed candidate datasets, never import
batches. Read applicable earlier runs and the population–ecosystem neighbour
when present. These neighbours must not share a collection wave under plan G.
Preserve all existing candidate files, including the pending digestive run.

The independent reviewer checks every stem, option, key, explanation, named
fact, skill, difficulty and source locator. Corrections must also update stale
`research.notes` on every affected item, so science and cognitive labels agree.
Check the notes of all recall items and verify cited lines before export.
Record old-to-new fields and obtain independent verification of the corrected
hash. Approval/export follows the existing owner-authorized workflow.

This is the ecological-factors run-1 component of CP1. The digestive bank,
chemical-composition supplement and CP1 verdict retain their existing status.
Finishing this batch does not authorize W4 or ecological run 2.

## Integration after review and approval

Use the existing approved export, reviewed importer and guarded canonical
refresh in the same content PR. Keep the batch and provenance sidecar together
under `data/import_batches/subjects/biology/grade-10/`, with a durable review
record under `docs/reviews/biology/`.

Build a disposable baseline, dry-run the batch, check reuse of curriculum 11,
section 66 and topic 178, then apply only to scratch. Use the stable-ID
allocator and preserve all old rows, IDs, values, mappings and order. Normalize
only new question/assignment `created_at` values to NULL in scratch, following
the canonical convention. Export with `canonical_export --replace-canonical`,
run `--check`, and build another fresh database from the updated files.

For approved count N, prove topic 178 and section 66 each have N visible and
quiz-eligible questions; Grade 10 Biology has `36 + N`, Biology overall
`244 + N`, total questions `1695 + N`, and options `6343 + 4N`. The real public
topic page must render exactly the new IDs and be reached from the grade and
section pages. Use fixture accounts and isolated scratch DB/vault/upload
paths to prove the questions can be selected, rendered, answered and scored
through the existing quiz flow. Keep that audit probe in scratch.

Update the affected current-canonical count/sequence/allocation expectations
in the same two existing files, `tests/test_build_database.py` and
`tests/test_canonical_content.py`, using actual allocated IDs. Preserve fixed
historical fixture IDs and assertion strength. Run the affected reviewed-import,
stable-ID, builder, canonical, subject-page, auth-guard and quiz-render suites;
compile with bytecode in scratch; check foreign keys, integrity, required DZI
readiness on an explicit scratch DB, whitespace and the intended diff.

The existing importer stores default `medium` difficulty; reviewed labels and
explanations live in provenance. Record that behavior. Gabriel stages, commits
and pushes intended files. A separate Opus / High review targets the exact
pushed integrated SHA before a merge decision.

## Preserve clauses

Application code, schema, migrations, scoring, auth, quiz/visual rules,
registry, templates, feature gates, deployment, existing questions and their
identities, structures and order remain outside this batch. No real runtime
database, generated repository vault artifact, credential/provider, VPS,
local server or tunnel operation is included. Production content updates are
a separate operational task.
