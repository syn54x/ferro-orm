"""Annotated-style companion to traversal.py (AGENTS.md I-7).

Field options move into ``Annotated[...]``. The relationship declarations are
identical in both styles: forward FKs are always ``Annotated[Target,
ForeignKey(...)]`` and ``BackRef()``/``ManyToMany()`` are always assignments.
The queries themselves declare no fields, so they live only in traversal.py.
"""

import asyncio
from typing import Annotated

from ferro import BackRef, Field, ForeignKey, ManyToMany, Model, Relation, connect, engines


# --8<-- [start:schema]
class Ledger(Model):
    id: Annotated[int | None, Field(default=None, primary_key=True)]
    name: str
    accounts: Relation[list["Account"]] = BackRef()


class Owner(Model):
    id: Annotated[int | None, Field(default=None, primary_key=True)]
    email: str
    accounts: Relation[list["Account"]] = BackRef()


class Account(Model):
    id: Annotated[int | None, Field(default=None, primary_key=True)]
    label: str
    ledger: Annotated[Ledger, ForeignKey(related_name="accounts")]
    owner: Annotated[Owner, ForeignKey(related_name="accounts")]
    transactions: Relation[list["Transaction"]] = BackRef()
    notes: Relation[list["Note"]] = BackRef()


class Transaction(Model):
    id: Annotated[int | None, Field(default=None, primary_key=True)]
    amount: int
    account: Annotated[Account, ForeignKey(related_name="transactions")]
# --8<-- [end:schema]


# --8<-- [start:note-model]
class Note(Model):
    id: Annotated[int | None, Field(default=None, primary_key=True)]
    body: str
    account: Annotated[Account | None, ForeignKey(related_name="notes")] = None
# --8<-- [end:note-model]


# --8<-- [start:two-fk-model]
class Airport(Model):
    id: Annotated[int | None, Field(default=None, primary_key=True)]
    code: str
    departures: Relation[list["Flight"]] = BackRef()
    arrivals: Relation[list["Flight"]] = BackRef()


class Flight(Model):
    id: Annotated[int | None, Field(default=None, primary_key=True)]
    origin: Annotated[Airport, ForeignKey(related_name="departures")]
    destination: Annotated[Airport, ForeignKey(related_name="arrivals")]
# --8<-- [end:two-fk-model]


# --8<-- [start:self-fk-model]
class Employee(Model):
    id: Annotated[int | None, Field(default=None, primary_key=True)]
    name: str
    manager: Annotated["Employee", ForeignKey(related_name="reports", nullable=True)] = None
    reports: Relation[list["Employee"]] = BackRef()
# --8<-- [end:self-fk-model]


# --8<-- [start:m2m-model]
class Author(Model):
    id: Annotated[int | None, Field(default=None, primary_key=True)]
    role: str
    tags: Relation[list["Tag"]] = BackRef()


class Tag(Model):
    id: Annotated[int | None, Field(default=None, primary_key=True)]
    name: str
    created_by: Annotated[Author, ForeignKey(related_name="tags")]
    posts: Relation[list["Post"]] = BackRef()


class Post(Model):
    id: Annotated[int | None, Field(default=None, primary_key=True)]
    title: str
    tags: Relation[list["Tag"]] = ManyToMany(related_name="posts")
# --8<-- [end:m2m-model]


async def main() -> None:
    await connect("sqlite::memory:", auto_migrate=True)

    async with engines.session():
        la = await Ledger.create(id=1, name="ledger-a")
        o1 = await Owner.create(id=1, email="o1@ferro.dev")
        a1 = await Account.create(id=1, label="a1", ledger=la, owner=o1)
        await Transaction.create(id=1, amount=10, account=a1)

        rows = await Transaction.where(
            lambda transaction: transaction.account.ledger_id == la.id
        ).all()
        assert {r.id for r in rows} == {1}

    print("traversal_annotated example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
