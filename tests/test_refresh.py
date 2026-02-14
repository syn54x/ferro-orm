import pytest
import uuid
import os
from typing import Annotated
from ferro import Model, connect, FerroField


@pytest.fixture
def db_url():
    db_file = f"test_refresh_{uuid.uuid4()}.db"
    url = f"sqlite:{db_file}?mode=rwc"
    yield url
    if os.path.exists(db_file):
        os.remove(db_file)


@pytest.mark.asyncio
async def test_instance_refresh(db_url):
    """Test that .refresh() updates the instance from the database."""

    class RefreshUser(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        username: str
        points: int

    await connect(db_url, auto_migrate=True)

    # 1. Create a user
    user = await RefreshUser.create(username="taylor", points=100)
    assert user.points == 100

    # 2. Update the DB directly (bypassing the 'user' object)
    # We'll use the QueryBuilder to perform a bulk update
    await RefreshUser.where(RefreshUser.id == user.id).update(points=200)

    # The 'user' object still has 100 points
    assert user.points == 100

    # 3. Refresh the user
    await user.refresh()

    # 4. Verify it was updated
    assert user.points == 200


@pytest.mark.asyncio
async def test_refresh_not_found(db_url):
    """Test that .refresh() raises an error if the record is gone."""

    class RefreshUser(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        username: str

    await connect(db_url, auto_migrate=True)

    user = await RefreshUser.create(username="deleted_soon")

    # Delete it from DB
    await RefreshUser.where(RefreshUser.id == user.id).delete()

    with pytest.raises(RuntimeError, match="Instance not found in database"):
        await user.refresh()
