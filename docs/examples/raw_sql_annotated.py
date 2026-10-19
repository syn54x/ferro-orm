"""Annotated-style companion to raw_sql.py (AGENTS.md I-7).

Declares the same ``Event`` model with its field options inside
``Annotated[...]`` and checks it creates the same ``event`` table.
"""

import asyncio
from datetime import UTC, datetime
from typing import Annotated

from ferro import Field, Model, connect, engines, fetch_all


# --8<-- [start:models]
class Event(Model):
    id: Annotated[int | None, Field(primary_key=True)] = None
    kind: str
    occurred_at: datetime


# --8<-- [end:models]


async def main() -> None:
    await connect("sqlite::memory:", auto_migrate=True)
    async with engines.session():
        await Event.create(
            kind="click", occurred_at=datetime(2026, 3, 2, 9, 0, tzinfo=UTC)
        )
        rows = await fetch_all("SELECT id, kind, occurred_at FROM event")
        assert rows == [
            {"id": 1, "kind": "click", "occurred_at": "2026-03-02T09:00:00Z"}
        ]

    print("raw_sql_annotated example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
