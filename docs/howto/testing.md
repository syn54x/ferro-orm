# How-To: Testing

Test your Ferro applications with pytest and test database isolation strategies.

## Basic Setup

```python
# conftest.py
import pytest
import ferro

@pytest.fixture(scope="session")
async def db():
    """Connect to test database once per session."""
    await ferro.connect("sqlite::memory:", auto_migrate=True)
    yield
    await ferro.disconnect()

@pytest.fixture
async def db_transaction(db):
    """Wrap each test in a transaction that rolls back."""
    from ferro import begin_transaction, rollback_transaction

    tx_id = await begin_transaction()
    try:
        yield
    finally:
        await rollback_transaction(tx_id)
```

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
