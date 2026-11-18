# A7: repository-root cleanup scope

## Goal and base

Complete A7 from Gabriel's `REVIEW_2026-11-11_NEXT_STEPS.md`: archive the three
root reports under `docs/reviews/` and document the three root scripts without
deleting files or changing executable behavior.

Base: `dc7bc3f899de19ed96ad8a1954fdf37b90379d33`, merged PR #358.
Branch: `docs/repository-root-cleanup`.
Worktree: `/Users/gabrielpilichev/dzi-generator-repository-root-cleanup`.
Read `AGENTS.md` before work. Leave the separate, dirty main checkout alone.

This scope is prepared before implementation. Gabriel commits and pushes it;
the coordinator then discovers or opens the actual draft PR before assigning
a fresh Sonnet / High implementation session. Gabriel reserves staging,
commits, pushes and the merge. A separate Opus / High session reviews the exact
pushed implementation commit before any merge commands are issued.

## Allowed changes

Move these reports one for one, preserving each file's bytes and mode:

| Existing path | Destination |
| --- | --- |
| `REVIEW_2026-06-11.md` | `docs/reviews/REVIEW_2026-06-11.md` |
| `REVIEW_2026-06-11_pass2.md` | `docs/reviews/REVIEW_2026-06-11_pass2.md` |
| `audit_reclassification_report.md` | `docs/reviews/audit_reclassification_report.md` |

Do not update their historical findings, counts, commands, source references or
dates. All three have mode `100644`; the preparation scan found no Markdown
links or reference-style link definitions in them that require relocation.
Verify that observation rather than rewriting historical prose.

Edit only `docs/SCRIPT_INVENTORY.md` in addition to the moves. Extend its opening
coverage statement beyond `src/` to the three retained root scripts, and add
exactly one entry for each under **Historical / one-shot**:

- `audit_reclassification_state.py`: old reclassification audit using relative
  paths to a local runtime DB, logs, vault and a named backup. It opens SQLite
  normally, writes the root `audit_reclassification_report.md`, and embeds a
  historical raw database-copy suggestion. It is not the current read-only
  canonical audit or an approved backup procedure. Its old output path remains
  unchanged and running it would recreate the root report.
- `cleanup_old_backups_keep_current.sh`: one-time, working-directory-relative
  deletion of local DB, `app.py`, template and vault backups with
  `find … -delete` / `rm -rf`; it has no dry-run mode. It first requires four
  named `current-stable-before-ui` snapshots to exist and stops before deleting
  anything if one is missing. It says it keeps all four, but its vault
  exclusion (`-name './vault.backup-current-stable-before-ui'`) can never match
  a basename, so that vault snapshot would also be removed. It is not the
  current backup/retention workflow.
- `repair_topic_areas_from_vault.py`: one-time repair of missing topic area IDs
  from vault frontmatter. Importing it already opens SQLite and executes the
  repair and commit; it has no main guard or dry-run mode.

Keep all three scripts at their current paths, with identical bytes and modes
(`100644`, `100755`, `100644`, respectively). The preparation search found no
tracked callers outside their own names and the audit's output-path reference;
this says nothing about external/manual use. Do not execute or import them.

Add a short historical-report index in the inventory, with relative links to
the three new report locations, and identify them as historical snapshots.
Preserve all existing inventory entries and status meanings. These are the
only implementation paths; this scope document itself remains unchanged.

## Preservation and verification

No application, test, script, dependency, schema, content, configuration,
authentication, scoring or deployment changes. No script relocation, deletion,
compatibility shim, new tooling or cleanup of unrelated files. Do not access
the real `data/questions.db` or sidecars, generated vault content, credentials,
providers, feature gates, servers, tunnels or the VPS.

This is documentation-only work. Verify the three moves against base blobs
using hashes and modes, all retained scripts against their base blobs and modes,
the exact allowed diff, the inventory's three entries and three links, and
tracked references to the old filenames. Distinguish historical literal paths
and the unchanged audit output constant from navigational links. Check that
every new relative link resolves. Run whitespace checks on changed/new text,
including unstaged destinations that `git diff --check` alone cannot see.
Inspect `git --no-pager diff`; after the owner commits, confirm three 100%
renames and no content loss. No tests, app imports, database build or audit,
network retrieval or full suite is needed for these moves and inventory edits.

## Handoff

Leave implementation edits unstaged and uncommitted. Report branch, exact HEAD,
full short status, file list, hashes/modes, reference and link results, whitespace
results, limitations and any jobs still running. Supply the intended staging
list and meaningful multi-paragraph commit and PR descriptions using the actual
PR number. Do not stage, commit, push, publish a review, mark ready or merge.
