"""``ferro migrate rerecord``: accept a deliberate edit of an applied step (ADR-0030).

```text
$ ferro migrate up
ferro migrate: 0007_nickname/01_add_nickname.up.postgres.sql was edited after it was applied to this database.
  applied   sha384:3f9a…c1  (2026-10-01 14:02 UTC)
  on disk   sha384:b7e2…09
An applied step is never run again. Restore the file, or accept a deliberate edit with
`ferro migrate rerecord 0007:01`. Nothing was applied.
$ ferro migrate rerecord 0007:01
re-recorded 0007_nickname/01_add_nickname.up.postgres.sql (sha384:3f9a…c1 → sha384:b7e2…09); nothing was run
```

A step record's checksum covers the up file this database executed, in
its own dialect. ``up`` never runs a finished step again, so an edit to
one is refused until the operator says it is deliberate; ``rerecord``
then changes the record and nothing else: no statement of the step runs.

An unfinished chunked step whose batches already committed rows is the
other case that needs a decision (``up`` refuses it): ``--continue`` keeps
the committed rows and resumes from the cursor, allowed only while the
edited query pages over the same order keys the cursor was committed
under; ``--restart`` clears the cursor and ``rows_done`` so the next ``up``
starts from the first row.

The Rust core decides (``_core._rerecord_plan``: which record, which
refusal) and writes (``_core._rerecord``: one statement under the run
lock); this module sequences the two and reads the facts only Python can:
a data step's declared kind and an edited chunked query's order keys.
``rerecord`` is a CLI verb and an in-process call for the CLI, never part
of the application API (ADR-0045).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from .. import _core
from ..settings import SettingsError
from . import runner
from .errors import MigrationRefused
from .report import RunRefused
from .steps import declared_up_kind

if TYPE_CHECKING:
    from ..settings import DatabaseSettings, FerroSettings

__all__ = ["RerecordReport", "rerecord"]

Mode = Literal["record", "continue", "restart"]
_MODES = ("record", "continue", "restart")


@dataclass(frozen=True)
class RerecordReport:
    """What ``rerecord`` changed: the record of ``file``
    (``NNNN_<name>/<file>``) now holds ``new_checksum`` where it held
    ``old_checksum``."""

    file: str
    old_checksum: str
    new_checksum: str
    mode: Mode = "record"

    def render(self) -> str:
        """The line ``ferro migrate rerecord`` prints."""
        done = {
            "record": "",
            "continue": "; the next up continues from its cursor",
            "restart": (
                "; its cursor and rows_done are cleared, so the next up starts from "
                "the first row"
            ),
        }[self.mode]
        return (
            f"re-recorded {self.file} (sha384:{self.old_checksum} → "
            f"sha384:{self.new_checksum}){done}; nothing was run"
        )


async def rerecord(
    settings: FerroSettings,
    database: DatabaseSettings,
    target: str,
    *,
    mode: Mode = "record",
    using: str | None = None,
    url: str | None = None,
    lock_timeout: str | float = "30s",
) -> RerecordReport:
    """Accept a deliberate edit of step ``target`` (``"0007:01"``) under the
    run lock: its record takes the file's checksum (and file name and kind),
    and nothing of the step runs.

    ``mode`` is ``"continue"`` or ``"restart"`` for an unfinished chunked
    step with committed batches, and ``"record"`` for anything else.
    ``using`` names an open connection; otherwise ``database``'s URL (or
    ``url``) is connected and closed after. A second run waits up to
    ``lock_timeout``.

    Raises:
        RunRefused: ``target`` is not one step (a migration alone, the
            snapshot: restore the file), names a step with no record (free to
            edit) or an unchanged one, ``mode`` does not fit the step, the
            edited chunked step needs ``--continue`` or ``--restart``, or
            ``--continue`` meets changed order keys. Each names the fix; the
            refusal's ``kind`` says which.
        MigrationRefused: an edited data step's file does not load.
    """
    del settings  # the database carries its project; kept for API symmetry
    if mode not in _MODES:
        raise SettingsError(f"rerecord mode {mode!r} is not one of {', '.join(_MODES)}")
    timeout = runner.parse_lock_timeout(lock_timeout)
    async with runner._connection(database, using, url) as name:
        dialect = runner.connection_dialect(name, database)
        tracking = runner.tracking_schema_for(database, dialect)
        handle = await _core._acquire_run_lock(name, None, timeout, runner._say_waiting)
        try:
            return await _rerecord(
                name, database, dialect, tracking, handle, target, mode
            )
        finally:
            await _core._release_run_lock(handle)


async def _rerecord(
    name: str,
    database: DatabaseSettings,
    dialect: str,
    tracking: str | None,
    handle: int,
    target: str,
    mode: Mode,
) -> RerecordReport:
    state = json.loads(await _core._read_records(name, tracking))
    if state["refusal"] is not None:
        raise RunRefused(state["refusal"])
    records = state["records"]
    try:
        keys = runner.order_keys_on_disk(database.directory, records)
    except MigrationRefused as refused:
        raise RunRefused(f"{refused}. Nothing was changed.") from None
    action = json.loads(
        _core._rerecord_plan(
            str(database.directory),
            json.dumps(records),
            target,
            mode,
            dialect,
            keys,
        )
    )
    if action["data"]:
        # A data step's record holds the shape its up declares, read from the
        # file's syntax tree as baseline reads it (the file is never run).
        try:
            action["kind"] = declared_up_kind(Path(action["path"]))
        except MigrationRefused as refused:
            raise RunRefused(f"{refused}. Nothing was changed.") from None
    await _core._rerecord(name, json.dumps(action), tracking, handle)
    return RerecordReport(
        file=f"{action['migration_name']}/{action['file']}",
        old_checksum=action["old_checksum"],
        new_checksum=action["new_checksum"],
        mode=mode,
    )
