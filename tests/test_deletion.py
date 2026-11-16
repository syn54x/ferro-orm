import pytest
from typing import Annotated
from ferro import Model, connect, FerroField, engines

pytestmark = pytest.mark.backend_matrix


@pytest.mark.asyncio
async def test_query_delete(db_url):
    """Test that multiple records can be deleted via the query builder."""

    class DeletableUser(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        username: str

    await connect(db_url, auto_migrate=True)
    async with engines.session():
        await DeletableUser(username="keep_1").save()
        await DeletableUser(username="delete_1").save()
        await DeletableUser(username="delete_2").save()
        await DeletableUser(username="keep_2").save()

        # Delete matching
        deleted_count = await DeletableUser.where(
            lambda u: u.username == "delete_1"
        ).delete()
        deleted_count += await DeletableUser.where(
            lambda u: u.username == "delete_2"
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
    async with engines.session():
        user = DeletableUser(username="evict_me")
        await user.save()
        user_id = user.id

        # Ensure it's in IM
        assert await DeletableUser.get(user_id) is user

        # Delete via query
        await DeletableUser.where(lambda u: u.id == user_id).delete()

        # A fresh 'get' should NOT return the old 'user' object (it should be None)
        assert await DeletableUser.get_or_none(user_id) is None
