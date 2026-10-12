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

A step file may open with `-- ferro: …` lines. The generator writes them, except `expand` and `irreversible`, which only a person writes; you read them in review.

| Header | Meaning |
| :--- | :--- |
| `-- ferro: destructive` | The step discards data: it drops a column, a table or an enum type. Review is the gate; nothing refuses it. |
| `-- ferro: data-dependent` | The step discards nothing but fails on rows that do not satisfy it: a `NOT NULL`, a unique over existing rows, a type cast, the validation of a staged constraint, a removed enum label. |
| `-- ferro: no-transaction` | The runner opens no transaction around the step, so a statement such as `CREATE INDEX CONCURRENTLY` can run. The step is written to be safe to re-run from its first statement. |
| `-- ferro: foreign-keys-off` | A SQLite [table rebuild](#sqlite-table-rebuilds): the runner turns foreign-key enforcement off while the step runs. |
| `-- ferro: not-applicable` | This dialect has nothing to do at this step number. |
| `-- ferro: expand` | In a [hand-written SQL step](#hand-written-steps): its author declares it *expand*, so the previous release's writes keep succeeding beside it ([step class](deploying.md#rolling-deploys-and-the-two-release-contract)). A hand-written step without it is *contract*. The generator never writes it: a generated step's class comes from what the step does. A ferro older than this header refuses the file, as it refuses any header it does not know. |
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

A rename hint renames in one migration, so the previous release's code, still naming `nickname`, fails the moment it runs. When old and new code serve side by side (a rolling deploy), rename online instead, over three releases: add the new column, write both in app code and copy the existing rows across in a [data step](data-steps.md#a-data-step); [retire](#retiring-a-column) the old one; delete its declaration, which drops it.

## Retiring a column

Dropping a column in the release that stops using it breaks the release still serving: its code names the column in every `INSERT` and `UPDATE`, so it fails from the moment the drop runs until it is replaced. A column leaves in two releases instead. In the first, declare it `retired`:

=== "Assignment"

    ```python
    --8<-- "docs/examples/migrations_retire.py:models"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/migrations_retire_annotated.py:models"
    ```

From this release on `bio` is gone from the model: `author.bio` raises `AttributeError`, `Author(bio="x")` raises `TypeError`, a query, `order_by`, `update()` or `save(only=...)` naming it fails with "retired in this release", and no `SELECT`, `INSERT` or `UPDATE` the model issues names it. The column stays in the schema snapshot, nullable, with its index, so the one change is the relaxation the previous release needs to keep writing beside this one:

```sql
-- 0002_retire_bio/01_schema.up.postgres.sql
ALTER TABLE "author" ALTER COLUMN "bio" DROP NOT NULL;
```

On SQLite that is a [table rebuild](#sqlite-table-rebuilds). Retiring a column that already accepts `NULL` writes nothing: `new` says "no schema change" and `check` is clean. In the next release, delete the declaration: `new` writes the `DROP COLUMN`, marked destructive, and, when it was the last column of a Postgres enum type, the `DROP TYPE` after it.

| Release | The model | The migration | Old code beside it |
|---|---|---|---|
| A | `bio: str = Field(index=True)` | creates `bio` | |
| B | `bio: str \| None = Field(index=True, retired=True)` | `DROP NOT NULL` | A writes `bio`; works |
| C | no `bio` | `DROP COLUMN "bio"` (destructive) | B never names `bio`; works |

- `ForeignKey(retired=True)` retires a relation and its `*_id` column; its foreign key constraint stays until the column is dropped. It needs no `related_name`, and a `BackRef` reading it back is refused: delete it.
- Nothing else about the column changes while it is retired: its index, unique, check and foreign key, and every table check, row policy, composite index or unique that names it, stay as declared.
- A [historical model](data-steps.md#historical-models) sees a retired column as an ordinary nullable column, so a data step can still read it.
- Auto-migrate creates the column on a fresh database and, under `migrate_updates`, relaxes its `NOT NULL`; it never drops it. Alembic autogenerate writes the same `alter_column(..., nullable=True)`.
- Refused at class definition, each naming the fix: retiring a field whose annotation does not allow `None` (declare it `str | None`), a field declared `nullable=False` (a retired column stays nullable), a primary key (drop `retired=True` and change the key first, see [Changing a primary key](#changing-a-primary-key)), a field that also declares `renamed_from` (retire it or rename it, not both), a `BackRef` or a `ManyToMany`.

The example runs all three releases end to end in [`docs/examples/migrations_retire.py`](https://github.com/syn54x/ferro-orm/blob/main/docs/examples/migrations_retire.py).

## Retiring an enum label

Removing a label in the release that stops writing it breaks the release still serving: its code may still write the label, and a removed label is gone from the Postgres type and from any `db_check`. Stop writing the label first, and remove it a release later. In the first release, declare it retired on the `StrEnum`, beside [`__ferro_renamed_labels__`](#renames):

=== "Assignment"

    ```python
    --8<-- "docs/examples/migrations_retire_label.py:models"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/migrations_retire_label_annotated.py:models"
    ```

The member stays, so a row that holds `canceled` still loads as `Status.CANCELED`. Writing the label is refused, at construction and by attribute assignment, with a validation error:

```text
Order(status="canceled")
  Status.CANCELED ('canceled') is retired in this release: the previous release may still write it, this one does not
```

A row loaded with the label can be saved after another field changes: loading does not validate, and `save()` writes every column, so the unchanged label goes back as it was. That is not a write of the label.

Retiring a label is not a schema change. The Postgres type and any `db_check` keep listing it, so `new` says "no schema change", writes nothing, and `check` is clean. In the next release, delete the member and the declaration together: `new` then writes the ordinary [label removal](data-steps.md#what-the-generator-writes), the backfill for rows that still hold it, and on Postgres the type swap.

Each label in `__ferro_retired_labels__` must be a member's value: a set of anything else, or a label that names no member, raises `TypeError` at class definition. The example runs in [`docs/examples/migrations_retire_label.py`](https://github.com/syn54x/ferro-orm/blob/main/docs/examples/migrations_retire_label.py).

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

## Two branches, one number

Your branch adds `bio` to `Author` and generates `0007_add_x`, with a hand-written step `02_fix_rows` you filled in. Meanwhile a teammate merges `0007_add_y`, which adds `nick`. Both were generated on top of `0006`, so after you rebase, the directory holds two `0007`s, and every command refuses it:

```text
$ ferro migrate check
duplicate_number: two migrations are numbered 0007: 0007_add_x and 0007_add_y. Keep the one that is applied anywhere and regenerate the other on top of it:
  ferro migrate new add_x --replace 0007_add_x
  ferro migrate new add_y --replace 0007_add_y
(each line regenerates the migration it names)
```

`0007_add_y` is on main, so keep it and regenerate yours:

```text
$ ferro migrate new add_x --replace 0007_add_x
migrations/0008_add_x/  (replaces 0007_add_x, now deleted)
  01_schema.up.postgres.sql  moved (was 0007_add_x/01_schema.up.postgres.sql)
  01_schema.down.postgres.sql  moved (was 0007_add_x/01_schema.down.postgres.sql)
  02_fix_rows.up.sql  carried, review it (was 0007_add_x/02_fix_rows.up.sql)
  02_fix_rows.down.sql  carried, review it (was 0007_add_x/02_fix_rows.down.sql)
  ir.json  new snapshot, chained from 0007_add_y
changed models: Author
```

`0008_add_x` is what `new` would have written had `0007_add_y` been there first: a new schema snapshot chained from `0007_add_y`'s, and the DDL regenerated from the difference, so it adds `bio` and nothing of `nick`.

- **What is carried.** Every data step (`NN_<name>.py`) and hand-written SQL step (`NN_<name>.up.sql` / `.down.sql`, headers included) keeps its body byte for byte, under the number its place in the regenerated migration gives it. A carried step wins over a step of the same name the regeneration scaffolds again (a backfill's `todo`), and the line says so. The DDL steps, the files with a dialect suffix, are regenerated. Read every step marked `carried, review it`: it was written against the old parent.
- **The number.** It is kept when it is free, and the next one is taken when it is not. A lone `0007_add_x` whose parent changed is regenerated in place as `0007_add_x`.
- **Naming the migration.** `--replace` takes `NNNN_<name>`, or `NNNN` when one directory carries the number. A number two directories share is refused, naming both.
- **Only the newest.** A migration a later one chains from is refused: `replace the later one first, or delete it`. A regeneration that holds nothing (the models add nothing it does not already hold, and nothing is carried) is refused too: delete the directory.
- **Offline.** Like `new`, it reads the models and the directory, never a database; `--url` is refused. `--sql-step`, `--data-step`, `--data-only` and `--no-backfill` mean what they mean to `new`.
- **Interrupted.** Until the new directory is written, the old one waits under a hidden name: `.0007_add_x.0008_add_x.replaced` (`.0007_add_x.replaced` when the number is kept). A run killed in between leaves it there, and every command refuses the directory, naming it and what to do: rename it back to `0007_add_x` when the replacement was never written, delete it when the replacement is complete.

A database that applied the old `0007_add_x` meets the replaced migration as a changed one. `up` refuses it, naming the replacement as the second cause:

```text
ferro migrate: this database has applied 0007_add_x, which is not in migrations/.
The directory is behind the database: check out the branch that holds it, or 0007 was replaced: revert it with the files that were applied (check out the commit that applied it, `ferro migrate down --to 0006`), then `up`. Nothing was applied.
```

When the number was free and the migration was regenerated in place, the same cause follows the snapshot refusal (`0007_add_x/ir.json is not the snapshot this database applied 0007_add_x under`). Either way, a down needs the files that went up:

```text
$ git checkout <the commit that applied 0007_add_x>
$ ferro migrate down --to 0006
$ git checkout -
$ ferro migrate up
```

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
