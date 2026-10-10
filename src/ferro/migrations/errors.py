"""What the migration calls raise (ADR-0038).

```python
await ferro.connect(url)
await ferro.migrations.require_applied()
# PendingMigrationsError: ferro.migrations: this database is behind its migrations.
#   pending  0002_add_teams
# Run `ferro migrate up` (or `await ferro.migrations.up()`) before serving.
```

Every refusal a migration call makes is a :class:`MigrationRefused`, so one
``except`` catches them all and the CLI renders any of them as exit 1. The
states an application branches on have their own subclasses:
:class:`PendingMigrationsError` (the database is behind the checkout),
:class:`DatabaseAheadError` (the database holds migrations the checkout
lacks), :class:`AlreadyTrackedError` (``baseline()`` on a tracked database)
and :class:`AppliedAboveBaselineError` (``remove_baseline()`` under a run
applied above the baseline).

```python
try:
    await ferro.migrations.baseline()
except ferro.migrations.AlreadyTrackedError:
    pass  # another pre-deploy adopted it first
await ferro.migrations.up()
```
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from ..exceptions import FerroError

if TYPE_CHECKING:
    from .generate import CheckReport
    from .report import StatusReport
    from .runner import RunReport

__all__ = [
    "AlreadyTrackedError",
    "AppliedAboveBaselineError",
    "DatabaseAheadError",
    "MigrationRefused",
    "PendingMigrationsError",
]


class MigrationRefused(FerroError):
    """A migration call refused, naming the fix.

    The base of every refusal: a run's (``RunRefused``), ``ferro migrate
    check``'s problems (``MigrationsCheckError``), a configuration that
    names no single database, and the two below.

    ``report`` is the report of the call that refused, when it has one: the
    :class:`~ferro.migrations.runner.RunReport` of a refused ``up`` or
    ``down`` (what it did before it stopped, and the refusal itself as
    ``report.refused``), the ``CheckReport`` whose problems
    ``raise_for_problems()`` raised, or the
    :class:`~ferro.migrations.report.StatusReport` of the database a
    baseline refusal is about. ``None`` otherwise.
    """

    def __init__(
        self,
        message: str,
        *,
        report: RunReport | CheckReport | StatusReport | None = None,
    ) -> None:
        super().__init__(message)
        self.report = report


class PendingMigrationsError(MigrationRefused):
    """``require_applied()``: the database is not at the head of its migrations.

    ``pending`` names each migration with a step still to apply
    (``NNNN_<name>``); ``refusals`` carries the tracking table's own
    refusals (an interrupted run, an edited applied file, ...), each the
    text ``ferro migrate up`` would print.
    """

    def __init__(self, pending: Sequence[str], refusals: Sequence[str] = ()) -> None:
        self.pending = list(pending)
        self.refusals = list(refusals)
        lines = ["ferro.migrations: this database is behind its migrations."]
        lines += [f"  pending  {name}" for name in self.pending]
        lines += self.refusals
        if self.pending and not self.refusals:
            lines.append(
                "Run `ferro migrate up` (or `await ferro.migrations.up()`) before "
                "serving."
            )
        super().__init__("\n".join(lines))


class DatabaseAheadError(MigrationRefused):
    """``up()`` / ``require_applied()``: the database has applied migrations
    this checkout does not have (``ahead``, ``NNNN_<name>``).

    Pass ``allow_ahead=True`` to run beside them, as a rolling deploy does.
    """

    def __init__(
        self, ahead: Sequence[str], message: str, *, report: RunReport | None = None
    ) -> None:
        self.ahead = list(ahead)
        super().__init__(
            f"{message}\nPass allow_ahead=True to run beside them (a rolling deploy).",
            report=report,
        )


class AlreadyTrackedError(MigrationRefused):
    """``baseline()``: the database is already tracked; its tracking table
    holds step records, and a baseline records only on a database that has
    none (ADR-0031). Nothing was recorded.

    Two pre-deploys that adopt one database at once both find no records and
    both call ``baseline()``; the run lock lets one record, and the other
    raises this. ``applied`` names every migration the records name
    (``NNNN_<name>``, in order), ``head`` the newest of them (both as the
    core decided the refusal), and ``report`` is the database's
    :class:`~ferro.migrations.report.StatusReport`, so the caller carries on
    to ``up()``::

        try:
            await ferro.migrations.baseline()
        except ferro.migrations.AlreadyTrackedError:
            pass
        await ferro.migrations.up()
    """

    def __init__(
        self,
        message: str,
        *,
        applied: Sequence[str],
        head: str | None,
        report: StatusReport | None = None,
    ) -> None:
        self.applied = list(applied)
        self.head = head
        super().__init__(message, report=report)


class AppliedAboveBaselineError(MigrationRefused):
    """``remove_baseline()``: a run applied migrations above the baseline
    (``above``, ``NNNN_<name>``); removing it under them would leave applied
    migrations above pending ones. The message names the ``ferro migrate
    down --to`` that reverts them; nothing was removed. ``report`` is the
    database's :class:`~ferro.migrations.report.StatusReport`.
    """

    def __init__(
        self,
        message: str,
        *,
        above: Sequence[str],
        report: StatusReport | None = None,
    ) -> None:
        self.above = list(above)
        super().__init__(message, report=report)
