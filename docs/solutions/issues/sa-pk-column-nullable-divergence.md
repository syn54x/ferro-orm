---
title: SA bridge marks PK columns nullable=True; Rust emits NOT NULL
type: issue
tags: [gotcha, schema, migrations, sqlalchemy, invariant, parity]
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

When a Ferro model declares a primary key with the conventional pattern
`Annotated[int | None, FerroField(primary_key=True)]`, the SA bridge in
`src/ferro/migrations/alembic.py::_build_sa_table` maps this to
`Column(name, Integer, primary_key=True, nullable=True)`.

The Rust runtime DDL emitter in `src/schema.rs` ignores the `nullable=True`
hint for primary keys and emits `NOT NULL` — which matches SQL semantics on
every supported backend (Postgres, MySQL, SQLite all enforce `PRIMARY KEY`
implies `NOT NULL` regardless of what you ask for).

Result: Alembic's metadata says PK columns are nullable; the live DB says
they are NOT NULL. `compare_metadata` reports a `modify_nullable` op for
every PK on every model. Running the migration would either no-op (PK
constraint forces NOT NULL) or fail.

The cross-emitter parity sentinel
(`tests/test_cross_emitter_parity.py::test_alembic_autogen_against_rust_migrated_db_is_idempotent`)
filters this specific case via `_is_pk_nullable_relaxation` so the sentinel
catches *new* drift without being permanently red.

## Takeaway

This is a real bug in the SA bridge. The fix is to clamp `nullable=False`
when `primary_key=True`, irrespective of what the annotation claims:

```python
# In _build_sa_table when constructing the SA Column
sa_column_kwargs["nullable"] = (
    False
    if sa_column_kwargs.get("primary_key")
    else sa_column_kwargs.get("nullable", True)
)
```

This change must be paired with:

1. Removing the `_is_pk_nullable_relaxation` filter from
   `tests/test_cross_emitter_parity.py`.
2. Reviewing every test that relies on `Annotated[int | None,
   FerroField(primary_key=True)]` — the metadata change shouldn't break them
   because actual DB behavior is unchanged, but Alembic-level introspection
   tests may need updates.
3. Confirming that reading rows back into Python still hydrates the `id`
   field cleanly (the model annotation still says `int | None`, only the SA
   metadata changes).

## How to recognize

- Schema-drift sentinel test (`test_alembic_autogen_against_rust_migrated_db_is_idempotent`)
  reports `modify_nullable` ops for `id` columns on every table.
- A user runs `alembic revision --autogenerate` on an `auto_migrate`'d DB
  and sees a migration proposing to make every `id` column nullable.
