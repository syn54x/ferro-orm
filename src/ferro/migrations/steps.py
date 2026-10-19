"""Data step declarations, `todo()`, and the loader that reads a step file.

```python
# migrations/0011_backfill_slugs/01_backfill_author.py
from ferro.migrations import atomic, nothing_to_reverse, todo

@atomic
async def up(ctx):
    for author in await ctx.models.Author.where(lambda author: author.slug == None).all():
        author.slug = todo("the slug for an existing author")
        await author.save()

@nothing_to_reverse("slugs are derived; nothing to put back")
def down(ctx): ...
```

Every `up` and `down` carries exactly one declaration: `atomic`, `chunked`,
or, on a `down` only, `irreversible` or `nothing_to_reverse`. `load_step`
refuses an undecorated or doubly decorated function naming the file and
function, and reads every `todo` call from the file's syntax tree, so `up`
refuses the migration with file, line and message before running anything:

```text
0011_backfill_slugs/01_backfill_author.py:7: not written yet: write this step
```
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import inspect
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .errors import MigrationRefused

if TYPE_CHECKING:
    from ..query import Query
    from .context import HistoricalModels

__all__ = [
    "Atomic",
    "Chunked",
    "Declared",
    "Irreversible",
    "LoadedStep",
    "NotWrittenError",
    "NothingToReverse",
    "StepRefused",
    "atomic",
    "chunked",
    "chunked_query",
    "declared_up_kind",
    "irreversible",
    "load_step",
    "nothing_to_reverse",
    "scan_todos",
    "todo",
    "unwritten",
]

_DECLARATIONS = "__ferro_step_declarations__"
_PUBLIC_MODULE = "ferro.migrations"


class StepRefused(MigrationRefused):
    """The refusal for a data step file that cannot run as written.

    An undeclared or doubly declared function, a file that does not load, or
    one edited since it was planned. The message names the file and the fix.
    """


class NotWrittenError(RuntimeError):
    """The error a [`todo`][ferro.migrations.todo] raises when a run reaches it."""


# -- the shapes -------------------------------------------------------------------------


@dataclass(frozen=True)
class Atomic:
    """The atomic shape: one transaction, the step's record committed inside it."""

    kind = "atomic"
    """The shape's name, as a step record and a refusal spell it."""


@dataclass(frozen=True)
class Chunked:
    """The chunked shape: the runner pages `query` by keyset, `batch_size` rows per transaction.

    `chunked_query` builds and checks the query.
    """

    query: Callable[[HistoricalModels], Query[Any]]
    """The declared query, a function of the step's historical models."""
    batch_size: int
    """The rows each transaction takes."""
    kind = "chunked"
    """The shape's name, as a step record and a refusal spell it."""


@dataclass(frozen=True)
class Irreversible:
    """The irreversible shape: a run that would revert the step refuses before reverting anything."""

    reason: str
    """Why the step cannot be reversed."""
    kind = "irreversible"
    """The shape's name, as a step record and a refusal spell it."""


@dataclass(frozen=True)
class NothingToReverse:
    """The nothing-to-reverse shape: reverting the step removes its record and runs nothing."""

    reason: str
    """Why there is nothing to reverse."""
    kind = "nothing_to_reverse"
    """The shape's name, as a step record and a refusal spell it."""


Shape = Atomic | Chunked | Irreversible | NothingToReverse


@dataclass(frozen=True)
class Declared:
    """One step function and its declared shape."""

    fn: Callable[..., Any]
    """The step function."""
    shape: Shape
    """The shape its one declaration gave it."""


@dataclass(frozen=True)
class LoadedStep:
    """A loaded data step file."""

    up: Declared
    """The file's `up` and its shape."""
    down: Declared
    """The file's `down` and its shape."""
    todos: list[tuple[int, str]]
    """Every `todo("…")` in the file as `(line, message)`, in file order."""


# -- the declarations --------------------------------------------------------------------


def _declare[F: Callable[..., Any]](fn: F, shape: Shape) -> F:
    if not callable(fn):
        raise TypeError(f"@{shape.kind} decorates a step function, not {fn!r}")
    declared: list[Shape] = list(getattr(fn, _DECLARATIONS, ()))
    declared.append(shape)
    setattr(fn, _DECLARATIONS, tuple(declared))
    return fn


def atomic[F: Callable[..., Awaitable[Any]]](fn: F) -> F:
    """Declare a step function atomic: one transaction, with the step's record committed inside it.

    The function is `async def up(ctx)` (or `down`); `ctx` is the step's
    [context](../guide/schema/data-steps.md).

    Args:
        fn: The step function.

    Returns:
        The function, unchanged apart from its declaration.

    Raises:
        TypeError: `fn` is not callable.

    Examples:
        ```python {test=compile reason="a step file in a migrations directory"}
        from ferro.migrations import atomic


        @atomic
        async def up(ctx):
            for author in await ctx.models.Author.where(lambda author: author.slug == None).all():
                author.slug = author.name.lower().replace(" ", "-")
                await author.save()
        ```
    """
    return _declare(fn, Atomic())


def chunked[F: Callable[..., Awaitable[Any]]](
    query: Callable[[HistoricalModels], Query[Any]], *, batch_size: int
) -> Callable[[F], F]:
    """Declare a step function chunked: the runner pages a query in batches, one transaction each.

    The runner calls `async def up(ctx, batch)` once per batch and commits
    the batch's cursor with it, so a failed or interrupted run resumes after
    the last committed batch. See
    [Data steps and backfills](../guide/schema/data-steps.md).

    Args:
        query: A function of `ctx.models` that returns the query to page. It
            orders by keys that include the model's primary key, and selects
            only the rows that still need the step, so a resumed run re-pages
            from its cursor.
        batch_size: The rows each transaction takes; a positive `int`.

    Returns:
        A decorator that declares the step function chunked.

    Raises:
        TypeError: `query` is not callable.
        ValueError: `batch_size` is not a positive `int`.
        StepRefused: At run time, before anything runs: the query does not
            build, pages a table without a single primary key, is not
            ordered by keys that include the primary key, orders by a
            related model's column, or sets its own `limit`, `offset`,
            `after` or `before`.

    Examples:
        ```python {test=compile reason="a step file in a migrations directory"}
        from ferro.migrations import chunked


        @chunked(
            lambda models: models.Author.where(lambda author: author.slug == None)
            .order_by(lambda author: author.id),
            batch_size=1000,
        )
        async def up(ctx, batch):
            for author in batch:
                author.slug = author.name.lower().replace(" ", "-")
                await author.save()
        ```
    """
    if not callable(query):
        raise TypeError("@chunked takes the query as a function of ctx.models")
    if (
        isinstance(batch_size, bool)
        or not isinstance(batch_size, int)
        or batch_size < 1
    ):
        raise ValueError(
            f"@chunked batch_size must be a positive int, not {batch_size!r}"
        )
    shape = Chunked(query, batch_size)

    def decorate(fn: F) -> F:
        return _declare(fn, shape)

    return decorate


def irreversible[F: Callable[..., Any]](reason: str) -> Callable[[F], F]:
    """Declare a `down` irreversible, saying why.

    A run that would revert the step refuses before reverting anything, and
    prints the reason.

    Args:
        reason: Why the step cannot be reversed; a non-empty string.

    Returns:
        A decorator that declares the `down` irreversible.

    Raises:
        TypeError: `reason` is not a non-empty string.

    Examples:
        ```python {test=compile reason="a step file in a migrations directory"}
        from ferro.migrations import irreversible


        @irreversible("the old nicknames were dropped with the column")
        def down(ctx): ...
        ```
    """
    shape = Irreversible(_reason(reason, "irreversible"))

    def decorate(fn: F) -> F:
        return _declare(fn, shape)

    return decorate


def nothing_to_reverse[F: Callable[..., Any]](reason: str) -> Callable[[F], F]:
    """Declare that a `down` has nothing to reverse, saying why.

    Reverting the step removes its record and runs nothing.

    Args:
        reason: Why there is nothing to reverse; a non-empty string.

    Returns:
        A decorator that declares the `down` as having nothing to reverse.

    Raises:
        TypeError: `reason` is not a non-empty string.

    Examples:
        ```python {test=compile reason="a step file in a migrations directory"}
        from ferro.migrations import nothing_to_reverse


        @nothing_to_reverse("slugs are derived; nothing to put back")
        def down(ctx): ...
        ```
    """
    shape = NothingToReverse(_reason(reason, "nothing_to_reverse"))

    def decorate(fn: F) -> F:
        return _declare(fn, shape)

    return decorate


def chunked_query(shape: Chunked, models: HistoricalModels, path: Path) -> Query[Any]:
    """Build a `@chunked` step's query and check that the runner can page it by keyset.

    The run checks this for every chunked step before anything runs.

    Args:
        shape: The step's chunked declaration.
        models: The step's historical models, passed to the query function.
        path: The step file, named in a refusal.

    Returns:
        The query, ready to page.

    Raises:
        StepRefused: The declaration does not return a query; the query
            pages a table without a single primary key (a many-to-many join
            table); it has no `order_by`, orders by a related model's
            column, or its order keys leave out the primary key; or it sets
            its own `limit`, `offset`, `after` or `before`. The message
            names the file and the fix.
    """
    from ..query import Query

    shown = _shown(path)
    try:
        query = shape.query(models)
    except Exception as err:
        raise StepRefused(
            f"ferro migrate: {shown}: @chunked's query does not build: "
            f"{type(err).__name__}: {err}"
        ) from err
    if not isinstance(query, Query):
        raise StepRefused(
            f"ferro migrate: {shown}: @chunked takes a function of ctx.models that "
            f"returns a query, and this one returned {type(query).__name__}"
        )
    model = query.model_cls
    table = getattr(model, "__ferro_table__", None) or model.__name__.lower()
    pk = getattr(model, "__ferro_pk__", None)
    if pk is None:
        raise StepRefused(
            f"ferro migrate: {shown}: @chunked pages {table}, a table without a single "
            f"primary key (a many-to-many join table), and keyset paging over a "
            f"composite key is not built yet (#492). Instead page the parent model "
            f"and change each parent's join rows inside the batch: @chunked over "
            f"ctx.models.<Parent> ordered by its primary key, then "
            f'ctx.models.table("{table}").where(...) in the step'
        )
    var = model.__name__.lower()
    add_pk = f".order_by(lambda {var}: {var}.{pk})"
    related = [
        ".".join((*entry.path, entry.column))
        for entry in query.order_by_clause
        if entry.path
    ]
    if related:
        raise StepRefused(
            f"ferro migrate: {shown}: @chunked orders {table} by {', '.join(related)}, "
            f"a column of a related model; the cursor holds the last row's own order "
            f"keys, so order by a column of the model, including its primary key: "
            f"{add_pk}"
        )
    keys = [entry.column for entry in query.order_by_clause]
    if not query.order_by_clause:
        raise StepRefused(
            f"ferro migrate: {shown}: @chunked needs an ordered query: the runner "
            f"pages it by its order keys and commits the last row's as the cursor. "
            f"Add {add_pk}"
        )
    if pk not in keys:
        raise StepRefused(
            f"ferro migrate: {shown}: @chunked orders {table} by {', '.join(keys)} "
            f"without its primary key {pk}, so two rows could share a cursor. Add "
            f"{add_pk} as the last order key"
        )
    bounds = [
        name
        for name, value in (
            ("limit", query._limit),
            ("offset", query._offset),
            ("after", query._after),
            ("before", query._before),
        )
        if value is not None
    ]
    if bounds:
        raise StepRefused(
            f"ferro migrate: {shown}: @chunked's query sets {', '.join(bounds)}; the "
            f"runner pages it itself (batch_size rows after the cursor), so remove it"
        )
    return query


def _reason(reason: Any, name: str) -> str:
    if not isinstance(reason, str) or not reason.strip():
        raise TypeError(f'@{name}("...") takes the reason as a non-empty string')
    return reason.strip()


def todo(message: str) -> Any:
    """Mark a value in a data step that only a person can supply.

    The loader reads every call from the file's syntax tree, and `up`
    refuses the migration before running anything, naming the file, the
    line and the message.

    Args:
        message: What is missing, as a string literal: the refusal prints it.

    Raises:
        NotWrittenError: Always, when a run reaches the call anyway.

    Examples:
        ```python {test=compile reason="a step file in a migrations directory"}
        from ferro.migrations import atomic, todo


        @atomic
        async def up(ctx):
            for author in await ctx.models.Author.where(lambda author: author.slug == None).all():
                author.slug = todo("the slug for an existing author")
                await author.save()
        ```
    """
    raise NotWrittenError(f"not written yet: {message}")


# -- reading the syntax tree ---------------------------------------------------------------


def _shown(path: Path) -> str:
    """``0011_backfill_slugs/01_backfill_author.py``."""
    return f"{path.parent.name}/{path.name}"


def _todo_names(tree: ast.Module) -> tuple[set[str], set[str]]:
    """The names ``todo`` is called by: bare names imported from
    ``ferro.migrations``, and module aliases whose ``.todo`` it is."""
    bare: set[str] = set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == _PUBLIC_MODULE:
            for alias in node.names:
                if alias.name == "todo":
                    bare.add(alias.asname or "todo")
        elif isinstance(node, ast.ImportFrom) and node.module == "ferro":
            for alias in node.names:
                if alias.name == "migrations":
                    modules.add(alias.asname or "migrations")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == _PUBLIC_MODULE and alias.asname:
                    modules.add(alias.asname)
                elif alias.name in (_PUBLIC_MODULE, "ferro"):
                    modules.add(_PUBLIC_MODULE)
    return bare, modules


def _dotted(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base is not None else None
    return None


def scan_todos(source: str | bytes, path: Path) -> list[tuple[int, str]]:
    """Find every `todo("…")` call in a step file's source.

    Args:
        source: The file's source.
        path: The file, named in a refusal.

    Returns:
        Each call as `(line, message)`, in line order.

    Raises:
        StepRefused: The file does not parse, or a `todo` carries no literal
            message.
    """
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as err:
        raise StepRefused(
            f"ferro migrate: {_shown(path)}:{err.lineno}: does not parse: {err.msg}"
        ) from None
    bare, modules = _todo_names(tree)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            called = func.id in bare
        elif isinstance(func, ast.Attribute) and func.attr == "todo":
            called = _dotted(func.value) in modules
        else:
            called = False
        if not called:
            continue
        message = node.args[0] if len(node.args) == 1 and not node.keywords else None
        if not (isinstance(message, ast.Constant) and isinstance(message.value, str)):
            raise StepRefused(
                f"ferro migrate: {_shown(path)}:{node.lineno}: todo() takes one literal "
                f'message saying what is missing: todo("the slug for an existing author")'
            )
        found.append((node.lineno, message.value))
    return sorted(found)


def unwritten(path: Path, todos: list[tuple[int, str]]) -> list[str]:
    """Render the refusal lines for a step's `todo` calls, one per call.

    Args:
        path: The step file.
        todos: Its `todo` calls as `(line, message)`.

    Returns:
        One `<migration>/<file>:<line>: not written yet: <message>` line per
            call.
    """
    return [
        f"{_shown(path)}:{line}: not written yet: {message}" for line, message in todos
    ]


_DECORATOR = re.compile(r"^(atomic|chunked|irreversible|nothing_to_reverse)$")


def declared_up_kind(path: Path) -> str:
    """Read the record kind a step file's `up` declares, without running the file.

    This is what `baseline` records for a step it never runs.

    Args:
        path: The step file.

    Returns:
        `"atomic"` or `"chunked"`.

    Raises:
        StepRefused: The file does not parse, has no `up`, or its `up` does
            not carry exactly one of `@atomic` / `@chunked`.
    """
    source = path.read_bytes()
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as err:
        raise StepRefused(
            f"ferro migrate: {_shown(path)}:{err.lineno}: does not parse: {err.msg}"
        ) from None
    for node in tree.body:
        if (
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name == "up"
        ):
            names = []
            for decorator in node.decorator_list:
                target = (
                    decorator.func if isinstance(decorator, ast.Call) else decorator
                )
                dotted = _dotted(target) or ""
                last = dotted.rsplit(".", 1)[-1]
                if _DECORATOR.match(last):
                    names.append(last)
            if len(names) == 1 and names[0] in ("atomic", "chunked"):
                return names[0]
            raise StepRefused(_declaration_problem(path, "up", names))
    raise StepRefused(f"ferro migrate: {_shown(path)} defines no up function")


def _declaration_problem(path: Path, function: str, kinds: list[str]) -> str:
    shown = _shown(path)
    if not kinds:
        allowed = (
            "@atomic or @chunked(...)"
            if function == "up"
            else '@atomic, @chunked(...), @irreversible("...") or @nothing_to_reverse("...")'
        )
        return (
            f"ferro migrate: {shown}: {function}() has no declaration; decorate it "
            f"with {allowed}"
        )
    if len(kinds) > 1:
        listed = " and ".join(f"@{kind}" for kind in kinds)
        return (
            f"ferro migrate: {shown}: {function}() carries {listed}; a step "
            f"function declares exactly one shape, so keep one"
        )
    return (
        f"ferro migrate: {shown}: {function}() is declared @{kinds[0]}, which only a "
        f"down can be; an up is @atomic or @chunked(...)"
    )


# -- loading -------------------------------------------------------------------------------


def load_step(path: Path, expected_checksum: str | None) -> LoadedStep:
    """Load a data step file, checked against the checksum it was planned with.

    The file is compiled from the same bytes that were checked, under the
    module name `ferro.migrations.steps_loaded.<migration>_<step>` (never
    put on `sys.path`), so the code that runs is the code that was checked.
    Its `todo` calls are read from the syntax tree; the caller refuses them.

    Args:
        path: The step file.
        expected_checksum: The file's SHA-384 as the run planner recorded
            it, in lowercase hex; `None` skips the check.

    Returns:
        The file's `up` and `down` with their shapes, and its `todo` calls.

    Raises:
        StepRefused: The file changed since it was planned, does not load,
            lacks an `up` or `down`, or a function is undeclared, doubly
            declared, not `async def` where it must run, or (`up`) declared
            irreversible or nothing-to-reverse.
    """
    shown = _shown(path)
    try:
        source = path.read_bytes()
    except OSError as err:
        raise StepRefused(f"ferro migrate: cannot read {shown} ({err})") from None
    if expected_checksum is not None:
        actual = hashlib.sha384(source).hexdigest()
        if actual != expected_checksum:
            raise StepRefused(
                f"ferro migrate: {shown} changed while this run was in progress; it was "
                f"planned with sha384:{expected_checksum}. Run `ferro migrate up` again."
            )
    todos = scan_todos(source, path)
    module_name = (
        f"ferro.migrations.steps_loaded.{path.parent.name}_{path.stem}".replace(
            "-", "_"
        )
    )
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None:  # pragma: no cover - a .py path always yields a spec
        raise StepRefused(f"ferro migrate: cannot load {shown}")
    module = importlib.util.module_from_spec(spec)
    try:
        exec(compile(source, str(path), "exec"), module.__dict__)
    except Exception as err:
        raise StepRefused(
            f"ferro migrate: {shown} does not load: {type(err).__name__}: {err}"
        ) from err
    return LoadedStep(
        up=_declared(path, module, "up"),
        down=_declared(path, module, "down"),
        todos=todos,
    )


def _declared(path: Path, module: Any, function: str) -> Declared:
    fn = getattr(module, function, None)
    if not callable(fn):
        raise StepRefused(
            f"ferro migrate: {_shown(path)} defines no {function}() function; a data "
            f"step defines both up() and down()"
        )
    shapes: tuple[Shape, ...] = getattr(fn, _DECLARATIONS, ())
    kinds = [shape.kind for shape in shapes]
    if len(shapes) != 1 or (
        function == "up" and not isinstance(shapes[0], Atomic | Chunked)
    ):
        raise StepRefused(_declaration_problem(path, function, kinds))
    shape = shapes[0]
    if isinstance(shape, Atomic | Chunked) and not inspect.iscoroutinefunction(fn):
        raise StepRefused(
            f"ferro migrate: {_shown(path)}: {function}() is declared @{shape.kind}, "
            f"so it runs: write it as `async def {function}(...)`"
        )
    return Declared(fn=fn, shape=shape)
