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
0006_add_teams      installed (baseline)
0007_nickname       pending
```

A database that ``connect(auto_migrate=True)`` or Alembic built already has
the tables ``0001`` creates. ``baseline`` records every step of every
migration through the target as applied without running it, and only after
checking: under the run lock, with no step records yet, the live database
is planned against the target migration's schema snapshot exactly as
``ferro migrate drift`` plans it against the last applied one (the same two
FFI doors, the same lines). Any line means nothing is recorded; there is no
flag that records past one. Live tables the snapshot does not declare
(``alembic_version``, a later migration's tables) are never drift.

``baseline --remove`` deletes the baseline's records again, refused while a
run has applied a migration above them; ``down`` never reverts a baselined
migration, whose down would drop tables it never created.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .. import _core
from ..settings import SettingsError
from . import runner
from .api import _connection, _resolve
from .drift import _DESTRUCTIVE, DriftReport, _describe, render_op
from .errors import MigrationRefused
from .report import RunRefused

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
    warnings: list[str] = field(default_factory=list)
    """What the drift check reported without counting it as drift (a
    foreign or unverifiable row policy, ...), as ``drift`` reports it."""

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


async def _against(name: str, target: str, snapshot: dict, dialect: str) -> DriftReport:
    """``drift``'s check, against ``target``'s snapshot rather than the last
    applied one: only the snapshot's tables are read, destructive changes
    count, and each op is one :func:`render_op` line."""
    tables = [model["table_name"] for model in snapshot["payload"]["models"]]
    live_json, facts_json = await _core._live_schema_ir(name, json.dumps(tables))
    plan = json.loads(
        _core._plan_from_ir(
            live_json, json.dumps(snapshot), dialect, _DESTRUCTIVE, False, facts_json
        )
    )
    operations = _describe(plan["operations"], json.loads(live_json), snapshot, dialect)
    return DriftReport(
        against=target,
        lines=[render_op(op) for op in operations],
        operations=operations,
        warnings=list(plan["warnings"]) + list(plan["always_warnings"]),
    )


async def _locked(
    name: str, database: DatabaseSettings, lock_timeout: str | float
) -> tuple[str, str | None, int]:
    """The connection's dialect, its tracking schema and a run-lock handle."""
    dialect = runner.connection_dialect(name, database)
    tracking = runner.tracking_schema_for(database, dialect)
    timeout = runner.parse_lock_timeout(lock_timeout)
    handle = await _core._acquire_run_lock(name, None, timeout, runner._say_waiting)
    return dialect, tracking, handle


async def _record(
    name: str,
    database: DatabaseSettings,
    target: str | None,
    lock_timeout: str | float,
) -> BaselineReport:
    dialect, tracking, handle = await _locked(name, database, lock_timeout)
    try:
        state = json.loads(await _core._read_records(name, tracking))
        if state["refusal"] is not None:
            raise RunRefused(state["refusal"])
        plan = json.loads(
            _core._plan_baseline(
                str(database.directory),
                json.dumps(state["records"]),
                dialect,
                target,
                runner._ferro_version(),
            )
        )
        drift = await _against(name, plan["target"], plan["snapshot"], dialect)
        if not drift.clean:
            return BaselineReport(
                recorded=[], data_steps_listed=[], drift=drift, warnings=drift.warnings
            )
        await _core._write_baseline_records(
            name, json.dumps(plan["records"]), tracking, handle
        )
        return BaselineReport(
            recorded=list(plan["recorded"]),
            data_steps_listed=list(plan["data_steps"]),
            drift=None,
            steps=len(plan["records"]),
            warnings=drift.warnings,
        )
    finally:
        await _core._release_run_lock(handle)


async def _remove(
    name: str, database: DatabaseSettings, lock_timeout: str | float
) -> list[str]:
    _, tracking, handle = await _locked(name, database, lock_timeout)
    try:
        state = json.loads(await _core._read_records(name, tracking))
        names = {r["migration"]: r["migration_name"] for r in state["records"]}
        removed = json.loads(
            await _core._remove_baseline_records(name, tracking, handle)
        )
    finally:
        await _core._release_run_lock(handle)
    return [names[number] for number in sorted({m for m, _ in removed})]


async def record(
    database: DatabaseSettings,
    *,
    target: str | None = None,
    using: str | None = None,
    url: str | None = None,
    lock_timeout: str | float = "30s",
) -> BaselineReport:
    """``ferro migrate baseline``: ``using`` names an open connection;
    otherwise ``database``'s URL (or ``url``) is connected and closed after."""
    async with runner._connection(database, using, url) as name:
        return await _record(name, database, target, lock_timeout)


async def remove(
    database: DatabaseSettings,
    *,
    using: str | None = None,
    url: str | None = None,
    lock_timeout: str | float = "30s",
) -> list[str]:
    """``ferro migrate baseline --remove``, on a connection as :func:`record`."""
    async with runner._connection(database, using, url) as name:
        return await _remove(name, database, lock_timeout)


async def baseline(
    settings: FerroSettings | None = None,
    database: str | None = None,
    *,
    target: str | None = None,
    using: str | None = None,
    url: str | None = None,
    lock_timeout: str = "30s",
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
    _, db = _resolve(settings, database)
    try:
        if url is not None:
            return await record(
                db, target=target, using=using, url=url, lock_timeout=lock_timeout
            )
        return await _record(_connection(using), db, target, lock_timeout)
    except SettingsError as err:
        raise MigrationRefused(str(err)) from None


async def remove_baseline(
    settings: FerroSettings | None = None,
    database: str | None = None,
    *,
    using: str | None = None,
    url: str | None = None,
    lock_timeout: str = "30s",
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
    _, db = _resolve(settings, database)
    try:
        if url is not None:
            return await remove(db, using=using, url=url, lock_timeout=lock_timeout)
        return await _remove(_connection(using), db, lock_timeout)
    except SettingsError as err:
        raise MigrationRefused(str(err)) from None
