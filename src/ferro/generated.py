"""Generated columns declared with `Field(generated=...)` (ADR-0058, ADR-0059).

A generated column's value comes only from the database. Its body is a lambda
over the model's own columns, evaluated once per compile against the same
validating proxy table checks use, and lowered to the schema's value
expression. The columns the expression reads are derived here, once, from
the lowered tree by the Rust core: clear-on-assign reads them.
"""

from __future__ import annotations

import dis
import enum
import json
import math
import types
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from . import _core
from . import fn as _fn
from .checks import (
    _bound_default_values,
    _check_literal_token,
    _CheckProxy,
    _iter_code_objects,
    _relation_shadow_columns,
    _reverse_relation_names,
)
from .query.nodes import (
    BinaryValueExpr,
    FieldProxy,
    FuncValueExpr,
    NegateValueExpr,
    QueryNode,
)

#: Python arithmetic to the IR's binary operator.
_BINARY_OPS = {"+": "add", "-": "sub", "*": "mul", "/": "div"}

_GLOBAL_LOAD_OPS = frozenset({"LOAD_GLOBAL", "LOAD_NAME"})

_DIALECT = (
    "a value expression is built from the model's own columns, "
    "literals, + - * /, unary - and the functions in ferro.fn"
)

_NOT_A_CONDITION = (
    "a generated column holds a value, not a condition; declare the condition "
    "as a Check in __ferro_checks__ instead"
)

#: The Python annotation each value family is declared with, for refusals;
#: the family table is the Rust core's.
_ANNOTATIONS: dict[str, str] = dict(_core._value_family_annotations())

#: Whether a value of one family fits a parameter of another (an integer
#: fits a float or decimal), as the Rust core decides it.
_fits = _core._value_family_fits


def _typing_column(spec: Any) -> dict[str, Any]:
    """A column spec as the Rust typing reads it: its SchemaIR column."""
    from .ir.compiler import _column_ir_from_spec

    # The typing never reads a default, and a Python default may not be JSON.
    return {**_column_ir_from_spec(spec), "default": None, "retired": spec.retired}


def _column_family(spec: Any) -> str | None:
    """The value family a column holds, or `None` for one no expression yields."""
    return _core._value_column_family(json.dumps(_typing_column(spec)))


def value_expr_type(
    tree: Mapping[str, Any], columns: Mapping[str, Any]
) -> tuple[str, bool]:
    """Type a lowered value expression under value parity.

    The typing is the Rust core's, the same one the Postgres renderer and
    the SQLite guard read.

    Args:
        tree: The lowered expression, as `lower_value_expr` returns it.
        columns: The compile's column specs, keyed by column name.

    Returns:
        The expression's value family (`"integer"`, `"number"`,
            `"decimal"`, `"string"`, `"date"`, `"datetime"` or
            `"boolean"`) and whether it can be `NULL`.

    Raises:
        TypeError: The expression could store a different value, or fail
            differently, on SQLite and Postgres.
    """
    typing_columns = [_typing_column(spec) for spec in columns.values()]
    return _core._value_expr_type(json.dumps(tree), json.dumps(typing_columns))


@dataclass(frozen=True)
class GeneratedColumnSpec:
    """One compiled generated column: its expression and the columns it reads."""

    expression: dict[str, Any]
    """The value expression, lowered to the JSON shape the schema carries."""
    inputs: frozenset[str]
    """Every column the expression reads."""


class _ValueProxy(_CheckProxy):
    """The check proxy, naming a generated expression and refusing relations."""

    __slots__ = ()

    _subject = "generated expression"

    def __getattr__(self, name: str) -> Any:
        shadow = self._relations.get(name)
        if shadow is not None or name in self._reverse_relations:
            hint = f"; read the shadow column {shadow!r} instead" if shadow else ""
            raise TypeError(
                f"generated expression reads the relation {name!r}: {_DIALECT}, "
                f"and a relation is another table{hint}"
            )
        return super().__getattr__(name)


def _is_value_function(value: Any) -> bool:
    if value is _fn:
        return True
    return callable(value) and getattr(value, "__module__", None) == _fn.__name__


def _is_allowed_capture(value: Any) -> bool:
    if _is_value_function(value) or isinstance(value, enum.Enum):
        return True
    return isinstance(value, type) and issubclass(value, enum.Enum)


def _validate_source(func: Callable[..., Any], where: str) -> None:
    """Refuse closures, bound defaults and every global that is not `ferro.fn`.

    A builtin such as `abs` is a global the module does not define: it would
    run in Python on the proxy, so it is refused naming `ferro.fn`.
    """
    code = func.__code__
    for name, cell in zip(code.co_freevars, func.__closure__ or (), strict=True):
        if not _is_allowed_capture(cell.cell_contents):
            raise TypeError(
                f"{where} closes over {name!r}: {_DIALECT}; inline the value as a "
                "literal, not a variable from the enclosing scope"
            )
    if _bound_default_values(func):
        raise TypeError(
            f"{where} binds a default argument: {_DIALECT}; inline the value as a "
            "literal"
        )
    for code_obj in _iter_code_objects(code):
        for inst in dis.get_instructions(code_obj):
            if inst.opname not in _GLOBAL_LOAD_OPS or not isinstance(inst.argval, str):
                continue
            name = inst.argval
            if name in func.__globals__:
                if _is_allowed_capture(func.__globals__[name]):
                    continue
                raise TypeError(
                    f"{where} closes over {name!r}: {_DIALECT}; inline the value "
                    "as a literal, not a module variable"
                )
            raise TypeError(
                f"{where} calls {name!r}, which is not a ferro.fn function: "
                f"{_DIALECT} (see ferro.fn.__all__)"
            )


def lower_value_expr(value: Any, *, model_name: str, owner: str) -> dict[str, Any]:
    """Lower an evaluated value expression to the schema's JSON shape.

    Args:
        value: What the lambda returned: a column, a literal, arithmetic or
            a `ferro.fn` call.
        model_name: The model, named in refusals.
        owner: The field or check the expression belongs to, named in
            refusals.

    Returns:
        The tagged tree: `column`, `literal`, `binary`, `negate` or `func`.

    Raises:
        TypeError: The expression holds something outside the value
            expression dialect.
    """
    where = f"{model_name}.{owner}"
    if isinstance(value, FieldProxy):
        if value.path:
            raise TypeError(
                f"{where} reads {value._dotted()}: {_DIALECT}, and relation "
                "traversal reads another table"
            )
        return {"kind": "column", "name": value.column}
    if isinstance(value, BinaryValueExpr):
        return {
            "kind": "binary",
            "op": _BINARY_OPS[value.op],
            "left": lower_value_expr(value.left, model_name=model_name, owner=owner),
            "right": lower_value_expr(value.right, model_name=model_name, owner=owner),
        }
    if isinstance(value, NegateValueExpr):
        return {
            "kind": "negate",
            "operand": lower_value_expr(
                value.operand, model_name=model_name, owner=owner
            ),
        }
    if isinstance(value, FuncValueExpr):
        return {
            "kind": "func",
            "name": value.name,
            "args": [
                lower_value_expr(arg, model_name=model_name, owner=owner)
                for arg in value.args
            ],
        }
    if isinstance(value, QueryNode):
        raise TypeError(
            f"{where} is a comparison where a value belongs: a value expression "
            "holds a value, not a condition; declare a condition as a Check in "
            "__ferro_checks__"
        )
    if isinstance(value, float) and not math.isfinite(value):
        raise TypeError(
            f"{where} holds the float {value!r}, which neither database stores "
            "in an expression; use a finite literal"
        )
    if isinstance(value, Decimal):
        raise TypeError(
            f"{where} holds the Decimal {value!r}: a value expression takes int "
            "and float literals; write the literal as one"
        )
    if isinstance(value, (bool, int, float, str, enum.Enum)):
        return {"kind": "literal", "token": _check_literal_token(value)}
    raise TypeError(f"{where} holds {type(value).__name__} {value!r}: {_DIALECT}")


def _refuse_unequal_family(
    where: str,
    expression: Mapping[str, Any],
    columns: Mapping[str, Any],
    field_name: str,
) -> None:
    """Refuse an expression whose value family is not its column's."""
    try:
        family, _ = value_expr_type(expression, columns)
    except TypeError as exc:
        raise TypeError(f"{where} {exc}") from exc
    if family == "boolean":
        raise TypeError(f"{where} is a boolean: {_NOT_A_CONDITION}")
    spec = columns[field_name]
    column_family = _column_family(spec)
    if column_family is None:
        annotation = getattr(spec.python_type, "__name__", spec.python_type)
        raise TypeError(
            f"{where} is declared {annotation}, which no value expression yields; "
            f"a generated column is one of {', '.join(_ANNOTATIONS.values())}"
        )
    if family != column_family:
        raise TypeError(
            f"{where} computes {family} ({_ANNOTATIONS[family]}) but the field is "
            f"{_ANNOTATIONS[column_family]}: SQLite and Postgres would store "
            f"different values; annotate the field {_ANNOTATIONS[family]} or "
            f"change the expression to yield {_ANNOTATIONS[column_family]}"
        )
    refusal = _core._generated_storage_refusal(json.dumps(_typing_column(spec)))
    if refusal is not None:
        raise TypeError(f"{where} {refusal}")


def _declared_generated(model_cls: type[Any]) -> dict[str, Callable[..., Any]]:
    return {
        field_name: meta.generated
        for field_name, meta in (getattr(model_cls, "ferro_fields", {}) or {}).items()
        if getattr(meta, "generated", None) is not None
    }


def compile_generated_columns(
    model_cls: type[Any], model_name: str, columns: Mapping[str, Any]
) -> dict[str, GeneratedColumnSpec]:
    """Validate and lower every `Field(generated=...)` of one model.

    Called on every compile pass beside the table checks, with the column
    specs that pass just built.

    Args:
        model_cls: The model class.
        model_name: The model class name, for refusals.
        columns: The compile's column specs, keyed by column name.

    Returns:
        One `GeneratedColumnSpec` per generated field, in declaration order.

    Raises:
        TypeError: An expression outside the value expression dialect, an
            unknown column, a closure, an expression that reads itself or
            another generated column, or one `value_expr_type` refuses.
    """
    declared = _declared_generated(model_cls)
    if not declared:
        return {}
    proxy = _ValueProxy(
        model_cls,
        columns,
        _relation_shadow_columns(model_cls, columns),
        _reverse_relation_names(model_cls),
    )
    specs: dict[str, GeneratedColumnSpec] = {}
    for field_name, func in declared.items():
        where = f"{model_name}.{field_name}"
        if not isinstance(func, types.FunctionType):
            raise TypeError(
                f"{where} generated must be a lambda over the model's columns, not "
                f"{type(func).__name__}"
            )
        _validate_source(func, where)
        try:
            result = func(proxy)
        except (AttributeError, TypeError, ValueError) as exc:
            raise TypeError(f"{where}: {exc}") from exc
        expression = lower_value_expr(result, model_name=model_name, owner=field_name)
        inputs = set(_core._value_expr_columns(json.dumps(expression)))
        generated_inputs = sorted(inputs & set(declared))
        if generated_inputs:
            raise TypeError(
                f"{where} reads the generated column {generated_inputs[0]!r}: a "
                "generated column reads plain columns only; repeat that column's "
                "expression instead"
            )
        _refuse_unequal_family(where, expression, columns, field_name)
        specs[field_name] = GeneratedColumnSpec(
            expression=expression, inputs=frozenset(inputs)
        )
    return specs
