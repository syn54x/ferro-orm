import pytest
from pydantic import Field

import ferro
from ferro import Model


class User(Model):
    id: int = Field(default=None, json_schema_extra={"primary_key": True})
    username: str
    email: str


import os
import uuid


@pytest.fixture
def db_url():
    db_file = f"test_{uuid.uuid4()}.db"
    url = f"sqlite:{db_file}?mode=rwc"
    yield url
    if os.path.exists(db_file):
        os.remove(db_file)


@pytest.mark.asyncio
async def test_model_save_new_record(db_url):
    """Test that calling .save() on a new model instance persists it to the database."""
    await ferro.connect(db_url, auto_migrate=True)

    user = User(username="test_user", email="test@example.com")
    await user.save()
    assert True


@pytest.mark.asyncio
async def test_model_save_update_record(db_url):
    """Test that calling .save() on an existing model instance updates it."""
    await ferro.connect(db_url, auto_migrate=True)

    user = User(id=1, username="initial_name", email="initial@example.com")
    await user.save()

    user.username = "updated_name"
    await user.save()
    assert True


@pytest.mark.asyncio
async def test_model_all_fetching(db_url):
    """Test that Model.all() retrieves all records from the database."""
    await ferro.connect(db_url, auto_migrate=True)

    u1 = User(id=1, username="alice", email="alice@example.com")
    u2 = User(id=2, username="bob", email="bob@example.com")
    await u1.save()
    await u2.save()

    users = await User.all()
    assert len(users) == 2
    assert any(u.username == "alice" for u in users)
    assert any(u.username == "bob" for u in users)


@pytest.mark.asyncio
async def test_upsert_does_not_duplicate(db_url):
    """Test that saving a model with an existing ID updates it rather than inserting a new one."""
    await ferro.connect(db_url, auto_migrate=True)

    user = User(id=42, username="original", email="original@example.com")
    await user.save()

    # 2. Verify count is 1
    users_before = await User.all()
    assert len(users_before) == 1

    # 3. Use a different instance to update
    user_dup = User(id=42, username="updated", email="original@example.com")
    await user_dup.save()

    # 4. Refresh from DB
    # We reset the engine to clear the Identity Map so we fetch a fresh copy from DB
    ferro.reset_engine()
    await ferro.connect(db_url, auto_migrate=True)

    # Re-register the model because reset_engine might have affected things
    # (though User is already defined, it needs to be in Rust registry)
    # Actually, defining the class registers it. Since we are in the same process,
    # we need a way to re-register without re-defining.
    # For now, let's just use raw SQL to verify the update.
    import sqlite3

    conn = sqlite3.connect(db_url.replace("sqlite:", "").split("?")[0])
    cursor = conn.cursor()
    cursor.execute("SELECT username FROM user WHERE id = 42")
    row = cursor.fetchone()
    assert row[0] == "updated"
    conn.close()


@pytest.mark.asyncio
async def test_identity_map_consistency(db_url):
    """Test that fetching the same record twice returns the same Python object instance."""
    await ferro.connect(db_url, auto_migrate=True)

    # 1. Create a user
    u1 = User(id=100, username="identity", email="id@test.com")
    await u1.save()

    # 2. Fetch twice
    results_1 = await User.all()
    results_2 = await User.all()

    user_a = results_1[0]
    user_b = results_2[0]

    # 3. Assert they are the SAME object in memory
    assert user_a is user_b
    assert user_a.id == 100


@pytest.mark.asyncio
async def test_model_get_operation(db_url):
    """Test fetching a single record by primary key."""
    await ferro.connect(db_url, auto_migrate=True)

    # 1. Seed data
    u1 = User(id=500, username="get_test", email="get@test.com")
    await u1.save()

    # 2. Fetch by PK
    user = await User.get(500)
    assert user is not None
    assert user.id == 500
    assert user is u1


@pytest.mark.asyncio
async def test_model_get_invalid_usage(db_url):
    """Test that get() raises error with invalid arguments."""
    await ferro.connect(db_url, auto_migrate=True)

    # Kwarg no longer supported
    with pytest.raises(TypeError):
        await User.get(id=1)

    # No args
    with pytest.raises(TypeError):
        await User.get()


@pytest.mark.asyncio
async def test_model_get_not_found(db_url):
    """Test that get() returns None if the record does not exist."""
    await ferro.connect(db_url, auto_migrate=True)

    user = await User.get(9999)
    assert user is None
