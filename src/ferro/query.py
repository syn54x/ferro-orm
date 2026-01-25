import json
import uuid
from decimal import Decimal
from typing import Any

from ._core import fetch_filtered, delete_filtered, count_filtered, update_filtered


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


class Query:
    """
    A fluent query builder that collects conditions and parameters
    to be executed by the Rust engine.
    """

    def __init__(self, model_cls: type):
        self.model_cls = model_cls
        self.where_clause: list[QueryNode] = []
        self.order_by_clause: list[dict[str, str]] = []
        self._limit: int | None = None
        self._offset: int | None = None

    def where(self, node: QueryNode) -> "Query":
        self.where_clause.append(node)
        return self

    def order_by(self, field: Any, direction: str = "asc") -> "Query":
        """
        Add an ordering requirement to the query.
        
        Args:
            field: The field to sort by (e.g., User.name).
            direction: 'asc' or 'desc'.
        """
        if direction.lower() not in ("asc", "desc"):
            raise ValueError("direction must be 'asc' or 'desc'")
        
        col_name = field.column if hasattr(field, "column") else str(field)
        self.order_by_clause.append({"column": col_name, "direction": direction.lower()})
        return self

    def limit(self, value: int) -> "Query":
        self._limit = value
        return self

    def offset(self, value: int) -> "Query":
        self._offset = value
        return self

    async def all(self) -> list[Any]:
        """
        Execute the query and return hydrated model instances.
        """
        # Prepare the query definition for Rust
        query_def = {
            "model_name": self.model_cls.__name__,
            "where_clause": [node.to_dict() for node in self.where_clause],
            "order_by": self.order_by_clause,
            "limit": self._limit,
            "offset": self._offset,
        }

        # Serialized query AST is passed to the thin Rust bridge
        from .models import _CURRENT_TRANSACTION
        tx_id = _CURRENT_TRANSACTION.get()
        results = await fetch_filtered(self.model_cls, json.dumps(query_def), tx_id)
        for instance in results:
            if hasattr(self.model_cls, "_fix_types"):
                self.model_cls._fix_types(instance)
        return results

    async def count(self) -> int:
        """
        Return the number of records matching the query.
        """
        query_def = {
            "model_name": self.model_cls.__name__,
            "where_clause": [node.to_dict() for node in self.where_clause],
            # count doesn't need limit/offset/order_by usually
        }
        from .models import _CURRENT_TRANSACTION
        tx_id = _CURRENT_TRANSACTION.get()
        return await count_filtered(self.model_cls.__name__, json.dumps(query_def), tx_id)

    async def update(self, **kwargs) -> int:
        """
        Update all records matching the query with the provided values.
        
        Args:
            **kwargs: Field names and their new values.
            
        Returns:
            int: The number of records updated.
        """
        query_def = {
            "model_name": self.model_cls.__name__,
            "where_clause": [node.to_dict() for node in self.where_clause],
            "limit": self._limit,
            "offset": self._offset,
        }
        from .models import _CURRENT_TRANSACTION
        tx_id = _CURRENT_TRANSACTION.get()
        return await update_filtered(self.model_cls.__name__, json.dumps(query_def), json.dumps(kwargs), tx_id)

    async def first(self) -> Any | None:
        """
        Execute the query and return the first hydrated model instance, or None.
        """
        # Save original limit to restore it later if needed (though Query is usually short-lived)
        old_limit = self._limit
        self._limit = 1
        try:
            results = await self.all()
            return results[0] if results else None
        finally:
            self._limit = old_limit

    async def delete(self) -> int:
        """
        Delete all records matching the query.
        
        Returns:
            int: The number of records deleted.
        """
        query_def = {
            "model_name": self.model_cls.__name__,
            "where_clause": [node.to_dict() for node in self.where_clause],
            "limit": self._limit,
            "offset": self._offset,
        }
        from .models import _CURRENT_TRANSACTION
        tx_id = _CURRENT_TRANSACTION.get()
        return await delete_filtered(self.model_cls.__name__, json.dumps(query_def), tx_id)

    async def exists(self) -> bool:
        """
        Return True if any records match the query.
        """
        return await self.count() > 0

    def __repr__(self):
        return f"<Query model={self.model_cls.__name__} where={self.where_clause}>"


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
