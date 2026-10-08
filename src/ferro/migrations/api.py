"""The migration calls an application makes (ADR-0038, ADR-0045).

```python
await ferro.connect(url)
await ferro.migrations.up()                # a desktop app with no deploy step
await ferro.migrations.require_applied()   # a server that migrates in its deploy step
```

Each call reads the project's configuration (``settings=None`` is
``FerroSettings()``), picks its database (``database=None`` is the one
configured, or a refusal naming them all) and works on an open connection:
``using=`` names it, the default connection otherwise. They are the CLI's
own functions: ``up()`` is ``ferro migrate up``, ``status()`` is ``ferro
migrate status``, ``check()`` is ``ferro migrate check``. ``down`` and
``rerecord`` stay CLI verbs: they are operator decisions with prompts.

A refusal is raised, never returned: an application that called ``up()``
must not go on serving a database the run did not reach.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .. import _core
from ..settings import FerroSettings, SettingsError
from . import generate, runner
from .errors import DatabaseAheadError, MigrationRefused, PendingMigrationsError
from .report import RunRefused, StatusReport

if TYPE_CHECKING:
    from ..settings import DatabaseSettings
    from .generate import CheckReport
    from .runner import RunReport

__all__ = ["check", "require_applied", "status", "up"]


def _resolve(
    settings: FerroSettings | None, database: str | None
) -> tuple[FerroSettings, DatabaseSettings]:
    """The project's settings and the database a call works on.

    Raises :class:`MigrationRefused` when no config is found, when several
    databases are configured and ``database`` names none, or when it names
    one that is not configured.
    """
    try:
        settings = FerroSettings() if settings is None else settings
        return settings, settings.database(database)
    except SettingsError as err:
        raise MigrationRefused(str(err)) from None


def _connection(using: str | None) -> str:
    """The open connection a call runs on: ``using``, else the default."""
    if using is not None:
        return using
    name = _core._default_connection_name()
    if name is None:
        raise MigrationRefused(
            "ferro.migrations: there is no default connection; call "
            "`await ferro.connect(url)` first, or pass using=<connection name>"
        )
    return name


async def up(
    settings: FerroSettings | None = None,
    database: str | None = None,
    *,
    using: str | None = None,
    lock_timeout: str = "30s",
    allow_ahead: bool = False,
) -> RunReport:
    """Apply every pending migration, in order, under the run lock
    (``ferro migrate up``).

    Waits up to ``lock_timeout`` for another run's lock. Returns the
    :class:`~ferro.migrations.runner.RunReport` of what it applied.

    Raises:
        DatabaseAheadError: the database has applied migrations this
            checkout lacks (``allow_ahead=True`` runs beside them).
        MigrationRefused: the run refused or a step failed; the message
            says why and how to resume.
    """
    settings, db = _resolve(settings, database)
    return await runner.up(
        settings,
        db,
        using=_connection(using),
        lock_timeout=lock_timeout,
        allow_ahead=allow_ahead,
    )


async def require_applied(
    settings: FerroSettings | None = None,
    database: str | None = None,
    *,
    using: str | None = None,
    allow_ahead: bool = False,
) -> None:
    """Return when the database stands at the head of its migrations; raise
    otherwise. Takes no lock and changes nothing.

    Raises:
        PendingMigrationsError: a migration is pending (``.pending``), or the
            tracking table refuses a run (``.refusals``: an interrupted run,
            an edited applied file, ...).
        DatabaseAheadError: the database has applied migrations this
            checkout lacks (``allow_ahead=True`` lets it through).
    """
    _, db = _resolve(settings, database)
    tracked = await runner.open_tracked(_connection(using), db)
    report = StatusReport.from_core(
        tracked.status(),
        database=db.name,
        dialect=tracked.dialect,
        table=tracked.tracking_table,
    )
    pending = [m.name for m in report.migrations if any(s.pending for s in m.steps)]
    if tracked.refusal is not None:
        raise PendingMigrationsError(pending, [tracked.refusal])
    try:
        await tracked.plan({"direction": "up"}, allow_ahead=allow_ahead)
    except RunRefused as refused:
        if refused.ahead_only:
            raise DatabaseAheadError(report.ahead, str(refused)) from None
        raise PendingMigrationsError(pending, [str(refused)]) from None
    if pending:
        raise PendingMigrationsError(pending)


async def status(
    settings: FerroSettings | None = None,
    database: str | None = None,
    *,
    using: str | None = None,
) -> StatusReport:
    """Where the database stands against its migrations (``ferro migrate
    status``). Takes no lock and changes nothing."""
    settings, db = _resolve(settings, database)
    return await runner.status(settings, db, using=_connection(using))


async def check(
    settings: FerroSettings | None = None, database: str | None = None
) -> CheckReport:
    """Check, offline, that every model change has a migration and the
    directory is intact (``ferro migrate check``). Raises nothing for a
    problem: ``.raise_for_problems()`` on the report does."""
    settings, db = _resolve(settings, database)
    return generate.check(settings, db)
