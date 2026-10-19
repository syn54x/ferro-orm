"""Annotated-style companion to aggregations.py (AGENTS.md I-7).

Field options move into ``Annotated[...]``. Aggregation is independent of the
declaration style, so the queries are the ones in aggregations.py; only the
schema and one grouped round trip live here.
"""

import asyncio
from decimal import Decimal
from typing import Annotated

from ferro import BackRef, Field, ForeignKey, Model, Relation, connect, engines


# --8<-- [start:schema]
class Account(Model):
    id: Annotated[int | None, Field(primary_key=True)] = None
    label: str
    transactions: Relation[list["Transaction"]] = BackRef()


class Transaction(Model):
    id: Annotated[int | None, Field(primary_key=True)] = None
    amount: int
    price: Decimal | None = None
    memo: str
    account: Annotated[Account | None, ForeignKey(related_name="transactions")] = None


# --8<-- [end:schema]


async def main() -> None:
    await connect("sqlite::memory:", auto_migrate=True)

    async with engines.session():
        checking = await Account.create(id=1, label="Checking")
        savings = await Account.create(id=2, label="Savings")
        await Transaction.create(
            id=1, amount=10, price=Decimal("1.25"), memo="coffee", account=checking
        )
        await Transaction.create(
            id=2, amount=20, price=Decimal("2.75"), memo="lunch", account=checking
        )
        await Transaction.create(
            id=3, amount=45, price=Decimal("5.00"), memo="dinner", account=savings
        )
        await Transaction.create(id=4, amount=40, memo="orphan")

        rows = await (
            Transaction.select(
                lambda transaction: {
                    "acct": transaction.account_id,
                    "total": transaction.amount.sum(),
                }
            )
            .where(lambda transaction: transaction.account_id != None)  # noqa: E711
            .order_by("acct")
            .all()
        )
        assert rows.model_dump() == [
            {"acct": 1, "total": 30},
            {"acct": 2, "total": 45},
        ]

    print("aggregations_annotated example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
