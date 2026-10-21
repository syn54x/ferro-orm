"""The statement log: one record on ``ferro.sql`` per statement ferro sends.

The Rust engine measures each statement as it finishes and hands its data to
[`emit`][ferro._sql_log.emit], which writes the record. Rust formats nothing;
this module is the one place a record's message and attributes are built.
"""

from __future__ import annotations

import logging
from typing import Final

LOGGER: Final = logging.getLogger("ferro.sql")
"""The statement log's logger. Set it to `DEBUG` to see every statement."""


class _Redacted:
    """The stand-in for a secret parameter in a record."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "***"

    __str__ = __repr__


REDACTED: Final = _Redacted()
"""A secret parameter as a record shows it: its `repr` and `str` are `***`."""


def emit(record: dict[str, object]) -> None:
    """Write one statement's record on `ferro.sql` at `DEBUG`.

    The message is `<sql> params=<params> <duration>ms`, followed by
    ` failed: <error>` when the statement raised. Every field except `mask`
    is also an attribute of the `LogRecord`, with each masked parameter
    replaced by [`REDACTED`][ferro._sql_log.REDACTED].

    Args:
        record: The statement's data, as the engine hands it over: `sql`,
            `params` (a tuple, `None` in each masked position), `mask` (a
            tuple of booleans, `True` where the parameter is a secret),
            `dialect`, `duration_ms`, `operation`, `subject`, `connection`,
            `rows` (rows affected, or `None` for a fetch) and `error` (the
            database's error text, or `None`).
    """
    mask = record["mask"]
    params = tuple(
        REDACTED if masked else value
        for value, masked in zip(record["params"], mask, strict=True)  # type: ignore[call-overload]
    )
    error = record["error"]
    message = f"{record['sql']} params={params!r} {record['duration_ms']:.1f}ms"
    if error is not None:
        message += f" failed: {error}"
    LOGGER.debug(
        message,
        extra={
            "sql": record["sql"],
            "params": params,
            "dialect": record["dialect"],
            "duration_ms": record["duration_ms"],
            "operation": record["operation"],
            "subject": record["subject"],
            "connection": record["connection"],
            "rows": record["rows"],
            "error": error,
        },
    )
