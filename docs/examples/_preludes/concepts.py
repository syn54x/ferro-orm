"""The running example of the Concepts pages.

The ``User`` model declared in ``docs/examples/concepts.py``, with its four
users (ada, grace, alan and edsger, who is inactive), in an in-memory SQLite
database. A block marked ``{test=run prelude=concepts}`` runs inside
``opened()``'s session and can use ``User`` directly.
"""

import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from concepts import User, seed  # noqa: E402,F401

from ferro import connect, engines  # noqa: E402


@asynccontextmanager
async def opened() -> AsyncIterator[None]:
    """Connect, add the four users and hold a session open around the block."""
    await connect("sqlite::memory:", auto_migrate=True)
    await seed()
    async with engines.session():
        yield
