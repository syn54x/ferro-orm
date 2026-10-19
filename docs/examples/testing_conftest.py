"""The pytest fixtures of the testing how-to (docs/pages/howto/testing.md).

Readers copy this file to ``tests/conftest.py``.
``tests/test_docs_testing_fixtures.py`` does the same in a scratch project
and runs the page's tests with it, so every fixture here runs under pytest.
"""

# --8<-- [start:fixtures]
import pytest

from ferro import connect, engines, reset_engine, transaction


class RollBack(Exception):
    """Raised at the end of a test to roll its transaction back."""


@pytest.fixture
async def db():
    """A fresh in-memory database, with a session open around the test."""
    await connect("sqlite::memory:", auto_migrate=True)
    async with engines.session():
        yield
    reset_engine()


@pytest.fixture
async def db_transaction(db):
    """The test runs in one transaction, which rolls back when it ends."""
    try:
        async with transaction() as tx:
            yield tx
            raise RollBack
    except RollBack:
        pass


# --8<-- [end:fixtures]
