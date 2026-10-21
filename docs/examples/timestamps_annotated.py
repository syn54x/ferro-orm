"""Runnable companion to the timestamps how-to (docs/pages/howto/timestamps.md).

Besides the recipe, it pins on SQLite that a batch ``update()`` that writes
``utcnow()`` stores the same text ``save()`` stores, so rows touched either
way sort together by ``updated_at`` and read back as aware datetimes
(AGENTS.md I-13).
"""

import asyncio
import re

from ferro import fetch_all

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
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


# --8<-- [end:model]


async def main() -> None:
    # --8<-- [start:session]
    await connect("sqlite::memory:", auto_migrate=True)

    async with engines.session():
        note = await Note.create(text="First draft")
        assert note.created_at.tzinfo is UTC
        # --8<-- [end:session]

        # --8<-- [start:usage]
        created = note.created_at
        edited_before = note.updated_at

        note.text = "Second draft"
        await note.save()

        assert note.created_at == created
        assert note.updated_at > edited_before
        # --8<-- [end:usage]

        # --8<-- [start:batch]
        archived = await Note.create(text="Old idea")
        await Note.create(text="Shopping list")

        touched = await Note.where(lambda note: note.text == "Old idea").update(
            text="Old idea (archived)", updated_at=utcnow()
        )
        assert touched == 1

        latest = (
            await Note.select().order_by(lambda note: note.updated_at, "desc").first()
        )
        assert latest.text == "Old idea (archived)"
        assert latest.updated_at.tzinfo is not None
        # --8<-- [end:batch]

        # SQLite stores datetimes as text. The batch update wrote the same
        # text shape save() writes (I-13), so the rows sort by time, and
        # every value reads back aware, comparable with the mixin's clock.
        stored = await fetch_all('SELECT text, updated_at FROM "note"')
        shapes = {re.sub(r"\d", "9", row["updated_at"]) for row in stored}
        assert len(shapes) == 1, shapes
        rows = await Note.select().order_by(lambda note: note.updated_at).all()
        assert [row.text for row in rows] == [
            "Second draft",
            "Shopping list",
            "Old idea (archived)",
        ]
        assert archived.id == rows[-1].id
        assert all(row.updated_at <= utcnow() for row in rows)

    print("timestamps example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
