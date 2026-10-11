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
:class:`PendingMigrationsError` (the database is behind the checkout) and
:class:`DatabaseAheadError` (the database holds migrations the checkout
lacks), here; a run planner refusal with a class of its own
(``AlreadyTrackedError``, ``AppliedAboveBaselineError``) is a ``RunRefused``,
beside it in :mod:`ferro.migrations.report`.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from ..exceptions import FerroError

if TYPE_CHECKING:
    from .generate import CheckReport
    from .report import StatusReport
    from .runner import RunReport

__all__ = ["DatabaseAheadError", "MigrationRefused", "PendingMigrationsError"]


class MigrationRefused(FerroError):
    """A migration call refused, naming the fix.

    The base of every refusal: a run's (``RunRefused``), ``ferro migrate
    check``'s problems (``MigrationsCheckError``), a configuration that
    names no single database, and the subclasses an application branches
    on.

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

    ``contract_steps`` names each of their steps whose class is not
    ``expand`` (``NNNN_<name>:NN``, ADR-0054): a step the previous release's
    writes may fail beside. ``allow_ahead="expand-only"`` lets the database
    through when it is empty, and its message names the first; pass
    ``allow_ahead=True`` to run beside any of them, as a rolling deploy with
    a fixed old version does.
    """

    def __init__(
        self,
        ahead: Sequence[str],
        message: str,
        *,
        report: RunReport | None = None,
        contract_steps: Sequence[str] = (),
    ) -> None:
        self.ahead = list(ahead)
        self.contract_steps = list(contract_steps)
        super().__init__(
            f"{message}\nPass allow_ahead=True to run beside them (a rolling deploy).",
            report=report,
        )
