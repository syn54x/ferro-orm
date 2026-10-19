# Migrations

This page lists the facts the schema-management guides link to: the headers a step file can carry, the class of each step, what each door covers, what auto-migrate does on each backend, and what a `PassReport` holds. A generated index step shows a header at work:

```sql
-- 0002_author_nickname/02_idx_author_nickname.up.postgres.sql
-- ferro: no-transaction

DROP INDEX CONCURRENTLY IF EXISTS "idx_author_nickname";

CREATE INDEX CONCURRENTLY "idx_author_nickname" ON "author" ("nickname");
```

The `no-transaction` header tells the runner to open no transaction around the step, because Postgres refuses `CREATE INDEX CONCURRENTLY` inside one. The [Migrations](../guide/schema/migrations.md) guide walks through the files a migration holds.

## Step file headers

A step file may open with `-- ferro: …` lines. The generator writes them, except `expand` and `irreversible`, which only a person writes.

| Header | Meaning |
| :--- | :--- |
| `-- ferro: destructive` | The step discards data: it drops a column, a table or an enum type. Review is the gate; nothing refuses it. |
| `-- ferro: data-dependent` | The step discards nothing but fails on rows that do not satisfy it: a `NOT NULL`, a unique over existing rows, a type cast, the validation of a staged constraint, a removed enum label. |
| `-- ferro: no-transaction` | The runner opens no transaction around the step, so a statement such as `CREATE INDEX CONCURRENTLY` can run. The step is written to be safe to re-run from its first statement. |
| `-- ferro: foreign-keys-off` | A SQLite [table rebuild](../guide/schema/migrations.md#sqlite-table-rebuilds): the runner turns foreign-key enforcement off while the step runs. |
| `-- ferro: not-applicable` | This dialect has nothing to do at this step number. |
| `-- ferro: expand` | In a [hand-written SQL step](../guide/schema/migrations.md#hand-written-steps): its author declares it *expand*, so the previous release's writes keep succeeding beside it (see [step classes](#step-classes)). A hand-written step without it is *contract*. The generator never writes it: a generated step's class comes from what the step does. |
| `-- ferro: nothing-to-reverse <reason>` | In a `.down` file: reverting removes the step's record and runs nothing. |
| `-- ferro: irreversible <reason>` | In a `.down` file: a run that would revert this step refuses before reverting anything. Only a person declares one. |

!!! note "Headers an older release does not know"
    A ferro release refuses a step file that carries a header it does not know, naming the file. A file with `-- ferro: expand` is refused by a release that predates the header.

## Step classes

Each step is *expand* or *contract*, decided from what the step does and recorded with the step when it is applied. `require_applied(allow_ahead="expand-only")` lets an older release start on a database that is ahead of it only by expand steps; the [two-release contract](../guide/schema/deploying.md#rolling-deploys-and-the-two-release-contract) shows it in a deploy.

| Expand | Contract |
| :--- | :--- |
| adding a table, or a nullable column | dropping a table or a column |
| `DROP NOT NULL` | `SET NOT NULL`: a required column's add-constraint and contract steps |
| a plain index; dropping an index, a check, a foreign key or a unique | a unique index; a type change |
| adding an enum label | renaming a column, a table, an enum type or a label; removing a label |
| a data step, a guard step | adding a check or a foreign key (`NOT VALID`) and its validate step |
| a hand-written SQL step whose file says `-- ferro: expand` | any other hand-written SQL step |

- A SQLite table rebuild takes the class of what it carries.
- A step that runs nothing on a dialect (`not-applicable`) is expand there.
- A generated step you edit by hand is classed like a hand-written one: contract unless its file says `-- ferro: expand`.

## What each door covers

The three doors are [auto-migrate](../guide/schema/auto-migrate.md) (here its update rung, `migrate_updates`), [migrations](../guide/schema/migrations.md) and the [Alembic bridge](../guide/schema/alembic.md).

| Change | Auto-update | Migrations | Alembic bridge |
| :--- | :--- | :--- | :--- |
| Add a table, a nullable column, an index, an enum label | ✅ | ✅ | ✅ |
| Add a check, a foreign key, change a type or nullability, on Postgres | ✅ | ✅ | ✅ |
| The same on SQLite (needs a table rebuild) | ⚠️ warns, no DDL | ✅ generated [table rebuild](../guide/schema/migrations.md#sqlite-table-rebuilds) | ❌ refused at autogenerate |
| Rename a column (`renamed_from`) or an enum label (`__ferro_renamed_labels__`) | ✅ | ✅ [declared on the model](../guide/schema/renames-and-retirements.md#rename-hints) | ✅ same hints |
| Rename a table (`__ferro_renamed_from__`) | ✅ `ALTER TABLE … RENAME TO` under `migrate_updates`, indexes and checks renamed with it; without `migrate_updates` the pass warns and creates nothing | ✅ | ✅ |
| Drop a column | with `migrate_destructive` | ✅ marked `-- ferro: destructive` | ✅ marked `# ferro: destructive` |
| Drop a table | ❌ | ✅ marked `-- ferro: destructive` | ✅ marked `# ferro: destructive` |
| Add a new required column to a table with rows | only with a literal default (backfills existing rows) | ✅ [expand, backfill, contract](../guide/schema/data-steps.md) generated | Postgres: ⚠️ the plain op, marked `# ferro: data-dependent`; the backfill is yours to write. SQLite: ❌ refused at autogenerate |
| Make a nullable column required | Postgres: `SET NOT NULL`, fails if any row is `NULL` (write it as a migration with a backfill); SQLite: warns, no DDL | ✅ expand, backfill, contract generated | Postgres: ⚠️ the plain op, marked `# ferro: data-dependent`. SQLite: ❌ refused at autogenerate |
| Remove an enum label rows may hold | warns, no DDL | ✅ [backfill and contract](../guide/schema/data-steps.md#removing-an-enum-label) generated | ❌ not written; [migrations](../guide/schema/data-steps.md#removing-an-enum-label) generate it |
| Python data steps over the models as they were | ❌ | ✅ [data steps](../guide/schema/data-steps.md) | hand-written `op.execute(...)` |
| Change a primary key | ❌ | refused, with [the recipe](../guide/schema/migrations.md#changing-a-primary-key) | ❌ refused, with the same recipe |

## Auto-migrate changes per backend

What `connect(..., migrate_updates=True)` and `ferro.migrate()` do to an existing table, change by change. `migrate_destructive=True` adds the drops marked so.

| Change | SQLite | PostgreSQL |
| :--- | :--- | :--- |
| Add missing column | ✅ `ADD COLUMN` | ✅ `ADD COLUMN` |
| Rename a column declared `renamed_from` | ✅ `RENAME COLUMN` | ✅ `RENAME COLUMN` |
| Add the column's index (`index=True`) | ✅ `CREATE INDEX` | ✅ `CREATE INDEX` |
| Add composite index (`__ferro_composite_indexes__`) to existing columns | ✅ `CREATE INDEX` | ✅ `CREATE INDEX` |
| Add table check (`__ferro_checks__`) on CREATE TABLE | ✅ inline CHECK | ✅ inline CHECK |
| Add table check to existing table | ⚠️ `UserWarning`, no DDL; [migrations](../guide/schema/migrations.md#sqlite-table-rebuilds) generate the table rebuild | ✅ `ADD CONSTRAINT` |
| Rebuild table check on body drift | ⚠️ `UserWarning`, no DDL; migrations generate the table rebuild | ✅ rebuild: `DROP CONSTRAINT` + `ADD CONSTRAINT` |
| Add column `db_check=True` on existing column | ⚠️ `UserWarning`, no DDL; migrations generate the table rebuild | ✅ `ADD CONSTRAINT` |
| Leftover ferro check (`ck_*` live, removed from model) under `migrate_updates` | ⚠️ `UserWarning`, constraint stays | ⚠️ `UserWarning`, constraint stays |
| Drop orphaned ferro check (`ck_*`) | ⚠️ `UserWarning`, no DDL; migrations generate the table rebuild | ✅ with `migrate_destructive=True` |
| Add unique column (`unique=True`) | ✅ via explicit unique index + warning | ✅ inline `UNIQUE` |
| Add foreign-key column | ✅ column only, no FK constraint + warning | ✅ column + FK constraint |
| Add missing FK constraint to an existing column | ⚠️ `UserWarning`, no DDL; migrations generate the table rebuild | ✅ `ADD CONSTRAINT` |
| Drop a ferro foreign key (`fk_*`) from a column the model keeps (`team: Annotated[Team, ForeignKey(...)]` became `team_id: int`) | ⚠️ `UserWarning`, constraint stays; migrations generate the table rebuild | ✅ with `migrate_destructive=True`: `DROP CONSTRAINT` |
| Change a foreign key's `on_delete` (or target) | ⚠️ `UserWarning`, no DDL; migrations generate the table rebuild | ✅ rebuild: `DROP CONSTRAINT` + `ADD CONSTRAINT` |
| Change column type | ⚠️ `UserWarning`, no DDL (SQLite type affinity makes drift mostly cosmetic); migrations generate the table rebuild | ✅ `ALTER COLUMN ... TYPE ... USING` cast |
| Change nullability | ⚠️ `UserWarning`, no DDL; migrations generate the table rebuild | ✅ `SET NOT NULL` / `DROP NOT NULL`. `SET NOT NULL` backfills nothing, whatever the default: it fails the connect if any row holds `NULL`. A [migration](../guide/schema/data-steps.md) writes the backfill first |
| A server default no model declares (a *foreign default*, see [destructive drops](../guide/schema/auto-migrate.md#destructive-drops-with-migrate_destructive)) | ⚠️ `UserWarning`, default stays; only a hand rebuild drops it (an expression, or any default on a nullable column; a literal on a `NOT NULL` column is ferro's own) | ⚠️ `UserWarning` under `migrate_updates`; ✅ with `migrate_destructive=True`: `DROP DEFAULT` |
| Drop orphaned Ferro-named index (`idx_*` / `uq_*`) | ✅ with `migrate_destructive=True` | ✅ with `migrate_destructive=True` |
| Redefine an index that keeps its name (a long name cut to 63 characters, two column groups that join to one name, or a live `idx_*` / `uq_*` index written another way) | ✅ `DROP INDEX` + `CREATE INDEX` under `migrate_updates`. A unique one over duplicate values fails the connect, counting them | ✅ same, in the table's transaction |
| Add a missing enum label (a `StrEnum` grew a member) | ✅ nothing to do: enums store as text | ✅ `ALTER TYPE ... ADD VALUE` |
| Rename an enum label declared with `__ferro_renamed_labels__` | ✅ nothing to do | ✅ `ALTER TYPE ... RENAME VALUE` |
| Remove an enum label | ✅ nothing to do | ⚠️ `UserWarning`, no DDL. [Migrations](../guide/schema/migrations.md) generate it with its [backfill](../guide/schema/data-steps.md#removing-an-enum-label) |
| Inline single-column `UNIQUE` on an existing column, index option changes | ❌ never here; [migrations](../guide/schema/migrations.md) generate it | ❌ never here; migrations generate it |
| Rename a table | ✅ `ALTER TABLE … RENAME TO` under `migrate_updates` when the database holds the old name from `__ferro_renamed_from__` and not the new one; derived index and check names follow. Without `migrate_updates` a `UserWarning` names the table, the hint and both doors, and nothing is created. [Migrations](../guide/schema/renames-and-retirements.md#rename-hints) are the reviewed path | ✅ same |
| Drop a column | ✅ with `migrate_destructive=True` | ✅ with `migrate_destructive=True` |
| Drop a table | ❌ never here; [migrations](../guide/schema/migrations.md#reading-a-migration) generate it, marked destructive | ❌ same |
| Change a primary key | ❌ never; no door generates it. Migrations refuse it with [the recipe](../guide/schema/migrations.md#changing-a-primary-key) | ❌ same |

## `PassReport` contents

`ferro.migrate()` and `ferro.create_tables()` return a [`PassReport`](../api/connection.md#ferro.PassReport): every statement the pass executed, in order, as an `ExecutedStatement`, and every warning it raised, as a `Report`. [On-demand `migrate()`](../guide/schema/auto-migrate.md#on-demand-migrate) shows one read in code.

An `ExecutedStatement`'s `role` says what the statement was for:

| `role` | The statement |
| :--- | :--- |
| `"schema"` | DDL: the create pass, the enum type statements, the reconciliation. |
| `"lock_timeout"` | The `SET LOCAL lock_timeout`, `SET` and `RESET` lines that bound each statement's wait for its table lock on Postgres. |
| `"probe"` | The SQLite query that looks for rows still holding a label the model renamed. |

A `Report`'s `kind` names the warning, so code can match on it rather than on the sentence. `subject` says what it is about (the modelset, a table, a column or an enum type), and `recurs` is `True` when the warning comes back on every pass until someone acts.

| `kind` | Raised when |
| :--- | :--- |
| `HintRefused` | A declared rename hint cannot be meant; nothing is renamed. |
| `LeftoverChecks` | Live ferro-owned checks (`ck_*`) the model no longer declares. |
| `ExtraEnumLabels` | Live labels of an enum type the model no longer declares; the labels stay. |
| `ForeignFkDrift` | A foreign key drifts from the model under a name ferro does not own; it is left untouched. |
| `DroppedRowSecurity` | The live table carries row security the model no longer asks for; it stays. |
| `ForeignPolicies` | Live policies ferro does not own; they are never touched. |
| `UnverifiablePolicy` | A raw policy body ferro cannot verify; it is left as it is. |
| `PolicyBodyReplaced` | A raw policy was rebuilt for its metadata, replacing its live body. |
| `RowSecurityTeardown` | `migrate_destructive` tore down row security on a table, naming the policies dropped. |
| `ExtraPolicies` | Live ferro-owned policies the model no longer declares; they stay. |
| `ForeignDefault` | A live server default no model declares; it stays until `migrate_destructive` or a hand rebuild drops it. |
| `RefusedConversion` | A storage conversion that could reinterpret or destroy stored values; it is not executed. |
| `SqliteInPlace` | A change SQLite cannot make to an existing table in place: it needs the table rebuild only a migration writes. |
| `PrimaryKeyKept` | The declared primary key differs from the live one; no door changes a primary key in place. |
| `EnumTypeMove` | A column moves to or from a native Postgres enum type, which no statement converts in place. |
| `RowSecuritySkipped` | Row security a table declares that the database does not apply: SQLite has none, or the create pass met the table already there. |
| `PendingTableRename` | A table rename hint is live, and a pass without `migrate_updates` holds the table back. |
| `StrandedLabelRename` | A SQLite column whose rows still hold a label the model renamed. |
| `RowSecurityUnderMigrator` | The migrating role is itself bound by the `FORCE` policies it maintains. |
| `RunLockWait` | The pass waited for another run's lock. |
| `DdlLockRetry` | A statement timed out waiting for its table lock and is retried. |

Every warning is also raised as a `UserWarning`. The [API reference](../api/connection.md#ferro.PassReport) documents each field.

## Exit codes

Every `ferro migrate` verb shares one set of exit codes; the [CLI reference](cli.md#exit-codes) lists them.
