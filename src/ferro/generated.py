"""Generated columns declared with `Field(generated=...)` (ADR-0058, ADR-0059).

A generated column's value comes only from the database. Its body is a lambda
over the model's own columns, evaluated once per compile against the same
validating proxy table checks use, and lowered to the schema's value
expression. The columns the expression reads are derived here, once, from
the lowered tree: clear-on-assign reads them.
"""

from __future__ import annotations

import dis
import enum
import types
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

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
    "a generated column's expression is built from the model's own columns, "
    "literals, + - * /, unary - and the functions in ferro.fn"
)


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
            f"{where} is a comparison: {_DIALECT}, and a generated column holds "
            "a value, not a condition"
        )
    if isinstance(value, Decimal) and value.is_finite():
        # A numeric token, never the quoted text a check gives a non-number.
        return {"kind": "literal", "token": str(value)}
    if isinstance(value, (bool, int, float, str, enum.Enum)):
        return {"kind": "literal", "token": _check_literal_token(value)}
    raise TypeError(f"{where} holds {type(value).__name__} {value!r}: {_DIALECT}")


def _collect_inputs(expression: Mapping[str, Any], out: set[str]) -> None:
    kind = expression["kind"]
    if kind == "column":
        out.add(expression["name"])
    elif kind == "binary":
        _collect_inputs(expression["left"], out)
        _collect_inputs(expression["right"], out)
    elif kind == "negate":
        _collect_inputs(expression["operand"], out)
    elif kind == "func":
        for arg in expression["args"]:
            _collect_inputs(arg, out)


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
            unknown column, a closure, a generated primary key, or an
            expression that reads itself or another generated column.
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
        if model_cls.ferro_fields[field_name].primary_key:
            raise TypeError(
                f"{where}: a primary key cannot be generated; drop primary_key=True "
                "or generated="
            )
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
        inputs: set[str] = set()
        _collect_inputs(expression, inputs)
        generated_inputs = sorted(inputs & set(declared))
        if generated_inputs:
            raise TypeError(
                f"{where} reads the generated column {generated_inputs[0]!r}: a "
                "generated column reads plain columns only; repeat that column's "
                "expression instead"
            )
        specs[field_name] = GeneratedColumnSpec(
            expression=expression, inputs=frozenset(inputs)
        )
    return specs
