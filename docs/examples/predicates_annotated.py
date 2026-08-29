"""Annotated-style companion to predicates.py (AGENTS.md I-7)."""

import asyncio
from datetime import UTC, datetime

# --8<-- [start:setup]
from typing import Annotated

from ferro import Field, Model, connect, engines


class User(Model):
    id: Annotated[int | None, Field(default=None, primary_key=True)]
    name: str
    age: int
    role: str = "member"
    archived: bool = False
# --8<-- [end:setup]


# --8<-- [start:nullable-model]
class Invoice(Model):
    id: Annotated[int | None, Field(default=None, primary_key=True)]
    reference: str
    amount: float | None = None  # None until the invoice is issued
# --8<-- [end:nullable-model]


# --8<-- [start:card-model]
class Card(Model):
    id: Annotated[int | None, Field(default=None, primary_key=True)]
    title: str
    pinned_at: datetime | None = None
    updated_at: datetime
# --8<-- [end:card-model]


async def main() -> None:
    await connect("sqlite::memory:", auto_migrate=True)

    async with engines.session():
        await User.bulk_create(
            [
                User(name="alice", age=34, role="admin"),
                User(name="bob", age=19),
            ]
        )

        adults = await User.where(lambda user: user.age >= 18).all()
        assert len(adults) == 2

        not_admins = await User.where(lambda user: ~(user.role == "admin")).all()
        assert len(not_admins) == 1

        await Invoice.bulk_create(
            [
                Invoice(reference="inv-1", amount=250.0),
                Invoice(reference="inv-2", amount=None),
            ]
        )

        # three-valued logic: the NULL-amount row matches neither predicate
        assert len(await Invoice.where(lambda invoice: invoice.amount > 100).all()) == 1
        assert len(await Invoice.where(lambda invoice: ~(invoice.amount > 100)).all()) == 0

        t0 = datetime(2026, 1, 1, tzinfo=UTC)
        t1 = datetime(2026, 2, 1, tzinfo=UTC)
        await Card.bulk_create(
            [
                Card(title="unpinned", pinned_at=None, updated_at=t1),
                Card(title="pinned", pinned_at=t0, updated_at=t0),
            ]
        )
        cards = (
            await Card.select()
            .order_by(lambda card: card.pinned_at, "desc", nulls="last")
            .order_by(lambda card: card.updated_at, "desc")
            .order_by(lambda card: card.id, "desc")
            .all()
        )
        assert [card.title for card in cards] == ["pinned", "unpinned"]

    print("predicates_annotated example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
