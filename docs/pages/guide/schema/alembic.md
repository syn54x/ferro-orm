---
description: "Let alembic revision --autogenerate write revisions for your Ferro models through the Alembic bridge."
---

# Alembic bridge

Ferro bridges your models into the SQLAlchemy metadata [Alembic](https://alembic.sqlalchemy.org/) reads, so `alembic revision --autogenerate` writes revisions for them. Add a `nickname` to the overview's `Author`:

=== "Assignment"

    ```python
    --8<-- "docs/examples/migrations_quickstart.py:models"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/migrations_quickstart_annotated.py:models"
    ```

Against a SQLite database that holds `author` without the field, `alembic revision --autogenerate -m "add nickname"` writes this revision:

<!-- revision: nickname -->
```python {test=compile reason="an Alembic revision; the docs tests regenerate it and compare"}
def upgrade() -> None:
    op.add_column('author', sa.Column('nickname', sa.String(), nullable=True))
    op.execute(sa.DDL('CREATE INDEX IF NOT EXISTS "idx_author_nickname" ON "author" ("nickname")'))


def downgrade() -> None:
    op.drop_index('idx_author_nickname', table_name='author')
    op.drop_column('author', 'nickname')
```

The column is Alembic's own `op.add_column`, and its index is the statement auto-migrate runs for the same change, byte for byte.

The bridge is a supported alternative to [Migrations](migrations.md) for a project that already runs Alembic, or keeps its own SQLAlchemy tables beside its Ferro models. Otherwise, use Migrations: the bridge writes no backfills and no SQLite table rebuilds. One database is managed by one door: Alembic's revisions, Ferro's migrations or auto-migrate ([one door per database](overview.md#one-door-per-database)).

## The `alembic` extra {#install}

Install the extra:

```bash
pip install "ferro-orm[alembic]"
```

It adds Alembic and SQLAlchemy, which generate and run revisions; Ferro never imports them at runtime.

## The Alembic environment {#initialize}

Scaffold an Alembic environment in a directory other than `migrations/`:

```bash
alembic init alembic
```

It writes `alembic.ini` and an `alembic/` directory holding `env.py` and `versions/`. Keep it out of `migrations/`: that is where `ferro migrate init` puts Ferro's own migrations, and it refuses a directory that holds an Alembic environment.

## `env.py` {#configure-envpy}

Pass Ferro's metadata and Ferro's options to `context.configure(...)` in `env.py`:

```python {test=compile reason="an Alembic env.py: Alembic runs it, not the docs harness"}
# alembic/env.py
from alembic import context

from ferro.migrations import ferro_options, get_metadata


def run_migrations_online() -> None:
    connectable = ...  # as generated
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=get_metadata(),
            **ferro_options(),
        )
        with context.begin_transaction():
            context.run_migrations()

# The rest of env.py stays as generated.
```

Autogenerate now compares Ferro's tables through Ferro's planner, and every other table through Alembic's own comparator:

- **`get_metadata()`** imports the models the [project configuration](../../reference/configuration.md) names (`ferro.toml` or `[tool.ferro]`) and returns their tables. With several configured databases, name one: `get_metadata("billing")`. Without a configuration it renders every registered model, so import your models module in `env.py` first.
- **`ferro_options()`** keeps Alembic's own comparator off Ferro's tables and both tracking tables, and renders the enum columns Alembic cannot. A project with its own `include_object` or `render_item` hooks passes them through it: `**ferro_options(include_object=mine, render_item=mine)`. Ferro's filter asks yours about every object it does not hide, and Ferro's renderer falls through to yours.
- Autogenerate over `get_metadata()` without `ferro_options()` is refused, naming the line to add.

An async `env.py` calls `context.configure(...)` inside the function it hands to `connection.run_sync(...)`:

```python {test=compile reason="an Alembic env.py: Alembic runs it, not the docs harness"}
from alembic import context

from ferro.migrations import ferro_options, get_metadata


def do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=get_metadata(),
        **ferro_options(),
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = ...  # as generated: async_engine_from_config(...)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
```

The options are the same; only where `configure` runs changes.

## How the bridge plans a revision {#one-decider}

The bridge does not compare schemas itself. Autogenerate reads the live database the way the auto-migrate pass reads it, plans the difference with the same planner, destructive changes included (a revision is reviewed before it runs), and writes the planner's operations as Alembic ops in the planner's order. An empty revision means no drift. A non-empty one holds what `connect(url, migrate_destructive=True)` would have done, plus one thing the pass never does: deleting a model drops its live table (`op.drop_table`, marked destructive), followed by `DROP TYPE` for any native enum type only that table used. `downgrade()` is the same planner run back from the models to the database as it was.

Where Alembic has an op of its own (a table, a column, its type and nullability, an index, a foreign key, a rename), the revision uses it. Everything else is the statement the auto-migrate pass would run, byte for byte, written as `op.execute(sa.DDL(...))`.

Alembic's version table, Ferro's tracking tables and any table your `include_object` or `include_name` filters exclude are never dropped. A dropped table's `downgrade()` puts the type and the table back as far as Ferro can read them: columns, types, nullability, indexes, foreign keys, checks and policies, but not server defaults or comments.

Generate and apply a revision:

```bash
alembic revision --autogenerate -m "add nickname"
alembic upgrade head
```

The first command writes the revision shown at the top of this page into `alembic/versions/`; read it before the second applies it.

### Marked operations

An operation that drops data, or that fails on existing rows, carries a comment saying so. Say `Author` also had `bio: str | None = None` and you delete that line; the revision marks the drop:

<!-- revision: drop-bio -->
```python {test=compile reason="an Alembic revision; the docs tests regenerate it and compare"}
def upgrade() -> None:
    # ferro: destructive (drops author.bio and the data it holds)
    op.drop_column('author', 'bio')


def downgrade() -> None:
    op.add_column('author', sa.Column('bio', sa.String(), nullable=True))
```

The `downgrade()` puts the column back empty: the values it held are gone.

A required column added to a table that has rows is a **data-dependent** op, a change existing rows need a value for. On Postgres the bridge writes it plain, under `# ferro: data-dependent (fails while author has rows; ferro migrations generate the backfill: \`ferro migrate new\`)`. The backfill is yours to write as `op.execute(...)` statements ahead of it, or generate the change as a [migration](data-steps.md), which writes the backfill for you. On SQLite the same add is refused (below).

### Enum types

Every native Postgres enum type a revision needs is created by the planner's own guarded `CREATE TYPE` statement, ahead of the table operations. Every column of that type is written `postgresql.ENUM(..., create_type=False)`, so SQLAlchemy never creates the type a second time (the `render_item` that `ferro_options()` carries writes that flag; SQLAlchemy's own rendering drops it). A `downgrade()` drops the types its upgrade created, after the tables that used them.

Enum label changes follow the planner too:

- A label added to a `StrEnum` is `ALTER TYPE ... ADD VALUE IF NOT EXISTS` inside an `autocommit_block()`. Its `downgrade()` raises: rows may hold the label, so labels are append-only on this door.
- A label renamed with `__ferro_renamed_labels__` is `ALTER TYPE ... RENAME VALUE`.
- A label removed is not written: the revision leaves that change out, and the type keeps the label. Existing rows need a new value first, so [Migrations](data-steps.md#removing-an-enum-label) generate its backfill and the swap to a type without it.

## What autogenerate refuses

The bridge refuses, before writing a revision, what an Alembic revision cannot write safely. Each refusal names where the change goes instead:

| Refusal | Why | Where it goes |
| :--- | :--- | :--- |
| `env.py` passes `get_metadata()` without `**ferro_options()` | Alembic would compare Ferro's tables a second way | Add `**ferro_options()` to `context.configure(...)` |
| The database carries Ferro's `_ferro_migrations` tracking table | The database is managed by Ferro's migrations | `ferro migrate new`. A project whose Alembic chain still manages its own SQLAlchemy tables drops `get_metadata()` from `target_metadata` and keeps `**ferro_options()` |
| A primary key changes | No door changes a key in place | [Changing a primary key](migrations.md#changing-a-primary-key) |
| A change SQLite can only make by rebuilding the table (a type, a nullability, a constraint on an existing table), or a `NOT NULL` column with no default added to an existing SQLite table | Alembic's batch mode does not handle foreign-key pragmas, so its rebuild cascades into `ON DELETE CASCADE` children | `ferro migrate new`, which writes the [table rebuild](migrations.md#sqlite-table-rebuilds) (and the backfill) |

A rename hint whose old name is still declared is refused with the generator's reason.

## Moving from Alembic to Migrations

A database Alembic manages can move to Ferro's migrations without running any DDL: bring it to `alembic upgrade head`, generate the first migration, and `ferro migrate baseline` it. [Adopting migrations on an existing database](adopting-migrations.md#coming-from-alembic) walks through it.

## See also

- [Schema management overview](overview.md): which door covers which change
- [Migrations API reference](../../api/migrations.md): `get_metadata()`, `ferro_options()`, `render_item()`
