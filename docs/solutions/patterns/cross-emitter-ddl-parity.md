---
title: Cross-emitter DDL parity
type: pattern
tags: [convention, invariant, schema, migrations, alembic, sqlalchemy, sea-query]
related_files:
  - AGENTS.md
  - src/ferro/migrations/alembic.py
  - src/ferro/migrations/translate.py
  - crates/ferro-migrate/src/generate/mod.rs
  - src/schema.rs
  - tests/test_cross_emitter_parity.py
  - tests/_casebook.py
  - tests/test_alembic_autogenerate.py
  - tests/test_schema_constraints.py
related_issues: [32, 120, 511, 533, 538]
related_prs: [36]
captured: 2026-04-28
---

## Problem

Ferro emits DDL through three doors: the **reconciliation pass** behind
`connect(auto_migrate=True)`, the **migrations door** (`ferro migrate new`:
`src/ferro/migrations/` + `crates/ferro-migrate/src/generate/`), and the
**Alembic autogenerate bridge**, which translates the one planner's ops
(ADR-0041). Future emitters (`ferro schema dump`, a `Ferro.to_sql()` API, an
introspection diff tool) will exist too.

```python
class Author(Model):
    ...
    bio: str | None = None   # the edit
```

```text
connect(migrate_updates=True)   ALTER TABLE "author" ADD COLUMN "bio" varchar
ferro migrate new               01_schema.up.postgres.sql: the same statement
alembic --autogenerate          op.add_column('author', sa.Column('bio', sa.String(), nullable=True))
```

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

**Every DDL emitter must use the same names for the same artifacts.** Since
FF-B (B3), the canonical name builders live in ONE place —
`crates/ferro-ddl-lowering/src/lib.rs` — and Python consumes them over FFI
(`_core._ddl_*`); the IR compiler and the Alembic bridge carry the resulting
names through the IR rather than re-deriving them. Derived column *types* are
equally single-sourced via `resolve_column_storage` (see
`derived-type-and-naming-decision-table.md`).

The current canonical conventions:

| Artifact                    | Name                                       |
| --------------------------- | ------------------------------------------ |
| Single-column index         | `idx_<table>_<col>`                        |
| Composite index             | `idx_<table>_<col1>_<col2>...`             |
| Single-column unique        | `uq_<table>_<col>` (standalone unique index) |
| Composite unique            | `uq_<table>_<col1>_<col2>...`              |
| Single-column `db_check`    | `ck_<table>_<col>`                         |
| Foreign key                 | `fk_<table>_<col>_<reftable>` (always named) |
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

## The migrations door's six pins

`ferro migrate new` plans with the same planner as the pass
(`ferro_migrate::plan_from_ir`) and renders through the same `render_plan`; it
only decides which step an op lands in. Some of what it writes is an *online
shape* the pass, inside its one transaction, never needs. Each is pinned to
the pass's rendering in `tests/test_cross_emitter_parity.py`, over every
casebook change in `tests/_casebook.py` (A1–F4, built from the generator
tests' own models), on both dialects:

| Pin | The migrations door writes | Pinned to the pass's |
| --- | -------------------------- | -------------------- |
| (a) | every generated DDL step, headers stripped, through `normalize_online_shape` | plan for the same before/after (`_core._plan_from_ir(..., render=True)`, two snapshots) |
| (b) | a SQLite rebuild's `CREATE TABLE "_ferro_new_<t>"` and the indexes it re-creates | create pass for the shape the step leaves, apart from the name |
| (c) | `CREATE [UNIQUE] INDEX CONCURRENTLY` / `DROP INDEX CONCURRENTLY IF EXISTS` | statement, but for one token (`IF NOT EXISTS` against `CONCURRENTLY`) |
| (d) | the validate, label-addition and type-creation statements | statement for the same op, through one renderer |
| (e) | a database taken through the chain | an auto-migrated database: no drift, the same live schema and facts; autogenerate empty against it |
| (f) | — (the bridge) | every statement the revision runs as written is the pass's, and the revision leaves the pass's schema |

The plain twins pin (a) compares by: a `NOT VALID` add is the plain add,
`CONCURRENTLY` is gone (the leading crash-leftover `DROP INDEX CONCURRENTLY`
of a build has no twin), a `VALIDATE CONSTRAINT` and the
`_ferro_notnull_<table>_<col>` staging are nothing (the `SET NOT NULL` stays),
and a rebuild (a SQLite table, or a Postgres enum type losing a label) stands
for the pass's statements of the ops it carries. Around a backfill the pass
has no statement for the value existing rows lack, so the pass side is two
plans that the backfill joins: parent → relaxed shape → target.

A pin that fails against merged behaviour is listed in `FINDINGS` in that file
as `xfail(strict=True)` with its reason, so the fix flips it and must remove
the entry.

## Recipe: adding a new artifact

1. Pick the name format and add it to the table in `AGENTS.md` § I-1.
2. Decide it in one `ferro_ddl_lowering` function and plan and render it
   through `plan_from_ir` / `render_plan`, so every door gets it in the same
   PR:
   - Python: extend `_FERRO_NAMING_CONVENTION` with the appropriate
     SQLAlchemy convention key (`ix`, `uq`, `fk`, `pk`, `ck`), and give the
     bridge's `translate.py` the op (Alembic's own, or the pass's statement).
   - Rust: add a helper next to `composite_index_name` and use it
     consistently in `src/schema.rs`.
3. Add a casebook case to `tests/_casebook.py` (and its generator test), so
   pins (a)–(f) cover it, and a parity test that asserts the names match.
4. Do not edit `CHANGELOG.md`: release tooling writes it (AGENTS.md I-10).

## Recipe: adding a new emitter

1. Plan with `plan_from_ir` and render with `render_plan`; read the canonical
   names in `_FERRO_NAMING_CONVENTION` and the `composite_*_name` helpers —
   those are the source of truth. Never a second renderer.
2. Run all existing parity tests against your emitter.
3. Add a pin to `tests/test_cross_emitter_parity.py` that compares your
   emitter's output with `_core._plan_from_ir(..., render=True)` for every
   casebook case on both dialects, as pin (a) does for the migrations door.
   The casebook covers single-column index, composite index, single-column
   unique, composite unique, FK with shadow column, default values and
   nullability; add a case for anything it lacks.
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

**CI enforcement:**

- `tests/test_migrate_plan.py` — render-level auto-migrate diff matrix (Python SchemaIR producer).
- `tests/test_cross_emitter_parity.py` — Alembic vs post-migrate database, and
  the migrations door's six pins over the casebook.
- `tests/test_db_type_cross_emitter_parity.py` — token vocabulary across emitters.
