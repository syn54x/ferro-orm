---
title: SA models single-column unique as separate UniqueConstraint; Rust emits inline UNIQUE
type: issue
tags: [gotcha, schema, migrations, sqlalchemy, sea-query, invariant, parity]
related_files:
  - src/ferro/migrations/alembic.py
  - src/schema.rs
  - tests/test_cross_emitter_parity.py
related_issues: [32]
related_prs: [36]
captured: 2026-04-28
status: tracked
---

## Problem

For a single-column `FerroField(unique=True)` (or `ForeignKey(unique=True)`)
the two emitters produce equivalent SQL but structurally different schemas:

- **Alembic / SA:** `_build_sa_table` calls `sa.Column(name, type,
  unique=True)`, which SA materializes into a separate `UniqueConstraint`
  object on the table's `constraints` collection. The DDL Alembic emits is
  `CREATE TABLE ... CONSTRAINT uq_<table>_<col> UNIQUE (col)` (with the
  `naming_convention`) or an unnamed inline constraint depending on options.

- **Rust runtime:** sea-query's `col_def.unique_key()` writes the `UNIQUE`
  keyword inline on the column definition: `col TYPE NOT NULL UNIQUE`.
  Equivalent SQL semantics, no separate named constraint.

When `alembic.autogenerate.compare_metadata` reflects the Rust-emitted
schema, it sees an inline-unique column and maps it to a column-level
`unique=True` flag — which doesn't match the metadata's separate
`UniqueConstraint` object, so it proposes adding a `UniqueConstraint`.

The cross-emitter parity sentinel
(`tests/test_cross_emitter_parity.py::test_alembic_autogen_against_rust_migrated_db_is_idempotent`)
filters this specific case via `_is_redundant_single_column_unique` so the
sentinel catches *new* drift without being permanently red.

## Takeaway

Two equally valid fixes; pick one and align both emitters:

**Option A (preferred): make Rust emit named separate UniqueConstraints.**
Use sea-query's `Index::create().unique().name("uq_<table>_<col>")` instead
of the inline `unique_key()`. This preserves the `uq_*` naming convention
already used for composite uniques and makes constraint introspection work
identically across the two paths.

**Option B: make SA emit inline column-level uniques.**
Drop the `naming_convention` for `uq` and rely on SA's column-level
`unique=True` to render inline. This is simpler but loses the named
constraint that operators rely on for `ALTER TABLE ... DROP CONSTRAINT
uq_table_col`.

Either change must be paired with:

1. Removing the `_is_redundant_single_column_unique` filter from
   `tests/test_cross_emitter_parity.py`.
2. Updating any test that asserts on the constraint's name or shape in
   `sqlite_master` / `pg_constraint`.

## How to recognize

- Schema-drift sentinel reports
  `add_constraint UniqueConstraint(Column('<col>', ...))` for any
  single-column `unique=True` field.
- A user dumps `sqlite_master` after `auto_migrate` and sees inline
  `UNIQUE` rather than a named constraint.
