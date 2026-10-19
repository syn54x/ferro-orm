"""The running example of the Upgrade guide.

The ``Account`` model of ``docs/examples/upgrade_account.py`` and four rows, in
an in-memory SQLite database. A block marked ``{test=run prelude=upgrade}`` runs
inside ``opened()`` and can use ``Account``, ``Role``, ``datetime``, ``UTC``,
``ferro`` and ``engines`` directly.

Ada Lovelace (36, admin, last seen 2026-03-01), Grace Hopper (45, never seen),
Alan Turing (41, inactive, last seen 2026-02-01) and Linus Torvalds (28, last
seen 2026-03-05). Every other account is an active member.
"""

import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime  # noqa: F401 - names the blocks use
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from upgrade_account import Account, Role  # noqa: E402,F401

import ferro  # noqa: E402,F401
from ferro import connect, engines  # noqa: E402,F401


@asynccontextmanager
async def opened() -> AsyncIterator[None]:
    """Connect, add the four accounts and hold a session open around the block."""
    await connect("sqlite::memory:", auto_migrate=True)
    async with engines.session():
        await Account.create(
            name="Ada Lovelace",
            email="ada@example.com",
            role=Role.ADMIN,
            age=36,
            seen_at=datetime(2026, 3, 1, 15, 0, tzinfo=UTC),
        )
        await Account.create(name="Grace Hopper", email="grace@example.com", age=45)
        await Account.create(
            name="Alan Turing",
            email="alan@example.com",
            age=41,
            active=False,
            seen_at=datetime(2026, 2, 1, 9, 30, tzinfo=UTC),
        )
        await Account.create(
            name="Linus Torvalds",
            email="linus@example.com",
            age=28,
            seen_at=datetime(2026, 3, 5, 18, 0, tzinfo=UTC),
        )
        yield
