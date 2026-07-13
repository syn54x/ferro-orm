"""Annotated-style companion to aggregations.py (AGENTS.md I-7).

Field options move into ``Annotated[...]``. Aggregation is independent of the
declaration style — the queries are identical in both — so only the schema and
one round-trip live here.
"""

import asyncio
from decimal import Decimal
from typing import Annotated

from ferro import BackRef, Field, ForeignKey, Model, Relation, connect, engines


# --8<-- [start:schema]
class Account(Model):
    id: Annotated[int | None, Field(default=None, primary_key=True)]
    label: str
    transactions: Relation[list["Transaction"]] = BackRef()


class Transaction(Model):
    id: Annotated[int | None, Field(default=None, primary_key=True)]
    amount: int
    price: Annotated[Decimal | None, Field(default=None)]
    memo: str
    account: Annotated[
        Account | None, ForeignKey(related_name="transactions")
    ] = None
# --8<-- [end:schema]


async def main() -> None:
    await connect("sqlite::memory:", auto_migrate=True)

    async with engines.session():
        a1 = await Account.create(id=1, label="a1")
        await Transaction.create(
            id=1, amount=10, price=Decimal("1.25"), memo="coffee", account=a1
        )
        await Transaction.create(id=2, amount=20, memo="lunch", account=a1)

        rows = await Transaction.select(
            lambda t: {"acct": t.account_id, "total": t.amount.sum()}
        ).all()
        assert rows.model_dump() == [{"acct": 1, "total": 30}]

    print("aggregations_annotated example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
