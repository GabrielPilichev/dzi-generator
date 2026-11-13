# A2 — quarantine question 329 without guessing its key

Gabriel authorized A2 after PR #353 merged. Base:
`83741a24cc56dcf0fcf3c181441fe4d6d89c31b1`.
Branch: `content/quarantine-question-329`.

## Observed defect and approved change

Canonical question 329 is a `true_false` item from
`classroom_tests_8_12_2026`. It has `is_ai_generated = 1`,
`quality_score = 1.0`, and two options (911 and 912), both with
`is_correct = 0`. It currently passes the application's approval predicate.
Its missing answer key remains unresolved; this change supplies no answer.

On a disposable database built from the exact base, change only question
329's `quality_score` from `1.0` to `0.0`. Preserve its id, prompt, source,
type, mappings, timestamps, options and all other values. Do not delete it,
guess a key, change its AI flag, or repair its topic mapping in this task.
Export through the identity-guarded workflow in `docs/CANONICAL_DATA.md`;
do not hand-edit canonical JSONL or use a real runtime database.

Retire 329's active entry from the A1 approved-question exception map in the
same PR. This is quarantine by approval state, not repair of the missing key.
This scope and the PR record retain the unresolved defect and its reason;
the historical A1 scope remains unchanged. Reapproval requires a separately
reviewed correct key and content change.

## Allowed implementation files

After this scope is committed and a dedicated draft PR exists, a fresh
Sonnet / High session may change only:

- `data/canonical/questions.jsonl`: only 329's quality score.
- `data/canonical/manifest.json`: only the corresponding questions hash.
- `tests/test_canonical_question_integrity.py`: retire the active exception,
  update its count wording, maintain mutation coverage that currently uses
  329 as an approved exception, and add a focused quarantine regression.

Keep the guard's predicate and invariants intact. Preserve equivalent
coverage for changed and resolved exceptions. Demonstrate that the stored
329 still has no key and is excluded from approval, and that changing only
its quality back to `1.0` on a scratch clone triggers a new violation.
Do not reapprove it in canonical merely to preserve an obsolete test fixture.

No application code, shared test support, schema, scoring, auth, importer,
other content, fixture identities or feature gates may change. No new global
count assertions are needed. The implementation leaves edits unstaged and
uncommitted for Gabriel.

## Verification and handoff

Verify every other canonical row, value, id and ordering is unchanged, with
all option rows and sequences preserved. Expected context: stored totals
remain 1709 questions and 6399 options; approved questions change 1690 to 1689
and active guard exceptions 52 to 51. Verify the actual approval predicate,
canonical export/check, fresh rebuild, FK/integrity and the unchanged DZI
pool. Do not claim a quiz-pool reduction: true/false was already ineligible.

Use a scratch Flask test client with isolated state to compare the relevant
public browsing question ids before and after quarantine: the only removed
question is 329. Do not start a server or probe the live site.

Run the integrity module, affected canonical/builder suites and required
auth/quiz smoke checks; compile to scratch and review whitespace/diff. Keep
testing proportional. The known missing-PDF DZI audit exit 1 is a disclosed
baseline limitation, not a new regression. No PDF download is requested.

Report the actual PR number, branch/HEAD, exact changed fields, preserved ids,
test and probe results, and remaining source defect. Gabriel commits/pushes;
a separate fresh Opus / High session reviews the exact final pushed SHA.
Merge requires Gabriel's go-ahead.

No runtime `data/questions.db` or sidecars, generated vault files, real
credentials/providers, VPS, deployment, server or tunnel operations are
authorized. Canonical changes affect future builds; an existing site's
database is not updated by merging this PR.
