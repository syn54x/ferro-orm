import pytest
import uuid
from typing import Annotated
from ferro import Model, connect, FerroField, transaction

pytestmark = pytest.mark.backend_matrix


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


@pytest.mark.asyncio
async def test_nested_transaction_rolls_back_with_outer(db_url):
    """Nested transaction blocks should not commit independently of the outer transaction."""

    class TxUser(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        username: str

    await connect(db_url, auto_migrate=True)

    try:
        async with transaction():
            await TxUser.create(username="outer")

            async with transaction():
                await TxUser.create(username="inner")

            raise RuntimeError("abort outer")
    except RuntimeError:
        pass

    assert not await TxUser.where(TxUser.username == "outer").exists()
    assert not await TxUser.where(TxUser.username == "inner").exists()


@pytest.mark.asyncio
async def test_bulk_create_participates_in_transaction(db_url):
    """bulk_create should use the active transaction instead of committing independently."""

    class TxUser(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        username: str

    await connect(db_url, auto_migrate=True)

    rows = [TxUser(username="bulk_a"), TxUser(username="bulk_b")]

    try:
        async with transaction():
            inserted = await TxUser.bulk_create(rows)
            assert inserted == 2
            raise RuntimeError("abort bulk transaction")
    except RuntimeError:
        pass

    assert not await TxUser.where(TxUser.username == "bulk_a").exists()
    assert not await TxUser.where(TxUser.username == "bulk_b").exists()


@pytest.mark.asyncio
async def test_nested_transaction_inner_rollback_allows_outer_commit(db_url):
    """An inner rollback should behave like a savepoint, not a separate transaction."""

    class TxUser(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        username: str

    await connect(db_url, auto_migrate=True)

    async with transaction():
        await TxUser.create(username="outer_before")

        try:
            async with transaction():
                await TxUser.create(username="inner")
                raise ValueError("abort inner")
        except ValueError:
            pass

        await TxUser.create(username="outer_after")

    assert await TxUser.where(TxUser.username == "outer_before").exists()
    assert await TxUser.where(TxUser.username == "outer_after").exists()
    assert not await TxUser.where(TxUser.username == "inner").exists()
