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


@pytest.mark.asyncio
async def test_marshal_uuid(db_url):
    """T7.1: uuid.UUID binds; on Postgres the SQL casts $N::uuid."""
    import uuid

    from ferro import execute, fetch_one

    await connect(db_url)
    if "postgres" in db_url:
        await execute("CREATE TABLE t7a (id UUID PRIMARY KEY)")
        await execute("INSERT INTO t7a (id) VALUES ($1::uuid)", uuid.UUID(int=1))
        row = await fetch_one("SELECT id::text AS id FROM t7a")
    else:
        await execute("CREATE TABLE t7a (id TEXT PRIMARY KEY)")
        await execute("INSERT INTO t7a (id) VALUES (?)", uuid.UUID(int=1))
        row = await fetch_one("SELECT id FROM t7a")
    assert row is not None
    assert row["id"] == "00000000-0000-0000-0000-000000000001"


@pytest.mark.asyncio
async def test_marshal_datetime(db_url):
    """T7.2: datetime binds via isoformat; Postgres uses ::timestamptz."""
    import datetime

    from ferro import execute, fetch_one

    await connect(db_url)
    dt = datetime.datetime(2026, 4, 27, 20, 13, 45, tzinfo=datetime.timezone.utc)
    if "postgres" in db_url:
        await execute("CREATE TABLE t7b (id INTEGER PRIMARY KEY, ts TIMESTAMPTZ)")
        await execute("INSERT INTO t7b (id, ts) VALUES (1, $1::timestamptz)", dt)
        row = await fetch_one("SELECT ts::text AS ts FROM t7b")
    else:
        await execute("CREATE TABLE t7b (id INTEGER PRIMARY KEY, ts TEXT)")
        await execute("INSERT INTO t7b (id, ts) VALUES (1, ?)", dt)
        row = await fetch_one("SELECT ts FROM t7b")
    assert row is not None
    assert "2026-04-27" in str(row["ts"])


@pytest.mark.asyncio
async def test_marshal_date(db_url):
    """T7.3: date binds via isoformat."""
    import datetime

    from ferro import execute, fetch_one

    await connect(db_url)
    d = datetime.date(2026, 4, 27)
    if "postgres" in db_url:
        await execute("CREATE TABLE t7c (id INTEGER PRIMARY KEY, day DATE)")
        await execute("INSERT INTO t7c (id, day) VALUES (1, $1::date)", d)
    else:
        await execute("CREATE TABLE t7c (id INTEGER PRIMARY KEY, day TEXT)")
        await execute("INSERT INTO t7c (id, day) VALUES (1, ?)", d)
    row = await fetch_one("SELECT day FROM t7c")
    assert row is not None
    assert "2026-04-27" in str(row["day"])


@pytest.mark.asyncio
async def test_marshal_time(db_url):
    """T7.3b: time binds via isoformat."""
    import datetime

    from ferro import execute, fetch_one

    await connect(db_url)
    t = datetime.time(20, 13, 45)
    if "postgres" in db_url:
        await execute("CREATE TABLE t7c2 (id INTEGER PRIMARY KEY, t TIME)")
        await execute("INSERT INTO t7c2 (id, t) VALUES (1, $1::time)", t)
        row = await fetch_one("SELECT t::text AS t FROM t7c2")
    else:
        await execute("CREATE TABLE t7c2 (id INTEGER PRIMARY KEY, t TEXT)")
        await execute("INSERT INTO t7c2 (id, t) VALUES (1, ?)", t)
        row = await fetch_one("SELECT t FROM t7c2")
    assert row is not None
    assert "20:13:45" in str(row["t"])


@pytest.mark.asyncio
async def test_marshal_decimal(db_url):
    """T7.4: Decimal binds as string; Postgres uses ::numeric."""
    import decimal

    from ferro import execute, fetch_one

    await connect(db_url)
    amt = decimal.Decimal("1234.5678")
    if "postgres" in db_url:
        await execute("CREATE TABLE t7d (id INTEGER PRIMARY KEY, amt NUMERIC(10,4))")
        await execute("INSERT INTO t7d (id, amt) VALUES (1, $1::numeric)", amt)
        row = await fetch_one("SELECT amt::text AS amt FROM t7d")
    else:
        await execute("CREATE TABLE t7d (id INTEGER PRIMARY KEY, amt TEXT)")
        await execute("INSERT INTO t7d (id, amt) VALUES (1, ?)", amt)
        row = await fetch_one("SELECT amt FROM t7d")
    assert row is not None
    assert "1234.5678" in str(row["amt"])


@pytest.mark.asyncio
async def test_marshal_enum_str(db_url):
    """T7.5: StrEnum unwraps to .value."""
    import enum

    from ferro import execute, fetch_one

    class Color(str, enum.Enum):
        RED = "red"
        BLUE = "blue"

    await connect(db_url)
    await execute("CREATE TABLE t7e (id INTEGER PRIMARY KEY, c TEXT)")
    p = "$1" if "postgres" in db_url else "?"
    await execute(f"INSERT INTO t7e (id, c) VALUES (1, {p})", Color.RED)
    row = await fetch_one("SELECT c FROM t7e")
    assert row is not None and row["c"] == "red"


@pytest.mark.asyncio
async def test_marshal_enum_int(db_url):
    """T7.6: IntEnum unwraps to .value (int)."""
    import enum

    from ferro import execute, fetch_one

    class Priority(enum.IntEnum):
        LOW = 1
        HIGH = 9

    await connect(db_url)
    await execute("CREATE TABLE t7f (id INTEGER PRIMARY KEY, p INTEGER)")
    p_ph = "$1" if "postgres" in db_url else "?"
    await execute(f"INSERT INTO t7f (id, p) VALUES (1, {p_ph})", Priority.HIGH)
    row = await fetch_one("SELECT p FROM t7f")
    assert row is not None and row["p"] == 9


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_marshal_dict_to_jsonb(db_url):
    """T7.7: dict binds as JSON string with $N::jsonb cast (Postgres-only)."""
    import json

    from ferro import execute, fetch_one

    await connect(db_url)
    await execute("CREATE TABLE t7g (id INTEGER PRIMARY KEY, data JSONB)")
    payload = {"a": 1, "b": [2, 3]}
    await execute("INSERT INTO t7g (id, data) VALUES (1, $1::jsonb)", payload)
    row = await fetch_one("SELECT data::text AS data FROM t7g")
    assert row is not None
    assert json.loads(row["data"]) == payload


@pytest.mark.asyncio
async def test_marshal_bool_before_int_order(db_url):
    """T7.8: True/False bind as bool, not 1/0 — guards the marshal-order trap."""
    from ferro import execute, fetch_all

    await connect(db_url)
    if "postgres" in db_url:
        await execute("CREATE TABLE t7h (id INTEGER PRIMARY KEY, b BOOLEAN)")
        await execute("INSERT INTO t7h (id, b) VALUES (1, $1)", True)
        await execute("INSERT INTO t7h (id, b) VALUES (2, $1)", False)
    else:
        # SQLite stores booleans as integers, but the bind path must still type as bool.
        await execute("CREATE TABLE t7h (id INTEGER PRIMARY KEY, b INTEGER)")
        await execute("INSERT INTO t7h (id, b) VALUES (1, ?)", True)
        await execute("INSERT INTO t7h (id, b) VALUES (2, ?)", False)
    rows = await fetch_all("SELECT id, b FROM t7h ORDER BY id")
    assert len(rows) == 2
    # Truthiness check works on both 1/0 and True/False.
    assert bool(rows[0]["b"]) is True
    assert bool(rows[1]["b"]) is False


@pytest.mark.asyncio
async def test_invalid_sql_raises_runtimeerror_with_db_message(db_url):
    """T8.1: driver error text appears in the surfaced exception."""
    from ferro import execute

    await connect(db_url)
    with pytest.raises(RuntimeError, match="Raw SQL execute failed"):
        await execute("THIS IS NOT VALID SQL")


@pytest.mark.asyncio
async def test_savepoint_rollback_preserves_outer_writes(db_url):
    """T8.2: nested transaction() rolls back only the inner savepoint."""
    from ferro import execute, fetch_all, transaction

    await connect(db_url)
    await execute("CREATE TABLE t8 (id INTEGER PRIMARY KEY, n INTEGER)")
    p = "$1" if "postgres" in db_url else "?"

    async with transaction() as outer:
        await outer.execute(f"INSERT INTO t8 (n) VALUES ({p})", 1)
        try:
            async with transaction() as inner:
                await inner.execute(f"INSERT INTO t8 (n) VALUES ({p})", 2)
                raise RuntimeError("inner-fail")
        except RuntimeError:
            pass
        rows = await outer.fetch_all("SELECT n FROM t8 ORDER BY n")
        assert rows == [{"n": 1}]

    final = await fetch_all("SELECT n FROM t8 ORDER BY n")
    assert final == [{"n": 1}]


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_set_config_then_current_setting_inside_tx(db_url):
    """T9.1: motivating Blueberry/RLS use case from the issue verbatim.

    set_config(..., true) sets a session-local GUC; current_setting reads it back.
    Connection affinity inside transaction() is what makes this work — without
    it, set_config and current_setting would run on different pool connections
    and the value would be invisible.
    """
    from ferro import transaction

    await connect(db_url)
    claims = '{"sub": "user-123", "role": "tenant_admin"}'

    async with transaction() as tx:
        await tx.execute(
            "select set_config('request.jwt.claims', $1, true)",
            claims,
        )
        row = await tx.fetch_one(
            "select current_setting('request.jwt.claims', true) as v"
        )
        assert row is not None
        assert row["v"] == claims
