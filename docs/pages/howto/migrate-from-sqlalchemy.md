---
description: "Translate each SQLAlchemy 2.0 idiom, from models to sessions and queries, into its Ferro equivalent."
---

# Migrate from SQLAlchemy

This page maps each SQLAlchemy 2.0 idiom to its Ferro equivalent, using one `User` and `Post` pair throughout. Ferro keeps a session, but it only routes calls to a connection and holds an identity map: there is no `add()`, `flush()` or `commit()`. Open one with `async with engines.session():` and write the call on the model:

=== "SQLAlchemy"

    ```python {test=compile reason="SQLAlchemy side of the comparison"}
    async with session_factory() as session:
        user = User(username="ada", email="ada@example.com", age=36)
        session.add(user)
        await session.commit()
    ```

=== "Ferro"

    ```python
    --8<-- "docs/examples/from_sqlalchemy.py:session"
    ```

The SQLAlchemy block inserts the row on `commit()`. The Ferro block inserts it inside `create()` and returns the instance with its primary key set. The Ferro blocks below run inside a session like this one; see [Connections › Sessions](../guide/connections.md#sessions).

## Comparison

| Concept | SQLAlchemy 2.0 | Ferro |
|---|---|---|
| Model definition | `DeclarativeBase` + `Mapped` / `mapped_column` | Pydantic `Model` + type annotations |
| Validation | Separate (for example Pydantic on top) | The model is a Pydantic model |
| Querying | `select(User).where(...)` executed on a session | `User.where(...)` awaited inside a session |
| Get by primary key | `session.get(User, pk)` returns `None` if missing | `User.get(pk)` raises; `User.get_or_none(pk)` returns `None` |
| Sessions | `async_sessionmaker`, `session.add`, `commit` | `async with engines.session():` once per request; no `add` or `commit` |
| Transactions | `async with session.begin():` | `async with transaction():` |
| Relationships | `relationship()` + `ForeignKey` columns | `Annotated[..., ForeignKey(...)]` + `BackRef` / `ManyToMany` |
| Async | Native | Native (async-only) |
| Migrations | Alembic | `ferro migrate`, or Alembic through `get_metadata()` and `ferro_options()` |

## Models

The SQLAlchemy models are:

```python {test=compile reason="SQLAlchemy side of the comparison"}
from sqlalchemy import ForeignKey
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(unique=True)
    email: Mapped[str]
    age: Mapped[int]
    posts: Mapped[list["Post"]] = relationship(back_populates="author")


class Post(Base):
    __tablename__ = "posts"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str]
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    author: Mapped["User"] = relationship(back_populates="posts")
```

The Ferro models, in either declaration style, are:

=== "Assignment"

    ```python
    --8<-- "docs/examples/from_sqlalchemy.py:models"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/from_sqlalchemy_annotated.py:models"
    ```

The two declare the same tables; the later sections reuse these models. What changes:

- There is no `__tablename__`: the table name comes from the class name (`user`).
- The auto-increment primary key is `int | None` with `default=None`: it is `None` until the row is inserted.
- Validation and serialization come from Pydantic, so the model doubles as your API schema.
- The `ForeignKey` annotation declares both the relation and the `author_id` column; Ferro creates the shadow column.
- Declare fields on each concrete model. A `Model` base class does not pass its fields on; shared behavior goes in plain mixins, as in the [Timestamps how-to](timestamps.md).

See the [Models and fields guide](../guide/models-and-fields.md) for every field option.

## Queries

Fetch every row:

=== "SQLAlchemy"

    ```python {test=compile reason="SQLAlchemy side of the comparison"}
    from sqlalchemy import select

    async with session_factory() as session:
        result = await session.execute(select(User))
        users = result.scalars().all()
    ```

=== "Ferro"

    ```python
    --8<-- "docs/examples/from_sqlalchemy.py:fetch-all"
    ```

Both return a list of `User` instances; the assert shows the Ferro list in insertion order. Filter, order and limit:

=== "SQLAlchemy"

    ```python {test=compile reason="SQLAlchemy side of the comparison"}
    stmt = select(User).where(User.age >= 18).order_by(User.age).limit(2)
    result = await session.execute(stmt)
    adults = result.scalars().all()
    ```

=== "Ferro"

    ```python
    --8<-- "docs/examples/from_sqlalchemy.py:filter"
    ```

Both return the two youngest adults as a list. Ferro takes lambda predicates named after the model; see the [Queries guide](../guide/queries.md) for the other styles.

Get by primary key. `session.get(User, pk)` returns `None` when the row is missing. Ferro's `User.get(pk)` raises `ModelDoesNotExist`, and `get_or_none` is the optional variant:

=== "SQLAlchemy"

    ```python {test=compile reason="SQLAlchemy side of the comparison"}
    user = await session.get(User, 1)
    ```

=== "Ferro"

    ```python
    --8<-- "docs/examples/from_sqlalchemy.py:get"
    ```

A hit returns the `User`. A miss raises, or returns `None` from `get_or_none`, which matches `session.get`.

See the [Queries guide](../guide/queries.md) for the full predicate and builder API.

## Creating records

`create()` inserts at once and returns the instance, so there is nothing to flush. Ferro also covers the common session idioms directly:

- `bulk_create([...])` inserts a batch.
- `get_or_create(...)` and `update_or_create(...)` cover upsert-style flows.
- `upsert(...)` is an insert-or-update on the primary key, like PostgreSQL `ON CONFLICT DO UPDATE`.
- `instance.refresh()` re-reads the row, like `session.refresh`.

Look up a user, or create one:

```python
--8<-- "docs/examples/from_sqlalchemy.py:upserts"
```

The user `ada` exists, so `created` is `False` and the stored row is unchanged. See the [Mutations guide](../guide/mutations.md) for each call.

## Relationships

Relations are lazy and awaitable. A forward foreign key returns the instance, and a `BackRef` returns a chainable query:

```python
--8<-- "docs/examples/from_sqlalchemy.py:relationships"
```

`post.author` returns the `User`, and `ada.posts` returns her posts, newest first. Many-to-many uses `ManyToMany(related_name=...)` on one side and `BackRef()` on the other; see the [Relationships guide](../guide/relationships.md).

## Transactions

SQLAlchemy commits when `session.begin()` exits. Ferro's `transaction()` runs everything inside it on one connection and commits or rolls back together:

=== "SQLAlchemy"

    ```python {test=compile reason="SQLAlchemy side of the comparison"}
    async with session.begin():
        session.add(User(username="margaret", email="m@example.com", age=30))
        raise RuntimeError("abort")  # rolls back
    ```

=== "Ferro"

    ```python
    --8<-- "docs/examples/from_sqlalchemy.py:transactions"
    ```

In both, the raised error rolls the insert back, and no `margaret` row exists afterwards. See the [Transactions guide](../guide/transactions.md).

## Migrations

Your Alembic chain keeps working through the switch. Ferro ships a bridge that builds a SQLAlchemy `MetaData` from your Ferro models, and its options keep Alembic's own comparator off Ferro's tables, so `alembic revision --autogenerate` writes the same changes Ferro's planner decides. Pass both in `env.py`:

```python {test=compile reason="an Alembic env.py: Alembic runs it, not the docs harness"}
from alembic import context

from ferro.migrations import ferro_options, get_metadata

context.configure(
    connection=connection,  # as generated
    target_metadata=get_metadata(),
    **ferro_options(),
)
```

The block hands Alembic Ferro's metadata. Install the extra with `pip install "ferro-orm[alembic]"`. The bridge fits while the project is in transition, or while Alembic still manages SQLAlchemy tables beside your Ferro models; see [Alembic](../guide/schema/alembic.md).

Once the models are all Ferro, move to Ferro's own [migrations](../guide/schema/migrations.md) (`pip install "ferro-orm[cli]"`), which also write backfills and SQLite table rebuilds. Bring each database to `alembic upgrade head` and [baseline it](../guide/schema/adopting-migrations.md#coming-from-alembic); no DDL runs. For development and tests, `connect(url, auto_migrate=True)` creates tables without migration files. See [Schema management](../guide/schema/overview.md) and the [Migrations API](../api/migrations.md).

## Features without a Ferro equivalent

Some SQLAlchemy features map differently, or have no Ferro counterpart today:

- **Eager loading** (`selectinload` / `joinedload`): forward-FK paths populate with [`include()`](../guide/queries.md#populating-relations-with-include) in one statement; `BackRef` and many-to-many collections still load lazily per access.
- **Partial column selects** (`select(User.id, User.name)`): [`select(lambda user: (user.id, user.username))`](../guide/queries.md#selecting-a-column-subset) returns projected records; traversal and output aliases use the dict form.
- **`func.sum` / `avg` / `min` / `max` and `group_by()`**: aggregate methods on columns (`order.amount.sum()`), with [grouping derived from the projection](../guide/aggregations.md). There is deliberately no `group_by()` chainer. `having()` is not built yet; filter groups in Python or use [raw SQL](../guide/raw-sql.md).
- **Atomic update expressions**: written as a recipe, not SQLAlchemy's `update().values(...)`: `update(lambda counter: {"n": counter.n + 1, "updated_at": now})`. Keyword `update()` accepts literals only. `update()` recipes support Postgres `.merge()`; `.concat()` is planned.

Where you hit a gap, `execute()` and `fetch_all()` run full SQL with bound parameters. For what is planned, see the [Roadmap](../roadmap.md).

## See also

- [Quickstart](../getting-started/quickstart.md): Ferro end to end in a few minutes
- [Queries guide](../guide/queries.md): the full query-building API
- [Schema management](../guide/schema/overview.md): auto-migrate, migrations and the Alembic bridge
