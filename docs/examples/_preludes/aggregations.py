"""The running example of the Aggregations guide.

The ledger declared in ``docs/examples/aggregations.py``, with its four
transactions, in an in-memory SQLite database. A block marked
``{test=run prelude=aggregations}`` runs inside ``opened()`` and can use
``Account``, ``Transaction`` and ``Decimal`` directly.
"""

import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aggregations import Account, Decimal, Transaction, seed  # noqa: E402,F401

from ferro import connect, engines  # noqa: E402


@asynccontextmanager
async def opened() -> AsyncIterator[None]:
    """Connect, add the ledger's rows and hold a session open around the block."""
    await connect("sqlite::memory:", auto_migrate=True)
    async with engines.session():
        await seed()
        yield
