---
title: Shadow FK columns
type: pattern
tags: [convention, schema, relationships, pydantic]
related_files:
  - src/ferro/base.py
  - src/ferro/schema_metadata.py
  - src/ferro/relations.py
related_issues: [32]
related_prs: [36]
captured: 2026-04-28
---

## Problem

Ferro models declare relationships using `Annotated[Target, ForeignKey(...)]`.
This says "this field stores a `Target` instance" — but at the database layer,
the table doesn't store an object, it stores a foreign key column.

So Ferro injects a **shadow column** named `<field>_id` whenever it processes
a `ForeignKey` annotation. That shadow column is what actually appears in the
SQL DDL and the Pydantic JSON schema; the original annotated field is the
hydrated/relationship-aware view.

## Takeaway

When you need to attach a column-level concern (index, unique constraint,
default, comment) to a foreign key, you attach it to the **shadow column** by
threading the flag through the JSON schema property in `build_model_schema`,
not to the original Python field.

The flow:

1. User declares `Annotated[Org, ForeignKey(related_name=..., index=True)]`.
2. `build_model_schema` walks the model, finds the `ForeignKey` metadata, and
   emits a property named `org_id` (the shadow column) with whatever flags it
   should carry.
3. Both emitters (Alembic, Rust) pick up the property and translate it to the
   appropriate DDL.

## Recipe

Adding a new column-level flag to `ForeignKey`:

1. Add the kwarg to `ForeignKey.__init__` and store it on `self`. Use
   keyword-only args (`*` separator) so positional binding stays stable.
2. In `src/ferro/schema_metadata.py`, propagate `metadata.flag_name` onto the
   shadow column's `prop` dict alongside `unique`, `index`, etc.
3. Both Alembic (`_build_sa_table`) and Rust (`src/schema.rs`) already iterate
   over JSON schema properties — if the flag is one they already understand
   (e.g. `index`, `unique`), no further changes are needed. Otherwise, wire
   support into both emitters in the same PR (see
   `cross-emitter-ddl-parity.md`).
4. Test the shadow column by reading `table.c.<field>_id.<attr>` in a
   pytest integration test.

## How to recognize

- The user wants to express something at the FK level that already works on
  plain columns via `FerroField(...)` — that's a strong hint to mirror the
  `FerroField` flag onto `ForeignKey` and route it through the shadow column.
- A test that tries to read `table.c.org` instead of `table.c.org_id` will
  fail with `KeyError`. The schema is keyed by the shadow column name.
