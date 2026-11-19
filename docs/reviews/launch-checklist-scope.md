# B5 launch-checklist index scope

## Goal and starting state

Add a compact index of the existing deployment and activation requirements so
Gabriel can see the evidence and sign-offs still needed for the classroom pilot.
This is B5 in the approved launch-first direction recorded by merged PR #360;
it does not require the still-open privacy copy or authorize operational work.

Base: `78ee596396d4d76e41e4aa0d08dcd0af4fafbaa6` (merged PR #360).
Branch: `docs/launch-checklist`.
Worktree: `/Users/gabrielpilichev/dzi-generator-launch-checklist`.
Read `AGENTS.md` first; preserve the separate dirty main checkout.

The proposed November review's B5 is a one-page index, not a new procedure.
Its filename is `REVIEW_2026-11-11_NEXT_STEPS.md`, SHA-256
`d3112d4f572f7949a9f3a59a97c23b8490f3de82ff03cace3fee993e9368d0f3`.
Use the tracked 2026-11-18 roadmap update as the current planning authority;
this does not adopt the review's other blank decisions or policy recommendations.

## Single implementation target

Create only `docs/LAUNCH_CHECKLIST.md`. Keep it a one-page-style index of at most
600 words, counted with `wc -w` over the whole file including headings, tables
and references. Link to existing procedures and acceptance criteria rather than
copying commands, rewriting them or creating a parallel runbook.

The checklist must contain:

1. Release/evidence fields: deployed commit, exact intended release commit,
   migration range and pending-migration evidence, dated evidence-record link,
   and final owner sign-off. The deployed commit, release candidate and migration
   range are unknown/not calculated here; a repository baseline is not a deployed
   release. No guessed SHA, migration list or prefilled pass/sign-off.
2. A distinct **deploy with every gate off** stage indexing
   `docs/DEPLOYMENT.md` section 20, with sections 9, 16 and 21 as needed for
   migration safety, complete snapshot and rollback, and, for each release or
   migration in the not-yet-calculated range, that release's own section among
   22–38 (its deployment-order/deploying and rollback parts). Which of them apply
   is unknown until the deployed commit is recorded; none is marked applicable or
   skipped here. Include the future record that the deployed build keeps all five
   build gates `False` (`registration.PRODUCTION_ACTIVATION`,
   `registration.TEACHER_ACTIVATION`, `profiles.PRODUCTION_ACTIVATION`,
   `recovery.PRODUCTION_ACTIVATION`, `google_oidc.PRODUCTION_ACTIVATION`; their
   values at the base commit are not deployment evidence), the smoke/health result
   and owner sign-off. This is a future checklist, not permission to deploy or run
   those procedures.
3. Separately identifiable coverage of **every row** of
   `docs/reviews/EMAIL_REGISTRATION_DESIGN.md` section 0.3: bounded mail,
   single-worker capacity, TLS/backend constraints, provider/sender and delivery,
   Nginx/client IP, shared NAT/total budget, code/notice timing, cleanup/restore,
   privacy, teacher-form quotas, recovery-before-public-opening and Google.
   Preserve the shared registration/recovery budget and the existing failure
   criteria; point to the source for exact thresholds instead of inventing any.
4. Recovery's additional evidence from
   `docs/reviews/PASSWORD_RECOVERY_DESIGN.md` section 9 (with 6.6) and
   `docs/DEPLOYMENT.md` sections 37 and 38 ("Still outstanding before any
   activation"): recovery cleanup and staging restore/snapshot, RD18
   credential-repair rehearsal with required maintenance isolation and
   postponement rule, code/neutral-notice timing, password-writer lock duration,
   real Nginx/header/mixed-class checks, RD15/D16/D17 prerequisites and the
   independent security review of every scope. Do not flatten RD18 into a generic
   restore pass.
5. Every evidence item has a status, an evidence placeholder/reference and a
   responsible sign-off. A concise shared legend may supply empty placeholders
   and the default accountable owner (Gabriel), provided each item is separately
   recordable. Initial evidence and sign-offs are pending/not inspected. A
   source link or merged implementation is not evidence of a live check.
6. A distinct **later activation** boundary: each pilot feature still needs its
   own separately authorized activation PR under the existing prerequisites.
   Teacher self-registration and Google remain outside R1; index their source
   requirements as deferred, not passed or prerequisites for student activation.
   The existing operator CLI remains the teacher-account path. Recovery must be
   ready before public opening. No gate changes or new activation ordering.
7. Current open inputs: D16 and final Bulgarian copy including RD15,
   controller/contact facts, sender domain/configuration and operational
   evidence. Brevo SMTP is the planning selection with Amazon SES EU fallback;
   live accounts, credentials, DNS, deployment and delivery are uninspected and
   unknown, not proven absent. No provider prices/plan limits, new limits or
   legal-policy conclusions.
8. State that schema migration is not canonical-content synchronization.
   If new content is included, its production update remains separate B4 work
   under `docs/CANONICAL_DATA.md`'s stable-id contract. No replacement database,
   import command or production content-update procedure is invented here.

## Preservation, checks and workflow

The scope is a workflow artifact, frozen after owner commit; the eventual PR
contains it plus the new checklist. No existing file is edited during
implementation. Do not change deployment/design/roadmap contracts, code,
templates, tests, schema, data, configuration, gates or vault content.

No tests, compile, app import, database read/build/audit, credential/config/env
inspection, live request, SSH, provider/DNS/mail/VPS/deployment or activation
operation is needed. Documentation-only diff, whitespace, full-file word count,
local link/heading checks and a source-to-item coverage check replace generic
AGENTS/roadmap test-suite, compile and database commands for this task.

Review the scope first. Gabriel stages, commits and pushes it; the coordinator
discovers/creates the actual draft PR before fresh Sonnet / High implementation.
Implementation stays unstaged/uncommitted for Gabriel. Report exact branch/HEAD,
intended paths, static checks, source coverage, preserved state, limitations and
meaningful commit/PR text using the actual PR number. After owner push, fresh
Opus / High reviews the exact implementation SHA. The coordinator prints guarded
squash-merge commands in chat; Gabriel merges.
