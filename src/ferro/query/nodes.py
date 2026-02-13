"""Define query AST nodes and field proxies for fluent filtering"""

import uuid
from decimal import Decimal
from typing import Any


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
            val = self.value
            if hasattr(val, "isoformat"):
                val = val.isoformat()
            elif isinstance(val, (Decimal, uuid.UUID)):
                val = str(val)

            return {
                "column": self.column,
                "operator": self.operator,
                "value": val,
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


class FieldProxy:
    """Capture field comparisons and build query nodes

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

    def __eq__(self, other: Any) -> QueryNode:
        """Build an equality comparison node"""
        return QueryNode(self.column, "==", other)

    def __ne__(self, other: Any) -> QueryNode:
        """Build an inequality comparison node"""
        return QueryNode(self.column, "!=", other)

    def __lt__(self, other: Any) -> QueryNode:
        """Build a less-than comparison node"""
        return QueryNode(self.column, "<", other)

    def __le__(self, other: Any) -> QueryNode:
        """Build a less-than-or-equal comparison node"""
        return QueryNode(self.column, "<=", other)

    def __gt__(self, other: Any) -> QueryNode:
        """Build a greater-than comparison node"""
        return QueryNode(self.column, ">", other)

    def __ge__(self, other: Any) -> QueryNode:
        """Build a greater-than-or-equal comparison node"""
        return QueryNode(self.column, ">=", other)

    def in_(self, other: Any) -> QueryNode:
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

    def like(self, pattern: str) -> QueryNode:
        """Build a ``LIKE`` comparison node

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

    def __lshift__(self, other: Any) -> QueryNode:
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
