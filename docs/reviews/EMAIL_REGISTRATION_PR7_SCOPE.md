# Email registration, PR 7 — teacher quotas and gated teacher registration: scope

Design: `docs/reviews/EMAIL_REGISTRATION_DESIGN.md` (sections 7.1, 7.3 and 9, PR 7; decisions D3, D4, D5, D14).
Status and results: `docs/reviews/EMAIL_REGISTRATION_PR7_STATUS.md`. Test plan: `EMAIL_REGISTRATION_TEST_PLAN.md`.
This is the "short scope for PR 7" the design asks for (section 9, row 7; open item 1a).

Branch `feature/teacher-registration-quotas`, started from `fd44c8f` (on top of `ede0c0b`, PR 6, #340). Implemented in `b15d02b`
(PR #341). **Review history:** an independent review of exactly `b15d02b` returned **REQUEST CHANGES** (three findings); the fixes were committed as
`3537f4f3ad9a28ee9aa147a6d93af4e6863da1ac`; an independent review of exactly `3537f4f` returned **REQUEST CHANGES** (two P2 findings). Both are answered
in "Review findings" below. **The fixes of the second review are uncommitted in the working tree and await independent review.** Nothing is approved.

## What PR 7 does

1. **Assignment quotas.** Two per-account limits on creating assignments, enforced inside the one function that
   inserts an assignment (`quiz_create_assignment`), under SQLite's writer lock, for every creation path:
   * at most **20 creations in a rolling 24-hour window**;
   * at most **10 open assignments**.
   Closing an assignment frees an open slot. It never gives back a creation: creations are counted in a ledger of their
   own that no lifecycle operation touches (migration `024_assignment_creations.sql`).
2. **Gated teacher registration.** `/register/teacher` (first and last name, email, exact password, school, one to three
   subjects with the approved „Друг предмет“), behind `registration.TEACHER_ACTIVATION`. A correct code creates the
   `users` row with role `teacher`, the teacher profile and its subjects in one transaction. Nothing is granted on
   submitted data alone: the role is the route's literal.
3. Documentation, tests, and the two carried corrections to the PR 6 status text.

## Approved decisions (input)

Chronology. The first six bullets were approved by Gabriel **before** implementation. The last two (admin accounts, no development switch) were approved by
Gabriel **after** the review of `b15d02b` and before the commit `3537f4f`; they were proposed as I6 and I11. Nothing was approved after the review of `3537f4f`.

* 20 creations per teacher in a rolling 24 hours; 10 open assignments per teacher.
* Closing frees an open slot and does not refund creation allowance.
* Enforced atomically across creation paths.
* A verified teacher registration receives role `teacher` only when the teacher gate permits activation.
* Production registration, teacher, profile and Google gates stay **off**.
* **Admin creations are recorded and exempt from refusal** (approved by Gabriel after the review of `b15d02b`; it was proposed as I6).
* **No development switch for the teacher gate** (approved by Gabriel after the review of `b15d02b`; it was proposed as I11).

Everything else under "Interpretations" is **not approved**: in particular the exact boundary semantics (I1), held and deadline behaviour of
the open count (I2), the refusal precedence (I3), the accounting rules and status (I12), and every timestamp rule of I13 (date-only mapping, the
2000-01-01 cutoff, the one-day future tolerance, the rounding of fractions). **No recovery policy for lost history is approved** (see I12).

## Creation paths (inventory)

`web/services/quiz.py: quiz_create_assignment` holds the only `INSERT INTO quiz_assignments` in `web/`, `src/` and the
canonical loader. Its callers:

| Path | Caller |
| --- | --- |
| New test, multiple choice | `POST /teacher/new` → `_create_assignment_with_deadline` |
| New test, mixed (closed + open questions) | `POST /teacher/new` → `_create_assignment_with_deadline` |
| Copy of a test | `POST /teacher/assignment/<id>/duplicate` → `_duplicate_assignment_with_deadline` |

The DZI training generator (`/teacher/dzi-training`) does not create assignments. `src.manage_assignment_tokens` and
`src.manage_lifecycle` update existing rows only. The importers create no assignments. A test pins that the insert
exists in one place only.

## Interpretations (judgement; each is a place Gabriel may overrule)

These are not approved decisions. They are the narrowest reading of the approved text that can be implemented, and each is
written down so that it can be changed.

* **I1. Window.** Rolling means the half-open interval (now − 24 h, now]. A creation exactly 24 h old no longer counts; one a
  second younger does. The 20th creation is allowed, the 21st refused. `now` is the lifecycle writer clock, sampled
  **after** the lock is held.
* **I2. Open.** "Open" is the lifecycle state `open` for the owner's rows: not closed manually, deadline not passed, no
  expiry or grace-end marker. A deadline that passes frees the slot just as a manual close does. A **held** assignment
  (`lifecycle_hold`) that is otherwise open **counts as open**, because the teacher cannot close it and it will be served
  again when the hold clears. The 10th open assignment is allowed, the 11th refused.
* **I3. Which limit is reported when both are exceeded.** The daily limit, because closing something cannot help.
* **I4. Whose creations count.** The account named by `users.id` (the owner), whatever role it has now. A duplicate is a
  creation of the duplicating account, not of the source's owner. A refusal consumes nothing and is not itself counted.
* **I5. Current-role changes.** The role is read from `users` under the lock. Quotas are enforced unless the role is
  `admin`. An operator who changes a role changes only what is enforced from then on; history is by account and is never
  reinterpreted. A promoted-then-demoted account finds its earlier creations still counted.
* **I6. Admin accounts (approved).** Admin creations are recorded in the ledger and not refused, so they count if the account's role
  changes. One constant: `assignment_quota.EXEMPT_ROLES`. Their unrecorded history is reconciled and their unplaceable data refused like
  anyone's (I12).
* **I7. Existing teachers.** The limits apply to every account of role `teacher`, whether it was created by the operator or will
  register itself. They are per account, not per person: a person with several addresses has several allowances. No claim
  of per-person abuse prevention is made.
* **I8. Existing assignments (no retroactive denial).** Migration 024 writes **no** ledger row (review finding 2: one set of timestamp rules,
  in Python). Every owned assignment without a ledger row is recorded, by its normalized age, by the first creation admission of its owner
  (I12), so a creation made shortly before the deploy still counts in its window. Nothing is closed, hidden, edited or made unreachable. A teacher who already has 10 or more open assignments keeps all of them and may
  edit, close and view them; they cannot create or copy another until fewer than 10 are open.
* **I9. NULL / legacy owners.** An assignment with `owner_user_id IS NULL` belongs to no account: it is in nobody's open count
  and has no ledger row. A creation by a principal without an account id (only tests and the retired shared sign-in produced
  one; no request principal lacks an id today) stays possible at the service level, is unowned and unaccounted. It is
  unreachable from any route.
* **I10. Refusals.** `POST /teacher/new` answers the form again with status 429 (daily) or 409 (open), a Bulgarian message and
  the typed values kept; the copy form on the assignment page does the same. No assignment, token, ledger row, clock mark or
  vault note is written. No `Retry-After` is sent.
* **I11. Teacher gate in development (approved).** `registration.TEACHER_ACTIVATION` stays the single switch, read per request. **No
  development environment variable is added.** A local try-out edits the constant.
* **I12. The ledger is verified and repaired at every admission (review finding 1).** Under the writer lock, before the limits are decided,
  `assignment_quota.reconcile` records the acting account's assignments that have no ledger row, from the `created_at` they carry, all
  or nothing. It is the same for an admin (history belongs to the account after a role change). The account's creation **fails closed**
  (`quota_accounting`, status 503, Bulgarian message, nothing written) when an unrecorded assignment has a NULL, malformed, ambiguous or
  implausible `created_at`, or when a ledger row disagrees with its assignment's owner. A deleted ledger row whose assignment survives is
  re-recorded, not forgiven. **Not recoverable:** an assignment row that was deleted before any admission recorded it. **Stated limits (none is an approved
  quota exception, and no recovery policy is approved):** deleted unrecorded history cannot be reconciled; a build that creates without this admission,
  concurrently with PR 7 builds, is prohibited (it enforces nothing while it runs, and the later admission only counts surviving rows); and after a
  **known history loss** creation must not reopen for the affected accounts until a reviewed reconstruction, or a conservative hold and recovery procedure, has
  been established. Neither exists yet, and the code neither detects the loss nor holds creation. A start-up notice
  (`production.assignment_accounting_warning`) counts such rows; it never refuses to start and is not what keeps the limits safe.
  Recovery or rollback builds must keep this admission (DEPLOYMENT, section 36). Operator handling: the owner stays refused until the value
  is corrected from a snapshot-verified source (DEPLOYMENT).
* **I13. Legacy timestamps (not approved).** `normalize_legacy_timestamp` (all rules in its docstring, tested): the only writer of
  `created_at` was `CURRENT_TIMESTAMP` (UTC). Accepted and kept at their age: the canonical form, ISO with `T`, fractional seconds of any number of
  digits, `Z` and numeric offsets up to 14 h (converted to UTC **before** anything is rounded or compared); **a time without an offset is UTC**; **a date
  alone is the end of that day, 23:59:59 UTC** (never older than it is). Rejected, never treated as old: NULL, empty or padded strings, numbers, other
  layouts, impossible dates or offsets, anything before 2000-01-01 (the epoch included) and anything **more than one day after the writer clock**. A value up to a day
  ahead is kept as it is and counts until it is a day old: the count is conservative there and only there (the count has no upper bound by
  design; the bound exists in the normalizer, so an implausible future date cannot hold an account for years).
  **Precision (review of `3537f4f`, finding 1).** The instant is judged exactly, to the last digit given; a fraction is never truncated, never limited to the six digits of
  `datetime`, and never converted to a number (the digits are compared as text). The ledger counts whole seconds, so a **nonzero** fraction is rounded **up** to the next whole
  second (`…:00.999999999Z` is `…:01`) and a zero fraction is the second itself; rounding up can only keep an allowance spent longer, never refund it early. Both bounds are
  checked on the instant before any rounding: the lower bound (`1999-12-31T23:59:59.9` is refused although it rounds to 2000-01-01) and the future bound (exactly `now`
  plus a day is accepted, any positive excess, however small, is refused).

## Durable accounting (migration 024)

Additive: one new table `assignment_creations (id, owner_user_id → users, assignment_id UNIQUE, created_at)` and one index.
`assignment_id` is a plain integer **without** a foreign key, so deleting an assignment (no code does; the retention cleanup of
`docs/ASSIGNMENT_LIFECYCLE_DESIGN.md` does not exist yet) never touches the ledger and never refunds an allowance. The ledger is
runtime state, never exported to `data/canonical`. Retention: kept as long as the account exists; accounts are disabled, never
deleted. It holds an account id, a time and an assignment number, no name, email or text. Old code ignores the table, so the *schema*
needs no change on a code rollback, **but accounting does**: code that does not know the ledger creates unrecorded, unlimited assignments.
A build that keeps migration 024 must therefore keep the quota admission (I12); the surviving rows such a build leaves are recovered at
the next admission, never forgiven.

## Review findings (b15d02b: REQUEST CHANGES) and their answers

| # | Finding | Answer (committed as `3537f4f`, which was itself reviewed: see the next table) |
| --- | --- | --- |
| 1 (P1) | Rolling back to the exact pre-PR 7 creation function and returning created 20 unaccounted assignments; another 20 succeeded (journal and WAL). | I12: reconcile under the writer lock at every admission, fail closed on what cannot be placed, admins included; docs no longer allow old-code creation against 024; tests run the exact base function in both journal modes. |
| 2 (P2) | The migration turned parseable ISO and date-only values into the epoch, refunding recent legacy creations. | I13 and I8: one Python normalizer with explicit rules; the migration writes nothing; NULL/malformed/ambiguous/implausible values quarantine the owner instead of becoming ancient. |
| 3 (P2) | Teacher cancel/restart and expired flows linked to the student form. | Every restart goes to the validated chooser (`/register`, with the stored `next`); the binding and lifecycle rules are unchanged. |

## Review findings (3537f4f: REQUEST CHANGES) and their answers

| # | Finding | Answer (uncommitted, awaiting independent review) |
| --- | --- | --- |
| 1 (P2) | `normalize_legacy_timestamp` captured the fraction and discarded it: twenty assignments stamped `2026-03-01T09:00:00.999999999Z` allowed twenty more creations at 2026-03-02 09:00:00; offset-bearing fractions did the same; fractions could pass the future-tolerance boundary. | I13 "Precision": exact instant, nonzero fraction rounded up, both bounds checked on the instant before rounding, offsets converted first, any number of digits. |
| 2 (P2) | The shared failure and unavailable helpers rendered an empty return target: gate-off after a teacher began lost `/teacher/new`; an injected verification failure and a resend during a restore hold lost `/account/attempts`. | The helpers take the validated target (the form's value on submit, the revalidated slot elsewhere, read before the binding is retired); no binding or an invalid one stays targetless; a raw request value is never used. |

## Out of scope

Activation of any gate; a production, real-database, provider, tunnel or server operation; teacher verification (the role is
self-declared plus a verified mailbox); password recovery (PR 8); privacy text (D16: no new privacy or consent claim is
written); an admin UI; a quota override or an operator command to raise a limit; per-IP or per-person limits; any change to
sign-in, ownership, or the existing routes' authorization.

## Activation prerequisites unchanged

The teacher gate still needs everything the scope lists outside the code (D16 privacy, sender, capacity, Linux/systemd
evidence, PR 8). PR 7 satisfies only the "quotas exist" prerequisite, and only once this implementation is independently
reviewed.
