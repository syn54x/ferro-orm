"""Run an auto-migrate pass the way ``connect(..., auto_migrate=…)`` does and
keep its ``PassReport`` (ADR-0049).

``connect()`` returns nothing, so a test that wants to assert what the pass
executed opens the connection plainly and runs the same pass through the door
that returns the report: ``create_tables()`` for ``auto_migrate=True``,
``migrate(updates=…, destructive=…)`` for the reconciliation flags. Both run
``connect()``'s own pass (one ``internal_migrate`` behind all three doors);
only the run-lock waiting warning names a different call.

```python
report = await auto_migrate(db_url, updates=True)
assert schema_sql(report) == ['ALTER TABLE "author" ADD COLUMN "slug" TEXT']
assert warning_texts(report) == []
```
"""

from __future__ import annotations

import warnings
from typing import Any, TypeVar

import ferro
from ferro import PassReport, connect, reset_engine

_T = TypeVar("_T")


async def auto_migrate(
    url: str,
    *,
    updates: bool = False,
    destructive: bool = False,
    **connect_kwargs: Any,
) -> PassReport:
    """``connect(url, auto_migrate=True, migrate_updates=updates,
    migrate_destructive=destructive, **connect_kwargs)``, returning the pass's
    report. A pass that fails leaves no connection behind, as a failed
    ``connect()`` does, and raises with ``.report`` set."""
    await connect(url, **connect_kwargs)
    try:
        if updates or destructive:
            return await ferro.migrate(
                using=connect_kwargs.get("name"), updates=True, destructive=destructive
            )
        return await ferro.create_tables(using=connect_kwargs.get("name"))
    except BaseException:
        reset_engine()
        raise


async def warned_auto_migrate(url: str, **kwargs: Any) -> PassReport:
    """``auto_migrate(url, **kwargs)`` with every ``UserWarning`` the pass
    raises captured, never escaping to the test run: the captured texts are
    the report's warnings, in order, each as the pass prints it
    (``ferro auto-migrate: <text>``). Any other warning category is raised
    again, as it was."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        report = await auto_migrate(url, **kwargs)
    raised = []
    for warning in caught:
        if issubclass(warning.category, UserWarning):
            raised.append(str(warning.message))
        else:
            warnings.warn_explicit(
                warning.message, warning.category, warning.filename, warning.lineno
            )
    assert raised == [f"ferro auto-migrate: {w}" for w in report.warnings]
    return report


def schema_sql(report: PassReport) -> list[str]:
    """The pass's schema statements, in execution order."""
    return [s.sql for s in report.statements if s.role == "schema"]


def schema_steps(report: PassReport) -> list[tuple[str, str]]:
    """The pass's schema statements as ``(subject, sql)``, in execution order."""
    return [(s.subject, s.sql) for s in report.statements if s.role == "schema"]


def warning_texts(report: PassReport) -> list[str]:
    """Each warning's sentence, in the order raised."""
    return [str(w) for w in report.warnings]


def on(url: str, *, sqlite: _T, postgres: _T) -> _T:
    """``sqlite`` or ``postgres``, by the dialect ``url`` connects to."""
    return sqlite if url.startswith("sqlite") else postgres
