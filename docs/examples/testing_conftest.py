"""Example pytest fixtures for the Testing how-to (docs/pages/howto/testing.md).

This file is snippeted into the docs; it is not meant to be executed directly.
"""

# --8<-- [start:fixtures]
import pytest

from ferro import connect, reset_engine, transaction


@pytest.fixture
async def db():
    """Fresh in-memory database per test."""
    await connect("sqlite::memory:", auto_migrate=True)
    yield
    reset_engine()


@pytest.fixture
async def db_transaction(db):
    """Run a test inside a transaction sharing one connection."""
    async with transaction():
        yield
# --8<-- [end:fixtures]


def main() -> None:  # pragma: no cover - import smoke check only
    """No-op: fixtures are exercised by pytest, not by running this file."""


if __name__ == "__main__":
    main()
