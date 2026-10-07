# Migrations

A migration is a numbered directory of steps that changes a database from one version of your models to the next. `ferro migrate new` writes it from the difference between your models and the previous migration; you read it, commit it, and `ferro migrate up` applies it. This is rung 3 of [the ladder](overview.md#the-ladder), and the door Ferro recommends for any database whose data you would mind losing.

## Install and set up

```bash
pip install "ferro-orm[cli]"
```

`ferro migrate init` writes the [project configuration](../../reference/configuration.md) and creates the migrations directory. It asks for what its flags do not answer:

```text
$ ferro migrate init
Config file [pyproject.toml]:
Models module (dotted) [blog.models]:
Target dialects [postgres]: postgres,sqlite
Another database (separate tables, own migration history)? [y/N]:
[tool.ferro]
models = ["blog.models"]
dialects = ["postgres", "sqlite"]
Wrote [tool.ferro] to pyproject.toml and created migrations/
```

- **Models module**: the dotted module(s) defining your models. Ferro imports them to read the models; a model belongs to a database by the module that defines it.
- **Target dialects**: every dialect this project's databases run on. Each generated step gets one rendering per dialect, so a project that uses SQLite locally and Postgres in production lists both.
- **Another database**: answer `y` only for a second set of tables with its own migration history (a separate service's database, say). Several servers running the same schema, such as SQLite locally and Postgres in production, are still one database.
- The database URL is never in the file: commands read `$DATABASE_URL` (or the variable `url_env` names), or take `--url`.

`init` refuses a directory that already holds an Alembic environment (`env.py`, `versions/` or an `alembic.ini` beside it), so the two never share `migrations/`. Every prompt has a flag (`--config-file`, `--models`, `--dialects`, `--directory`, `--database`); see the [CLI reference](../../reference/cli.md#ferro-migrate-init).

## The loop

Edit a model, generate, read, apply, commit. Starting from an `Author` with `id`, `name` and `email`, add a `nickname`:

=== "Assignment"

    ```python
    --8<-- "docs/examples/migrations_quickstart.py:models"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/migrations_quickstart_annotated.py:models"
    ```

```text
$ ferro migrate check
ungenerated: the models changed since 0001_create_author and no migration records it (changed models: Author); run `ferro migrate new <name>` and commit the migration it writes

$ ferro migrate new author_nickname
migrations/0002_author_nickname/
  01_schema.up.postgres.sql
  01_schema.down.postgres.sql
  01_schema.up.sqlite.sql
  01_schema.down.sqlite.sql
  02_idx_author_nickname.up.postgres.sql
  02_idx_author_nickname.down.postgres.sql
  02_idx_author_nickname.up.sqlite.sql
  02_idx_author_nickname.down.sqlite.sql
  ir.json
changed models: Author

$ ferro migrate up
0002_author_nickname  01_schema               applied (1 ms)
0002_author_nickname  02_idx_author_nickname  applied (0 ms)
```

`new` diffs the models against the newest migration's [schema snapshot](#reading-a-migration), never against a database, so it needs no connection and gives every developer the same files. A change that renders no DDL (a Python-side default, a back-reference) writes nothing and says `no schema change: nothing written`.

The whole loop runs in [`docs/examples/migrations_quickstart.py`](https://github.com/syn54x/ferro-orm/blob/main/docs/examples/migrations_quickstart.py): `init`, `new`, `up`, `check`, `drift`, `down`, on SQLite.

## Reading a migration

```text
migrations/
  .gitattributes                         written once: the directory is byte-exact (no line-ending conversion)
  0001_create_author/
    01_schema.up.postgres.sql            a DDL step, one rendering per target dialect
    01_schema.down.postgres.sql          ...and its down
    01_schema.up.sqlite.sql
    01_schema.down.sqlite.sql
    ir.json                              the schema snapshot: the models as they were
  0002_author_nickname/
    ...
```

- **Migrations are numbered** `0001`, `0002`, …; numbers count migrations, never steps. A migration is applied whole and recorded step by step, so a failure resumes at the step that failed.
- **Steps are numbered within a migration** (`01_`, `02_`, …) and share their number across dialects. A DDL step has an `.up` and a `.down` file per target dialect. Where a dialect has nothing to do, its rendering says so with `-- ferro: not-applicable`.
- **A data step** is a Python file, `NN_<name>.py`, that serves every dialect; see [Data steps and backfills](data-steps.md).
- **`ir.json`** is the schema snapshot: the declared models when the migration was generated, linked by checksum to the previous one. The next `new` diffs against it, and data steps build their [historical models](data-steps.md#historical-models) from it. Never edit it: later migrations and every database that applied this one depend on it.

The SQL is exactly what the auto-migrate passes run for the same change; Postgres and SQLite each get their own spelling:

```sql
-- 0001_create_author/01_schema.up.postgres.sql
CREATE TABLE IF NOT EXISTS "author" ( "id" serial PRIMARY KEY NOT NULL, "name" varchar NOT NULL );

-- 0001_create_author/01_schema.up.sqlite.sql
CREATE TABLE IF NOT EXISTS "author" ( "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "name" varchar NOT NULL );

-- 0001_create_author/01_schema.down.postgres.sql
DROP TABLE "author";
```

### Headers

A step file may open with `-- ferro: …` lines. The generator writes them; you read them in review.

| Header | Meaning |
| :--- | :--- |
| `-- ferro: destructive` | The step discards data: it drops a column, a table or an enum type. Review is the gate; nothing refuses it. |
| `-- ferro: data-dependent` | The step discards nothing but fails on rows that do not satisfy it: a `NOT NULL`, a unique over existing rows, a type cast, the validation of a staged constraint, a removed enum label. |
| `-- ferro: no-transaction` | The runner opens no transaction around the step, so a statement such as `CREATE INDEX CONCURRENTLY` can run. The step is written to be safe to re-run from its first statement. |
| `-- ferro: foreign-keys-off` | A SQLite [table rebuild](#sqlite-table-rebuilds): the runner turns foreign-key enforcement off while the step runs. |
| `-- ferro: not-applicable` | This dialect has nothing to do at this step number. |
| `-- ferro: nothing-to-reverse <reason>` | In a `.down` file: reverting removes the step's record and runs nothing. |
| `-- ferro: irreversible <reason>` | In a `.down` file: a run that would revert this step refuses before reverting anything. Only a person declares one. |

A new column with an index on a table that already exists shows the shape most changes take on Postgres: the column in one step, the index in its own [index step](#index-steps) that builds without blocking writers:

```sql
-- 0002_author_nickname/02_idx_author_nickname.up.postgres.sql
-- ferro: no-transaction

DROP INDEX CONCURRENTLY IF EXISTS "idx_author_nickname";

CREATE INDEX CONCURRENTLY "idx_author_nickname" ON "author" ("nickname");
```

The first statement removes whatever an earlier failed build left under that name, so the step is exact from the top. On SQLite the same step is a plain `CREATE INDEX IF NOT EXISTS` inside a transaction.

### Index steps

On a table that already exists, every index, and every unique, gets a step of its own after the migration's data steps. A unique's step is the migration's data-dependent step: a duplicate fails it, and nothing is generated to resolve one. An index on a table the same migration creates needs no step of its own.

### Staged constraints and `NOT NULL` on Postgres

On a Postgres table that already exists, a generated migration never scans the table under a lock that blocks writers. A foreign key or a check is added `NOT VALID` first (refusing every new write that violates it) and validated in a later step; a column becoming `NOT NULL` gets a temporary `CHECK ("slug" IS NOT NULL) NOT VALID` first, and the contract step validates it, sets `NOT NULL` and drops it. [Data steps and backfills](data-steps.md#what-the-generator-writes) shows a whole one. A table the same migration creates needs none of this, and SQLite has no equivalent: there, the same changes are table rebuilds.

## Renames

Without a hint, a renamed column is a dropped column and an added one, and `new` writes exactly that, marked destructive, and says what it saw:

```text
$ ferro migrate new rename_writer
...
author: if "nickname" became "handle", declare renamed_from="nickname"
```

With a hint, it writes a rename and every row keeps its value. A hint says what a column, a table or an enum label was called before:

=== "Assignment"

    ```python
    --8<-- "docs/examples/migrations_rename.py:models"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/migrations_rename_annotated.py:models"
    ```

(The `# before:` and `# new` comments show the previous version of the models; they are not part of the declaration.)

```sql
-- 0002_rename_writer/01_schema.up.postgres.sql
ALTER TABLE "author" RENAME TO "writer";

ALTER TABLE "writer" RENAME COLUMN "nickname" TO "handle";

ALTER INDEX "idx_author_nickname" RENAME TO "idx_writer_handle";

ALTER TYPE "status" RENAME VALUE 'canceled' TO 'cancelled';
```

- `Field(renamed_from="nickname")` renames a column; on a relation, `ForeignKey(..., renamed_from="writer")` renames its `*_id` column.
- `__ferro_renamed_from__ = "author"` on a model renames its table, and every index and constraint named after it.
- `__ferro_renamed_labels__ = {"cancelled": "canceled"}` on a `StrEnum` maps each new label to its old one. On SQLite, where labels are text in the rows, the rows are rewritten in a table rebuild.
- An enum *type*'s rename needs no hint: it is read off the columns that moved to it.

A hint is live only while the previous snapshot still holds the old name and lacks the new one. Once its migration is generated it is inert and may be deleted. A hint whose old name is still declared, or two hints naming one old name, is refused. Auto-migrate honours the column and label hints but not a table's: its create pass builds the new table name as a new, empty table ([what each door covers](overview.md#what-each-door-covers)).

The rename example runs end to end, including the revert, in [`docs/examples/migrations_rename.py`](https://github.com/syn54x/ferro-orm/blob/main/docs/examples/migrations_rename.py).

## Changes existing rows need a value for

Adding a required column to a table with rows, making a column required, or removing an enum label that rows still hold cannot be done by DDL alone: someone has to say what the existing rows get. `new` splits such a migration into an **expand step** (the new column, nullable), a **backfill** per model (a Python data step you write), and a **contract step** (the `NOT NULL`, the label removal), and `up` refuses the migration until the backfill is written. When you know no row needs a value, `--no-backfill <table>.<column>` writes a **guard step** instead, which fails the migration if one does. Both are on [Data steps and backfills](data-steps.md).

## Hand-written steps

`new` can add a step for what the generator does not write:

```text
$ ferro migrate new seed --sql-step seed_admins
migrations/0004_seed/
  01_seed_admins.up.sql
  01_seed_admins.down.sql
  ir.json

$ ferro migrate new fix_slugs --data-only --data-step Author
migrations/0003_fix_slugs/
  01_backfill_author.py
  ir.json
```

- `--sql-step <name>` adds `NN_<name>.up.sql` / `.down.sql` without a dialect suffix: one file serves every dialect, so write portable SQL. The files start as `-- write this step`.
- `--data-step <Model>` adds a Python data step over that model (`NN_backfill_<model>.py`), with `todo("write this step")` in its `up` and `down`.
- `--data-only` writes only the data step: no DDL, and a copy of the previous snapshot.

`--data-step` also works beside a generated change (`ferro migrate new add_writer --data-step Writer`): the data step is placed after the generated schema steps. A step that still says `todo(...)` is an *unwritten step*: `check` reports it and `up` refuses the migration before running anything.

## SQLite table rebuilds

SQLite cannot add a constraint, change a type or nullability, or drop a constraint on a table that exists. A migration does it the only way SQLite allows: it creates the table again in its new shape under a temporary name, copies the rows, drops the old table and renames the new one into place, inside the step whose change needs it:

```sql
-- 0002_author_slug/04_contract.up.sqlite.sql
-- ferro: foreign-keys-off
-- ferro: data-dependent

CREATE TABLE IF NOT EXISTS "_ferro_new_author" ( "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "name" varchar NOT NULL, "slug" varchar NOT NULL );

INSERT INTO "_ferro_new_author" ("id", "name", "slug") SELECT "id", "name", "slug" FROM "author";

DROP TABLE "author";

ALTER TABLE "_ferro_new_author" RENAME TO "author";
```

The copy carries only what the schema snapshot declares, so before a rebuild runs, the runner refuses a live table that holds anything else, naming each object and its fix:

```text
ferro migrate: a SQLite rebuild of table "post" copies only the columns and recreates only the indexes the schema snapshot declares, and the live table also holds:
  - index "my_idx": drop it, or declare it on the model in a migration (ferro builds the indexes it declares, named idx_/uq_)
  - trigger "t": drop it; a rebuild's DROP TABLE would drop it, and ferro cannot carry it across
Nothing was applied.
```

An undeclared column is refused the same way (`declare it on the model in a migration, or drop it`).

## Checking

| Command | Needs a database | What it answers |
| :--- | :--- | :--- |
| `ferro migrate check` | no | Does every model change have a migration, and is the directory intact? Exit 0, or 3 naming each problem: an ungenerated change, a broken snapshot chain, a duplicate or missing number, a step missing a target dialect's rendering, an unwritten step. |
| `ferro migrate status` | yes | Which migrations this database has applied. Exit 0 up to date, 3 pending, 4 needs attention. |
| `ferro migrate drift` | yes | Does the live schema match the snapshot of the last migration applied? Exit 0, or 4 with one line per difference. |

```text
$ ferro migrate drift
drift against 0002_rename_writer:
  idx_writer_handle index is missing
```

Drift is reported, never repaired: write the fix as a migration, or put the object back. Tables the migrations never declared are not drift. `check` belongs in CI and in a [pre-commit hook](deploying.md#a-pre-commit-hook).

## Going back

```text
$ ferro migrate down
down reverts, in this order:
  0002_rename_writer  01_schema
Revert 0002_rename_writer (1 step)? [y/N] y
0002_rename_writer  01_schema  reverted (1 ms)
```

- With no flag, `down` reverts the latest applied migration; `--to 0005` leaves `0005` fully applied, `--to 0007:02` leaves steps `01`–`02` of `0007` applied, `--to 0000` (or `--all`) reverts everything.
- It prints the plan and asks first. `--yes` skips the question; without a terminal and without `--yes` it prints the plan and refuses.
- A down restores the schema of the previous snapshot, never the rows a drop removed. A step declared irreversible stops the whole run before anything is reverted.
- A migration that `baseline` recorded created nothing on that database, so `down` refuses to go below it ([Adopting migrations](../../howto/adopting-migrations.md#undoing-a-baseline)).

## Editing a migration

A migration no database has applied is yours to change: edit it, or delete its directory and run `new` again. Once a database has applied a step, `up` checks the step's file against the checksum it recorded, and an edited file is refused; see [Edited files and `rerecord`](deploying.md#edited-files-and-rerecord).

## Changing a primary key

No door changes a primary key in place: the key moved to another column, the key column's type changed, or a key made composite. `new` refuses it before writing anything, on every dialect:

```text
$ ferro migrate new rekey
changing the primary key of "author" is not generated: write it as a new table (ferro migrate new --data-step …), a backfill of parent and children, and a drop; see the Migrations docs § Changing a primary key
```

The recipe, for an `author` table keyed by an integer `id` that should be keyed by a `UUID`, with `book.author_id` pointing at it:

1. **Add the new table beside the old one.** Declare a new model (`Writer`) with the new key, and give each child a nullable relation to it (`Book.writer`). Generate the migration with a data step over the new model, and write the step so it copies each parent row and points each child at its copy:

    ```text
    $ ferro migrate new add_writer --data-step Writer
    migrations/0002_add_writer/
      01_schema.up.postgres.sql
      ...
      02_validate.up.postgres.sql
      ...
      03_backfill_writer.py
      ir.json
    ```

    ```python
    # migrations/0002_add_writer/03_backfill_writer.py
    from uuid import uuid4

    from ferro.migrations import atomic, nothing_to_reverse


    @atomic
    async def up(ctx):
        for author in await ctx.models.Author.all():
            writer = ctx.models.Writer(id=uuid4(), name=author.name)
            await writer.save()
            for book in await ctx.models.Book.where(lambda book: book.author_id == author.id).all():
                book.writer_id = writer.id
                await book.save()


    @nothing_to_reverse("the schema step's down drops writer and book.writer_id")
    def down(ctx): ...
    ```

    On a large table, write it as a [`@chunked`](data-steps.md#atomic-or-chunked) step instead.

2. **Drop the old table.** Remove `Author` and `Book.author`, and make `Book.writer` required. Every book already has a writer, so generate the change with a guard rather than a backfill:

    ```text
    $ ferro migrate new drop_author --no-backfill book.writer_id
    ```

    The drop is marked `-- ferro: destructive`. Its down restores the schema, not the rows: if reverting past it must be impossible, declare its down `-- ferro: irreversible <reason>`.

3. **Optionally take the old name back.** Rename `Writer` to `Author` with `__ferro_renamed_from__ = "writer"` and the relation with `ForeignKey(..., renamed_from="writer")`, and generate the rename.

On a server that deploys with rolling restarts, ship each step as its own release, as the [two-release contract](deploying.md#rolling-deploys-and-the-two-release-contract) describes: code running during step 1 must write both keys.

A native enum column moved to another enum type, while other columns stay on the old type, is refused the same way, with its own recipe: add a column of the new type, copy the values across in a data step (`ferro migrate new --data-step …`), then drop the old column.

## See Also

- [Data steps and backfills](data-steps.md) — what to write where `new` says `todo(...)`
- [Deploying migrations](deploying.md) — `up` in a deploy, `require_applied()`, rolling deploys, edited files
- [Testing migrations](testing.md) — prove a migration in your test suite
- [CLI reference](../../reference/cli.md) — every verb, flag and exit code
