"""Runnable companion to "Existence tests on reverse & many-to-many
relations" in the Queries guide (docs/pages/guide/queries.md).

Runs every existence test the guide shows against the guide's ledger
(declared in predicates.py, rows from _ledger_seed.py) and asserts the rows
that come back.
"""

import asyncio

import predicates
from _ledger_seed import seed
from predicates import Account, Category, Owner, Tag, Transaction

from ferro import connect, engines


async def main() -> None:
    await connect("sqlite::memory:", auto_migrate=True)

    async with engines.session():
        await seed(vars(predicates))

        # --8<-- [start:first]
        split = await Transaction.where(
            lambda transaction: transaction.lines.exists()
        ).all()
        assert [transaction.memo for transaction in split] == ["Weekly shop"]
        # --8<-- [end:first]

        # --8<-- [start:bare]
        in_a_transfer = await Transaction.where(
            lambda transaction: (
                transaction.transfer_out.exists() | transaction.transfer_in.exists()
            )
        ).all()
        assert {transaction.memo for transaction in in_a_transfer} == {
            "To savings",
            "From checking",
        }

        not_in_a_transfer = await Transaction.where(
            lambda transaction: (
                ~transaction.transfer_out.exists() & ~transaction.transfer_in.exists()
            )
        ).count()
        assert not_in_a_transfer == 7
        # --8<-- [end:bare]

        groceries = await Category.where(
            lambda category: category.name == "Groceries"
        ).first()
        dining = await Category.where(
            lambda category: category.name == "Dining"
        ).first()
        assert groceries is not None and dining is not None

        # --8<-- [start:scoped]
        # Groceries on the transaction itself, or on any of its split lines.
        grocery_spend = await Transaction.where(
            lambda transaction: (
                (transaction.category_id == groceries.id)
                | transaction.lines.exists(
                    lambda split_line: split_line.category_id == groceries.id
                )
            )
        ).all()
        assert {transaction.memo for transaction in grocery_spend} == {
            "Market run",
            "Weekly shop",
        }
        # --8<-- [end:scoped]

        # --8<-- [start:composes]
        largest_grocery_spend = (
            await Transaction.where(
                lambda transaction: (
                    (transaction.category_id == groceries.id)
                    | transaction.lines.exists(
                        lambda split_line: split_line.category_id == groceries.id
                    )
                )
            )
            .order_by(lambda transaction: transaction.amount)
            .first()
        )
        assert largest_grocery_spend is not None
        assert largest_grocery_spend.memo == "Weekly shop"
        # --8<-- [end:composes]

        # --8<-- [start:grouping]
        # One split line that is both Dining and below -50:
        one_line_both = await Transaction.where(
            lambda transaction: transaction.lines.exists(
                lambda split_line: (
                    (split_line.category_id == dining.id) & (split_line.amount < -50)
                )
            )
        ).all()
        assert one_line_both == []

        # A Dining line, and a line below -50 (here, two different lines):
        some_line_each = await Transaction.where(
            lambda transaction: (
                transaction.lines.exists(
                    lambda split_line: split_line.category_id == dining.id
                )
                & transaction.lines.exists(lambda split_line: split_line.amount < -50)
            )
        ).all()
        assert [transaction.memo for transaction in some_line_each] == ["Weekly shop"]
        # --8<-- [end:grouping]

        # --8<-- [start:traversal-inside]
        food_lines = await Transaction.where(
            lambda transaction: transaction.lines.exists(
                lambda split_line: split_line.category.parent.name == "Food"
            )
        ).all()
        assert [transaction.memo for transaction in food_lines] == ["Weekly shop"]
        # --8<-- [end:traversal-inside]

        # --8<-- [start:nested]
        owners_with_split_spending = await Owner.where(
            lambda owner: owner.accounts.exists(
                lambda account: account.transactions.exists(
                    lambda transaction: transaction.lines.exists()
                )
            )
        ).all()
        assert [owner.name for owner in owners_with_split_spending] == ["Ada Lovelace"]
        # --8<-- [end:nested]

        # --8<-- [start:m2m]
        taxed = await Transaction.where(
            lambda transaction: transaction.tags.exists(lambda tag: tag.name == "tax")
        ).all()
        assert [transaction.memo for transaction in taxed] == ["Rent"]

        tags_on_uncleared = await Tag.where(
            lambda tag: tag.transactions.exists(
                lambda transaction: transaction.cleared_on == None  # noqa: E711
            )
        ).all()
        assert [tag.name for tag in tags_on_uncleared] == ["reimbursable"]

        untagged = await Transaction.where(
            lambda transaction: ~transaction.tags.exists()
        ).count()
        assert untagged == 6
        # --8<-- [end:m2m]

        # --8<-- [start:dead-ends]
        try:
            Transaction.where(
                lambda transaction: transaction.lines.category_id == groceries.id
            )
        except AttributeError as error:
            assert "lines.exists(" in str(error)
        else:
            raise AssertionError("column access on a reverse relation must raise")

        try:
            Transaction.where(
                lambda transaction: transaction.transfer_out != None  # noqa: E711
            )
        except TypeError as error:
            assert "transfer_out.exists()" in str(error)
        else:
            raise AssertionError("comparing a reverse relation must raise")

        try:
            Account.select().join(lambda account: account.transactions)
        except TypeError as error:
            assert "transactions.exists(" in str(error)
        else:
            raise AssertionError("joining a reverse relation must raise")
        # --8<-- [end:dead-ends]

    print("existence_tests example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
