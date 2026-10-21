"""The running example of the Raw SQL guide.

The ``Event`` model declared in ``docs/examples/raw_sql.py``, with its three
events, in an in-memory SQLite database. A block marked
``{test=run prelude=raw_sql}`` runs inside ``opened()`` and can use
``Event``, ``execute``, ``fetch_all``, ``fetch_one`` and ``transaction``
directly.
"""

import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from raw_sql import Event, add_events  # noqa: E402,F401

from ferro import (  # noqa: E402,F401
    connect,
    engines,
    execute,
    fetch_all,
    fetch_one,
    transaction,
)


@asynccontextmanager
async def opened() -> AsyncIterator[None]:
    """Connect, add the three events and hold a session open around the block."""
    await connect("sqlite::memory:", auto_migrate=True)
    async with engines.session():
        await add_events()
        yield
