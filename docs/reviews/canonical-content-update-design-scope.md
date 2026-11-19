# B4 canonical content-update design scope

## Goal and starting state

Write the design note before implementing B4's production content-update tool.
The approved 2026-11-18 roadmap allows this launch preparation while owner inputs
wait. B5 is merged as PR #361. This task designs an explicit update of canonical
content in an existing SQLite database with accounts and attempts; it neither
implements the updater nor operates any database or server.

Base: `9b634d152c42eb8f1f5b5fc2c80fe818019408d7` (merged PR #361).
Branch: `docs/canonical-content-update-design`.
Worktree: `/Users/gabrielpilichev/dzi-generator-content-update-design`.
Read `AGENTS.md` first and preserve the separate dirty main checkout.

## Single implementation target

Create only `docs/CANONICAL_CONTENT_UPDATE_DESIGN.md`: a compact design note,
at most 1,200 words by `wc -w` over the whole file, including headings, tables
and references. This is the review's two-page B4 design step; the later tool
needs its own scope and PR. Prefer source links over repeating existing contracts.

Read the actual canonical/export/load/parity/builder/identity/backup code and
relevant migrations and tests as source, without executing them. Ground the
note in `docs/CANONICAL_DATA.md`, `docs/ASSIGNMENT_LIFECYCLE_DESIGN.md`,
the current roadmap, `docs/LAUNCH_CHECKLIST.md` and the existing
deployment/backup/restore contracts. Source observations,
recommended v1 decisions and unresolved prerequisites must be distinguishable.
Do not silently amend an approved contract or claim a proposed command exists.

The design must settle or explicitly bound:

1. **Purpose and reuse.** Explain why a fresh rebuild/replacement is not an
   update of an existing database. Identify reusable pieces and their limits:
   `canonical_load` skips identical rows but rejects different values;
   fresh-build parity requires empty runtime/operational state. Never apply
   those empty-database assumptions to a populated target.
2. **Interface and trust.** Propose explicit target/source inputs, dry run by
   default and separately explicit apply. Specify manifest/hash/format/schema/
   migration/classification preflight, no missing-target creation or implicit
   runtime-DB default, a bounded plan/report and failure/exit semantics. The
   target's deployed version/content baseline is unknown; say what evidence or
   trusted preimage an update needs rather than inferring it from a repo SHA.
3. **Update envelope and identities.** Give a decision matrix for absent rows,
   identical rows, differing existing values, target-only rows, primary/natural
   key collisions, legacy NULL natural keys, and sequence/high-water conflicts.
   State the v1 policy for reviewed value changes (including timestamps and
   approval flags), unsupported edits and deletions. Consider the effect of
   changing prompts/options/keys on historical assignments/attempts, not only
   whether IDs still resolve: per `docs/ASSIGNMENT_LIFECYCLE_DESIGN.md`, content
   is not copied into attempts and scores/option order are derived on read. An
   apply receipt is not thereby an accepted history witness (left to PR #327's
   design). Canonical IDs never change or get reused; no
   remapping, delete/reinsert, generic replacement or force bypass. State how
   unrecognized divergence is refused and which reconciliation needs separate
   review. Do not promise every unknown production database is updatable.
4. **Preservation.** Use the current `TABLE_CLASSIFICATION`, including accounts,
   auth/session/registration/recovery/profile state and ledgers, not just the
   older abbreviated documentation list. Preserve runtime and operational rows;
   only the existing contract's canonical `sqlite_sequence` entries may rise,
   never fall. Preserve target-only content and FK-less/JSON references. Define
   validation against the resulting populated target, not canonical-only IDs
   or exact fresh-build counts/sequences that reject legitimate extra state.
5. **Apply consistency.** Specify atomicity, source pinning, target rechecks under
   the write lock, stale-plan/concurrent-change and busy/failure behavior,
   rollback without partial changes, and idempotent repeat application. Define
   schema/trigger compatibility instead of running migrations automatically.
   Distinguish transaction rollback from backup restore; report/receipt failure
   after commit must not be reported as a database rollback.
6. **SQLite, backup and authority.** Define a consistent WAL-aware dry run and
   its logical-write/sidecar guarantees. Do not use `immutable` to ignore an
   active WAL or copy a live SQLite file. State backup and maintenance
   prerequisites and reference the existing authorized procedures without
   designing a new restore protocol. Like the existing backup, account and
   registration-cleanup tools, refuse before reading and again under the write
   lock while the restore lock (`web/services/lifecycle.py`, DEPLOYMENT section
   17) exists. Existing backups scrub transient
   registration/recovery data; do not use a scrubbed backup as an exact runtime
   preservation baseline or promise a credential-neutral full restore.
   Execution, backup/restore, deployment and activation remain separately
   authorized, and none is done by this design PR.
7. **Assets and reporting.** Distinguish canonical asset metadata from actual
   files; define missing-file/availability boundaries without adding a file
   copier or claiming live assets exist. Plan reports must expose the proposed
   canonical actions/conflicts without credentials, runtime personal data or
   sensitive row dumps. No content generation/import or canonical refresh here.
8. **Acceptance plan and follow-up.** List meaningful future temporary-database
   cases: first update and repeat, populated runtime preserved, bad input/schema/
   missing target, restore lock present, identity/value/NULL-key/sequence
   conflicts, target-only state, historical-reference semantics,
   dry-run/WAL/concurrency/stale-plan behavior,
   injected failure rollback and post-commit reporting. These are a test plan,
   not tests to execute now. Identify a narrow later implementation PR and any
   remaining owner decision; legal/provider facts need not block this note.

## Preservation and verification

This scope is the workflow artifact, frozen after owner commit; the eventual
design PR contains it and the new note. No existing file changes during
implementation. No code, test, dependency, schema/migration, canonical data,
deployment configuration, gate or vault change. Preserve stable-ID and existing
storage/backup/restore/access contracts. No live state is inferred.

No tests, compile, app import, database read/build/audit, credential/config/env
inspection, network/live requests, SSH, mail/provider/DNS/VPS/server/tunnel,
deployment or activation work. Documentation-only diff, whitespace, whole-file
word count, local link/heading checks and a source-to-decision coverage check
replace generic AGENTS/roadmap test, compile and database commands.

Review this scope first. Gabriel stages, commits and pushes it; the coordinator
discovers/creates the actual draft PR before fresh Sonnet / High authors the
note. Edits stay unstaged/uncommitted for Gabriel. Report exact branch/HEAD,
source findings versus proposals, static checks, preservation, limitations and
meaningful commit/PR text with the actual PR number. After owner push, fresh
Opus / High reviews the exact design commit. The coordinator prints guarded
squash-merge commands in chat; Gabriel merges. Tool implementation follows
separately, and no real database, deployment or activation is authorized here.
