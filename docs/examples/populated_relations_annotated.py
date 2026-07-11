"""Annotated-style companion to populated_relations.py (AGENTS.md I-7).

Field options move into ``Annotated[...]``. The relationship declarations are
identical in both styles: forward FKs are always ``Annotated[Target,
ForeignKey(...)]`` and ``BackRef()`` is always an assignment. The include()
queries themselves declare no fields, so they live only in
populated_relations.py.
"""

import asyncio
from typing import Annotated

from ferro import BackRef, Field, ForeignKey, Model, Relation, connect, engines


# --8<-- [start:schema]
class Owner(Model):
    id: Annotated[int | None, Field(default=None, primary_key=True)]
    name: str
    accounts: Relation[list["Account"]] = BackRef()


class Account(Model):
    id: Annotated[int | None, Field(default=None, primary_key=True)]
    label: str
    owner: Annotated[Owner | None, ForeignKey(related_name="accounts")] = None
    transactions: Relation[list["Transaction"]] = BackRef()


class Transaction(Model):
    id: Annotated[int | None, Field(default=None, primary_key=True)]
    amount: int
    account: Annotated[Account | None, ForeignKey(related_name="transactions")] = None


# --8<-- [end:schema]


async def main() -> None:
    await connect("sqlite::memory:", auto_migrate=True)

    async with engines.session():
        owner = Owner(id=1, name="o1")
        await owner.save()
        account = Account(id=1, label="a1", owner=owner)
        await account.save()
        await Transaction(id=1, amount=10, account=account).save()

        transactions = (
            await Transaction.select().include(lambda t: t.account.owner).all()
        )
        assert transactions[0].account.label == "a1"
        assert transactions[0].account.owner.name == "o1"

    print("populated_relations_annotated example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
