"""Runnable companion to "Selecting a column subset" in the Queries guide
(docs/pages/guide/queries.md).

Runs every projection the guide shows against the guide's ledger (declared in
predicates.py, rows from _ledger_seed.py) and asserts the records that come
back.
"""

import asyncio
from decimal import Decimal

import predicates
from _ledger_seed import seed
from predicates import Transaction

from ferro import Rows, connect, engines


async def main() -> None:
    await connect("sqlite::memory:", auto_migrate=True)

    async with engines.session():
        await seed(vars(predicates))

        # --8<-- [start:basic]
        rows = (
            await Transaction.select(
                lambda transaction: (transaction.memo, transaction.amount)
            )
            .order_by(lambda transaction: transaction.id)
            .limit(2)
            .all()
        )
        assert rows[1].memo == "Rent"  # attribute access, like a model
        assert rows.model_dump() == [
            {"memo": "Salary", "amount": Decimal("3200")},
            {"memo": "Rent", "amount": Decimal("-1450")},
        ]
        # --8<-- [end:basic]

        # --8<-- [start:not-a-model]
        row = rows[0]
        assert not isinstance(row, Transaction)
        assert not hasattr(row, "save")
        # --8<-- [end:not-a-model]

        # --8<-- [start:single]
        memos = (
            await Transaction.select(lambda transaction: transaction.memo)
            .where(lambda transaction: transaction.cleared_on == None)
            .order_by(lambda transaction: transaction.id)
            .all()
        )
        assert [row.memo for row in memos] == [
            "Train to Lyon",
            "Dinner in Lyon",
            "Weekly shop",
        ]
        # --8<-- [end:single]

        # --8<-- [start:container]
        newest = (
            await Transaction.select(
                lambda transaction: (transaction.id, transaction.memo)
            )
            .order_by(lambda transaction: transaction.id, "desc")
            .all()
        )
        assert len(newest) == 9
        assert newest[0].memo == "Weekly shop"

        top_two = newest[:2]  # a slice is a Rows too
        assert isinstance(top_two, Rows)
        assert top_two.model_dump() == [
            {"id": 9, "memo": "Weekly shop"},
            {"id": 8, "memo": "Office coffee"},
        ]
        # --8<-- [end:container]

        # --8<-- [start:strings]
        rows = await Transaction.select("id", "memo").order_by("id").limit(1).all()
        assert rows.model_dump() == [{"id": 1, "memo": "Salary"}]
        # --8<-- [end:strings]

        # --8<-- [start:traversed]
        rows = (
            await Transaction.select(
                lambda transaction: (transaction.memo, transaction.account.label)
            )
            .order_by(lambda transaction: transaction.id)
            .limit(1)
            .all()
        )
        assert rows.model_dump() == [{"memo": "Salary", "label": "Checking"}]
        # --8<-- [end:traversed]

        # --8<-- [start:aliases]
        rows = (
            await Transaction.select(
                lambda transaction: {
                    "memo": transaction.memo,
                    "account": transaction.account.label,
                    "owner_email": transaction.account.owner.email,
                }
            )
            .order_by(lambda transaction: transaction.id)
            .limit(1)
            .all()
        )
        assert rows.model_dump() == [
            {"memo": "Salary", "account": "Checking", "owner_email": "ada@example.com"}
        ]
        # --8<-- [end:aliases]

        # --8<-- [start:collision]
        try:
            Transaction.select(
                lambda transaction: (transaction.id, transaction.account.id)
            )
        except ValueError as error:
            assert "two fields named 'id'" in str(error)
            assert "dict selector form" in str(error)
        else:
            raise AssertionError("two fields named 'id' must raise ValueError")
        # --8<-- [end:collision]

        # --8<-- [start:traversal-narrows]
        owner_emails = await Transaction.select(
            lambda transaction: {
                "memo": transaction.memo,
                "owner_email": transaction.account.owner.email,
            }
        ).all()
        assert len(owner_emails) == 8  # Office coffee's account has no owner

        with_ownerless = (
            await Transaction.select(
                lambda transaction: {
                    "memo": transaction.memo,
                    "owner_email": transaction.account.owner.email,
                }
            )
            .left_join(lambda transaction: transaction.account.owner)
            .order_by(lambda transaction: transaction.id)
            .all()
        )
        assert len(with_ownerless) == 9
        assert with_ownerless[7].model_dump() == {
            "memo": "Office coffee",
            "owner_email": None,
        }
        # --8<-- [end:traversal-narrows]

        # --8<-- [start:compose]
        rows = (
            await Transaction.select(
                lambda transaction: (transaction.id, transaction.memo)
            )
            .where(lambda transaction: transaction.account.label == "Travel")
            .order_by(lambda transaction: transaction.amount)  # not selected
            .all()
        )
        assert rows.model_dump() == [
            {"id": 6, "memo": "Train to Lyon"},
            {"id": 7, "memo": "Dinner in Lyon"},
        ]
        # --8<-- [end:compose]

        # --8<-- [start:projected-paging]
        by_account = (
            Transaction.select(
                lambda transaction: {
                    "account": transaction.account.label,
                    "id": transaction.id,
                    "memo": transaction.memo,
                }
            )
            .order_by(lambda transaction: transaction.account.label)
            .order_by(lambda transaction: transaction.id)
        )
        first_page = await by_account.limit(5).all()
        next_page = await by_account.after(first_page[-1]).limit(2).all()
        assert [row.memo for row in next_page] == ["Office coffee", "From checking"]
        # --8<-- [end:projected-paging]

        # --8<-- [start:count]
        uncleared = Transaction.select(lambda transaction: transaction.memo).where(
            lambda transaction: transaction.cleared_on == None
        )
        assert await uncleared.count() == 3
        assert await uncleared.exists() is True
        # --8<-- [end:count]

        # --8<-- [start:typo]
        try:
            Transaction.select(lambda transaction: (transaction.id, transaction.amonut))
        except AttributeError as error:
            assert str(error).startswith(
                "Transaction has no queryable column 'amonut'. Did you mean 'amount'?"
            )
        else:
            raise AssertionError("a misspelled column must raise AttributeError")
        # --8<-- [end:typo]

    print("partial_selects example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
