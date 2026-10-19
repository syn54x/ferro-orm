"""Annotated-style companion to predicates.py (AGENTS.md I-7).

Declares the Queries guide's ledger with field options inside
``Annotated[...]``, adds the same rows and checks that a few of the guide's
queries return what they return against predicates.py's models. Foreign keys
and ``BackRef()`` / ``ManyToMany()`` have one form and read the same in both
files.
"""

import asyncio

# --8<-- [start:models]
from datetime import date
from decimal import Decimal
from typing import Annotated

from ferro import BackRef, Field, ForeignKey, ManyToMany, Model, Relation


class Owner(Model):
    id: Annotated[int | None, Field(primary_key=True)] = None
    name: str
    email: Annotated[str, Field(unique=True)]
    accounts: Relation[list["Account"]] = BackRef()


class Account(Model):
    id: Annotated[int | None, Field(primary_key=True)] = None
    label: str
    owner: Annotated[Owner | None, ForeignKey(related_name="accounts")] = None
    transactions: Relation[list["Transaction"]] = BackRef()


class Category(Model):
    id: Annotated[int | None, Field(primary_key=True)] = None
    name: Annotated[str, Field(unique=True)]
    parent: Annotated[
        "Category", ForeignKey(related_name="children", nullable=True)
    ] = None
    children: Relation[list["Category"]] = BackRef()
    transactions: Relation[list["Transaction"]] = BackRef()
    split_lines: Relation[list["SplitLine"]] = BackRef()


class Transaction(Model):
    id: Annotated[int | None, Field(primary_key=True)] = None
    memo: str
    amount: Decimal
    posted_on: date
    cleared_on: date | None = None
    account: Annotated[Account, ForeignKey(related_name="transactions")]
    category: Annotated[Category | None, ForeignKey(related_name="transactions")] = None
    lines: Relation[list["SplitLine"]] = BackRef()
    transfer_out: "Transfer" = BackRef()
    transfer_in: "Transfer" = BackRef()
    tags: Relation[list["Tag"]] = BackRef()


class SplitLine(Model):
    id: Annotated[int | None, Field(primary_key=True)] = None
    amount: Decimal
    transaction: Annotated[Transaction, ForeignKey(related_name="lines")]
    category: Annotated[Category, ForeignKey(related_name="split_lines")]


class Transfer(Model):
    id: Annotated[int | None, Field(primary_key=True)] = None
    outflow: Annotated[
        Transaction, ForeignKey(related_name="transfer_out", unique=True)
    ]
    inflow: Annotated[Transaction, ForeignKey(related_name="transfer_in", unique=True)]


class Tag(Model):
    id: Annotated[int | None, Field(primary_key=True)] = None
    name: Annotated[str, Field(unique=True)]
    transactions: Relation[list[Transaction]] = ManyToMany(related_name="tags")


# --8<-- [end:models]


async def main() -> None:
    from _ledger_seed import seed

    from ferro import connect, engines

    await connect("sqlite::memory:", auto_migrate=True)
    async with engines.session():
        await seed(globals())

        large = (
            await Transaction.where(lambda transaction: transaction.amount <= -100)
            .order_by(lambda transaction: transaction.amount)
            .all()
        )
        assert [transaction.memo for transaction in large] == ["Rent", "To savings"]

        on_ada_accounts = await Transaction.where(
            lambda transaction: transaction.account.owner.name == "Ada Lovelace"
        ).count()
        assert on_ada_accounts == 6

        split = await Transaction.where(
            lambda transaction: transaction.lines.exists()
        ).all()
        assert [transaction.memo for transaction in split] == ["Weekly shop"]

        reimbursable = await Transaction.where(
            lambda transaction: transaction.tags.exists(
                lambda tag: tag.name == "reimbursable"
            )
        ).count()
        assert reimbursable == 2

        under_food = await Category.where(
            lambda category: category.parent.name == "Food"
        ).count()
        assert under_food == 2

    print("predicates_annotated example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
