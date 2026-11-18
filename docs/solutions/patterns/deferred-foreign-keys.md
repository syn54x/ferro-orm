---
title: A foreign key to a table created later is deferred past the last CREATE TABLE on Postgres
type: pattern
tags: [invariant, gotcha, migrations, schema, rust, postgres]
related_files:
  - crates/ferro-migrate/src/order.rs
  - crates/ferro-migrate/src/emit.rs
  - crates/ferro-migrate/src/plan.rs
  - crates/ferro-migrate/src/render.rs
  - src/schema.rs
  - src/migrate.rs
  - src/ferro/migrations/translate.py
  - tests/test_auto_migrate.py
  - tests/_casebook.py
related_issues: [304, 302, 753]
captured: 2026-11-18
---

## Problem

Postgres refuses a `REFERENCES` to a table that does not exist yet, and
refuses to drop a table while another table's key references it. Two new
tables whose foreign keys point at each other have no create order that avoids
the first, and no drop order that avoids the second. `connect(auto_migrate=True)`
failed with `relation "crew" does not exist`.

## Takeaway

The planner defers the key that points forward: it is left out of its
`CREATE TABLE` and added after the plan's last one. Every emitter reads that
from the plan and decides nothing.

```python
class Captain(Model):
    id: int | None = Field(default=None, primary_key=True)
    crew_id: int | None = Field(default=None, foreign_key="Crew")

class Crew(Model):
    id: int | None = Field(default=None, primary_key=True)
    captain_id: int | None = Field(default=None, foreign_key=Captain)
```

The plan from no tables to these two models on Postgres, and back:

```sql
CREATE TABLE IF NOT EXISTS "captain" ( "crew_id" integer, "id" serial PRIMARY KEY NOT NULL );
CREATE TABLE IF NOT EXISTS "crew" ( ..., CONSTRAINT "fk_crew_captain_id_captain" FOREIGN KEY ("captain_id") REFERENCES "captain" ("id") ON DELETE CASCADE );
ALTER TABLE "captain" ADD CONSTRAINT "fk_captain_crew_id_crew" FOREIGN KEY ("crew_id") REFERENCES "crew" ("id") ON DELETE CASCADE;

ALTER TABLE "captain" DROP CONSTRAINT "fk_captain_crew_id_crew";
DROP TABLE "crew";
DROP TABLE "captain";
```

`captain` comes first because the planner orders new tables by name before it
orders them by their keys, so its key to `crew` is the one deferred.

## The three rules

1. **Order.** `order_by_dependencies` (`crates/ferro-migrate/src/order.rs`)
   no longer gives up when no table is ready. It emits the first remaining
   table, in input order, and goes on ordering. A table that only references
   the cycle still follows its target with its key inline, and a cycle of
   three defers one key, not two.
2. **Create, Postgres.** `deferred_foreign_keys`
   (`crates/ferro-migrate/src/emit.rs`) is the one decision: a key of a new
   table whose target is another new table later in that order. `plan_named`
   plans each as the existing `AddForeignKey` op after its last `AddTable`,
   in table order then column order, ahead of the new tables' row security
   (#753): a policy may read through a key, a key never reads a policy. The
   renderer leaves a key out of a `CREATE TABLE` when the plan holds an
   `AddForeignKey` for it, and renders that op plain in every mode, never
   `NOT VALID`, because the table is new and holds no row. A self-reference
   and a key to a table that already exists are never deferred. The name is
   `fk_<table>_<col>_<to_table>`, as for every key.
3. **Drop, Postgres.** Among the tables a plan drops, every key rule 2 would
   defer when creating them is a `DropForeignKey` ahead of the plan's first
   `DropTable`. Its verdict says `goes_with: dropped_table`, so beside a data
   step the generator keeps it in the contract step with its table.

## Why SQLite keeps the key inline

SQLite accepts a `REFERENCES` to a table that does not exist yet, and has no
`ALTER TABLE … ADD CONSTRAINT`. So `deferred_foreign_keys` returns nothing
there: both keys stay inside their `CREATE TABLE`, no `AddForeignKey` or
`DropForeignKey` is planned for a cycle, and only rule 1's order applies.

## The create pass is one transaction

The create pass never revisits a table that exists (ADR-0010). A deferred key
that failed after its tables committed would never be added. So on Postgres
the new tables of a pass are one transaction whenever a statement of theirs
runs after the last `CREATE TABLE` (`MigrationOp::follows_the_creates`): a
deferred key, or row security. That transaction holds every new table of the
pass, not only the tables of the cycle: each `CREATE TABLE` with its indexes
and checks, then the deferred keys, then the row-security statements. A
failure rolls back all of them, the error names the table and the statement,
and `.report` lists none (ADR-0049, amended 2026-11-18). A pass with neither
keeps one transaction per table.

The create pass builds that unit from the plan
(`planned_create_unit` in `src/schema.rs`): it plans from no tables to the
tables it creates and runs every statement the plan renders, the `CREATE
TABLE`s included, since only the plan knows which key each one leaves out.
Under `migrate_updates` it creates none of those tables
(`CreatePass::left_to_plan`) and the reconciliation pass runs the plan's ops
as one unit (`new_tables_unit` in `src/migrate.rs`).

A generated schema step and an Alembic revision are each one transaction
already. The generator needed nothing. The bridge's `translate.py` leaves a
key out of `op.create_table` when the revision holds an `AddForeignKey` for
it, and writes that op as `op.create_foreign_key`.

## How to recognize

- `relation "…" does not exist` on a `CREATE TABLE` whose target is created
  by the same plan, or `cannot drop table … because other objects depend on
  it`: something rendered a table without reading the plan's foreign-key ops.
- You are about to render a `CREATE TABLE` for a plan through
  `render_create_table` directly: it writes every key inline. Render the
  plan's `AddTable` instead.
- A test of create order passes on SQLite only. SQLite accepts forward
  references, so run it on Postgres. Casebook cases
  `B1-new-models-that-reference-each-other` and
  `B2-drop-models-that-reference-each-other` pin both directions on every
  emitter.
- `alembic revision --autogenerate` prints SQLAlchemy's `Cannot correctly
  sort tables` warning for such models. It comes from SQLAlchemy sorting the
  metadata, and the revision is still the planner's.
