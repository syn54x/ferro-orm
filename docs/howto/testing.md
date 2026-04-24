# How-To: Testing

Test your Ferro applications with pytest and test database isolation strategies.

## Ferro Test Matrix

The repository test suite supports two database modes:

- **Default SQLite run** for the full fast suite
- **Dual-backend matrix** for ORM coverage on both SQLite and PostgreSQL/Supabase

The matrix is opt-in so day-to-day test runs stay quick and deterministic.

### Local Setup

Install the development dependencies used by the matrix:

```bash
uv sync --group dev
uv run maturin develop
```

Set `FERRO_SUPABASE_URL` to a PostgreSQL connection string. A root `.env` file works well for local development:

```bash
FERRO_SUPABASE_URL='postgresql://...'
```

The Postgres matrix reads `FERRO_SUPABASE_URL` from either the environment or the project `.env` file. Tests create a dedicated schema per test and use that schema as the search path so one shared Supabase database can still run isolated tests safely.

### Run The Default Suite

Run the normal SQLite-first suite:

```bash
uv run pytest -q
```

### Run The Dual-Backend ORM Matrix

Run the backend-matrix and Postgres-specific tests on both SQLite and PostgreSQL:

```bash
uv run pytest -m "backend_matrix or postgres_only" --db-backends=sqlite,postgres -q
```

If you only want the PostgreSQL side of the matrix:

```bash
uv run pytest -m "backend_matrix or postgres_only" --db-backends=postgres -q
```

### Test Markers

The repository uses three database markers:

- `backend_matrix`: run this test once per selected backend
- `sqlite_only`: keep SQLite-specific catalog, file-path, or pragma assertions on SQLite
- `postgres_only`: run Postgres/Supabase-specific assertions only when `FERRO_SUPABASE_URL` is configured

If `FERRO_SUPABASE_URL` is not set, `postgres_only` tests are skipped and `backend_matrix` tests run only on SQLite.

## Basic Setup

```python
# conftest.py
import pytest
import ferro

@pytest.fixture
async def db():
    """Connect to a fresh test database for one test."""
    await ferro.connect("sqlite::memory:", auto_migrate=True)
    yield
    ferro.reset_engine()

@pytest.fixture
async def db_transaction(db):
    """Wrap each test in Ferro's transaction() helper."""
    from ferro import transaction

    async with transaction():
        yield
```

For backend-matrix tests, Ferro's own suite uses `--db-backends=sqlite,postgres` together with `backend_matrix` / `postgres_only` markers and a `FERRO_SUPABASE_URL` environment variable.

## Test Example

```python
# test_users.py
import pytest
from myapp.models import User

@pytest.mark.asyncio
async def test_create_user(db_transaction):
    """Test user creation."""
    user = await User.create(
        username="testuser",
        email="test@example.com"
    )

    assert user.id is not None
    assert user.username == "testuser"

    # Verify in database
    found = await User.where(User.username == "testuser").first()
    assert found is not None
    assert found.id == user.id

@pytest.mark.asyncio
async def test_user_unique_email(db_transaction):
    """Test unique email constraint."""
    await User.create(username="user1", email="same@example.com")

    # Use general Exception or your database driver's specific exception
    with pytest.raises(Exception):  # Or use specific exception from driver
        await User.create(username="user2", email="same@example.com")
```

## Factory Pattern

```python
# factories.py
from typing import Any
from myapp.models import User, Post

class UserFactory:
    _counter = 0

    @classmethod
    async def create(cls, **kwargs: Any) -> User:
        cls._counter += 1
        defaults = {
            "username": f"user_{cls._counter}",
            "email": f"user{cls._counter}@example.com"
        }
        defaults.update(kwargs)
        return await User.create(**defaults)

class PostFactory:
    _counter = 0

    @classmethod
    async def create(cls, **kwargs: Any) -> Post:
        cls._counter += 1

        # Auto-create author if not provided
        if "author" not in kwargs:
            kwargs["author"] = await UserFactory.create()

        defaults = {
            "title": f"Post {cls._counter}",
            "content": "Test content"
        }
        defaults.update(kwargs)
        return await Post.create(**defaults)

# Usage in tests
async def test_post_with_author(db_transaction):
    post = await PostFactory.create(title="Custom Title")
    assert post.author is not None
```

## Pytest-AsyncIO Configuration

```ini
# pytest.ini
[pytest]
asyncio_mode = auto
testpaths = tests
python_files = test_*.py
python_classes = Test*
python_functions = test_*
```

## See Also

- [Transactions](../guide/transactions.md)
- [Database Setup](../guide/database.md)
