# Data Steps and Backfills

Some changes need more than DDL. Add a required `slug` to a table that already has authors, and every existing row needs a slug before the column can be `NOT NULL`. Only you know what that value is, so the migration holds a Python **data step** where you write it:

=== "Assignment"

    ```python
    --8<-- "docs/examples/migrations_backfill.py:models"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/migrations_backfill_annotated.py:models"
    ```

```text
$ ferro migrate new author_slug
migrations/0002_author_slug/
  01_expand.up.postgres.sql
  01_expand.down.postgres.sql
  01_expand.up.sqlite.sql
  01_expand.down.sqlite.sql
  02_backfill_author.py
  03_add_constraint.up.postgres.sql
  03_add_constraint.down.postgres.sql
  03_add_constraint.up.sqlite.sql
  03_add_constraint.down.sqlite.sql
  04_contract.up.postgres.sql
  04_contract.down.postgres.sql
  04_contract.up.sqlite.sql
  04_contract.down.sqlite.sql
  ir.json
changed models: Author
02_backfill_author.py needs writing where it says todo(...); if no author needs a value, delete 0002_author_slug/ and run: ferro migrate new author_slug --no-backfill author.slug
```

This whole page runs in [`docs/examples/migrations_backfill.py`](https://github.com/syn54x/ferro-orm/blob/main/docs/examples/migrations_backfill.py): the guard refusing, the backfill written, the recovery from a late row.

## What the generator writes

The migration is split around the rows:

| Step | What it holds |
| :--- | :--- |
| `01_expand` | Only what existing rows already satisfy: `ALTER TABLE "author" ADD COLUMN "slug" varchar;`, nullable. |
| `02_backfill_author.py` | The data step that gives each existing row its value. You write it. |
| `03_add_constraint` | Postgres: `ALTER TABLE "author" ADD CONSTRAINT "_ferro_notnull_author_slug" CHECK ("slug" IS NOT NULL) NOT VALID;`, which refuses every new `NULL` from this point without scanning the table. SQLite: `-- ferro: not-applicable`. |
| `04_contract` | What the rows had to be prepared for, marked `-- ferro: data-dependent`. |

The contract on each dialect:

```sql
-- 04_contract.up.postgres.sql
-- ferro: data-dependent

ALTER TABLE "author" VALIDATE CONSTRAINT "_ferro_notnull_author_slug";

ALTER TABLE "author" ALTER COLUMN "slug" SET NOT NULL;

ALTER TABLE "author" DROP CONSTRAINT "_ferro_notnull_author_slug";
```

```sql
-- 04_contract.up.sqlite.sql
-- ferro: foreign-keys-off
-- ferro: data-dependent

CREATE TABLE IF NOT EXISTS "_ferro_new_author" ( "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "name" varchar NOT NULL, "slug" varchar NOT NULL );

INSERT INTO "_ferro_new_author" ("id", "name", "slug") SELECT "id", "name", "slug" FROM "author";

DROP TABLE "author";

ALTER TABLE "_ferro_new_author" RENAME TO "author";
```

The same split happens for a column that stops accepting `NULL` and for an [enum label rows still carry](#removing-an-enum-label). A migration with no data step has no expand or contract: its DDL is one schema step.

## Writing the backfill

The generated file:

```python
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

Until you replace the `todo(...)`, `up` refuses the migration before running any of it, naming the file and line:

```text
$ ferro migrate up
0002_author_slug/02_backfill_author.py:15: not written yet: the slug for an existing author
Write each step where it says todo(...), then run `ferro migrate up` again. Nothing was applied.
```

Write the value in place of the `todo(...)`, then run `up` again:

```python
author.slug = author.name.lower().replace(" ", "-")
```

```text
$ ferro migrate up
0002_author_slug  01_expand           applied (0 ms)
0002_author_slug  02_backfill_author  applied (1 ms)
0002_author_slug  03_add_constraint   applied (0 ms)
0002_author_slug  04_contract         applied (0 ms)
```

The query selects only the rows that still need a value, so running the step again touches only what is left. A `default_factory` from the standard library (`uuid.uuid4`) is pre-filled as its call instead of a `todo`.

## Backfill or guard

When you know no existing row needs a value (the table is new in production, or every writer already sets the column), skip the backfill: `--no-backfill <table>.<column>` writes a **guard step** in its place. A guard is generated and complete; it changes nothing and fails the migration if a row turns out to need a value.

```text
ferro migrate new author_slug                  ferro migrate new author_slug --no-backfill author.slug

0002_author_slug/                              0002_author_slug/
  01_expand.up.<dialect>.sql                     01_expand.up.<dialect>.sql
  02_backfill_author.py      ← you write it      02_guard_author.py        ← generated, complete
  03_add_constraint.up.<dialect>.sql             03_add_constraint.up.<dialect>.sql
  04_contract.up.<dialect>.sql                   04_contract.up.<dialect>.sql
  ir.json                                        ir.json
```

```python
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

Pick the backfill when rows need values, the guard when you can say none do. Never delete a backfill file to skip it: the step number would be a gap, and the contract would fail on the rows. Delete the unapplied migration and regenerate it with `--no-backfill` instead (step numbers stay dense either way, ADR-0037).

A project can replace either skeleton with `_templates/backfill.py` or `_templates/guard.py` in its migrations directory, used verbatim with `{model}`, `{columns}` and `{query}` filled in (and `_templates/data_step.py` for `new --data-step`).

## A data step

Every data step file defines `up` and `down`, and each carries exactly one declaration:

| Declaration | On | Meaning |
| :--- | :--- | :--- |
| `@atomic` | `up` or `down` | `async def up(ctx)`: the whole step runs in one transaction, its step record committed inside it. |
| `@chunked(query, batch_size=…)` | `up` or `down` | `async def up(ctx, batch)`: the runner pages `query` and calls the function once per batch, one transaction per batch. |
| `@nothing_to_reverse("<why>")` | `down` | Reverting removes the step's record and runs nothing. |
| `@irreversible("<why>")` | `down` | A run that would revert this step refuses before reverting anything. |

An undeclared or doubly declared function is refused when the file loads, naming the file and the function. `todo("…")` marks what only a person can supply; it is read from the file before anything runs, so a migration holding one never starts.

### Atomic or chunked

Pick `@atomic` for work small enough to hold in one transaction: the step lands whole or not at all. Pick `@chunked` for a big table: each batch commits with its **cursor** (the last row's order-key values), so an interrupted step resumes at its last committed batch and never replays a row.

```python
from ferro.migrations import chunked, nothing_to_reverse


@chunked(
    lambda models: models.Author.where(lambda author: author.slug == None)
    .order_by(lambda author: author.id),
    batch_size=1000,
)
async def up(ctx, batch):
    for author in batch:
        author.slug = author.name.lower().replace(" ", "-")
        await author.save()


@nothing_to_reverse("01_expand.down.sql drops the column")
def down(ctx): ...
```

The query is a function of `ctx.models`. It must be ordered by columns of the model itself including its primary key, select only the rows that still need the step, and leave `limit`, `offset`, `after` and `before` to the runner; anything else is refused before the run starts, naming the fix. A many-to-many join table has no single primary key to page by: page its parent instead and change each parent's join rows inside the batch.

### Historical models

A data step never sees the models in your codebase today, which will have moved on by the time someone runs an old migration. `ctx.models` holds a **historical model** for every table, built from the migration's schema snapshots:

- `ctx.models.Author` reaches a model by its class name; `ctx.models.table("author_tags")` reaches any table by name, a many-to-many join table included.
- Each class is the table as it stands between the migration's expand and contract steps: the union of the previous snapshot's columns and this migration's. A column only this migration adds is nullable there, and a renamed column appears once, under its new name.
- It carries columns only: no relations, methods or validators. A foreign key is its plain `*_id` column (`book.author_id`).
- A json-family column is typed `dict[str, Any] | list[Any]` with its `db_type`, and a column of a type Ferro cannot name is `Any`. Your own models may not declare those shapes (ADR-0004); historical models carry them because they describe what the database holds, not what a model should declare.
- Querying a model from your codebase inside a step raises, naming the historical model to use instead.

### The step context

`ctx` is all a data step can reach (ADR-0035):

| Member | What it is |
| :--- | :--- |
| `ctx.models` | The historical models. |
| `ctx.execute(sql, *args)` | A raw statement on the step's own transaction; returns the rows affected. |
| `ctx.fetch_all(sql, *args)` / `ctx.fetch_one(sql, *args)` | A raw query on the step's transaction. |
| `ctx.dialect` | `"postgres"` or `"sqlite"`: the database this run is on. |
| `ctx.log` | A logger named for the step (`ferro.migrations.0002.02_backfill_author`). |

It has no way to open, commit or leave the step's transaction. A nested `ferro.transaction()` inside a step is a savepoint, as in application code.

## When a late row arrives

A backfill fills the rows that exist when it runs. On a live database, a writer that does not know the column yet (code from before this release) can insert a `NULL` after the backfill and before the contract. The contract then fails loudly and says how to recover:

```text
$ ferro migrate up
0002_author_slug  01_expand           applied (0 ms)
0002_author_slug  02_backfill_author  applied (1 ms)
0002_author_slug  03_add_constraint   applied (0 ms)
ferro migrate: 0002_author_slug/04_contract.up.sqlite.sql failed: 1 row still has NULL "slug" in "author"; run ferro migrate down --to 0002:01 then ferro migrate up to re-run the backfill
The step was rolled back; fix the file or the database and run `ferro migrate up` again to resume at it. Nothing after it ran.
```

`down --to 0002:01` leaves the expand step applied and reverts the rest; `up` re-runs the backfill (which selects only the rows still missing a value) and the contract:

```text
$ ferro migrate down --to 0002:01 --yes
down reverts, in this order:
  0002_author_slug  04_contract  (nothing to reverse: its up never finished, and a transactional step that does not finish leaves nothing behind)
  0002_author_slug  03_add_constraint
  0002_author_slug  02_backfill_author  (nothing to reverse: 01_expand.down.sql drops the column)
...
$ ferro migrate up
```

On Postgres the add-constraint step already refuses new `NULL`s, so the window is between the backfill and that step. To close it entirely, ship the change as two releases: see [the two-release contract](deploying.md#rolling-deploys-and-the-two-release-contract).

## Removing an enum label

Removing a member from a `StrEnum` whose rows may still hold it is the same shape. The backfill pages the rows holding the removed label and asks which label each gets instead:

```python
@chunked(
    lambda models: models.Order.where(lambda order: order.status == "canceled")
    .order_by(lambda order: order.id),
    batch_size=1000,
)
async def up(ctx, batch):
    for order in batch:
        order.status = type(order.status)(todo("the label to use instead of 'canceled'"))
        await order.save()
```

The contract then removes the label. Postgres cannot drop an enum label in place, so the contract creates the type anew without it, moves the column over (`ALTER COLUMN … TYPE "orderstatus_new" USING "status"::text::"orderstatus_new"`), drops the old type and renames the new one into its place; its down adds the label back. On SQLite, where labels are text in the rows, the contract is `-- ferro: not-applicable`.

## See Also

- [Migrations](migrations.md) — the loop, headers and renames
- [Deploying migrations](deploying.md) — rolling deploys and the two-release contract
- [Testing migrations](testing.md) — seed rows through historical models and prove a backfill
