"""Raw SQL: `execute`, `fetch_all`, `fetch_one` and the `Transaction` handle.

Bound values are sent as plain primitives, and rows come back as
`dict[str, str | int | float | bool | bytes | None]`: UUID, datetime and JSON
columns come back as strings. For typed rows, use the ORM.

The top-level functions run in the current session, or inside the enclosing
`transaction()` block; the `Transaction` handle a block yields has the same
three methods, bound to its connection. The Raw SQL guide
(`docs/pages/guide/raw-sql.md`) has the bind type table and the Postgres
casts.
"""

from __future__ import annotations

import datetime
import decimal
import enum
import json
import uuid
from typing import Any

from ._core import RouteHandle
from ._core import raw_execute as _raw_execute
from ._core import raw_fetch_all as _raw_fetch_all
from ._core import raw_fetch_one as _raw_fetch_one
from .state import resolve_operation_scope

__all__ = ["execute", "fetch_all", "fetch_one", "Transaction"]


def _marshal(val: Any) -> bool | int | float | str | bytes | None:
    """Convert a Python value into a primitive accepted by the FFI bind path."""
    if val is None:
        return None
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float, str)):
        return val
    if isinstance(val, (bytes, bytearray)):
        return bytes(val)
    if isinstance(val, uuid.UUID):
        return str(val)
    if isinstance(val, (datetime.datetime, datetime.date, datetime.time)):
        return val.isoformat()
    if isinstance(val, decimal.Decimal):
        return str(val)
    if isinstance(val, enum.Enum):
        return _marshal(val.value)
    if isinstance(val, (dict, list)):
        return json.dumps(val)
    raise TypeError(
        f"Cannot bind {type(val).__name__} to raw SQL parameter (value={val!r}). "
        f"Supported: str, int, float, bool, bytes, None, UUID, datetime, date, "
        f"time, Decimal, Enum, dict, list."
    )


def _check_sql(sql: str) -> None:
    if not isinstance(sql, str) or not sql.strip():
        raise ValueError("sql must be a non-empty statement")


def _transaction_or_using(using: str | None, session: Any | None) -> RouteHandle:
    return resolve_operation_scope(using=using, session=session)


async def execute(
    sql: str,
    *args: Any,
    using: str | None = None,
    session: Any | None = None,
    autocommit: bool = False,
) -> int:
    """Run one SQL statement and return the number of rows it changed.

    It runs inside the enclosing `transaction()` block, if any. Outside one,
    two calls may run on different pooled connections; wrap statements that
    need one connection (`SET LOCAL`, an advisory lock) in `transaction()`.
    See [Raw SQL](../guide/raw-sql.md).

    !!! warning "`autocommit=True` drops the session's settings"
        Under the default settings delivery, an `autocommit` statement
        carries none of the session's settings, so a row-security policy
        sees no tenant while it runs. Use it for maintenance statements,
        never for statements that read or write rows a policy protects.

    Args:
        sql: One SQL statement, with `?` placeholders on SQLite and `$1`,
            `$2`, ... on Postgres.
        *args: The values bound to the placeholders, in order: `str`, `int`,
            `float`, `bool`, `bytes`, `None`, `UUID`, `datetime`, `date`,
            `time`, `Decimal`, an `Enum` member, or a `dict` or `list` sent
            as JSON text.
        using: The connection to run on, outside a session. Defaults to
            `None`, the current session's connection.
        session: The session to run in. Defaults to `None`, the one opened
            by the enclosing `async with engines.session():`.
        autocommit: Run the statement outside the transaction Ferro opens for
            each operation in a session with settings, for the Postgres
            statements that refuse one (`CREATE INDEX CONCURRENTLY`,
            `VACUUM`). Defaults to `False`. Inside an explicit
            `transaction()` block it changes nothing. See
            [Statements that refuse a transaction](../guide/raw-sql.md#statements-that-refuse-a-transaction).

    Returns:
        The number of rows the statement changed.

    Raises:
        ValueError: `sql` is empty, or `using` names another connection than
            the session's or the transaction's.
        TypeError: A value in `args` has a type that cannot be bound.
        RuntimeError: No session is open and neither `using` nor `session`
            was given.

    Examples:
        ```python {test=run prelude=reference}
        from ferro import execute

        changed = await execute('UPDATE "post" SET "views" = "views" + 1 WHERE "published" = ?', True)
        assert changed == 2
        ```
    """
    _check_sql(sql)
    marshalled = [_marshal(a) for a in args]
    route = _transaction_or_using(using, session)
    return await _raw_execute(sql, marshalled, route, autocommit)


async def fetch_all(
    sql: str, *args: Any, using: str | None = None, session: Any | None = None
) -> list[dict[str, Any]]:
    """Run one SQL query and return every row as a dict.

    It runs inside the enclosing `transaction()` block, if any. Values come
    back as plain primitives: UUID, datetime and JSON columns as strings.

    Args:
        sql: One SQL statement, with `?` placeholders on SQLite and `$1`,
            `$2`, ... on Postgres.
        *args: The values bound to the placeholders, in order: `str`, `int`,
            `float`, `bool`, `bytes`, `None`, `UUID`, `datetime`, `date`,
            `time`, `Decimal`, an `Enum` member, or a `dict` or `list` sent
            as JSON text.
        using: The connection to run on, outside a session. Defaults to
            `None`, the current session's connection.
        session: The session to run in. Defaults to `None`, the one opened
            by the enclosing `async with engines.session():`.

    Returns:
        One dict per row, keyed by column name, in the query's order.

    Raises:
        ValueError: `sql` is empty, or `using` names another connection than
            the session's or the transaction's.
        TypeError: A value in `args` has a type that cannot be bound.
        RuntimeError: No session is open and neither `using` nor `session`
            was given.

    Examples:
        ```python {test=run prelude=reference}
        from ferro import fetch_all

        rows = await fetch_all('SELECT "title", "views" FROM "post" WHERE "views" > ? ORDER BY "views"', 900)
        assert rows == [
            {"title": "The Education of a Computer", "views": 952},
            {"title": "Notes on the Analytical Engine", "views": 1843},
        ]
        ```
    """
    _check_sql(sql)
    marshalled = [_marshal(a) for a in args]
    route = _transaction_or_using(using, session)
    return await _raw_fetch_all(sql, marshalled, route)


async def fetch_one(
    sql: str, *args: Any, using: str | None = None, session: Any | None = None
) -> dict[str, Any] | None:
    """Run one SQL query and return its first row as a dict, or `None`.

    It runs inside the enclosing `transaction()` block, if any. Ferro does
    not add `LIMIT 1`; write it when more than one row can match, so the
    database stops at the first.

    Args:
        sql: One SQL statement, with `?` placeholders on SQLite and `$1`,
            `$2`, ... on Postgres.
        *args: The values bound to the placeholders, in order: `str`, `int`,
            `float`, `bool`, `bytes`, `None`, `UUID`, `datetime`, `date`,
            `time`, `Decimal`, an `Enum` member, or a `dict` or `list` sent
            as JSON text.
        using: The connection to run on, outside a session. Defaults to
            `None`, the current session's connection.
        session: The session to run in. Defaults to `None`, the one opened
            by the enclosing `async with engines.session():`.

    Returns:
        The first row, keyed by column name, or `None` when no row matched.

    Raises:
        ValueError: `sql` is empty, or `using` names another connection than
            the session's or the transaction's.
        TypeError: A value in `args` has a type that cannot be bound.
        RuntimeError: No session is open and neither `using` nor `session`
            was given.

    Examples:
        ```python {test=run prelude=reference}
        from ferro import fetch_one

        row = await fetch_one('SELECT "name" FROM "author" WHERE "email" = ?', "ada@example.com")
        assert row == {"name": "Ada Lovelace"}
        assert await fetch_one('SELECT "name" FROM "author" WHERE "id" = ?', 99) is None
        ```
    """
    _check_sql(sql)
    marshalled = [_marshal(a) for a in args]
    route = _transaction_or_using(using, session)
    return await _raw_fetch_one(sql, marshalled, route)


class Transaction:
    """The handle `async with transaction() as tx` yields, for raw SQL on its connection.

    Its methods take the same `sql` and `*args` as the top-level
    [`execute`][ferro.execute], [`fetch_all`][ferro.fetch_all] and
    [`fetch_one`][ferro.fetch_one], and always run on the transaction's
    connection. After the block exits, every call raises `RuntimeError`.

    Examples:
        ```python {test=run prelude=reference}
        from ferro import transaction

        async with transaction() as tx:
            await tx.execute('UPDATE "post" SET "views" = 0 WHERE "id" = ?', 1)
            row = await tx.fetch_one('SELECT "views" FROM "post" WHERE "id" = ?', 1)
        assert row == {"views": 0}
        ```
    """

    __slots__ = ("_route",)

    def __init__(self, route: RouteHandle) -> None:
        self._route = route

    async def execute(self, sql: str, *args: Any) -> int:
        """Run one SQL statement on this transaction's connection.

        Args:
            sql: One SQL statement, with `?` placeholders on SQLite and `$1`,
                `$2`, ... on Postgres.
            *args: The values bound to the placeholders, in order.

        Returns:
            The number of rows the statement changed.

        Raises:
            ValueError: `sql` is empty.
            TypeError: A value in `args` has a type that cannot be bound.
            RuntimeError: The transaction's block has exited.
        """
        _check_sql(sql)
        marshalled = [_marshal(a) for a in args]
        return await _raw_execute(sql, marshalled, self._route)

    async def fetch_all(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        """Run one SQL query on this transaction's connection and return every row.

        Args:
            sql: One SQL query, with `?` placeholders on SQLite and `$1`,
                `$2`, ... on Postgres.
            *args: The values bound to the placeholders, in order.

        Returns:
            One dict per row, keyed by column name.

        Raises:
            ValueError: `sql` is empty.
            TypeError: A value in `args` has a type that cannot be bound.
            RuntimeError: The transaction's block has exited.
        """
        _check_sql(sql)
        marshalled = [_marshal(a) for a in args]
        return await _raw_fetch_all(sql, marshalled, self._route)

    async def fetch_one(self, sql: str, *args: Any) -> dict[str, Any] | None:
        """Run one SQL query on this transaction's connection and return its first row.

        Args:
            sql: One SQL query, with `?` placeholders on SQLite and `$1`,
                `$2`, ... on Postgres.
            *args: The values bound to the placeholders, in order.

        Returns:
            The first row, keyed by column name, or `None` when no row matched.

        Raises:
            ValueError: `sql` is empty.
            TypeError: A value in `args` has a type that cannot be bound.
            RuntimeError: The transaction's block has exited.
        """
        _check_sql(sql)
        marshalled = [_marshal(a) for a in args]
        return await _raw_fetch_one(sql, marshalled, self._route)
