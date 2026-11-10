# Biology Grade 8 — respiratory system, run 1 scope

This execution sheet scopes one small content batch. It adds no questions,
approves no candidate, and does not amend the Biology collection plan.
Collection starts only after the dedicated branch has a draft PR.

## Target and baseline

- Subject `biology`, Grade 8, general education, framework-plan dependent as
  recorded by the existing structure review.
- Curriculum id 9; section `biology-g8-metabolism` (id 56,
  „Обмяна на веществата“); topic `biology-g8-respiratory-system` (id 160,
  „Дихателна система“).
- Public route: `/subjects/biology/8/metabolism/respiratory-system`.
- Fresh canonical-build baseline at
  `1e4e8593ccea2cb9deec5bc08aaf4127676bb8b5`: topic 0 visible
  and 0 curriculum-quiz-eligible questions; section 30; Grade 8 Biology 126;
  Biology overall 227.
- Existing structure: `data/subject_structures/biology/grade-8.json`.
- Scope reference: МОН Приложение № 18, theme 2.2 „Дихателна система“,
  <https://www.mon.bg/nfs/2023/12/up_viii_bio.pdf>. It supplies curriculum
  scope, not official exam questions or a claim that every framework plan
  teaches the subject in Grade 8.

## Collection mission

Follow sections A–K of
`docs/reviews/biology_question_bank_collection_plan.md`, especially B1–B12,
D.2, E, F, G and the W3/CP1 schedule. The historical appendix applies only
where that plan explicitly references it.

Run 1 covers D1–D3 only: airways and lungs, breathing movements, gas exchange,
and structure–function relationships. Air composition, diseases, smoking,
air-quality effects and vital-capacity readings belong to run 2. Blood gas
transport belongs to the cardiovascular topic. Image-dependent outcomes stay
uncovered until a separately planned image pass.

The operational ceiling is **20 raw candidates**, below the plan's run-1
ceiling of 26. This is a ceiling, not a quota or an amendment. Do not pad a
run or top it up to reach a number. Record any uncovered dimensions and the
stop reason; do not call the entire respiratory bank complete. If no
candidates survive review, stop without integrating an empty batch.

Questions must be newly written Bulgarian text-only multiple choice, with
four A–D options and exactly one correct answer. Record one named fact or
relation per candidate in `research.concept`; reject re-angles of the same
fact. The plan's reasoning signal is at least 35 percent, with deviations
justified by the independent reviewer rather than inflated skill labels.
Do not use tables, image dependencies, „всички изброени“ or „нито едно от
изброените“. Do not require memorised alveolus counts or lung surface areas.

Start the run with the existing tool rather than hand-writing its limits:

```sh
python3 -B -m src.start_question_collection --subject biology --grade 8 \
  --topics biology-g8-respiratory-system \
  --structure data/subject_structures/biology/grade-8.json \
  --max-pages 8 --max-candidates 20 --max-per-source 20 --max-topics 1 \
  --max-runtime-minutes 60 --run-id biology-g8-respiratory-system-run-1 \
  --skills "recall,explain,compare,sequence,predict,apply-health,interpret-data" \
  --output data/question_candidates/biology/grade-8/biology-g8-respiratory-system-run-1.json
```

Check for an existing dataset with this run id before creation. Preserve and
report a collision; never overwrite or create a competing run 1. The
candidate directory is ignored by Git, so absence from a clean worktree is
not proof that no earlier collection exists elsewhere.

Use Sonnet / High for collection and Opus / High for independent review,
following the current coordination instructions. The collector remains a
collector: it does not approve, export, import, edit tracked files or commit.

## Sources, duplicate checks and review

Use the programme for scope and authoritative science references for
correctness. Record the actual pages used from the respiratory chapter of
OpenStax *Anatomy and Physiology 2e*, with retrieval times and the facts they
support. The classification is `original_from_research`; neither a programme
citation nor a government publisher makes these official exam questions.
Record the actual collector model in provenance.

Respiratory is MEDIUM risk, so B8's HIGH-risk preflight is not mandatory.
B7 still applies to every health or numerical claim: it needs a fetched
authoritative source. Keep run-2 claims out of this batch rather than
broadening the mission. Do not fall back to model memory for a claim that
requires source verification.

Validate duplicates against a fresh canonical temporary database using
`validate_question_candidates --db`. This includes existing excretory,
cell-and-tissues, skin and embryonic material. Use `--compare` only for
candidate datasets that actually exist; it does not accept import batches.
Also review factual re-angles and the boundary between gas exchange and
blood transport. The cardiovascular neighbour remains outside this run.

Use the current `quiz_prompt_needs_visual` helper to check every proposed
stem, rather than copying a list of keyword patterns. The batch must have no
visual dependency. Do not change quiz rules or reword existing Physics
questions as part of this work.

An independent reviewer checks every stem, option, key, explanation, source,
scope boundary, named fact and difficulty label. Corrections get a separate
verification before approval. Gabriel reviews and approves the resulting
candidate set before export. This is the respiratory run-1 component of CP1;
it does not complete CP1 or authorise W4, and it does not replace the pending
digestive and chemical-composition work.

## Integration after candidate approval

Use the existing export/import/build workflow. Keep the batch and provenance
sidecar together under `data/import_batches/subjects/biology/grade-8/`, and
record the scientific review and corrections under `docs/reviews/biology/`.
Export only the approved candidates.

Build a disposable database from migrations plus canonical data. Dry-run the
reviewed importer, check that the existing curriculum, section and topic are
reused, then apply only to that temporary database. Preserve every existing
row, id, answer, mapping and order. Export through the guarded
`canonical_export --replace-canonical`, then run `canonical_export --check`.
**The same content PR includes the canonical refresh** so a fresh build can
browse the new questions; this follows `docs/CANONICAL_DATA.md`.

Update only the affected canonical count expectations in
`tests/test_build_database.py` and `tests/test_canonical_content.py`:
questions `1678 + N`, options `6275 + 4N`, where N is the approved count.
No application-code, schema, scoring, auth or deployment change is included.

Build another fresh database from the updated canonical files and verify:

- Topic 160 has exactly N visible and N quiz-eligible new questions, and the
  real topic route renders precisely their ids.
- A test-only assignment/attempt can select and render the new questions and
  reach a result using fixture accounts and disposable state. Keep any
  test-generated quiz/vault files under scratch, never in the repository.
- Section 56 has `30 + N`, Grade 8 Biology `126 + N`, and Biology overall
  `227 + N`; no unrelated topic changes.
- Existing ids, content and mappings remain unchanged; new ids come from the
  repository's stable-id allocator.
- Foreign-key checks are empty and integrity checks return `ok`.
- The importer, builder, canonical, topic-page, required auth-guard and
  quiz-render suites pass. Compile the edited test files with bytecode output
  directed to scratch, and inspect whitespace and the intended diff.

Report actual checks and skipped tests without counting assertion loops as
unittest cases. Keep author evidence distinct from the final independent
review of the exact pushed commit. Gabriel stages, commits and pushes only
the intended files; merge remains a later approved action.

## Boundaries

No real database, VPS, local server/tunnel, feature gate, registry, template,
existing question, Physics/IT/DZI content or real/generated repository vault
artifact changes.
No collector may approve/export/import. No canonical refresh occurs before
the reviewed set is approved. No production import or deployment follows this
PR; a future production release must separately validate identity safety,
high-water marks and references. Current row maxima do not establish that
production ids are safe to reuse.
