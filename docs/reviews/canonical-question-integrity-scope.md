# A1 — canonical approved-question integrity guard

Gabriel approved A1 from the user-supplied next-steps review dated 2026-11-11.
This scope authorizes a tests-only guard. Other review tracks, roadmap
reordering, activation, deployment and content repairs remain separate.

Base: `2876c2bcc6a40712f47bc6aa085a3c1172c074a1` (merged PR #352).
Branch: `test/canonical-question-integrity`. The base contains 1709 questions,
6399 options and 1690 questions passing the approval predicate; these totals
are context, not new pinned assertions.

## Guard and known exceptions

Build disposable canonical state through `tests/support.py`. Scan every
question passing the actual `web.services.quiz.QUIZ_APPROVED_FILTER`, including
approved questions that are currently ineligible for quizzes. Do not substitute
the quiz pool or a quality-score-only approximation for the approval predicate.

- `multiple_choice` and `true_false`: exactly one correct option.
- `multiple_choice`: exactly four options.
- `fill_in`: at least one sub-answer, and every stored `correct_answer` is
  nonblank after trimming whitespace. This covers the existing quiz rule's
  requirement for answers to all blanks; it adds no visual or grading policy.

The verified base has 52 approved questions violating these invariants:

| Invariant | Exact known shape and question ids |
|---|---|
| Correct option | 329: `true_false`, two options, zero correct |
| MC option count | Three options, one correct: 268, 326, 336, 354, 389, 392, 393, 394, 407, 452, 471, 485, 486, 487, 492, 493, 499, 500, 504, 530 |
| MC option count | 400: two options, one correct |
| Fill-in answers | Zero sub-answers: 37–46, 81–90, 171–180 |

The review's description of all 21 MC cases as three-option questions was
incorrect: question 400 has two. Unapproved 496, 516 and 522 are outside this
scan. Duplicates and missing topic mappings are separate findings, not new
invariants in A1.

Keep a small explicit exception map with reasons and exact violation details.
Do not generate expected exceptions from the database at test runtime, exempt
whole sources, or exempt every defect on a listed id. Unknown violations, an
extra defect or a changed defect shape on a known id must fail with useful
question/rule diagnostics. Resolved exceptions require deliberate maintenance.

A2 will separately quarantine 329 without guessing its answer. When it stops
passing the approval predicate, retire its active approved-pool exception in
that same data PR; retain the unresolved source defect and quarantine reason
in that PR's record. Unapproval must not be described as repairing the key.
A1 itself changes no approval state or content.

## Implementation and verification

After the scope is committed and a dedicated draft PR exists, use a fresh
Sonnet / High session. The implementation may add only
`tests/test_canonical_question_integrity.py`; keep production modules, test
support, canonical data and existing fixtures unchanged.

Meaningful temporary-DB mutation checks must show that a newly malformed
approved question fails each invariant, worsening a known exception fails,
a valid new question passes, and a malformed unapproved question is excluded.
Exercise the actual predicate's non-AI branch too; low quality alone does not
exclude a non-AI question. Keep the canonical source and pristine baseline
unchanged while exercising mutations.

Run the new module and affected canonical/builder suites, plus the required
auth/quiz smoke checks. Compile into scratch, check whitespace and the intended
diff, and use explicit disposable DB paths for FK/integrity/DZI checks. Run the
full suite only if a new failure or unresolved concern justifies it. Disclose
the existing DZI audit exit 1 caused by the missing untracked PDF accurately.

Leave edits unstaged and uncommitted. Handoff: actual PR number, branch and
HEAD, exception details, mutation-check evidence, test results and limitations.
Gabriel commits and pushes; a separate Opus / High review targets the exact
pushed commit before a merge decision.

No runtime `data/questions.db`, repository generated vault files, providers,
VPS, server or tunnel operations are included. This scope changes no feature
gate, schema, scoring, auth, teacher workflow or roadmap sequence.
