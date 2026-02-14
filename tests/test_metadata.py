import pytest
import uuid
import os
from typing import Annotated
from ferro import Model, connect, FerroField


@pytest.fixture
def db_url():
    db_file = f"test_metadata_{uuid.uuid4()}.db"
    url = f"sqlite:{db_file}?mode=rwc"
    yield url
    if os.path.exists(db_file):
        os.remove(db_file)


@pytest.mark.asyncio
async def test_autoincrement_id_retrieval(db_url):
    """
    Test that saving a model with an autoincrementing PK retrieves the ID back from the DB.
    """

    class AutoUser(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str

    await connect(db_url, auto_migrate=True)

    user = AutoUser(name="taylor")
    assert user.id is None

    await user.save()

    # The ID should have been updated on the instance
    assert user.id is not None
    assert isinstance(user.id, int)
    assert user.id > 0


@pytest.mark.asyncio
async def test_manual_id_no_autoincrement(db_url):
    """
    Test that disabling autoincrement allows manual ID assignment.
    """

    class ManualUser(Model):
        id: Annotated[int, FerroField(primary_key=True, autoincrement=False)]
        name: str

    await connect(db_url, auto_migrate=True)

    user = ManualUser(id=999, name="jeff")
    await user.save()

    assert user.id == 999

    # Verify it actually persisted with that ID
    fetched = await ManualUser.get(999)
    assert fetched is not None
    assert fetched.name == "jeff"


@pytest.mark.asyncio
async def test_string_primary_key(db_url):
    """
    Test using a string as a primary key (implies autoincrement=False).
    """

    class Session(Model):
        token: Annotated[str, FerroField(primary_key=True)]
        user_id: int

    await connect(db_url, auto_migrate=True)

    token = str(uuid.uuid4())
    s = Session(token=token, user_id=1)
    await s.save()

    assert s.token == token
    fetched = await Session.get(token)
    assert fetched is not None
    assert fetched.user_id == 1
