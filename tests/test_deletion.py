import pytest
import uuid
import os
from typing import Annotated
from ferro import Model, connect, FerroField


@pytest.fixture
def db_url():
    db_file = f"test_delete_{uuid.uuid4()}.db"
    url = f"sqlite:{db_file}?mode=rwc"
    yield url
    if os.path.exists(db_file):
        os.remove(db_file)


@pytest.mark.asyncio
async def test_instance_delete(db_url):
    """Test that a specific model instance can be deleted."""

    class DeletableUser(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        username: str

    await connect(db_url, auto_migrate=True)

    user = DeletableUser(username="delete_me")
    await user.save()
    user_id = user.id

    # Verify it exists
    fetched = await DeletableUser.get(user_id)
    assert fetched is not None

    # Delete
    await user.delete()

    # Verify it's gone from DB
    assert await DeletableUser.get(user_id) is None

    # Verify it's gone from Identity Map (fetching again should return None)
    # Note: the 'user' object still exists in Python memory, but it's disconnected from the DB.


@pytest.mark.asyncio
async def test_query_delete(db_url):
    """Test that multiple records can be deleted via the query builder."""

    class DeletableUser(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        username: str

    await connect(db_url, auto_migrate=True)

    await DeletableUser(username="keep_1").save()
    await DeletableUser(username="delete_1").save()
    await DeletableUser(username="delete_2").save()
    await DeletableUser(username="keep_2").save()

    # Delete matching
    deleted_count = await DeletableUser.where(
        DeletableUser.username == "delete_1"
    ).delete()
    deleted_count += await DeletableUser.where(
        DeletableUser.username == "delete_2"
    ).delete()
    assert deleted_count == 2

    # Verify results
    remaining = await DeletableUser.all()
    assert len(remaining) == 2
    assert all("keep" in u.username for u in remaining)


@pytest.mark.asyncio
async def test_delete_evicts_identity_map(db_url):
    """Test that deleting a record via query evicts it from the Identity Map."""

    class DeletableUser(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        username: str

    await connect(db_url, auto_migrate=True)

    user = DeletableUser(username="evict_me")
    await user.save()
    user_id = user.id

    # Ensure it's in IM
    assert await DeletableUser.get(user_id) is user

    # Delete via query
    await DeletableUser.where(DeletableUser.id == user_id).delete()

    # A fresh 'get' should NOT return the old 'user' object (it should be None)
    assert await DeletableUser.get(user_id) is None
