"""Runnable companion to the Existence Tests guide (docs/pages/guide/queries.md).

Seeds the transfer/split-line domain the guide walks through (one-to-one and
to-many BackRefs plus a category both layers point at) and a tagged-user M2M
pair, then runs every existence test the guide shows and asserts the exact
rows that come back.
"""

import asyncio
from typing import Annotated

from ferro import BackRef, Field, ForeignKey, ManyToMany, Model, Relation, connect, engines


# --8<-- [start:schema]
class Category(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    transactions: Relation[list["Transaction"]] = BackRef()
    lines: Relation[list["SplitLine"]] = BackRef()


class Transaction(Model):
    id: int | None = Field(default=None, primary_key=True)
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
    id: int | None = Field(default=None, primary_key=True)
    outflow_transaction: Annotated[
        Transaction | None, ForeignKey(related_name="transfer_out", unique=True)
    ] = None
    inflow_transaction: Annotated[
        Transaction | None, ForeignKey(related_name="transfer_in", unique=True)
    ] = None


class SplitLine(Model):
    id: int | None = Field(default=None, primary_key=True)
    txn: Annotated[Transaction, ForeignKey(related_name="lines", on_delete="CASCADE")]
    category: Annotated[
        Category | None, ForeignKey(related_name="lines", on_delete="SET NULL")
    ] = None
    amount: int = 0
# --8<-- [end:schema]


# --8<-- [start:m2m-schema]
class Tag(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    users: Relation[list["User"]] = ManyToMany(related_name="tags")


class User(Model):
    id: int | None = Field(default=None, primary_key=True)
    username: str
    tags: Relation[list["Tag"]] = BackRef()
# --8<-- [end:m2m-schema]


async def main() -> None:
    await connect("sqlite::memory:", auto_migrate=True)

    async with engines.session():
        outflow = await Transaction.create(amount=-100)
        inflow = await Transaction.create(amount=100)
        plain = await Transaction.create(amount=-20)
        assert plain.amount == -20
        await Transfer.create(outflow_transaction=outflow)
        await Transfer.create(inflow_transaction=inflow)

        # --8<-- [start:bare]
        # "Is this transaction part of any transfer?" — membership via either
        # FK column, one EXISTS per side, each matching row exactly once.
        in_transfer = await Transaction.where(
            lambda t: t.transfer_out.exists() | t.transfer_in.exists()
        ).all()

        # The negated branch: ~ renders NOT EXISTS
        not_in_transfer = await Transaction.where(
            lambda t: ~t.transfer_out.exists() & ~t.transfer_in.exists()
        ).all()
        # --8<-- [end:bare]
        assert {t.amount for t in in_transfer} == {-100, 100}
        assert {t.amount for t in not_in_transfer} == {-20}

        groceries = await Category.create(name="Groceries")
        split = await Transaction.create(amount=-70)  # category vacated while split
        await SplitLine.create(txn=split, category=groceries, amount=-30)
        await SplitLine.create(txn=split, category=groceries, amount=-40)
        await Transaction.create(amount=-10, category=groceries)
        ids = [groceries.id]

        # --8<-- [start:scoped]
        # The inner lambda is a full ferro predicate over the related model:
        # "transactions carrying the category at the root OR on any line".
        # A line-less transaction survives through the OR's root branch, and
        # a transaction with several matching lines comes back exactly once.
        matching = await Transaction.where(
            lambda t: t.category_id.in_(ids)
            | t.lines.exists(lambda line: line.category_id.in_(ids))
        ).all()
        # --8<-- [end:scoped]
        assert {t.amount for t in matching} == {-70, -10}

        # --8<-- [start:composes]
        # Root-shaped results: existence tests compose with every other
        # predicate, ordering, and paging — nothing about the query changes.
        page = (
            await Transaction.where(
                lambda t: t.category_id.in_(ids)
                | t.lines.exists(lambda line: line.category_id.in_(ids))
            )
            .order_by("amount", "desc")
            .limit(1)
            .all()
        )
        # --8<-- [end:composes]
        assert [t.amount for t in page] == [-10]

        fuel_run = await Transaction.create(amount=-55)
        await SplitLine.create(txn=fuel_run, category=groceries, amount=-55)
        spread = await Transaction.create(amount=-60)
        await SplitLine.create(txn=spread, category=groceries, amount=-5)
        await SplitLine.create(txn=spread, amount=-55)

        # --8<-- [start:grouping]
        # Grouping is YOUR choice, spelled explicitly — the two shapes below
        # are different questions with different answers.

        # One line matches BOTH conditions:
        one_line_both = await Transaction.where(
            lambda t: t.lines.exists(
                lambda line: line.category_id.in_(ids) & (line.amount <= -50)
            )
        ).all()

        # SOME line matches each condition (possibly different lines):
        some_line_each = await Transaction.where(
            lambda t: t.lines.exists(lambda line: line.category_id.in_(ids))
            & t.lines.exists(lambda line: line.amount <= -50)
        ).all()
        # --8<-- [end:grouping]
        assert {t.amount for t in one_line_both} == {-55}
        assert {t.amount for t in some_line_each} == {-55, -60}

        # --8<-- [start:traversal-inside]
        # Forward traversal works inside the test (joins render INSIDE the
        # EXISTS subquery, ADR-0006 semantics unchanged), and tests nest.
        by_name = await Transaction.where(
            lambda t: t.lines.exists(lambda line: line.category.name == "Groceries")
        ).all()

        active_categories = await Category.where(
            lambda c: c.lines.exists(lambda line: line.txn.amount < -50)
        ).all()
        # --8<-- [end:traversal-inside]
        assert {t.amount for t in by_name} == {-70, -55, -60}
        assert {c.name for c in active_categories} == {"Groceries"}

        admin = await Tag.create(name="admin")
        beta = await Tag.create(name="beta")
        alice = await User.create(username="alice")
        bob = await User.create(username="bob")
        await User.create(username="carol")
        await admin.users.add(alice)
        await beta.users.add(alice, bob)

        # --8<-- [start:m2m]
        # Many-to-many spells identically — the test correlates through the
        # join table, and the inner lambda scopes over the target model.
        admins = await User.where(
            lambda u: u.tags.exists(lambda tag: tag.name == "admin")
        ).all()
        tagged = await User.where(lambda u: u.tags.exists()).all()
        untagged = await User.where(lambda u: ~u.tags.exists()).all()
        # --8<-- [end:m2m]
        assert {u.username for u in admins} == {"alice"}
        assert {u.username for u in tagged} == {"alice", "bob"}
        assert {u.username for u in untagged} == {"carol"}

    print("existence_tests example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
