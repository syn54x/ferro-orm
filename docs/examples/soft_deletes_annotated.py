"""Annotated-style companion to soft_deletes.py (AGENTS.md I-7)."""

import asyncio

# --8<-- [start:model]
from datetime import UTC, datetime
from typing import Annotated

from ferro import Field, Model, connect, engines
from ferro.query import Query


class SoftDeleteMixin:
    """Soft-delete behaviour as a plain class.

    Each model that uses it declares ``is_deleted`` and ``deleted_at``
    itself; the mixin supplies the methods.
    """

    async def soft_delete(self) -> None:
        self.is_deleted = True
        self.deleted_at = datetime.now(UTC)
        await self.save()

    async def restore(self) -> None:
        self.is_deleted = False
        self.deleted_at = None
        await self.save()

    @classmethod
    def active(cls) -> Query:
        return cls.where(lambda row: row.is_deleted == False)  # noqa: E712


class Invoice(SoftDeleteMixin, Model):
    id: Annotated[int | None, Field(primary_key=True)] = None
    number: Annotated[str, Field(unique=True)]
    is_deleted: bool = False
    deleted_at: datetime | None = None


# --8<-- [end:model]


async def main() -> None:
    # --8<-- [start:session]
    await connect("sqlite::memory:", auto_migrate=True)

    async with engines.session():
        invoice = await Invoice.create(number="INV-001")
        await Invoice.create(number="INV-002")
        await Invoice.create(number="INV-003")
        # --8<-- [end:session]

        await invoice.soft_delete()
        assert await Invoice.active().count() == 2
        assert await Invoice.select().count() == 3

        await invoice.restore()
        assert await Invoice.active().count() == 3

    print("soft_deletes_annotated example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
