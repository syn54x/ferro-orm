---
description: "Make the database match your models every time you connect, with no files to write or review."
---

# Auto-migrate

Auto-migrate makes the database match the models when you connect, with no files and nothing to review. Take the overview's `Author`, which has just gained a `nickname`:

=== "Assignment"

    ```python
    --8<-- "docs/examples/migrations_quickstart.py:models"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/migrations_quickstart_annotated.py:models"
    ```

The development database `dev.db` was built before that change: its `author` table has no `nickname` column, still has a `bio` column the model has since dropped, and holds one row, Ada Lovelace's. Connect with `migrate_updates=True`, and the connect adds the column before your code runs:

```python {test=run prelude=auto_migrate}
import ferro

await ferro.connect("sqlite:dev.db?mode=rwc", migrate_updates=True)
async with engines.session():
    ada = await Author.where(lambda author: author.email == "ada@example.com").first()
    assert ada.nickname is None
```

The connect ran `ALTER TABLE "author" ADD COLUMN "nickname" varchar` and created `idx_author_nickname`; Ada's row is still there, with no nickname yet. The ORM calls run inside a [session](../connections.md#sessions), as every block on this page does.

Auto-migrate is rungs 1 and 2 of [the ladder](overview.md#the-ladder). It suits tests, scripts and early development, and does not suit a database whose data matters: use [Migrations](migrations.md) there. A database is managed by auto-migrate or by migrations, never both: on a database that has run a migration, every flag on this page is refused before any DDL ([one door per database](overview.md#one-door-per-database)).

## Creating tables with `auto_migrate=True`

Pass `auto_migrate=True` to `connect()` and every missing table is created:

```python {test=run prelude=auto_migrate}
import ferro

await ferro.connect("sqlite:test.db?mode=rwc", auto_migrate=True)
async with engines.session():
    grace = await Author.create(
        name="Grace Hopper", email="grace@example.com", nickname="amazing grace"
    )
    assert grace.id == 1
```

The empty `test.db` gets an `author` table with every column and index the model declares. `auto_migrate=True` creates the tables of every registered model, many-to-many join tables included, and leaves every table that already exists untouched, whatever its shape: on `dev.db` it would have added no `nickname`.

## Applying column changes with `migrate_updates`

`migrate_updates=True`, the flag in the first example, also brings existing tables to the models' shape, as far as one in-place statement can:

- A new column, a new index, a column declared `renamed_from` and a new enum label are added or renamed on both backends.
- A change of type or nullability, and a new or changed check or foreign key, run on PostgreSQL. SQLite can only make them by rebuilding the table, so there the connect raises a `UserWarning` and runs nothing ([below](#sqlite-changes-that-need-a-table-rebuild)).
- Nothing is dropped unless you also pass `migrate_destructive=True`, and a table never is.

The [per-backend table](../../reference/migrations.md#auto-migrate-changes-per-backend) lists every change the pass makes or refuses, checks, foreign keys, defaults, index redefinitions and renames included.

Rules worth knowing:

- **A new required column needs a literal default.** Existing rows must get a value, so a new required field without a literal default fails the connect:

    <!-- shows: the error connect() raises for this change on an existing table -->
    ```text
    Cannot add NOT NULL column 'author.bio' to an existing table: it has no literal default to backfill existing rows. Make the field nullable, give it a literal default, or generate a reviewed migration with `ferro migrate new`.
    ```

    Nothing was added. A migration splits that change into an expand step, a [backfill](data-steps.md) you write, and a contract step.

    - Json-family fields (`dict`, `list` or a nested model) may use a JSON object or array as that literal: `Field(default={})`, or `default_factory=dict` / `list`.
    - Postgres drops the backfill `DEFAULT` after the add.
    - SQLite has no `DROP DEFAULT`, so on SQLite the column keeps the literal as its `DEFAULT`, the same as `default="draft"`: the column is `NOT NULL` as the model says, and a raw `INSERT` that leaves it out gets the model's value. It is the one server default Ferro leaves behind; a migration from `ferro migrate new` rebuilds the table without it.

    !!! warning "A `default_factory` backfills one value into every row"
        The factory is called once, when the column's DDL is compiled, not once per row. `default_factory=lambda: {"id": str(uuid4())}` writes the same UUID onto every existing row, as if you had written that dict in `default=`.

- **Added columns reuse the exact `CREATE TABLE` DDL**, so a database brought forward by `migrate_updates` matches one created fresh, and `alembic revision --autogenerate` stays clean afterwards.
- **Only Ferro-owned constraints are rebuilt.** Foreign-key reconciliation matches the `fk_<table>_<col>_<to_table>` names Ferro emits, as index reconciliation only touches `idx_*` and `uq_*`. A drifting constraint with any other name is left untouched and reported with a `UserWarning`, so schema you created by hand survives auto-migrate. Rebuilding is metadata-only: rows are never touched, and the new `ADD CONSTRAINT` validates existing rows, failing loudly (and rolling back the table's plan on Postgres) if they violate it.
- **Table checks and column `db_check` share the `ck_*` prefix.** Every Ferro-owned `ck_*` (table checks from `__ferro_checks__`, column checks from `Field(db_check=True)`) is reconciled the same way on PostgreSQL: a missing check is added under `migrate_updates`, a check whose body drifted is rebuilt, and an orphaned one drops only under `migrate_destructive`. A leftover `ck_*` the model no longer declares stays live under `migrate_updates` and raises a `UserWarning`, because the database would otherwise go on rejecting rows the model now allows without saying so.
- **Postgres type changes take an exclusive lock** and fail the connect if existing data does not cast cleanly. Every statement waits for its table lock under the [`ddl_lock_timeout`](../../reference/configuration.md#ddl_lock_timeout) of the project configuration (default `5s`), retried up to ten times before the connect fails.
- **The pool refreshes after any schema change**, so no cached statement or stale identity-mapped instance can observe the pre-migration schema.

### SQLite changes that need a table rebuild

SQLite cannot add, change or drop a constraint, a type or a nullability on a table that exists; the only way is to create the table again in its new shape and copy the rows across (a *table rebuild*). Auto-migrate never rebuilds a table, because a rebuild drops whatever the copy does not carry. It raises a `UserWarning` naming the object and the door that can, and changes nothing. Declare `db_check=True` on an existing column `author.kind`, for example, and the connect warns:

<!-- shows: the error connect() raises for this change on an existing SQLite table -->
```text
Check constraint 'ck_author_kind' on column 'author.kind' is declared but missing from the live table, and SQLite cannot add a constraint to an existing column (it requires a full table rebuild). The invariant is not database-enforced; generate a reviewed migration with `ferro migrate new` to apply it.
```

The connect goes on, and the column stays unchecked. [Migrations](migrations.md#sqlite-table-rebuilds) generate the rebuild as a reviewed step. A *foreign default* (a server default on a column that no model declares, [below](#destructive-drops-with-migrate_destructive)) is the one exception: no migration plans it, since the migrations' snapshots never hold it, so the warning names the hand rebuild that drops it, or a hand-written SQL step that does (`ferro migrate new <name> --sql-step drop_default`). Row-level security is Postgres-only on every door: SQLite gets one warning per table and no DDL.

## Evolving enums: label addition

On PostgreSQL, a `StrEnum` field creates a **native enum type**, and a type that already exists in the database does not learn new members on its own. When a `StrEnum` grows, `migrate_updates=True` performs **label addition**: it compares the model's members with the live type and appends what is missing. Say `Author` gains a `plan`, and `Plan` later gains `PRO`:

=== "Assignment"

    ```python {test=compile reason="declares models only; label addition needs Postgres"}
    from enum import StrEnum

    from ferro import Field, Model


    class Plan(StrEnum):
        FREE = "free"
        PRO = "pro"  # new: the live type has only 'free'


    class Author(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str
        email: str = Field(unique=True)
        nickname: str | None = Field(default=None, index=True)
        plan: Plan = Plan.FREE
    ```

=== "Annotated"

    ```python {test=compile reason="declares models only; label addition needs Postgres"}
    from enum import StrEnum
    from typing import Annotated

    from ferro import Field, Model


    class Plan(StrEnum):
        FREE = "free"
        PRO = "pro"  # new: the live type has only 'free'


    class Author(Model):
        id: Annotated[int | None, Field(primary_key=True)] = None
        name: str
        email: Annotated[str, Field(unique=True)]
        nickname: Annotated[str | None, Field(index=True)] = None
        plan: Plan = Plan.FREE
    ```

Connect to the Postgres database with `migrate_updates=True`:

```python {test=compile reason="label addition needs a Postgres database"}
await ferro.connect("postgresql://localhost/app", migrate_updates=True)
```

The connect runs `ALTER TYPE "plan" ADD VALUE IF NOT EXISTS 'pro'`, and from then on an author can be saved with `plan=Plan.PRO`.

!!! warning "Existing enum types without `migrate_updates`"
    Under plain `auto_migrate=True`, an existing enum type is never updated: like every existing object, it belongs to the update pass. Every test suite that creates its schema fresh gets the complete enum and stays green, while every existing database rejects the new member at runtime with `invalid input value for enum`. No test against a throwaway schema can catch this. If your models' enums evolve, run with `migrate_updates=True`, or generate the change as a [migration](migrations.md) (the [Alembic bridge](alembic.md) sees the same drift).

The contract, precisely:

- **Append-only, metadata-only.** Label addition adds labels and does nothing else; rows are never touched. A shared `StrEnum` used by several models is one type and reconciles once.
- **Removals are never automatic here.** A live label the model no longer declares raises a `UserWarning` naming the type and labels, and the label stays: rows may still hold it, and older code may still be running against the schema mid-deploy. A label you declare renamed with `__ferro_renamed_labels__` ([Renames](renames-and-retirements.md#rename-hints)) is renamed in place; a migration removes one with a generated [backfill and contract](data-steps.md#removing-an-enum-label).
- **Labels commit before table changes.** Additions run as their own autocommit statements ahead of the per-table plans, so a new column whose literal default is a brand-new member works in a single deploy, on every supported PostgreSQL version.
- **Appended labels sort last.** `ADD VALUE` appends: a member inserted mid-enum in Python lands at the end of the database ordering, and `ORDER BY` on an enum column follows *database* order, not declaration order.
- **SQLite is unaffected.** Enums store as text there; a new member needs no DDL.

## Destructive drops with `migrate_destructive`

`migrate_destructive=True` also drops live columns that no longer exist on the model, never whole tables. On `dev.db`, it drops the `bio` column the model no longer declares:

```python {test=run prelude=auto_migrate}
import ferro

await ferro.connect("sqlite:dev.db?mode=rwc", migrate_destructive=True)
async with engines.session():
    columns = await ferro.fetch_all("PRAGMA table_info(author)")
    assert [column["name"] for column in columns] == ["email", "id", "name", "nickname"]
```

Ada's `bio` is gone with its column; `nickname` was added in the same connect. Dropping is dependency-aware and fails loudly rather than skipping:

- Explicit indexes covering a dropped column are dropped first, since they would be orphaned anyway.
- Columns that are **primary keys**, enforced by table constraints, or referenced by other tables' **foreign keys** abort with an error naming the constraint and pointing at `ferro migrate new`, which writes the drop as a reviewed [migration](migrations.md).

It also drops a **foreign default**: a server default on a column that Ferro did not leave there. Ferro persists no server default of its own, so a default someone set by hand (`ALTER TABLE "author" ALTER COLUMN "slug" SET DEFAULT ''`) is removed, and the `PassReport` lists the statement:

```sql
ALTER TABLE "author" ALTER COLUMN "slug" DROP DEFAULT
```

Under `migrate_updates` alone the default stays, and the pass warns on every start:

<!-- shows: the warning the pass gives on every start -->
```text
author.slug has default '' in the database, none declared; it is left in place: connect(..., migrate_destructive=True) drops it, or run ALTER TABLE "author" ALTER COLUMN "slug" DROP DEFAULT.
```

Ferro's own two defaults are never foreign: a `serial` key's sequence on Postgres (recognised by the sequence the column owns, so a renamed table's still counts), and on SQLite a literal default on a `NOT NULL` column (what `migrate_updates` keeps when it adds a required column, above). On SQLite any other default (an expression such as `CURRENT_TIMESTAMP`, or any default on a nullable column) is foreign, and the pass warns and leaves it, since only a rebuild drops a default there. `ferro migrate drift` and `ferro migrate baseline` report the same defaults.

## On-demand `migrate()`

`ferro.migrate()` runs the same pass on a connection that is already open, and returns a `PassReport`: every statement the pass sent to the database, in order, and every warning it raised. Run it on `dev.db` and read what it did:

```python {test=run prelude=auto_migrate}
import ferro

await ferro.connect("sqlite:dev.db?mode=rwc")
report = await ferro.migrate()
assert [(s.subject, s.sql) for s in report.statements if s.role == "schema"] == [
    ("author", 'ALTER TABLE "author" ADD COLUMN "nickname" varchar'),
    ("author", 'CREATE INDEX IF NOT EXISTS "idx_author_nickname" ON "author" ("nickname")'),
]
assert report.warnings == ()
```

The pass created the column and its index, raised no warning, and left `bio` in place. The report is built from what the pass executed, never from its plan, so it never lists a statement that did not run. `ferro.migrate()` runs the create pass and the updates by default:

- `ferro.migrate(destructive=True)` also drops removed columns, as `migrate_destructive` does (on `dev.db`, `ALTER TABLE "author" DROP COLUMN "bio"`).
- `ferro.migrate(updates=False)` runs only the create pass, as `ferro.create_tables()` does.
- `ferro.migrate(using="service")` runs against a named connection.

Each statement is an `ExecutedStatement` whose `role` says what it was for (`"schema"` for the DDL itself), and each warning a `Report` whose `kind` names it, so code can match on the kind rather than the sentence. Every warning is still raised as a `UserWarning` too. The [API reference](../../api/connection.md#ferro.PassReport) lists every field, role and kind. Both `migrate()` and `create_tables()` are refused on a database that has run a migration, as the flags are.

A pass that fails partway raises its usual error with `.report` set to what committed before the failure, the failing statement left out. On Postgres each table is its own transaction, so earlier tables stay changed, and the report says which:

```python {test=compile reason="needs a pass that fails partway on Postgres"}
try:
    await ferro.migrate()
except ferro.OperationalError as error:
    changed = {s.subject for s in error.report.statements if s.role == "schema"}
```

`changed` names every table whose changes committed. `connect(..., auto_migrate=True)` runs the same pass and returns nothing; its failure carries the same `.report`. Each statement is also a record on the `ferro.sql` logger as it finishes, with its `sql`, `subject` and duration as attributes (see [Logging](../connections.md#logging)); the log is for watching, and the report is still the structured answer.

## Concurrent passes and the run lock

Every auto-migrate pass takes the same run lock `ferro migrate up` takes, so two processes booting together never collide: the second waits, saying so with a `UserWarning`, and sees the first one's DDL:

<!-- shows: the warning a pass gives while another holds the run lock -->
```text
connect(auto_migrate=…) is waiting: another ferro migration run or auto-migrate pass holds the run lock on this database. It goes on once that one finishes.
```

It waits up to the project's [`lock_timeout`](../../reference/configuration.md#lock_timeout) (default `30s`; the setting's `"0"` waits without a limit, unlike the CLI's `--lock-timeout 0`, which refuses at once), then refuses:

<!-- shows: the error a pass raises once lock_timeout runs out -->
```text
connect(auto_migrate=…) gave up waiting for the run lock on public: another ferro migration run or auto-migrate pass held it longer than lock_timeout (30s). Nothing was applied. Wait for that run to finish and try again, or raise lock_timeout; `ferro migrate status` shows a migration run while it holds the lock.
```

The refused process applied nothing. A database Ferro migrations govern is refused before the pass waits at all, so a boot never sits behind a `ferro migrate up` only to be refused once it finishes.

On Postgres the lock is a session-level advisory lock, so auto-migrate is refused behind a transaction-mode connection pooler; connect to the database directly to migrate.

## Safety guidance

!!! warning "Destructive auto-migration deletes data"
    `auto_migrate` and its extension flags are for development and tests, while the schema is still moving. `migrate_destructive` deletes data the moment a field is removed from a model. For production, use [migrations](migrations.md): renames, data transforms and the changes SQLite can only make by rebuilding a table live there, reviewed before they run.

## See also

- [Schema management overview](overview.md): the ladder and one door per database
- [Connections & Databases](../connections.md): `connect()` options
- [Adopting migrations on an existing database](adopting-migrations.md): moving a database auto-migrate built onto migrations
- [Connection API reference](../../api/connection.md#ferro.PassReport): `connect()`, `migrate()`, `create_tables()` and `PassReport`
