"""Build fluent query objects that serialize filter definitions for the Rust core"""

import json
from typing import TYPE_CHECKING, Any, Generic, Type, TypeVar, overload

from .._core import (
    add_m2m_links,
    clear_m2m_links,
    count_filtered,
    delete_filtered,
    fetch_filtered,
    remove_m2m_links,
    update_filtered,
)
from .nodes import _serialize_query_value

if TYPE_CHECKING:
    from .nodes import QueryNode

T = TypeVar("T")
E = TypeVar("E")


def _query_def_to_json(query_def: dict[str, Any]) -> str:
    """Serialize query definitions while preserving typed values in live Query state."""
    return json.dumps(_serialize_query_value(query_def))


class Query(Generic[T]):
    """Build and execute fluent ORM queries.

    Attributes:
        model_cls: Model class used to hydrate results.
        where_clause: Accumulated filter nodes for the query.
        order_by_clause: Sort definitions sent to the Rust core.
    """

    def __init__(self, model_cls: Type[T]):
        """Initialize a query for a model class.

        Args:
            model_cls: Model class that defines the target table.

        Examples:
            >>> query = Query(User)
            >>> query.model_cls is User
            True
        """
        self.model_cls = model_cls
        self.where_clause: list["QueryNode"] = []
        self.order_by_clause: list[dict[str, str]] = []
        self._limit: int | None = None
        self._offset: int | None = None
        self._m2m_context: dict[str, Any] | None = None

    def _m2m(
        self, join_table: str, source_col: str, target_col: str, source_id: Any
    ) -> "Query[T]":
        """Store many-to-many linkage context for relationship operations"""
        self._m2m_context = {
            "join_table": join_table,
            "source_col": source_col,
            "target_col": target_col,
            "source_id": source_id,
        }
        return self

    def where(self, node: "QueryNode") -> "Query[T]":
        """Add a filter condition to the query

        Args:
            node: A QueryNode representing the condition (e.g., User.id == 1).

        Returns:
            The current Query instance for chaining.

        Examples:
            >>> query = User.where(User.id == 1)
            >>> isinstance(query, Query)
            True
        """
        self.where_clause.append(node)
        return self

    def order_by(self, field: Any, direction: str = "asc") -> "Query[T]":
        """Add an ordering clause to the query

        Args:
            field: The field to order by (e.g., User.username).
            direction: The direction of the sort ("asc" or "desc").

        Returns:
            The current Query instance for chaining.

        Raises:
            ValueError: If direction is not "asc" or "desc".

        Examples:
            >>> query = User.select().order_by(User.username, "desc")
            >>> query.order_by_clause[-1]["direction"]
            'desc'
        """
        if direction.lower() not in ("asc", "desc"):
            raise ValueError("direction must be 'asc' or 'desc'")

        col_name = field.column if hasattr(field, "column") else str(field)
        self.order_by_clause.append(
            {"column": col_name, "direction": direction.lower()}
        )
        return self

    def limit(self, value: int) -> "Query[T]":
        """Limit the number of records returned

        Args:
            value: The maximum number of records to return.

        Returns:
            The current Query instance for chaining.

        Examples:
            >>> query = User.select().limit(10)
            >>> query._limit
            10
        """
        self._limit = value
        return self

    def offset(self, value: int) -> "Query[T]":
        """Skip a specific number of records

        Args:
            value: The number of records to skip.

        Returns:
            The current Query instance for chaining.

        Examples:
            >>> query = User.select().offset(20)
            >>> query._offset
            20
        """
        self._offset = value
        return self

    async def all(self) -> list[T]:
        """Return all model instances that match the current query

        Returns:
            A list of model instances.

        Examples:
            >>> users = await User.where(User.active == True).all()
            >>> isinstance(users, list)
            True
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
        results = await fetch_filtered(
            self.model_cls, _query_def_to_json(query_def), tx_id
        )
        for instance in results:
            if hasattr(self.model_cls, "_fix_types"):
                self.model_cls._fix_types(instance)
        return results

    async def count(self) -> int:
        """Return the number of records that match the current query

        Returns:
            The count of matching records.

        Examples:
            >>> total = await User.where(User.active == True).count()
            >>> isinstance(total, int)
            True
        """
        query_def = {
            "model_name": self.model_cls.__name__,
            "where_clause": [node.to_dict() for node in self.where_clause],
            "m2m": self._m2m_context,
        }
        from ..state import _CURRENT_TRANSACTION

        tx_id = _CURRENT_TRANSACTION.get()
        return await count_filtered(
            self.model_cls.__name__, _query_def_to_json(query_def), tx_id
        )

    async def update(self, **fields) -> int:
        """Update all records matching the current query

        Args:
            **fields: Field names and values to update.

        Returns:
            The number of records updated.

        Examples:
            >>> updated = await User.where(User.id == 1).update(name="Taylor")
            >>> isinstance(updated, int)
            True
        """
        query_def = {
            "model_name": self.model_cls.__name__,
            "where_clause": [node.to_dict() for node in self.where_clause],
            "limit": self._limit,
            "offset": self._offset,
        }
        from pydantic_core import to_json

        from ..state import _CURRENT_TRANSACTION

        tx_id = _CURRENT_TRANSACTION.get()
        # Use pydantic_core.to_json to handle Decimals, UUIDs, etc. in kwargs
        return await update_filtered(
            self.model_cls.__name__,
            _query_def_to_json(query_def),
            to_json(fields).decode(),
            tx_id,
        )

    async def first(self) -> T | None:
        """Return the first matching record, or None

        Returns:
            A model instance or None.

        Examples:
            >>> user = await User.select().order_by(User.id).first()
            >>> user is None or isinstance(user, User)
            True
        """
        old_limit = self._limit
        self._limit = 1
        try:
            results = await self.all()
            return results[0] if results else None
        finally:
            self._limit = old_limit

    async def delete(self) -> int:
        """Delete all records matching the current query

        Returns:
            The number of records deleted.

        Examples:
            >>> deleted = await User.where(User.disabled == True).delete()
            >>> isinstance(deleted, int)
            True
        """
        query_def = {
            "model_name": self.model_cls.__name__,
            "where_clause": [node.to_dict() for node in self.where_clause],
            "limit": self._limit,
            "offset": self._offset,
        }
        from ..state import _CURRENT_TRANSACTION

        tx_id = _CURRENT_TRANSACTION.get()
        return await delete_filtered(
            self.model_cls.__name__, _query_def_to_json(query_def), tx_id
        )

    async def exists(self) -> bool:
        """Return whether at least one record matches the current query

        Returns:
            True if records exist, otherwise False.

        Examples:
            >>> found = await User.where(User.email == "a@b.com").exists()
            >>> isinstance(found, bool)
            True
        """
        return await self.count() > 0

    async def add(self, *instances: Any) -> None:
        """Add links to a many-to-many relationship

        Args:
            *instances: Target model instances that provide an ``id`` attribute.

        Raises:
            RuntimeError: If the query is not bound to a many-to-many context.

        Examples:
            >>> user = await User.create(email="taylor@example.com")
            >>> admin = await Group.create(name="admin")
            >>> staff = await Group.create(name="staff")
            >>> await user.groups.add(admin, staff)
        """
        if not self._m2m_context:
            raise RuntimeError(
                "'.add()' can only be used on Many-to-Many relationships"
            )

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
        """Remove links from a many-to-many relationship

        Args:
            *instances: Target model instances that provide an ``id`` attribute.

        Raises:
            RuntimeError: If the query is not bound to a many-to-many context.

        Examples:
            >>> user = await User.create(email="taylor@example.com")
            >>> admin = await Group.create(name="admin")
            >>> await user.groups.remove(admin)
        """
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
        """Clear all links in a many-to-many relationship

        Raises:
            RuntimeError: If the query is not bound to a many-to-many context.

        Examples:
            >>> user = await User.create(email="taylor@example.com")
            >>> await user.groups.clear()
        """
        if not self._m2m_context:
            raise RuntimeError(
                "'.clear()' can only be used on Many-to-Many relationships"
            )

        from ..state import _CURRENT_TRANSACTION

        tx_id = _CURRENT_TRANSACTION.get()
        await clear_m2m_links(
            self._m2m_context["join_table"],
            self._m2m_context["source_col"],
            self._m2m_context["source_id"],
            tx_id,
        )

    def __repr__(self):
        """Return a developer-friendly representation of the query"""
        return f"<Query model={self.model_cls.__name__} where={self.where_clause}>"


class Relation(Query[T]):
    """Represent lazy collection relationship queries with typing support

    Examples:
        >>> class User(Model):
        ...     id: Annotated[int, FerroField(primary_key=True)]
        ...     name: str
        ...     posts: Relation[list["Post"]] = BackRef()

        >>> class Post(Model):
        ...     id: Annotated[int, FerroField(primary_key=True)]
        ...     title: str
        ...     user: Annotated[User, ForeignKey(related_name="posts")]

        >>> user = await User.get(1)
        >>> posts = await user.posts.all()
        >>> isinstance(posts, list)
        True
    """

    def _m2m(
        self, join_table: str, source_col: str, target_col: str, source_id: Any
    ) -> "Relation[T]":
        super()._m2m(join_table, source_col, target_col, source_id)
        return self

    def where(self, node: "QueryNode") -> "Relation[T]":
        super().where(node)
        return self

    def order_by(self, field: Any, direction: str = "asc") -> "Relation[T]":
        super().order_by(field, direction)
        return self

    def limit(self, value: int) -> "Relation[T]":
        super().limit(value)
        return self

    def offset(self, value: int) -> "Relation[T]":
        super().offset(value)
        return self

    # NOTE ON TYPING:
    #
    # Users annotate collection relationships as Relation[list[Model]] to encode
    # cardinality (one-to-many / many-to-many). Since Query.all() is typed as list[T],
    # that would naively become list[list[Model]] in IDEs.
    #
    # We fix hinting by overriding Relation.{all,first} with overloads that interpret
    # Relation[T] as a query whose *rows* are model instances, regardless of whether
    # T is written as Model or list[Model] in the field annotation.
    if TYPE_CHECKING:

        @overload
        async def all(self: "Relation[list[E]]") -> list[E]: ...

        @overload
        async def all(self: "Relation[E]") -> list[E]: ...

        @overload
        async def first(self: "Relation[list[E]]") -> E | None: ...

        @overload
        async def first(self: "Relation[E]") -> E | None: ...

    async def all(self):  # type: ignore[override]
        return await super().all()

    async def first(self):  # type: ignore[override]
        return await super().first()

    @classmethod
    def __get_pydantic_core_schema__(cls, _source_type, _handler):
        """Allow pydantic-core to treat relationships as arbitrary runtime values"""
        from pydantic_core import core_schema

        return core_schema.any_schema()
