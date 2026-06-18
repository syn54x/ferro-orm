# Migrating from SQLAlchemy

Ferro's model-centric API replaces SQLAlchemy's session/statement split: models are Pydantic classes, queries hang off the model, and there is no session to manage. This page maps each SQLAlchemy concept to its Ferro equivalent.

## Quick Comparison

| Concept | SQLAlchemy 2.0 | Ferro |
|---|---|---|
| Model definition | `DeclarativeBase` + `Mapped` / `mapped_column` | Pydantic `Model` + type annotations |
| Validation | Separate (e.g. Pydantic on top) | Built in — models *are* Pydantic models |
| Querying | `select(User).where(...)` executed via a session | `User.where(...)` awaited directly |
| Get by primary key | `session.get(User, pk)` → `None` if missing | `User.get(pk)` raises; `User.get_or_none(pk)` → `None` |
| Sessions | Required (`async_sessionmaker`, `session.add`, `commit`) | None — connections are managed for you |
| Transactions | `async with session.begin():` | `async with transaction():` |
| Relationships | `relationship()` + `ForeignKey` columns | `Annotated[..., ForeignKey(...)]` + `BackRef` / `ManyToMany` |
| Async | Native | Native (async-only) |
| Migrations | Alembic | Alembic (via `ferro.migrations.get_metadata`) |

## Models

SQLAlchemy:

```python
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(unique=True)
    email: Mapped[str]
```

Ferro:

=== "Assignment"

    ```python
    from ferro import Field, Model


    class User(Model):
        id: int | None = Field(default=None, primary_key=True)
        username: str = Field(unique=True)
        email: str
    ```

=== "Annotated"

    ```python
    from typing import Annotated

    from ferro import Field, Model


    class User(Model):
        id: Annotated[int | None, Field(default=None, primary_key=True)]
        username: Annotated[str, Field(unique=True)]
        email: str
    ```

Differences worth noting:

- No `__tablename__` — the table name is derived from the class name (`user`).
- The auto-increment primary key is annotated `int | None` with `default=None`: it is `None` until the row is inserted.
- Because Ferro models are Pydantic models, you get validation, serialization, and FastAPI integration for free — no separate schema classes.
- Declare fields on each concrete model. Ferro does not support inheriting fields from a `Model` base class (the ORM registers query proxies per model class); shared behavior goes in plain mixins instead — see the [Timestamps how-to](timestamps.md).

## Queries

Fetch all:

```python
# SQLAlchemy
from sqlalchemy import select

async with session_factory() as session:
    result = await session.execute(select(User))
    users = result.scalars().all()
```

```python
# Ferro
users = await User.all()
```

Filtering, ordering, limiting:

```python
# SQLAlchemy
stmt = select(User).where(User.age >= 18).order_by(User.age).limit(10)
result = await session.execute(stmt)
adults = result.scalars().all()
```

```python
# Ferro
adults = await User.where(lambda t: t.age >= 18).order_by(User.age).limit(10).all()
```

Get by primary key — the semantics differ. `session.get(User, pk)` returns `None` when the row is missing; Ferro's `User.get(pk)` raises `ModelDoesNotExist`, and `User.get_or_none(pk)` is the optional variant:

```python
# SQLAlchemy
user = await session.get(User, 1)
```

```python
# Ferro — raises if missing
from ferro import ModelDoesNotExist

try:
    user = await User.get(1)
except ModelDoesNotExist:
    user = None

# Ferro — optional (like session.get when no row)
user = await User.get_or_none(1)
```

See the [Queries guide](../guide/queries.md) for the full predicate and builder API.

## Creating Records

```python
# SQLAlchemy
async with session_factory() as session:
    user = User(username="alice", email="alice@example.com")
    session.add(user)
    await session.commit()
```

```python
# Ferro
user = await User.create(username="alice", email="alice@example.com")
```

There is no unit of work to flush: `create()` inserts immediately and returns the instance with its primary key set. Ferro also covers the common session idioms directly: `bulk_create([...])` for batch inserts, `get_or_create(...)` and `update_or_create(...)` for upsert-style flows, and `instance.refresh()` to re-read from the database (the rough analog of `session.refresh`).

## Relationships

```python
# SQLAlchemy
from sqlalchemy import ForeignKey
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    posts: Mapped[list["Post"]] = relationship(back_populates="author")


class Post(Base):
    __tablename__ = "posts"

    id: Mapped[int] = mapped_column(primary_key=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    author: Mapped["User"] = relationship(back_populates="posts")
```

=== "Assignment"

    ```python
    # Ferro
    from typing import Annotated

    from ferro import BackRef, Field, ForeignKey, Model, Relation


    class User(Model):
        id: int | None = Field(default=None, primary_key=True)
        posts: Relation[list["Post"]] = BackRef()


    class Post(Model):
        id: int | None = Field(default=None, primary_key=True)
        author: Annotated[User, ForeignKey(related_name="posts")]
    ```

=== "Annotated"

    ```python
    # Ferro
    from typing import Annotated

    from ferro import BackRef, Field, ForeignKey, Model, Relation


    class User(Model):
        id: Annotated[int | None, Field(default=None, primary_key=True)]
        posts: Relation[list["Post"]] = BackRef()


    class Post(Model):
        id: Annotated[int | None, Field(default=None, primary_key=True)]
        author: Annotated[User, ForeignKey(related_name="posts")]
    ```

The `ForeignKey` annotation declares both the relation and the underlying `author_id` column (Ferro creates the shadow column for you). Accessing relations is lazy and awaitable:

```python
author = await post.author                 # forward FK → instance
posts = await user.posts.all()             # BackRef → chainable query
recent = await user.posts.order_by(Post.id, "desc").limit(5).all()
```

Many-to-many uses `ManyToMany(related_name=...)` on one side and `BackRef()` on the other — see the [Relationships guide](../guide/relationships.md).

## Transactions

```python
# SQLAlchemy
async with session.begin():
    session.add(User(username="alice"))
    # commits on exit, rolls back on exception
```

```python
# Ferro
from ferro import transaction

async with transaction():
    await User.create(username="alice")
    # commits on exit, rolls back on exception
```

Same shape, no session: everything inside the block runs on one connection and commits or rolls back together. See the [Transactions guide](../guide/transactions.md).

## Migrations

Alembic works for both — Ferro ships a bridge that builds a SQLAlchemy `MetaData` from your registered Ferro models, so `alembic revision --autogenerate` keeps working after the switch. Point your `env.py` at it:

```python
# migrations/env.py
import myapp.models  # noqa: F401  — import so all models register

from ferro.migrations import get_metadata

target_metadata = get_metadata()
```

Install the extra with `pip install "ferro-orm[alembic]"`. For development, `connect(url, auto_migrate=True)` creates tables without any migration files. See the [Schema Migrations guide](../guide/migrations.md) and the [Migrations API](../api/migrations.md).

## What Has No Ferro Equivalent Yet

Some SQLAlchemy features have no Ferro counterpart today:

- **Eager loading** (`selectinload` / `joinedload`) — relations load lazily per access; there is no prefetch API yet.
- **Partial column selects** — queries always hydrate full model instances; there is no `select(User.id, User.name)` equivalent.
- **Aggregations beyond `count()` / `exists()`** — no `func.sum`/`avg`/`min`/`max` or `GROUP BY` builder; use [raw SQL](../guide/raw-sql.md) for those.
- **Atomic update expressions** — no `update().values(count=Model.count + 1)`; batch `update()` sets literal values.

For what's planned, see the [Roadmap](../roadmap.md). Where you hit a gap, `execute()` / `fetch_all()` give you full SQL with bound parameters.

## See Also

- [Quickstart Tutorial](../getting-started/quickstart.md) — Ferro end to end in a few minutes
- [Queries guide](../guide/queries.md) — the full query-building API
- [Schema Migrations guide](../guide/migrations.md) — the Alembic bridge in depth
