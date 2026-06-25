---
title: Cross-emitter DDL parity
type: pattern
tags: [convention, invariant, schema, migrations, alembic, sqlalchemy, sea-query]
related_files:
  - AGENTS.md
  - src/ferro/migrations/alembic.py
  - src/schema.rs
  - tests/test_alembic_autogenerate.py
  - tests/test_schema_constraints.py
related_issues: [32, 120]
related_prs: [36]
captured: 2026-04-28
---

## Problem

Ferro emits DDL through more than one path: the **Alembic autogenerate bridge**
in Python and the **Rust runtime emitter** behind `connect(auto_migrate=True)`.
Future emitters (a `Ferro.to_sql()` API, a "dump schema" CLI, an introspection
diff tool) will exist too.

If those paths disagree on _any_ schema artifact name — index, unique
constraint, foreign key, check constraint — the user gets **phantom diffs**.
Alembic sees an `ix_*` index that "doesn't exist" in the model and an `idx_*`
index that the model "wants", and proposes a drop+create. The migration is a
no-op but it pollutes history and is unreviewable.

This is exactly what happened in PR #36. Single-column `index=True` columns
emitted `idx_<table>_<col>` from Rust (sea-query default) but `ix_<table>_<col>`
from Alembic (SQLAlchemy default). Composite indexes were already aligned
because Ferro generates the names explicitly through `composite_index_name`.

## Takeaway

**Every DDL emitter must use the same names for the same artifacts.** The
canonical names live in two places and they MUST agree:

- Python: `_FERRO_NAMING_CONVENTION` in `src/ferro/migrations/alembic.py`.
- Rust: hard-coded `format!()` strings in `src/schema.rs` and the `composite_*_name`
  helpers.

The current canonical conventions:

| Artifact                    | Name                                       |
| --------------------------- | ------------------------------------------ |
| Single-column index         | `idx_<table>_<col>`                        |
| Composite index             | `idx_<table>_<col1>_<col2>...`             |
| Single-column unique        | `uq_<table>_<col>`                         |
| Composite unique            | `uq_<table>_<col1>_<col2>...`              |
| Single-column `db_check`    | `ck_<table>_<col>`                         |
| Foreign key (when named)    | `fk_<table>_<col>_<reftable>` *(planned)*  |
| Primary key (when named)    | `pk_<table>` *(planned)*                   |

Canonical column-type vocabulary (`db_type` tokens) is also load-bearing: both
emitters dispatch on the same set of tokens (`text`, `varchar(N)`, `smallint`,
`int`, `bigint`, `uuid`, `timestamp`, `timestamptz`, `date`, `time`). See
`configurable-column-storage-types.md` for the recipe.

This invariant is enforced by paired tests in
`tests/test_alembic_autogenerate.py::test_index_name_matches_rust_runtime_convention_*`,
`tests/test_schema_constraints.py::test_foreign_key_index_runtime_ddl_parity`,
and `tests/test_db_type_cross_emitter_parity.py` (every canonical `db_type`
token × dialect plus `ck_<table>_<col>` parity).

## Recipe: adding a new artifact

1. Pick the name format and add it to the table in `AGENTS.md` § I-1.
2. Wire it into both emitters in the same PR:
   - Python: extend `_FERRO_NAMING_CONVENTION` with the appropriate
     SQLAlchemy convention key (`ix`, `uq`, `fk`, `pk`, `ck`).
   - Rust: add a helper next to `composite_index_name` and use it
     consistently in `src/schema.rs`.
3. Add a parity test that constructs a model, runs both emitters, and asserts
   the rendered names match exactly.
4. Mention the new artifact in `CHANGELOG.md` under the next release.

## Recipe: adding a new emitter

1. Read the canonical names in `_FERRO_NAMING_CONVENTION` and the `composite_*_name`
   helpers — those are the source of truth.
2. Run all existing parity tests against your emitter.
3. Add at least one new parity test in your emitter's test file that covers:
   single-column index, composite index, single-column unique, composite unique,
   FK with shadow column, default values, nullability.
4. Update the bulleted emitter list in `AGENTS.md` § I-1.

## How to recognize the violation

- A user reports "Alembic keeps wanting to drop and recreate an index even
  though I haven't changed anything."
- `alembic revision --autogenerate` against an `auto_migrate=True` database
  produces non-empty diffs immediately after `connect()`.
- A grep for index names returns two different prefixes for what should be the
  same constraint: `rg "(idx_|ix_)<your_table>"`.

If you see any of those, the cross-emitter parity invariant has been broken.
The fix is _always_ to align both emitters, never to silence the diff.

## Migration planner shadow gate (Phase 8 / #120)

Auto-migrate has two Rust planners until Phase 9 ([#108](https://github.com/syn54x/ferro-orm/issues/108)):

- **Primary:** `plan_table_migration` — SchemaIR diff (`plan_from_ir`) + `emit_sql_with_ir`.
- **Reference:** `plan_table_migration_legacy` — enriched-JSON walk via `build_column_plan`.

`shadow_compare_migration_plan` in `src/migrate.rs` diffs IR vs legacy on
`statements`, `drop_columns`, and `warnings`. When `FERRO_SHADOW_RUNTIME=1` is
set, `internal_migrate` runs this compare on every table diff; with
`FERRO_SHADOW_RUNTIME_STRICT=1`, a mismatch aborts the migration.

**When IR and legacy disagree**, align the IR path to legacy (KTD-1 in the
#120 plan) unless legacy is objectively wrong vs Alembic/`create_tables` — then
fix both in the same PR with cross-emitter proof.

**CI enforcement:**

- `cargo test migrate::tests::ir_legacy_parity_matrix` — explicit matrix.
- `tests/test_shadow_reports.py` — `_shadow_compare_migration_plan_for_test` fixtures.
- `tests/test_cross_emitter_parity.py` — Alembic vs post-migrate database.

**Adapter rule:** `schema_json_to_schema_ir` must derive column `db_type` /
nullability / PK metadata from `build_column_plan` (via `canonical_to_db_type_token`
in `src/schema.rs`). Do not add a third independent inference function in
`src/migrate.rs`.
