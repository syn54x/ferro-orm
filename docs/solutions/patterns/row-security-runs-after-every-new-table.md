---
title: Policy drops run before every table change and policy creates after the last
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
related_issues: [753, 995, 1001]
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

## A column is the same problem (#995)

Postgres resolves the columns a policy reads too, and refuses to drop a
column a policy still reads. The first fix kept each existing table's row
security beside that table, so these failed:

```python
class Team(Model):                      # existed, without `name`
    id: int | None = Field(default=None, primary_key=True)
    name: str | None = None

class Document(Model):                  # new in the same pass
    id: int | None = Field(default=None, primary_key=True)
    owner_id: int

    __ferro_rls__: ClassVar = RowSecurity(
        RowPolicy(name="members", using='"owner_id" IN (SELECT id FROM team WHERE name IS NOT NULL)'),
    )
```

The policy ran before `team` had `name`. The plan now runs:

```sql
CREATE TABLE IF NOT EXISTS "document" (...);
ALTER TABLE "team" ADD COLUMN "name" varchar;
ALTER TABLE "document" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "document" FORCE ROW LEVEL SECURITY;
CREATE POLICY "rls_document_members" ON "document" FOR ALL USING (...);
```

The other way round, `account` loses `name` and `document` loses the policy
that read it:

```sql
DROP POLICY "rls_document_members" ON "document";
ALTER TABLE "account" DROP COLUMN "name";
ALTER TABLE "document" NO FORCE ROW LEVEL SECURITY;
ALTER TABLE "document" DISABLE ROW LEVEL SECURITY;
```

One function gives every plan that order, `order_row_security` in `plan.rs`,
run over what `plan_named` planned table by table:

1. every `DropRowPolicy`, ahead of the first table or column change;
2. the table and column changes, as planned;
3. each table's `ENABLE` / `FORCE`, then its `AddRowPolicy` and
   `RebuildRowPolicy` ops, after the last change;
4. every `NO FORCE` / `DISABLE`;
5. the dropped tables and enum types.

The plan's renames stay ahead of all of it: Postgres carries a policy's body
with a rename, and every op names its policy as the renames leave it.

A changed policy is two ops, so that it obeys both ends: a `DropRowPolicy`
among the drops, whose verdict is marked `carried: changed`, and its
`RebuildRowPolicy`, which renders the `CREATE POLICY` alone. The mark keeps
the drop out of drift, which prints the one `policy body differs` line for
the rebuild. A policy carried around a generated column's replace goes the
same way, with its own `carried` mark.

In the reconciliation pass a policy can then no longer commit with its own
table, so on Postgres a plan with a row-security op runs every table op as one
transaction (`table_ops_unit` in `src/migrate.rs`; ADR-0049, amended
2026-11-18). The cost is that the pass holds its locks on every table it
changes until the last statement commits.

Whether the pass is one transaction is decided before it sends its first
statement, from the plan (`plan_changes_row_security`). The first version
decided it when the table ops began, and two statement families had already
committed by then:

- **The create pass.** It built every new table that had no row security or
  deferred key of its own, so a plain new `team` stayed behind when a policy
  on an existing table failed. It now creates no table when the plan changes
  row security (`CreatePass::left_to_plan`); the plan's `AddTable` ops do.
- **The renames ahead of a type statement.** The plan's renames come before
  its `CREATE TYPE`, and the pass ran every type statement in autocommit,
  which ended the transaction, so a table rename committed alone. The pass
  now runs `CREATE TYPE` and `DROP TYPE` inside the transaction, where the
  plan puts them: Postgres creates a type and uses it in one transaction.

The second fix first moved every type statement ahead of the renames, to
keep each in autocommit. That broke a pass that renames table `writer` to
`author` and adds a column of a new enum type named `writer`: a table's row
type holds the table's name until the rename runs, so the guarded
`CREATE TYPE` found a type called `writer`, skipped, and the column's
`ADD COLUMN` failed with `type "writer" does not exist`. A type statement
keeps its place in the plan.

Only a label added to an enum type commits on its own, ahead of the
transaction (`commits_ahead`). Postgres runs `ALTER TYPE … ADD VALUE` inside
a transaction but refuses any use of the new label before it commits, and a
pass may add a label and a column default that uses it. A failed pass can
therefore leave a label added, and `.report` lists it. It is additive and
the next pass finds it in place. The rename of a type that gains a label
commits ahead with it, since the addition names the type by its new name.

Not covered (#1009): a generated migration that is split into expand and
contract steps keeps the order within each step only, since an op's step is
decided by its verdict, not by this order. Two shapes follow:

- The policy of a dropped table waits for the contract with its table (rule
  2 below). When the schema step changes the type of a column that policy
  reads, `ferro migrate up` fails on Postgres with `cannot alter type of a
  column used in a policy definition`.
- A policy that reads a new required column is created in the expand step,
  ahead of the contract's `SET NOT NULL`. That migration applies.

## The two rules for new and dropped tables

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
  the plan's ops for them as one unit (`table_ops_unit` in
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
over the op's table (`created_table`, in `generate/staging.rs`), and a policy
that reads a table that already exists changes nothing: a policy refuses rows
of its own table only. The same ops on a table that already exists are
`contract`. A renamed table is not a new one (`created_table` asks
`table_rename_hint`): it already exists under its old name, holding rows the
previous release writes.

## How to recognize

- A `CREATE POLICY` fails with `relation "…" does not exist` or `column "…"
  does not exist` on something the same plan creates, or a `DROP TABLE` or
  `DROP COLUMN` with `cannot drop … because other objects depend on it`:
  something emitted row security outside the plan's order.
- You are about to make `AddTable` render a flag or a policy, or to give an
  emitter its own ordering: add or move the op in the planner instead, and let
  casebook cases `E4-new-tables-whose-policy-reads-the-other` and `E5` to
  `E7` (a policy that reads a column the plan adds or drops) pin it on every
  emitter.
