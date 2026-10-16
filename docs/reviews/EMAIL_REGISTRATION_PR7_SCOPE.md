# Email registration, PR 7 — teacher quotas and gated teacher registration: scope

Design: `docs/reviews/EMAIL_REGISTRATION_DESIGN.md` (sections 7.1, 7.3 and 9, PR 7; decisions D3, D4, D5, D14).
Status and results: `docs/reviews/EMAIL_REGISTRATION_PR7_STATUS.md`. Test plan: `EMAIL_REGISTRATION_TEST_PLAN.md`.
This is the "short scope for PR 7" the design asks for (section 9, row 7; open item 1a).

Branch `feature/teacher-registration-quotas`, started from `fd44c8f` (on top of `ede0c0b`, PR 6, #340). Nothing is committed.

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

* 20 creations per teacher in a rolling 24 hours; 10 open assignments per teacher.
* Closing frees an open slot and does not refund creation allowance.
* Enforced atomically across creation paths.
* A verified teacher registration receives role `teacher` only when the teacher gate permits activation.
* Production registration, teacher, profile and Google gates stay **off**.

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
* **I6. Admin accounts (substantive, unresolved).** The brief says "per teacher" and approves no admin limit and no admin
  exemption. Applying the limits to admins would invent a limit on the operator's own tooling; exempting them would invent an
  exemption. PR 7 does neither silently: **admin creations are recorded in the ledger but not refused**, so the numbers exist
  if Gabriel later decides to limit them. This is one constant (`assignment_quota.EXEMPT_ROLES`). **Needs Gabriel's decision.**
* **I7. Existing teachers.** The limits apply to every account of role `teacher`, whether it was created by the operator or will
  register itself. They are per account, not per person: a person with several addresses has several allowances. No claim
  of per-person abuse prevention is made.
* **I8. Existing assignments (no retroactive denial).** Migration 024 copies one ledger row per existing owned assignment
  (`owner_user_id` and `created_at`), so a creation made shortly before the deploy still counts in its window. Nothing is
  closed, hidden, edited or made unreachable. A teacher who already has 10 or more open assignments keeps all of them and may
  edit, close and view them; they cannot create or copy another until fewer than 10 are open.
* **I9. NULL / legacy owners.** An assignment with `owner_user_id IS NULL` belongs to no account: it is in nobody's open count
  and has no ledger row. A creation by a principal without an account id (only tests and the retired shared sign-in produced
  one; no request principal lacks an id today) stays possible at the service level, is unowned and unaccounted. It is
  unreachable from any route.
* **I10. Refusals.** `POST /teacher/new` answers the form again with status 429 (daily) or 409 (open), a Bulgarian message and
  the typed values kept; the copy form on the assignment page does the same. No assignment, token, ledger row, clock mark or
  vault note is written. No `Retry-After` is sent.
* **I11. Teacher gate in development.** `registration.TEACHER_ACTIVATION` stays the single switch, read per request, as it is
  today. **No development environment variable is added** (PR 6 added one for the profile gate with Gabriel's approval; this
  PR does not assume the same approval). A local try-out edits the constant. **Question for Gabriel.**

## Durable accounting (migration 024)

Additive: one new table `assignment_creations (id, owner_user_id → users, assignment_id UNIQUE, created_at)` and one index.
`assignment_id` is a plain integer **without** a foreign key, so deleting an assignment (no code does; the retention cleanup of
`docs/ASSIGNMENT_LIFECYCLE_DESIGN.md` does not exist yet) never touches the ledger and never refunds an allowance. The ledger is
runtime state, never exported to `data/canonical`. Retention: kept as long as the account exists; accounts are disabled, never
deleted. It holds an account id, a time and an assignment number, no name, email or text. Old code ignores the table, so a code
rollback needs no schema change.

## Out of scope

Activation of any gate; a production, real-database, provider, tunnel or server operation; teacher verification (the role is
self-declared plus a verified mailbox); password recovery (PR 8); privacy text (D16: no new privacy or consent claim is
written); an admin UI; a quota override or an operator command to raise a limit; per-IP or per-person limits; any change to
sign-in, ownership, or the existing routes' authorization.

## Activation prerequisites unchanged

The teacher gate still needs everything the scope lists outside the code (D16 privacy, sender, capacity, Linux/systemd
evidence, PR 8). PR 7 satisfies only the "quotas exist" prerequisite, and only once this implementation is independently
reviewed.
