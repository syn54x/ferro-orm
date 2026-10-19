"""What a data step is handed: `StepContext` and `HistoricalModels`.

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

The context is closed: the historical models, raw SQL on the step's own
transaction, the target dialect and a logger. It has no `transaction()`, no
database name, migration number or direction, and no path to the models in
the codebase today. A nested `ferro.transaction()` inside a step is a
savepoint, as in application code.
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

    `models.Author` is a model by class name; `models.table("author_tags")`
    reaches any table by table name, a many-to-many join table included (it
    never had a class of its own). A name the union lacks raises and lists
    what it holds.
    """

    __slots__ = ("_by_name", "_by_table", "_registry_state", "_rev")

    def __init__(
        self,
        rev: str,
        classes: dict[str, type[Model]],
        registry_state: RegistrySnapshot,
    ) -> None:
        """Hold a migration's historical classes.

        Args:
            rev: The migration these models belong to.
            classes: Each table name mapped to its historical class.
            registry_state: The registry those classes registered into.
        """
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
        """The migration these models belong to (`0011_backfill_slugs`)."""
        return self._rev

    @property
    def registry_state(self) -> RegistrySnapshot:
        """The registry holding exactly these classes, which a step run installs."""
        return self._registry_state

    def names(self) -> list[str]:
        """List the class names, sorted (a join table's class is named after it).

        Returns:
            The class names.
        """
        return sorted(self._by_name)

    def tables(self) -> list[str]:
        """List the table names, sorted.

        Returns:
            The table names.
        """
        return list(self._by_table)

    def table(self, name: str) -> type[Model]:
        """Return the historical model of a table, by table name.

        Args:
            name: The table name, such as `"author_tags"`.

        Returns:
            The table's historical model class.

        Raises:
            LookupError: The union has no such table; the message lists the
                tables it holds.
        """
        try:
            return self._by_table[name]
        except KeyError:
            raise LookupError(
                f"the {self._rev} union has no table {name!r}; it holds: "
                f"{', '.join(self._by_table)}"
            ) from None

    def unreachable(self, cls: FerroModelClass) -> str:
        """Explain why a model from today's codebase cannot be queried in a data step.

        Args:
            cls: The model class from the codebase today.

        Returns:
            The sentence naming the historical model to use instead.
        """
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
    """The `ctx` a data step receives: everything the step can reach.

    `execute`, `fetch_all` and `fetch_one` are Ferro's
    [raw SQL](../guide/raw-sql.md) calls on the step's own transaction,
    without `using`, `session` or `autocommit`.
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
        """The historical models: `ctx.models.Author`, `ctx.models.table(...)`."""
        return self._historical

    @property
    def dialect(self) -> str:
        """The target dialect: `"postgres"` or `"sqlite"`."""
        return self._dialect

    @property
    def log(self) -> logging.Logger:
        """A logger named for the step (`ferro.migrations.0011.01_backfill_author`)."""
        return self._log

    async def execute(self, sql: str, *args: Any) -> int:
        """Run a raw statement on the step's transaction.

        Args:
            sql: The statement.
            *args: Values bound to the statement's placeholders, in order.

        Returns:
            The number of rows affected.
        """
        return await self._tx.execute(sql, *args)

    async def fetch_all(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        """Run a raw query on the step's transaction and return every row.

        Args:
            sql: The query.
            *args: Values bound to the query's placeholders, in order.

        Returns:
            Every row, as a dict keyed by column name.
        """
        return await self._tx.fetch_all(sql, *args)

    async def fetch_one(self, sql: str, *args: Any) -> dict[str, Any] | None:
        """Run a raw query on the step's transaction and return its first row.

        Args:
            sql: The query.
            *args: Values bound to the query's placeholders, in order.

        Returns:
            The first row as a dict keyed by column name, or `None` when there
                is none.
        """
        return await self._tx.fetch_one(sql, *args)

    def __repr__(self) -> str:
        return f"<StepContext {self._log.name} ({self._dialect})>"
