import pytest
import uuid
import os
from typing import Annotated
from ferro import Model, connect, FerroField

@pytest.fixture
def db_url():
    db_file = f"test_help_{uuid.uuid4()}.db"
    url = f"sqlite:{db_file}?mode=rwc"
    yield url
    if os.path.exists(db_file):
        os.remove(db_file)

@pytest.mark.asyncio
async def test_create_helper(db_url):
    """Test Model.create() convenience method."""
    class HelperUser(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        username: Annotated[str, FerroField(unique=True)]
        is_active: bool = True

    await connect(db_url, auto_migrate=True)
    
    user = await HelperUser.create(username="taylor")
    assert user.id is not None
    assert user.username == "taylor"
    
    # Verify in DB
    fetched = await HelperUser.get(user.id)
    assert fetched.username == "taylor"

@pytest.mark.asyncio
async def test_exists_helper(db_url):
    """Test Query.exists() convenience method."""
    class HelperUser(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        username: Annotated[str, FerroField(unique=True)]
        is_active: bool = True

    await connect(db_url, auto_migrate=True)
    
    await HelperUser.create(username="exists_check")
    
    assert await HelperUser.where(HelperUser.username == "exists_check").exists() is True
    assert await HelperUser.where(HelperUser.username == "does_not_exist").exists() is False

@pytest.mark.asyncio
async def test_bulk_create_helper(db_url):
    """Test Model.bulk_create() for efficient batch inserts."""
    class HelperUser(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        username: Annotated[str, FerroField(unique=True)]
        is_active: bool = True

    await connect(db_url, auto_migrate=True)
    
    users = [
        HelperUser(username="user1"),
        HelperUser(username="user2"),
        HelperUser(username="user3"),
    ]
    
    count = await HelperUser.bulk_create(users)
    assert count == 3
    
    all_users = await HelperUser.all()
    assert len(all_users) == 3
    assert {u.username for u in all_users} == {"user1", "user2", "user3"}

@pytest.mark.asyncio
async def test_get_or_create(db_url):
    """Test Model.get_or_create() behavior."""
    class HelperUser(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        username: Annotated[str, FerroField(unique=True)]
        is_active: bool = True

    await connect(db_url, auto_migrate=True)
    
    # 1. Create case
    user1, created = await HelperUser.get_or_create(username="new_user", defaults={"is_active": False})
    assert created is True
    assert user1.username == "new_user"
    assert user1.is_active is False
    
    # 2. Get case
    user2, created = await HelperUser.get_or_create(username="new_user")
    assert created is False
    assert user2.id == user1.id
    assert user2 is user1 # Identity Map should return same object

@pytest.mark.asyncio
async def test_update_or_create(db_url):
    """Test Model.update_or_create() behavior."""
    class HelperUser(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        username: Annotated[str, FerroField(unique=True)]
        is_active: bool = True

    await connect(db_url, auto_migrate=True)
    
    # 1. Create case
    user1, created = await HelperUser.update_or_create(username="up_user", defaults={"is_active": True})
    assert created is True
    assert user1.is_active is True
    
    # 2. Update case
    user2, created = await HelperUser.update_or_create(username="up_user", defaults={"is_active": False})
    assert created is False
    assert user2.id == user1.id
    assert user2.is_active is False
    
    # Verify DB
    fetched = await HelperUser.get(user1.id)
    assert fetched.is_active is False
