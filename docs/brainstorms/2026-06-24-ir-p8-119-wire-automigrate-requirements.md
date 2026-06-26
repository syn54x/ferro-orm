# Requirements: Wire runtime auto_migrate to ferro-migrate IR planner (#119)

**Date:** 2026-06-24
**Epic:** [#117](https://github.com/syn54x/ferro-orm/issues/117)
**Issue:** [#119](https://github.com/syn54x/ferro-orm/issues/119)
**Phase:** 8 — Runtime migration IR cutover (`v0.13.0`)

---

## Problem

`plan_table_migration` in `src/migrate.rs` already builds SchemaIR snapshots and calls `plan_from_ir` / `emit_sql`, but discards the results and still executes the legacy enriched-JSON `properties` diff walk. Runtime `connect(auto_migrate=True)` and `ferro.migrate` therefore do not use the IR pipeline that #118 completed.

Two parallel planners exist with no runtime cutover. Phase 8 requires the IR path to become the **primary executor** while the legacy planner remains available as a **deprecated shadow reference** until Phase 9 ([#108](https://github.com/syn54x/ferro-orm/issues/108)).

---

## Goal

Make `plan_table_migration` produce the same `(statements, drop_columns, warnings)` the executor already consumes, but sourced from `plan_from_ir` + `emit_sql_with_ir` instead of the JSON walk — with **observably identical behavior** for the supported `auto_migrate` capability matrix.

---

## Success criteria

1. `plan_table_migration` executes the ferro-migrate IR pipeline as its only hot-path planner.
2. Legacy JSON diff logic lives in an isolated, clearly deprecated helper (e.g. `plan_table_migration_legacy`) retained for #120 shadow comparison — not removed.
3. `tests/test_migrate_plan.py` and `tests/test_auto_migrate.py` pass on SQLite + Postgres without changing public Python APIs.
4. Semantics preserved: `migrate_updates`, `migrate_destructive`, fail-loud NOT NULL without default, Postgres type/nullability reconciliation, SQLite index-aware column drops at execution time, PK drop guard.
5. No `_typed_plan` / `_typed_sql` discard scaffolding remains in the hot path.

---

## Scope

### In scope

- IR-primary `plan_table_migration` wiring in `src/migrate.rs`.
- Map ferro-migrate `EmissionResult` + filtered `MigrationOp` list → runtime `MigrationPlan` (`statements`, `drop_columns`, `warnings`).
- Respect `MigrateOptions`: `updates=false` → empty plan; `destructive=false` → omit `DropColumn` ops.
- Keep `drop_columns` separate from `statements` so `execute_drop_column` can resolve SQLite index dependencies (executor unchanged).
- Minimal IR adapter enrichment so existing render-level tests pass:
  - `schema_json_to_schema_ir`: foreign keys, `db_check` constraints, `enum_type_name` where present in JSON schema.
  - `live_columns_to_schema_ir`: Postgres native enum UDT marker on live columns (for “leave enum columns to Alembic” reconciliation).
- Extract current JSON walk into `plan_table_migration_legacy`.
- Map `EmissionError` → `PyValueError` with actionable messages (match legacy error tone).
- Doc note: runtime IR cutover + deprecated legacy shadow path.

### Out of scope (#120 / Phase 9)

- IR vs legacy shadow comparison rewrite (`shadow_compare_migration_plan`).
- `FERRO_SHADOW_RUNTIME` / `FERRO_SHADOW_RUNTIME_STRICT` CI enforcement.
- Full single-sourcing of `schema_json_to_schema_ir` / `live_columns_to_schema_ir` with `build_column_plan` (consolidation when parity harness exists).
- Removing the legacy planner ([#108](https://github.com/syn54x/ferro-orm/issues/108)).
- Alembic / `create_tables` parity exit gates.

---

## Approaches considered

### A. Thin adapter cutover (recommended)

Wire IR plan + `emit_sql_with_ir` as primary. Enrich JSON→IR adapters only enough for `test_migrate_plan.py` parity. Extract legacy helper unchanged for #120.

**Pros:** Smallest correct cutover; #118 emission logic is already tested; legacy preserved for shadow.
**Cons:** Temporary duplication between adapters and `build_column_plan` until #120 consolidates.

### B. Consolidate adapters first, then wire

Refactor `schema_json_to_schema_ir` to share `build_column_plan` metadata extraction before switching the hot path.

**Pros:** Less duplication long-term.
**Cons:** Larger blast radius; mixes #119 execution wiring with #120 consolidation; harder to bisect parity failures.

### C. Dual-run with legacy execution

Run IR and legacy planners, compare, execute legacy until parity proven.

**Rejected:** Violates I-6 (stop-gap). Phase 8 already defers removal, not primary execution, to shadow comparison in #120.

---

## Key decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Primary planner | `plan_from_ir` + `emit_sql_with_ir` | #118 deliverable; AGENTS.md I-1 alignment |
| `drop_columns` handling | Partition `DropColumn` ops before emit; populate `drop_columns` vec | Executor relies on `execute_drop_column` for SQLite index deps |
| Legacy planner | `plan_table_migration_legacy`, deprecated, not hot path | #120 shadow gate; Phase 9 removal |
| Adapter scope | Minimal enrichment in #119 | Unblock render tests; full consolidation in #120 |
| Shadow compare | No change in #119 | Owned by #120 |
| Public API | Unchanged | `connect`, `migrate`, `_render_migration_sql_for_test` signatures stable |

---

## Risks

| Risk | Mitigation |
|------|------------|
| IR adapters omit FK/check metadata → different ADD COLUMN SQL | Enrich `schema_json_to_schema_ir`; pin with existing `test_migrate_plan.py` |
| Live enum UDT not modeled → spurious Postgres ALTER TYPE | Add live-side enum marker; skip type reconcile in emit when live column is native enum |
| `emit_sql_with_ir` DROP in statements breaks executor | Never emit drops into `statements`; route to `drop_columns` |
| SQLite cosmetic type drift false positives | Rely on #118 `sqlite_type_storage_drift`; existing reconcile tests |

---

## Dependencies

- #118 merged (`emit_sql_with_ir` complete on integration branch).
- `feat/ir-p8-migrate-cutover` integration branch.

---

## Outstanding questions

None blocking — issue #119 and Phase 8 roadmap define scope. Adapter consolidation depth is explicitly deferred to #120.
