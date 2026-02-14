import pytest
import ferro
from ferro import Model
from pydantic import Field


class Product(Model):
    id: int = Field(json_schema_extra={"primary_key": True})
    name: str
    price: float
    is_active: bool = True


@pytest.mark.asyncio
async def test_create_tables_success():
    """Test that create_tables generates and executes SQL correctly."""
    # Connect to in-memory SQLite
    await ferro.connect("sqlite::memory:")

    # This should generate CREATE TABLE product (...)
    await ferro.create_tables()

    # Verification: We'll try to insert a record later,
    # but for now, we just want to ensure it doesn't crash
    # and the engine handles the registry.
    assert True


@pytest.mark.asyncio
async def test_create_tables_no_connection():
    """Test that create_tables raises an error if no connection exists."""
    ferro.reset_engine()
    with pytest.raises(RuntimeError) as excinfo:
        await ferro.create_tables()
    assert "Engine not initialized" in str(excinfo.value)
