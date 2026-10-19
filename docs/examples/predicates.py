"""Runnable companion to the Queries guide (docs/pages/guide/queries.md).

Declares the ledger every section of the guide queries, adds its rows
(``_ledger_seed.py``), then runs the filtering, ordering, paging and terminal
examples and asserts the rows that come back. The guide's other example
scripts import these models from here.
"""

import asyncio

# --8<-- [start:models]
from datetime import date
from decimal import Decimal
from typing import Annotated

from ferro import BackRef, Field, ForeignKey, ManyToMany, Model, Relation


class Owner(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    email: str = Field(unique=True)
    accounts: Relation[list["Account"]] = BackRef()


class Account(Model):
    id: int | None = Field(default=None, primary_key=True)
    label: str
    owner: Annotated[Owner | None, ForeignKey(related_name="accounts")] = None
    transactions: Relation[list["Transaction"]] = BackRef()


class Category(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(unique=True)
    parent: Annotated[
        "Category", ForeignKey(related_name="children", nullable=True)
    ] = None
    children: Relation[list["Category"]] = BackRef()
    transactions: Relation[list["Transaction"]] = BackRef()
    split_lines: Relation[list["SplitLine"]] = BackRef()


class Transaction(Model):
    id: int | None = Field(default=None, primary_key=True)
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
    id: int | None = Field(default=None, primary_key=True)
    amount: Decimal
    transaction: Annotated[Transaction, ForeignKey(related_name="lines")]
    category: Annotated[Category, ForeignKey(related_name="split_lines")]


class Transfer(Model):
    id: int | None = Field(default=None, primary_key=True)
    outflow: Annotated[
        Transaction, ForeignKey(related_name="transfer_out", unique=True)
    ]
    inflow: Annotated[Transaction, ForeignKey(related_name="transfer_in", unique=True)]


class Tag(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(unique=True)
    transactions: Relation[list[Transaction]] = ManyToMany(related_name="tags")


# --8<-- [end:models]


async def main() -> None:
    from _ledger_seed import seed

    # --8<-- [start:first-query]
    from ferro import connect, engines

    await connect("sqlite::memory:", auto_migrate=True)
    async with engines.session():
        await seed(globals())  # adds the rows this page queries

        large = (
            await Transaction.where(lambda transaction: transaction.amount <= -100)
            .order_by(lambda transaction: transaction.amount)
            .all()
        )
        for transaction in large:
            print(transaction.memo, transaction.amount)
        # --8<-- [end:first-query]
        assert [transaction.memo for transaction in large] == ["Rent", "To savings"]

        # --8<-- [start:filtering]
        uncleared = await Transaction.where(
            lambda transaction: transaction.cleared_on == None  # noqa: E711
        ).all()
        assert {transaction.memo for transaction in uncleared} == {
            "Train to Lyon",
            "Dinner in Lyon",
            "Weekly shop",
        }
        # --8<-- [end:filtering]

        # --8<-- [start:typo]
        try:
            Transaction.where(lambda transaction: transaction.amout < 0)
        except AttributeError as error:
            assert str(error) == (
                "Transaction has no queryable column 'amout'. Did you mean "
                "'amount'? Valid columns: account_id, amount, category_id, "
                "cleared_on, id, memo, posted_on. Valid relations: account, "
                "category."
            )
        else:
            raise AssertionError("a misspelled column must raise AttributeError")
        # --8<-- [end:typo]

        # --8<-- [start:operators]
        coffee = await Transaction.where(
            lambda transaction: transaction.memo.like("%coffee")
        ).all()
        assert {transaction.memo for transaction in coffee} == {"Office coffee"}

        lyon = await Transaction.where(
            lambda transaction: transaction.posted_on.in_(
                [date(2026, 3, 9), date(2026, 3, 10)]
            )
        ).all()
        assert {transaction.memo for transaction in lyon} == {
            "Train to Lyon",
            "Dinner in Lyon",
        }
        # --8<-- [end:operators]

        # --8<-- [start:combining]
        # & is AND, | is OR: parenthesize each side.
        small_uncleared = await Transaction.where(
            lambda transaction: (
                (transaction.cleared_on == None)  # noqa: E711
                & (transaction.amount > -90)
            )
        ).all()
        assert {transaction.memo for transaction in small_uncleared} == {
            "Train to Lyon",
            "Dinner in Lyon",
        }

        # Chained where() calls AND together.
        march_debits = (
            await Transaction.where(lambda transaction: transaction.amount < 0)
            .where(lambda transaction: transaction.posted_on >= date(2026, 3, 10))
            .all()
        )
        assert {transaction.memo for transaction in march_debits} == {
            "Dinner in Lyon",
            "Office coffee",
            "Weekly shop",
        }
        # --8<-- [end:combining]

        # --8<-- [start:negation]
        not_lyon = await Transaction.where(
            lambda transaction: (
                ~transaction.memo.like("%Lyon") & (transaction.amount < -50)
            )
        ).all()
        assert {transaction.memo for transaction in not_lyon} == {
            "Rent",
            "Market run",
            "To savings",
            "Weekly shop",
        }
        # --8<-- [end:negation]

        # --8<-- [start:three-valued]
        before_march_5 = await Transaction.where(
            lambda transaction: transaction.cleared_on < date(2026, 3, 5)
        ).all()
        not_before_march_5 = await Transaction.where(
            lambda transaction: ~(transaction.cleared_on < date(2026, 3, 5))
        ).all()
        assert {transaction.memo for transaction in before_march_5} == {
            "Salary",
            "Rent",
        }
        # The three uncleared rows are in neither list.
        assert {transaction.memo for transaction in not_before_march_5} == {
            "Market run",
            "To savings",
            "From checking",
            "Office coffee",
        }
        # --8<-- [end:three-valued]

        # --8<-- [start:keep-nulls]
        not_before_or_uncleared = await Transaction.where(
            lambda transaction: (
                ~(transaction.cleared_on < date(2026, 3, 5))
                | (transaction.cleared_on == None)  # noqa: E711
            )
        ).all()
        assert len(not_before_or_uncleared) == 7
        # --8<-- [end:keep-nulls]

        # --8<-- [start:get]
        from ferro import ModelDoesNotExist

        rent = await Transaction.get(2)
        assert rent.memo == "Rent"

        try:
            await Transaction.get(99)
        except ModelDoesNotExist as error:
            assert (error.model, error.pk) == (Transaction, 99)
        else:
            raise AssertionError("a missing row must raise ModelDoesNotExist")

        assert await Transaction.get_or_none(99) is None
        # --8<-- [end:get]

        # --8<-- [start:ordering-slicing]
        largest_debits = (
            await Transaction.select()
            .order_by(lambda transaction: transaction.amount)
            .limit(3)
            .all()
        )
        assert [transaction.memo for transaction in largest_debits] == [
            "Rent",
            "To savings",
            "Weekly shop",
        ]

        next_three = (
            await Transaction.select()
            .order_by(lambda transaction: transaction.amount)
            .limit(3)
            .offset(3)
            .all()
        )
        assert [transaction.memo for transaction in next_three] == [
            "Market run",
            "Train to Lyon",
            "Dinner in Lyon",
        ]
        # --8<-- [end:ordering-slicing]

        # --8<-- [start:nulls-ordering]
        uncleared_first = (
            await Transaction.select()
            .order_by(lambda transaction: transaction.cleared_on, "desc", nulls="first")
            .order_by(lambda transaction: transaction.id)
            .limit(4)
            .all()
        )
        assert [transaction.memo for transaction in uncleared_first] == [
            "Train to Lyon",
            "Dinner in Lyon",
            "Weekly shop",
            "Office coffee",
        ]
        # --8<-- [end:nulls-ordering]

        # --8<-- [start:after-paging]
        by_date = (
            Transaction.select()
            .order_by(lambda transaction: transaction.posted_on)
            .order_by(lambda transaction: transaction.id)
        )
        first_page = await by_date.limit(3).all()
        second_page = await by_date.after(first_page[-1]).limit(3).all()
        assert [transaction.memo for transaction in first_page] == [
            "Salary",
            "Rent",
            "Market run",
        ]
        assert [transaction.memo for transaction in second_page] == [
            "To savings",
            "From checking",
            "Train to Lyon",
        ]
        # --8<-- [end:after-paging]

        # --8<-- [start:position-of]
        position = by_date.position_of(first_page[-1])
        assert position == (date(2026, 3, 5), 3)

        same_page = await by_date.after(position).limit(3).all()
        assert same_page == second_page
        # --8<-- [end:position-of]

        # --8<-- [start:before-paging]
        previous_page = await by_date.before(second_page[0]).limit(2).all()
        assert [transaction.memo for transaction in previous_page] == [
            "Rent",
            "Market run",
        ]

        everything_earlier = await by_date.before(second_page[0]).all()
        assert [transaction.memo for transaction in everything_earlier] == [
            "Salary",
            "Rent",
            "Market run",
        ]

        previous_row = await by_date.before(second_page[0]).first()
        assert previous_row is not None and previous_row.memo == "Market run"
        # --8<-- [end:before-paging]

        # --8<-- [start:after-null-paging]
        by_clearing = (
            Transaction.select()
            .order_by(lambda transaction: transaction.cleared_on, "desc")
            .order_by(lambda transaction: transaction.id)
        )
        rest_of_uncleared = await by_clearing.after((None, 6)).all()
        assert [transaction.memo for transaction in rest_of_uncleared] == [
            "Dinner in Lyon",
            "Weekly shop",
        ]
        # --8<-- [end:after-null-paging]

        # --8<-- [start:terminals]
        everything = await Transaction.all()
        assert len(everything) == 9

        newest = (
            await Transaction.select()
            .order_by(lambda transaction: transaction.posted_on, "desc")
            .first()
        )
        assert newest is not None and newest.memo == "Weekly shop"

        uncleared_count = await Transaction.where(
            lambda transaction: transaction.cleared_on == None  # noqa: E711
        ).count()
        assert uncleared_count == 3

        any_refunds = await Transaction.where(
            lambda transaction: transaction.memo.like("Refund%")
        ).exists()
        assert any_refunds is False
        # --8<-- [end:terminals]

        # --8<-- [start:immutability]
        debits = Transaction.where(lambda transaction: transaction.amount < 0)

        debit_count = await debits.count()
        biggest = await debits.order_by(lambda transaction: transaction.amount).first()
        uncleared_debits = await debits.where(
            lambda transaction: transaction.cleared_on == None  # noqa: E711
        ).count()

        assert debit_count == 7
        assert biggest is not None and biggest.memo == "Rent"
        assert uncleared_debits == 3
        assert await debits.count() == 7  # debits itself never changed
        # --8<-- [end:immutability]

    print("predicates example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
