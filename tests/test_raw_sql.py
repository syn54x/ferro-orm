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


@pytest.mark.asyncio
async def test_top_level_execute_outside_tx_creates_table(db_url):
    """T3.1: ferro.execute outside any tx runs on a one-off pool connection."""
    from ferro import execute

    await connect(db_url)
    rows_affected = await execute(
        "CREATE TABLE t3_users (id INTEGER PRIMARY KEY, name TEXT)"
    )
    assert isinstance(rows_affected, int)


@pytest.mark.asyncio
async def test_top_level_execute_returns_rows_affected(db_url):
    """T3.2: execute returns an integer row count for DML."""
    from ferro import execute

    await connect(db_url)
    await execute("CREATE TABLE t3b (id INTEGER PRIMARY KEY, n INTEGER)")
    inserted = await execute("INSERT INTO t3b (n) VALUES (1), (2), (3)")
    assert inserted == 3


@pytest.mark.asyncio
async def test_top_level_execute_binds_scalars(db_url):
    """T3.3: scalar binds round-trip via positional args."""
    from ferro import execute

    await connect(db_url)
    await execute("CREATE TABLE t3c (id INTEGER PRIMARY KEY, name TEXT, n INTEGER)")
    placeholder_n = "$1" if "postgres" in db_url else "?"
    placeholder_name = "$2" if "postgres" in db_url else "?"
    inserted = await execute(
        f"INSERT INTO t3c (n, name) VALUES ({placeholder_n}, {placeholder_name})",
        7,
        "alice",
    )
    assert inserted == 1


@pytest.mark.asyncio
async def test_empty_sql_raises_valueerror(db_url):
    """T3.4: empty / whitespace-only SQL is rejected before the FFI."""
    from ferro import execute

    await connect(db_url)
    with pytest.raises(ValueError, match="non-empty"):
        await execute("")
    with pytest.raises(ValueError, match="non-empty"):
        await execute("   \n\t")


@pytest.mark.asyncio
async def test_unsupported_bind_type_raises_typeerror(db_url):
    """T3.5: passing a pathlib.Path raises TypeError with the type name."""
    import pathlib

    from ferro import execute

    await connect(db_url)
    await execute("CREATE TABLE t3d (id INTEGER PRIMARY KEY, p TEXT)")
    placeholder = "$1" if "postgres" in db_url else "?"
    with pytest.raises(TypeError, match="PosixPath|Path"):
        await execute(
            f"INSERT INTO t3d (p) VALUES ({placeholder})",
            pathlib.Path("/tmp"),
        )


@pytest.mark.asyncio
async def test_top_level_execute_picks_up_active_tx_commit(db_url):
    """T4.1: top-level execute inside transaction() honors the ContextVar."""
    from ferro import execute, fetch_one, transaction

    await connect(db_url)
    await execute("CREATE TABLE t4 (id INTEGER PRIMARY KEY, n INTEGER)")
    placeholder = "$1" if "postgres" in db_url else "?"

    async with transaction():
        await execute(f"INSERT INTO t4 (n) VALUES ({placeholder})", 1)
        await execute(f"INSERT INTO t4 (n) VALUES ({placeholder})", 2)

    row = await fetch_one("SELECT COUNT(*) AS c FROM t4")
    assert row is not None and row["c"] == 2


@pytest.mark.asyncio
async def test_top_level_execute_rolls_back_on_exception(db_url):
    """T4.2: top-level execute writes are rolled back if transaction() raises."""
    from ferro import execute, fetch_one, transaction

    await connect(db_url)
    await execute("CREATE TABLE t4b (id INTEGER PRIMARY KEY, n INTEGER)")
    placeholder = "$1" if "postgres" in db_url else "?"

    with pytest.raises(RuntimeError, match="boom"):
        async with transaction():
            await execute(f"INSERT INTO t4b (n) VALUES ({placeholder})", 99)
            raise RuntimeError("boom")

    row = await fetch_one("SELECT COUNT(*) AS c FROM t4b")
    assert row is not None and row["c"] == 0
