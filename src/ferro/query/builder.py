import json
from typing import TYPE_CHECKING, Any, Generic, Type, TypeVar

from .._core import (
    add_m2m_links,
    clear_m2m_links,
    count_filtered,
    delete_filtered,
    fetch_filtered,
    remove_m2m_links,
    update_filtered,
)

if TYPE_CHECKING:
    from .nodes import QueryNode

T = TypeVar("T")


class Query(Generic[T]):
    """
    A fluent query builder that collects conditions and parameters
    to be executed by the Rust engine.
    """

    def __init__(self, model_cls: Type[T]):
        self.model_cls = model_cls
        self.where_clause: list["QueryNode"] = []
        self.order_by_clause: list[dict[str, str]] = []
        self._limit: int | None = None
        self._offset: int | None = None
        self._m2m_context: dict[str, Any] | None = None

    def _m2m(
        self, join_table: str, source_col: str, target_col: str, source_id: Any
    ) -> "Query[T]":
        """Internal helper to set Many-to-Many context."""
        self._m2m_context = {
            "join_table": join_table,
            "source_col": source_col,
            "target_col": target_col,
            "source_id": source_id,
        }
        return self

    def where(self, node: "QueryNode") -> "Query[T]":
        self.where_clause.append(node)
        return self

    def order_by(self, field: Any, direction: str = "asc") -> "Query[T]":
        if direction.lower() not in ("asc", "desc"):
            raise ValueError("direction must be 'asc' or 'desc'")

        col_name = field.column if hasattr(field, "column") else str(field)
        self.order_by_clause.append(
            {"column": col_name, "direction": direction.lower()}
        )
        return self

    def limit(self, value: int) -> "Query[T]":
        self._limit = value
        return self

    def offset(self, value: int) -> "Query[T]":
        self._offset = value
        return self

    async def all(self) -> list[T]:
        """
        Execute the query and return hydrated model instances.
        """
        query_def = {
            "model_name": self.model_cls.__name__,
            "where_clause": [node.to_dict() for node in self.where_clause],
            "order_by": self.order_by_clause,
            "limit": self._limit,
            "offset": self._offset,
            "m2m": self._m2m_context,
        }
        from ..state import _CURRENT_TRANSACTION

        tx_id = _CURRENT_TRANSACTION.get()
        results = await fetch_filtered(self.model_cls, json.dumps(query_def), tx_id)
        for instance in results:
            if hasattr(self.model_cls, "_fix_types"):
                self.model_cls._fix_types(instance)
        return results

    async def count(self) -> int:
        query_def = {
            "model_name": self.model_cls.__name__,
            "where_clause": [node.to_dict() for node in self.where_clause],
            "m2m": self._m2m_context,
        }
        from ..state import _CURRENT_TRANSACTION

        tx_id = _CURRENT_TRANSACTION.get()
        return await count_filtered(
            self.model_cls.__name__, json.dumps(query_def), tx_id
        )

    async def update(self, **kwargs) -> int:
        query_def = {
            "model_name": self.model_cls.__name__,
            "where_clause": [node.to_dict() for node in self.where_clause],
            "limit": self._limit,
            "offset": self._offset,
        }
        from ..state import _CURRENT_TRANSACTION

        tx_id = _CURRENT_TRANSACTION.get()
        return await update_filtered(
            self.model_cls.__name__, json.dumps(query_def), json.dumps(kwargs), tx_id
        )

    async def first(self) -> T | None:
        """
        Execute the query and return the first hydrated model instance, or None.
        """
        old_limit = self._limit
        self._limit = 1
        try:
            results = await self.all()
            return results[0] if results else None
        finally:
            self._limit = old_limit

    async def delete(self) -> int:
        query_def = {
            "model_name": self.model_cls.__name__,
            "where_clause": [node.to_dict() for node in self.where_clause],
            "limit": self._limit,
            "offset": self._offset,
        }
        from ..state import _CURRENT_TRANSACTION

        tx_id = _CURRENT_TRANSACTION.get()
        return await delete_filtered(
            self.model_cls.__name__, json.dumps(query_def), tx_id
        )

    async def exists(self) -> bool:
        return await self.count() > 0

    async def add(self, *instances: Any) -> None:
        """Add links to a Many-to-Many relationship."""
        if not self._m2m_context:
            raise RuntimeError("'.add()' can only be used on Many-to-Many relationships")

        ids = []
        for inst in instances:
            # Assume 'id' for now
            ids.append(getattr(inst, "id"))

        from ..state import _CURRENT_TRANSACTION

        tx_id = _CURRENT_TRANSACTION.get()
        await add_m2m_links(
            self._m2m_context["join_table"],
            self._m2m_context["source_col"],
            self._m2m_context["target_col"],
            self._m2m_context["source_id"],
            ids,
            tx_id,
        )

    async def remove(self, *instances: Any) -> None:
        """Remove links from a Many-to-Many relationship."""
        if not self._m2m_context:
            raise RuntimeError(
                "'.remove()' can only be used on Many-to-Many relationships"
            )

        ids = []
        for inst in instances:
            ids.append(getattr(inst, "id"))

        from ..state import _CURRENT_TRANSACTION

        tx_id = _CURRENT_TRANSACTION.get()
        await remove_m2m_links(
            self._m2m_context["join_table"],
            self._m2m_context["source_col"],
            self._m2m_context["target_col"],
            self._m2m_context["source_id"],
            ids,
            tx_id,
        )

    async def clear(self) -> None:
        """Clear all links in a Many-to-Many relationship."""
        if not self._m2m_context:
            raise RuntimeError("'.clear()' can only be used on Many-to-Many relationships")

        from ..state import _CURRENT_TRANSACTION

        tx_id = _CURRENT_TRANSACTION.get()
        await clear_m2m_links(
            self._m2m_context["join_table"],
            self._m2m_context["source_col"],
            self._m2m_context["source_id"],
            tx_id,
        )

    def __repr__(self):
        return f"<Query model={self.model_cls.__name__} where={self.where_clause}>"


class BackRelationship(Query[T]):
    """
    Marker for a reverse relationship query that provides full Query intellisense.
    """

    @classmethod
    def __get_pydantic_core_schema__(cls, _source_type, _handler):
        from pydantic_core import core_schema

        return core_schema.any_schema()
