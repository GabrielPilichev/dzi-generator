# Expression-index introspection follow-up: scope

Status: narrow read-only schema-tooling fix after PR #365, before the C2
start service. No practice service, route, content or schema change.

## Baseline and reproduced problem

- Base: 30e6b9eed2de13d58e0ef02df40dbe4385721e62 (merged PR #365).
- Branch: fix/expression-index-introspection, dedicated worktree.
- Follow AGENTS.md and the approved roadmap/C1 design. The independent
  Opus/High review of #365 recommended this separately scoped follow-up.
- PR #365 was approved at 49ff713e9e17d58e7acbe4e0584f605b47d32469.
  Its scratch probe replaced uniq_practice_sessions_open_scope with the
  same named index using topic_id + 0 or NULLIF(topic_id, topic_id).
  PRAGMA index_xinfo describes every expression key with a NULL name;
  table_shape therefore considered the wrong index equal to the correct
  COALESCE index. Both dry-run and apply accepted the incompatible target,
  which then allowed two open section-wide sessions.
- The migration itself creates the correct index. This task fixes the
  shared comparison, without changing that migration or practice storage.

## Allowed changes

- src/schema_introspection.py.
- Add tests/test_schema_introspection.py.
- tests/test_update_canonical_content.py for actual target refusal and
  positive-control integration tests.
- tests/test_schema_baseline.py, tests/test_migration_runner_dry_run.py and
  tests/test_migration_runner_table_rebuild.py only for relevant regression
  coverage or a directly affected expectation.
- tests/test_practice_runtime_migration_sql.py only if a shape expectation
  needs the newly recorded identity; preserve its behavioral assertions.
- Everything else, including this scope, migrations, canonical files,
  updater production code, route/service code and existing design documents,
  stays byte-identical. Report a concrete additional dependency before editing.

## Behavior and boundaries

Recognize expression keys using the index_xinfo expression marker, not
just a NULL name. Record enough expression identity from sqlite_master.sql
that different key expressions compare unequal in table_shape and every
existing consumer. Keep ordinary named and automatic index shapes identical
to the base. Preserve uniqueness, origin, key order, DESC/collation and
partial-predicate checks.

The implementation may record expression-key SQL or conservatively record
the expression index's statement identity. It need not prove mathematical
or SQL semantic equivalence: rejecting different spellings conservatively
is acceptable when documented. Identical recreated DDL must compare equal.
Retain whitespace/comment tolerance outside quoted content. Never collapse
distinct quoted literal contents or discard comment markers inside a
literal; handle nested parentheses and escaped quotes without pretending a
naive first-parenthesis or WHERE split parses SQL.

Keep normalize_sql and ordinary-index behavior unchanged. If expression
identity cannot be obtained safely, fail closed rather than comparing it
as an unspecified expression. Add no SQL execution based on captured DDL,
write operation, migration, dependency, general SQL parser or semantic
optimizer. Column-level collation and other preexisting normalization
limitations are outside this task.

The extra shape identity may change schema/plan hashes for schemas with
expression indexes. New dry-run/apply tokens must agree; do not preserve an
old token by weakening validation. Change no hash algorithm or report code.

## Meaningful verification

- Demonstrate the reproduced wrong-expression test fails on the exact base
  helper in a scratch copy and passes after the fix.
- On disposable targets, reject the same-name topic_id + 0 and NULLIF
  replacements as incompatible_target, in both dry-run and apply, before
  any write. Verify full row/sequence snapshots remain unchanged.
- An identical recreated correct index must still plan and genuinely apply.
  Preserve refusal for wrong partial predicates and non-unique indexes.
- Test expression identity directly, including mixed column/expression
  keys, nested expressions, quoted literals/identifiers, embedded whitespace,
  escaped quotes and literal comment/WHERE text. A different literal must
  never normalize equal. Keep tests behavioral rather than source pins.
- Compare representative ordinary/automatic index shapes against the
  unchanged base implementation. Test shared snapshot/shape comparison so
  the fix reaches the updater without a practice-specific exception.
- Run the new introspection suite and affected schema, migration-runner,
  practice migration, canonical, builder, database-support and three updater
  suites, plus repo auth-guard/attempt-render regressions. Compile changed
  Python to scratch. Run one full unittest suite after final code/tests;
  repeat only for a justified new failure or correction.
- All databases are temporary fixtures. Preserve every canonical file and
  historical migration byte; fresh build/export/FK/integrity checks must
  still pass. No real runtime DB is opened, copied or hashed.

Leave edits unstaged/uncommitted, index empty and branch/HEAD unchanged.
Report exact paths, tests, hashes and limitations. Provide the staging list,
multi-paragraph commit/PR text and stable evidence. Coordinator commits and
pushes in a new Terminal; fresh Opus/High reviews the exact pushed SHA.

## Exclusions

No real migration/import, provider/credential use, network research,
deployment/VPS/backup/restore, generated vault, servers/tunnels or gates.
No runtime schema/content, auth/scoring/quota/lifecycle/retention change,
practice service or HTTP/UI implementation. No unrelated cleanup.
