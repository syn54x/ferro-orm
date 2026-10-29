"""SQL functions for a generated column's expression.

Each function here is one the database evaluates, with the same result on
SQLite and Postgres. Call it on a column inside a `Field(generated=...)`
lambda, or on either side of a [`Check`][ferro.Check] comparison; it builds
an expression the database computes, never a Python value. An expression
combines these functions with columns, literals, `+ - * /` and unary `-`.
[Generated columns](../guide/models-and-fields.md#generated-columns) lists
the rules an expression must follow to store the same value on both
databases.

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

__all__ = [
    "abs",
    "coalesce",
    "day",
    "greatest",
    "least",
    "length",
    "month",
    "sign",
    "year",
]


def sign(value: Any) -> Any:
    """Return -1, 0 or 1 by the sign of a number.

    Args:
        value: An integer, float or decimal column or expression.

    Returns:
        The expression the database computes, of the argument's type: `-1`
            for a negative value, `0` for zero and `1` for a positive one.
    """
    return FuncValueExpr("sign", (value,))


def abs(value: Any) -> Any:  # noqa: A001 - the SQL function's name
    """Return a number without its sign.

    Args:
        value: An integer, float or decimal column or expression.

    Returns:
        The expression the database computes, of the argument's type.
    """
    return FuncValueExpr("abs", (value,))


def least(first: Any, second: Any, *more: Any) -> Any:
    """Return the smallest of two or more numbers or dates.

    Every argument must be non-nullable, since the two databases disagree on
    a `NULL`; wrap a nullable column in [`coalesce`][ferro.fn.coalesce].
    Text is not accepted: Postgres orders it by collation and SQLite byte by
    byte.

    Args:
        first: An integer, float, decimal or date column or expression.
        second: A value of the same type.
        *more: More values of the same type.

    Returns:
        The expression the database computes, of the arguments' type.
    """
    return FuncValueExpr("least", (first, second, *more))


def greatest(first: Any, second: Any, *more: Any) -> Any:
    """Return the largest of two or more numbers or dates.

    Every argument must be non-nullable, as for [`least`][ferro.fn.least].

    Args:
        first: An integer, float, decimal or date column or expression.
        second: A value of the same type.
        *more: More values of the same type.

    Returns:
        The expression the database computes, of the arguments' type.
    """
    return FuncValueExpr("greatest", (first, second, *more))


def coalesce(first: Any, second: Any, *more: Any) -> Any:
    """Return the first argument that is not `NULL`.

    Args:
        first: An integer, float, decimal, text, date or datetime column or
            expression.
        second: A value of the same type, such as a literal fallback.
        *more: More values of the same type.

    Returns:
        The expression the database computes, of the arguments' type. It is
            `NULL` only when every argument is.
    """
    return FuncValueExpr("coalesce", (first, second, *more))


def length(value: Any) -> Any:
    """Return the number of characters in a text.

    Args:
        value: A text column or expression.

    Returns:
        The expression the database computes, an integer.
    """
    return FuncValueExpr("length", (value,))


def year(value: Any) -> Any:
    """Return the year of a date.

    Args:
        value: A date column. A datetime is refused: its year depends on
            the session's time zone.

    Returns:
        The expression the database computes, an integer.
    """
    return FuncValueExpr("year", (value,))


def month(value: Any) -> Any:
    """Return the month of a date, from 1 to 12.

    Args:
        value: A date column. A datetime is refused: its month depends on
            the session's time zone.

    Returns:
        The expression the database computes, an integer.
    """
    return FuncValueExpr("month", (value,))


def day(value: Any) -> Any:
    """Return the day of the month of a date, from 1 to 31.

    Args:
        value: A date column. A datetime is refused: its day depends on the
            session's time zone.

    Returns:
        The expression the database computes, an integer.
    """
    return FuncValueExpr("day", (value,))


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
