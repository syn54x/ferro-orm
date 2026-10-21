"""Runnable companion to the Raw SQL guide (docs/pages/guide/raw-sql.md).

Every block on the page uses the ``event`` table this script declares and
fills; the ``raw_sql`` prelude (``docs/examples/_preludes/raw_sql.py``)
imports both for the page's inline blocks.
"""

import asyncio
from datetime import UTC, datetime
from typing import Annotated

from ferro import Field, Model


# --8<-- [start:models]
class Event(Model):
    id: Annotated[int | None, Field(primary_key=True)] = None
    kind: str
    occurred_at: datetime


# --8<-- [end:models]


async def add_events() -> None:
    """Add the page's three events: two clicks and a signup."""
    await Event.bulk_create(
        [
            Event(kind="click", occurred_at=datetime(2026, 3, 2, 9, 0, tzinfo=UTC)),
            Event(kind="signup", occurred_at=datetime(2026, 3, 2, 9, 5, tzinfo=UTC)),
            Event(kind="click", occurred_at=datetime(2026, 3, 2, 9, 30, tzinfo=UTC)),
        ]
    )


async def main() -> None:
    # --8<-- [start:first]
    from ferro import connect, engines, fetch_all

    await connect("sqlite::memory:", auto_migrate=True)
    async with engines.session():
        await add_events()  # two clicks and a signup

        counts = await fetch_all(
            "SELECT kind, COUNT(*) AS n FROM event GROUP BY kind ORDER BY n DESC"
        )
        print(counts)
        # --8<-- [end:first]
        assert counts == [{"kind": "click", "n": 2}, {"kind": "signup", "n": 1}]

        # --8<-- [start:fetch-one]
        from ferro import fetch_one

        earliest = await fetch_one(
            "SELECT kind, occurred_at FROM event ORDER BY occurred_at LIMIT 1"
        )
        assert earliest == {"kind": "click", "occurred_at": "2026-03-02T09:00:00Z"}

        nothing = await fetch_one("SELECT kind FROM event WHERE kind = ?", "purchase")
        assert nothing is None
        # --8<-- [end:fetch-one]

        # --8<-- [start:execute]
        from ferro import execute

        renamed = await execute(
            "UPDATE event SET kind = ? WHERE kind = ?", "tap", "click"
        )
        assert renamed == 2
        # --8<-- [end:execute]

        # --8<-- [start:in-transaction]
        from ferro import transaction

        async with transaction() as tx:
            await tx.execute("DELETE FROM event WHERE kind = ?", "tap")
            remaining = await tx.fetch_all("SELECT id, kind FROM event")
        assert remaining == [{"id": 2, "kind": "signup"}]
        # --8<-- [end:in-transaction]

    # --8<-- [start:no-session]
    try:
        await fetch_all("SELECT kind FROM event")
    except RuntimeError as error:
        assert str(error).startswith("No database route for this operation.")
    else:
        raise AssertionError("raw SQL outside a session must raise RuntimeError")
    # --8<-- [end:no-session]

    print("raw_sql example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
