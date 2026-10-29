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
import functools
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

#: The value families a column can hold, by its logical type.
_FAMILIES = frozenset(
    {"integer", "number", "decimal", "string", "date", "datetime", "boolean"}
)

#: The Python annotation each family is declared with, for refusals.
_ANNOTATIONS = {
    "integer": "int",
    "number": "float",
    "decimal": "Decimal",
    "string": "str",
    "date": "date",
    "datetime": "datetime",
    "boolean": "bool",
}

#: An integer argument also fits a float or decimal parameter.
_WIDENS_TO = {"integer": frozenset({"number", "decimal"})}


@functools.cache
def _function_table() -> dict[str, dict[str, Any]]:
    """The value function table, read once from the Rust table over FFI."""
    return {entry["name"]: entry for entry in json.loads(_core._value_function_table())}


def _column_family(spec: Any) -> str | None:
    """The value family a column holds, or `None` for one no expression yields."""
    if spec.enum_class is not None or spec.enum_values is not None:
        return None
    return spec.logical_type if spec.logical_type in _FAMILIES else None


def _literal_family(token: str) -> str:
    """The family of a literal token as `_check_literal_token` renders it."""
    if token in ("true", "false"):
        return "boolean"
    if token.startswith("'"):
        return "string"
    if token.lstrip("-").isdigit():
        return "integer"
    return "number"


def _fits(argument: str, parameter: str) -> bool:
    return argument == parameter or parameter in _WIDENS_TO.get(argument, ())


def _signature_text(signature: Mapping[str, Any]) -> str:
    args = ", ".join(signature["args"]) + (", ..." if signature["variadic"] else "")
    return f"({args}) -> {signature['result']}"


def _call_type(
    name: str, args: list[tuple[str, bool]], columns: list[str | None]
) -> tuple[str, bool]:
    """The family and nullability of one `ferro.fn` call, or a refusal."""
    function = _function_table()[name]
    families = [family for family, _ in args]
    for signature in function["signatures"]:
        params = list(signature["args"])
        if signature["variadic"]:
            if len(families) < len(params):
                continue
            params += [params[-1]] * (len(families) - len(params))
        elif len(families) != len(params):
            continue
        if all(_fits(family, param) for family, param in zip(families, params)):
            break
    else:
        reason = ""
        if "datetime" in families and any(
            "date" in signature["args"] for signature in function["signatures"]
        ):
            reason = (
                ": the part of a datetime depends on the session's time zone, "
                "which Postgres refuses in a generated column; read a date "
                "field instead"
            )
        elif "string" in families and name in ("least", "greatest"):
            reason = (
                ": text compares by collation on Postgres and byte by byte on "
                "SQLite, so it is refused"
            )
        signatures = ", ".join(
            _signature_text(signature) for signature in function["signatures"]
        )
        raise TypeError(
            f"fn.{name} takes {signatures}, not ({', '.join(families)}){reason}"
        )
    nulls = function["nulls"]
    if nulls == "refused":
        nullable = [
            column for (_, is_nullable), column in zip(args, columns) if is_nullable
        ]
        if nullable:
            subject = (
                f"the nullable column {nullable[0]!r}"
                if nullable[0]
                else "a nullable argument"
            )
            raise TypeError(
                f"fn.{name} over {subject}: Postgres skips a NULL where SQLite "
                "returns NULL; wrap it as fn.coalesce(<column>, <fallback>)"
            )
        return signature["result"], False
    if nulls == "first_non_null":
        return signature["result"], all(is_nullable for _, is_nullable in args)
    return signature["result"], any(is_nullable for _, is_nullable in args)


def _arithmetic_type(
    op: str, left: tuple[str, bool], right: tuple[str, bool]
) -> tuple[str, bool]:
    families = {left[0], right[0]}
    if "decimal" in families:
        raise TypeError(
            f"{op!r} over a decimal: SQLite computes decimal arithmetic in "
            "floating point and Postgres exactly, so the stored values would "
            "differ; store the amount in minor units as an int"
        )
    if not families <= {"integer", "number"}:
        shown = ", ".join(sorted(families))
        raise TypeError(f"{op!r} takes integers and floats, not {shown}")
    family = "integer" if families == {"integer"} else "number"
    if family == "number" and op in ("*", "/"):
        raise TypeError(
            f"{op!r} over a float: Postgres raises when a float product or "
            "quotient underflows to zero where SQLite stores 0.0, so the "
            "databases would disagree; keep the arithmetic in integers (an "
            "amount in minor units, such as amount_minor) or compute the "
            "value in the application"
        )
    return family, left[1] or right[1]


def value_expr_type(
    tree: Mapping[str, Any], columns: Mapping[str, Any]
) -> tuple[str, bool]:
    """Type a lowered value expression under value parity.

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
    kind = tree["kind"]
    if kind == "column":
        spec = columns[tree["name"]]
        if spec.retired:
            raise TypeError(
                f"reads the retired column {tree['name']!r}, which is leaving the "
                "table; read a column the model still maps"
            )
        family = _column_family(spec)
        if family is None:
            raise TypeError(
                f"reads {tree['name']!r}, a column no value expression can read; "
                f"it takes {', '.join(_ANNOTATIONS.values())} columns"
            )
        return family, spec.nullable
    if kind == "literal":
        return _literal_family(tree["token"]), False
    if kind == "negate":
        family, nullable = value_expr_type(tree["operand"], columns)
        if family not in ("integer", "number", "decimal"):
            raise TypeError(f"unary - takes a number, not {family}")
        return family, nullable
    if kind == "binary":
        op = {"add": "+", "sub": "-", "mul": "*", "div": "/"}[tree["op"]]
        right = tree["right"]
        if op == "/" and not (
            right["kind"] == "literal"
            and _literal_family(right["token"]) in ("integer", "number")
            and float(right["token"]) != 0
        ):
            raise TypeError(
                "divides by something other than a non-zero literal: Postgres "
                "raises on division by zero where SQLite stores NULL; divide by "
                "a non-zero literal such as / 100"
            )
        return _arithmetic_type(
            op,
            value_expr_type(tree["left"], columns),
            value_expr_type(right, columns),
        )
    args = [value_expr_type(arg, columns) for arg in tree["args"]]
    names = [arg["name"] if arg["kind"] == "column" else None for arg in tree["args"]]
    return _call_type(tree["name"], args, names)


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
        inputs: set[str] = set()
        _collect_inputs(expression, inputs)
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
