# DZI Part 1 simulation submit and grading scope

Implement C3.3 from the reviewed C3.2 merge on
`feature/dzi-part1-submit-service` at base `a4df5e6edcda393a3f17bfe06b010a34f11cc0c6`. Follow
[the reviewed design](../DZI_PART1_SIMULATION_DESIGN.md),
sections 5, 6.4–6.7, 7, 9 and 10. The C3.2 service already handles
current pools and starting. The first writer and paired clock
recovery are in place.

## Change boundary

- `web/services/dzi_part1.py` for the dormant submit service and
  scoped request boundary. Reuse the saved attempt, presentability,
  quiz matcher and lifecycle primitives without changing their
  established callers. A separate module is unnecessary unless a
  concrete service dependency requires it.
- New `tests/test_dzi_part1_submit_service.py` and
  `tests/test_dzi_part1_submit_request.py` for temporary database,
  clock, and in-process request tests.

Preserve migration 027, canonical content and import tools, pool and
start behavior, shared quiz grading, C2, lifecycle and its operator
command, and existing HTTP/rendering behavior. Keep result loading,
routing, templates, browser code, feature gates and activation in
their later slices. No real database, live clock, server, tunnel,
VPS, provider, credential, generated vault, or deployment operations.
Do not stage or commit author changes.

## Bound the request before parsing

Use Flask/Werkzeug's `request.content_length` and the approved
64 KiB body limit. Missing usable length gives 411 and a declared
length above the limit gives 413, both before reading the form or a
database write. Set the per-request cap, read once into Werkzeug's
cache, and require the received bounded byte count to equal the
declared length before reading `request.form`. For a mismatch return
400 without parsing, grading or writing any state. A request with
no fields that survived the checks follows the normal empty outcome.

Exercise this helper in-process without registering production
routes or starting a server. Use the reviewed cases in section 9,
including terminated WSGI streams with short and long delivery, no
length or chunked encoding, an oversize declaration, exact 64 KiB,
and malformed length as interpreted by Werkzeug. At a declaration
of exactly 65,536, a terminated stream delivering more may leave the
bounded prefix equal to declared length. Assert the consumed and
parsed prefix and that excess bytes are never graded or stored.
Do not claim over-delivery past the cap is always detected.

## Submission and persisted scores

Use an account principal and the path attempt id. Validate the
connection and check the active owner under the lifecycle writer
lock. Find the row by id and owner. A foreign or absent row is
`not_found`. Only one admitted transaction can store a submission.

Return a stable outcome for submitted, expired, abandoned and
future-start rows. Before parsing a nonterminal plan, use the stored
time and cutoff. An at-cutoff or later submit on an unmarked row
marks it expired once and stores no answers. Keeping an earlier
abandonment depends only on the row, even if the wall clock later
passes its false stamp. Grace is exclusive at the cutoff. Rows
already stored never recalculate it from a grace constant.

Use the saved 25 ids and slot format, the same current per-item
readiness as resume, and matching current positive whole part
weights. Each submitted name must match the design's stored
question and part pattern and that actual planned item. Ignore
out-of-plan ids, unknown fields and non-parts. Duplicate fields are
unanswered. A recognized short-answer value longer than 200 Python
code points or containing NUL refuses the submission with no
write. No usable answers leaves the attempt open. Unpresentable
items do not get graded or contribute to the effective maximum.
No fields from the request control owner, plan, seed, timestamps,
weights or marks.

Use the current stored MC option key and established short-answer
normalization, accepted-answer and ordered/order-independent
matching rules. Persist distinct rows for the MC slot or each
short-answer part. Take MC points from saved slot format and
short-answer points from the eligible current part weights, whose
sum agrees with the saved slot points. Keep awarded points,
possible points, binary verdict and grader version coherent, and
persist the sum for the score and effective maximum. Unanswered
presentable parts retain their zero verdict with possible points.
Do not store correct keys, accepted answers, client timestamps,
or unrelated text as answers.

Use one lifecycle now and call `admit()` once before any writes.
Discard no-write outcomes, and do not invoke `check_factual()` on
C3 data. Guard final updates by id, owner and open state, and verify
the row count. Failures roll back both answer rows and terminal
state plus normal clock admission. Verify idempotent retry and
separate-connection races between submit and start. Later content
or blueprint changes cannot alter a persisted result.

## Disposable verification and known clock limits

Test both kinds, normalization, partly answered attempts and
effective scores, source and presentability drift, malformed
snapshots, recognized vs unknown fields, invalid option keys,
duplicates, part membership and exact input bounds. Exercise
submission before and at the deadline, cutoff-minus-one and
exact cutoff. Check clock refusal and high-water behavior with
the real fixture clock. Run the two reviewed fault and recovery
sequences through the existing CLI entry point on disposable
databases and confirm refused abandoned submit with no answer
rows. Reproduce the documented undetected-reboot late-submit
case as a limitation, with the same-boot refusal controls and
the already reviewed two-step case as context. Keep actual host
boot and NTP inputs, policy decisions and activation separate.

Run focused service, direct-fixture lifecycle, grading, and
migration regressions, compile and whitespace checks, and
scratch FK/integrity. Use an in-memory or explicit scratch app
DB before imports. Existing helpers can resolve the default
runtime filename during setup. Use the prior review's scratch
path guard or comparable test-only protection to intercept that
metadata lookup before any underlying syscall, without changing
tracked helpers. Avoid test modules that require operating on
the excluded real or default runtime path, and disclose any
guard-blocked modules and environment limits. Do not list ignored
files. No full-suite repeat without a specific finding.
