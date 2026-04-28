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


@pytest.mark.asyncio
async def test_tx_execute_runs_on_tx_connection(db_url):
    """T5.1: tx.execute writes are visible to tx.fetch_one before commit."""
    from ferro import execute, transaction

    await connect(db_url)
    await execute("CREATE TABLE t5 (id INTEGER PRIMARY KEY, n INTEGER)")
    placeholder = "$1" if "postgres" in db_url else "?"

    async with transaction() as tx:
        await tx.execute(f"INSERT INTO t5 (n) VALUES ({placeholder})", 42)
        row = await tx.fetch_one("SELECT n FROM t5 ORDER BY id DESC LIMIT 1")
        assert row is not None and row["n"] == 42


@pytest.mark.asyncio
async def test_tx_handle_after_exit_raises(db_url):
    """T5.2: keeping the tx reference past async-with raises RuntimeError."""
    from ferro import execute, transaction

    await connect(db_url)
    await execute("CREATE TABLE t5b (id INTEGER PRIMARY KEY)")

    captured = None
    async with transaction() as tx:
        captured = tx

    assert captured is not None
    with pytest.raises(RuntimeError, match="closed"):
        await captured.execute("SELECT 1")


@pytest.mark.asyncio
async def test_transaction_yields_transaction_handle(db_url):
    """T5.3: transaction() yields a Transaction instance."""
    from ferro import Transaction, transaction

    await connect(db_url)
    async with transaction() as tx:
        assert isinstance(tx, Transaction)


@pytest.mark.asyncio
async def test_transaction_without_as_clause_still_works(db_url):
    """T5.4: backwards-compatible — bare async-with still works."""
    from ferro import execute, fetch_one, transaction

    await connect(db_url)
    await execute("CREATE TABLE t5d (id INTEGER PRIMARY KEY, n INTEGER)")
    placeholder = "$1" if "postgres" in db_url else "?"

    async with transaction():
        await execute(f"INSERT INTO t5d (n) VALUES ({placeholder})", 1)

    row = await fetch_one("SELECT COUNT(*) AS c FROM t5d")
    assert row is not None and row["c"] == 1


@pytest.mark.asyncio
async def test_fetch_all_returns_list_of_dicts(db_url):
    """T6.1: column names become dict keys; values are primitives."""
    from ferro import execute, fetch_all

    await connect(db_url)
    await execute(
        "CREATE TABLE t6 (id INTEGER PRIMARY KEY, name TEXT, n INTEGER)"
    )
    p1 = "$1" if "postgres" in db_url else "?"
    p2 = "$2" if "postgres" in db_url else "?"
    await execute(f"INSERT INTO t6 (name, n) VALUES ({p1}, {p2})", "alice", 1)
    await execute(f"INSERT INTO t6 (name, n) VALUES ({p1}, {p2})", "bob", 2)

    rows = await fetch_all("SELECT name, n FROM t6 ORDER BY n")
    assert isinstance(rows, list)
    assert len(rows) == 2
    assert rows[0] == {"name": "alice", "n": 1}
    assert rows[1] == {"name": "bob", "n": 2}


@pytest.mark.asyncio
async def test_fetch_one_returns_none_when_empty(db_url):
    """T6.2: fetch_one on no-match query returns None."""
    from ferro import execute, fetch_one

    await connect(db_url)
    await execute("CREATE TABLE t6b (id INTEGER PRIMARY KEY)")

    row = await fetch_one("SELECT id FROM t6b WHERE id = -1")
    assert row is None


@pytest.mark.asyncio
async def test_fetch_one_returns_first_row_when_multiple(db_url):
    """T6.3: fetch_one returns the first row when multiple match."""
    from ferro import execute, fetch_one

    await connect(db_url)
    await execute("CREATE TABLE t6c (id INTEGER PRIMARY KEY, n INTEGER)")
    p = "$1" if "postgres" in db_url else "?"
    await execute(f"INSERT INTO t6c (n) VALUES ({p})", 10)
    await execute(f"INSERT INTO t6c (n) VALUES ({p})", 20)

    row = await fetch_one("SELECT n FROM t6c ORDER BY n")
    assert row == {"n": 10}


@pytest.mark.asyncio
async def test_fetch_inside_tx_sees_uncommitted_writes(db_url):
    """T6.4: read-your-writes within a single transaction."""
    from ferro import execute, transaction

    await connect(db_url)
    await execute("CREATE TABLE t6d (id INTEGER PRIMARY KEY, n INTEGER)")
    p = "$1" if "postgres" in db_url else "?"

    async with transaction() as tx:
        await tx.execute(f"INSERT INTO t6d (n) VALUES ({p})", 7)
        rows = await tx.fetch_all("SELECT n FROM t6d")
        assert rows == [{"n": 7}]
