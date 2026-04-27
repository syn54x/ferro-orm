# How-To: Testing

Test your Ferro applications with pytest and test database isolation strategies.

## Ferro Test Matrix

The repository test suite supports two database modes:

- **Default SQLite run** for the full fast suite
- **Dual-backend matrix** for ORM coverage on both SQLite and PostgreSQL

The matrix is opt-in so day-to-day test runs stay quick and deterministic.

### Local Setup

Install the development dependencies used by the matrix:

```bash
uv sync --group dev
uv run maturin develop
```

For local PostgreSQL matrix runs, install PostgreSQL server binaries so `pytest-postgresql` can start an ephemeral database:

```bash
brew install postgresql@16
```

You can also point the suite at an externally managed PostgreSQL database. A root `.env` file works well for local development:

```bash
FERRO_POSTGRES_URL='postgresql://...'
```

The Postgres matrix first reads `FERRO_POSTGRES_URL` from either the environment or the project `.env` file. It still accepts the older `FERRO_SUPABASE_URL` name as a compatibility fallback. Tests create a dedicated schema per test and use that schema as the search path so one shared external database can still run isolated tests safely.

To force the local `pytest-postgresql` provider even when `.env` contains an external URL:

```bash
FERRO_POSTGRES_PROVIDER=local uv run pytest -m "backend_matrix or postgres_only" --db-backends=postgres -q
```

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
- `postgres_only`: run Postgres-specific assertions when either an external Postgres URL is configured or `pytest-postgresql` can start a local server

If no external Postgres URL is set and local PostgreSQL server binaries are unavailable, `postgres_only` tests are skipped and `backend_matrix` tests run only on SQLite.

### Bridge-Boundary Regressions

When a bug involves values crossing the Python/Rust bridge, preserve the public API shape in the regression test. These issues often depend on whether a value travels as JSON (`Query.all()`, `Query.count()`, `Query.update()`, `Query.delete()`) or as a typed Python value passed directly to Rust (`ManyToMany(...).add()`, `.remove()`, `.clear()`).

Use these conventions:

- Put relationship and auto-migration regressions in `tests/test_auto_migrate.py` when they strengthen the backend matrix.
- Put structural type regressions in `tests/test_structural_types.py` when they involve UUID, Decimal, JSON, enum, binary, date, or datetime behavior.
- Use `backend_matrix` when the public behavior should work on both SQLite and PostgreSQL.
- Use `postgres_only` when the assertion depends on native PostgreSQL types, catalogs, or casts.
- Convert user repro scripts with minimal translation: keep the same model shape and public method sequence, trim incidental setup, and assert the original failure mode is gone.
- Add a fast serializer or static-contract test when the bug is caused by a Python boundary rule, such as raw `json.dumps(query_def)` bypassing Ferro's query serializer.

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

For backend-matrix tests, Ferro's own suite uses `--db-backends=sqlite,postgres` together with `backend_matrix` / `postgres_only` markers. Postgres coverage uses `pytest-postgresql` locally, or `FERRO_POSTGRES_URL` / `FERRO_SUPABASE_URL` when an external database is configured.

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
