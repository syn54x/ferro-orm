"""The database a migration verb works on, and the connection that reaches it.

```python
await ferro.migrations.up()                          # the configured database, default connection
await ferro.migrations.up(database="analytics", using="reporting")
await ferro.migrations.up(url="postgres://…/app")    # a private connection, closed after
```

Every verb (``up``, ``down``, ``status``, ``require_applied``, ``drift``,
``baseline``, ``remove_baseline``, ``rerecord``, ``check``) takes the same
``(settings=None, database=None, *, using=None, url=None)`` and hands them
to :meth:`Target.resolve`, which decides three things once: the project's
configuration (``settings=None`` is ``FerroSettings()``), which configured
database (``database=None`` is the only one, or a refusal naming them all),
and which connection (``using`` names an open one, ``url`` opens a private
one, neither is the default connection; both is refused). The CLI calls the
same verbs with ``url=``: the ``--url`` flag, else the database's
``url_env``.

A configuration that names no single database, and a connection the verb
cannot work on, are :class:`~ferro.migrations.errors.MigrationRefused`
naming the fix, for every caller.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from .. import _core
from ..settings import DatabaseSettings, FerroSettings, SettingsError
from .errors import MigrationRefused

__all__ = ["Target", "connection_dialect"]


def connection_dialect(name: str, database: DatabaseSettings) -> str:
    """The dialect of open connection ``name``.

    Raises:
        SettingsError: ``name`` is not open, or ``database`` does not target
            its dialect.
    """
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


@dataclass(frozen=True)
class Target:
    """The database a verb works on: its project's configuration, the
    configured database, and the connection choice (``using``, ``url``, or
    neither: the default connection). Build it with :meth:`resolve`; work on
    it inside :meth:`open`."""

    settings: FerroSettings
    database: DatabaseSettings
    using: str | None = None
    """An open connection's name."""
    url: str | None = None
    """A URL to open a private connection to, for the verb's lifetime."""

    @classmethod
    def resolve(
        cls,
        settings: FerroSettings | None = None,
        database: str | None = None,
        *,
        using: str | None = None,
        url: str | None = None,
    ) -> Target:
        """The target ``settings`` (``None``: ``FerroSettings()``),
        ``database`` (``None``: the only one configured) and ``using`` /
        ``url`` name. Opens nothing.

        Raises:
            MigrationRefused: no configuration is found, several databases
                are configured and ``database`` names none, ``database``
                names one that is not configured, or both ``using`` and
                ``url`` are given.
        """
        try:
            settings = FerroSettings() if settings is None else settings
            chosen = settings.database(database)
        except SettingsError as err:
            raise MigrationRefused(str(err)) from None
        if using is not None and url is not None:
            raise MigrationRefused("pass using= or url=, not both")
        return cls(settings, chosen, using, url)

    @asynccontextmanager
    async def open(self) -> AsyncIterator[str]:
        """The name of the connection to work on, for the block: ``using``,
        a private connection to ``url`` (closed when the block ends), or the
        default connection. The connection is checked before the block runs:
        it is open, and its dialect is one the database targets.

        Only reaching the connection is translated; whatever the block raises
        reaches the caller unchanged.

        Raises:
            MigrationRefused: there is no default connection, ``using`` names
                a connection that is not open, or the connection's dialect is
                not one the database targets.
        """
        if self.url is None:
            name = self._existing_connection()
            self._check(name)
            yield name
            return
        from .. import connect

        name = f"_ferro_migrate_{uuid.uuid4().hex}"
        await connect(self.url, name=name)
        try:
            self._check(name)
            yield name
        finally:
            await _core._disconnect(name)

    def _existing_connection(self) -> str:
        """``using``, else the default connection's name."""
        if self.using is not None:
            return self.using
        name = _core._default_connection_name()
        if name is None:
            raise MigrationRefused(
                "ferro.migrations: there is no default connection; call "
                "`await ferro.connect(url)` first, or pass using=<connection name>"
            )
        return name

    def _check(self, name: str) -> None:
        """Refuse connection ``name`` unless it is open and of a dialect the
        database targets."""
        try:
            connection_dialect(name, self.database)
        except SettingsError as err:
            raise MigrationRefused(str(err)) from None
