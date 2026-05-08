"""Define query AST nodes and field proxies for fluent filtering"""

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
        >>> active_filter = User.active == True
        >>> admin_filter = User.role == "admin"
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
            >>> expr = (User.role == "admin") | (User.role == "owner")
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
            >>> expr = (User.active == True) & (User.email.like("%@ferro.dev"))
            >>> expr.is_compound
            True
        """
        if not isinstance(other, QueryNode):
            return NotImplemented
        return QueryNode(left=self, operator="AND", right=other, is_compound=True)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the query node tree into a JSON-friendly dictionary

        Returns:
            A dictionary representation of the current node and its children.

        Examples:
            >>> expr = (User.active == True) & (User.id > 10)
            >>> payload = expr.to_dict()
            >>> payload["is_compound"]
            True
        """
        if not self.is_compound:
            return {
                "column": self.column,
                "operator": self.operator,
                "value": _serialize_query_value(self.value),
                "is_compound": False,
            }
        return {
            "left": self.left.to_dict() if self.left else None,
            "operator": self.operator,
            "right": self.right.to_dict() if self.right else None,
            "is_compound": True,
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


class FieldProxy(Generic[TField]):
    """Capture field comparisons and build query nodes

    ``FieldProxy`` is generic over the column's Python type so that operator
    overloads carry that type into static analysis. At runtime the type
    parameter is erased and the proxy works identically for any column type.

    Attributes:
        column: Database column name associated with the model field.

    Examples:
        >>> email_filter = User.email == "taylor@example.com"
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
            >>> status_filter = User.status.in_(["active", "pending"])
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
            >>> email_filter = User.email.like("%@example.com")
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
            >>> role_filter = User.role << {"admin", "owner"}
            >>> role_filter.operator
            'IN'
        """
        return self.in_(other)

    def __repr__(self):
        """Return a developer-friendly representation of the field proxy"""
        return f"FieldProxy(column={self.column!r})"


def col(value: TField) -> "FieldProxy[TField]":
    """Treat a model class attribute as a typed query column.

    At runtime Ferro's metaclass replaces ``Model.field`` with a
    :class:`FieldProxy`, so ``Model.field`` is already a ``FieldProxy`` when
    accessed on the class. Static type checkers, however, see the field's
    Pydantic-annotated type (``bool``, ``int``, ...). That makes expressions
    like ``Model.archived == False`` resolve to ``bool`` statically, even
    though the runtime value is a ``QueryNode``.

    ``col()`` is runtime-identity for ``FieldProxy`` inputs and statically
    narrows the return type to ``FieldProxy[T]``, so ``col(Model.archived) ==
    False`` type-checks as ``QueryNode``. Use it when a single attribute
    trips your type checker; for new code, prefer the lambda predicate API
    on :meth:`Query.where`.

    Args:
        value: A model class attribute (already a ``FieldProxy`` at runtime).

    Returns:
        The same object, statically typed as ``FieldProxy[T]``.

    Raises:
        TypeError: If ``value`` is not a ``FieldProxy``. This guards against
            calling ``col()`` on a literal (e.g., ``col(False)``), which is
            almost certainly a bug.

    Examples:
        >>> rows = await User.where(col(User.archived) == False).all()  # noqa: E712
    """
    if not isinstance(value, FieldProxy):
        raise TypeError(
            f"col() expects a model column reference (FieldProxy), got {type(value).__name__}"
        )
    return value  # type: ignore[return-value]


class QueryProxy(Generic[TModel]):
    """Lazy attribute proxy used by lambda predicates passed to ``Query.where``.

    A fresh ``QueryProxy`` is constructed each time a lambda predicate is
    evaluated. Any attribute access returns a :class:`FieldProxy` for the
    accessed name, so ``lambda t: t.archived == False`` builds a
    :class:`QueryNode` without ever asking the model class what type
    ``archived`` is. The ``TModel`` type parameter exists so user-supplied
    lambdas can narrow ``t`` to a specific model in static analysis; the
    proxy itself ignores the parameter at runtime.

    The proxy attribute return type is intentionally ``FieldProxy[Any]`` for
    now — wiring per-field types through a lambda parameter requires
    ``@dataclass_transform`` plumbing on the metaclass, which is outside this
    feature's scope.

    Examples:
        >>> rows = await User.where(lambda t: t.archived == False).all()  # noqa: E712
    """

    __slots__ = ()

    def __getattr__(self, name: str) -> "FieldProxy[Any]":
        """Return a fresh ``FieldProxy`` for any attribute name."""
        return FieldProxy(name)


Predicate: TypeAlias = Callable[[QueryProxy[TModel]], QueryNode]
"""Type alias for lambda predicates accepted by :meth:`Query.where`."""
