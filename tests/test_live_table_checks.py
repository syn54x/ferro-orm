"""Integration tests for live CHECK constraint introspection (#342)."""

import pytest

from ferro import connect, engines, execute


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_live_table_checks_ffi_reads_sqlite_inline_named_checks(db_url):
    """Python FFI round-trips planted inline CHECKs on SQLite."""
    from ferro._core import _live_table_checks_for_test

    await connect(db_url)
    async with engines.session():
        await execute(
            "CREATE TABLE transfer ("
            "id INTEGER PRIMARY KEY, "
            "outflow_transaction_id INTEGER, "
            "outflow_activity_id INTEGER, "
            "amount REAL NOT NULL, "
            'CONSTRAINT "ck_transfer_at_most_one_outflow" '
            'CHECK (("outflow_transaction_id" IS NULL) OR ("outflow_activity_id" IS NULL)), '
            'CONSTRAINT "user_positive" CHECK (amount > 0)'
            ")"
        )

        checks = await _live_table_checks_for_test("transfer")
        assert len(checks) == 2

        ferro = next(c for c in checks if c["name"] == "ck_transfer_at_most_one_outflow")
        assert ferro["ferro_owned"] is True
        assert ferro["definition"] == (
            'CHECK (("outflow_transaction_id" IS NULL) OR ("outflow_activity_id" IS NULL))'
        )

        user = next(c for c in checks if c["name"] == "user_positive")
        assert user["ferro_owned"] is False
        assert user["definition"] == "CHECK (amount > 0)"


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_live_table_checks_reads_postgres_ferro_and_user_owned(db_url):
    """Planted ck_* and user-owned CHECKs round-trip name + definition."""
    from ferro._core import _live_table_checks_for_test

    await connect(db_url)
    async with engines.session():
        await execute(
            "CREATE TABLE transfer ("
            "id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY, "
            "outflow_transaction_id INTEGER, "
            "outflow_activity_id INTEGER, "
            "amount DOUBLE PRECISION NOT NULL, "
            'CONSTRAINT "ck_transfer_at_most_one_outflow" '
            'CHECK (("outflow_transaction_id" IS NULL) OR ("outflow_activity_id" IS NULL)), '
            'CONSTRAINT "user_positive" CHECK (amount > 0)'
            ")"
        )

        checks = await _live_table_checks_for_test("transfer")
        assert len(checks) == 2

        ferro = next(c for c in checks if c["name"] == "ck_transfer_at_most_one_outflow")
        assert ferro["ferro_owned"] is True
        assert "CHECK" in ferro["definition"]
        assert "outflow_transaction_id" in ferro["definition"]
        assert "outflow_activity_id" in ferro["definition"]

        user = next(c for c in checks if c["name"] == "user_positive")
        assert user["ferro_owned"] is False
        assert "CHECK" in user["definition"]
        assert "amount" in user["definition"]
