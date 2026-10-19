"""What the migration calls raise.

```python
await ferro.connect(url)
await ferro.migrations.require_applied()
# PendingMigrationsError: ferro.migrations: this database is behind its migrations.
#   pending  0002_add_teams
# Run `ferro migrate up` (or `await ferro.migrations.up()`) before serving.
```

Every refusal a migration call makes is a `MigrationRefused`, so one
`except` catches them all and the CLI renders any of them as exit 1. The
states an application branches on have their own subclasses:
`PendingMigrationsError` (the database is behind the checkout) and
`DatabaseAheadError` (the database holds migrations the checkout lacks),
here; a run planner refusal with a class of its own (`AlreadyTrackedError`,
`AppliedAboveBaselineError`) is a `RunRefused`, beside it in
`ferro.migrations.report`.
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
    """A migration call's refusal, naming the fix.

    The base of every refusal: a run's, `ferro migrate check`'s problems, a
    configuration that names no single database, and the subclasses an
    application branches on.
    """

    def __init__(
        self,
        message: str,
        *,
        report: RunReport | CheckReport | StatusReport | None = None,
    ) -> None:
        super().__init__(message)
        self.report = report
        """The report of the call that refused, or `None` when it has none.

        The `RunReport` of a refused `up` (what it did before it stopped,
        and the refusal itself as `report.refused`), the `CheckReport` whose
        problems `raise_for_problems()` raised, or the `StatusReport` of the
        database a baseline refusal is about.
        """


class PendingMigrationsError(MigrationRefused):
    """The refusal `require_applied()` raises for a database behind its migrations."""

    def __init__(self, pending: Sequence[str], refusals: Sequence[str] = ()) -> None:
        self.pending = list(pending)
        """Each migration with a step still to apply, as `NNNN_<name>`."""
        self.refusals = list(refusals)
        """The tracking table's own refusals, each the text `ferro migrate up` would print.

        An interrupted run or an edited applied file, for example.
        """
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
    """The refusal `up()` and `require_applied()` raise for a database ahead of the checkout.

    The database has applied migrations this checkout does not have.
    `allow_ahead="expand-only"` lets the database through when
    `contract_steps` is empty, and its message names the first of them;
    pass `allow_ahead=True` to run beside any of them, as a rolling deploy
    with a fixed old version does. See
    [Deploying migrations](../guide/schema/deploying.md). Its `report` is
    the `RunReport` of the refused `up`, or `None` for `require_applied()`.
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
        """The migrations the database has applied that the checkout lacks, as `NNNN_<name>`."""
        self.contract_steps = list(contract_steps)
        """Each of their steps whose class is not `expand`, as `NNNN_<name>:NN`.

        Each is a step the previous release's writes may fail beside.
        """
        super().__init__(
            f"{message}\nPass allow_ahead=True to run beside them (a rolling deploy).",
            report=report,
        )
