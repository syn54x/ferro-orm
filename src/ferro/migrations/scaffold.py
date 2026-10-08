"""The generated data step's text (ADR-0035, ADR-0037, ADR-0040).

The generator decides every data step of a migration and hands each one over
as a record; :func:`render` turns that record into the step's file. There are
three shapes, and the record says which.

A **backfill**. ``Author`` gains a required ``slug``; ``ferro migrate new
author_slug`` writes it between the expand and the contract::

    # migrations/0012_author_slug/02_backfill_author.py
    from ferro.migrations import chunked, nothing_to_reverse, todo


    @chunked(
        lambda models: models.Author.where(lambda author: author.slug == None)
        .order_by(lambda author: author.id),
        batch_size=1000,
    )
    async def up(ctx, batch):
        for author in batch:
            author.slug = todo("the slug for an existing author")
            await author.save()


    @nothing_to_reverse("01_expand.down.sql drops the column")
    def down(ctx): ...

The query always selects the rows that still need a value, so a re-run (the
contract's recipe for rows written behind the cursor) touches only what is
left. A ``default_factory`` from the standard library (``uuid.uuid4``) is
pre-filled as its call; any other is a ``todo`` naming it. A model without a
single primary key has no keyset to page by: its backfill is one ``@atomic``
``update`` per column.

A label removed from a ``StrEnum`` (D2) is a backfill over the rows still
holding it, each given another label (the historical model's enum still
declares the removed one, and the label written is looked up in it)::

    @chunked(
        lambda models: models.Order.where(lambda order: order.status == "canceled")
        .order_by(lambda order: order.id),
        batch_size=1000,
    )
    async def up(ctx, batch):
        for order in batch:
            order.status = type(order.status)(todo("the label to use instead of 'canceled'"))
            await order.save()

A **guard step**. ``new --no-backfill author.slug`` writes one in the
backfill's place: an ``@atomic`` step that fails the migration, naming the
count, when any row still needs a value, so a step number is never a gap
(ADR-0037).

A **hand-requested data step** (``ferro migrate new backfill_slugs
--data-step Author``)::

    from ferro.migrations import atomic, todo


    @atomic
    async def up(ctx):
        # ctx.models.Author is the table as this migration leaves it.
        todo("write this step")


    @atomic
    async def down(ctx):
        todo("write this step")

A project overrides any of the three with a file in ``_templates/`` beside
its migrations (``backfill.py``, ``guard.py``, ``data_step.py``), used
verbatim with these placeholders replaced, and nothing else touched:

- ``{model}``: the model's class name (``Author``), in all three;
- ``{columns}``: the columns, comma-separated (``slug, bio``), in a backfill
  or guard;
- ``{query}``: the query of the rows still needing a value
  (``models.Author.where(lambda author: author.slug == None)``), in a
  backfill or guard.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "BACKFILL_TEMPLATE",
    "DATA_STEP_TEMPLATE",
    "GUARD_TEMPLATE",
    "TEMPLATES_DIR",
    "note",
    "render",
]

TEMPLATES_DIR = "_templates"
"""The directory beside the migrations that holds project templates (the
directory reader ignores ``_``-entries)."""

BACKFILL_TEMPLATE = "backfill.py"
GUARD_TEMPLATE = "guard.py"
DATA_STEP_TEMPLATE = "data_step.py"

BATCH_SIZE = 1000

_HAND_SKELETON = """\
from ferro.migrations import atomic, todo


@atomic
async def up(ctx):
    # ctx.models.{model} is the table as this migration leaves it.
    todo("write this step")


@atomic
async def down(ctx):
    todo("write this step")
"""


def render(step: Mapping[str, Any], *, template_dir: Path | None) -> str:
    """The text of the data step ``step`` describes.

    ``step`` is one step record of ``_core._generate_migration``'s output, as
    the generator produced it: its ``data`` (a backfill, or the guard when
    ``data["guard"]``) or its ``hand_model`` (``--data-step``). The project's
    template in ``template_dir`` wins when it exists.

    Raises:
        ValueError: ``step`` is not a data step the generator scaffolds.
    """
    hand = step.get("hand_model")
    if hand is not None:
        template = _template(template_dir, DATA_STEP_TEMPLATE) or _HAND_SKELETON
        return template.replace("{model}", hand)
    data = step.get("data")
    if data is None:
        raise ValueError(
            f"step {step.get('name')!r} carries neither data nor hand_model: "
            "it is not a scaffolded data step"
        )
    demand = _Demand.of(data)
    if demand.guard:
        return _guard(demand, template_dir)
    return _backfill(demand, template_dir)


def note(step: Mapping[str, Any], *, dir_name: str, migration_name: str) -> str | None:
    """The summary line a backfill that still needs writing adds to the
    migration (``02_backfill_author.py needs writing where it says
    todo(...); …``): where to write, and the command that regenerates the
    migration with a guard instead. ``None`` for any other step: a guard, a
    hand-requested step, or a backfill pre-filled throughout."""
    data = step.get("data")
    if data is None or data["guard"]:
        return None
    demand = _Demand.of(data)
    if not demand.removed and len(demand.prefill) == len(demand.columns):
        return None
    var = _var(demand.model)
    return (
        f"{step['ordinal']:02d}_{step['name']}.py needs writing where it says todo(...); "
        f"if no {var} needs a value, delete {dir_name}/ and "
        f"run: ferro migrate new {migration_name} {demand.skip}"
    )


# -- the record, decoded ------------------------------------------------------------


@dataclass(frozen=True)
class _Demand:
    """A generated data step's record (``DataStep`` in the Rust generator),
    decoded into what its text is written from."""

    model: str
    columns: tuple[str, ...]
    """Each column once, in the record's order."""
    fills: tuple[tuple[str, str | None], ...]
    """What the step fills, in order: ``(column, None)`` for a column whose
    rows hold ``NULL``, ``(column, label)`` for each removed enum ``label``
    its rows may hold (D2)."""
    removed: bool
    """Some column's rows hold a label the migration removes."""
    driver: str
    key: str | None
    reverse: str
    guard: bool
    prefill: dict[str, str]
    """A column to the Python expression each row gets (a chunked backfill's
    standard-library ``default_factory``, called)."""
    todos: dict[str, str]
    """A column to the message of the ``todo`` it holds, where the default
    (``the <column> for an existing <model>``) would not say enough."""
    skip: str
    """The ``--no-backfill`` arguments that skip the step for a guard."""

    @classmethod
    def of(cls, data: Mapping[str, Any]) -> _Demand:
        model, table, driver = data["model"], data["table"], data["driver"]
        chunked = driver == "chunked"
        var = _var(model)
        # A column appears once per reason: a removed enum label (D2) is a
        # reason of its own, one per label.
        columns = tuple(dict.fromkeys(column["name"] for column in data["columns"]))
        removed: dict[str, list[str]] = {}
        nulls: set[str] = set()
        prefill: dict[str, str] = {}
        todos: dict[str, str] = {}
        for column in data["columns"]:
            name, reason = column["name"], column["reason"]
            if reason["kind"] == "label_removed":
                removed.setdefault(name, []).append(reason["label"])
                continue
            nulls.add(name)
            if reason["kind"] != "default_factory":
                continue
            factory = reason["factory"]
            call = _prefill_for(factory) if chunked else None
            if call is not None:
                prefill[name] = call
            elif chunked:
                todos[name] = f"call {factory} for an existing {var}"
            else:
                todos[name] = (
                    f"one {name} for every existing {var} ({factory} was its "
                    f"default_factory; {var} has no single primary key to give each "
                    "row its own)"
                )
        # A column with removed labels fills its NULL rows too only when
        # another reason says they need a value.
        fills: list[tuple[str, str | None]] = []
        for name in columns:
            labels = removed.get(name, ())
            if not labels or name in nulls:
                fills.append((name, None))
            fills.extend((name, label) for label in labels)
        return cls(
            model=model,
            columns=columns,
            fills=tuple(fills),
            removed=bool(removed),
            driver=driver,
            key=data.get("key"),
            reverse=data["reverse"],
            guard=bool(data["guard"]),
            prefill=prefill,
            todos=todos,
            skip=" ".join(f"--no-backfill {table}.{c}" for c in columns),
        )


def _prefill_for(factory: str) -> str | None:
    """The call that pre-fills a ``default_factory`` named ``factory``
    (``uuid.uuid4`` → ``uuid.uuid4()``) when its module is in the standard
    library; ``None`` for any other (a ``todo`` names it instead)."""
    root = factory.split(".", 1)[0]
    parts = factory.split(".")
    if root not in sys.stdlib_module_names or not all(
        part.isidentifier() for part in parts
    ):
        return None
    return f"{factory}()"


# -- the text ---------------------------------------------------------------------


def _var(model: str) -> str:
    return model.lower()


def _condition(var: str, fill: tuple[str, str | None]) -> str:
    """``author.slug == None`` or ``order.status == "canceled"``."""
    column, label = fill
    if label is None:
        return f"{var}.{column} == None"
    return f"{var}.{column} == {_literal(label)}"


def _missing(model: str, fills: Sequence[tuple[str, str | None]]) -> str:
    """``author.slug == None``, or ``(author.slug == None) | (author.bio == None)``."""
    var = _var(model)
    if len(fills) == 1:
        return _condition(var, fills[0])
    return " | ".join(f"({_condition(var, fill)})" for fill in fills)


def _query(model: str, fills: Sequence[tuple[str, str | None]]) -> str:
    return f"models.{model}.where(lambda {_var(model)}: {_missing(model, fills)})"


def _label_todo(label: str) -> str:
    return f"todo({_literal(f'the label to use instead of {label!r}')})"


def _template(template_dir: Path | None, name: str) -> str | None:
    if template_dir is None:
        return None
    path = template_dir / name
    if not path.is_file():
        return None
    return path.read_bytes().decode("utf-8")


def _fill(template: str, demand: _Demand) -> str:
    return (
        template.replace("{model}", demand.model)
        .replace("{columns}", ", ".join(demand.columns))
        .replace("{query}", _query(demand.model, demand.fills))
    )


def _imports(prefill: Mapping[str, str]) -> list[str]:
    roots = {value.split(".", 1)[0] for value in prefill.values()}
    return [
        f"import {root}" for root in sorted(roots) if root in sys.stdlib_module_names
    ]


def _backfill(demand: _Demand, template_dir: Path | None) -> str:
    """The backfill over ``demand``'s columns: ``@chunked`` (paged by keyset
    over the primary key) or ``@atomic``; every value not pre-filled is a
    ``todo``, and the header names the ``--no-backfill`` that skips it."""
    if demand.driver not in ("chunked", "atomic"):
        raise ValueError(f"a backfill is chunked or atomic, not {demand.driver!r}")
    if demand.driver == "chunked" and demand.key is None:
        raise ValueError(
            "a chunked backfill pages by the model's primary key: the record has no key"
        )
    template = _template(template_dir, BACKFILL_TEMPLATE)
    if template is not None:
        return _fill(template, demand)
    model, columns, fills, prefill = (
        demand.model,
        demand.columns,
        demand.fills,
        demand.prefill,
    )
    var = _var(model)

    def value(fill: tuple[str, str | None]) -> str:
        column, label = fill
        if label is not None:
            return _label_todo(label)
        return prefill.get(column) or (
            f"todo({_literal(demand.todos.get(column) or f'the {column} for an existing {var}')})"
        )

    names = ["nothing_to_reverse"]
    names.append("chunked" if demand.driver == "chunked" else "atomic")
    unwritten = any(
        label is not None or column not in prefill for column, label in fills
    )
    lines = [f"# Gives every existing {var} a value for {', '.join(columns)}."]
    if unwritten:
        names.append("todo")
        lines.append("# Write each value marked todo, then run `ferro migrate up`.")
    lines += [
        "# If no row needs a value, delete this unapplied migration and run",
        f"# `ferro migrate new <name> {demand.skip}` for a guard step instead.",
    ]
    lines += [*_imports(prefill)]
    lines += [f"from ferro.migrations import {', '.join(sorted(names))}", "", ""]
    if demand.driver == "chunked":
        lines += [
            "@chunked(",
            f"    lambda models: {_query(model, fills)}",
            f"    .order_by(lambda {var}: {var}.{demand.key}),",
            f"    batch_size={BATCH_SIZE},",
            ")",
            "async def up(ctx, batch):",
            f"    for {var} in batch:",
        ]
        for fill in fills:
            column = fill[0]
            # A removed label's row holds a member of the historical enum:
            # the label written is looked up in that same enum.
            assigned = (
                value(fill)
                if fill[1] is None
                else f"type({var}.{column})({value(fill)})"
            )
            if len(fills) == 1:
                lines.append(f"        {var}.{column} = {assigned}")
            else:
                test = (
                    f"{var}.{column} is None"
                    if fill[1] is None
                    else f"{var}.{column} is not None and "
                    f"{var}.{column}.value == {_literal(fill[1])}"
                )
                lines += [
                    f"        if {test}:",
                    f"            {var}.{column} = {assigned}",
                ]
        lines.append(f"        await {var}.save()")
    else:
        lines += ["@atomic", "async def up(ctx):"]
        for fill in fills:
            lines += [
                f"    await ctx.models.{model}.where(lambda {var}: {_condition(var, fill)}).update(",
                f"        {fill[0]}={value(fill)},",
                "    )",
            ]
    lines += [
        "",
        "",
        f"@nothing_to_reverse({_literal(demand.reverse)})",
        "def down(ctx): ...",
        "",
    ]
    return "\n".join(lines)


def _guard(demand: _Demand, template_dir: Path | None) -> str:
    """The guard that stands in for the backfill under ``new
    --no-backfill``: an ``@atomic`` step failing the migration, naming the
    count, when any row still holds ``NULL`` in one of the columns, or one of
    the enum labels the migration removes."""
    template = _template(template_dir, GUARD_TEMPLATE)
    if template is not None:
        return _fill(template, demand)
    model, columns, fills = demand.model, demand.columns, demand.fills
    var = _var(model)
    flags = " ".join(f"--no-backfill {var}.{column}" for column in columns)
    null_columns = [column for column, label in fills if label is None]
    held = [f"{label!r} in {column}" for column, label in fills if label is not None]
    what = " or ".join(
        ([f"NULL in {', '.join(null_columns)}"] if null_columns else []) + held
    )
    verb = "have" if null_columns else "hold"
    claim = (
        f"# existing {var} needs a value for {', '.join(columns)}, and this step checks it on"
        if not held
        else f"# existing {var} holds a removed label in {', '.join(columns)}, and this step checks it on"
    )
    return "\n".join(
        [
            f"# Generated by `ferro migrate new {flags}`: the migration claims no",
            claim,
            "# every database before the contract makes it required."
            if not held
            else "# every database before the contract removes the label.",
            "from ferro.migrations import MigrationRefused, atomic, nothing_to_reverse",
            "",
            "",
            "@atomic",
            "async def up(ctx):",
            f"    missing = await ctx.models.{model}.where(",
            f"        lambda {var}: {_missing(model, fills)}",
            "    ).count()",
            "    if missing:",
            "        raise MigrationRefused(",
            f'            f"{{missing}} {var} rows still {verb} {_in_fstring(what)}, which '
            f'{flags} said none would; "',
            '            "give them a value and run ferro migrate up again, or regenerate "',
            '            "the migration without --no-backfill to get a backfill"',
            "        )",
            "",
            "",
            '@nothing_to_reverse("a guard writes nothing")',
            "def down(ctx): ...",
            "",
        ]
    )


def _in_fstring(text: str) -> str:
    """``text`` inside a double-quoted f-string literal: backslashes, double
    quotes and braces escaped."""
    return (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("{", "{{")
        .replace("}", "}}")
    )


def _literal(text: str) -> str:
    """``text`` as a Python string literal, double-quoted."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
