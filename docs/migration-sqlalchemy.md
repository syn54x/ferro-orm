# Migrating from SQLAlchemy

This guide helps you migrate from SQLAlchemy to Ferro.

## Quick Comparison

| Feature | SQLAlchemy 2.0 | Ferro |
|---------|----------------|-------|
| Model Definition | Declarative Base | Pydantic Model |
| Queries | `select()` | `.where()` |
| Sessions | Required | Not needed |
| Async | Native | Native |
| Migrations | Alembic | Alembic |

## Model Definition

### SQLAlchemy

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

### Ferro

```python
from typing import Annotated
from ferro import Model, FerroField

class User(Model):
    id: Annotated[int, FerroField(primary_key=True)]
    username: Annotated[str, FerroField(unique=True)]
    email: str
```

## Queries

### Fetch All

```python
# SQLAlchemy
from sqlalchemy import select

async with session() as db:
    result = await db.execute(select(User))
    users = result.scalars().all()

# Ferro
users = await User.all()
```

### Filtering

```python
# SQLAlchemy
stmt = select(User).where(User.age >= 18)
result = await db.execute(stmt)
users = result.scalars().all()

# Ferro
users = await User.where(User.age >= 18).all()
```

## Relationships

### One-to-Many

```python
# SQLAlchemy
class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    posts: Mapped[List["Post"]] = relationship(back_populates="author")

class Post(Base):
    __tablename__ = "posts"
    id: Mapped[int] = mapped_column(primary_key=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    author: Mapped["User"] = relationship(back_populates="posts")

# Ferro
class User(Model):
    id: Annotated[int, FerroField(primary_key=True)]
    posts: BackRef[list["Post"]] = None

class Post(Model):
    id: Annotated[int, FerroField(primary_key=True)]
    author: Annotated[User, ForeignKey(related_name="posts")]
```

## Creating Records

```python
# SQLAlchemy
async with session() as db:
    user = User(username="alice", email="alice@example.com")
    db.add(user)
    await db.commit()

# Ferro
user = await User.create(username="alice", email="alice@example.com")
```

## Transactions

```python
# SQLAlchemy
async with session.begin():
    user = User(username="alice")
    db.add(user)
    # Auto-commits on exit

# Ferro
from ferro import transaction

async with transaction():
    user = await User.create(username="alice")
    # Auto-commits on exit
```

## Migration Checklist

- [ ] Install Ferro: `pip install ferro-orm`
- [ ] Replace SQLAlchemy models with Ferro models
- [ ] Update queries to use Ferro's `.where()` API
- [ ] Remove session management (Ferro doesn't use sessions)
- [ ] Update relationship syntax
- [ ] Test thoroughly
- [ ] Update Alembic `env.py` to use Ferro's `get_metadata()`

## Key Differences

1. **No Sessions**: Ferro manages connections automatically
2. **Pydantic Models**: Ferro models are Pydantic, get validation for free
3. **Simpler API**: Fewer concepts to learn
4. **Better Performance**: Rust engine for bulk operations

## Getting Help

- [Ferro Documentation](index.md)
- [GitHub Discussions](https://github.com/syn54x/ferro-orm/discussions)
