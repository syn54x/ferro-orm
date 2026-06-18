# Testing

Ferro applications are easy to test: connect to a fresh in-memory SQLite database per test, run your code, and reset the engine on teardown. This page shows the standard pytest setup, a factory pattern for test data, and how to isolate tests against a real PostgreSQL database.

## Test Setup

Put two fixtures in your `conftest.py`:

```python
--8<-- "docs/examples/testing_conftest.py:fixtures"
```

How this works:

- **`db`** connects to a fresh in-memory SQLite database for each test. `auto_migrate=True` creates tables for every registered model, so there is no schema setup to maintain. On teardown, [`reset_engine()`](../api/connection.md) closes the pool and clears the identity map, guaranteeing no state leaks between tests.
- **`db_transaction`** layers a [`transaction()`](../guide/transactions.md) on top. Everything inside the test shares one connection, which gives you connection affinity for the duration of the test. Use it when a test mixes ORM calls with raw SQL that must observe the same uncommitted state.

Since each test gets its own database, most tests only need `db`.

## Configuring pytest-asyncio

Ferro is async, so tests are `async def` functions. With `asyncio_mode = auto`, pytest-asyncio runs them without per-test decorators:

```ini
# pytest.ini
[pytest]
asyncio_mode = auto
testpaths = tests
python_files = test_*.py
python_classes = Test*
python_functions = test_*
```

Or in `pyproject.toml`:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

If you prefer `asyncio_mode = strict`, mark each test with `@pytest.mark.asyncio`.

## Writing Your First Test

Request the `db` fixture and use your models directly:

```python
from myapp.models import User


async def test_create_user(db):
    user = await User.create(username="testuser", email="test@example.com")

    assert user.id is not None
    assert user.username == "testuser"

    # Verify it round-trips through the database
    found = await User.where(lambda t: t.username == "testuser").first()
    assert found is not None
    assert found.id == user.id
```

Constraint violations surface as exceptions from the engine:

```python
import pytest

from myapp.models import User


async def test_user_unique_email(db):
    await User.create(username="user1", email="same@example.com")

    with pytest.raises(Exception):
        await User.create(username="user2", email="same@example.com")
```

## Factories

For tests that need realistic object graphs, a small factory class keeps setup terse without pulling in a library:

```python
from typing import Any

from myapp.models import Post, User


class UserFactory:
    _counter = 0

    @classmethod
    async def create(cls, **kwargs: Any) -> User:
        cls._counter += 1
        defaults = {
            "username": f"user_{cls._counter}",
            "email": f"user{cls._counter}@example.com",
        }
        defaults.update(kwargs)
        return await User.create(**defaults)


class PostFactory:
    _counter = 0

    @classmethod
    async def create(cls, **kwargs: Any) -> Post:
        cls._counter += 1

        # Auto-create an author when none is provided
        if "author" not in kwargs:
            kwargs["author"] = await UserFactory.create()

        defaults = {"title": f"Post {cls._counter}", "content": "Test content"}
        defaults.update(kwargs)
        return await Post.create(**defaults)


async def test_post_with_author(db):
    post = await PostFactory.create(title="Custom Title")
    assert (await post.author) is not None
```

Override only the fields the test cares about; the counters keep unique-constrained fields distinct.

## Testing Against Postgres

SQLite in memory covers most logic, but behavior that depends on native Postgres types, casts, or constraints should run against a real PostgreSQL database.

### Schema isolation with `?ferro_search_path=...`

Appending `ferro_search_path=<schema>` to a Postgres connection URL makes Ferro run `SET search_path TO <schema>` on every pooled connection. All tables created by `auto_migrate` (and all queries) then live in that schema, so many test runs can share one physical database without colliding.

The schema must already exist — create it before connecting, and drop it on teardown:

```python
import uuid

import pytest

from ferro import connect, execute, reset_engine

POSTGRES_URL = "postgresql://localhost:5432/app_test"


@pytest.fixture
async def pg_db():
    schema = f"test_{uuid.uuid4().hex[:8]}"

    # Create the schema with a throwaway connection
    await connect(POSTGRES_URL)
    await execute(f'CREATE SCHEMA "{schema}"')
    reset_engine()

    # Reconnect with the schema as the search path
    await connect(f"{POSTGRES_URL}?ferro_search_path={schema}", auto_migrate=True)
    yield

    await execute(f'DROP SCHEMA "{schema}" CASCADE')
    reset_engine()
```

Schema names passed through `ferro_search_path` must contain only ASCII letters, digits, and underscores; anything else is rejected at connect time.

## See Also

- [Transactions guide](../guide/transactions.md) — semantics of `transaction()` and connection affinity
- [Connections & Databases guide](../guide/connections.md) — connection URLs and `auto_migrate`
- [Connection & Registry API](../api/connection.md) — `connect`, `reset_engine`, and friends
