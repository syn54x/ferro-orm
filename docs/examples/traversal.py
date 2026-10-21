"""Runnable companion to "Querying across relationships" in the Queries guide
(docs/pages/guide/queries.md).

Runs every traversal query the guide shows against the guide's ledger
(declared in predicates.py, rows from _ledger_seed.py) and asserts the rows
that come back.
"""

import asyncio
from datetime import date

from _ledger_seed import seed
import predicates
from predicates import Account, Category, Owner, Tag, Transaction, Transfer

from ferro import connect, engines


async def main() -> None:
    await connect("sqlite::memory:", auto_migrate=True)

    async with engines.session():
        await seed(vars(predicates))

        # --8<-- [start:basic]
        on_savings = await Transaction.where(
            lambda transaction: transaction.account.label == "Savings"
        ).all()
        assert [transaction.memo for transaction in on_savings] == ["From checking"]
        # --8<-- [end:basic]

        # --8<-- [start:multi-hop]
        graces = await Transaction.where(
            lambda transaction: transaction.account.owner.email == "grace@example.com"
        ).all()
        assert {transaction.memo for transaction in graces} == {
            "Train to Lyon",
            "Dinner in Lyon",
        }
        # --8<-- [end:multi-hop]

        # --8<-- [start:typo]
        try:
            Transaction.where(
                lambda transaction: transaction.account.owner.emial == "ada@example.com"
            )
        except AttributeError as error:
            assert str(error) == (
                "Owner has no queryable column 'emial'. Did you mean 'email'? "
                "Valid columns: email, id, name."
            )
        else:
            raise AssertionError("a misspelled hop must raise AttributeError")
        # --8<-- [end:typo]

        # --8<-- [start:inner-narrows]
        not_adas = await Transaction.where(
            lambda transaction: transaction.account.owner.name != "Ada Lovelace"
        ).all()
        # Office coffee is on Petty cash, which has no owner: it is not here.
        assert {transaction.memo for transaction in not_adas} == {
            "Train to Lyon",
            "Dinner in Lyon",
        }
        # --8<-- [end:inner-narrows]

        # --8<-- [start:inner-or]
        graces_or_coffee = await Transaction.where(
            lambda transaction: (
                (transaction.account.owner.name == "Grace Hopper")
                | (transaction.memo == "Office coffee")
            )
        ).all()
        # The join drops Office coffee before the OR is evaluated.
        assert {transaction.memo for transaction in graces_or_coffee} == {
            "Train to Lyon",
            "Dinner in Lyon",
        }
        # --8<-- [end:inner-or]

        # --8<-- [start:count]
        adas_count = await Transaction.where(
            lambda transaction: transaction.account.owner.name == "Ada Lovelace"
        ).count()
        assert adas_count == 6
        # --8<-- [end:count]

        # --8<-- [start:order-by]
        by_account = (
            await Transaction.select()
            .order_by(lambda transaction: transaction.account.label, "desc")
            .order_by(lambda transaction: transaction.id)
            .limit(4)
            .all()
        )
        assert [transaction.memo for transaction in by_account] == [
            "Train to Lyon",  # Travel
            "Dinner in Lyon",  # Travel
            "From checking",  # Savings
            "Office coffee",  # Petty cash
        ]
        # --8<-- [end:order-by]

        # --8<-- [start:order-by-narrows]
        by_owner = (
            await Transaction.select()
            .order_by(lambda transaction: transaction.account.owner.name)
            .order_by(lambda transaction: transaction.id)
            .all()
        )
        assert len(by_owner) == 8  # Office coffee has no owner to sort by
        # --8<-- [end:order-by-narrows]

        # --8<-- [start:traversed-paging]
        by_label = (
            Transaction.select()
            .order_by(lambda transaction: transaction.account.label)
            .order_by(lambda transaction: transaction.id)
        )
        next_two = await by_label.after(("Petty cash", 8)).limit(2).all()
        assert [transaction.memo for transaction in next_two] == [
            "From checking",
            "Train to Lyon",
        ]
        # --8<-- [end:traversed-paging]

        # --8<-- [start:composed]
        adas_largest_debits = (
            await Transaction.where(
                lambda transaction: transaction.account.owner.name == "Ada Lovelace"
            )
            .where(lambda transaction: transaction.amount < 0)
            .order_by(lambda transaction: transaction.amount)
            .limit(2)
            .all()
        )
        assert [transaction.memo for transaction in adas_largest_debits] == [
            "Rent",
            "To savings",
        ]
        # --8<-- [end:composed]

        # --8<-- [start:shadow-column]
        savings = await Account.get(2)
        on_savings = await Transaction.where(
            lambda transaction: transaction.account_id == savings.id
        ).all()
        assert [transaction.memo for transaction in on_savings] == ["From checking"]
        # --8<-- [end:shadow-column]

        # --8<-- [start:instance-eq]
        on_savings = await Transaction.where(
            lambda transaction: transaction.account == savings
        ).all()
        elsewhere = await Transaction.where(
            lambda transaction: transaction.account != savings
        ).count()
        assert [transaction.memo for transaction in on_savings] == ["From checking"]
        assert elsewhere == 8
        # --8<-- [end:instance-eq]

        # --8<-- [start:deep-instance-eq]
        grace = await Owner.get(2)
        graces = await Transaction.where(
            lambda transaction: transaction.account.owner == grace
        ).all()
        assert {transaction.memo for transaction in graces} == {
            "Train to Lyon",
            "Dinner in Lyon",
        }
        # --8<-- [end:deep-instance-eq]

        # --8<-- [start:unpersisted]
        try:
            Transaction.where(
                lambda transaction: transaction.account == Account(label="Brokerage")
            )
        except ValueError as error:
            assert str(error).startswith(
                "cannot compare relation 'account' to an unpersisted Account instance"
            )
        else:
            raise AssertionError("an unpersisted instance must be refused")
        # --8<-- [end:unpersisted]

        # --8<-- [start:existence]
        owned = await Account.select().join(lambda account: account.owner).all()
        assert {account.label for account in owned} == {"Checking", "Savings", "Travel"}
        # --8<-- [end:existence]

        # --8<-- [start:is-null]
        ownerless = await Account.where(lambda account: account.owner == None).all()
        owned_count = await Account.where(lambda account: account.owner != None).count()
        assert [account.label for account in ownerless] == ["Petty cash"]
        assert owned_count == 3
        # --8<-- [end:is-null]

        # --8<-- [start:left-join]
        by_owner_name = (
            await Account.select()
            .left_join(lambda account: account.owner)
            .order_by(lambda account: account.owner.name)
            .order_by(lambda account: account.id)
            .all()
        )
        assert [account.label for account in by_owner_name] == [
            "Checking",  # Ada Lovelace
            "Savings",  # Ada Lovelace
            "Travel",  # Grace Hopper
            "Petty cash",  # no owner: NULL sorts last
        ]
        # --8<-- [end:left-join]

        # --8<-- [start:two-fk-query]
        checking_to_savings = await Transfer.where(
            lambda transfer: (
                (transfer.outflow.account.label == "Checking")
                & (transfer.inflow.account.label == "Savings")
            )
        ).all()
        assert [
            (transfer.outflow_id, transfer.inflow_id)
            for transfer in checking_to_savings
        ] == [(4, 5)]
        # --8<-- [end:two-fk-query]

        # --8<-- [start:self-fk-query]
        food_categories = await Category.where(
            lambda category: category.parent.name == "Food"
        ).all()
        assert {category.name for category in food_categories} == {
            "Groceries",
            "Dining",
        }
        # --8<-- [end:self-fk-query]

        # --8<-- [start:m2m-query]
        reimbursable = await Tag.where(lambda tag: tag.name == "reimbursable").first()
        assert reimbursable is not None

        reimbursable_meals = await reimbursable.transactions.where(
            lambda transaction: transaction.category.name == "Dining"
        ).all()
        assert [transaction.memo for transaction in reimbursable_meals] == [
            "Dinner in Lyon"
        ]
        # --8<-- [end:m2m-query]

        # --8<-- [start:reverse-filter]
        checking = await Account.get(1)

        largest_debits = (
            await checking.transactions.where(
                lambda transaction: transaction.amount < 0
            )
            .order_by(lambda transaction: transaction.amount)
            .limit(2)
            .all()
        )
        checking_count = await checking.transactions.count()

        assert [transaction.memo for transaction in largest_debits] == [
            "Rent",
            "To savings",
        ]
        assert checking_count == 5
        # --8<-- [end:reverse-filter]

        # --8<-- [start:mutate-error]
        try:
            await Transaction.where(
                lambda transaction: transaction.account.label == "Travel"
            ).update(cleared_on=date(2026, 3, 14))
        except ValueError as error:
            assert str(error).startswith(
                "update() does not support relation traversal: portable SQL has "
                "no UPDATE ... JOIN."
            )
        else:
            raise AssertionError("a traversed update() must be refused")
        # --8<-- [end:mutate-error]

        # --8<-- [start:mutate-limitation]
        # Fetch the primary keys through the join...
        travel_ids = [
            transaction.id
            for transaction in await Transaction.where(
                lambda transaction: transaction.account.label == "Travel"
            ).all()
        ]
        # ...then update by that key set, with no join.
        updated = await Transaction.where(
            lambda transaction: transaction.id.in_(travel_ids)
        ).update(cleared_on=date(2026, 3, 14))
        assert updated == 2
        # --8<-- [end:mutate-limitation]

        # --8<-- [start:guardrails]
        try:
            Transaction.where(lambda transaction: transaction.account)
        except TypeError:
            pass
        else:
            raise AssertionError("a bare relation is not a predicate")

        try:
            Transaction.where(
                lambda transaction: (
                    (transaction.amount < 0) and (transaction.cleared_on == None)
                )
            )
        except TypeError as error:
            assert str(error).startswith(
                "QueryNode cannot be used in a boolean context; use & / | to "
                "combine predicates and ~ to negate them"
            )
        else:
            raise AssertionError("`and` between predicates must raise TypeError")
        # --8<-- [end:guardrails]

    print("traversal example ran successfully")


if __name__ == "__main__":
    asyncio.run(main())
