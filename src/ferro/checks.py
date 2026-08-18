"""Table-level CHECK constraints declared on Ferro models (ADR-0012).

A **table check** is declared as ``Check(suffix, predicate)`` in
``__ferro_checks__``. The predicate is a ferro lambda over the model's own
columns — not a SQL string — so both DDL emitters render the constraint body
from one structured IR (``CheckExpr``) and unknown columns fail at class
definition (I-1, ADR-0012).

This module owns the declaration surface and the lowering from the evaluated
``QueryNode`` tree to the ``CheckExpr`` wire shape. The constraint NAME is
derived by the SchemaIR compiler from the shared Rust builder
(``_ddl_table_check_constraint_name``), never here.

Supported predicate dialect in this release (ADR-0016): ``== None`` /
``!= None`` — on a column, on a forward-FK relation, or on its shadow ``*_id``
column — combined with ``&``, ``|``, and ``~``. Comparisons against literals,
``.in_()``, ``.like()``, relation traversal, existence tests, and aggregates
are rejected at class definition until the check-predicate dialect grows
(#346); the lowering below is the single place new arms attach.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .base import ForeignKey
from .query.nodes import FieldProxy, Predicate, QueryNode, QueryProxy

FERRO_CHECKS = "__ferro_checks__"

_SUFFIX_RE = re.compile(r"^[a-z][a-z0-9_]*\Z")
_SUFFIX_SHAPE = "[a-z][a-z0-9_]*"

#: Leaf operators that lower to a NULL test, and the ``CheckExpr`` kind each
#: produces. Every other operator belongs to #346.
_NULL_TEST_KINDS = {"==": "is_null", "!=": "is_not_null"}

#: How to spell a rejected leaf operator back to the user.
_OPERATOR_SPELLING = {"IN": "in_()", "LIKE": "like()"}


@dataclass(frozen=True)
class Check:
    """One table check: a name suffix plus a ferro predicate lambda.

    The live constraint name is ``ck_<table>_<suffix>`` — ferro owns the
    prefix, so ``suffix`` is a bare identifier (``[a-z][a-z0-9_]*``), unique
    per model.

    Examples:
        >>> Check("at_most_one_outflow", lambda transfer: transfer.outflow_transaction == None)
        Check(suffix='at_most_one_outflow')
    """

    suffix: str
    predicate: Predicate[Any]

    def __post_init__(self) -> None:
        if not isinstance(self.suffix, str):
            raise TypeError(
                f"Check suffix must be a str, not {type(self.suffix).__name__}"
            )
        if self.suffix.startswith("ck_"):
            raise TypeError(
                f"Check suffix {self.suffix!r} must not include the 'ck_<table>_' "
                "prefix: ferro derives the full constraint name from the table and "
                "the suffix (e.g. Check('at_most_one_outflow', ...) becomes "
                "'ck_transfer_at_most_one_outflow')."
            )
        if not _SUFFIX_RE.match(self.suffix):
            raise TypeError(
                f"Check suffix {self.suffix!r} is not a valid identifier: expected "
                f"{_SUFFIX_SHAPE} (lowercase letters, digits, and underscores, "
                "starting with a letter)."
            )
        if not callable(self.predicate):
            raise TypeError(
                f"Check({self.suffix!r}, ...) predicate must be a callable ferro "
                "predicate (e.g. `lambda transfer: transfer.outflow_transaction "
                f"== None`), not {type(self.predicate).__name__}. Raw SQL strings "
                "are not check predicates (ADR-0012)."
            )

    def __repr__(self) -> str:
        return f"Check(suffix={self.suffix!r})"


@dataclass(frozen=True)
class TableCheckSpec:
    """One compiled table check: its suffix and its ``CheckExpr`` payload.

    The SchemaIR compiler turns the suffix into the canonical constraint name;
    ``predicate`` is already in the ``CheckExpr`` wire shape pinned by
    ``crates/ferro-schema-ir``.
    """

    suffix: str
    predicate: dict[str, Any]


class _CheckProxy(QueryProxy[Any]):
    """Validating predicate proxy over ONE compile's freshly built facts.

    ``where()`` builds its proxy from the facts published on the model class
    (``__ferro_query_columns__``, ``__ferro_relation_specs__``). A check
    predicate is evaluated *inside* the compile that produces those facts —
    before they are published, and for a model whose relations never change
    during resolution that compile is the only one there is — so this proxy
    reads the column specs and relation metadata the compile just built.

    Relation resolution deliberately does not need a resolved target class: a
    forward FK's shadow column is ``{field}_id`` by the single ferro
    convention, which is all a null test needs, so a forward-referenced FK
    compiles at class definition like any other.
    """

    __slots__ = ("_columns", "_relations", "_reverse_relations")

    def __init__(
        self,
        model_cls: type,
        columns: Mapping[str, Any],
        relations: Mapping[str, str],
        reverse_relations: frozenset[str],
    ) -> None:
        super().__init__(model_cls)
        self._columns = columns
        self._relations = relations
        self._reverse_relations = reverse_relations

    def __getattr__(self, name: str) -> Any:
        shadow = self._relations.get(name)
        if shadow is not None:
            return _RelationNullProxy(self._model_cls, name, shadow)
        if name in self._reverse_relations:
            raise TypeError(
                f"check predicate references the reverse relation {name!r}: an "
                "existence test answers a question about other rows, which a "
                "CHECK cannot see. A check predicate reads this table's own "
                "columns (ADR-0016)."
            )
        if name in self._columns:
            return FieldProxy(name, owner=self._model_cls)
        raise AttributeError(
            f"check predicate references unknown column {name!r}. Valid columns: "
            f"{', '.join(sorted(self._columns))}."
            + (
                f" Valid relations: {', '.join(sorted(self._relations))}."
                if self._relations
                else ""
            ),
            name=name,
            obj=self._model_cls,
        )


class _RelationNullProxy:
    """A forward-FK relation inside a check predicate: null tests only.

    ``t.<relation> == None`` desugars to the shadow ``{relation}_id`` column —
    the same join-free leaf ``RelationProxy._instance_comparison`` builds for
    ``where()``. It cannot reuse that proxy: ``RelationProxy`` resolves the
    shadow column through the model's *published* ``__ferro_relation_specs__``
    (which requires a resolved target class), while a check predicate compiles
    before publication and, for a forward-referenced FK, before resolution.
    """

    __slots__ = ("_model_cls", "_relation", "_shadow")

    def __init__(self, model_cls: type, relation: str, shadow: str) -> None:
        self._model_cls = model_cls
        self._relation = relation
        self._shadow = shadow

    def _null_comparison(self, other: object, operator: str) -> QueryNode:
        if other is not None:
            raise TypeError(
                f"check predicate compares the relation {self._relation!r} to "
                f"{other!r}: a check predicate supports only "
                f"t.{self._relation} == None / != None (equivalently "
                f"t.{self._shadow} == None). Comparing to a row would bake a "
                "primary key into the schema (ADR-0016)."
            )
        return QueryNode(
            column=self._shadow,
            operator=operator,
            value=None,
            owner=self._model_cls,
        )

    def __eq__(self, other: object) -> QueryNode:  # type: ignore[override]  # ty: ignore[invalid-method-override]
        return self._null_comparison(other, "==")

    def __ne__(self, other: object) -> QueryNode:  # type: ignore[override]  # ty: ignore[invalid-method-override]
        return self._null_comparison(other, "!=")

    __hash__ = None  # type: ignore[assignment]

    def __getattr__(self, name: str) -> Any:
        raise TypeError(
            f"check predicate traverses the relation {self._relation!r} to reach "
            f"{name!r}: relation traversal reads another table, which a CHECK "
            f"cannot do. Test the relation itself (t.{self._relation} == None) or "
            f"its shadow column (t.{self._shadow}) — richer check predicates land "
            "with #346."
        )

    def _reject_operator(self, symbol: str) -> Any:
        raise TypeError(
            f"check predicate applies {symbol} to the relation {self._relation!r}: "
            f"only == None / != None are supported (or use t.{self._shadow})."
        )

    def __lt__(self, other: object) -> Any:
        return self._reject_operator("<")

    def __le__(self, other: object) -> Any:
        return self._reject_operator("<=")

    def __gt__(self, other: object) -> Any:
        return self._reject_operator(">")

    def __ge__(self, other: object) -> Any:
        return self._reject_operator(">=")

    def in_(self, other: object) -> Any:
        return self._reject_operator("in_()")

    def like(self, other: object) -> Any:
        return self._reject_operator("like()")

    def __repr__(self) -> str:
        return f"_RelationNullProxy(relation={self._relation!r}, shadow={self._shadow!r})"


def _declared_checks(model_cls: type[Any]) -> tuple[Check, ...]:
    """Validate the declared ``__ferro_checks__`` tuple and its entries."""
    raw = getattr(model_cls, FERRO_CHECKS, ())
    if raw in ((), None):
        return ()
    if not isinstance(raw, tuple):
        raise TypeError(
            f"{model_cls.__qualname__}.{FERRO_CHECKS} must be a tuple of Check "
            f"objects, not {type(raw).__name__}"
        )
    for index, entry in enumerate(raw):
        if not isinstance(entry, Check):
            raise TypeError(
                f"{model_cls.__qualname__}.{FERRO_CHECKS}[{index}] must be a Check "
                f"object (e.g. Check('at_most_one_outflow', lambda t: ...)), not "
                f"{type(entry).__name__}"
            )
    seen: set[str] = set()
    for entry in raw:
        if entry.suffix in seen:
            raise TypeError(
                f"{model_cls.__qualname__}.{FERRO_CHECKS} declares the duplicate "
                f"table-check suffix {entry.suffix!r}; each suffix names a distinct "
                "constraint and must be unique per model."
            )
        seen.add(entry.suffix)
    return raw


def _relation_shadow_columns(
    model_cls: type[Any], columns: Mapping[str, Any]
) -> dict[str, str]:
    """Map each forward-FK relation field to its shadow ``{field}_id`` column.

    Read from the class-body-registered ``ferro_relations`` (not from resolved
    relation specs) so the mapping is complete at class definition, before
    ``resolve_relationships`` has a target class to offer.
    """
    relations: dict[str, str] = {}
    for field_name, meta in (getattr(model_cls, "ferro_relations", {}) or {}).items():
        if not isinstance(meta, ForeignKey):
            continue
        shadow = f"{field_name}_id"
        if shadow in columns:
            relations[field_name] = shadow
    return relations


def _reverse_relation_names(model_cls: type[Any]) -> frozenset[str]:
    """Declared BackRef / ManyToMany field names (rejected in a check body)."""
    return frozenset(
        field_name
        for field_name, meta in (getattr(model_cls, "ferro_relations", {}) or {}).items()
        if not isinstance(meta, ForeignKey)
    )


def _unsupported(model_name: str, suffix: str, what: str) -> "TypeError":
    """The one rejection message for predicate forms #346 will add."""
    return TypeError(
        f"{model_name}.{FERRO_CHECKS} check {suffix!r} uses {what}, which check "
        "predicates do not support yet. This release compiles NULL tests "
        "(== None / != None, on a column, a forward-FK relation, or its shadow "
        "*_id column) combined with & / | / ~ (ADR-0016). Richer predicates "
        "land with #346."
    )


def _lower_node(node: QueryNode, model_name: str, suffix: str) -> dict[str, Any]:
    """Lower one evaluated ``QueryNode`` into the ``CheckExpr`` wire shape."""
    if node.child is not None:
        return {"kind": "not", "child": _lower_node(node.child, model_name, suffix)}
    if node.exists is not None:
        raise _unsupported(model_name, suffix, "an existence test")
    if node.is_compound:
        if node.left is None or node.right is None:
            raise _unsupported(model_name, suffix, "an incomplete compound predicate")
        kind = "and" if node.operator == "AND" else "or"
        return {
            "kind": kind,
            "left": _lower_node(node.left, model_name, suffix),
            "right": _lower_node(node.right, model_name, suffix),
        }
    if node.path:
        raise _unsupported(
            model_name,
            suffix,
            f"relation traversal (t.{'.'.join((*node.path, str(node.column)))})",
        )
    kind = _NULL_TEST_KINDS.get(str(node.operator))
    if kind is None or node.value is not None:
        operator = _OPERATOR_SPELLING.get(str(node.operator), str(node.operator))
        if kind is not None:
            raise _unsupported(
                model_name,
                suffix,
                f"the literal comparison {node.column!r} {operator} {node.value!r}",
            )
        raise _unsupported(
            model_name, suffix, f"the comparison operator {operator!r}"
        )
    return {"kind": kind, "column": node.column}


def _resolve_check_node(
    model_cls: type[Any],
    model_name: str,
    check: Check,
    columns: Mapping[str, Any],
) -> QueryNode:
    """Evaluate one check's lambda against a validating proxy.

    Every failure inside the lambda — an unknown column, an unsupported
    operator on a relation, a bare relation returned — is re-raised as a
    ``TypeError`` naming the model and the check, so a model with several
    checks says which one is wrong. ``TypeError`` is also the declaration-error
    type the metaclass surfaces unwrapped.
    """
    proxy = _CheckProxy(
        model_cls,
        columns,
        _relation_shadow_columns(model_cls, columns),
        _reverse_relation_names(model_cls),
    )
    prefix = f"{model_name}.{FERRO_CHECKS} check {check.suffix!r}: "
    try:
        result = check.predicate(proxy)
    except (AttributeError, TypeError, ValueError) as exc:
        raise TypeError(f"{prefix}{exc}") from exc
    if isinstance(result, _RelationNullProxy):
        raise TypeError(
            f"{prefix}the predicate returned the bare relation "
            f"{result._relation!r}; test it with == None / != None."
        )
    if isinstance(result, FieldProxy):
        raise TypeError(
            f"{prefix}the predicate returned the bare column {result.column!r}; "
            "a check predicate is a comparison (e.g. t."
            f"{result.column} == None)."
        )
    if not isinstance(result, QueryNode):
        what = (
            "an aggregate"
            if type(result).__name__ == "AggregateExpr"
            else f"{type(result).__name__}"
        )
        raise _unsupported(model_name, check.suffix, what)
    return result


def compile_table_checks(
    model_cls: type[Any], model_name: str, columns: Mapping[str, Any]
) -> tuple[TableCheckSpec, ...]:
    """Validate and lower ``__ferro_checks__`` for one model.

    The single validate-and-lower choke point for table checks: the SchemaIR
    compiler calls it on every compile pass (class definition and the resolved
    second pass) with the column specs that pass just built, so a check can
    reference a shadow FK column without waiting for relationship resolution.

    Args:
        model_cls: The model class carrying ``__ferro_checks__``.
        model_name: Registry key / model class name, for error messages.
        columns: The compile's column specs, keyed by column name.

    Returns:
        One :class:`TableCheckSpec` per declared check, in declaration order.

    Raises:
        TypeError: For any declaration error — a malformed ``__ferro_checks__``,
            a duplicate suffix, an unknown column, or a predicate form outside
            the supported dialect.
    """
    declared = _declared_checks(model_cls)
    if not declared:
        return ()
    specs: list[TableCheckSpec] = []
    for check in declared:
        node = _resolve_check_node(model_cls, model_name, check, columns)
        specs.append(
            TableCheckSpec(
                suffix=check.suffix,
                predicate=_lower_node(node, model_name, check.suffix),
            )
        )
    return tuple(specs)
