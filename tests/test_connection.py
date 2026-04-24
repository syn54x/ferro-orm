import pytest

import ferro

pytestmark = pytest.mark.backend_matrix


@pytest.mark.asyncio
async def test_connection_smoke(db_url):
    """Test connecting to the configured backend."""
    await ferro.connect(db_url)


@pytest.mark.asyncio
async def test_invalid_connection_string():
    """Test that invalid connection strings raise the appropriate error."""
    with pytest.raises(Exception) as excinfo:
        await ferro.connect("nonexistent_db://localhost")

    # The error should come from Rust/SQLx
    assert "DB Connection failed" in str(excinfo.value)


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_postgres_connection(db_url):
    """Test connecting to the configured Postgres backend."""
    await ferro.connect(db_url)
