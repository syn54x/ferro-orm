"""Runnable companion to "Populating relations with include()" in the Queries
guide (docs/pages/guide/queries.md); the relationships guide embeds ``basic``.

Runs every include() query the guide shows against the guide's ledger
(declared in predicates.py, rows from _ledger_seed.py) and asserts the
populations that come back.

Two sessions on purpose: populations accumulate across one session's queries,
so the accumulation example needs instances no earlier include has touched.
"""

import asyncio
import inspect

import predicates
from _ledger_seed import seed
from predicates import Account, Transaction

from ferro import connect, engines, execute


async def main() -> None:
    await connect("sqlite::memory:", auto_migrate=True)

    async with engines.session():
        await seed(vars(predicates))

        # --8<-- [start:awaitable]
        transaction = await Transaction.get(6)

        account = await transaction.account  # runs its own query
        assert account.label == "Travel"
        # --8<-- [end:awaitable]

        # --8<-- [start:basic]
        latest = (
            await Transaction.select()
            .include(lambda transaction: transaction.account)
            .order_by(lambda transaction: transaction.id, "desc")
            .limit(3)
            .all()
        )

        for transaction in latest:
            print(transaction.memo, "|", transaction.account.label)  # no await
        # --8<-- [end:basic]
        assert [
            (transaction.memo, transaction.account.label) for transaction in latest
        ] == [
            ("Weekly shop", "Checking"),
            ("Office coffee", "Petty cash"),
            ("Dinner in Lyon", "Travel"),
        ]

        # --8<-- [start:membership]
        everything = (
            await Transaction.select()
            .include(lambda transaction: transaction.category)
            .all()
        )
        assert len(everything) == 9

        salary = next(row for row in everything if row.memo == "Salary")
        assert salary.category is None  # no category: populated as None
        # --8<-- [end:membership]

        # --8<-- [start:interplay]
        on_travel = (
            await Transaction.where(
                lambda transaction: transaction.account.label == "Travel"
            )
            .include(lambda transaction: transaction.account)
            .all()
        )
        assert {transaction.memo for transaction in on_travel} == {
            "Train to Lyon",
            "Dinner in Lyon",
        }
        # --8<-- [end:interplay]

        # --8<-- [start:count]
        debits = Transaction.where(lambda transaction: transaction.amount < 0)
        with_accounts = debits.include(lambda transaction: transaction.account)

        assert await with_accounts.count() == await debits.count() == 7
        # --8<-- [end:count]

        # --8<-- [start:multi-hop]
        with_owners = (
            await Transaction.select()
            .include(lambda transaction: transaction.account.owner)
            .order_by(lambda transaction: transaction.id)
            .all()
        )

        salary = with_owners[0]
        assert salary.account.label == "Checking"  # first hop
        assert salary.account.owner.name == "Ada Lovelace"  # second hop

        coffee = with_owners[7]
        assert coffee.account.label == "Petty cash"
        assert coffee.account.owner is None  # the chain ends at the NULL
        # --8<-- [end:multi-hop]

        # --8<-- [start:identity]
        travel = await Account.get(3)
        on_travel = (
            await Transaction.where(
                lambda transaction: transaction.account_id == travel.id
            )
            .include(lambda transaction: transaction.account)
            .all()
        )

        assert on_travel[0].account is travel
        assert on_travel[1].account is on_travel[0].account
        # --8<-- [end:identity]

    async with engines.session():
        # --8<-- [start:accumulate]
        market_run = await Transaction.get(3)  # nothing populated yet

        await (
            Transaction.select().include(lambda transaction: transaction.account).all()
        )
        await (
            Transaction.select().include(lambda transaction: transaction.category).all()
        )

        assert market_run.account.label == "Checking"
        assert market_run.category.name == "Groceries"
        # --8<-- [end:accumulate]

        # --8<-- [start:refresh-rule]
        from_checking = await (
            Transaction.where(lambda transaction: transaction.id == 5)
            .include(lambda transaction: transaction.account)
            .first()
        )
        assert from_checking.account.label == "Savings"

        # Move the row to another account behind the ORM's back, then fetch it.
        # ($1 and $2 are placeholders SQLite and PostgreSQL both accept.)
        await execute('UPDATE "transaction" SET account_id = $1 WHERE id = $2', 1, 5)
        await Transaction.get(5)  # refreshes the instance in place

        pending = from_checking.account  # awaitable again
        assert inspect.isawaitable(pending)
        assert (await pending).label == "Checking"
        # --8<-- [end:refresh-rule]

        # --8<-- [start:limits]
        try:
            Account.select().include(lambda account: account.transactions)
        except TypeError as error:
            assert "reverse (BackRef) or many-to-many" in str(error)
        else:
            raise AssertionError("including a reverse relation must raise")

        try:
            Transaction.select().include(
                lambda transaction: transaction.account
            ).select(lambda transaction: (transaction.id, transaction.memo))
        except ValueError as error:
            assert "traversed projection" in str(error)
        else:
            raise AssertionError("include() with a projection must raise")

        try:
            await (
                Transaction.select()
                .include(lambda transaction: transaction.account)
                .delete()
            )
        except ValueError as error:
            assert "does not support include()" in str(error)
        else:
            raise AssertionError("delete() on an included query must raise")
        # --8<-- [end:limits]

    print("populated_relations example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
