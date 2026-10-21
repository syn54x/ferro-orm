"""Runnable companion to the soft-deletes how-to (docs/pages/howto/soft-deletes.md)."""

import asyncio

# --8<-- [start:model]
from datetime import UTC, datetime

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
        return cls.where(lambda row: row.is_deleted == False)


class Invoice(SoftDeleteMixin, Model):
    id: int | None = Field(default=None, primary_key=True)
    number: str = Field(unique=True)
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

        # --8<-- [start:usage]
        await invoice.soft_delete()
        assert await Invoice.active().count() == 2
        assert await Invoice.select().count() == 3

        await invoice.restore()
        assert await Invoice.active().count() == 3
        # --8<-- [end:usage]

        await invoice.soft_delete()

        # --8<-- [start:querying]
        live = await (
            Invoice.active()
            .where(lambda invoice: invoice.number >= "INV-002")
            .order_by(lambda invoice: invoice.number)
            .all()
        )
        assert [invoice.number for invoice in live] == ["INV-002", "INV-003"]

        trashed = await Invoice.where(lambda invoice: invoice.is_deleted == True).all()
        assert [invoice.number for invoice in trashed] == ["INV-001"]
        # --8<-- [end:querying]

        # --8<-- [start:unique]
        from ferro import UniqueViolationError

        try:
            await Invoice.create(number="INV-001")
        except UniqueViolationError:
            print("INV-001 is still taken")
        else:
            raise AssertionError("expected UniqueViolationError")
        # --8<-- [end:unique]

        # --8<-- [start:purge]
        soft_deleted = Invoice.where(lambda invoice: invoice.is_deleted == True)
        purged = await soft_deleted.delete()
        assert purged == 1
        assert await Invoice.select().count() == 2
        # --8<-- [end:purge]

    print("soft_deletes example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
