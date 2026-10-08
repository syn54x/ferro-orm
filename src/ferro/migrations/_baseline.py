"""``ferro migrate baseline``: adopt migrations on a database that already
has their schema (ADR-0031).

```text
$ ferro migrate baseline
drift against 0007_nickname:
  user.nickname column is missing
nothing was recorded
$ ferro migrate baseline 0006
recorded 0001_create_author … 0006_add_teams as baseline (14 steps; 2 data steps listed, not run)
$ ferro migrate status
0006_add_teams      applied (baseline)
0007_nickname       pending
```

A database that ``connect(auto_migrate=True)`` or Alembic built already has
the tables ``0001`` creates. ``baseline`` records every step of every
migration through the target as applied without running it, and only after
checking: under the run lock, with no step records yet, the live database
is planned against the target migration's schema snapshot exactly as
``ferro migrate drift`` plans it against the last applied one: both call
:func:`ferro.migrations._drift.against`, so the lines are the same. Any
line means nothing is recorded; there is no flag that records past one.
Live tables the snapshot does not declare (``alembic_version``, a later
migration's tables) are never drift.

``baseline --remove`` deletes the baseline's records again, refused while a
run has applied a migration above them; ``down`` never reverts a baselined
migration, whose down would drop tables it never created.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import runner
from ._drift import DriftReport, against
from .errors import MigrationRefused
from .steps import declared_up_kind
from .target import Target

if TYPE_CHECKING:
    from ..settings import DatabaseSettings, FerroSettings

__all__ = ["BaselineReport", "baseline", "remove_baseline"]

NOT_COMPARED = (
    "baseline checked what `ferro migrate drift` checks: column defaults and "
    "objects ferro does not own were not compared"
)
"""What a successful baseline says it did not check (ADR-0031)."""


@dataclass(frozen=True)
class BaselineReport:
    """What ``baseline`` did.

    ``recorded`` names every migration it recorded (``NNNN_<name>``), empty
    when the drift check found something; ``data_steps_listed`` every data
    step it recorded without running (``NNNN_<name>/<file>``); ``drift`` the
    drift check's report when it found something, ``None`` when the
    database matched the target's snapshot.
    """

    recorded: list[str]
    data_steps_listed: list[str]
    drift: DriftReport | None
    steps: int = 0
    """How many step records were written."""
    reports: list[dict[str, Any]] = field(default_factory=list)
    """What the drift check reported without counting it as drift (a
    foreign or unverifiable row policy, ...), as ``drift`` reports it."""

    @property
    def warnings(self) -> list[str]:
        """Each report's sentence, in order."""
        return [report["text"] for report in self.reports]

    def render(self) -> str:
        """The text ``ferro migrate baseline`` prints on stdout."""
        if self.drift is not None:
            return f"{self.drift.render()}\nnothing was recorded"
        data = len(self.data_steps_listed)
        counted = _count(self.steps, "step")
        if data:
            counted += f"; {_count(data, 'data step')} listed, not run"
        lines = [f"recorded {_span(self.recorded)} as baseline ({counted})"]
        lines += [f"  {step}  recorded, not run" for step in self.data_steps_listed]
        lines.append(NOT_COMPARED)
        return "\n".join(lines)

    def raise_for_problems(self) -> None:
        """Raise :class:`MigrationRefused` naming every drift line unless the
        baseline was recorded."""
        if self.drift is not None:
            raise MigrationRefused(self.render())


def render_removed(removed: list[str]) -> str:
    """The text ``ferro migrate baseline --remove`` prints for the migrations
    whose baseline it removed."""
    if not removed:
        return "no baseline to remove"
    return f"removed the baseline of {_span(removed)}"


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def _span(names: list[str]) -> str:
    """``0001_a`` or ``0001_a … 0006_f``."""
    return names[0] if len(names) == 1 else f"{names[0]} … {names[-1]}"


async def _record(
    name: str,
    database: DatabaseSettings,
    target: str | None,
    lock_timeout: str | float | None,
) -> BaselineReport:
    timeout = database.lock_wait(lock_timeout)
    tracked = await runner.open_tracked(name, database)
    async with tracked.locked(timeout, runner.say_waiting) as run:
        plan = run.plan_baseline(target)
        # A baseline never runs a data step, so its file is never executed:
        # each one's record holds the shape its up declares, read from the
        # file's syntax tree (ADR-0035).
        kinds = {
            (migration, step): declared_up_kind(Path(path))
            for migration, step, path in plan.data_step_files
        }
        drift = await against(plan.snapshot, migration=plan.target, using=name)
        if not drift.clean:
            return BaselineReport(
                recorded=[], data_steps_listed=[], drift=drift, reports=drift.reports
            )
        await run.write_baseline(plan, kinds)
        return BaselineReport(
            recorded=list(plan.recorded),
            data_steps_listed=list(plan.data_steps),
            drift=None,
            steps=plan.steps,
            reports=drift.reports,
        )


async def _remove(
    name: str, database: DatabaseSettings, lock_timeout: str | float | None
) -> list[str]:
    timeout = database.lock_wait(lock_timeout)
    tracked = await runner.open_tracked(name, database)
    async with tracked.locked(timeout, runner.say_waiting) as run:
        names = {r["migration"]: r["migration_name"] for r in run.records}
        removed = await run.remove_baseline()
    return [names[number] for number in sorted({m for m, _ in removed})]


async def baseline(
    settings: FerroSettings | None = None,
    database: str | None = None,
    *,
    target: str | None = None,
    using: str | None = None,
    url: str | None = None,
    lock_timeout: str | float | None = None,
) -> BaselineReport:
    """Record every migration through ``target`` (the head when ``None``;
    ``"0006"`` or ``"0006_add_teams"``) as applied on a database that already
    has their schema, under the run lock (``ferro migrate baseline``).

    The database is checked against the target's schema snapshot first, as
    :func:`~ferro.migrations.drift` checks it; when that finds anything,
    nothing is recorded and the report's ``drift`` lists it
    (``.raise_for_problems()`` raises with every line). There is no
    override (ADR-0031). Works on ``using`` (an open connection), on a
    private connection to ``url``, or on the default connection.

    Raises:
        MigrationRefused: the database already has migration records, the
            target is not in the migrations directory, the lock wait outlasts
            ``lock_timeout``, or the configuration names no single database.
    """
    where = Target.resolve(settings, database, using=using, url=url)
    async with where.open() as name:
        return await _record(name, where.database, target, lock_timeout)


async def remove_baseline(
    settings: FerroSettings | None = None,
    database: str | None = None,
    *,
    using: str | None = None,
    url: str | None = None,
    lock_timeout: str | float | None = None,
) -> list[str]:
    """Delete every record a baseline wrote, under the run lock (``ferro
    migrate baseline --remove``). Returns the migrations whose baseline was
    removed (``NNNN_<name>``), empty when there was none; they are pending
    afterwards.

    Raises:
        MigrationRefused: a run applied a migration above the baseline
            (revert it with ``ferro migrate down`` first; the message names
            it), or the lock wait outlasts ``lock_timeout``.
    """
    target = Target.resolve(settings, database, using=using, url=url)
    async with target.open() as name:
        return await _remove(name, target.database, lock_timeout)
