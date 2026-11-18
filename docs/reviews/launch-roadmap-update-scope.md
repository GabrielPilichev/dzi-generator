# Launch-first roadmap update scope

## Approval and starting state

Gabriel approved the presented Opus 5.5 / High selections with **"go with the
plan"** on 2026-11-18. Record the approved planning direction in the canonical
roadmap; this does not approve legal copy, deployment or activation.

Base: `316d82015c9a70a57f47aeb138df0bc39c9e4fd5` (merged PR #359).
Branch: `docs/launch-roadmap-update`.
Worktree: `/Users/gabrielpilichev/dzi-generator-launch-roadmap-update`.
Read `AGENTS.md` first and preserve the separate dirty main checkout.

The selected plan and narrow next-task brief are the corrected `DECISIONS.md`
and `NEXT_TASK.md` under `/private/tmp/learnpilot-launch-options.1f_wmoel/`.
Those files are inputs, not additional files to copy into the repository.

## Single implementation target

Edit only `docs/REFACTOR_ACCOUNTS_ROADMAP.md`. Add a self-contained
**Current roadmap update — 2026-11-18** section immediately before the existing
2026-10-13 update. The new block is at most 400 words, counted with `wc -w`
over the inserted lines, heading included. Rename the old
heading to **Earlier roadmap update — 2026-10-13**; preserve every other byte
of the existing document. State that on order and release intent this update
overrides earlier sections, including the earlier update's sequence, the
PR-sequence table and the timing of entitlements and billing, while approved
engineering, access and storage contracts and the DZI gate's specification
and acceptance checks remain authoritative.

The new section records:

1. Gabriel's approval of the Opus-selected direction. Name the proposed review
   `REVIEW_2026-11-11_NEXT_STEPS.md` and its SHA-256
   `d3112d4f572f7949a9f3a59a97c23b8490f3de82ff03cace3fee993e9368d0f3`
   as provenance, without tracking that report or linking to a local Downloads
   path. Use **review D1/D2/D5** to avoid the other meanings of those numbers.
2. Modified review D1: launch preparation first; bring the production vault-note
   change into that track. While owner-dependent inputs wait, advance the launch
   checklist, the canonical content-update design and tool, the practice design
   note and section practice, **one PR at a time**. Then practice, then the DZI
   gate; content alongside that work once review D6/D7 permit it; then
   teacher-builder work and operations as needed.
   Defer entitlements/billing until real users. Keep the DZI gate's existing
   specification and acceptance checks intact.
3. Review D2: a February 2027 classroom-pilot target with student registration,
   recovery, profiles, existing teacher tests/current content and a privacy page.
   Teacher accounts use the existing operator CLI; teacher self-registration and
   Google remain off for this pilot. The 2027-03-15 target adds practice, DZI
   Part 1 simulation, the DZI gate and new content subject to review D6/D7.
   Dates are planning targets, not verified school-calendar facts or promises.
   Record the 2027-01-08 readiness checkpoint and the assumed six owner hours
   weekly as estimates; missing prerequisites or failed capacity evidence delay
   activation regardless of a target date. Each pilot feature is switched on
   only by its own later activation PR under separate authorization, following
   the existing activation prerequisites; this entry activates nothing.
4. Privacy-first public `/privacy`, its small `legal` blueprint and separate
   text include, with footer/form links, as future brief defaults. Final legal
   assessment may require additional notices or terms even for a free pilot.
5. Review D5: a separately scoped change stops production quiz-note writing
   while retaining local development use. It deletes no existing notes and
   leaves backup/restore and other vault content unchanged.
6. Brevo SMTP as the selected provider, Amazon SES in an EU region as fallback.
   This records a choice, not provisioning or verification. Existing live
   accounts, DNS, credentials and deployment state are uninspected/unknown.
   Record no prices, quotas, plan limits or send caps.
7. Open prerequisites: controller/contact facts, approved Bulgarian copy and
   recovery wording, sender domain, D16 policy decisions and required evidence.
   Policy recommendations are not legal approval. No statutory summary, legal
   basis, consent control or retention period is adopted by this entry. Preserve
   other unresolved review decisions; subsequent scopes and all operational
   actions still follow their own authorization.

## Preservation and verification

No implementation outside the one roadmap file. This scope document is the
workflow preparation artifact and remains unchanged during implementation;
the eventual PR contains it plus the roadmap edit. No code, template, test,
dependency, schema, canonical data, other design/contract, config or deployment
change. Every activation constant stays off. No app import, test suite,
database access/build/audit, credentials, provider, DNS, server, tunnel or VPS
operation is needed for this documentation task.

Review the complete diff and whitespace, count the new block's words, and prove
that removing it and restoring the old heading yields the exact base document.
Verify only the roadmap changes after the owner commits the scope; review the
new block against the approved selections, with no invented live-state claims
or blanket approval of the entire November review. Existing open-decision
tables and approved registration/recovery/storage/access contracts stay intact.
For this documentation-only task, these checks replace the roadmap's generic
full-suite-before-push rule and the `AGENTS.md` compile/database checks.

## Workflow and handoff

Scope review first. Gabriel stages, commits and pushes the scope; the
coordinator then opens the actual draft PR before fresh Sonnet / High
implementation. Implementation remains unstaged/uncommitted for Gabriel.
Report exact branch/HEAD, allowed paths, diff/whitespace and preservation
results, limitations, and meaningful commit/PR descriptions using the discovered
PR number. After push, fresh Opus / High reviews that exact commit. The
coordinator prints guarded squash-merge commands in chat; Gabriel merges.
