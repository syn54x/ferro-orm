"""Raw SQL escape hatch — ``execute``, ``fetch_all``, ``fetch_one``.

Raw SQL is an escape hatch. Bind values cross the FFI as wire-close primitives,
and rows come back as ``dict[str, str | int | float | bool | bytes | None]``.
UUID / datetime / JSON columns are returned as strings. **If you want typed
rows, use the ORM.**

Two interchangeable surfaces, both backed by the same Rust functions:

* :class:`Transaction` — handle yielded by ``async with transaction() as tx``.
  Hard-to-misuse path; ``tx.execute(...)`` cannot be spelled outside the tx
  block.
* Top-level :func:`execute` / :func:`fetch_all` / :func:`fetch_one` — auto-pick
  up the active tx via the same ``_CURRENT_TRANSACTION`` ContextVar that
  ``Model.create()`` uses. Outside any tx, they run on a one-off pool
  connection.

See ``docs/api/raw-sql.md`` for the full bind type table and the Postgres cast
cheat-sheet.
"""

from __future__ import annotations

import datetime
import decimal
import enum
import json
import uuid
from typing import Any

from ._core import raw_execute as _raw_execute
from ._core import raw_fetch_all as _raw_fetch_all
from ._core import raw_fetch_one as _raw_fetch_one
from .state import _CURRENT_TRANSACTION

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


async def execute(sql: str, *args: Any) -> int:
    """Run a raw SQL statement, returning rows affected.

    Honors the active ``transaction()`` block via the ``_CURRENT_TRANSACTION``
    ContextVar. Outside any transaction, runs on a one-off pool connection.
    Two consecutive top-level ``execute`` calls outside a transaction may use
    different pool connections — wrap in ``transaction()`` if you need
    connection affinity (e.g. ``SET LOCAL``, advisory locks, ``LISTEN/NOTIFY``).
    """
    _check_sql(sql)
    marshalled = [_marshal(a) for a in args]
    return await _raw_execute(sql, marshalled, _CURRENT_TRANSACTION.get())


async def fetch_all(sql: str, *args: Any) -> list[dict[str, Any]]:
    """Run a raw SQL query and return all rows as a list of dicts.

    Values are wire-close primitives. UUID/datetime/JSON columns come back as
    strings. If you want typed rows, use the ORM.
    """
    _check_sql(sql)
    marshalled = [_marshal(a) for a in args]
    return await _raw_fetch_all(sql, marshalled, _CURRENT_TRANSACTION.get())


async def fetch_one(sql: str, *args: Any) -> dict[str, Any] | None:
    """Run a raw SQL query and return the first row as a dict, or ``None``.

    Callers should ``LIMIT 1`` if the query may return more than one row.
    """
    _check_sql(sql)
    marshalled = [_marshal(a) for a in args]
    return await _raw_fetch_one(sql, marshalled, _CURRENT_TRANSACTION.get())


class Transaction:
    """Handle for a live transaction.

    Obtained via ``async with transaction() as tx``. Methods delegate to the
    top-level :func:`execute` / :func:`fetch_all` / :func:`fetch_one` with this
    transaction's ``tx_id`` set explicitly, so they don't depend on the
    ContextVar state. This makes the connection-affinity invariant
    structurally impossible to violate from inside the ``async with`` block.

    The handle becomes invalid once the ``async with`` block exits — any
    subsequent call raises :class:`RuntimeError`.
    """

    __slots__ = ("_tx_id",)

    def __init__(self, tx_id: str) -> None:
        self._tx_id = tx_id

    async def execute(self, sql: str, *args: Any) -> int:
        _check_sql(sql)
        marshalled = [_marshal(a) for a in args]
        return await _raw_execute(sql, marshalled, self._tx_id)

    async def fetch_all(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        _check_sql(sql)
        marshalled = [_marshal(a) for a in args]
        return await _raw_fetch_all(sql, marshalled, self._tx_id)

    async def fetch_one(self, sql: str, *args: Any) -> dict[str, Any] | None:
        _check_sql(sql)
        marshalled = [_marshal(a) for a in args]
        return await _raw_fetch_one(sql, marshalled, self._tx_id)
