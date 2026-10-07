"""Historical models: the classes a data step queries, built from snapshots.

Migration ``0011_backfill_slugs`` adds ``slug`` to ``author`` and renames
``name`` to ``full_name``. Its data step runs between the expand and the
contract, when the live table holds both the old columns and the new ones,
so its historical ``Author`` is the **union** of the parent's snapshot and
the migration's own (ADR-0025)::

    parent  0010/ir.json   author: id, name, bio
    own     0011/ir.json   author: id, full_name (renamed_from "name"), slug
    union                  author: id, full_name, bio, slug (nullable)

- a column only the migration's own snapshot has is nullable, whatever it
  declares: the expand created it so and every row holds ``NULL`` until the
  backfill runs;
- a live rename hint (the own snapshot names an old column the parent has,
  and the parent lacks the new name) is one column under its new name;
- a same-named column whose storage differs between the two snapshots (its
  type, ``db_type`` or enum type) is refused, naming both declarations: the
  safe expand/contract shape never produces it. A column that only stops (or
  starts) accepting ``NULL`` is not that: the union keeps it nullable, which
  is what lets a backfill fill the ``NULL``s before the contract;
- an enum's labels are the union of both snapshots' labels.

Each table becomes a columns-only class built through ferro's own metaclass
(no relations, methods or validators; a foreign key is its plain ``*_id``
column), under the module identity ``ferro.migrations.historical.<rev>``,
registered into a registry of its own (:meth:`ferro.registry.Registry.capture`)
so today's registry never sees it. A many-to-many join table gets a class
like any other table.
"""

from __future__ import annotations

import datetime as _dt
import decimal
import enum
import uuid
from collections.abc import Iterable
from typing import Any, cast

from pydantic import ConfigDict, TypeAdapter

from .context import HistoricalModels
from .errors import MigrationRefused

__all__ = ["HistoricalModelError", "build", "build_single"]

_MODULE = "ferro.migrations.historical"

_ANNOTATIONS: dict[str, Any] = {
    "integer": int,
    "number": float,
    "decimal": decimal.Decimal,
    "boolean": bool,
    "string": str,
    "datetime": _dt.datetime,
    "date": _dt.date,
    "time": _dt.time,
    "uuid": uuid.UUID,
    "binary": bytes,
    "json": dict[str, Any] | list[Any],
    "unknown": Any,
}
"""Each SchemaIR ``logical_type`` and the annotation that compiles back to it
(``ferro.columns._logical_type`` read in reverse). :func:`_check_compiled`
pins every class against its union, so a gap here fails loudly."""

# The column facts that decide storage: a same-named column differing in one
# of them is two columns, which the union refuses.
_STORAGE = ("logical_type", "db_type", "enum_type_name", "primary_key")

_CONFIG = ConfigDict(
    from_attributes=True,
    arbitrary_types_allowed=True,
    use_attribute_docstrings=False,
)
"""Every historical class's ``model_config``: not the application's, and
never ``use_attribute_docstrings`` (there is no source to read)."""


class HistoricalModelError(MigrationRefused):
    """A union of two snapshots no historical model can describe."""


def build(
    previous: dict[str, Any] | None, current: dict[str, Any], *, rev: str
) -> HistoricalModels:
    """The historical models of migration ``rev``: the union of its parent's
    snapshot ``previous`` (``None`` for the first migration) and its own
    ``current`` (SchemaIR envelopes, as ``ir.json`` stores them).

    Raises:
        HistoricalModelError: a same-named column is declared with different
            storage in the two snapshots (naming both).
    """
    tables = _union(_tables(previous), _tables(current), rev)
    return _models(tables, rev)


def build_single(snapshot: dict[str, Any], rev: str) -> HistoricalModels:
    """The historical models of one snapshot alone, as it declares them."""
    return _models(_tables(snapshot), rev)


# -- the union ------------------------------------------------------------------------


def _tables(envelope: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if envelope is None:
        return {}
    return {model["table_name"]: model for model in envelope["payload"]["models"]}


def _columns(model: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {column["name"]: column for column in model["columns"]}


def _union(
    previous: dict[str, dict[str, Any]],
    current: dict[str, dict[str, Any]],
    rev: str,
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    consumed: set[str] = set()
    for table, model in current.items():
        old = model.get("renamed_from")
        source = table
        if old is not None and old in previous and table not in previous:
            source = old
        if source not in previous:
            # A table the migration creates is created as declared.
            out[table] = model
            continue
        consumed.add(source)
        out[table] = {**model, "columns": _union_columns(previous[source], model, rev)}
    for table, model in previous.items():
        if table not in consumed and table not in out:
            out[table] = model
    return out


def _union_columns(
    previous: dict[str, Any], current: dict[str, Any], rev: str
) -> list[dict[str, Any]]:
    before = _columns(previous)
    after = _columns(current)
    # A live rename hint: the parent holds the old name and lacks the new.
    for name, column in after.items():
        old = column.get("renamed_from")
        if old is not None and old in before and name not in before:
            before[name] = before.pop(old)
    merged: dict[str, dict[str, Any]] = {}
    for name in [*before, *(n for n in after if n not in before)]:
        old, new = before.get(name), after.get(name)
        if old is None:
            assert new is not None
            merged[name] = new if new["primary_key"] else {**new, "nullable": True}
        elif new is None:
            merged[name] = old
        else:
            merged[name] = _same_column(previous, old, current, new, rev)
    return list(merged.values())


def _same_column(
    previous: dict[str, Any],
    old: dict[str, Any],
    current: dict[str, Any],
    new: dict[str, Any],
    rev: str,
) -> dict[str, Any]:
    """One column both snapshots declare: refused when its storage differs."""
    if any(old.get(fact) != new.get(fact) for fact in _STORAGE):
        raise HistoricalModelError(
            f"ferro migrate: {rev}: a data step cannot see {current['table_name']}."
            f"{new['name']} as one column: the parent snapshot declares it as "
            f"{_describe(old)} ({previous['table_name']}.{old['name']}) and this "
            f"migration's as {_describe(new)}. Split the change: add a new column, "
            f"copy into it in a data step, drop the old one, then rename."
        )
    column = {**new, "nullable": bool(old["nullable"] or new["nullable"])}
    if old.get("enum_values") is not None or new.get("enum_values") is not None:
        column["enum_values"] = list(
            dict.fromkeys(
                [*(old.get("enum_values") or []), *(new.get("enum_values") or [])]
            )
        )
    return column


def _describe(column: dict[str, Any]) -> str:
    parts = [column["logical_type"]]
    if column.get("db_type"):
        parts.append(f"db_type={column['db_type']}")
    if column.get("enum_type_name"):
        parts.append(f"enum {column['enum_type_name']}")
    if column["primary_key"]:
        parts.append("primary key")
    parts.append("nullable" if column["nullable"] else "not null")
    return ", ".join(parts)


# -- the classes ----------------------------------------------------------------------


def _models(tables: dict[str, dict[str, Any]], rev: str) -> HistoricalModels:
    from .. import ensure_resolved_modelset
    from ..registry import REGISTRY

    names = [_class_name(model) for model in tables.values()]
    module = f"{_MODULE}.{rev}"

    def define() -> dict[str, Any]:
        classes = {
            table: _model_class(model, module, names.count(_class_name(model)) > 1)
            for table, model in tables.items()
        }
        ensure_resolved_modelset()
        return classes

    classes, state = REGISTRY.capture(define)
    for table, cls in classes.items():
        _check_compiled(cls, tables[table], rev)
    return HistoricalModels(rev, classes, state)


def _class_name(model: dict[str, Any]) -> str:
    return str(model["model_name"]).rsplit(".", 1)[-1]


def _model_class(model: dict[str, Any], module: str, shared_name: bool) -> type:
    from ..metaclass import ModelMetaclass
    from ..models import Model

    name = _class_name(model)
    namespace: dict[str, Any] = {
        "__module__": module,
        # Identity is ``<module>.<qualname>``: a class name two tables share
        # takes its table name so each identity stays unique.
        "__qualname__": model["table_name"] if shared_name else name,
        "__ferro_table__": model["table_name"],
        "model_config": _CONFIG,
    }
    annotations: dict[str, Any] = {}
    for column in model["columns"]:
        annotation, default = _field(model, column)
        annotations[column["name"]] = annotation
        namespace[column["name"]] = default
    namespace["__annotations__"] = annotations
    return ModelMetaclass(name, (Model,), namespace)


def _field(model: dict[str, Any], column: dict[str, Any]) -> tuple[Any, Any]:
    """One column's annotation and its ``Field``."""
    from ..fields import Field

    annotation = (
        _enum(column)
        if column.get("enum_values") is not None
        else _ANNOTATIONS.get(column["logical_type"])
    )
    if annotation is None:
        raise HistoricalModelError(
            f"ferro migrate: {model['table_name']}.{column['name']} has logical type "
            f"{column['logical_type']!r}, which no historical model can carry"
        )
    options: dict[str, Any] = {}
    if column.get("db_type_explicit"):
        options["db_type"] = column["db_type"]
    if column["primary_key"]:
        options["primary_key"] = True
        options["autoincrement"] = column["autoincrement"]
    nullable = column["nullable"] or column["primary_key"]
    if nullable:
        annotation = annotation | None
    default = column.get("default")
    if default is not None:
        default = TypeAdapter(annotation).validate_python(default)
        return annotation, Field(default=default, **options)
    if nullable:
        return annotation, Field(default=None, **options)
    return annotation, Field(..., **options)


def _enum(column: dict[str, Any]) -> type[enum.Enum]:
    """The historical enum: every label either snapshot declares. A value
    the live type holds beyond them fails hydration naming it (ADR-0035)."""
    type_name = column["enum_type_name"]
    values: Iterable[Any] = column["enum_values"]
    # Member names only label the values; a sunder-looking label gets a prefix.
    members = {
        (f"v{value}" if str(value).startswith("_") else str(value)): value
        for value in values
    }
    # The functional API on an empty Enum subclass returns the new enum class.
    return cast("type[enum.Enum]", _HistoricalEnum(type_name, members))


class _HistoricalEnum(enum.Enum):
    """The base of every historical enum: a label the class lacks is drift."""

    @classmethod
    def _missing_(cls, value: object) -> Any:
        declared = ", ".join(repr(member.value) for member in cls)
        raise ValueError(
            f"the live enum type {cls.__name__} holds the label {value!r}, which "
            f"neither snapshot of this migration declares ({declared}); that is "
            f"drift, and a data step does not run against a schema nobody reviewed"
        )


def _check_compiled(cls: type, model: dict[str, Any], rev: str) -> None:
    """Every class compiles back to exactly the storage its union declares."""
    compiled = {
        column.name: column for column in getattr(cls, "__ferro_columns__").values()
    }
    for column in model["columns"]:
        spec = compiled.get(column["name"])
        expected = (
            column["logical_type"],
            column.get("db_type") if column.get("db_type_explicit") else None,
            column.get("enum_type_name"),
            column["primary_key"],
            column["nullable"] and not column["primary_key"],
        )
        actual = (
            (
                spec.logical_type,
                spec.db_type,
                spec.enum_type_name,
                spec.primary_key,
                spec.nullable,
            )
            if spec is not None
            else None
        )
        if actual != expected:
            raise RuntimeError(
                f"ferro: the historical model of {model['table_name']} ({rev}) compiles "
                f"{column['name']} as {actual}, not the snapshot's {expected}; this is a "
                f"ferro bug, please report it"
            )
