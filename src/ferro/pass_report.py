"""What an auto-migrate pass did: the value ``ferro.migrate()`` and
``ferro.create_tables()`` return (ADR-0049).

```python
report = await ferro.migrate()
[(s.subject, s.sql) for s in report.statements if s.role == "schema"]
# [('author', 'ALTER TABLE "author" ADD COLUMN "slug" TEXT')]
[(w.kind, str(w)) for w in report.warnings]
# []
```

The Rust core builds the report from what its DDL executor actually ran,
never from the plan, so it cannot describe a statement that did not execute.
A pass that fails partway raises its usual error with ``.report`` set: what
committed before the failure, the failing statement left out.
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
"""What an executed statement was for: ``"schema"`` (the create pass, the
type statements, the reconciliation), ``"lock_timeout"`` (the ``SET LOCAL
lock_timeout`` / ``SET`` / ``RESET`` lines of ADR-0044) or ``"probe"`` (the
SQLite label row probe of ADR-0047)."""

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
    """What it was for (see ``Role``)."""


@dataclass(frozen=True)
class Subject:
    """What a ``Report`` is about: the modelset as a whole, one table, one
    column, or one enum type. Only the fields of its ``scope`` are set."""

    scope: Literal["modelset", "table", "column", "enum_type"]
    table: str | None = None
    column: str | None = None
    type_name: str | None = None

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
    """One warning, typed (ADR-0052): the planner's, the renderer's, or the
    pass's own. ``str(report)`` is the sentence printed.

    ``kind`` is the kind's name: the planner's (``"LeftoverChecks"``,
    ``"HintRefused"``, ...), the renderer's (``"RefusedConversion"``,
    ``"SqliteInPlace"``, ``"PrimaryKeyKept"``, ``"RowSecuritySkipped"``) or
    the pass's (``"PendingTableRename"``, ``"StrandedLabelRename"``,
    ``"RowSecurityUnderMigrator"``, ``"RunLockWait"``, ``"DdlLockRetry"``).
    ``recurs`` says whether it is raised on every pass until someone acts.
    """

    kind: str
    subject: Subject
    text: str
    recurs: bool

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
    """Every statement an auto-migrate pass sent to the database, in
    execution order, and every warning it raised, in the order raised.

    Every warning is also raised as a Python ``UserWarning``, as before; the
    report lists it as well.
    """

    statements: tuple[ExecutedStatement, ...] = ()
    warnings: tuple[Report, ...] = ()

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
    """Await a Rust pass door; a pass error leaves with ``.report`` set."""
    try:
        return await call
    except BaseException as error:
        wire = getattr(error, _PASS_REPORT_ATTR, None)
        if isinstance(wire, str):
            delattr(error, _PASS_REPORT_ATTR)
            setattr(error, "report", PassReport._from_json(wire))  # noqa: B010
        raise


async def _run_pass(call: Awaitable[str]) -> PassReport:
    """Await a Rust pass door that returns the report's JSON."""
    return PassReport._from_json(await _carrying_report(call))
