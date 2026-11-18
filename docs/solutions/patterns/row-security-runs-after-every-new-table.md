---
title: A new table's row security runs after every new table, and its policies go before any drop
type: pattern
tags: [invariant, gotcha, migrations, schema, rust]
related_files:
  - crates/ferro-migrate/src/plan.rs
  - crates/ferro-migrate/src/render.rs
  - src/schema.rs
  - src/migrate.rs
  - src/ddl_exec.rs
  - crates/ferro-migrate/src/run_plan/step_class.rs
  - tests/test_row_security_create_pass.py
  - tests/_casebook.py
related_issues: [753, 1001]
captured: 2026-11-17
---

## Problem

Postgres resolves the tables a policy's SQL reads when the policy is created,
and refuses to drop a table while another table's policy reads it. A policy on
`document` that reads `membership` failed with `relation "membership" does not
exist` whenever both tables were new, because each table's policies ran right
after its own `CREATE TABLE` and tables are ordered by foreign keys only.

## Takeaway

The planner orders row security around the tables, never the tables around
row security. Every emitter gets the order from the plan and decides nothing.

```python
class Document(Model):
    id: int | None = Field(default=None, primary_key=True)
    owner_id: int

    __ferro_rls__: ClassVar = RowSecurity(
        RowPolicy(name="members", using='"owner_id" IN (SELECT user_id FROM membership)'),
    )

class Membership(Model):
    id: int | None = Field(default=None, primary_key=True)
    user_id: int
```

The plan from no tables to these two models, and back:

```sql
CREATE TABLE IF NOT EXISTS "document" (...);
CREATE TABLE IF NOT EXISTS "membership" (...);
ALTER TABLE "document" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "document" FORCE ROW LEVEL SECURITY;
CREATE POLICY "rls_document_members" ON "document" FOR ALL USING (...);

DROP POLICY "rls_document_members" ON "document";
DROP TABLE "membership";
DROP TABLE "document";
```

## The two rules

Both live in `plan_named` (`crates/ferro-migrate/src/plan.rs`).

1. **Create.** A new table's `ENABLE`, `FORCE` and policies are ops of their
   own (`EnableRowSecurity`, `ForceRowSecurity`, `AddRowPolicy`, from
   `beyond_the_create`), placed after the plan's last `AddTable`: tables in
   create order, each table's statements in the order
   `row_security_statements` gives them. An `AddTable` renders the table, its
   indexes and its checks, and no row security. On SQLite the plan has no such
   op and the `AddTable` carries the one warning.
2. **Drop.** Every Ferro-owned policy (`rls_` name) of a table the plan drops
   is a `DropRowPolicy` ahead of the plan's first `DropTable`. The flags are
   not turned off, since the `DROP TABLE` takes them, and a policy Ferro does
   not own is never touched (ADR-0019). The op's verdict says
   `goes_with: dropped_table`, which keeps it in the generator's contract step
   beside its table when the migration has a data step: dropped earlier, a
   forced table with no policy would show that data step no rows.

## Why the tables are not ordered by what a policy reads

A raw policy body is SQL Ferro does not parse (ADR-0019), so it cannot know
which tables a policy reads. And two policies may read each other's tables,
which no order of the tables satisfies. Running every policy after every table
needs neither.

## The create pass is one transaction

A table used to be its own Postgres transaction, policies included, so a
failed policy left no table. With the policies after the last table, that unit
has to be the whole set. On Postgres, when any new table of a pass declares
row security, the pass runs every
`CREATE TABLE` with its indexes and checks, then every table's row security,
as one transaction under the DDL lock timeout. A failure rolls back all of
them, the error names the table and the statement, and `.report` lists none
(ADR-0049, amended 2026-11-17). A pass with no row security among its new
tables keeps one transaction per table.

The pass never decides where row security goes. Which code runs that one
transaction depends on whether a plan follows:

- **`auto_migrate=True` alone, or `create_tables()`.** No plan follows, so
  the create pass asks the planner for one, from no tables to the tables it
  creates (`planned_create_unit` in `src/schema.rs`), and runs its ops in
  order, each as the plan renders it (since #304 the `AddTable`s too: only
  the plan knows which foreign key a `CREATE TABLE` leaves out, see
  `deferred-foreign-keys.md`).
- **`migrate_updates`.** The create pass creates none of those tables and
  hands them over (`CreatePass::left_to_plan`). The reconciliation pass runs
  the plan's ops for them as one unit (`new_tables_unit` in
  `src/migrate.rs`), beside the tables held back for a rename and after the
  plan's renames. So a policy may read a table the pass renames, or one that
  could only be created after a rename.

The first version of this fix let the create pass run row security for the
tables it built, before the reconciliation pass had renamed anything or
created the held-back tables. A policy on a built table that read a held-back
one failed with the old `relation … does not exist`. Two places decided the
order, and they disagreed as soon as a rename was in the pass.

The unit's statements belong to several tables, so the executor takes
`Sender::PassEach(&tables)`: each statement is reported under its own table.

A generated schema step and an Alembic revision were already one transaction.
They needed the order only.

## The step is still expand

The ops placed after the last `CREATE TABLE` are part of creating their table.
A step's class (ADR-0054) asks whether a write of the previous release can
fail beside the step, and the previous release never writes a table the step
creates. So `op_class` classes any op on such a table `expand`, by one test
over the op's table (`existing_table`), and a policy that reads a table that
already exists changes nothing: a policy refuses rows of its own table only.
The same ops on a table that already exists are `contract`.

## How to recognize

- A `CREATE POLICY` fails with `relation "…" does not exist` on a table the
  same plan creates, or a `DROP TABLE` with `cannot drop table … because other
  objects depend on it`: something emitted row security outside the plan's
  order.
- You are about to make `AddTable` render a flag or a policy, or to give an
  emitter its own ordering: add or move the op in the planner instead, and let
  casebook case `E4-new-tables-whose-policy-reads-the-other` pin it on every
  emitter.
