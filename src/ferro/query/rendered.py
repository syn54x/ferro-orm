"""Rendered statements: what a query terminal would send, without sending it.

A terminal (`all`, `first`, `count`, `exists`, `update`, `delete`) is an
`async def` method wrapped by [`terminal`][ferro.query.rendered.terminal].
Calling it runs the method; its `.sql` attribute runs the same method in
render mode, where the one engine call that would send the statement is
swapped for its rendering twin. Both twins build the statement through the
same engine function, so `.sql()` returns exactly what the call sends.
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    Any,
    Concatenate,
    Generic,
    Literal,
    ParamSpec,
    TypeVar,
    overload,
)

from .._sql_log import REDACTED

if TYPE_CHECKING:
    from typing import Self

__all__ = ["REDACTED", "RenderedSql", "Terminal", "TerminalMethod", "send", "terminal"]

S = TypeVar("S")
P = ParamSpec("P")
R = TypeVar("R")
R_co = TypeVar("R_co", covariant=True)

_RenderedParts = tuple[str, tuple[object, ...], tuple[bool, ...], str]
"""What an engine rendering entry returns: the SQL, its parameters (`None`
where masked), the mask (`True` where the parameter is a secret) and the
dialect."""


@dataclass(frozen=True, slots=True)
class RenderedSql:
    """One statement a query terminal would send, as the statement log records it.

    `str()` of it is the SQL alone, so `print(rendered)` shows the statement.

    Examples:
        ```python {test=run prelude=reference}
        rendered = await Author.where(lambda author: author.id == 1).all.sql()
        print(rendered.dialect, rendered.params)
        ```
    """

    sql: str
    """The SQL text, with the dialect's placeholders (`$1` or `?`)."""
    params: tuple[object, ...]
    """The bound values in placeholder order; a secret value is
    [`REDACTED`][ferro.REDACTED]."""
    dialect: Literal["postgres", "sqlite"]
    """The dialect of the database the call would run on."""

    def __str__(self) -> str:
        """Return the SQL alone."""
        return self.sql


_DIALECTS: dict[str, Literal["postgres", "sqlite"]] = {
    "postgres": "postgres",
    "sqlite": "sqlite",
}
"""The dialect names the engine renders for, as `RenderedSql.dialect` types them."""

_rendering: ContextVar[bool] = ContextVar("ferro_rendering", default=False)
"""Whether the running terminal is rendering (`.sql()`) rather than sending."""


class _Rendered(Exception):
    """Carries a terminal's rendered statement out of its method body.

    Raised where the method would have sent its statement, so nothing after
    the send (decoding, the identity map, the result's post-processing) runs
    when rendering.
    """

    def __init__(self, rendered: RenderedSql) -> None:
        super().__init__(rendered.sql)
        self.rendered = rendered


def send(
    execute: Callable[P, Awaitable[R]],
    render: Callable[P, Awaitable[_RenderedParts]],
) -> Callable[P, Awaitable[R]]:
    """Pair an engine entry that sends a statement with its rendering twin.

    A terminal's method sends its statement through this pair. Called
    normally, it runs `execute`. Called from `.sql()`, it runs `render` with
    the same arguments and hands the rendered statement back to `.sql()`.

    Args:
        execute: The engine entry that builds and sends the statement.
        render: The engine entry that builds the same statement and returns
            it without sending it.

    Returns:
        A callable with `execute`'s signature.
    """

    @functools.wraps(execute)
    async def call(*args: P.args, **kwargs: P.kwargs) -> R:
        if not _rendering.get():
            return await execute(*args, **kwargs)
        sql, params, mask, dialect = await render(*args, **kwargs)
        raise _Rendered(
            RenderedSql(
                sql=sql,
                params=tuple(
                    REDACTED if masked else value
                    for value, masked in zip(params, mask, strict=True)
                ),
                dialect=_DIALECTS[dialect],
            )
        )

    return call


class Terminal(Generic[P, R_co]):
    """A query terminal bound to its query: awaiting a call runs it, `.sql()` renders it."""

    __slots__ = ("_method", "_query")

    def __init__(self, method: Callable[..., Awaitable[R_co]], query: object) -> None:
        self._method = method
        self._query = query

    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> Awaitable[R_co]:
        """Run the terminal: send its statement and return its result."""
        return self._method(self._query, *args, **kwargs)

    async def sql(self, *args: P.args, **kwargs: P.kwargs) -> RenderedSql:
        """Render the statement this terminal would send, without sending it.

        Takes exactly the arguments the terminal takes and routes like the
        call: the enclosing session, `using=` and `session=` pick the
        database. On Postgres it may read the table's column types once, the
        way the call would; it never sends the statement itself, opens no
        transaction and changes no row.

        Args:
            *args: The terminal's positional arguments.
            **kwargs: The terminal's keyword arguments.

        Returns:
            The SQL, parameters and dialect the call would send, as the
            statement log would record them, with each secret value shown
            as [`REDACTED`][ferro.REDACTED].

        Raises:
            RuntimeError: No database route: the call is outside a session
                and no connection was named.
            TypeError: The arguments are ones the terminal rejects.

        Examples:
            ```python {test=run prelude=reference}
            rendered = await Author.where(lambda author: author.id == 1).all.sql()
            print(rendered)
            ```
        """
        token = _rendering.set(True)
        try:
            await self._method(self._query, *args, **kwargs)
        except _Rendered as rendered:
            return rendered.rendered
        finally:
            _rendering.reset(token)
        raise RuntimeError(
            f"{getattr(self._method, '__qualname__', self._method)!r} returned "
            "without sending a statement, so there is nothing to render"
        )

    def __repr__(self) -> str:
        return f"<terminal {getattr(self._method, '__qualname__', self._method)}>"


class TerminalMethod(Generic[S, P, R_co]):
    """A query terminal on its class: reading it from a query binds a [`Terminal`][ferro.query.rendered.Terminal]."""

    def __init__(self, method: Callable[Concatenate[S, P], Awaitable[R_co]]) -> None:
        self._method = method
        functools.update_wrapper(self, method)

    @overload
    def __get__(self, query: None, owner: type[Any]) -> Self: ...

    @overload
    def __get__(self, query: S, owner: type[Any]) -> Terminal[P, R_co]: ...

    def __get__(self, query: S | None, owner: type[Any]) -> Self | Terminal[P, R_co]:
        if query is None:
            return self
        return Terminal(self._method, query)

    def __call__(self, query: S, *args: P.args, **kwargs: P.kwargs) -> Awaitable[R_co]:
        """Run the terminal on `query`, read from the class."""
        return self._method(query, *args, **kwargs)


def terminal(
    method: Callable[Concatenate[S, P], Awaitable[R]],
) -> TerminalMethod[S, P, R]:
    """Make a query method a terminal: callable to run, `.sql()` to render.

    The method sends its statement through [`send`][ferro.query.rendered.send];
    `.sql()` runs the same method with that send swapped for its rendering
    twin, so the call and the rendering share every argument check, the
    routing and the statement build.

    Args:
        method: The `async def` query method, or a method returning another
            terminal's awaitable.

    Returns:
        The terminal, read from a query as a bound
        [`Terminal`][ferro.query.rendered.Terminal].
    """
    return TerminalMethod(method)
