# Auto-migrate

Auto-migrate makes the database match the models when you connect. It is rungs 1 and 2 of [the ladder](overview.md#the-ladder): no files and nothing to review, which is what you want in tests, scripts and early development, and what you do not want for a database whose data matters (use [Migrations](migrations.md) there).

A database is managed by auto-migrate or by migrations, never both: on a database that has run a migration, every flag on this page is refused before any DDL ([one door per database](overview.md#one-door-per-database)).

## Creating tables with `auto_migrate=True`

```python
import ferro

await ferro.connect("sqlite:dev.db?mode=rwc", auto_migrate=True)
```

Creates tables for every registered model (including many-to-many join tables) and leaves existing tables untouched, whatever their shape.

## Applying column changes with `migrate_updates`

*Added in 0.11.0.* When models gain or change fields between runs, `migrate_updates=True` reconciles existing tables at connect time:

```python
import ferro

await ferro.connect("sqlite:dev.db?mode=rwc", migrate_updates=True)
```

What it covers depends on what each backend can do in place:

| Change | SQLite | PostgreSQL |
| :--- | :--- | :--- |
| Add missing column | ✅ `ADD COLUMN` | ✅ `ADD COLUMN` |
| Rename a column declared `renamed_from` | ✅ `RENAME COLUMN` | ✅ `RENAME COLUMN` |
| Add the column's index (`index=True`) | ✅ `CREATE INDEX` | ✅ `CREATE INDEX` |
| Add composite index (`__ferro_composite_indexes__`) to existing columns | ✅ `CREATE INDEX` | ✅ `CREATE INDEX` |
| Add table check (`__ferro_checks__`) on CREATE TABLE | ✅ inline CHECK | ✅ inline CHECK |
| Add table check to existing table | ⚠️ `UserWarning`, no DDL; [migrations](migrations.md#sqlite-table-rebuilds) generate the table rebuild | ✅ `ADD CONSTRAINT` |
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
| Change nullability | ⚠️ `UserWarning`, no DDL; migrations generate the table rebuild | ✅ `SET NOT NULL` / `DROP NOT NULL`. `SET NOT NULL` backfills nothing, whatever the default: it fails the connect if any row holds `NULL`. A [migration](data-steps.md) writes the backfill first |
| Drop orphaned Ferro-named index (`idx_*` / `uq_*`) | ✅ with `migrate_destructive=True` | ✅ with `migrate_destructive=True` |
| Redefine an index that keeps its name (a long name cut to 63 characters, two column groups that join to one name, or a live `idx_*` / `uq_*` index written another way) | ✅ `DROP INDEX` + `CREATE INDEX` under `migrate_updates`. A unique one over duplicate values fails the connect, counting them | ✅ same, in the table's transaction |
| Add a missing enum label (a `StrEnum` grew a member) | ✅ nothing to do — enums store as text | ✅ `ALTER TYPE ... ADD VALUE` *0.18.0+* |
| Rename an enum label declared with `__ferro_renamed_labels__` | ✅ nothing to do | ✅ `ALTER TYPE ... RENAME VALUE` |
| Remove an enum label | ✅ nothing to do | ⚠️ `UserWarning`, no DDL. [Migrations](migrations.md) generate it with its [backfill](data-steps.md#removing-an-enum-label) |
| Inline single-column `UNIQUE` on an existing column, index option changes | ❌ never here; [migrations](migrations.md) generate it | ❌ never here; migrations generate it |
| Rename a table | ✅ `RENAME TABLE` under `migrate_updates` when the database holds the old name from `__ferro_renamed_from__` and not the new one; derived index and check names follow. Without `migrate_updates` a `UserWarning` names the table, the hint and both doors, and nothing is created. [Migrations](migrations.md#renames) are the reviewed path | ✅ same |
| Drop a table | ❌ never here; [migrations](migrations.md#reading-a-migration) generate it, marked destructive | ❌ same |
| Change a primary key | ❌ never; no door generates it. Migrations refuse it with [the recipe](migrations.md#changing-a-primary-key) | ❌ same |

Rules worth knowing:

- **NOT NULL additions need a literal default.** Existing rows must get a value, so a new required field without a literal default fails the connect:

    ```text
    Cannot add NOT NULL column 'author.bio' to an existing table: it has no literal default to backfill existing rows. Make the field nullable, give it a literal default, or generate a reviewed migration with `ferro migrate new`.
    ```

    A migration splits that change into an expand step, a [backfill](data-steps.md) you write, and a contract step. Json-family fields (`dict` / `list` / nested model) may use a JSON object or array — `Field(default={})` or `default_factory=dict` / `list` — as that literal. The factory is called once when the column spec is compiled, not per row: `lambda: {"id": str(uuid4())}` freezes one UUID onto every existing row, the same as writing that dict in `default=`. Postgres drops the backfill `DEFAULT` after the add. SQLite has no `DROP DEFAULT`, so on SQLite the pass adds the column nullable, fills existing rows with an `UPDATE`, and warns that the `NOT NULL` needs a table rebuild, which `ferro migrate new` writes (same as `default="draft"`): ferro never leaves a server `DEFAULT` behind.
- **Added columns reuse the exact `CREATE TABLE` DDL**, so a database brought forward by `migrate_updates` matches one created fresh, and `alembic revision --autogenerate` stays clean afterwards.
- **Only ferro-owned constraints are rebuilt.** FK reconciliation matches the `fk_<table>_<col>_<to_table>` names ferro emits (just as index reconciliation only touches `idx_*`/`uq_*`). A drifting constraint with any other name is left untouched and reported with a `UserWarning` — user-created schema survives auto-migrate. Rebuilding is metadata-only: rows are never touched, and the new `ADD CONSTRAINT` validates existing rows, failing loudly (and rolling back the table's plan on Postgres) if they violate it.
- **Table checks and column `db_check` share the `ck_*` prefix.** Every ferro-owned `ck_*` — table checks from `__ferro_checks__` and column checks from `Field(db_check=True)` — participates in the same reconciliation pass on PostgreSQL: missing checks are added on `migrate_updates`, same-name body drift triggers a rebuild, and orphaned ferro-owned checks drop only on `migrate_destructive`. A leftover `ck_*` that the model no longer declares stays live under `migrate_updates` and emits a `UserWarning` (silence would leave the database rejecting rows the model now allows).
- **Postgres type changes take an exclusive lock** and fail the connect if existing data does not cast cleanly — fine for a development flag, but worth knowing. Every statement waits for its table lock under the [`ddl_lock_timeout`](../../reference/configuration.md#ddl_lock_timeout) of the project configuration (default `5s`), retried up to ten times before the connect fails.
- **The pool refreshes after any schema change**, so no cached statement or stale identity-mapped instance can observe the pre-migration schema.

### SQLite: warn and skip

SQLite cannot add, change or drop a constraint, a type or a nullability on a table that exists; the only way is to create the table again in its new shape and copy the rows across (a *table rebuild*). Auto-migrate never rebuilds a table: it raises a `UserWarning` naming the object and the door that can, and changes nothing (ADR-0014). For example:

```text
Check constraint 'ck_author_kind' on column 'author.kind' is declared but missing from the live table, and SQLite cannot add a constraint to an existing column (it requires a full table rebuild). The invariant is not database-enforced; generate a reviewed migration with `ferro migrate new` to apply it.
```

[Migrations](migrations.md#sqlite-table-rebuilds) generate the rebuild as a reviewed step. Row-level security is Postgres-only on every door: SQLite gets one warning per table and no DDL.

## Evolving enums: label addition

*Added in 0.18.0.* On PostgreSQL, `StrEnum` fields create a **native enum type**, and a type that already exists in the database does not learn new members on its own. When a `StrEnum` grows, `migrate_updates=True` performs **label addition**: it compares the model's members against the live type and appends what's missing with `ALTER TYPE ... ADD VALUE IF NOT EXISTS`.

!!! danger "This gap is invisible to your tests"
    Under plain `auto_migrate=True` (without `migrate_updates`), an existing enum type is **never** updated — like every existing object, it belongs to the update pass. The failure mode is nasty: every test suite that creates its schema fresh gets the complete enum and stays green, while every *existing* database rejects the new member at runtime with `invalid input value for enum`. No app-side test against a throwaway schema can catch this. If your models' enums evolve, run with `migrate_updates=True`, or generate the change as a [migration](migrations.md) (the [Alembic bridge](alembic.md) sees the same drift).

=== "Assignment"

    ```python
    from enum import StrEnum

    import ferro
    from ferro import Model


    class Provider(StrEnum):
        PLAID = "plaid"
        MX = "mx"  # new member — the live type only has 'plaid'


    class Feed(Model):
        id: int | None = ferro.Field(primary_key=True, default=None)
        provider: Provider


    await ferro.connect("postgres://...", migrate_updates=True)
    # → ALTER TYPE "provider" ADD VALUE IF NOT EXISTS 'mx'

    feed = await Feed.create(provider=Provider.MX)
    recent = await Feed.where(lambda feed: feed.provider == Provider.MX).all()
    ```

=== "Annotated"

    ```python
    from enum import StrEnum
    from typing import Annotated

    import ferro
    from ferro import FerroField, Model


    class Provider(StrEnum):
        PLAID = "plaid"
        MX = "mx"  # new member — the live type only has 'plaid'


    class Feed(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        provider: Provider


    await ferro.connect("postgres://...", migrate_updates=True)
    # → ALTER TYPE "provider" ADD VALUE IF NOT EXISTS 'mx'

    feed = await Feed.create(provider=Provider.MX)
    recent = await Feed.where(lambda feed: feed.provider == Provider.MX).all()
    ```

The contract, precisely:

- **Append-only, metadata-only.** Label addition adds labels and does nothing else; rows are never touched. A shared `StrEnum` used by several models is one type and reconciles once.
- **Removals are never automatic here.** A live label the model no longer declares raises a `UserWarning` naming the type and labels — rows may still hold that label, and older code may still be running against the schema mid-deploy — and the label stays. A label you declare renamed with `__ferro_renamed_labels__` ([Renames](migrations.md#renames)) is renamed in place; a migration removes one with a generated [backfill and contract](data-steps.md#removing-an-enum-label).
- **Labels commit before table changes.** Additions run as their own autocommit statements ahead of the per-table plans, so a new column whose literal default is a brand-new member works in a single deploy, on every supported PostgreSQL version.
- **Appended labels sort last.** `ADD VALUE` appends: a member inserted mid-enum in Python lands at the end of the database ordering, and `ORDER BY` on an enum column follows *database* order, not declaration order.
- **SQLite is unaffected.** Enums store as text there; a new member needs no DDL.

## Destructive drops with `migrate_destructive`

*Added in 0.11.0.* Also **drop** live columns that no longer exist on the model (never whole tables):

```python
import ferro

await ferro.connect("sqlite:dev.db?mode=rwc", migrate_destructive=True)
```

Dropping is dependency-aware and fails loudly rather than skipping silently:

- Explicit indexes covering a dropped column are dropped first (they would be orphaned anyway).
- Columns that are **primary keys**, enforced by table constraints, or referenced by other tables' **foreign keys** abort with an error naming the constraint and pointing at `ferro migrate new`, which writes the drop as a reviewed [migration](migrations.md).

## On-demand `migrate()`

Run the same pass explicitly on a live connection instead of at connect time:

```python
import ferro

await ferro.migrate()                  # create missing tables + apply updates (default)
await ferro.migrate(destructive=True)  # also drop removed columns
await ferro.migrate(using="service")   # against a named connection
```

`ferro.create_tables()` runs only the create pass. Both are refused on a database that has run a migration, as the flags are.

## What the pass did: `PassReport`

`ferro.migrate()` and `ferro.create_tables()` return a `PassReport`: every statement the pass sent to the database, in order, and every warning it raised. Say `Author` gains a `slug` field:

=== "Assignment"

    ```python
    import ferro
    from ferro import Model


    class Author(Model):
        id: int | None = ferro.Field(primary_key=True, default=None)
        name: str
        slug: str | None = None  # new
    ```

=== "Annotated"

    ```python
    from typing import Annotated

    from ferro import FerroField, Model


    class Author(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        slug: str | None = None  # new
    ```

```python
report = await ferro.migrate()

[(s.subject, s.sql) for s in report.statements if s.role == "schema"]
# [('author', 'ALTER TABLE "author" ADD COLUMN "slug" varchar')]

[(w.kind, str(w)) for w in report.warnings]
# []
```

The report is built from what the pass actually executed, never from its plan, so it never lists a statement that did not run.

- **`statements`** is a tuple of `ExecutedStatement(subject, sql, role)`. `subject` is the table or enum type the statement belongs to. `role` is one of:
    - `"schema"`: the create pass, the enum type statements and the reconciliation;
    - `"lock_timeout"`: the `SET LOCAL lock_timeout` (or `SET` / `RESET`) the pass wraps each Postgres unit in, so a statement never queues behind a long lock;
    - `"probe"`: the row read SQLite costs for a [label rename](#evolving-enums-label-addition) on a column with no check.
- **`warnings`** is a tuple of `Report(kind, subject, text, recurs)`; `str(warning)` is its sentence. Every warning is still raised as a `UserWarning` too. `kind` names it, so code can match on it rather than on the text:
    - the planner's and renderer's kinds: `LeftoverChecks`, `ExtraEnumLabels`, `ForeignFkDrift`, `HintRefused`, the row-security kinds (`DroppedRowSecurity`, `ForeignPolicies`, `UnverifiablePolicy`, `PolicyBodyReplaced`, `RowSecurityTeardown`, `ExtraPolicies`), `RefusedConversion`, `SqliteInPlace`, `PrimaryKeyKept`, `EnumTypeMove` (a column moving to or from a native enum type, reported with the recipe a migration follows), `RowSecuritySkipped`;
    - the pass's own: `PendingTableRename`, `StrandedLabelRename`, `RowSecurityUnderMigrator`, `RunLockWait` (it waited for the run lock) and `DdlLockRetry` (a statement timed out waiting for a table lock and its unit is retried).

    `recurs` is `True` for a warning raised on every pass until someone acts.

A pass that fails partway raises its usual error with `.report` set to what committed before the failure, the failing statement left out. On Postgres each table is its own transaction, so earlier tables stay changed, and the report says which:

```python
try:
    await ferro.migrate()
except ferro.OperationalError as error:
    changed = {s.subject for s in error.report.statements if s.role == "schema"}
```

`connect(..., auto_migrate=True)` runs the same pass and returns nothing; its failure carries the same `.report`. The `ferro` logger's debug lines name each statement as it runs, but they are free text: read the report, not the log.

## Two processes at once

Every auto-migrate pass takes the same run lock `ferro migrate up` takes, so two processes booting together never collide: the second waits, saying so with a `UserWarning`, and sees the first one's DDL:

```text
connect(auto_migrate=…) is waiting: another ferro migration run or auto-migrate pass holds the run lock on this database. It goes on once that one finishes.
```

It waits up to the project's [`lock_timeout`](../../reference/configuration.md#lock_timeout) (default `30s`, the same wait `ferro migrate up` gives another run; `"0"` waits without a limit), then refuses:

```text
connect(auto_migrate=…) gave up waiting for the run lock on public: another ferro migration run or auto-migrate pass held it longer than lock_timeout (30s). Nothing was applied. Wait for that run to finish and try again, or raise lock_timeout; `ferro migrate status` shows a migration run while it holds the lock.
```

A database ferro migrations govern is refused before the pass waits at all, so a boot never sits behind a `ferro migrate up` only to be refused once it finishes.

On Postgres the lock is a session-level advisory lock, so auto-migrate is refused behind a transaction-mode connection pooler; connect to the database directly to migrate.

## Safety guidance

!!! danger "Never use destructive auto-migration in production"
    `auto_migrate` and its extension flags are for development and tests, while the schema is still moving. `migrate_destructive` deletes data the moment a field is removed from a model. For production, use [migrations](migrations.md): renames, data transforms and the changes SQLite can only make by rebuilding a table live there, reviewed before they run.

## See Also

- [Schema Management overview](overview.md) — the ladder and one door per database
- [Connections & Databases](../connections.md) — `connect()` options
- [Adopting migrations on an existing database](../../howto/adopting-migrations.md) — moving a database auto-migrate built onto migrations
