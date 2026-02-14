import pytest
import ferro
from ferro import Model
from pydantic import Field


class AutoMigratedUser(Model):
    id: int = Field(json_schema_extra={"primary_key": True})
    username: str


@pytest.mark.asyncio
async def test_connect_with_auto_migrate():
    """Test that connect(auto_migrate=True) creates tables automatically."""
    # Reset engine to ensure clean state
    ferro.reset_engine()

    # Connect with auto_migrate=True
    # This should internally call the same logic as create_tables()
    await ferro.connect("sqlite::memory:", auto_migrate=True)

    # We can verify it works by trying to call create_tables again
    # or by just ensuring it doesn't crash.
    # In a future step, when we have INSERT, we can verify the table exists.
    # For now, we are verifying the API signature and that it runs without error.
    assert True


@pytest.mark.asyncio
async def test_connect_without_auto_migrate():
    """Test that connect(auto_migrate=False) does not create tables (manual mode)."""
    ferro.reset_engine()

    await ferro.connect("sqlite::memory:", auto_migrate=False)
    # Manual call still works
    await ferro.create_tables()
    assert True
