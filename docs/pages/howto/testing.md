---
description: "Set up pytest so each test gets a fresh in-memory database with a session open, plus factories and a PostgreSQL fixture."
---

# How to test a Ferro app with pytest

This page sets up pytest so that each test gets a fresh in-memory SQLite database with a session open, then shows a first test, factories for test data, and a fixture for tests against PostgreSQL.

## The `db` fixture

Put two fixtures in `tests/conftest.py`:

```python
--8<-- "docs/examples/testing_conftest.py:fixtures"
```

`db` connects to a new in-memory SQLite database and holds an [`engines.session()`](../guide/connections.md#sessions) open while the test runs, so the test's ORM calls need no session of their own. `auto_migrate=True` creates a table for every model your code has imported, so there is no schema setup to keep up to date. On teardown, [`reset_engine()`][ferro.reset_engine] closes the connection and clears the identity map, so no rows or cached instances carry over to the next test.

`db_transaction` runs the test inside one [`transaction()`](../guide/transactions.md) and yields its handle. The handle's `fetch_one()`, `fetch_all()` and `execute()` run raw SQL on the test's own connection, so they see what the test's ORM calls wrote before anything is committed. When the test ends, the fixture raises `RollBack` inside the transaction, which rolls back every write. Each test already has a database of its own, so most tests need only `db`.

## Configuring pytest-asyncio

Ferro is async, so tests are `async def` functions, run by [pytest-asyncio](https://pytest-asyncio.readthedocs.io/). Set its mode to `auto` in `pyproject.toml`, and it runs every async test and fixture without a decorator:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

`pytest.ini` takes the same `asyncio_mode = auto` line under `[pytest]`. In `strict` mode, decorate each async fixture with `@pytest_asyncio.fixture` instead of `@pytest.fixture`, and mark each test with `@pytest.mark.asyncio`.

## The models

The tests on this page use two models, kept in `myapp/models.py`:

=== "Assignment"

    ```python
    --8<-- "docs/examples/testing_models.py:models"
    ```

=== "Annotated"

    ```python
    --8<-- "docs/examples/testing_models_annotated.py:models"
    ```

`username` and `email` are unique, which the tests below rely on.

## A first test

Request the `db` fixture and use the models directly. A second insert of the same email raises [`UniqueViolationError`][ferro.UniqueViolationError], which `pytest.raises` expects:

```python {test=pytest path="tests/test_users.py"}
import pytest

from ferro import UniqueViolationError
from myapp.models import User


async def test_create_user(db):
    user = await User.create(username="ada", email="ada@example.com")

    found = await User.where(lambda user: user.username == "ada").first()
    assert found is not None
    assert found.id == user.id


async def test_email_is_unique(db):
    await User.create(username="ada", email="ada@example.com")

    with pytest.raises(UniqueViolationError):
        await User.create(username="grace", email="ada@example.com")
```

Run the file:

<!-- shows: a shell session running pytest -->
```text
$ pytest -q tests/test_users.py
..                                                                       [100%]
2 passed in 0.04s
```

Both tests create `ada@example.com`, and neither sees the other's row: each starts with an empty database.

## Raw SQL in the test's transaction

Request `db_transaction` instead of `db` when a test checks its work with raw SQL. The handle it yields reads on the test's connection:

```python {test=pytest path="tests/test_raw_sql.py"}
from myapp.models import User


async def test_raw_count_sees_new_user(db_transaction):
    await User.create(username="ada", email="ada@example.com")

    row = await db_transaction.fetch_one('SELECT count(*) AS total FROM "user"')
    assert row == {"total": 1}
```

The raw count sees the new user inside the transaction, and the fixture rolls the row back when the test ends.

## Factories

A test that needs a post also needs its author. A factory class per model fills in the fields a test does not care about:

```python {test=pytest path="tests/test_posts.py"}
from typing import Any

from myapp.models import Post, User


class UserFactory:
    count = 0

    @classmethod
    async def create(cls, **fields: Any) -> User:
        cls.count += 1
        defaults = {
            "username": f"user{cls.count}",
            "email": f"user{cls.count}@example.com",
        }
        return await User.create(**(defaults | fields))


class PostFactory:
    count = 0

    @classmethod
    async def create(cls, **fields: Any) -> Post:
        cls.count += 1
        if "author" not in fields:
            fields["author"] = await UserFactory.create()
        defaults = {"title": f"Post {cls.count}", "content": "Test content"}
        return await Post.create(**(defaults | fields))


async def test_post_gets_an_author(db):
    post = await PostFactory.create(title="Custom title")

    author = await post.author
    assert post.title == "Custom title"
    assert author.username.startswith("user")
```

The post keeps the title the test passed and gets a generated author. The counters keep the unique `username` and `email` values distinct across every call in the run.

## Testing against Postgres

In-memory SQLite covers most logic. Behaviour that depends on Postgres types, casts or constraints needs a real PostgreSQL database. This fixture gives each test a schema of its own in one shared database, through the URL's [`ferro_search_path`](../guide/connections.md#postgres-schema-isolation) parameter:

```python
--8<-- "docs/examples/testing_conftest_postgres.py:postgres"
```

`pg_db` creates the schema on a first connection, reconnects with `ferro_search_path` set to it so that `auto_migrate=True` creates the tables there, and drops the schema with everything in it when the test ends. `with_search_path()` adds the parameter beside any the URL already has, such as `sslmode=require`. Set `TEST_POSTGRES_URL` to point the fixture at your test database.

## Projects with migrations

The fixtures above stay as they are when a project adopts [migrations](../guide/schema/migrations.md). A fresh test database has never run a migration, so `auto_migrate=True` is not refused on it ([one door per database](../guide/schema/overview.md#one-door-per-database)), and it builds the schema the models declare.

That schema is the schema your migrations end at only while the models and the migrations agree, so run `ferro migrate check` in CI beside the tests. `auto_migrate=True` never runs a migration's own SQL, backfill or down step. Test those with the migration test harness, which stands a database at a named migration; see [Testing migrations](../guide/schema/testing.md).

## See also

- [Transactions](../guide/transactions.md): what `transaction()` commits and rolls back.
- [Connections](../guide/connections.md): connection URLs, sessions and `auto_migrate`.
- [`connect()`][ferro.connect], [`reset_engine()`][ferro.reset_engine] and [`transaction()`][ferro.transaction] in the API reference.
