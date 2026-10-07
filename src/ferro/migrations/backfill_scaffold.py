"""The generated backfill and guard steps (ADR-0035, ADR-0037, ADR-0040).

``Author`` gains a required ``slug``; ``ferro migrate new author_slug``
writes the backfill between the expand and the contract::

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

``new --no-backfill author.slug`` writes :func:`guard` in the backfill's
place: an ``@atomic`` step that fails the migration, naming the count, when
any row still needs a value, so a step number is never a gap (ADR-0037).

A project overrides either skeleton with ``_templates/backfill.py`` or
``_templates/guard.py`` beside its migrations, used verbatim with these
placeholders replaced:

- ``{model}``: the model's class name (``Author``);
- ``{columns}``: the columns, comma-separated (``slug, bio``);
- ``{query}``: the query of the rows still needing a value
  (``models.Author.where(lambda author: author.slug == None)``).
"""

from __future__ import annotations

import sys
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path

__all__ = [
    "BACKFILL_TEMPLATE",
    "GUARD_TEMPLATE",
    "PLACEHOLDERS",
    "backfill",
    "guard",
    "prefill_for",
]

BACKFILL_TEMPLATE = "backfill.py"
GUARD_TEMPLATE = "guard.py"
PLACEHOLDERS = ("{model}", "{columns}", "{query}")
"""What a template may say; each is replaced, nothing else is touched."""

BATCH_SIZE = 1000


def prefill_for(factory: str) -> str | None:
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


def _var(model: str) -> str:
    return model.lower()


def _fills(
    columns: Sequence[str],
    removed: Mapping[str, Sequence[str]] | None,
    nulls: Collection[str] | None,
) -> list[tuple[str, str | None]]:
    """What the step fills, in order: ``(column, None)`` for a column whose
    rows hold ``NULL``, ``(column, label)`` for each removed ``label`` its
    rows may hold. A column in ``removed`` fills its ``NULL`` rows too only
    when ``nulls`` names it; every other column fills its ``NULL`` rows."""
    removed = removed or {}
    out: list[tuple[str, str | None]] = []
    for column in columns:
        labels = removed.get(column, ())
        if not labels or (nulls is not None and column in nulls):
            out.append((column, None))
        out.extend((column, label) for label in labels)
    return out


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


def _fill(
    template: str,
    model: str,
    columns: Sequence[str],
    fills: Sequence[tuple[str, str | None]],
) -> str:
    return (
        template.replace("{model}", model)
        .replace("{columns}", ", ".join(columns))
        .replace("{query}", _query(model, fills))
    )


def _imports(prefill: Mapping[str, str]) -> list[str]:
    roots = {value.split(".", 1)[0] for value in prefill.values()}
    return [
        f"import {root}" for root in sorted(roots) if root in sys.stdlib_module_names
    ]


def backfill(
    model: str,
    columns: Sequence[str],
    *,
    driver: str,
    prefill: Mapping[str, str],
    template_dir: Path | None,
    todos: Mapping[str, str] | None = None,
    key: str | None = None,
    reverse: str = "the column goes back with the expand step's down",
    skip: str | None = None,
    removed: Mapping[str, Sequence[str]] | None = None,
    nulls: Collection[str] | None = None,
) -> str:
    """The text of the backfill over ``model``'s ``columns``.

    ``driver`` is ``"chunked"`` (paged by keyset over ``key``, the model's
    primary key) or ``"atomic"``. ``prefill`` maps a column to the Python
    expression each row gets (a standard-library factory's call,
    :func:`prefill_for`); every other column holds ``todo(todos[column])``
    (default: ``the <column> for an existing <model>``). ``removed`` maps a
    column to the enum labels the migration removes (D2): the rows holding
    each get ``todo("the label to use instead of '<label>'")``, and the
    column's ``NULL`` rows are filled too only when ``nulls`` names it.
    ``reverse`` is the down's ``@nothing_to_reverse`` reason; ``skip`` the
    ``--no-backfill`` arguments the header names for skipping the step. The
    project's ``template_dir/backfill.py`` wins when it exists.
    """
    if driver not in ("chunked", "atomic"):
        raise ValueError(f"a backfill is chunked or atomic, not {driver!r}")
    if driver == "chunked" and key is None:
        raise ValueError(
            "a chunked backfill pages by the model's primary key: pass key="
        )
    fills = _fills(columns, removed, nulls)
    template = _template(template_dir, BACKFILL_TEMPLATE)
    if template is not None:
        return _fill(template, model, columns, fills)
    var = _var(model)

    def value(fill: tuple[str, str | None]) -> str:
        column, label = fill
        if label is not None:
            return _label_todo(label)
        return prefill.get(column) or (
            f"todo({_literal((todos or {}).get(column) or f'the {column} for an existing {var}')})"
        )

    names = ["nothing_to_reverse"]
    names.append("chunked" if driver == "chunked" else "atomic")
    unwritten = any(
        label is not None or column not in prefill for column, label in fills
    )
    lines = [f"# Gives every existing {var} a value for {', '.join(columns)}."]
    if unwritten:
        names.append("todo")
        lines.append("# Write each value marked todo, then run `ferro migrate up`.")
    if skip is not None:
        lines += [
            "# If no row needs a value, delete this unapplied migration and run",
            f"# `ferro migrate new <name> {skip}` for a guard step instead.",
        ]
    lines += [*_imports(prefill)]
    lines += [f"from ferro.migrations import {', '.join(sorted(names))}", "", ""]
    if driver == "chunked":
        lines += [
            "@chunked(",
            f"    lambda models: {_query(model, fills)}",
            f"    .order_by(lambda {var}: {var}.{key}),",
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
        f"@nothing_to_reverse({_literal(reverse)})",
        "def down(ctx): ...",
        "",
    ]
    return "\n".join(lines)


def guard(
    model: str,
    columns: Sequence[str],
    *,
    template_dir: Path | None = None,
    removed: Mapping[str, Sequence[str]] | None = None,
    nulls: Collection[str] | None = None,
) -> str:
    """The text of the guard that stands in for ``model``'s backfill under
    ``new --no-backfill``: an ``@atomic`` step failing the migration, naming
    the count, when any row still holds ``NULL`` in one of ``columns`` — or,
    for a column in ``removed``, one of the enum labels the migration
    removes (``nulls`` as for :func:`backfill`). The project's
    ``template_dir/guard.py`` wins when it exists."""
    fills = _fills(columns, removed, nulls)
    template = _template(template_dir, GUARD_TEMPLATE)
    if template is not None:
        return _fill(template, model, columns, fills)
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
