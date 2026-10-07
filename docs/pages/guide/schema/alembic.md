# Alembic

Ferro bridges your models into the SQLAlchemy metadata [Alembic](https://alembic.sqlalchemy.org/) uses, so `alembic revision --autogenerate` writes revisions for them. It is a supported alternative to [Migrations](migrations.md) for a project that already runs Alembic, or keeps its own SQLAlchemy tables beside its Ferro models. Otherwise, Ferro recommends Migrations: the bridge writes no backfills and no SQLite table rebuilds.

One database is managed by one door: Alembic's revisions, or Ferro's migrations, or auto-migrate ([one door per database](overview.md#one-door-per-database)).

## Install

```bash
pip install "ferro-orm[alembic]"
```

This adds Alembic and SQLAlchemy, used only to generate and run revisions, never by Ferro at runtime.

## Initialize

```bash
alembic init alembic
```

This scaffolds `alembic.ini` and an `alembic/` directory with `env.py` and `versions/`. Avoid `alembic init migrations`: `migrations/` is where `ferro migrate init` puts Ferro's own migrations, and it refuses a directory that holds an Alembic environment.

## Configure env.py

`env.py` passes Ferro's metadata and Ferro's options to `context.configure(...)`:

```python
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

- **`get_metadata()`** imports the models the [project configuration](../../reference/configuration.md) names (`ferro.toml` or `[tool.ferro]`) and returns their tables; with several configured databases, name one: `get_metadata("billing")`. Without a configuration it renders every registered model, so import your models module in `env.py` first.
- **`ferro_options()`** keeps Alembic's own comparator off Ferro's tables and both tracking tables, so every operation on a Ferro table comes from Ferro's planner, and renders the enum columns Alembic cannot. A project with its own `include_object` or `render_item` hooks passes them through it: `**ferro_options(include_object=mine, render_item=mine)`. Ferro's filter asks yours about every object it does not hide; Ferro's renderer falls through to yours.
- Autogenerate over `get_metadata()` without `ferro_options()` is refused, naming the line to add.

An async `env.py` works the same way: call `context.configure(...)` inside the function you hand to `connection.run_sync(...)`:

```python
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

## One decider

The bridge does not compare schemas itself. Autogenerate reads the live database the way the auto-migrate pass reads it, plans the difference with the same planner (destructive changes included, since a revision is reviewed before it runs), and writes the planner's operations as Alembic ops, in the planner's order. An empty revision means no drift; a non-empty one holds exactly what `connect(url, migrate_destructive=True)` would have done. `downgrade()` is the same planner run back from the models to the database as it was.

Where Alembic has an op of its own (a table, a column, its type and nullability, an index, a foreign key, a rename), the revision uses it. Everything else is the statement the auto-migrate pass would run, byte for byte, as `op.execute(sa.DDL(...))`.

One change goes beyond the pass: deleting a model drops its live table (`op.drop_table`, marked destructive), the same table Alembic would drop, followed by `DROP TYPE` for any native enum type only that table used. Alembic's version table, ferro's tracking tables and any table your `include_object` or `include_name` filters exclude are never dropped, and the `downgrade()` puts the type and the table back as far as ferro can read them (columns, types, nullability, indexes, foreign keys, checks and policies; not server defaults or comments).

```bash
alembic revision --autogenerate -m "add nickname"
alembic upgrade head
```

Always review the revision before applying it.

### Marked operations

An operation that drops data, or that fails on existing rows, carries a comment saying so:

```python
def upgrade():
    # ferro: data-dependent (fails while card has rows; ... `ferro migrate new`)
    op.add_column('card', sa.Column('size', sa.Integer(), nullable=False))
    # ferro: destructive (drops card.legacy and the data it holds)
    op.drop_column('card', 'legacy')
```

A **data-dependent** op is a change existing rows need a value for (here, a required column). The bridge writes it plain: the backfill is yours to write as `op.execute(...)` statements ahead of it, or generate the change as a [migration](data-steps.md), which writes the backfill for you. On SQLite such an add is refused instead (below).

### Enum types

Every native Postgres enum type a revision needs is created by the planner's own guarded `CREATE TYPE` statement, ahead of the table operations, and every column of it is written `postgresql.ENUM(..., create_type=False)` so SQLAlchemy never creates it a second time (the `render_item` that `ferro_options()` carries writes that flag; SQLAlchemy's own rendering drops it). A `downgrade()` drops the types its upgrade created, after the tables that used them.

Enum label changes follow the planner too:

- A label added to a `StrEnum` is `ALTER TYPE ... ADD VALUE IF NOT EXISTS` inside an `autocommit_block()`. Its `downgrade()` raises: enum labels are append-only on this door (ADR-0011), since rows may hold the label.
- A label renamed with `__ferro_renamed_labels__` is `ALTER TYPE ... RENAME VALUE`.
- A label removed is a change existing rows need a value for: [Migrations](data-steps.md#removing-an-enum-label) generate its backfill and the swap to a type without it.

## What autogenerate refuses

The bridge refuses, before writing a revision, what an Alembic revision cannot write safely. Each refusal names where the change goes instead:

| Refusal | Why | Where it goes |
| :--- | :--- | :--- |
| `env.py` passes `get_metadata()` without `**ferro_options()` | Alembic would compare Ferro's tables a second way | Add `**ferro_options()` to `context.configure(...)` |
| The database carries Ferro's `_ferro_migrations` tracking table | The database is managed by Ferro's migrations | `ferro migrate new`. A project whose Alembic chain still manages its own SQLAlchemy tables drops `get_metadata()` from `target_metadata` and keeps `**ferro_options()` |
| A primary key changes | No door changes a key in place | [Changing a primary key](migrations.md#changing-a-primary-key) |
| A change SQLite can only make by rebuilding the table (a type, a nullability, a constraint on an existing table), or a `NOT NULL` column added to a SQLite table with rows and no default | Alembic's batch mode does not handle foreign-key pragmas, so its rebuild cascades into `ON DELETE CASCADE` children | `ferro migrate new`, which writes the [table rebuild](migrations.md#sqlite-table-rebuilds) (and the backfill) |

A refused rename hint (a hint whose old name is still declared) is refused with the generator's reason.

## Moving from Alembic to Migrations

A database Alembic manages can move to Ferro's migrations without running any DDL: bring it to `alembic upgrade head`, generate the first migration, and `ferro migrate baseline` it. See [Adopting migrations on an existing database](../../howto/adopting-migrations.md#coming-from-alembic).

## See Also

- [Schema Management overview](overview.md) — which door covers which change
- [Migrations API reference](../../api/migrations.md) — `get_metadata()`, `ferro_options()`, `render_item()`
