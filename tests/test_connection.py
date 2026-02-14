import pytest

import ferro


@pytest.mark.asyncio
async def test_sqlite_memory_connection():
    """Test connecting to an in-memory SQLite database."""
    # This should succeed and print the success message from Rust
    await ferro.connect("sqlite::memory:")


@pytest.mark.asyncio
async def test_invalid_connection_string():
    """Test that invalid connection strings raise the appropriate error."""
    with pytest.raises(Exception) as excinfo:
        await ferro.connect("nonexistent_db://localhost")

    # The error should come from Rust/SQLx
    assert "DB Connection failed" in str(excinfo.value)


@pytest.mark.skip(reason="Requires a running Postgres instance")
@pytest.mark.asyncio
async def test_postgres_connection():
    """Placeholder for postgres testing."""
    await ferro.connect("postgres://user:pass@localhost/db")
