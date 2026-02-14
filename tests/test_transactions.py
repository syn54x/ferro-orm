import pytest
import uuid
import os
from typing import Annotated
from ferro import Model, connect, FerroField, transaction


@pytest.fixture
def db_url():
    db_file = f"test_tx_{uuid.uuid4()}.db"
    url = f"sqlite:{db_file}?mode=rwc"
    yield url
    if os.path.exists(db_file):
        os.remove(db_file)


@pytest.mark.asyncio
async def test_transaction_commit(db_url):
    """Test that operations inside a transaction are committed on success."""

    class TxUser(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        username: str

    await connect(db_url, auto_migrate=True)

    async with transaction():
        await TxUser.create(username="alice")
        await TxUser.create(username="bob")

    # Verify both exist
    assert await TxUser.where(TxUser.username == "alice").exists()
    assert await TxUser.where(TxUser.username == "bob").exists()


@pytest.mark.asyncio
async def test_transaction_rollback(db_url):
    """Test that operations inside a transaction are rolled back on exception."""

    class TxUser(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        username: str

    await connect(db_url, auto_migrate=True)

    try:
        async with transaction():
            await TxUser.create(username="charlie")
            raise ValueError("Something went wrong!")
    except ValueError:
        pass

    # Verify charlie DOES NOT exist
    assert not await TxUser.where(TxUser.username == "charlie").exists()


@pytest.mark.asyncio
async def test_transaction_atomicity(db_url):
    """Test that if one operation fails, all are rolled back."""

    class TxUser(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        username: str

    await connect(db_url, auto_migrate=True)

    # Create initial user
    await TxUser.create(username="dave")

    try:
        async with transaction():
            # Update dave
            dave = await TxUser.where(TxUser.username == "dave").first()
            dave.username = "dave_updated"
            await dave.save()

            # Create eve
            await TxUser.create(username="eve")

            # Trigger failure
            raise RuntimeError("Abort!")
    except RuntimeError:
        pass

    # dave should still be "dave", eve should not exist
    from ferro import evict_instance

    evict_instance("TxUser", "1")

    dave_check = await TxUser.where(TxUser.username == "dave").first()
    assert dave_check is not None
    assert dave_check.username == "dave"
    assert not await TxUser.where(TxUser.username == "eve").exists()
