"""Annotated-style companion to existence_tests.py (AGENTS.md I-7)."""

import asyncio
from typing import Annotated

from ferro import BackRef, Field, ForeignKey, ManyToMany, Model, Relation, connect, engines


# --8<-- [start:schema]
class Category(Model):
    id: Annotated[int | None, Field(default=None, primary_key=True)]
    name: str
    transactions: Relation[list["Transaction"]] = BackRef()
    lines: Relation[list["SplitLine"]] = BackRef()


class Transaction(Model):
    id: Annotated[int | None, Field(default=None, primary_key=True)]
    amount: int
    category: Annotated[
        Category | None, ForeignKey(related_name="transactions", on_delete="SET NULL")
    ] = None
    # One-to-one BackRefs: a transfer references a transaction via a unique FK
    transfer_out: "Transfer" = BackRef()
    transfer_in: "Transfer" = BackRef()
    # To-many BackRef: a split transaction carries its lines
    lines: Relation[list["SplitLine"]] = BackRef()


class Transfer(Model):
    id: Annotated[int | None, Field(default=None, primary_key=True)]
    outflow_transaction: Annotated[
        Transaction | None, ForeignKey(related_name="transfer_out", unique=True)
    ] = None
    inflow_transaction: Annotated[
        Transaction | None, ForeignKey(related_name="transfer_in", unique=True)
    ] = None


class SplitLine(Model):
    id: Annotated[int | None, Field(default=None, primary_key=True)]
    txn: Annotated[Transaction, ForeignKey(related_name="lines", on_delete="CASCADE")]
    category: Annotated[
        Category | None, ForeignKey(related_name="lines", on_delete="SET NULL")
    ] = None
    amount: int = 0
# --8<-- [end:schema]


# --8<-- [start:m2m-schema]
class Tag(Model):
    id: Annotated[int | None, Field(default=None, primary_key=True)]
    name: str
    users: Relation[list["User"]] = ManyToMany(related_name="tags")


class User(Model):
    id: Annotated[int | None, Field(default=None, primary_key=True)]
    username: str
    tags: Relation[list["Tag"]] = BackRef()
# --8<-- [end:m2m-schema]


async def main() -> None:
    await connect("sqlite::memory:", auto_migrate=True)

    async with engines.session():
        outflow = await Transaction.create(amount=-100)
        await Transaction.create(amount=-20)
        await Transfer.create(outflow_transaction=outflow)

        in_transfer = await Transaction.where(
            lambda t: t.transfer_out.exists() | t.transfer_in.exists()
        ).all()
        assert {t.amount for t in in_transfer} == {-100}

        groceries = await Category.create(name="Groceries")
        split = await Transaction.create(amount=-70)
        await SplitLine.create(txn=split, category=groceries, amount=-70)

        line_aware = await Transaction.where(
            lambda t: t.category_id.in_([groceries.id])
            | t.lines.exists(lambda line: line.category_id.in_([groceries.id]))
        ).all()
        assert {t.amount for t in line_aware} == {-70}

        admin = await Tag.create(name="admin")
        alice = await User.create(username="alice")
        await User.create(username="bob")
        await admin.users.add(alice)

        admins = await User.where(
            lambda u: u.tags.exists(lambda tag: tag.name == "admin")
        ).all()
        assert {u.username for u in admins} == {"alice"}

    print("existence_tests_annotated example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
