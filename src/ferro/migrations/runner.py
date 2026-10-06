"""``ferro migrate up`` and ``ferro migrate status``: the run loop (ADR-0028).

```text
$ ferro migrate up
0001_create_author  01_schema  applied (12 ms)
0002_add_teams      01_schema  applied (9 ms)
```

Python sequences a run; the Rust core decides and executes it. :func:`up`
takes the run lock, reads the tracking table, asks ``_core._run_plan`` for
the ordered pending steps (or the refusal that stops the run), and calls
``_core._execute_sql_step`` once per step, which verifies the lock, runs the
file and writes its step record. Nothing here decides which step runs, how,
or whether a file may run at all.

:func:`status` reads the same records with no lock and creates nothing.
"""

from __future__ import annotations

import json
import re
import sys
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

from .. import _core
from .._core import _acquire_run_lock, _execute_sql_step
from ..settings import _DURATION, _DURATION_UNITS, SettingsError
from .report import RunRefused, StatusReport

if TYPE_CHECKING:
    from ..settings import DatabaseSettings, FerroSettings

__all__ = ["AppliedStep", "RunReport", "parse_lock_timeout", "status", "up"]

_UP = json.dumps({"direction": "up"})
_STEP_STEM = re.compile(r"\.(up|down)(\.[a-z]+)?\.sql$")


class AppliedStep(NamedTuple):
    """One step a run applied."""

    migration: str
    """``NNNN_<name>``."""
    step: str
    """``NN_<name>``."""
    ms: int


@dataclass
class RunReport:
    """What :func:`up` did."""

    applied: list[AppliedStep] = field(default_factory=list)
    """The steps applied, in order."""
    refusal: str | None = None
    """Why the run stopped: a refusal before anything ran, a lost lock, or a
    failed step (its error and how to resume). ``None`` when it finished."""
    notes: list[str] = field(default_factory=list)
    """What the run accepted on the way (an edited unfinished step)."""
    ahead: list[str] = field(default_factory=list)
    """Applied migrations the directory lacks, let through by ``allow_ahead``."""


def parse_lock_timeout(value: str | float) -> float:
    """Seconds from ``"30s"``, ``"500ms"``, ``"1m"`` or a plain number of
    seconds (``0`` tries the lock once)."""
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
    return seconds


def _ferro_version() -> str:
    try:
        return version("ferro-orm")
    except PackageNotFoundError:  # pragma: no cover - a source tree without metadata
        return "unknown"


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


def _dialect(name: str, database: DatabaseSettings) -> str:
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


def _tracking_schema(database: DatabaseSettings, dialect: str) -> str | None:
    return database.tracking_schema if dialect == "postgres" else None


def _say_waiting(text: str) -> None:
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
) -> RunReport:
    """Apply every pending step of every pending migration, in order, under
    the run lock.

    ``using`` names an open connection; otherwise ``database``'s URL (or
    ``url``) is connected for the run and closed after. A second run waits
    up to ``lock_timeout``, saying so on stderr at once. ``allow_ahead`` lets
    a database holding migrations the directory lacks through (ADR-0038).
    ``progress`` receives each line as the run goes (one per applied step).

    Returns a :class:`RunReport`; a refusal or a failed step is reported in
    ``refusal``, not raised.
    """
    del settings  # the database carries its project; kept for API symmetry
    timeout = parse_lock_timeout(lock_timeout)
    report = RunReport()
    say = progress or (lambda _line: None)
    async with _connection(database, using, url) as name:
        dialect = _dialect(name, database)
        tracking = _tracking_schema(database, dialect)
        try:
            handle = await _acquire_run_lock(name, None, timeout, _say_waiting)
        except RunRefused as refused:
            report.refusal = str(refused)
            return report
        try:
            await _run(
                name, database, dialect, tracking, handle, allow_ahead, report, say
            )
        except RunRefused as refused:
            report.refusal = str(refused)
        finally:
            await _core._release_run_lock(handle)
    return report


async def _run(
    name: str,
    database: DatabaseSettings,
    dialect: str,
    tracking: str | None,
    handle: int,
    allow_ahead: bool,
    report: RunReport,
    say: Callable[[str], Any],
) -> None:
    state = json.loads(await _core._read_records(name, tracking))
    if state["refusal"] is not None:
        raise RunRefused(state["refusal"])
    records = state["records"]
    live = None if records else await _core._live_tables(name)
    plan = json.loads(
        _core._run_plan(
            str(database.directory),
            json.dumps(records),
            dialect,
            _UP,
            allow_ahead,
            live,
        )
    )
    report.ahead = list(plan["ahead"])
    steps = plan["steps"]
    if not steps:
        return
    await _core._ensure_tracking_tables(name, tracking)
    name_width = max(len(step["migration_name"]) for step in steps)
    stem_width = max(len(_stem(step["file"])) for step in steps)
    ferro_version = _ferro_version()
    for step in steps:
        shown = f"{step['migration_name']}/{step['file']}"
        if step["edited"] is not None:
            note = (
                f"{shown} changed since its unfinished attempt "
                f"(sha384:{step['edited']['recorded']}); running the file as it is "
                f"now and re-recording its checksum (sha384:{step['checksum']})"
            )
            report.notes.append(note)
            say(note)
        try:
            sql = Path(step["path"]).read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError) as err:
            raise RunRefused(
                f"ferro migrate: cannot read {shown} ({err}). Nothing more was applied."
            ) from None
        record = {**step["record"], "ferro_version": ferro_version}
        outcome = json.loads(
            await _execute_sql_step(
                name, json.dumps(step), sql, json.dumps(record), tracking, handle
            )
        )
        if not outcome["ok"]:
            report.refusal = outcome["message"]
            return
        stem = _stem(step["file"])
        report.applied.append(AppliedStep(step["migration_name"], stem, outcome["ms"]))
        say(
            f"{step['migration_name']:<{name_width}}  {stem:<{stem_width}}  "
            f"applied ({outcome['ms']} ms)"
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
        dialect = _dialect(name, database)
        tracking = _tracking_schema(database, dialect)
        state = json.loads(await _core._read_records(name, tracking))
        held = await _core._run_lock_is_held(name, None)
        raw = json.loads(
            _core._run_status(
                str(database.directory),
                json.dumps(state["records"]),
                dialect,
                held,
            )
        )
    return StatusReport.from_core(
        raw,
        database=database.name,
        dialect=dialect,
        table=state["table"],
        refusal=state["refusal"],
    )
