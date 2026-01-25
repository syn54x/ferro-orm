import uuid
from decimal import Decimal
from typing import Any

class QueryNode:
    """
    Represents a node in the query expression AST.
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
        self.column = column
        self.operator = operator
        self.value = value
        self.left = left
        self.right = right
        self.is_compound = is_compound

    def __or__(self, other: "QueryNode") -> "QueryNode":
        if not isinstance(other, QueryNode):
            return NotImplemented
        return QueryNode(left=self, operator="OR", right=other, is_compound=True)

    def __and__(self, other: "QueryNode") -> "QueryNode":
        if not isinstance(other, QueryNode):
            return NotImplemented
        return QueryNode(left=self, operator="AND", right=other, is_compound=True)

    def to_dict(self) -> dict[str, Any]:
        """Recursive serialization to dict for JSON conversion."""
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
        if not self.is_compound:
            return f"QueryNode(column={self.column!r}, operator={self.operator!r}, value={self.value!r})"
        return f"QueryNode(left={self.left!r}, op={self.operator!r}, right={self.right!r})"

class FieldProxy:
    """
    A proxy for a model field that captures operators to build a QueryNode.
    """
    def __init__(self, column: str):
        self.column = column

    def __eq__(self, other: Any) -> QueryNode:
        return QueryNode(self.column, "==", other)

    def __ne__(self, other: Any) -> QueryNode:
        return QueryNode(self.column, "!=", other)

    def __lt__(self, other: Any) -> QueryNode:
        return QueryNode(self.column, "<", other)

    def __le__(self, other: Any) -> QueryNode:
        return QueryNode(self.column, "<=", other)

    def __gt__(self, other: Any) -> QueryNode:
        return QueryNode(self.column, ">", other)

    def __ge__(self, other: Any) -> QueryNode:
        return QueryNode(self.column, ">=", other)

    def in_(self, other: Any) -> QueryNode:
        """Helper for IN operator: Field.in_([1, 2, 3])"""
        if not isinstance(other, (list, tuple, set)):
            raise TypeError(
                f"The 'in_' operator expects a list, tuple, or set, got {type(other).__name__}"
            )
        return QueryNode(self.column, "IN", list(other))

    def like(self, pattern: str) -> QueryNode:
        """Helper for LIKE operator."""
        return QueryNode(self.column, "LIKE", pattern)

    def __lshift__(self, other: Any) -> QueryNode:
        """Shorthand for IN operator: Field << [1, 2, 3]"""
        return self.in_(other)

    def __repr__(self):
        return f"FieldProxy(column={self.column!r})"
