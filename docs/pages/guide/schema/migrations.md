# Migrations

A migration is a numbered directory of steps that changes a database from one version of your models to the next. `ferro migrate new` writes it from the difference between your models and the previous migration; you read it, commit it, and `ferro migrate up` applies it. This is rung 3 of [the ladder](overview.md#the-ladder), and the door Ferro recommends for any database whose data you would mind losing.

## Installation and setup {#install-and-set-up}

Install the CLI extra:

```bash
pip install "ferro-orm[cli]"
```

The `ferro` command is now on your path. `ferro migrate init` writes the [project configuration](../../reference/configuration.md) and creates the migrations directory. On a terminal it asks for what its flags do not answer (the config file, the models module, the target dialects, whether there is another database). Pass every answer as a flag and `init` asks nothing:

```bash
ferro migrate init --models blog.models --dialects postgres,sqlite --config-file pyproject.toml
```

It prints what it wrote:

<!-- output: init-pyproject -->
```text
[tool.ferro]
models = ["blog.models"]
dialects = ["postgres", "sqlite"]
Wrote [tool.ferro] to pyproject.toml and created migrations/
```

The project now has its configuration and an empty `migrations/`. The questions, and what each answer means:

- **Config file**: `pyproject.toml` (a `[tool.ferro]` table, the default when the project has one) or `ferro.toml`.
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

The `# new` comment marks the field the edit adds; `0001_create_author`, the first migration, created the table without it. `ferro migrate check` notices the edit, offline, and says what to run:, offline, and says what to run:

<!-- output: loop-check -->
```text
default · migrations
ungenerated: the models changed since 0001_create_author and no migration records it (changed models: Author); run `ferro migrate new <name>` and commit the migration it writes
```

It exits 3, so a CI job running it fails until the migration exists. `ferro migrate new author_nickname` writes the migration, one file per step and dialect, and lists what changed:

<!-- output: loop-new -->
```text
default · migrations
ok Wrote migrations/0002_author_nickname/
  01_schema.up.postgres.sql                 schema step, up (postgres)
  01_schema.down.postgres.sql               schema step, down (postgres)
  01_schema.up.sqlite.sql                   schema step, up (sqlite)
  01_schema.down.sqlite.sql                 schema step, down (sqlite)
  02_idx_author_nickname.up.postgres.sql    schema step, up (postgres)
  02_idx_author_nickname.down.postgres.sql  schema step, down (postgres)
  02_idx_author_nickname.up.sqlite.sql      schema step, up (sqlite)
  02_idx_author_nickname.down.sqlite.sql    schema step, down (sqlite)
  ir.json                                   snapshot

  1 changed model
    Author
```

The column is one step and its index a second, each rendered for both dialects. Read the files, commit them, and `ferro migrate up` applies them, one line per step:

<!-- output: loop-up -->
```text
default · sqlite:///./app.db
0002_author_nickname  01_schema               applied (0 ms)
0002_author_nickname  02_idx_author_nickname  applied (0 ms)
```

The database now has the `nickname` column and its index. `new` diffs the models against the newest migration's [schema snapshot](#reading-a-migration), never against a database, so it needs no connection and gives every developer the same files. A change that renders no DDL (a Python-side default, a back-reference) writes nothing and says `no schema change: nothing written`.

The example `docs/examples/migrations_quickstart.py` in the repository runs the whole loop on SQLite: `init`, `new`, `up`, `check`, `drift`, `down`.

## Reading a migration

`new` writes one directory per migration:

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

Each directory holds a migration's steps and the schema snapshot it was generated from:

- **Migrations are numbered** `0001`, `0002`, …; numbers count migrations, never steps. A migration is applied whole and recorded step by step, so a failure resumes at the step that failed.
- **Steps are numbered within a migration** (`01_`, `02_`, …) and share their number across dialects. A DDL step has an `.up` and a `.down` file per target dialect. Where a dialect has nothing to do, its rendering says so with `-- ferro: not-applicable`.
- **A data step** is a Python file, `NN_<name>.py`, that serves every dialect; see [Data steps and backfills](data-steps.md).
- **`ir.json`** is the schema snapshot: the declared models when the migration was generated, linked by checksum to the previous one. The next `new` diffs against it, and data steps build their [historical models](data-steps.md#historical-models) from it. Never edit it: later migrations and every database that applied this one depend on it.

The SQL is exactly what the auto-migrate passes run for the same change; Postgres and SQLite each get their own spelling:

```sql
-- 0001_create_author/01_schema.up.postgres.sql
CREATE TABLE IF NOT EXISTS "author" ( "email" varchar NOT NULL, "id" serial PRIMARY KEY NOT NULL, "name" varchar NOT NULL );

CREATE UNIQUE INDEX IF NOT EXISTS "uq_author_email" ON "author" ("email");

-- 0001_create_author/01_schema.up.sqlite.sql
CREATE TABLE IF NOT EXISTS "author" ( "email" varchar NOT NULL, "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "name" varchar NOT NULL );

CREATE UNIQUE INDEX IF NOT EXISTS "uq_author_email" ON "author" ("email");

-- 0001_create_author/01_schema.down.postgres.sql
DROP TABLE "author";
```

The table and the unique index on `email` are created together, and the down drops the table.

### Step file headers

A step file may open with `-- ferro: …` lines that say how the runner treats it: whether it discards data, runs outside a transaction, or turns SQLite's foreign-key checks off. The generator writes them, except `expand` and `irreversible`, which only a person writes; you read them in review. The [header table](../../reference/migrations.md#step-file-headers) lists every one.

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

## Backfills

Adding a required column to a table with rows, making a column required, or removing an enum label that rows still hold cannot be done by DDL alone: someone has to say what the existing rows get. `new` splits such a migration into an **expand step** (the new column, nullable), a **backfill** per model (a Python data step you write), and a **contract step** (the `NOT NULL`, the label removal), and `up` refuses the migration until the backfill is written. When you know no row needs a value, `--no-backfill <table>.<column>` writes a **guard step** instead, which fails the migration if one does. Both are on [Data steps and backfills](data-steps.md).

## Renames and retirements

A column, a table or an enum label renamed in the models is a drop and an add unless you declare a rename hint, and a column or a label leaves over two releases so the release still serving keeps working. [Renaming and retiring columns and labels](renames-and-retirements.md) shows both.

## Hand-written steps

`new` can add a step for what the generator does not write. Add a SQL step:

```bash
ferro migrate new seed --sql-step seed_admins
```

It writes the step beside the snapshot:

<!-- output: sql-step -->
```text
default · migrations
ok Wrote migrations/0002_seed/
  01_seed_admins.up.sql    SQL step, up
  01_seed_admins.down.sql  SQL step, down
  ir.json                  snapshot
```

The two SQL files are yours to write, and the snapshot is a copy of the previous one. Add a data step and nothing else:

```bash
ferro migrate new fix_slugs --data-only --data-step Author
```

It writes one Python step and a copy of the snapshot:

<!-- output: data-step -->
```text
default · migrations
ok Wrote migrations/0003_fix_slugs/
  01_backfill_author.py  data step
  ir.json                snapshot
```

The data step says `todo("write this step")` where your code goes. The flags:

- `--sql-step <name>` adds `NN_<name>.up.sql` / `.down.sql` without a dialect suffix: one file serves every dialect, so write portable SQL. The files start as `-- write this step`.
- `--data-step <Model>` adds a Python data step over that model (`NN_backfill_<model>.py`), with `todo("write this step")` in its `up` and `down`.
- `--data-only` writes only the data step: no DDL, and a copy of the previous snapshot.

`--data-step` also works beside a generated change (`ferro migrate new add_writer --data-step Writer`): the data step is placed after the generated schema steps. A step that still says `todo(...)` is an *unwritten step*: `check` reports it and `up` refuses the migration before running anything.

## SQLite table rebuilds

SQLite cannot add a constraint, change a type or nullability, or drop a constraint on a table that exists. A migration does it the only way SQLite allows, inside the step whose change needs it:

1. It creates the table again in its new shape, under a temporary name.
2. It copies the rows and the table's `AUTOINCREMENT` counter across.
3. It drops the old table and renames the new one into place.
4. It recreates the table's indexes.

[Data steps and backfills](data-steps.md#what-the-generator-writes) quotes a whole rebuild, the contract that makes `author.slug` required.

The copy carries only what the schema snapshot declares, so before a rebuild runs, the runner refuses a live table that holds anything else, naming each object and its fix. Make `nickname` required (`nickname: str = Field(index=True)`) on a database whose `author` table also holds an index and a trigger ferro never declared, generate the change with `ferro migrate new nickname_required --no-backfill author.nickname`, and `ferro migrate up` stops at the rebuild:

<!-- output: rebuild-refused -->
```text
default · sqlite:///./app.db
0003_nickname_required  01_guard_author    applied (0 ms)
0003_nickname_required  02_add_constraint  applied (0 ms)
ferro migrate: a SQLite rebuild of table "author" copies only the columns and recreates only the indexes the schema snapshot declares, and the live table also holds:
  - index "my_idx": drop it, or declare it on the model in a migration (ferro builds the indexes it declares, named idx_/uq_)
  - trigger "t": drop it; a rebuild's DROP TABLE would drop it, and ferro cannot carry it across
Nothing was applied.
```

The steps before the rebuild have run; the rebuild has not, and the table is as it was. An undeclared column is refused the same way (`declare it on the model in a migration, or drop it`).

## `check`, `status` and `drift`

Three verbs tell you where the models, the directory and a database stand:

- `ferro migrate check` needs no database. It answers whether every model change has a migration and the directory is intact: no broken snapshot chain, no duplicate or missing number, no step missing a target dialect's rendering, no unwritten step. `check` belongs in CI and in a [pre-commit hook](deploying.md#a-pre-commit-hook).
- `ferro migrate status` reads a database and lists which migrations it has applied ([Database status](deploying.md#database-status)).
- `ferro migrate drift` reads a database and compares its live schema with the snapshot of the last migration applied.

Drop by hand the index `up` built, and `ferro migrate drift` lists the difference:

<!-- output: checking-drift -->
```text
default · sqlite:///./app.db
xx 1 difference against 0002_author_nickname
  - idx_author_nickname index is missing
```

It exits 4. Drift is reported, never repaired: write the fix as a migration, or put the object back. Tables the migrations never declared are not drift. The [CLI reference](../../reference/cli.md#exit-codes) lists every verb's exit codes.

## Reverting with `down`

`ferro migrate down` prints what it will revert and, on a terminal, asks `Revert 0002_author_nickname (2 steps)? [y/N]`. Skip the question with `--yes`:

```bash
ferro migrate down --yes
```

It reverts the latest migration, newest step first:

<!-- output: going-back -->
```text
default · sqlite:///./app.db
down reverts, in this order:
  0002_author_nickname  02_idx_author_nickname
  0002_author_nickname  01_schema
0002_author_nickname  02_idx_author_nickname  reverted (0 ms)
0002_author_nickname  01_schema               reverted (0 ms)
```

The `nickname` column and its index are gone, and `0002_author_nickname` is pending again.

- With no flag, `down` reverts the latest applied migration; `--to 0005` leaves `0005` fully applied, `--to 0007:02` leaves steps `01`–`02` of `0007` applied, `--to 0000` (or `--all`) reverts everything.
- Without a terminal and without `--yes`, it prints the plan and refuses.
- A down restores the schema of the previous snapshot, never the rows a drop removed. A step declared irreversible stops the whole run before anything is reverted.
- A migration that `baseline` recorded created nothing on that database, so `down` refuses to go below it ([Adopting migrations](../../howto/adopting-migrations.md#undoing-a-baseline)).

## Edited migrations

A migration no database has applied is yours to change: edit it, or delete its directory and run `new` again. Once a database has applied a step, `up` checks the step's file against the checksum it recorded, and an edited file is refused; see [Edited files and `rerecord`](deploying.md#edited-files-and-rerecord).

## Duplicate migration numbers and `--replace`

A teammate merges `0002_author_nickname`, the migration the loop above wrote. Meanwhile your branch added `bio` to `Author` and generated `0002_author_bio`, with a hand-written SQL step `02_fix_rows`. Both were generated on top of `0001`, so after you rebase, the directory holds two `0002`s, and every command refuses it. `ferro migrate check` says:

<!-- output: duplicate-number -->
```text
default · migrations
duplicate_number: two migrations are numbered 0002: 0002_author_bio and 0002_author_nickname. Keep the one that is applied anywhere and regenerate the other on top of it:
  ferro migrate new author_bio --replace 0002_author_bio
  ferro migrate new author_nickname --replace 0002_author_nickname
(each line regenerates the migration it names)
```

`0002_author_nickname` is on main, so keep it and regenerate yours:

```bash
ferro migrate new author_bio --replace 0002_author_bio
```

It writes the migration again under the next number:

<!-- output: replace -->
```text
default · migrations
ok Wrote migrations/0003_author_bio/  (replaces 0002_author_bio, now deleted)
  01_schema.up.postgres.sql    schema step, up (postgres)    moved (was 0002_author_bio/01_schema.up.postgres.sql)
  01_schema.down.postgres.sql  schema step, down (postgres)  moved (was 0002_author_bio/01_schema.down.postgres.sql)
  01_schema.up.sqlite.sql      schema step, up (sqlite)      moved (was 0002_author_bio/01_schema.up.sqlite.sql)
  01_schema.down.sqlite.sql    schema step, down (sqlite)    moved (was 0002_author_bio/01_schema.down.sqlite.sql)
  02_fix_rows.up.sql           SQL step, up                  carried, review it (was 0002_author_bio/02_fix_rows.up.sql)
  02_fix_rows.down.sql         SQL step, down                carried, review it (was 0002_author_bio/02_fix_rows.down.sql)
  ir.json                      snapshot                      new snapshot, chained from 0002_author_nickname

  1 changed model
    Author
```

`0003_author_bio` is what `new` would have written had `0002_author_nickname` been there first: a new schema snapshot chained from `0002_author_nickname`'s, and the DDL regenerated from the difference, so it adds `bio` and nothing of `nickname`.

- **What is carried.** Every data step (`NN_<name>.py`) and hand-written SQL step (`NN_<name>.up.sql` / `.down.sql`, headers included) keeps its body byte for byte, under the number its place in the regenerated migration gives it. A carried step wins over a step of the same name the regeneration scaffolds again (a backfill's `todo`), and the line says so. The DDL steps, the files with a dialect suffix, are regenerated. Read every step marked `carried, review it`: it was written against the old parent.
- **The number.** It is kept when it is free, and the next one is taken when it is not. A lone `0002_author_bio` whose parent changed is regenerated in place as `0002_author_bio`.
- **Naming the migration.** `--replace` takes `NNNN_<name>`, or `NNNN` when one directory carries the number. A number two directories share is refused, naming both.
- **Only the newest.** A migration a later one chains from is refused: `replace the later one first, or delete it`. A regeneration that holds nothing (the models add nothing it does not already hold, and nothing is carried) is refused too: delete the directory.
- **Offline.** Like `new`, it reads the models and the directory, never a database; `--url` is refused. `--sql-step`, `--data-step`, `--data-only` and `--no-backfill` mean what they mean to `new`.
- **Interrupted.** Until the new directory is written, the old one waits under a hidden name: `.0002_author_bio.0003_author_bio.replaced` (`.0002_author_bio.replaced` when the number is kept). A run killed in between leaves it there, and every command refuses the directory, naming it and what to do: rename it back to `0002_author_bio` when the replacement was never written, delete it when the replacement is complete.

A database that applied the old `0002_author_bio` meets the replaced migration as a changed one. `ferro migrate up` refuses it, naming the replacement as the second cause:

<!-- output: replaced-up-refused -->
```text
default · sqlite:///./app.db
ferro migrate: this database has applied 0002_author_bio, which is not in migrations/.
The directory is behind the database: check out the branch that holds it, or 0002 was replaced: revert it with the files that were applied (check out the commit that applied it, `ferro migrate down --to 0001`), then `up`. Nothing was applied.
```

Nothing was applied. When the number was free and the migration was regenerated in place, the same cause follows the snapshot refusal (`0002_author_bio/ir.json is not the snapshot this database applied 0002_author_bio under`). Either way, a down needs the files that went up:

```bash
git checkout <the commit that applied 0002_author_bio>
ferro migrate down --to 0001
git checkout -
ferro migrate up
```

The database is back at `0001`, and `up` applies `0002_author_nickname` and `0003_author_bio`.

## Squashing

A feature branch that wrote one migration per task can replace them with the one migration `new` would have written across them, before it merges. [Squashing a feature branch's migrations](squash.md) shows when and how, and what a database that applied the originals does with the squash.

## Changing a primary key

No door changes a primary key in place: the key moved to another column, the key column's type changed, or a key made composite. `new` refuses it before writing anything, on every dialect. Change `Author`'s key and run:

```bash
ferro migrate new rekey
```

It writes nothing and says:

<!-- output: rekey-refused -->
```text
default · migrations
changing the primary key of "author" is not generated: write it as a new table (ferro migrate new --data-step …), a backfill of parent and children, and a drop; see the Migrations docs § Changing a primary key
```

The recipe, for an `author` table keyed by an integer `id` that should be keyed by a `UUID`, with `book.author_id` pointing at it:

1. **Add the new table beside the old one.** Declare a new model (`Writer`) with the new key, and give each child a nullable relation to it (`Book.writer`). Generate the migration with a data step over the new model, and write the step so it copies each parent row and points each child at its copy:

    ```bash
    ferro migrate new add_writer --data-step Writer
    ```

    The data step lands after the generated schema steps, as `03_backfill_writer.py` here, with `todo(...)` where you write the copy. Written, it reads:

    ```python {test=compile reason="a data step file; the runner calls it with a step context during ferro migrate up"}
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

    Every author now has a writer, and every book points at it. The step runs inside the step's own transaction, so it opens no session. On a large table, write it as a [`@chunked`](data-steps.md#atomic-or-chunked) step instead.

2. **Drop the old table.** Remove `Author` and `Book.author`, and make `Book.writer` required. Every book already has a writer, so generate the change with a guard rather than a backfill:

    ```bash
    ferro migrate new drop_author --no-backfill book.writer_id
    ```

    The drop is marked `-- ferro: destructive`. Its down restores the schema, not the rows: if reverting past it must be impossible, declare its down `-- ferro: irreversible <reason>`.

3. **Take the old name back, if you want it.** Rename `Writer` to `Author` with `__ferro_renamed_from__ = "writer"` and the relation with `ForeignKey(..., renamed_from="writer")`, and generate the rename ([rename hints](renames-and-retirements.md#rename-hints)).

On a server that deploys with rolling restarts, ship each step as its own release, as the [two-release contract](deploying.md#rolling-deploys-and-the-two-release-contract) describes: code running during step 1 must write both keys.

A native enum column moved to another enum type, while other columns stay on the old type, is refused the same way, with its own recipe: add a column of the new type, copy the values across in a data step (`ferro migrate new --data-step …`), then drop the old column.

## See also

- [Data steps and backfills](data-steps.md): what to write where `new` says `todo(...)`
- [Renaming and retiring columns and labels](renames-and-retirements.md): rename hints and two-release retirements
- [Squashing a feature branch's migrations](squash.md): one migration in place of a branch's many
- [Deploying migrations](deploying.md): `up` in a deploy, `require_applied()`, rolling deploys, edited files
- [Testing migrations](testing.md): prove a migration in your test suite
- [Migrations reference](../../reference/migrations.md): step file headers, step classes, door coverage
- [CLI reference](../../reference/cli.md): every verb, flag and exit code
