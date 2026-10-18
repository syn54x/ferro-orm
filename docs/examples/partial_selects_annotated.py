"""Annotated-style companion to partial_selects.py (AGENTS.md I-7).

Field options move into ``Annotated[...]``. Projection is independent of the
declaration style — the queries are identical in both — so only the schema and
one round-trip live here.
"""

import asyncio
from typing import Annotated

from ferro import BackRef, Field, ForeignKey, Model, Relation, connect, engines


# --8<-- [start:schema]
class Owner(Model):
    id: Annotated[int | None, Field(default=None, primary_key=True)]
    email: str
    accounts: Relation[list["Account"]] = BackRef()


class Account(Model):
    id: Annotated[int | None, Field(default=None, primary_key=True)]
    label: str
    owner: Annotated[Owner | None, ForeignKey(related_name="accounts")] = None
    transactions: Relation[list["Transaction"]] = BackRef()


class Transaction(Model):
    id: Annotated[int | None, Field(default=None, primary_key=True)]
    amount: int
    memo: str
    account: Annotated[Account | None, ForeignKey(related_name="transactions")] = None


# --8<-- [end:schema]


async def main() -> None:
    await connect("sqlite::memory:", auto_migrate=True)

    async with engines.session():
        a1 = await Account.create(id=1, label="a1")
        await Transaction.create(id=1, amount=10, memo="coffee", account=a1)

        rows = await Transaction.select(lambda t: (t.id, t.amount)).all()
        assert rows.model_dump() == [{"id": 1, "amount": 10}]

    print("partial_selects_annotated example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
