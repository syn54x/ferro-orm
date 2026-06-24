"""Runnable companion to the Soft Deletes how-to (docs/pages/howto/soft-deletes.md)."""

import asyncio

# --8<-- [start:model]
from datetime import UTC, datetime

from ferro import Field, Model, connect
from ferro.query import Query


class SoftDeleteMixin:
    """Soft-delete behavior as a plain mixin.

    Declare ``is_deleted`` and ``deleted_at`` on each concrete model;
    the mixin supplies the lifecycle methods.
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
        return cls.where(lambda invoice: invoice.is_deleted == False)  # noqa: E712


class Invoice(SoftDeleteMixin, Model):
    id: int | None = Field(default=None, primary_key=True)
    number: str
    is_deleted: bool = False
    deleted_at: datetime | None = None
# --8<-- [end:model]


async def main() -> None:
    await connect("sqlite::memory:", auto_migrate=True)

    # --8<-- [start:usage]
    invoice = await Invoice.create(number="INV-001")
    await Invoice.create(number="INV-002")

    await invoice.soft_delete()
    assert await Invoice.active().count() == 1
    assert await Invoice.select().count() == 2  # row still exists

    await invoice.restore()
    assert await Invoice.active().count() == 2
    # --8<-- [end:usage]

    print("soft_deletes example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
