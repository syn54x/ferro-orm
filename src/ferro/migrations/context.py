"""What a data step is handed: :class:`StepContext` and :class:`HistoricalModels`.

```python
@atomic
async def up(ctx):
    for author in await ctx.models.Author.where(lambda author: author.slug == None).all():
        author.slug = author.name.lower().replace(" ", "-")
        await author.save()
    links = await ctx.models.table("author_tags").all()      # a join table, by table name
    await ctx.execute("UPDATE author SET bio = '' WHERE bio IS NULL")
    ctx.log.info("backfilled %d authors", ...)
```

The context is closed (ADR-0035): the historical models, raw SQL on the
step's own transaction, the target dialect and a logger. It has no
``transaction()``, no database name, migration number or direction, and no
path to the models in the codebase today. A nested ``ferro.transaction()``
inside a step is a savepoint, as in application code.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..models import Model
    from ..raw import Transaction
    from ..registry import FerroModelClass, RegistrySnapshot

__all__ = ["HistoricalModels", "StepContext"]


class HistoricalModels:
    """The historical model of every table in a migration's snapshot union.

    ``models.Author`` is a model by class name; ``models.table("author_tags")``
    reaches any table by table name, a many-to-many join table included (it
    never had a class of its own). A name the union lacks raises and lists
    what it holds. Built by :func:`ferro.migrations.historical.build`.
    """

    __slots__ = ("_by_name", "_by_table", "_registry_state", "_rev")

    def __init__(
        self,
        rev: str,
        classes: dict[str, type[Model]],
        registry_state: RegistrySnapshot,
    ) -> None:
        """``classes`` maps each table name to its historical class;
        ``registry_state`` is the registry those classes registered into."""
        self._rev = rev
        self._by_table = dict(sorted(classes.items()))
        self._by_name: dict[str, type[Model]] = {}
        for cls in self._by_table.values():
            # A class name two tables share is reached by table name only.
            if cls.__name__ in self._by_name:
                self._by_name[cls.__name__] = _AMBIGUOUS
            else:
                self._by_name[cls.__name__] = cls
        self._registry_state = registry_state

    @property
    def rev(self) -> str:
        """The migration these models belong to (``0011_backfill_slugs``)."""
        return self._rev

    @property
    def registry_state(self) -> RegistrySnapshot:
        """The registry holding exactly these classes (what
        :meth:`ferro.registry.Registry.swap` installs)."""
        return self._registry_state

    def names(self) -> list[str]:
        """The class names, sorted (a join table's class is named after it)."""
        return sorted(self._by_name)

    def tables(self) -> list[str]:
        """The table names, sorted."""
        return list(self._by_table)

    def table(self, name: str) -> type[Model]:
        """The historical model of table ``name``."""
        try:
            return self._by_table[name]
        except KeyError:
            raise LookupError(
                f"the {self._rev} union has no table {name!r}; it holds: "
                f"{', '.join(self._by_table)}"
            ) from None

    def unreachable(self, cls: FerroModelClass) -> str:
        """Why today's ``cls`` cannot be queried while these models are
        installed, naming the historical model to use instead."""
        name = cls.__name__
        return (
            f"{name} is the model in the codebase today, which a data step cannot "
            f"reach; use ctx.models.{name} (the table as migration {self._rev} sees it)"
        )

    def __getattr__(self, name: str) -> type[Model]:
        if name.startswith("__"):
            raise AttributeError(name)
        cls = self._by_name.get(name)
        if cls is _AMBIGUOUS:
            tables = [t for t, c in self._by_table.items() if c.__name__ == name]
            raise AttributeError(
                f"two tables of the {self._rev} union have a model named {name} "
                f"({', '.join(tables)}); reach each with ctx.models.table(...)"
            )
        if cls is None:
            raise AttributeError(
                f"the {self._rev} union has no model {name!r}; it holds: "
                f"{', '.join(self.names())} (any table is reachable with "
                f"ctx.models.table(...))"
            )
        return cls

    def __repr__(self) -> str:
        return f"<HistoricalModels {self._rev}: {', '.join(self.names())}>"


_AMBIGUOUS: Any = object()


class StepContext:
    """``ctx``: everything a data step can reach (ADR-0035).

    ``models`` are the historical models; ``execute`` / ``fetch_all`` /
    ``fetch_one`` are ferro's raw API on the step's own transaction (no
    ``using``, ``session`` or ``autocommit``); ``dialect`` is the target
    dialect (``"postgres"`` or ``"sqlite"``); ``log`` is a logger named for
    the step.
    """

    __slots__ = ("_dialect", "_log", "_historical", "_tx")

    def __init__(
        self,
        models: HistoricalModels,
        tx: Transaction,
        dialect: str,
        log: logging.Logger,
    ) -> None:
        self._historical = models
        self._tx = tx
        self._dialect = dialect
        self._log = log

    @property
    def models(self) -> HistoricalModels:
        """The historical models: ``ctx.models.Author``, ``ctx.models.table(...)``."""
        return self._historical

    @property
    def dialect(self) -> str:
        """The target dialect: ``"postgres"`` or ``"sqlite"``."""
        return self._dialect

    @property
    def log(self) -> logging.Logger:
        """A logger named for the step (``ferro.migrations.0011.01_backfill_author``)."""
        return self._log

    async def execute(self, sql: str, *args: Any) -> int:
        """Run a raw statement on the step's transaction; rows affected."""
        return await self._tx.execute(sql, *args)

    async def fetch_all(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        """Run a raw query on the step's transaction; every row as a dict."""
        return await self._tx.fetch_all(sql, *args)

    async def fetch_one(self, sql: str, *args: Any) -> dict[str, Any] | None:
        """Run a raw query on the step's transaction; the first row, or ``None``."""
        return await self._tx.fetch_one(sql, *args)

    def __repr__(self) -> str:
        return f"<StepContext {self._log.name} ({self._dialect})>"
