"""Integration tests for raw SQL execute / fetch_all / fetch_one."""

import pytest

from ferro import connect

pytestmark = pytest.mark.backend_matrix


@pytest.mark.asyncio
async def test_raw_execute_ffi_smoke(db_url):
    """Task 2 smoke: ferro._core.raw_execute is callable and creates a table."""
    from ferro._core import raw_execute

    await connect(db_url)
    rows_affected = await raw_execute(
        "CREATE TABLE raw_smoke (id INTEGER PRIMARY KEY)", [], None
    )
    assert isinstance(rows_affected, int)
