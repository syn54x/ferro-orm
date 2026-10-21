"""The rows of the Queries guide's ledger, shared by its example scripts.

The guide (docs/pages/guide/queries.md) declares one ledger in
``predicates.py`` and its Annotated twin ``predicates_annotated.py``. Both
twins, the other query examples that import ``predicates``, and the
``queries`` prelude add these same rows, so every result the guide shows is
the same wherever it runs.

Two owners, four accounts (``Petty cash`` has no owner), four categories
(``Groceries`` and ``Dining`` sit under ``Food``), nine transactions, one
split transaction with two lines, one transfer between ``Checking`` and
``Savings`` and two tags.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from typing import Any


async def seed(models: Mapping[str, Any]) -> None:
    """Add the ledger's rows through the model classes in ``models``.

    ``models`` maps each class name (``Owner``, ``Account``, ``Category``,
    ``Transaction``, ``SplitLine``, ``Transfer``, ``Tag``) to the class, so
    either declaration style's classes can be seeded.
    """
    Owner = models["Owner"]
    Account = models["Account"]
    Category = models["Category"]
    Transaction = models["Transaction"]
    SplitLine = models["SplitLine"]
    Transfer = models["Transfer"]
    Tag = models["Tag"]

    ada = await Owner.create(name="Ada Lovelace", email="ada@example.com")
    grace = await Owner.create(name="Grace Hopper", email="grace@example.com")

    checking = await Account.create(label="Checking", owner=ada)
    savings = await Account.create(label="Savings", owner=ada)
    travel = await Account.create(label="Travel", owner=grace)
    petty_cash = await Account.create(label="Petty cash")

    food = await Category.create(name="Food")
    groceries = await Category.create(name="Groceries", parent=food)
    dining = await Category.create(name="Dining", parent=food)
    housing = await Category.create(name="Housing")

    async def add(
        memo: str,
        amount: str,
        posted_on: date,
        cleared_on: date | None,
        account: Any,
        category: Any = None,
    ) -> Any:
        return await Transaction.create(
            memo=memo,
            amount=Decimal(amount),
            posted_on=posted_on,
            cleared_on=cleared_on,
            account=account,
            category=category,
        )

    await add("Salary", "3200.00", date(2026, 3, 1), date(2026, 3, 2), checking)
    rent = await add(
        "Rent", "-1450.00", date(2026, 3, 2), date(2026, 3, 3), checking, housing
    )
    await add(
        "Market run", "-86.40", date(2026, 3, 5), date(2026, 3, 6), checking, groceries
    )
    to_savings = await add(
        "To savings", "-500.00", date(2026, 3, 7), date(2026, 3, 8), checking
    )
    from_checking = await add(
        "From checking", "500.00", date(2026, 3, 7), date(2026, 3, 8), savings
    )
    train = await add("Train to Lyon", "-64.00", date(2026, 3, 9), None, travel)
    dinner = await add(
        "Dinner in Lyon", "-48.50", date(2026, 3, 10), None, travel, dining
    )
    await add(
        "Office coffee",
        "-12.75",
        date(2026, 3, 11),
        date(2026, 3, 12),
        petty_cash,
        dining,
    )
    weekly_shop = await add("Weekly shop", "-95.00", date(2026, 3, 12), None, checking)

    await SplitLine.create(
        amount=Decimal("-72.00"), transaction=weekly_shop, category=groceries
    )
    await SplitLine.create(
        amount=Decimal("-23.00"), transaction=weekly_shop, category=dining
    )

    await Transfer.create(outflow=to_savings, inflow=from_checking)

    reimbursable = await Tag.create(name="reimbursable")
    tax = await Tag.create(name="tax")
    await reimbursable.transactions.add(train, dinner)
    await tax.transactions.add(rent)
