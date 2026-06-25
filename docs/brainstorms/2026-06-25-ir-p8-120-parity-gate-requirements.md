# Requirements: Migration parity gate and IR vs legacy shadow (#120)

**Date:** 2026-06-25
**Epic:** [#117](https://github.com/syn54x/ferro-orm/issues/117)
**Issue:** [#120](https://github.com/syn54x/ferro-orm/issues/120)
**Phase:** 8 — Runtime migration IR cutover (`v0.13.0`)

---

## Problem

#119 cut `plan_table_migration` over to `plan_from_ir` + `emit_sql_with_ir`, but the Phase 8 parity gate is not yet real:

1. **`shadow_compare_migration_plan` is a no-op check** — it runs the IR path twice (JSON roundtrip self-compare), not IR vs `plan_table_migration_legacy`. `FERRO_SHADOW_RUNTIME_STRICT=1` in CI therefore cannot catch IR↔legacy drift.
2. **Adapters are duplicated** — `schema_json_to_schema_ir` / `live_columns_to_schema_ir` in `src/migrate.rs` partially mirror `build_column_plan` / `canonical_column_type`. #119 fixed known gaps (anyOf strings, Postgres type tokens, `db_check` naming) via targeted patches; consolidation was deferred here.
3. **Cross-emitter exit coverage is thin** — `test_cross_emitter_parity.py` pins Alembic vs auto-migrate for create and migrate_updates sentinel paths, but there is no systematic IR-vs-legacy matrix across the `auto_migrate` capability ladder.

Until this work lands, Phase 8 cannot close with confidence that runtime IR migration matches the legacy planner and the Alembic/`create_tables` emitters (AGENTS.md I-1).

---

## Goal

Make **IR vs legacy planner parity** a enforced, test-backed contract for the supported `auto_migrate` capability matrix (SQLite + Postgres), and keep **Alembic / `create_tables` exit tests** green as the external DDL parity sentinel.

---

## Success criteria

1. `shadow_compare_migration_plan` diffs **IR primary** (`plan_table_migration`) vs **`plan_table_migration_legacy`** on `statements`, `drop_columns`, and `warnings`.
2. With `FERRO_SHADOW_RUNTIME=1` and `FERRO_SHADOW_RUNTIME_STRICT=1`, `internal_migrate` fails loudly when IR and legacy disagree (existing hook in `src/migrate.rs`; behavior becomes meaningful).
3. Explicit parity tests cover the capability matrix: add column (nullable, NOT NULL + default, indexed, unique, FK, `db_check`), Postgres type/nullability reconcile, destructive drops, PK drop guard, empty plan when `updates=false`.
4. `tests/test_cross_emitter_parity.py` and existing `test_migrate_plan.py` / `test_auto_migrate.py` remain green on SQLite + Postgres.
5. JSON→IR adapters are **single-sourced or shared** with `build_column_plan` / `property_json_type_and_format` where feasible — no independent type-inference tables that can drift again.
6. Legacy planner remains **deprecated, not removed** (Phase 9 / [#108](https://github.com/syn54x/ferro-orm/issues/108)).

---

## Scope

### In scope

- Rewire `shadow_compare_migration_plan` (IR vs legacy).
- Rust unit/integration parity suite: IR plan == legacy plan per scenario (both backends where applicable).
- Optional test-only FFI helper to expose migration shadow diff for `tests/test_shadow_reports.py` fixtures (mirror query shadow pattern).
- Adapter consolidation: route `schema_json_to_schema_ir` column metadata through shared lowering (`build_column_plan` outputs or extracted shared helpers in `src/schema.rs` / `ferro-ddl-lowering`).
- Update `docs/solutions/patterns/cross-emitter-ddl-parity.md` or add `docs/solutions/patterns/ir-legacy-migration-parity.md` with the shadow gate recipe.
- Mark legacy JSON walk `#[deprecated]` with planned removal `v0.14.0` (note in module docs; Rust deprecation attribute where applicable).
- Phase 8 roadmap / migration-guide checkbox updates when merged.

### Out of scope (Phase 9 / #108)

- Removing `plan_table_migration_legacy` or the JSON properties walk.
- New public Python APIs.
- Alembic bridge rewrites beyond existing parity tests.
- Runtime dual-execution (execute legacy when IR differs) — rejected per I-7.

---

## Approaches considered

### A. Shadow rewire + explicit parity matrix + adapter consolidation (recommended)

1. Fix `shadow_compare_migration_plan` first so CI strict mode is meaningful.
2. Add parameterized Rust tests that assert byte-identical plans for IR vs legacy across the matrix.
3. Consolidate adapters onto `build_column_plan` / shared helpers; delete duplicate inference in `infer_schema_db_type` where redundant.

**Pros:** Matches Phase 8 roadmap ordering; failures bisect cleanly (shadow → unit matrix → consolidation).
**Cons:** Consolidation may surface latent diffs requiring emitter or legacy alignment decisions.

### B. Consolidate adapters first, then shadow

Refactor adapters before turning on IR vs legacy compare.

**Pros:** Fewer expected diffs when shadow first runs.
**Cons:** Larger initial diff; shadow stays false confidence until the end; harder to prove consolidation didn't change behavior without the matrix.

### C. Python-only parity tests (no shadow rewire)

Add pytest that calls a new `_shadow_compare_migration_plan_for_test` without wiring runtime strict compare.

**Rejected:** Leaves `FERRO_SHADOW_RUNTIME_STRICT` meaningless on migrate path; duplicates the enforcement surface.

---

## Key decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Shadow compare | IR (`plan_table_migration`) vs `plan_table_migration_legacy` | Phase 8 deliverable; #119 preserved legacy for this |
| Compare fields | `statements`, `drop_columns`, `warnings` (order-sensitive for statements) | Matches existing shadow shape |
| Strict failure | Keep existing `PyRuntimeError` on strict mismatch | Consistent with query/create-table shadow |
| Adapter strategy | Shared lowering via `build_column_plan` / `property_json_type_and_format` | I-1; eliminates duplicate inference |
| Legacy removal | Deferred to Phase 9 | Parity confidence required first |
| Known intentional SQLite divergences | Document + exclude from matrix where Alembic parity tests already carve out (inline UNIQUE/FK on ADD COLUMN) | `test_cross_emitter_parity.py` precedent |

---

## Risks

| Risk | Mitigation |
|------|------------|
| IR vs legacy byte diffs on edge cases (warnings text, statement order) | Normalize compare or sort where order is immaterial; pin exact order only where executor depends on it |
| Consolidation changes IR output | Land matrix tests before deleting duplicate adapter logic; fix IR to match legacy (legacy is reference until Phase 9) |
| Shadow strict breaks CI on first rewire | Run matrix locally first; fix #119 residual gaps before enabling strict in same PR |
| `plan_table_migration_legacy` rots | `#[cfg(test)]` direct calls + shadow hook keep it exercised |

---

## Dependencies

- #118 and #119 merged on `feat/ir-p8-migrate-cutover`.
- `plan_table_migration_legacy` present in `src/migrate.rs`.

---

## Outstanding questions

None blocking — issue #120 and Phase 8 roadmap define scope. If consolidation reveals systematic legacy bugs, fix IR to match legacy for in-scope matrix (legacy is the shadow reference until Phase 9), or document an intentional change with Alembic parity proof.
