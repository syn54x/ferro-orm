"""Define query AST nodes and field proxies for fluent filtering"""

import difflib
import uuid
from collections.abc import Callable
from decimal import Decimal
from typing import Any, Generic, TypeAlias, TypeVar

TField = TypeVar("TField")
TModel = TypeVar("TModel")


class QueryNode:
    """Represent a node in the query expression tree

    Attributes:
        column: Column name for leaf nodes.
        operator: Comparison or logical operator.
        value: Right-hand value for leaf comparisons.
        left: Left child node for compound expressions.
        right: Right child node for compound expressions.
        is_compound: Flag indicating whether the node combines two child nodes.

    Examples:
        >>> active_filter = FieldProxy("active") == True
        >>> admin_filter = FieldProxy("role") == "admin"
        >>> expr = active_filter & admin_filter
        >>> isinstance(expr, QueryNode)
        True
    """

    def __init__(
        self,
        column: str | None = None,
        operator: str | None = None,
        value: Any = None,
        left: "QueryNode | None" = None,
        right: "QueryNode | None" = None,
        is_compound: bool = False,
    ):
        """Initialize a query expression node

        Args:
            column: Column name for a leaf comparison node.
            operator: Comparison or logical operator string.
            value: Comparison value for leaf nodes.
            left: Left child node for compound expressions.
            right: Right child node for compound expressions.
            is_compound: Set to True for logical expressions with child nodes.
        """
        self.column = column
        self.operator = operator
        self.value = value
        self.left = left
        self.right = right
        self.is_compound = is_compound

    def __or__(self, other: "QueryNode") -> "QueryNode":
        """Combine two nodes with logical OR

        Args:
            other: Another query node to combine.

        Returns:
            A compound node representing ``self OR other``.

        Examples:
            >>> role = FieldProxy("role")
            >>> expr = (role == "admin") | (role == "owner")
            >>> expr.is_compound
            True
        """
        if not isinstance(other, QueryNode):
            return NotImplemented
        return QueryNode(left=self, operator="OR", right=other, is_compound=True)

    def __and__(self, other: "QueryNode") -> "QueryNode":
        """Combine two nodes with logical AND

        Args:
            other: Another query node to combine.

        Returns:
            A compound node representing ``self AND other``.

        Examples:
            >>> active = FieldProxy("active") == True
            >>> email = FieldProxy("email").like("%@ferro.dev")
            >>> expr = active & email
            >>> expr.is_compound
            True
        """
        if not isinstance(other, QueryNode):
            return NotImplemented
        return QueryNode(left=self, operator="AND", right=other, is_compound=True)

    def to_ir_dict(self) -> dict[str, Any]:
        """Serialize the query node tree into a QueryIR payload shape."""
        if not self.is_compound:
            serialized = _serialize_query_value(self.value)
            return {
                "node_kind": "leaf",
                "column": self.column,
                "operator": self.operator,
                "value": {"kind": _query_value_kind(serialized), "value": serialized},
            }
        return {
            "node_kind": "compound",
            "operator": self.operator,
            "left": self.left.to_ir_dict() if self.left else None,
            "right": self.right.to_ir_dict() if self.right else None,
        }

    def __repr__(self):
        """Return a developer-friendly representation of the node"""
        if not self.is_compound:
            return f"QueryNode(column={self.column!r}, operator={self.operator!r}, value={self.value!r})"
        return (
            f"QueryNode(left={self.left!r}, op={self.operator!r}, right={self.right!r})"
        )


def _serialize_query_value(value: Any) -> Any:
    """Normalize Python values into JSON-friendly query payloads."""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, (Decimal, uuid.UUID)):
        return str(value)
    if isinstance(value, (list, tuple, set)):
        return [_serialize_query_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _serialize_query_value(item) for key, item in value.items()}
    return value


def _query_value_kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "object"
    return "unknown"


class FieldProxy(Generic[TField]):
    """Capture field comparisons and build query nodes

    ``FieldProxy`` is generic over the column's Python type so that operator
    overloads carry that type into static analysis. At runtime the type
    parameter is erased and the proxy works identically for any column type.

    Attributes:
        column: Database column name associated with the model field.

    Examples:
        >>> email_filter = FieldProxy("email") == "taylor@example.com"
        >>> isinstance(email_filter, QueryNode)
        True
    """

    def __init__(self, column: str):
        """Initialize a field proxy for a specific column

        Args:
            column: Database column name to target in expressions.
        """
        self.column = column

    def __eq__(  # type: ignore[override]  # ty: ignore[invalid-method-override]
        self, other: "TField | FieldProxy[TField]"
    ) -> QueryNode:
        """Build an equality comparison node"""
        return QueryNode(self.column, "==", other)

    def __ne__(  # type: ignore[override]  # ty: ignore[invalid-method-override]
        self, other: "TField | FieldProxy[TField]"
    ) -> QueryNode:
        """Build an inequality comparison node"""
        return QueryNode(self.column, "!=", other)

    def __lt__(self, other: "TField | FieldProxy[TField]") -> QueryNode:
        """Build a less-than comparison node"""
        return QueryNode(self.column, "<", other)

    def __le__(self, other: "TField | FieldProxy[TField]") -> QueryNode:
        """Build a less-than-or-equal comparison node"""
        return QueryNode(self.column, "<=", other)

    def __gt__(self, other: "TField | FieldProxy[TField]") -> QueryNode:
        """Build a greater-than comparison node"""
        return QueryNode(self.column, ">", other)

    def __ge__(self, other: "TField | FieldProxy[TField]") -> QueryNode:
        """Build a greater-than-or-equal comparison node"""
        return QueryNode(self.column, ">=", other)

    def in_(
        self, other: "list[TField] | tuple[TField, ...] | set[TField]"
    ) -> QueryNode:
        """Build an ``IN`` comparison node from an iterable

        Args:
            other: Collection of values to match against the field.

        Returns:
            A node using the SQL ``IN`` operator.

        Raises:
            TypeError: If ``other`` is not a list, tuple, or set.

        Examples:
            >>> status_filter = FieldProxy("status").in_(["active", "pending"])
            >>> status_filter.operator
            'IN'
        """
        if not isinstance(other, (list, tuple, set)):
            raise TypeError(
                f"The 'in_' operator expects a list, tuple, or set, got {type(other).__name__}"
            )
        return QueryNode(self.column, "IN", list(other))

    def like(self: "FieldProxy[str]", pattern: str) -> QueryNode:
        """Build a ``LIKE`` comparison node

        The ``self: FieldProxy[str]`` annotation prevents type checkers from
        accepting ``.like(...)`` on non-string columns; at runtime the method
        is available on any ``FieldProxy``.

        Args:
            pattern: SQL LIKE pattern such as ``"%@example.com"``.

        Returns:
            A node using the SQL ``LIKE`` operator.

        Examples:
            >>> email_filter = FieldProxy("email").like("%@example.com")
            >>> email_filter.operator
            'LIKE'
        """
        return QueryNode(self.column, "LIKE", pattern)

    def __lshift__(
        self, other: "list[TField] | tuple[TField, ...] | set[TField]"
    ) -> QueryNode:
        """Use ``<<`` as shorthand syntax for ``IN`` comparisons

        Args:
            other: Collection of values to match against the field.

        Returns:
            A node using the SQL ``IN`` operator.

        Examples:
            >>> role_filter = FieldProxy("role") << {"admin", "owner"}
            >>> role_filter.operator
            'IN'
        """
        return self.in_(other)

    def __repr__(self):
        """Return a developer-friendly representation of the field proxy"""
        return f"FieldProxy(column={self.column!r})"


def validate_query_column(model_cls: type, name: str) -> str:
    """Validate a queryable column name at build time (FF-F F-2).

    Raises:
        AttributeError: If ``name`` is not a declared field or shadow
            ``{fk}_id`` column of ``model_cls``. The message names the bad
            column, suggests the closest valid one, and lists all valid
            columns.
    """
    valid = getattr(model_cls, "__ferro_query_columns__", None)
    if valid is None:
        raise TypeError(
            f"{model_cls!r} is not a registered Ferro model class; "
            "query predicates require a Ferro Model."
        )
    if name in valid:
        return name
    close = difflib.get_close_matches(name, sorted(valid), n=1)
    hint = f" Did you mean {close[0]!r}?" if close else ""
    raise AttributeError(
        f"{model_cls.__name__} has no queryable column {name!r}.{hint} "
        f"Valid columns: {', '.join(sorted(valid))}."
    )


class QueryProxy(Generic[TModel]):
    """Validating attribute proxy passed to lambda predicates (FF-F F-2).

    A fresh ``QueryProxy`` is constructed for the queried model each time a
    lambda predicate is evaluated. Attribute access validates the name
    against the model's queryable columns (declared fields plus shadow
    ``{fk}_id`` columns) and returns a :class:`FieldProxy` — so
    ``lambda user: user.archived == False`` builds a :class:`QueryNode`,
    while ``lambda user: user.archievd == False`` raises ``AttributeError``
    at build time naming the valid columns.

    Attribute types are ``FieldProxy[Any]``: per-field static types for bare
    lambda parameters require TypeScript-style mapped types, proposed for
    Python in PEP 827 (draft, targeting 3.16) — adopted here when type
    checkers support it.

    Examples:
        >>> rows = await User.where(lambda user: user.archived == False).all()  # noqa: E712
    """

    __slots__ = ("_model_cls",)

    def __init__(self, model_cls: type) -> None:
        self._model_cls = model_cls

    def __getattr__(self, name: str) -> "FieldProxy[Any]":
        """Validate ``name`` and return a ``FieldProxy`` for it."""
        validate_query_column(self._model_cls, name)
        return FieldProxy(name)


Predicate: TypeAlias = Callable[[QueryProxy[TModel]], QueryNode]
"""Type alias for lambda predicates accepted by :meth:`Query.where`."""
