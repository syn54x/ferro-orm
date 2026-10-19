# Data steps and backfills

A data step is a Python step inside a migration, for the part of a change only you can write: the value existing rows get. Add a required `slug` to a table that already has authors, and every existing row needs a slug before the column can be `NOT NULL`:

=== "Assignment"

    ```python
    --8<-- "docs/examples/migrations_backfill.py:models"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/migrations_backfill_annotated.py:models"
    ```

`ferro migrate new author_slug` writes the migration in four steps and says what is left for you:

<!-- output: backfill-new -->
```text
default · migrations
ok Wrote migrations/0002_author_slug/
  01_expand.up.postgres.sql            schema step, up (postgres)
  01_expand.down.postgres.sql          schema step, down (postgres)
  01_expand.up.sqlite.sql              schema step, up (sqlite)
  01_expand.down.sqlite.sql            schema step, down (sqlite)
  02_backfill_author.py                backfill
  03_add_constraint.up.postgres.sql    schema step, up (postgres)
  03_add_constraint.down.postgres.sql  schema step, down (postgres)
  03_add_constraint.up.sqlite.sql      schema step, up (sqlite)
  03_add_constraint.down.sqlite.sql    schema step, down (sqlite)
  04_contract.up.postgres.sql          schema step, up (postgres)
  04_contract.down.postgres.sql        schema step, down (postgres)
  04_contract.up.sqlite.sql            schema step, up (sqlite)
  04_contract.down.sqlite.sql          schema step, down (sqlite)
  ir.json                              snapshot

  1 changed model
    Author
  - 02_backfill_author.py needs writing where it says todo(...); if no author needs a value, delete 0002_author_slug/ and run: ferro migrate new author_slug --no-backfill author.slug
```

The `02_backfill_author.py` step is the one you write; the rest is generated and complete. This whole page runs in [`docs/examples/migrations_backfill.py`](https://github.com/syn54x/ferro-orm/blob/main/docs/examples/migrations_backfill.py): the guard refusing, the backfill written, the step reverted and run again.

## What the generator writes

The migration is split around the rows:

| Step | What it holds |
| :--- | :--- |
| `01_expand` | Only what existing rows already satisfy: `ALTER TABLE "author" ADD COLUMN "slug" varchar;`, nullable. |
| `02_backfill_author.py` | The data step that gives each existing row its value. You write it. |
| `03_add_constraint` | Postgres: `ALTER TABLE "author" ADD CONSTRAINT "_ferro_notnull_author_slug" CHECK ("slug" IS NOT NULL) NOT VALID;`, which refuses every new `NULL` from this point without scanning the table. SQLite: `-- ferro: not-applicable`. |
| `04_contract` | What the rows had to be prepared for, marked `-- ferro: data-dependent`. |

The contract on Postgres validates the staged check, sets `NOT NULL` and drops the check:

```sql
-- 0002_author_slug/04_contract.up.postgres.sql
-- ferro: data-dependent

ALTER TABLE "author" VALIDATE CONSTRAINT "_ferro_notnull_author_slug";

ALTER TABLE "author" ALTER COLUMN "slug" SET NOT NULL;

ALTER TABLE "author" DROP CONSTRAINT "_ferro_notnull_author_slug";
```

Only the validation scans the table, and it blocks no writer while it does.

SQLite cannot make a column `NOT NULL` in place, so its contract is a [table rebuild](migrations.md#sqlite-table-rebuilds):

```sql
-- 0002_author_slug/04_contract.up.sqlite.sql
-- ferro: foreign-keys-off
-- ferro: data-dependent

CREATE TABLE IF NOT EXISTS "_ferro_new_author" ( "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "name" varchar NOT NULL, "slug" varchar NOT NULL );

INSERT INTO "_ferro_new_author" ("id", "name", "slug") SELECT "id", "name", "slug" FROM "author";

DELETE FROM sqlite_sequence WHERE name = '_ferro_new_author';

INSERT INTO sqlite_sequence (name, seq) SELECT '_ferro_new_author', seq FROM sqlite_sequence WHERE name = 'author';

DROP TABLE "author";

ALTER TABLE "_ferro_new_author" RENAME TO "author";

CREATE INDEX IF NOT EXISTS "idx_author_name" ON "author" ("name");
```

The new table takes over the rows, the `AUTOINCREMENT` counter and the name, and the index on `name` is built again on it.

The same split happens for a column that stops accepting `NULL` and for an [enum label rows still carry](#removing-an-enum-label). A migration with no data step has no expand or contract: its DDL is one schema step.

## Writing the backfill

`new` writes the backfill with a `todo(...)` where the value goes:

```python {test=compile reason="a generated step file; the docs tests regenerate it and compare"}
# migrations/0002_author_slug/02_backfill_author.py
# Gives every existing author a value for slug.
# Write each value marked todo, then run `ferro migrate up`.
# If no row needs a value, delete this unapplied migration and run
# `ferro migrate new <name> --no-backfill author.slug` for a guard step instead.
from ferro.migrations import chunked, nothing_to_reverse, todo


@chunked(
    lambda models: models.Author.where(lambda author: author.slug == None)
    .order_by(lambda author: author.id),
    batch_size=1000,
)
async def up(ctx, batch):
    for author in batch:
        author.slug = todo("the slug for an existing author")
        await author.save()


@nothing_to_reverse("01_expand.down.sql drops the column")
def down(ctx): ...
```

The query selects only the rows that still need a value, so running the step again touches only what is left. The runner calls `up` once per batch, inside that batch's own transaction: the step opens no session of its own.

Until you replace the `todo(...)`, `ferro migrate up` refuses the migration before running any of it, naming the file and line:

<!-- output: backfill-unwritten -->
```text
default · sqlite:///./app.db
0002_author_slug/02_backfill_author.py:15: not written yet: the slug for an existing author
Write each step where it says todo(...), then run `ferro migrate up` again. Nothing was applied.
```

Write the value in place of the `todo(...)`:

```python {test=compile reason="one line of the generated backfill above; the example script runs it"}
author.slug = author.name.lower().replace(" ", "-")
```

Then `ferro migrate up` applies the four steps:

<!-- output: backfill-up -->
```text
default · sqlite:///./app.db
0002_author_slug  01_expand           applied (0 ms)
0002_author_slug  02_backfill_author  applied (2 ms)
0002_author_slug  03_add_constraint   applied (0 ms)
0002_author_slug  04_contract         applied (0 ms)
```

Ada Lovelace's row now has the slug `ada-lovelace`, and the column is `NOT NULL`. A `default_factory` from the standard library (`uuid.uuid4`) is pre-filled as its call instead of a `todo`.

## Backfill or guard

When you know no existing row needs a value (the table is new in production, or every writer already sets the column), skip the backfill: `--no-backfill <table>.<column>` writes a **guard step** in its place. A guard is generated and complete; it changes nothing and fails the migration if a row turns out to need a value.

The two migrations side by side:

```text
ferro migrate new author_slug                  ferro migrate new author_slug --no-backfill author.slug

0002_author_slug/                              0002_author_slug/
  01_expand.up.<dialect>.sql                     01_expand.up.<dialect>.sql
  02_backfill_author.py      ← you write it      02_guard_author.py        ← generated, complete
  03_add_constraint.up.<dialect>.sql             03_add_constraint.up.<dialect>.sql
  04_contract.up.<dialect>.sql                   04_contract.up.<dialect>.sql
  ir.json                                        ir.json
```

Only step 02 differs. The guard step `--no-backfill` writes counts the rows that would need a value:

```python {test=compile reason="a generated step file; the docs tests regenerate it and compare"}
# migrations/0002_author_slug/02_guard_author.py
# Generated by `ferro migrate new --no-backfill author.slug`: the migration claims no
# existing author needs a value for slug, and this step checks it on
# every database before the contract makes it required.
from ferro.migrations import MigrationRefused, atomic, nothing_to_reverse


@atomic
async def up(ctx):
    missing = await ctx.models.Author.where(
        lambda author: author.slug == None
    ).count()
    if missing:
        raise MigrationRefused(
            f"{missing} author rows still have NULL in slug, which --no-backfill author.slug said none would; "
            "give them a value and run ferro migrate up again, or regenerate "
            "the migration without --no-backfill to get a backfill"
        )


@nothing_to_reverse("a guard writes nothing")
def down(ctx): ...
```

On a database that holds Ada Lovelace's row, the guard refuses before the contract runs, and nothing after it is applied.

Pick the backfill when rows need values, the guard when you can say none do. Never delete a backfill file to skip it: the step number would be a gap, and the contract would fail on the rows. Delete the unapplied migration and regenerate it with `--no-backfill` instead.

A project can replace either skeleton with `_templates/backfill.py` or `_templates/guard.py` in its migrations directory, used verbatim with `{model}`, `{columns}` and `{query}` filled in (and `_templates/data_step.py` for `new --data-step`).

## Data step declarations {#a-data-step}

Every data step file defines `up` and `down`, and each carries exactly one declaration:

| Declaration | On | Meaning |
| :--- | :--- | :--- |
| `@atomic` | `up` or `down` | `async def up(ctx)`: the whole step runs in one transaction, its step record committed inside it. |
| `@chunked(query, batch_size=…)` | `up` or `down` | `async def up(ctx, batch)`: the runner pages `query` and calls the function once per batch, one transaction per batch. |
| `@nothing_to_reverse("<why>")` | `down` | Reverting removes the step's record and runs nothing. |
| `@irreversible("<why>")` | `down` | A run that would revert this step refuses before reverting anything. |

An undeclared or doubly declared function is refused when the file loads, naming the file and the function. `todo("…")` marks what only a person can supply; it is read from the file before anything runs, so a migration holding one never starts. Each declaration has its entry in the [API reference](../../api/migrations.md#data-step-declarations).

### `@atomic` and `@chunked` {#atomic-or-chunked}

Pick `@atomic` for work small enough to hold in one transaction: the step lands whole or not at all, as the guard above does. Pick `@chunked` for a big table, as the generated backfill does: each batch commits with its **cursor** (the last row's order-key values), so an interrupted step resumes at its last committed batch and never replays a row.

The `@chunked` query is a function of `ctx.models`. It must be ordered by columns of the model itself, including its primary key, select only the rows that still need the step, and leave `limit`, `offset`, `after` and `before` to the runner; anything else is refused before the run starts, naming the fix. A many-to-many join table has no single primary key to page by: page its parent instead, and change each parent's join rows inside the batch.

### Historical models

A data step never sees the models in your codebase today, which will have moved on by the time someone runs an old migration. `ctx.models` holds a **historical model** for every table, built from the migration's schema snapshots:

- `ctx.models.Author` reaches a model by its class name; `ctx.models.table("author_tags")` reaches any table by name, a many-to-many join table included.
- Each class is the table as it stands between the migration's expand and contract steps: the union of the previous snapshot's columns and this migration's. A column only this migration adds is nullable there (`slug` is `str | None` in the backfill above), and a renamed column appears once, under its new name.
- It carries columns only: no relations, methods or validators. A foreign key is its plain `*_id` column (`book.author_id`).
- A json-family column is typed `dict[str, Any] | list[Any]` with its `db_type`, and a column of a type Ferro cannot name is `Any`. Your own models may not declare those shapes; historical models carry them because they describe what the database holds, not what a model should declare.
- Querying a model from your codebase inside a step raises, naming the historical model to use instead.

### The step context

`ctx` is all a data step can reach:

| Member | What it is |
| :--- | :--- |
| `ctx.models` | The historical models. |
| `ctx.execute(sql, *args)` | A raw statement on the step's own transaction; returns the rows affected. |
| `ctx.fetch_all(sql, *args)` / `ctx.fetch_one(sql, *args)` | A raw query on the step's transaction. |
| `ctx.dialect` | `"postgres"` or `"sqlite"`: the database this run is on. |
| `ctx.log` | A logger named for the step (`ferro.migrations.0002.02_backfill_author`). |

It has no way to open, commit or leave the step's transaction, so a step cannot commit half of its work behind the runner's back. A nested `ferro.transaction()` inside a step is a savepoint, as in application code.

## Late rows and contract recovery {#when-a-late-row-arrives}

A backfill fills the rows that exist when it runs. On a live database, a writer that does not know the column yet (code from before this release) can insert a `NULL` after the backfill and before the contract. The contract then fails loudly and says how to recover. With one such row, `ferro migrate up` applies the first three steps and stops at the contract:

<!-- output: late-row-up -->
```text
default · sqlite:///./app.db
0002_author_slug  01_expand           applied (0 ms)
0002_author_slug  02_backfill_author  applied (0 ms)
0002_author_slug  03_add_constraint   applied (0 ms)
ferro migrate: 0002_author_slug/04_contract.up.sqlite.sql failed: 1 row still has NULL "slug" in "author"; run ferro migrate down --to 0002:01 then ferro migrate up to re-run the backfill
The step was rolled back; fix the file or the database and run `ferro migrate up` again to resume at it. Nothing after it ran.
```

`ferro migrate down --to 0002:01 --yes` leaves the expand step applied and reverts the rest:

<!-- output: late-row-down -->
```text
default · sqlite:///./app.db
down reverts, in this order:
  0002_author_slug  04_contract  (nothing to reverse: its up never finished, and a transactional step that does not finish leaves nothing behind)
  0002_author_slug  03_add_constraint
  0002_author_slug  02_backfill_author  (nothing to reverse: 01_expand.down.sql drops the column)
0002_author_slug  04_contract         nothing to reverse: its up never finished, and a transactional step that does not finish leaves nothing behind
0002_author_slug  03_add_constraint   reverted (0 ms)
0002_author_slug  02_backfill_author  nothing to reverse: 01_expand.down.sql drops the column
```

The `slug` column and the values the backfill wrote stay. Then `ferro migrate up` runs the backfill again, which selects only the rows still missing a value, and the contract:

<!-- output: late-row-resume -->
```text
default · sqlite:///./app.db
0002_author_slug  02_backfill_author  applied (0 ms)
0002_author_slug  03_add_constraint   applied (0 ms)
0002_author_slug  04_contract         applied (0 ms)
```

On Postgres the add-constraint step already refuses new `NULL`s, so the window is between the backfill and that step. To close it entirely, ship the change as two releases: see [the two-release contract](deploying.md#rolling-deploys-and-the-two-release-contract).

## Removing an enum label

Removing a member from a `StrEnum` whose rows may still hold it takes the same shape. Say `Order.status` is a `Status` with the labels `paid` and `canceled`, and you delete `CANCELED = "canceled"`. `ferro migrate new drop_canceled` writes a backfill that pages the rows holding the removed label and asks which label each gets instead:

```python {test=compile reason="a generated step file; the docs tests regenerate it and compare"}
# migrations/0002_drop_canceled/01_backfill_order.py
# Gives every existing order a value for status.
# Write each value marked todo, then run `ferro migrate up`.
# If no row needs a value, delete this unapplied migration and run
# `ferro migrate new <name> --no-backfill order.status` for a guard step instead.
from ferro.migrations import chunked, nothing_to_reverse, todo


@chunked(
    lambda models: models.Order.where(lambda order: order.status == "canceled")
    .order_by(lambda order: order.id),
    batch_size=1000,
)
async def up(ctx, batch):
    for order in batch:
        order.status = type(order.status)(todo("the label to use instead of 'canceled'"))
        await order.save()


@nothing_to_reverse("the contract's down restores the label")
def down(ctx): ...
```

Postgres cannot drop an enum label in place, so the contract creates the type anew without it, moves the column over, drops the old type and renames the new one into its place:

```sql
-- 0002_drop_canceled/02_contract.up.postgres.sql
-- ferro: data-dependent

CREATE TYPE "status_new" AS ENUM ('paid');

ALTER TABLE "order" ALTER COLUMN "status" DROP DEFAULT;

ALTER TABLE "order" ALTER COLUMN "status" TYPE "status_new" USING "status"::text::"status_new";

DROP TYPE "status";

ALTER TYPE "status_new" RENAME TO "status";
```

A row still holding `canceled` fails the cast, so the contract cannot run before the backfill has moved every such row. Its down adds the label back with `ALTER TYPE "status" ADD VALUE IF NOT EXISTS 'canceled' AFTER 'paid'`. On SQLite, where labels are text in the rows, the contract is a [table rebuild](migrations.md#sqlite-table-rebuilds) that narrows the column to the remaining labels.

## See also

- [Migrations](migrations.md): the loop, headers and renames
- [Deploying migrations](deploying.md): rolling deploys and the two-release contract
- [Testing migrations](testing.md): seed rows through historical models and prove a backfill
- [Migrations API reference](../../api/migrations.md#data-step-declarations): `atomic`, `chunked`, `irreversible`, `nothing_to_reverse`, `todo`
