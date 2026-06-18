"""Runnable companion to the Timestamps how-to (docs/pages/howto/timestamps.md)."""

import asyncio

# --8<-- [start:model]
from datetime import UTC, datetime

from ferro import Field, Model, connect


def utcnow() -> datetime:
    return datetime.now(UTC)


class TimestampMixin:
    """Touch ``updated_at`` on every save.

    A plain mixin (not a Model subclass): declare the timestamp fields on
    each concrete model, and the mixin keeps them fresh.
    """

    async def save(self, **kwargs) -> None:
        self.updated_at = utcnow()
        await super().save(**kwargs)


class Note(TimestampMixin, Model):
    id: int | None = Field(default=None, primary_key=True)
    text: str
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
# --8<-- [end:model]


async def main() -> None:
    await connect("sqlite::memory:", auto_migrate=True)

    # --8<-- [start:usage]
    note = await Note.create(text="first draft")
    original = note.updated_at

    note.text = "second draft"
    await note.save()

    assert note.updated_at > original
    assert note.created_at <= note.updated_at
    # --8<-- [end:usage]

    print("timestamps example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
