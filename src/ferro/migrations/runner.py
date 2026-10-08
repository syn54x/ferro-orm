"""``ferro migrate up``, ``down`` and ``status``: the run loop (ADR-0028, ADR-0048).

```text
$ ferro migrate up
0001_create_author  01_schema  applied (12 ms)
0002_add_teams      01_schema  applied (9 ms)
```

Python sequences a run; the Rust core decides and executes it. :func:`up`
opens the database as one object (``_core._open_tracked``: its records and
one read of the migrations directory, held), takes the run lock as a block
(``tracked.locked(...)``, whose run object re-reads the records under the
lock), asks that run for its plan, and walks the plan's opaque step
handles: ``run.execute(step)`` runs a SQL step from the held bytes and
settles its record. Nothing here decides which step runs, how, or whether
a file may run at all, and nothing here builds a step record.

Each SQL step waits for table locks under the database's
``ddl_lock_timeout`` (ADR-0044); a step that times out is retried from its
first statement, and each attempt is a progress line::

    0003_add_slug  01_expand  waiting for a lock on "author" (attempt 1 of 10, retry in 1s)
    0003_add_slug  01_expand  applied (2214 ms)

:func:`down` walks back (ADR-0033) through the same walk over the run's
``Down`` plan, each step's ``.down`` file run by the same executor, which
removes the step's record in the down's own transaction::

    $ ferro migrate down
    Revert 0002_add_teams (1 step)? [y/N] y
    0002_add_teams  01_schema  reverted (8 ms)

A data step (``NN_<name>.py``, ADR-0024) is Python's to run: every planned
one is loaded before anything runs (its checksum checked, its declarations
read, every ``todo`` refused by file and line), then each runs on the run's
connection inside one transaction under its migration's historical models
(:meth:`ferro.registry.Registry.swap`), its record moved through the run's
named transitions (``start``, then ``finish`` inside the step's
transaction)::

    0011_backfill_slugs  01_backfill_author  applied (40 ms)

A ``@chunked`` step runs one transaction per batch instead, its cursor
committed with each batch (:mod:`ferro.migrations.chunked`), and its query
is checked over the historical models before anything runs.

:func:`status` reads the same object with no lock and creates nothing.
"""

from __future__ import annotations

import logging
import re
import sys
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

from .. import _core
from ..models import transaction
from ..registry import REGISTRY
from ..settings import _DURATION, _DURATION_UNITS, SettingsError
from ..state import resolve_operation_scope
from . import historical
from .chunked import BatchFailed, order_keys, run_chunked
from .context import HistoricalModels, StepContext
from .errors import MigrationRefused
from .historical import HistoricalModelError
from .report import RunRefused, StatusReport
from .steps import (
    Chunked,
    Irreversible,
    LoadedStep,
    NothingToReverse,
    StepRefused,
    chunked_query,
    load_step,
    unwritten,
)

if TYPE_CHECKING:
    from .._core import LockedDatabase, Plan, StepHandle, TrackedDatabase
    from ..raw import Transaction
    from ..settings import DatabaseSettings, FerroSettings

__all__ = [
    "AppliedStep",
    "DownPlan",
    "RunReport",
    "connection_dialect",
    "down",
    "parse_lock_timeout",
    "parse_target",
    "plan_down",
    "status",
    "tracking_schema_for",
    "up",
]

_STEP_STEM = re.compile(r"(\.(up|down)(\.[a-z]+)?\.sql|\.py)$")
_TARGET = re.compile(r"(\d{4})(?::(\d{2}))?")


class AppliedStep(NamedTuple):
    """One step a run applied."""

    migration: str
    """``NNNN_<name>``."""
    step: str
    """``NN_<name>``."""
    ms: int


@dataclass
class RunReport:
    """What :func:`up` or :func:`down` did."""

    applied: list[AppliedStep] = field(default_factory=list)
    """The steps applied, in order."""
    reverted: list[AppliedStep] = field(default_factory=list)
    """The steps :func:`down` reverted, newest first."""
    declined: bool = False
    """:func:`down`'s ``confirm`` said no: nothing was reverted."""
    refusal: str | None = None
    """Why the run stopped: a refusal before anything ran, a lost lock, or a
    failed step (its error and how to resume). ``None`` when it finished."""
    refused: RunRefused | None = None
    """The refusal itself when the run was refused (``refusal`` is its
    text): its ``kind``, ``migration``, ``step`` and ``reason`` say what it
    is about. ``None`` when the run finished or a step failed."""
    notes: list[str] = field(default_factory=list)
    """What the run accepted on the way (an edited unfinished step)."""
    ahead: list[str] = field(default_factory=list)
    """Applied migrations the directory lacks, let through by ``allow_ahead``."""

    def _refuse(self, refused: RunRefused) -> None:
        self.refusal, self.refused = str(refused), refused


MAX_LOCK_TIMEOUT_S = 60.0 * 60 * 24 * 365
"""The longest lock timeout accepted: one year, the same "until it is free"
bound ``connect(auto_migrate=...)`` waits (ADR-0038)."""


def parse_lock_timeout(value: str | float) -> float:
    """Seconds from ``"30s"``, ``"500ms"``, ``"1m"`` or a plain number of
    seconds (``0`` tries the lock once, :data:`MAX_LOCK_TIMEOUT_S` is the
    most).

    Raises:
        SettingsError: ``value`` is not a duration, is negative, or is longer
            than :data:`MAX_LOCK_TIMEOUT_S`.
    """
    if isinstance(value, int | float) and not isinstance(value, bool):
        seconds = float(value)
    elif isinstance(value, str) and re.fullmatch(r"\d+(\.\d+)?", value.strip()):
        seconds = float(value)
    elif isinstance(value, str) and (match := _DURATION.match(value.strip())):
        unit = _DURATION_UNITS[match.group(2)]
        seconds = (
            float(match.group(1))
            * {
                "milliseconds": 0.001,
                "seconds": 1.0,
                "minutes": 60.0,
            }[unit]
        )
    else:
        raise SettingsError(
            f'lock timeout {value!r} is not a duration; write it as "30s", '
            f'"500ms", "1m" or a number of seconds (0 refuses at once)'
        )
    if seconds < 0:
        raise SettingsError(f"lock timeout {value!r} is negative; use 0 or more")
    if not seconds <= MAX_LOCK_TIMEOUT_S:  # also refuses nan
        raise SettingsError(
            f"lock timeout {value!r} is too long; use at most one year "
            f"({MAX_LOCK_TIMEOUT_S:.0f} seconds)"
        )
    return seconds


@asynccontextmanager
async def _connection(
    database: DatabaseSettings, using: str | None, url: str | None
) -> AsyncIterator[str]:
    """The connection name to run on: ``using`` as given, or a private
    connection to ``database``'s URL, closed afterwards."""
    if using is not None:
        if url is not None:
            raise SettingsError("pass using= or url=, not both")
        yield using
        return
    from .. import connect

    name = f"_ferro_migrate_{uuid.uuid4().hex}"
    await connect(database.url_for(url), name=name)
    try:
        yield name
    finally:
        await _core._disconnect(name)


def connection_dialect(name: str, database: DatabaseSettings) -> str:
    """The dialect of open connection ``name``, refused when it is not open
    or ``database`` does not target it."""
    dialect = _core.connection_backend(name)
    if dialect is None:
        raise SettingsError(f"connection `{name}` is not open; connect it first")
    if dialect not in database.dialects:
        raise SettingsError(
            f"database `{database.name}` targets {', '.join(database.dialects)}, but "
            f"this connection is {dialect}; add {dialect!r} to its dialects and "
            f"regenerate, or connect to a {' or '.join(database.dialects)} database"
        )
    return dialect


def tracking_schema_for(database: DatabaseSettings, dialect: str) -> str | None:
    """Where ``database``'s tracking tables live on ``dialect``: its
    ``tracking_schema`` on Postgres, else the governed schema (``None``)."""
    return database.tracking_schema if dialect == "postgres" else None


async def open_tracked(name: str, database: DatabaseSettings) -> TrackedDatabase:
    """``database``'s tracking tables and migrations directory on open
    connection ``name``: its records and one read of the directory, held
    (ADR-0048). Creates nothing and takes no lock.

    Raises:
        SettingsError: ``name`` is not open, or ``database`` does not target
            its dialect.
    """
    dialect = connection_dialect(name, database)
    return await _core._open_tracked(
        name,
        tracking_schema_for(database, dialect),
        str(database.directory),
        database.ddl_lock_timeout_seconds,
    )


def say_waiting(text: str) -> None:
    """Say on stderr, at once, that a run waits for another run's lock."""
    print(text, file=sys.stderr, flush=True)


def _stem(file: str) -> str:
    return _STEP_STEM.sub("", file)


async def up(
    settings: FerroSettings,
    database: DatabaseSettings,
    *,
    using: str | None = None,
    url: str | None = None,
    lock_timeout: str | float = "30s",
    allow_ahead: bool = False,
    progress: Callable[[str], Any] | None = None,
    through: str | None = None,
) -> RunReport:
    """Apply every pending step of every pending migration, in order, under
    the run lock.

    ``using`` names an open connection; otherwise ``database``'s URL (or
    ``url``) is connected for the run and closed after. A second run waits
    up to ``lock_timeout``, saying so on stderr at once. ``allow_ahead`` lets
    a database holding migrations the directory lacks through (ADR-0038).
    ``progress`` receives each line as the run goes: one per applied step,
    and one per attempt a step makes while it waits for a table lock under
    ``database.ddl_lock_timeout``.

    ``through`` (``"0007"``, a migration number) stops the run after that
    migration: only pending steps of migrations up to and including it run.
    It is the test harness's target (ADR-0045); the application's ``up()``
    has none (ADR-0040). A number the directory lacks is refused, and so is
    a step (``"0007:02"``): neither is a target. A ``through`` at or below
    the last applied migration has nothing pending to run: the run applies
    nothing and reverts nothing (going down is :func:`down`'s).

    Returns a :class:`RunReport`; a refusal or a failed step is reported in
    ``refusal``, not raised.
    """
    del settings  # the database carries its project; kept for API symmetry
    timeout = parse_lock_timeout(lock_timeout)
    direction: dict[str, Any] = {"direction": "up"}
    if through is not None:
        direction["through"] = through
    report = RunReport()
    async with _connection(database, using, url) as name:
        tracked = await open_tracked(name, database)
        try:
            async with tracked.locked(timeout, say_waiting) as run:
                keys = _order_keys_for(run, "applied")
                plan = await run.plan(
                    direction, allow_ahead=allow_ahead, order_keys=keys
                )
                report = await _walk(run, plan, progress, using=name)
        except RunRefused as refused:
            report._refuse(refused)
    return report


async def _walk(
    run: LockedDatabase,
    plan: Plan,
    say: Callable[[str], Any] | None,
    *,
    using: str,
) -> RunReport:
    """Walk ``plan``'s steps on ``run``, in order, on connection ``using``:
    ``run.execute`` for a SQL step, a data step in Python with its record
    moved through the run's transitions. The plan's direction decides which
    transitions settle each step and whether a step is reported ``applied``
    or ``reverted``. Stops at the first failed step (its message is the
    report's ``refusal``) or refusal."""
    down = plan.direction == "down"
    report = RunReport(ahead=list(plan.ahead))
    steps = plan.steps
    if not steps:
        return report
    say = say or (lambda _line: None)
    done = report.reverted if down else report.applied
    try:
        loaded, reasons = _load_data_steps(steps, plan.direction)
        name_width = max(len(step.migration_name) for step in steps)
        stem_width = max(len(_stem(step.file)) for step in steps)
        with _HistoricalSwaps(run) as swaps:
            _check_chunked(steps, loaded, swaps, plan.direction)
            for step in steps:
                line = _step_line(step, name_width, stem_width, say)
                if step.edited is not None:
                    # ADR-0030: an unfinished attempt that committed nothing (or
                    # a no-transaction step, re-runnable from its first
                    # statement) runs its edited file; the record takes the
                    # new checksum.
                    note = (
                        f"re-recorded {step.migration_name}/{step.file} "
                        f"(sha384:{step.edited['recorded']} → sha384:{step.checksum})"
                    )
                    report.notes.append(note)
                    say(note)
                reason = step.nothing_to_reverse or reasons.get(_key(step))
                if step.data:
                    this = loaded.get(_key(step))
                    models = swaps.models_for(step) if this is not None else None
                    data = _DataStep(run, step, line, using)
                    outcome = await (
                        data.down(this, models) if down else data.up(this, models)
                    )
                else:
                    swaps.leave(step)
                    outcome = await run.execute(step, line)
                if not outcome["ok"]:
                    report.refusal = outcome["message"]
                    return report
                done.append(
                    AppliedStep(step.migration_name, _stem(step.file), outcome["ms"])
                )
                if not down:
                    line(f"applied ({outcome['ms']} ms)")
                elif reason is not None:
                    line(f"nothing to reverse: {reason}")
                else:
                    line(f"reverted ({outcome['ms']} ms)")
    except RunRefused as refused:
        report._refuse(refused)
    return report


# -- historical models and order keys ----------------------------------------------


def _key(step: StepHandle) -> tuple[int, int]:
    return step.migration, step.step


def historical_models(migrations: dict[str, Any], number: int) -> HistoricalModels:
    """The historical models of migration ``number`` (ADR-0035), built from
    the held directory read (``run.migrations``): its snapshot over its
    parent's. The one builder, for a data step's run and for the order keys
    of an edited chunked step alike.

    Raises:
        HistoricalModelError: the snapshots do not build.
    """
    by_number = {m["number"]: m for m in migrations["migrations"]}
    own = by_number[number]
    parent = by_number.get(number - 1)
    return historical.build(
        parent["snapshot"]["ir"] if parent is not None else None,
        own["snapshot"]["ir"],
        rev=f"{number:04}_{own['name']}",
    )


def order_keys_on_disk(
    tracked: TrackedDatabase | LockedDatabase,
) -> list[tuple[int, int, list[str]]]:
    """The order keys each cursor-holding chunked step's file pages over
    today, as the run planner reads them (``[(migration, step, ["author.id",
    ...]), ...]``; an empty list for a step whose ``up`` is no longer
    ``@chunked``).

    A fact only Python can read (the query is a function of the migration's
    historical models), read for every record that is an unfinished
    chunked step holding a cursor: the planner decides whether its file was
    edited and whether its cursor is still a position in the edited query
    (ADR-0030). A directory that does not read gives no facts; the planner
    refuses it with its own text.

    Raises:
        MigrationRefused: such a step's file does not load, or its query
            does not build over its historical models.
    """
    wanted = sorted(
        (record["migration"], record["step"])
        for record in tracked.records
        if record["kind"] == "chunked"
        and record["finished_at"] is None
        and record["resume_cursor"] is not None
    )
    if not wanted:
        return []
    try:
        migrations = tracked.migrations
    except RunRefused:
        return []
    by_number = {m["number"]: m for m in migrations["migrations"]}
    facts: list[tuple[int, int, list[str]]] = []
    for number, ordinal in wanted:
        migration = by_number.get(number)
        step = next(
            (s for s in (migration or {}).get("steps", []) if s["ordinal"] == ordinal),
            None,
        )
        if migration is None or step is None:
            continue  # the planner refuses a record the directory lacks
        if step["kind"] != "data":
            facts.append((number, ordinal, []))
            continue
        path = Path(step["files"]["portable"]["up"])
        shape = load_step(path, None).up.shape
        if not isinstance(shape, Chunked):
            facts.append((number, ordinal, []))
            continue
        models = historical_models(migrations, number)
        facts.append((number, ordinal, order_keys(chunked_query(shape, models, path))))
    return facts


def _order_keys_for(
    run: LockedDatabase, nothing: str
) -> list[tuple[int, int, list[str]]]:
    """:func:`order_keys_on_disk` for a run that refuses on a file that does
    not load (``Nothing was <nothing>.``)."""
    try:
        return order_keys_on_disk(run)
    except MigrationRefused as refused:
        raise RunRefused(f"{refused}. Nothing was {nothing}.") from None


# -- data steps -------------------------------------------------------------------


def _load_data_steps(
    steps: list[StepHandle], direction: str
) -> tuple[dict[tuple[int, int], LoadedStep], dict[tuple[int, int], str]]:
    """Load every planned data step before anything runs, refusing the run
    on a file that does not load, an unwritten step (every ``todo`` named by
    file, line and message), or (going down) an irreversible one. Returns
    the loaded steps, and going down each data step whose down declares
    ``nothing_to_reverse`` with its reason (it runs no function). A
    ``@chunked`` query is checked once its historical models are built
    (:func:`_check_chunked`)."""
    nothing = "applied" if direction == "up" else "reverted"
    loaded: dict[tuple[int, int], LoadedStep] = {}
    reasons: dict[tuple[int, int], str] = {}
    todos: list[str] = []
    for step in steps:
        if not step.data or step.nothing_to_reverse is not None:
            continue
        path = Path(step.path)
        try:
            this = load_step(path, step.checksum)
        except StepRefused as refused:
            raise RunRefused(f"{refused}. Nothing was {nothing}.") from None
        todos += unwritten(path, this.todos)
        declared = this.up if direction == "up" else this.down
        shape = declared.shape
        if isinstance(shape, Irreversible):
            raise RunRefused(
                f"ferro migrate: {step.migration:04}:{step.step:02} is "
                f"irreversible: {shape.reason}\nThere is no flag to skip it: to revert "
                f"past it, write the step's down in place of the declaration. Nothing "
                f"was reverted.",
                kind="irreversible",
                migration=step.migration,
                step=step.step,
                reason=shape.reason,
            )
        if isinstance(shape, NothingToReverse):
            reasons[_key(step)] = shape.reason
            continue
        loaded[_key(step)] = this
    if todos:
        raise RunRefused(
            "\n".join(todos)
            + f"\nWrite each step where it says todo(...), then run `ferro migrate "
            f"{direction}` again. Nothing was {nothing}."
        )
    return loaded, reasons


def _check_chunked(
    steps: list[StepHandle],
    loaded: dict[tuple[int, int], LoadedStep],
    swaps: _HistoricalSwaps,
    direction: str,
) -> None:
    """Refuse the run before anything runs when a ``@chunked`` step's query
    cannot be paged by keyset (:func:`~ferro.migrations.steps.chunked_query`),
    built over its migration's historical models."""
    nothing = "applied" if direction == "up" else "reverted"
    for step in steps:
        this = loaded.get(_key(step))
        if this is None:
            continue
        shape = (this.up if direction == "up" else this.down).shape
        if not isinstance(shape, Chunked):
            continue
        try:
            chunked_query(shape, swaps.built_for(step), Path(step.path))
        except StepRefused as refused:
            raise RunRefused(f"{refused}. Nothing was {nothing}.") from None


class _HistoricalSwaps:
    """The registry swap of the migration whose data step is running: one
    swap per migration with data steps, held until the run leaves that
    migration (ADR-0035), restored on exit, error and cancellation. Built
    from the run's held directory read, never from disk again."""

    def __init__(self, run: LockedDatabase) -> None:
        self._run = run
        self._stack = ExitStack()
        self._migration: int | None = None
        self._installed: HistoricalModels | None = None
        self._built: dict[int, HistoricalModels] = {}

    def __enter__(self) -> _HistoricalSwaps:
        return self

    def __exit__(self, *exc: object) -> None:
        self._stack.close()

    def leave(self, step: StepHandle) -> None:
        """Restore today's registry when ``step`` belongs to another migration."""
        if self._migration is not None and step.migration != self._migration:
            self._stack.close()
            self._migration = None
            self._installed = None

    def models_for(self, step: StepHandle) -> HistoricalModels:
        """The historical models of ``step``'s migration, installed."""
        self.leave(step)
        if self._installed is None:
            models = self.built_for(step)
            self._stack.enter_context(REGISTRY.swap(models))
            self._migration, self._installed = step.migration, models
        return self._installed

    def built_for(self, step: StepHandle) -> HistoricalModels:
        """The historical models of ``step``'s migration, built once and not
        installed."""
        models = self._built.get(step.migration)
        if models is None:
            try:
                models = historical_models(self._run.migrations, step.migration)
            except HistoricalModelError as refused:
                raise RunRefused(f"{refused} Nothing more was run.") from None
            self._built[step.migration] = models
        return models


def _route() -> Any:
    """The route of the transaction open in this task: where a step record
    written inside a step's transaction commits."""
    return resolve_operation_scope(using=None, session=None)


class _DataStep:
    """Run one planned data step, its record moved through the run's
    transitions (ADR-0024, ADR-0048).

    An atomic step is one transaction on the run's connection, its record
    finished (going up) or removed (going down) inside that transaction. A
    chunked step runs through :func:`~ferro.migrations.chunked.run_chunked`,
    one transaction per batch with its cursor committed in it.

    A failure rolls back what it was running and is recorded only where the
    database moved (the tracking table says where it stands now): going up
    the started record always carries ``failed_at`` and the error; going down
    a record changes only when the down left part of itself committed (a
    chunked down past its first batch, which stays ``reverting``). A down
    that rolled back whole leaves the step applied and its record untouched:
    the error is the run's to report. A refusal (a lost run lock) is never a
    step's failure: it stops the run as itself.
    """

    def __init__(
        self,
        run: LockedDatabase,
        step: StepHandle,
        line: Callable[[str], None],
        using: str,
    ) -> None:
        self._run = run
        self._step = step
        self._line = line
        self._using = using

    @property
    def _shown(self) -> str:
        return f"{self._step.migration_name}/{self._step.file}"

    def _context(self, models: HistoricalModels, tx: Transaction) -> StepContext:
        stem = _stem(self._step.file)
        log = logging.getLogger(f"ferro.migrations.{self._step.migration:04}.{stem}")
        return StepContext(models, tx, self._run.dialect, log)

    def _failed(self, ms: int, error: str, after: str) -> dict[str, Any]:
        return {
            "ok": False,
            "ms": ms,
            "message": f"ferro migrate: {self._shown} failed: {error}\n{after} Nothing "
            f"after it ran.",
        }

    async def up(
        self, loaded: LoadedStep | None, models: HistoricalModels | None
    ) -> dict[str, Any]:
        """Run the step's ``up``; a resumed chunked step resumes from its
        cursor."""
        if loaded is None or models is None:  # pragma: no cover - every up loads
            raise RunRefused(f"ferro migrate: {self._shown} was not loaded")
        run, step = self._run, self._step
        await run.start(step, loaded.up.shape.kind)
        clock = time.monotonic()
        if isinstance(loaded.up.shape, Chunked):
            return await self._up_chunked(loaded, models, clock)
        try:
            async with transaction(using=self._using) as tx:
                await loaded.up.fn(self._context(models, tx))
                ms = _elapsed(clock)
                await run.finish(step, ms, _route())
        except RunRefused:
            raise
        except Exception as err:
            ms = _elapsed(clock)
            error = _error_text(err)
            await run.fail(step, ms, error)
            return self._failed(
                ms,
                error,
                "The step was rolled back; fix the file or the database and run "
                "`ferro migrate up` again to resume at it.",
            )
        return {"ok": True, "ms": ms}

    async def _up_chunked(
        self, loaded: LoadedStep, models: HistoricalModels, clock: float
    ) -> dict[str, Any]:
        try:
            await run_chunked(
                lambda tx: self._context(models, tx),
                loaded.up,
                self._run,
                self._step,
                direction="up",
                using=self._using,
            )
        except BatchFailed as failed:
            ms = _elapsed(clock)
            error = _error_text(failed.error)
            await self._run.fail(self._step, ms, error, failed.cursor, failed.rows_done)
            return self._failed(
                ms,
                error,
                f"The failing batch was rolled back; the {failed.rows_done:,} rows of "
                f"the batches before it stay committed. Fix the file or the database "
                f"and run `ferro migrate up` again to resume after them.",
            )
        return {"ok": True, "ms": _elapsed(clock)}

    async def down(
        self, loaded: LoadedStep | None, models: HistoricalModels | None
    ) -> dict[str, Any]:
        """Run the step's ``down`` (none for one with nothing to reverse) and
        remove its record in the same transaction."""
        clock = time.monotonic()
        if (
            loaded is not None
            and models is not None
            and isinstance(loaded.down.shape, Chunked)
        ):
            return await self._down_chunked(loaded, models, clock)
        try:
            async with transaction(using=self._using) as tx:
                if loaded is not None and models is not None:
                    await loaded.down.fn(self._context(models, tx))
                await self._run.remove(self._step, _route())
        except RunRefused:
            raise
        except Exception as err:
            return self._failed(
                _elapsed(clock),
                _error_text(err),
                "The down was rolled back, so the step stays applied and its record "
                "unchanged; fix the file or the database and run `ferro migrate down` "
                "again to revert it.",
            )
        return {"ok": True, "ms": _elapsed(clock)}

    async def _down_chunked(
        self, loaded: LoadedStep, models: HistoricalModels, clock: float
    ) -> dict[str, Any]:
        try:
            await run_chunked(
                lambda tx: self._context(models, tx),
                loaded.down,
                self._run,
                self._step,
                direction="down",
                using=self._using,
            )
        except BatchFailed as failed:
            ms = _elapsed(clock)
            error = _error_text(failed.error)
            if not failed.committed:
                return self._failed(
                    ms,
                    error,
                    "Its first batch was rolled back, so the step stays applied and "
                    "its record unchanged; fix the file or the database and run "
                    "`ferro migrate down` again to revert it.",
                )
            await self._run.fail_revert(
                self._step, error, failed.cursor, failed.rows_done
            )
            return self._failed(
                ms,
                error,
                f"The failing batch was rolled back; the {failed.rows_done:,} rows the "
                f"down reverted before it stay reverted and the step's record stays "
                f"reverting, so `ferro migrate up` refuses until the down finishes. Fix "
                f"the file or the database and run `ferro migrate down` again to resume "
                f"after them.",
            )
        return {"ok": True, "ms": _elapsed(clock)}


def _elapsed(clock: float) -> int:
    return int((time.monotonic() - clock) * 1000)


def _error_text(err: BaseException) -> str:
    text = str(err)
    return f"{type(err).__name__}: {text}" if text else type(err).__name__


def _step_line(
    step: StepHandle, name_width: int, stem_width: int, say: Callable[[str], Any]
) -> Callable[[str], None]:
    """Say ``text`` as one of ``step``'s progress lines:
    ``<migration>  <step>  <text>``."""
    prefix = f"{step.migration_name:<{name_width}}  {_stem(step.file):<{stem_width}}  "

    def line(text: str) -> None:
        say(prefix + text)

    return line


# -- status -----------------------------------------------------------------------


async def status_of(
    tracked: TrackedDatabase, database: DatabaseSettings
) -> StatusReport:
    """Where ``tracked`` stands against its held directory read: each step's
    state (a run holding the lock makes its step ``running``), and the
    refusal ``up`` would meet. Takes no lock and creates nothing."""
    try:
        keys: list[tuple[int, int, list[str]]] | None = order_keys_on_disk(tracked)
    except MigrationRefused:
        # A step file that does not load: status still answers, and the
        # edited-chunked refusal offers --continue on its condition.
        keys = None
    held = await tracked.lock_held()
    return StatusReport.from_core(
        tracked.status(keys, lock_held=held),
        database=database.name,
        dialect=tracked.dialect,
        table=tracked.tracking_table,
        rows_done={
            (r["migration"], r["step"]): r["rows_done"]
            for r in tracked.records
            if r["kind"] == "chunked" and r["rows_done"] is not None
        },
    )


async def status(
    settings: FerroSettings,
    database: DatabaseSettings,
    *,
    using: str | None = None,
    url: str | None = None,
) -> StatusReport:
    """Where ``database`` stands against its migrations directory.

    Takes no lock and creates nothing: a database without the tracking
    table reports every migration pending. The refusal ``up`` would meet
    (an edited file, a broken chain, ...) is part of the report.
    """
    del settings
    async with _connection(database, using, url) as name:
        return await status_of(await open_tracked(name, database), database)


# -- down ------------------------------------------------------------------------


class DownStep(NamedTuple):
    """One step :func:`down` would revert."""

    migration: str
    """``NNNN_<name>``."""
    step: str
    """``NN_<name>``."""
    nothing_to_reverse: str | None
    """Why its down runs no statement, when it runs none."""


@dataclass
class DownPlan:
    """What a ``down`` would revert, newest first, or why it would not."""

    steps: list[DownStep] = field(default_factory=list)
    refusal: str | None = None

    def describe(self) -> str:
        """The plan as the prompt shows it, one line per step."""
        if not self.steps:
            return "nothing to revert"
        name_width = max(len(step.migration) for step in self.steps)
        lines = ["down reverts, in this order:"]
        for step in self.steps:
            line = f"  {step.migration:<{name_width}}  {step.step}"
            if step.nothing_to_reverse is not None:
                line += f"  (nothing to reverse: {step.nothing_to_reverse})"
            lines.append(line)
        return "\n".join(lines)

    def question(self) -> str:
        """``Revert 0002_add_teams (1 step)?``"""
        migrations = list(dict.fromkeys(step.migration for step in self.steps))
        count = len(self.steps)
        return (
            f"Revert {', '.join(migrations)} ({count} step{'' if count == 1 else 's'})?"
        )


def parse_target(target: str | None = None, *, all: bool = False) -> dict[str, Any]:
    """The run planner's target for ``down``: the latest migration, ``"0005"``
    (keep 0005 applied; ``"0000"`` reverts everything), ``"0007:02"`` (keep
    steps 01-02 of 0007), or ``all``."""
    if all and target is not None:
        raise SettingsError("pass --to or --all, not both")
    if all:
        return {"direction": "down", "target": "all"}
    if target is None:
        return {"direction": "down", "target": "latest"}
    match = _TARGET.fullmatch(target.strip())
    if match is None:
        raise SettingsError(
            f"--to {target!r} is not a migration or a step; write a migration "
            f"number (0005), a migration and step (0007:02), or 0000 for everything"
        )
    migration = int(match.group(1))
    if match.group(2) is None:
        return {"direction": "down", "target": {"migration": migration}}
    return {
        "direction": "down",
        "target": {"step": [migration, int(match.group(2))]},
    }


async def _preview_down(
    tracked: TrackedDatabase, direction: dict[str, Any]
) -> tuple[Plan, DownPlan]:
    """What a ``down`` would revert, planned on ``tracked`` with no lock: a
    data step's down declares itself in Python, so an irreversible one
    refuses here too (before any lock), and a nothing-to-reverse one says
    so.

    Raises:
        RunRefused: the down would be refused.
    """
    plan = await tracked.plan(direction)
    _, reasons = _load_data_steps(plan.steps, "down")
    shown = DownPlan(
        steps=[
            DownStep(
                step.migration_name,
                _stem(step.file),
                step.nothing_to_reverse or reasons.get(_key(step)),
            )
            for step in plan.steps
        ]
    )
    return plan, shown


async def plan_down(
    settings: FerroSettings,
    database: DatabaseSettings,
    *,
    target: str | None = None,
    all: bool = False,
    using: str | None = None,
    url: str | None = None,
) -> DownPlan:
    """What :func:`down` would revert, read with no lock, changing nothing."""
    del settings
    direction = parse_target(target, all=all)
    async with _connection(database, using, url) as name:
        tracked = await open_tracked(name, database)
        try:
            return (await _preview_down(tracked, direction))[1]
        except RunRefused as refused:
            return DownPlan(refusal=str(refused))


async def down(
    settings: FerroSettings,
    database: DatabaseSettings,
    *,
    target: str | None = None,
    all: bool = False,
    using: str | None = None,
    url: str | None = None,
    lock_timeout: str | float = "30s",
    confirm: Callable[[DownPlan], bool] | None = None,
    progress: Callable[[str], Any] | None = None,
) -> RunReport:
    """Revert every applied step above ``target``, newest first, under the
    run lock (ADR-0033).

    ``target`` is ``None`` (the latest migration with a record, a partly
    applied one included), ``"0005"`` (leave 0005 fully applied; ``"0000"``
    reverts everything), or ``"0007:02"`` (leave steps 01-02 of 0007
    applied); ``all`` reverts everything. A step whose down is declared
    irreversible, or one a baseline recorded, refuses the whole run before
    anything is reverted.

    ``confirm`` is shown the plan before the lock is taken; when it returns
    false nothing is reverted (``declined``). Once the lock is held the plan
    is made again, and a database that changed in between is refused rather
    than reverted unseen. ``progress`` receives one line per reverted step.

    Returns a :class:`RunReport`; a refusal or a failed down is reported in
    ``refusal``, not raised. A failed down that rolled back whole leaves its
    step applied and its record as it was; one that left part of itself
    applied (a no-transaction SQL down, a chunked down past its first
    batch) leaves the record carrying the error, a chunked one still
    ``reverting`` at its cursor. The next ``down`` resumes at it.
    """
    del settings
    direction = parse_target(target, all=all)
    timeout = parse_lock_timeout(lock_timeout)
    report = RunReport()
    async with _connection(database, using, url) as name:
        tracked = await open_tracked(name, database)
        try:
            seen, shown = await _preview_down(tracked, direction)
        except RunRefused as refused:
            report._refuse(refused)
            return report
        if not seen.steps:
            return report
        if confirm is not None and not confirm(shown):
            report.declined = True
            return report
        try:
            async with tracked.locked(timeout, say_waiting) as run:
                plan = await run.plan(direction)
                if [s.standing for s in plan.steps] != [s.standing for s in seen.steps]:
                    report.refusal = (
                        "ferro migrate: the database's migration records changed while "
                        "the plan was shown; nothing was reverted. Run `ferro migrate "
                        "down` again to see the plan as it stands now."
                    )
                    return report
                report = await _walk(run, plan, progress, using=name)
        except RunRefused as refused:
            report._refuse(refused)
    return report
