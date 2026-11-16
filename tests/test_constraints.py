import pytest
from typing import Annotated
from ferro import Model, UniqueViolationError, connect, FerroField, engines

pytestmark = pytest.mark.backend_matrix


@pytest.mark.asyncio
async def test_unique_constraint(db_url):
    """Test that unique=True correctly enforces database uniqueness."""

    class UniqueUser(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        email: Annotated[str, FerroField(unique=True)]

    await connect(db_url, auto_migrate=True)
    async with engines.session():
        # Save first user
        await UniqueUser(email="test@example.com").save()

        # Attempt to save second user with same email should fail
        with pytest.raises(UniqueViolationError):
            await UniqueUser(email="test@example.com").save()
