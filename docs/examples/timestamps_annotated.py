"""Annotated-style companion to timestamps.py (AGENTS.md I-7)."""

import asyncio

# --8<-- [start:model]
from datetime import UTC, datetime
from typing import Annotated

from ferro import Field, Model, connect, engines


def utcnow() -> datetime:
    return datetime.now(UTC)


class TimestampMixin:
    """Set ``updated_at`` on every save.

    A plain class, not a ``Model`` subclass: each model that uses it
    declares ``created_at`` and ``updated_at`` itself.
    """

    async def save(self, **kwargs) -> None:
        self.updated_at = utcnow()
        await super().save(**kwargs)


class Note(TimestampMixin, Model):
    id: Annotated[int | None, Field(primary_key=True)] = None
    text: str
    created_at: Annotated[datetime, Field(default_factory=utcnow)]
    updated_at: Annotated[datetime, Field(default_factory=utcnow)]


# --8<-- [end:model]


async def main() -> None:
    # --8<-- [start:session]
    await connect("sqlite::memory:", auto_migrate=True)

    async with engines.session():
        note = await Note.create(text="First draft")
        assert note.created_at.tzinfo is UTC
        # --8<-- [end:session]

        edited_before = note.updated_at
        note.text = "Second draft"
        await note.save()
        assert note.updated_at > edited_before

    print("timestamps_annotated example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
