# Email registration, PR 1 — schema, profiles and validators: status and contract

Design: `docs/reviews/EMAIL_REGISTRATION_DESIGN.md` (sections 1, 6.1, 6.3, 9).
Tests: `docs/reviews/EMAIL_REGISTRATION_TEST_PLAN.md`, sections A and B.
Scope: `docs/reviews/EMAIL_REGISTRATION_SCOPE.md`.

**Status:** implemented in draft PR #335 and **not independently reviewed**. It
is storage and validation only. No route, template, mail sender, verification
lifecycle, activation, teacher quota, profile gate or password recovery exists,
no gate exists to turn on, and Google sign-in and account sign-in are unchanged. Deployment
notes: `docs/DEPLOYMENT.md`, section 30.

## What PR 1 contains

| Part | File |
| --- | --- |
| Migration 021 | `web/migrations/021_registration_schema.sql` |
| Table classification (all five **runtime**) | `src/canonical_tables.py` |
| Manifest schema metadata (level, migration list, runtime exclusions) | `data/canonical/manifest.json` |
| Profile service | `web/services/profiles.py` |
| Form validators | `web/services/registration_forms.py` |
| Tests | `tests/test_registration_migration_sql.py`, `tests/test_registration_forms.py`, `tests/test_profiles_service.py` |
| Sweep lists of existing tests (new level, new runtime tables) | `tests/test_canonical_content.py`, `tests/test_build_database.py`, `tests/test_database_test_support.py`, `tests/test_schema_baseline.py`, `tests/test_assignment_owner_migration_sql.py` |

Not in PR 1, and not moved into it: `users.profile_required_at` (PR 6, D11);
`web/services/registration.py`, `mail*.py` and the `accounts` additions
(PR 2 and 3); `needs_profile` (PR 6, see "Deviations"). No canonical row, import
or vault file changes; the canonical JSONL files are byte-identical.

## Schema (migration 021)

`ALTER TABLE users ADD COLUMN email_verified_at TEXT` (NULL for every existing
account; no row written, no rebuild) and the five tables of design 6.1:
`user_profiles`, `teacher_subjects`, `pending_registrations`,
`registration_sends`, `registration_control`, with the sketch's columns, CHECKs
and indexes. `'admin'` cannot be an `account_type`. A pending row that is not
`pending` cannot hold an email, a name, a grade, a school, subjects, a password
hash, a code hash or a code expiry.

Additions to the sketch, each an invariant the design states in prose and none a
product decision. A reviewer may drop any of them with a one-line change:

- The 64-byte lower-case hex check on `binding_hash`, `code_hash` and
  `email_hash` (design 6.1, "should be applied"), in the form of migration 020.
- `typeof(...) = 'integer'` on `grade`, `position`, `code_attempts` and
  `send_count`: an INTEGER column keeps `9.5` as a real, which `BETWEEN` accepts.
- `grade_set_at` set exactly when `grade` is (D2, "stored with the date set").
- `teacher_subjects.subject_id` is NULL or 1–64 characters, and two partial
  unique indexes: a subject cannot repeat for a profile, and „Друг предмет“
  (`subject_id` NULL) appears at most once (form rules, test B6).

## Validators (`registration_forms`)

`validate_student(form)` and `validate_teacher(form)` take a `dict` or
`MultiDict` and return a frozen `ValidationResult(values, errors, echo, password)`:

- Valid: `values` holds the cleaned fields (`account_type` is the function
  called, never a submitted value), `errors` is empty, and `password` is the
  exact string received. Invalid: `values` and `password` are `None` and
  `errors` maps a field to one Bulgarian message. `password` is out of `repr`;
  a password is never in `values`, an error or the echo.
- `echo` holds every submitted value except the passwords, each cut to its
  maximum length, for a re-rendered form.
- Names, school and „Друг предмет“ are stripped and their whitespace collapsed
  (`" ".join(value.split())`, so a tab or no-break space between words becomes a
  space); nothing else is normalised. Passwords and the confirmation are used
  exactly as received. `email` goes through `accounts.normalize_email`, then the
  public ASCII/dotted-domain rule (`public_email`); a CR or LF anywhere is refused.
- `grade` is an `int` 8–12 or exactly `"8"`–`"12"`. Subjects are registry ids
  (read at call time) or `other`; `other` counts toward the three and needs its
  text; a text sent without `other` is ignored.
- `next` is kept only if `web.redirects.safe_redirect_target` returns it
  unchanged (imported on first use so the module imports without Flask), at most
  512 characters, else it is `None`.
- The public cleaners (`clean_name`, `clean_grade`, `clean_school`,
  `clean_subject_other`) are what the profile service re-checks with.

## Profile service (`profiles`)

`get_profile`, `create_profile`, `update_profile` and `display_name`. Like the
other services it takes an open connection, never commits or rolls back, and
needs `now` as a timezone-aware `datetime`.

- It never reads or writes a `users` column except an existence check, so a
  profile cannot grant or change a role; a profile and a role may disagree.
- Values must already be in cleaned form and are refused, never repaired.
  Everything is validated before the first write. Subjects are
  `{"subject_id", "label"}` dicts; a registry id must carry that subject's
  current title. Errors are Bulgarian and never contain a value.
- `create_profile`: `LookupError` for an unknown account, `ProfileExists` for a
  second profile. `update_profile` keeps the type fixed, replaces subjects as a
  whole, and moves `grade_set_at` only when the grade changes (no automatic
  advancement).

**Caller obligations (later PRs).** Begin the transaction (`BEGIN IMMEDIATE` on
the write paths) and commit or roll back, rolling back after any exception.
Store `users.display_name` from `profiles.display_name(...)` yourself: PR 3 at
activation, PR 6 when a profile is completed, which needs an `accounts`
function for it. Decide whether an account should have a profile at all. Hash
the exact password with the `accounts` additions of PR 3, which do not exist yet.

## Deviations and contradictions found

1. **`_USER_COLUMNS` (design 6.1) is not widened.** Design 6.1 says
   `_USER_COLUMNS` gains `email_verified_at` with PR 1. The test plan says the
   existing suites pass unedited (Conventions) and that code predating the
   migration still loads users (A9). Widening it failed 88 existing tests that
   build `users` from migration 012 alone and made the CLI fail with a bare
   `no such column` on an unmigrated database. Smallest correction: it gains the
   column in the first PR with a reader (PR 3 or 4), together with the CLI's
   schema check.
2. **`needs_profile` (design 6.3) is not in PR 1.** It is listed under
   `profiles.py` but reads `profile_required_at` (PR 6) and the profile gate
   (PR 4 and 6). Smallest correction: list it with PR 6.
3. **One message the design lacks.** Design 1.2 has no text for a subject id
   outside the list, a repeated one or a non-text value; PR 1 uses „Избери
   предмети от списъка.“ for them.
4. **A text sent without `other` is ignored** (design 1.1 says only "required
   with `other`").

## Running the checks

`python3 -B -m unittest tests.test_registration_migration_sql tests.test_registration_forms tests.test_profiles_service`
for the PR's own tests, `python3 -B -m src.canonical_export --check` for the
tracked canonical data, and the full suite once on the final state. All use
temporary databases built from migrations and canonical data.
