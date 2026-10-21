"""`ferro migrate up`, `down` and `status`: the run loop.

```text
$ ferro migrate up
0001_create_author  01_schema  applied (12 ms)
0002_add_teams      01_schema  applied (9 ms)
```

```python {test=compile reason="needs a project with a migrations directory"}
await ferro.migrations.up()                # the same run, in-process
await ferro.migrations.require_applied()   # where it stands, raised when behind
```

Each verb here is one function the CLI and the application both call,
`(settings=None, database=None, *, using=None, url=None, ...)`: the
database and connection come from `Target.resolve`. A refused or failed run
raises `MigrationRefused` carrying its `RunReport` as `.report`, for every
caller; the CLI prints the report's refusal and exits 1.

Python sequences a run; the core decides and executes it. `up` opens the
database as one object (its records and one read of the migrations
directory, held), takes the run lock as a block (whose run object re-reads
the records under the lock), asks that run for its plan, and walks the
plan's opaque step handles: `run.execute(step)` runs a SQL step from the
held bytes and settles its record. Nothing here decides which step runs,
how, or whether a file may run at all, and nothing here builds a step
record.

Each SQL step waits for table locks under the database's
`ddl_lock_timeout`; a step that times out is retried from its first
statement, and each attempt is a progress line:

```text
0003_add_slug  01_expand  waiting for a lock on "author" (attempt 1 of 10, retry in 1s)
0003_add_slug  01_expand  applied (2214 ms)
```

`down` walks back through the same walk over the run's down plan, each
step's `.down` file run by the same executor, which removes the step's
record in the down's own transaction:

```text
$ ferro migrate down
Revert 0002_add_teams (1 step)? [y/N] y
0002_add_teams  01_schema  reverted (8 ms)
```

A data step (`NN_<name>.py`) is Python's to run: every planned one is
loaded before anything runs (its checksum checked, its declarations read,
every `todo` refused by file and line), then each runs on the run's
connection inside one transaction under its migration's historical models,
its record moved through the run's named transitions (`start`, then
`finish` inside the step's transaction):

```text
0011_backfill_slugs  01_backfill_author  applied (40 ms)
```

A `@chunked` step runs one transaction per batch instead, its cursor
committed with each batch, and its query is checked over the historical
models before anything runs.

`status` and `require_applied` read the same object with no lock and
create nothing.
"""

from __future__ import annotations

import logging
import re
import sys
import time
import warnings
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, TypeAlias

from .. import _core
from ..models import transaction
from ..registry import REGISTRY
from ..settings import MAX_LOCK_TIMEOUT_S, SettingsError, parse_lock_timeout
from ..state import resolve_operation_scope
from . import historical
from ._chunk_loop import BatchFailed, order_keys, run_chunked
from .context import HistoricalModels, StepContext
from .errors import DatabaseAheadError, MigrationRefused, PendingMigrationsError
from .historical import HistoricalModelError
from .report import RunRefused, StatusReport, migration_span
from .steps import (
    Chunked,
    Irreversible,
    LoadedStep,
    NothingToReverse,
    StepRefused,
    chunked_query,
    declared_up_kind,
    load_step,
    unwritten,
)
from .target import Target, connection_dialect

if TYPE_CHECKING:
    from .._core import LockedDatabase, Plan, StepHandle, TrackedDatabase
    from ..raw import Transaction
    from ..settings import DatabaseSettings, FerroSettings

__all__ = [
    "MAX_LOCK_TIMEOUT_S",
    "InheritedEvent",
    "InheritedMigration",
    "AllowAhead",
    "AppliedStep",
    "DownPlan",
    "RunEvent",
    "RunNote",
    "RunReport",
    "StepEvent",
    "StepFailure",
    "connection_dialect",
    "down",
    "parse_lock_timeout",
    "parse_target",
    "plan_down",
    "require_applied",
    "status",
    "tracking_schema_for",
    "up",
]

AllowAhead: TypeAlias = bool | Literal["expand-only"]
"""How far `up` and `require_applied` let a database ahead of the checkout through.

`False` refuses any applied migration the directory lacks, `"expand-only"`
admits them when every step they hold is of class `expand`, `True` admits
any distance.
"""


_STEP_STEM = re.compile(r"(\.(up|down)(\.[a-z]+)?\.sql|\.py)$")
_TARGET = re.compile(r"(\d{4})(?::(\d{2}))?")


class AppliedStep(NamedTuple):
    """One step a run applied or reverted."""

    migration: str
    """The migration, `NNNN_<name>`."""
    step: str
    """The step, `NN_<name>`."""
    ms: int
    """How long the step ran, in milliseconds."""


@dataclass(frozen=True)
class StepFailure:
    """The step a run stopped at: its error, what the failure left applied, and how to resume.

    The run report's `refusal` is its `message`.
    """

    migration: str
    """The migration, `NNNN_<name>`."""
    file: str
    """The step's file (`02_backfill_account.py`)."""
    duration_ms: int
    """How long the step ran before it failed, in milliseconds."""
    error: str
    """What failed (`NotNullViolationError: null value in column ...`)."""
    hint: str
    """What stayed applied and how to resume (its last sentence is `Nothing after it ran.`)."""
    rows_done: int | None = None
    """The rows a chunked step's committed batches did before it failed; `None` for any other step."""

    @property
    def stem(self) -> str:
        """The step's name, its file without the suffix (`01_expand`)."""
        return _stem(self.file)

    @property
    def message(self) -> str:
        """The failure as the run prints it: `ferro migrate: <migration>/<file> failed: <error>`, then the hint."""
        return (
            f"ferro migrate: {self.migration}/{self.file} failed: {self.error}\n"
            f"{self.hint}"
        )


@dataclass(frozen=True)
class InheritedMigration:
    """A squash a run inherited before it planned.

    The database had applied every migration the squash replaced, so the run
    swapped their step records for the squash's and ran nothing.
    """

    squash: str
    """The squash, `NNNN_<name>`."""
    replaced: list[str]
    """Each migration whose records it replaced (`NNNN_<name>`), in chain order: what the database had applied."""

    @property
    def line(self) -> str:
        """The line a run prints: `inherited <squash>: this database had applied <first> … <last>; nothing was run`."""
        return (
            f"inherited {self.squash}: this database had applied "
            f"{migration_span(self.replaced)}; nothing was run"
        )


@dataclass
class RunReport:
    """What a migration run did: what [`up`][ferro.migrations.up] returns.

    A refused run raises `MigrationRefused` with this report as `.report`,
    saying what the run applied before it stopped.

    Examples:
        ```python {test=compile reason="needs a migrations project and a database"}
        report = await ferro.migrations.up()
        for step in report.applied:
            print(step.migration, step.step, step.ms)
        # 0002_add_teams 01_schema 9
        ```
    """

    inherited: list[InheritedMigration] = field(default_factory=list)
    """The squashes the run inherited before anything else, in order."""
    applied: list[AppliedStep] = field(default_factory=list)
    """The steps applied, in order."""
    reverted: list[AppliedStep] = field(default_factory=list)
    """The steps a down run reverted, newest first."""
    declined: bool = False
    """Whether a down run's `confirm` said no, so nothing was reverted."""
    refusal: str | None = None
    """Why the run stopped: a refusal before anything ran, a lost lock, or a failed step; `None` when it finished.

    For a failed step it holds the error and how to resume.
    """
    refused: RunRefused | None = None
    """The refusal itself when the run was refused; `None` when it finished or a step failed.

    `refusal` is its text; its `kind`, `migration`, `step` and `reason` say
    what it is about.
    """
    notes: list[str] = field(default_factory=list)
    """What the run accepted on the way (an edited unfinished step)."""
    ahead: list[str] = field(default_factory=list)
    """Applied migrations the directory lacks: let through by `allow_ahead`, or the ones that refused the run."""
    failed: StepFailure | None = None
    """The step the run stopped at when one failed (`refusal` is its message); `None` otherwise."""
    direction: Literal["up", "down"] = "up"
    """Which way the run went: `"up"` or `"down"`."""

    def _refuse(self, refused: RunRefused) -> None:
        self.refusal, self.refused = str(refused), refused

    def _raise_if_refused(self) -> RunReport:
        """Return this report, or raise `MigrationRefused` carrying it when the run refused or a step failed.

        The error is a `DatabaseAheadError` when applied migrations the
        directory lacks were the only thing in the way.
        """
        if self.refusal is None:
            return self
        if self.refused is not None and self.refused.ahead_only:
            raise DatabaseAheadError(
                self.ahead,
                self.refusal,
                report=self,
                contract_steps=self.refused.contract_steps,
            )
        raise MigrationRefused(self.refusal, report=self)


def tracking_schema_for(database: DatabaseSettings, dialect: str) -> str | None:
    """Return the schema a database's tracking tables live in on a dialect.

    Args:
        database: The configured database.
        dialect: `"postgres"` or `"sqlite"`.

    Returns:
        The database's `tracking_schema` on Postgres; `None` (the governed
            schema) otherwise.
    """
    return database.tracking_schema if dialect == "postgres" else None


async def open_tracked(name: str, database: DatabaseSettings) -> TrackedDatabase:
    """Open a database's tracking tables and migrations directory on an open connection.

    The result holds its records and one read of the directory. Creates
    nothing and takes no lock.

    Args:
        name: The open connection's name.
        database: The configured database.

    Returns:
        The tracked database.

    Raises:
        SettingsError: `name` is not open, or `database` does not target its
            dialect.
    """
    dialect = connection_dialect(name, database)
    return await _core._open_tracked(
        name,
        tracking_schema_for(database, dialect),
        str(database.directory),
        database.ddl_lock_timeout_seconds,
    )


def say_waiting(text: str) -> None:
    """Say on stderr, at once, that a run waits for another run's lock.

    Args:
        text: The line to print.
    """
    print(text, file=sys.stderr, flush=True)


def warn_unclassified(run: LockedDatabase) -> list[str]:
    """Warn, once per migration, of the records taking the lock left without a class.

    A verb that reads no class (`down`, `rerecord`, `baseline --remove`)
    does not refuse on a migration that does not regenerate. Those records
    read as `contract` until the next `up` or `baseline` classifies them or
    refuses naming the fix.

    Args:
        run: The locked database.

    Returns:
        The warnings, each also raised as a `UserWarning`.
    """
    deferred = list(run.class_warnings)
    for text in deferred:
        warnings.warn(text, UserWarning, stacklevel=3)
    return deferred


def _stem(file: str) -> str:
    return _STEP_STEM.sub("", file)


@dataclass(frozen=True)
class StepEvent:
    """One step a run finished: applied, reverted, reverted with nothing to reverse, or failed.

    `line()` is the line `progress` receives for it
    (`0002_add_team  01_expand  applied (412 ms)`); a failed step's is
    `None`, as `progress` hears nothing of it (its message is the run's
    refusal, in the report's `failed`).
    """

    migration: str
    """The migration, `NNNN_<name>`."""
    file: str
    """The step's file (`01_expand.up.postgres.sql`)."""
    outcome: Literal["applied", "reverted", "nothing to reverse", "failed"]
    """What happened: `"applied"`, `"reverted"`, `"nothing to reverse"` or `"failed"`."""
    duration_ms: int | None
    """How long the step ran; `None` when it had nothing to reverse."""
    reason: str | None
    """Why its down runs no statement, when `outcome` is `"nothing to reverse"`."""
    name_width: int = 0
    """The run's migration column width: its longest migration name."""
    stem_width: int = 0
    """The run's step column width: its longest step name."""

    @property
    def stem(self) -> str:
        """The step's name, its file without the suffix (`01_expand`)."""
        return _stem(self.file)

    @property
    def said(self) -> str:
        """What happened, as the line ends it: `applied (412 ms)`, `reverted (8 ms)` or `nothing to reverse: <reason>`."""
        if self.outcome == "nothing to reverse":
            return f"nothing to reverse: {self.reason}"
        return f"{self.outcome} ({self.duration_ms} ms)"

    def line(
        self, name_width: int | None = None, stem_width: int | None = None
    ) -> str | None:
        """Render the progress line `<migration>  <step>  <said>`.

        Args:
            name_width: The migration column's width. Defaults to the run's.
            stem_width: The step column's width. Defaults to the run's.

        Returns:
            The line, or `None` for a failed step.
        """
        if self.outcome == "failed":
            return None
        names = self.name_width if name_width is None else name_width
        stems = self.stem_width if stem_width is None else stem_width
        return _step_prefix(self.migration, self.file, names, stems) + self.said


@dataclass(frozen=True)
class RunNote:
    """Any other line a run says, such as a step waiting for a table lock.

    For example `0003_add_slug  01_add_slug  waiting for a lock on "author" ...`,
    or an edited unfinished step re-recorded before it runs.
    """

    text: str
    """The line."""

    def line(self) -> str:
        """Return the line `progress` receives: the text itself.

        Returns:
            The text.
        """
        return self.text


@dataclass(frozen=True)
class InheritedEvent:
    """A squash the run inherited, said before any step.

    Its line reads `inherited 0005_teams_epic: this database had applied
    0005_add_teams … 0009_fix_rows; nothing was run`.
    """

    squash: str
    """The squash, `NNNN_<name>`."""
    replaced: list[str]
    """Each migration whose records it replaced, `NNNN_<name>`."""

    def line(self) -> str:
        """Return the line `progress` receives.

        Returns:
            The same line the run report's `InheritedMigration.line` holds.
        """
        return InheritedMigration(self.squash, list(self.replaced)).line


RunEvent: TypeAlias = StepEvent | RunNote | InheritedEvent
"""What a run says: one event per line ``progress`` receives."""


def _declared_kind(path: Path) -> str:
    """The shape (``atomic`` / ``chunked``) an inherited data step's ``up``
    declares, read from its syntax tree: inheritance runs no step, so its
    record holds the declaration, as a baseline's does."""
    try:
        return declared_up_kind(Path(path))
    except StepRefused as refused:
        raise RunRefused(f"{refused}. Nothing was changed.") from None


async def _inherit(
    run: LockedDatabase, tell: Callable[[RunEvent], None]
) -> list[InheritedMigration]:
    """Inherit, on ``run``, every squash whose witness its records hold, before
    anything is planned, telling ``tell`` one event per squash
    inherited. Raises ``RunRefused`` (``partial_witness``) on a squash the
    records hold part of, and (``mixed_witness``) on one a baseline
    recorded part of, with nothing written."""
    inherited = [
        InheritedMigration(entry["squash"], list(entry["replaced"]))
        for entry in await run.inherit(_declared_kind)
    ]
    for migration in inherited:
        tell(InheritedEvent(migration.squash, list(migration.replaced)))
    return inherited


def _step_prefix(migration: str, file: str, name_width: int, stem_width: int) -> str:
    return f"{migration:<{name_width}}  {_stem(file):<{stem_width}}  "


async def up(
    settings: FerroSettings | None = None,
    database: str | None = None,
    *,
    using: str | None = None,
    url: str | None = None,
    lock_timeout: str | float | None = None,
    allow_ahead: AllowAhead = False,
    progress: Callable[[str], Any] | None = None,
    on_event: Callable[[RunEvent], Any] | None = None,
) -> RunReport:
    """Apply every pending migration, in order, under the run lock.

    This is `ferro migrate up`. A second run waits for the lock, saying so
    on stderr at once. See
    [Deploying migrations](../guide/schema/deploying.md).

    Args:
        settings: The project's configuration. Defaults to `FerroSettings()`.
        database: The configured database. Defaults to the only one
            configured.
        using: An open connection to work on. Defaults to the default
            connection.
        url: A URL to open a private connection to, closed after the call.
            Refused together with `using`.
        lock_timeout: How long to wait for another run's lock: `"30s"`,
            `"500ms"`, `"1m"` or a number of seconds; `0` refuses at once.
            Defaults to the database's `lock_timeout`.
        allow_ahead: Run beside a database that has applied migrations the
            directory lacks: `"expand-only"` when every step they hold is
            `expand` (the previous release of a rolling deploy), `True`
            whatever they hold. Defaults to `False`, which refuses.
        progress: Receives each line as the run goes: one per squash it
            inherits (before any step), one per applied step, and one per
            attempt a step makes while it waits for a table lock.
        on_event: Receives the same lines as data, each just before
            `progress` receives its `line()`: an `InheritedEvent` per squash
            inherited, a `StepEvent` per applied step, a `RunNote` for any
            other. A failed step is a `StepEvent` too, with no line for
            `progress`.

    Returns:
        What the run did: the squashes it inherited, in order, then the
            steps it applied.

    Raises:
        DatabaseAheadError: The database has applied migrations the
            directory lacks, and nothing else is in the way.
        MigrationRefused: The run refused or a step failed; the message says
            why and how to resume, and `.report` is the run's report. Also
            raised when the configuration names no single database, or for
            a connection the run cannot work on.
        TypeError: `allow_ahead` is not `False`, `True` or `"expand-only"`.

    Examples:
        ```python {test=compile reason="needs a migrations project and a database"}
        await ferro.connect(url)
        report = await ferro.migrations.up(progress=print)
        # 0002_add_teams  01_schema  applied (9 ms)
        print([step.migration for step in report.applied])
        # ['0002_add_teams']
        ```
    """
    allow_ahead = _core._allow_ahead(allow_ahead)
    target = Target.resolve(settings, database, using=using, url=url)
    async with target.open() as name:
        return await _up(
            name,
            target.database,
            lock_timeout=lock_timeout,
            allow_ahead=allow_ahead,
            progress=progress,
            on_event=on_event,
        )


async def _up(
    name: str,
    database: DatabaseSettings,
    *,
    lock_timeout: str | float | None = None,
    allow_ahead: AllowAhead = False,
    progress: Callable[[str], Any] | None = None,
    on_event: Callable[[RunEvent], Any] | None = None,
    through: str | None = None,
) -> RunReport:
    """`up` on open connection ``name``, raising as it does.

    ``through`` (``"0007"``, a migration number) stops the run after that
    migration: only pending steps of migrations up to and including it run.
    It is the test harness's target; the application's ``up()``
    has none, so it is reachable only here. A number the
    directory lacks is refused, and so is a step (``"0007:02"``): neither is
    a target. A ``through`` at or below the last applied migration has
    nothing pending to run: the run applies nothing and reverts nothing
    (going down is `down`'s).
    """
    timeout = database.lock_wait(lock_timeout)
    direction: dict[str, Any] = {"direction": "up"}
    if through is not None:
        direction["through"] = through
    report = RunReport()
    tell = _teller(progress, on_event)
    tracked = await open_tracked(name, database)
    try:
        async with tracked.locked(timeout, say_waiting) as run:
            report.inherited = await _inherit(run, tell)
            keys = _order_keys_for(run, "applied")
            try:
                plan = await run.plan(
                    direction, allow_ahead=allow_ahead, order_keys=keys
                )
            except RunRefused as refused:
                if refused.ahead_only:
                    report.ahead = list(run.status()["ahead"])
                raise
            walked = await _walk(run, plan, tell, using=name)
            walked.inherited = report.inherited
            report = walked
    except RunRefused as refused:
        report._refuse(refused)
    return report._raise_if_refused()


def _teller(
    progress: Callable[[str], Any] | None,
    on_event: Callable[[RunEvent], Any] | None,
) -> Callable[[RunEvent], None]:
    """Tell both hooks of one event: ``on_event`` the event, then
    ``progress`` its ``line()`` when it has one. The one place a run's lines are said, so
    the string hook is always the event's own line."""

    def tell(event: RunEvent) -> None:
        if on_event is not None:
            on_event(event)
        line = event.line()
        if progress is not None and line is not None:
            progress(line)

    return tell


async def _walk(
    run: LockedDatabase,
    plan: Plan,
    tell: Callable[[RunEvent], None] | None,
    *,
    using: str,
) -> RunReport:
    """Walk ``plan``'s steps on ``run``, in order, on connection ``using``:
    ``run.execute`` for a SQL step, a data step in Python with its record
    moved through the run's transitions. The plan's direction decides which
    transitions settle each step and whether a step is reported ``applied``
    or ``reverted``, each line told to ``tell`` as an event (``None``:
    to nobody). Stops at the first failed step (the report's ``failed``,
    its message the report's ``refusal``) or refusal."""
    down = plan.direction == "down"
    report = RunReport(ahead=list(plan.ahead), direction="down" if down else "up")
    steps = plan.steps
    if not steps:
        return report
    tell = tell or (lambda _event: None)
    done = report.reverted if down else report.applied
    try:
        loaded, reasons = _load_data_steps(steps, plan.direction)
        name_width = max(len(step.migration_name) for step in steps)
        stem_width = max(len(_stem(step.file)) for step in steps)
        with _HistoricalSwaps(run) as swaps:
            _check_chunked(steps, loaded, swaps, plan.direction)
            for step in steps:
                line = _step_line(step, name_width, stem_width, tell)
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
                    tell(RunNote(note))
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
                    tell(
                        StepEvent(
                            migration=step.migration_name,
                            file=step.file,
                            outcome="failed",
                            duration_ms=outcome["ms"],
                            reason=None,
                            name_width=name_width,
                            stem_width=stem_width,
                        )
                    )
                    report.failed = StepFailure(
                        migration=step.migration_name,
                        file=step.file,
                        duration_ms=outcome["ms"],
                        error=outcome["error"],
                        hint=outcome["hint"],
                        rows_done=outcome.get("rows_done"),
                    )
                    report.refusal = outcome["message"]
                    return report
                done.append(
                    AppliedStep(step.migration_name, _stem(step.file), outcome["ms"])
                )
                nothing = down and reason is not None
                tell(
                    StepEvent(
                        migration=step.migration_name,
                        file=step.file,
                        outcome=(
                            "nothing to reverse"
                            if nothing
                            else "reverted"
                            if down
                            else "applied"
                        ),
                        duration_ms=None if nothing else outcome["ms"],
                        reason=reason if nothing else None,
                        name_width=name_width,
                        stem_width=stem_width,
                    )
                )
    except RunRefused as refused:
        report._refuse(refused)
    return report


# -- historical models and order keys ----------------------------------------------


def _key(step: StepHandle) -> tuple[int, int]:
    return step.migration, step.step


def historical_models(migrations: dict[str, Any], number: int) -> HistoricalModels:
    """Build the historical models of one migration: its snapshot over its parent's.

    The one builder, for a data step's run and for the order keys of an
    edited chunked step alike.

    Args:
        migrations: The held read of the migrations directory.
        number: The migration's number.

    Returns:
        The historical models.

    Raises:
        HistoricalModelError: The snapshots do not build.
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
    """Read the order keys each cursor-holding chunked step's file pages over today.

    Only Python can read this (the query is a function of the migration's
    historical models). It is read for every record that is an unfinished
    chunked step holding a cursor: its `up`'s keys for an up that stopped
    part-way, its `down`'s for a record `reverting` at a revert cursor. The
    planner decides whether its file was edited and whether its cursor is
    still a position in the edited query. A directory that does not read
    gives no facts; the planner refuses it with its own text.

    Args:
        tracked: The tracked database, locked or not.

    Returns:
        Each step as `(migration, step, ["author.id", ...])`; the list is
            empty for a step whose function is no longer `@chunked`.

    Raises:
        MigrationRefused: Such a step's file does not load, or its query
            does not build over its historical models.
    """
    wanted = sorted(
        (record["migration"], record["step"], bool(record["reverting"]))
        for record in tracked.records
        if record["kind"] == "chunked"
        and (
            (record["finished_at"] is None and record["resume_cursor"] is not None)
            or (record["reverting"] and record["revert_cursor"] is not None)
        )
    )
    if not wanted:
        return []
    try:
        migrations = tracked.migrations
    except RunRefused:
        return []
    by_number = {m["number"]: m for m in migrations["migrations"]}
    facts: list[tuple[int, int, list[str]]] = []
    for number, ordinal, reverting in wanted:
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
        loaded = load_step(path, None)
        shape = (loaded.down if reverting else loaded.up).shape
        if not isinstance(shape, Chunked):
            facts.append((number, ordinal, []))
            continue
        models = historical_models(migrations, number)
        facts.append((number, ordinal, order_keys(chunked_query(shape, models, path))))
    return facts


def _order_keys_for(
    run: LockedDatabase, nothing: str
) -> list[tuple[int, int, list[str]]]:
    """`order_keys_on_disk` for a run that refuses on a file that does
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
    (`_check_chunked`)."""
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
    cannot be paged by keyset (`chunked_query`),
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
    migration, restored on exit, error and cancellation. Built
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
    transitions.

    An atomic step is one transaction on the run's connection, its record
    finished (going up) or removed (going down) inside that transaction. A
    chunked step runs through `run_chunked`,
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

    def _failed(
        self, ms: int, error: str, after: str, rows_done: int | None = None
    ) -> dict[str, Any]:
        """The outcome of a failed data step, in the SQL executor's shape:
        its ``error``, its ``hint`` (``after`` and ``Nothing after it
        ran.``) and the ``message`` they make."""
        failure = StepFailure(
            migration=self._step.migration_name,
            file=self._step.file,
            duration_ms=ms,
            error=error,
            hint=f"{after} Nothing after it ran.",
            rows_done=rows_done,
        )
        return {
            "ok": False,
            "ms": ms,
            "error": failure.error,
            "hint": failure.hint,
            "rows_done": rows_done,
            "message": failure.message,
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
                failed.rows_done,
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
                failed.rows_done,
            )
        return {"ok": True, "ms": _elapsed(clock)}


def _elapsed(clock: float) -> int:
    return int((time.monotonic() - clock) * 1000)


def _error_text(err: BaseException) -> str:
    text = str(err)
    return f"{type(err).__name__}: {text}" if text else type(err).__name__


def _step_line(
    step: StepHandle,
    name_width: int,
    stem_width: int,
    tell: Callable[[RunEvent], None],
) -> Callable[[str], None]:
    """Tell ``text`` as a note on ``step``'s row (a lock-wait attempt):
    ``<migration>  <step>  <text>``."""
    prefix = _step_prefix(step.migration_name, step.file, name_width, stem_width)

    def line(text: str) -> None:
        tell(RunNote(prefix + text))

    return line


# -- status -----------------------------------------------------------------------


async def status_of(
    tracked: TrackedDatabase | LockedDatabase,
    database: DatabaseSettings,
    *,
    lock_held: bool | None = None,
) -> StatusReport:
    """Report where a tracked database stands against its held directory read.

    Each step's state (a run holding the lock makes its step `running`), and
    the refusal `up` would meet. Takes no lock and creates nothing.

    Args:
        tracked: The tracked database, locked or not.
        database: The configured database.
        lock_held: Whether a run holds the lock. Defaults to asking the
            database. A locked run that executes nothing (a refused
            baseline) passes `False`: its own lock puts no step in progress.

    Returns:
        The status report.
    """
    try:
        keys: list[tuple[int, int, list[str]]] | None = order_keys_on_disk(tracked)
    except MigrationRefused:
        # A step file that does not load: status still answers, and the
        # edited-chunked refusal offers --continue on its condition.
        keys = None
    held = await tracked.lock_held() if lock_held is None else lock_held
    raw = tracked.status(keys, lock_held=held)
    # A step's own records only: a squash's originals share its numbers.
    names = {m["name"] for m in raw["migrations"]}
    own = [r for r in tracked.records if r["migration_name"] in names]
    return StatusReport.from_core(
        raw,
        database=database.name,
        url=tracked.url,
        dialect=tracked.dialect,
        table=tracked.tracking_table,
        rows_done={
            (r["migration"], r["step"]): r["rows_done"]
            for r in own
            if r["kind"] == "chunked" and r["rows_done"] is not None
        },
        finished_at={(r["migration"], r["step"]): r["finished_at"] for r in own},
    )


async def status(
    settings: FerroSettings | None = None,
    database: str | None = None,
    *,
    using: str | None = None,
    url: str | None = None,
) -> StatusReport:
    """Report where the database stands against its migrations directory.

    This is `ferro migrate status`. It takes no lock and creates nothing: a
    database without the tracking table reports every migration pending.
    The refusal `up` would meet (an edited file, a broken chain) is part of
    the report.

    Args:
        settings: The project's configuration. Defaults to `FerroSettings()`.
        database: The configured database. Defaults to the only one
            configured.
        using: An open connection to work on. Defaults to the default
            connection.
        url: A URL to open a private connection to, closed after the call.
            Refused together with `using`.

    Returns:
        The status report: each migration's state, and whether anything is
            pending or needs attention.

    Raises:
        MigrationRefused: The configuration names no single database, or
            there is no connection to work on.

    Examples:
        ```python {test=compile reason="needs a migrations project and a database"}
        report = await ferro.migrations.status()
        print(report.head_applied.name, report.pending)
        # 0006_add_teams True
        ```
    """
    target = Target.resolve(settings, database, using=using, url=url)
    async with target.open() as name:
        return await status_of(
            await open_tracked(name, target.database), target.database
        )


async def require_applied(
    settings: FerroSettings | None = None,
    database: str | None = None,
    *,
    using: str | None = None,
    url: str | None = None,
    allow_ahead: AllowAhead = False,
) -> None:
    """Return when the database stands at the head of its migrations; raise otherwise.

    Takes no lock and changes nothing. The answer is the run planner's: the
    plan `up` would make, read with no lock, after the squash inheritance it
    would make first. A database that applied a squash's originals is
    pending that squash, never ahead by them, whatever `allow_ahead` says.
    A step's class is read from its step record; a record an older Ferro
    wrote holds none and reads as `contract` until the first run of this
    Ferro on the database classifies it.

    Args:
        settings: The project's configuration. Defaults to `FerroSettings()`.
        database: The configured database. Defaults to the only one
            configured.
        using: An open connection to work on. Defaults to the default
            connection.
        url: A URL to open a private connection to, closed after the call.
            Refused together with `using`.
        allow_ahead: Let a database that has applied migrations this
            checkout lacks through: `"expand-only"` when every step they
            hold is `expand` (the previous release of a rolling deploy),
            `True` whatever they hold. Defaults to `False`, which refuses.

    Raises:
        PendingMigrationsError: A migration is pending, or the tracking
            table refuses a run (an interrupted run, an edited applied file).
        DatabaseAheadError: The database has applied migrations this
            checkout lacks.
        TypeError: `allow_ahead` is not `False`, `True` or `"expand-only"`.

    Examples:
        ```python {test=compile reason="needs a migrations project and a database"}
        try:
            await ferro.migrations.require_applied(allow_ahead="expand-only")
        except ferro.migrations.DatabaseAheadError as ahead:
            print(ahead.contract_steps)  # ['0008_drop_bio:01']
        else:
            print("the database is at the head")
        ```
    """
    allow_ahead = _core._allow_ahead(allow_ahead)
    target = Target.resolve(settings, database, using=using, url=url)
    async with target.open() as name:
        tracked = await open_tracked(name, target.database)
        report = StatusReport.from_core(
            tracked.status(),
            database=target.database.name,
            url=tracked.url,
            dialect=tracked.dialect,
            table=tracked.tracking_table,
        )
        pending = [m.name for m in report.migrations if any(s.pending for s in m.steps)]
        if tracked.refusal is not None:
            raise PendingMigrationsError(pending, [tracked.refusal])
        try:
            await tracked.plan(
                {"direction": "up"}, allow_ahead=allow_ahead, inheriting=_declared_kind
            )
        except RunRefused as refused:
            if refused.ahead_only:
                raise DatabaseAheadError(
                    report.ahead,
                    str(refused),
                    contract_steps=refused.contract_steps,
                ) from None
            raise PendingMigrationsError(pending, [str(refused)]) from None
    if pending:
        raise PendingMigrationsError(pending)


# -- down ------------------------------------------------------------------------


class DownStep(NamedTuple):
    """One step a down run would revert."""

    migration: str
    """The migration, `NNNN_<name>`."""
    step: str
    """The step, `NN_<name>`."""
    nothing_to_reverse: str | None
    """Why its down runs no statement, when it runs none."""
    file: str
    """The step's file (`01_schema.up.postgres.sql`), which names its kind."""


@dataclass
class DownPlan:
    """What a down run would revert, newest first, or why it would not."""

    steps: list[DownStep] = field(default_factory=list)
    """The steps it would revert, newest first."""
    refusal: str | None = None
    """Why it would be refused, or `None`."""

    def describe(self) -> str:
        """Render the plan as the prompt shows it, one line per step.

        Returns:
            The plan, or `nothing to revert`.
        """
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
        """Render the prompt's question, such as `Revert 0002_add_teams (1 step)?`.

        Returns:
            The question.
        """
        migrations = list(dict.fromkeys(step.migration for step in self.steps))
        count = len(self.steps)
        return (
            f"Revert {', '.join(migrations)} ({count} step{'' if count == 1 else 's'})?"
        )


def parse_target(target: str | None = None, *, all: bool = False) -> dict[str, Any]:
    """Parse where a down run stops into the run planner's target.

    Args:
        target: `"0005"` keeps 0005 applied (`"0000"` reverts everything);
            `"0007:02"` keeps steps 01-02 of 0007 applied. Defaults to
            reverting the latest migration.
        all: Revert everything. Refused together with `target`.

    Returns:
        The planner's direction and target.

    Raises:
        SettingsError: Both `target` and `all` are given, or `target` is not
            a migration or a step.
    """
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
    """What a ``down`` would revert, planned on ``tracked`` with no lock, as
    the run plans it once it has inherited every squash whose witness the
    records hold: a data step's down declares itself in Python,
    so an irreversible one refuses here too (before any lock), and a
    nothing-to-reverse one says so.

    Raises:
        RunRefused: the down would be refused.
    """
    try:
        keys = order_keys_on_disk(tracked)
    except MigrationRefused as refused:
        raise RunRefused(f"{refused}. Nothing was reverted.") from None
    plan = await tracked.plan(direction, order_keys=keys, inheriting=_declared_kind)
    _, reasons = _load_data_steps(plan.steps, "down")
    shown = DownPlan(
        steps=[
            DownStep(
                step.migration_name,
                _stem(step.file),
                step.nothing_to_reverse or reasons.get(_key(step)),
                step.file,
            )
            for step in plan.steps
        ]
    )
    return plan, shown


async def plan_down(
    settings: FerroSettings | None = None,
    database: str | None = None,
    *,
    using: str | None = None,
    url: str | None = None,
    target: str | None = None,
    all: bool = False,
) -> DownPlan:
    """Report what a down run would revert, read with no lock, changing nothing.

    Args:
        settings: The project's configuration. Defaults to `FerroSettings()`.
        database: The configured database. Defaults to the only one
            configured.
        using: An open connection to work on. Defaults to the default
            connection.
        url: A URL to open a private connection to, closed after the call.
            Refused together with `using`.
        target: Where to stop, as `parse_target` reads it. Defaults to the
            latest migration.
        all: Revert everything.

    Returns:
        The plan; its `refusal` says why the run would be refused.
    """
    direction = parse_target(target, all=all)
    where = Target.resolve(settings, database, using=using, url=url)
    async with where.open() as name:
        tracked = await open_tracked(name, where.database)
        try:
            return (await _preview_down(tracked, direction))[1]
        except RunRefused as refused:
            return DownPlan(refusal=str(refused))


_INHERITANCE_STAMP = ("started_at", "finished_at", "ferro_version")
"""What an inherited record holds of the moment it was inherited: the preview
plans over the records inheritance would write, the run over the ones it
wrote a moment later."""


def _records_as_shown(locked: list[Any], shown: list[Any]) -> bool:
    """Whether the steps planned under the lock stand on the records the
    preview showed. Taking the lock gives a record an older ferro wrote
    without a class the class the lock decides for it; that fills a gap and
    changes nothing the plan was shown, so a class the preview read as
    missing is not compared. An inherited record is compared without the time
    and version it was inherited at (`_INHERITANCE_STAMP`)."""

    def standing(step: Any, missing_class: bool) -> dict[str, Any] | None:
        record = step.standing
        if record is None:
            return record
        skipped = ("class",) if missing_class else ()
        if record.get("origin") == "inherited":
            skipped += _INHERITANCE_STAMP
        return {key: value for key, value in record.items() if key not in skipped}

    if len(locked) != len(shown):
        return False
    for now, before in zip(locked, shown, strict=True):
        missing = before.standing is not None and before.standing.get("class") is None
        if standing(now, missing) != standing(before, missing):
            return False
    return True


async def down(
    settings: FerroSettings | None = None,
    database: str | None = None,
    *,
    using: str | None = None,
    url: str | None = None,
    target: str | None = None,
    all: bool = False,
    lock_timeout: str | float | None = None,
    confirm: Callable[[DownPlan], bool] | None = None,
    progress: Callable[[str], Any] | None = None,
    on_event: Callable[[RunEvent], Any] | None = None,
) -> RunReport:
    """Revert applied steps, newest first, under the run lock.

    This is `ferro migrate down`. A step whose down is declared
    irreversible, or one a baseline recorded, refuses the whole run before
    anything is reverted. Once the lock is held the plan is made again, and
    a database that changed since `confirm` saw it is refused rather than
    reverted unseen. Under the lock and before it plans, a down inherits
    every squash whose witness the records hold, as `up` does.

    Args:
        settings: The project's configuration. Defaults to `FerroSettings()`.
        database: The configured database. Defaults to the only one
            configured.
        using: An open connection to work on. Defaults to the default
            connection.
        url: A URL to open a private connection to, closed after the call.
            Refused together with `using`.
        target: Where to stop: `"0005"` leaves 0005 fully applied (`"0000"`
            reverts everything), `"0007:02"` leaves steps 01-02 of 0007
            applied. Defaults to the latest migration with a record, a
            partly applied one included.
        all: Revert everything. Refused together with `target`.
        lock_timeout: How long to wait for another run's lock: `"30s"`,
            `"500ms"`, `"1m"` or a number of seconds; `0` refuses at once.
            Defaults to the database's `lock_timeout`.
        confirm: Shown the plan before the lock is taken; when it returns
            false nothing is reverted. Defaults to reverting without asking.
        progress: Receives one line per squash inherited, then one per
            reverted step.
        on_event: Receives each line as data just before `progress`
            receives its `line()`: an `InheritedEvent` per squash inherited,
            a `StepEvent` per reverted step, a `RunNote` for any other. A
            failed down is a `StepEvent` with no line.

    Returns:
        What the run did: the squashes it inherited, then the steps it
            reverted, newest first (`declined` when `confirm` said no).

    Raises:
        MigrationRefused: The run refused or a down failed; `.report` is the
            run's report. A failed down that rolled back whole leaves its
            step applied and its record as it was; one that left part of
            itself applied (a no-transaction SQL down, a chunked down past
            its first batch) leaves the record carrying the error, a chunked
            one still `reverting` at its cursor. The next down resumes at it.
        SettingsError: `target` is not a migration or a step, or both
            `target` and `all` are given.
    """
    direction = parse_target(target, all=all)
    report = RunReport(direction="down")
    where = Target.resolve(settings, database, using=using, url=url)
    timeout = where.database.lock_wait(lock_timeout)
    async with where.open() as name:
        tracked = await open_tracked(name, where.database)
        try:
            seen, shown = await _preview_down(tracked, direction)
        except RunRefused as refused:
            report._refuse(refused)
            return report._raise_if_refused()
        if not seen.steps:
            return report
        if confirm is not None and not confirm(shown):
            report.declined = True
            return report
        try:
            async with tracked.locked(
                timeout, say_waiting, decides_classes=False
            ) as run:
                deferred = warn_unclassified(run)
                report.notes.extend(deferred)
                tell = _teller(progress, on_event)
                report.inherited = await _inherit(run, tell)
                plan = await run.plan(
                    direction, order_keys=_order_keys_for(run, "reverted")
                )
                if not _records_as_shown(plan.steps, seen.steps):
                    report.refusal = (
                        "ferro migrate: the database's migration records changed while "
                        "the plan was shown; nothing was reverted. Run `ferro migrate "
                        "down` again to see the plan as it stands now."
                    )
                    return report._raise_if_refused()
                inherited = report.inherited
                report = await _walk(run, plan, tell, using=name)
                report.inherited = inherited
                report.notes[:0] = deferred
        except RunRefused as refused:
            report._refuse(refused)
    return report._raise_if_refused()
