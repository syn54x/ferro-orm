"""The running example of the Queries guide and the typed-predicates page.

The ledger declared in ``docs/examples/predicates.py``, with the rows of
``docs/examples/_ledger_seed.py``, in an in-memory SQLite database. A block
marked ``{test=run prelude=queries}`` runs inside ``opened()`` and can use
``Owner``, ``Account``, ``Category``, ``Transaction``, ``SplitLine``,
``Transfer``, ``Tag``, ``date`` and ``Decimal`` directly.
"""

import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import predicates  # noqa: E402
from _ledger_seed import seed  # noqa: E402
from predicates import (  # noqa: E402,F401
    Account,
    Category,
    Owner,
    SplitLine,
    Tag,
    Transaction,
    Transfer,
    date,
    Decimal,
)

from ferro import connect, engines  # noqa: E402


@asynccontextmanager
async def opened() -> AsyncIterator[None]:
    """Connect, add the ledger's rows and hold a session open around the block."""
    await connect("sqlite::memory:", auto_migrate=True)
    async with engines.session():
        await seed(vars(predicates))
        yield
