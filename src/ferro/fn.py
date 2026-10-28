"""SQL functions for a generated column's expression.

Each function here is one the database evaluates, with the same result on
SQLite and Postgres. Call it on a column inside a `Field(generated=...)`
lambda; it builds an expression the database computes, never a Python value.

Examples:
    ```python {test=run prelude=reference}
    from ferro import Field, Model, create_tables, fn


    class Transaction(Model):
        id: int | None = Field(default=None, primary_key=True)
        amount_minor: int
        direction: int = Field(
            generated=lambda transaction: fn.sign(transaction.amount_minor)
        )


    await create_tables()
    transaction = await Transaction.create(amount_minor=-250)
    assert transaction.direction == -1
    ```
"""

from __future__ import annotations

import json
from typing import Any

from .query.nodes import FuncValueExpr

__all__ = ["sign"]


def sign(value: Any) -> Any:
    """Return -1, 0 or 1 by the sign of a number.

    Args:
        value: A numeric column or expression.

    Returns:
        An expression the database computes: `-1` for a negative value, `0`
        for zero and `1` for a positive one.
    """
    return FuncValueExpr("sign", (value,))


def __getattr__(name: str) -> Any:
    if name.startswith("__"):
        raise AttributeError(name)
    from ._core import _value_function_table

    names = ", ".join(entry["name"] for entry in json.loads(_value_function_table()))
    raise AttributeError(
        f"ferro.fn has no function {name!r}; the functions a generated column "
        f"can call are: {names}",
        name=name,
    )
