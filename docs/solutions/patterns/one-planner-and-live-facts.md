---
title: One planner over two snapshots; the live database is an IR plus LiveFacts
type: pattern
tags: [convention, invariant, gotcha, migrations, schema]
related_files:
  - crates/ferro-migrate/src/plan.rs
  - crates/ferro-migrate/src/render.rs
  - src/live_ir.rs
  - src/migrate.rs
  - tests/test_pass_recording.py
related_issues: [511, 517]
captured: 2026-10-06
---

## The shape

Every migration door asks one question: what turns snapshot `old` into
snapshot `new`? `ferro_migrate::plan_from_ir(old, new, dialect, &facts,
options)` answers it for the whole modelset; `render_plan` turns each op into
the exact statements. The reconciliation pass is now just:

```text
live_schema_ir(engine, tables)  ->  (live IR, LiveFacts)
plan_from_ir(live, declared, …) ->  MigrationPlan
render_plan(plan, live, declared, dialect) -> Vec<RenderedOp>
execute (type ops autocommit; each table's ops in one Postgres transaction)
```

`LiveFacts` carries what the IR cannot say: CHECK bodies and row policies as
the catalog prints them, validity flags, live enum labels. A table absent from
`LiveFacts.tables` reads as the `old` snapshot declares it, so
`LiveFacts::declared()` is the side-table for snapshot-vs-snapshot planning.

## Gotchas

- **A new deciding rule goes in `ferro_ddl_lowering`, never in `plan.rs`.**
  The planner only decides *where* a verdict lands. Row security is translated
  from `plan_row_security_reconcile`'s result into ops; a pin
  (`row_security_ops_render_byte_identical_to_the_reconcile_decision`) holds
  the rendered ops byte-equal to that function's statements.
- **Declared vs declared needs the declared side's live view.** The
  storage-drift decision reads `postgres_native_enum` on its live side; a
  declared old snapshot does not set it, so the planner sets it from
  `resolve_column_storage` (`declared_live_view`). Without it two identical
  enum columns read as drift.
- **The storage decision is `resolve_column_storage`, on both sides.** The
  drift check used the bare string cascade for the model side, so a SQLite
  enum (`varchar(<longest label>)`) always read as a phantom
  `AlterColumnType` that rendered to nothing. A plan that renders nothing is
  still not empty to a drift check.
- **`AddTable` ops in the pass belong to the create pass.** The pass reads
  only tables that existed before the create pass (ADR-0010), so tables it
  just created plan as adds, which the executor skips with their warnings.
- **The pass is pinned by its recorded output.** `tests/test_pass_recording.py`
  re-runs the pass's suites under a recorder plugin and compares every
  executed statement, warning and `_render_migration_sql_for_test` result
  with `tests/fixtures/pass_recording/`. Re-record only for an intended
  behaviour change (`FERRO_PASS_RECORD=1`).
