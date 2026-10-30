"""What an auto-migrate pass did: the value `ferro.migrate()` and `ferro.create_tables()` return.

The report is built from what the pass actually executed, never from its
plan, so it cannot list a statement that did not run. A pass that fails
partway raises its usual error with `.report` set to what committed before
the failure, the failing statement left out. See
[Auto-migrate](../guide/schema/auto-migrate.md).
"""

from __future__ import annotations

import json
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any, Literal, TypeVar

__all__ = [
    "ExecutedStatement",
    "PassReport",
    "Report",
    "Role",
    "Subject",
]

Role = Literal["schema", "lock_timeout", "probe"]
"""What an executed statement was for: `"schema"`, `"lock_timeout"` or `"probe"`.

`"schema"` is the create pass, the enum type statements and the
reconciliation. `"lock_timeout"` is a `SET LOCAL lock_timeout` (or `SET` /
`RESET`) the pass wraps each Postgres unit in. `"probe"` is a read that only
decides what the pass reports, such as SQLite looking for rows that hold an
enum label being renamed.
"""

_PASS_REPORT_ATTR = "_ferro_pass_report"
"""The attribute the Rust core sets on a failed pass's error (the report as
JSON); read once into ``.report``."""


@dataclass(frozen=True)
class ExecutedStatement:
    """One statement the pass sent to the database."""

    subject: str
    """The table or enum type the statement belongs to."""
    sql: str
    """The statement, exactly as it ran."""
    role: Role
    """What it was for: `"schema"` (DDL), `"lock_timeout"` or `"probe"` (a read that changes nothing)."""


@dataclass(frozen=True)
class Subject:
    """What a [`Report`][ferro.Report] is about.

    The modelset as a whole, one table, one column, or one enum type. Only the
    fields its `scope` names are set.
    """

    scope: Literal["modelset", "table", "column", "enum_type"]
    """Which of the four it is: `"modelset"`, `"table"`, `"column"` or `"enum_type"`."""
    table: str | None = None
    """The table, for the `"table"` and `"column"` scopes; `None` otherwise."""
    column: str | None = None
    """The column, for the `"column"` scope; `None` otherwise."""
    type_name: str | None = None
    """The Postgres enum type, for the `"enum_type"` scope; `None` otherwise."""

    @classmethod
    def _from_wire(cls, wire: dict[str, Any]) -> Subject:
        return cls(
            scope=wire["scope"],
            table=wire.get("table"),
            column=wire.get("column"),
            type_name=wire.get("type_name"),
        )


@dataclass(frozen=True)
class Report:
    """One warning a pass raised, with a kind code can match on.

    `str(report)` is the sentence the pass printed. The
    [Auto-migrate](../guide/schema/auto-migrate.md) guide lists every kind.
    """

    kind: str
    """The warning's kind, such as `"LeftoverChecks"`, `"RefusedConversion"` or `"RunLockWait"`."""
    subject: Subject
    """What the warning is about."""
    text: str
    """The sentence the pass printed and raised as a `UserWarning`."""
    recurs: bool
    """Whether every pass raises it again until someone acts."""

    def __str__(self) -> str:
        return self.text

    @classmethod
    def _from_wire(cls, wire: dict[str, Any]) -> Report:
        kind = wire["kind"]
        name = kind if isinstance(kind, str) else next(iter(kind))
        return cls(
            kind=name,
            subject=Subject._from_wire(wire["subject"]),
            text=wire["text"],
            recurs=wire["recurs"],
        )


@dataclass(frozen=True)
class PassReport:
    """What one auto-migrate pass sent to the database and the warnings it raised.

    Every warning is also raised as a Python `UserWarning`.

    Examples:
        ```python {test=run prelude=reference}
        import ferro

        await ferro.raw.execute('DROP TABLE "post"')
        report = await ferro.create_tables()
        print([(statement.subject, statement.role) for statement in report.statements])
        # [('post', 'schema')]
        print(report.warnings)
        # ()
        ```
    """

    statements: tuple[ExecutedStatement, ...] = ()
    """Every statement the pass sent, in execution order."""
    warnings: tuple[Report, ...] = ()
    """Every warning the pass raised, in the order raised."""

    @classmethod
    def _from_json(cls, text: str) -> PassReport:
        wire = json.loads(text)
        return cls(
            statements=tuple(
                ExecutedStatement(s["subject"], s["sql"], s["role"])
                for s in wire["statements"]
            ),
            warnings=tuple(Report._from_wire(w) for w in wire["warnings"]),
        )


_T = TypeVar("_T")


async def _carrying_report(call: Awaitable[_T]) -> _T:
    """Await a Rust pass entry point; a pass error leaves with ``.report`` set."""
    try:
        return await call
    except BaseException as error:
        wire = getattr(error, _PASS_REPORT_ATTR, None)
        if isinstance(wire, str):
            delattr(error, _PASS_REPORT_ATTR)
            setattr(error, "report", PassReport._from_json(wire))  # noqa: B010
        raise


async def _run_pass(call: Awaitable[str]) -> PassReport:
    """Await a Rust pass entry point that returns the report's JSON."""
    return PassReport._from_json(await _carrying_report(call))
