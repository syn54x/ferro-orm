"""Build fluent query objects that serialize QueryIR payloads for the Rust core."""

import warnings
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
from .nodes import QueryNode, QueryProxy, _serialize_query_value

if TYPE_CHECKING:
    from .nodes import Predicate

T = TypeVar("T")
E = TypeVar("E")


try:
    from warnings import deprecated as _warnings_deprecated
except ImportError:

    def _warnings_deprecated(message: str, **_: Any):
        def _decorate(func):
            def _wrapped(*args, **kwargs):
                warnings.warn(message, DeprecationWarning, stacklevel=3)
                return func(*args, **kwargs)

            return _wrapped

        return _decorate


def _query_ir_payload_to_json(query_payload: dict[str, Any]) -> str:
    """Serialize a QueryIR payload into a versioned IR envelope JSON string."""
    import json

    return json.dumps(
        {
            "ir_kind": "query",
            "ir_version": 1,
            "payload": _serialize_query_value(query_payload),
        }
    )


@_warnings_deprecated(
    "Operator predicate style (Model.field OP value) is deprecated; use lambda "
    "predicates (`where(lambda t: ...)`) or col(Model.field) instead. Planned "
    "removal: v0.13.0."
)
def _deprecated_operator_query_node(node: QueryNode) -> QueryNode:
    return node


def _resolve_where_node(node: Any) -> QueryNode:
    """Normalize a ``where`` argument into a ``QueryNode``.

    Accepts an existing ``QueryNode`` directly (the operator and ``col()``
    paths) or a predicate callable that takes a ``QueryProxy`` and returns
    a ``QueryNode`` (the lambda path).
    """
    if isinstance(node, QueryNode):
        if node.uses_operator_style():
            return _deprecated_operator_query_node(node)
        return node
    if callable(node):
        result = node(QueryProxy())
        if not isinstance(result, QueryNode):
            raise TypeError(
                "where() predicate callable must return QueryNode, "
                f"got {type(result).__name__}"
            )
        return result
    raise TypeError(
        "where() expected QueryNode or predicate callable, "
        f"got {type(node).__name__}"
    )


class Query(Generic[T]):
    """Build and execute fluent ORM queries.

    Attributes:
        model_cls: Model class used to hydrate results.
        where_clause: Accumulated filter nodes for the query.
        order_by_clause: Sort definitions sent to the Rust core.
    """

    def __init__(self, model_cls: Type[T], using: str | None = None):
        """Initialize a query for a model class.

        Args:
            model_cls: Model class that defines the target table.

        Examples:
            >>> query = Query(User)
            >>> query.model_cls is User
            True
        """
        self.model_cls = model_cls
        self._using = using
        self.where_clause: list["QueryNode"] = []
        self.order_by_clause: list[dict[str, str]] = []
        self._limit: int | None = None
        self._offset: int | None = None
        self._m2m_context: dict[str, Any] | None = None

    def _transaction_or_using(self) -> tuple[str | None, str | None]:
        from ..state import _CURRENT_TRANSACTION, _CURRENT_TRANSACTION_CONNECTION

        tx_id = _CURRENT_TRANSACTION.get()
        if tx_id is not None and self._using is not None:
            if self._using == _CURRENT_TRANSACTION_CONNECTION.get():
                return tx_id, None
            raise ValueError(
                "ORM queries inside a transaction inherit the transaction connection"
            )
        return tx_id, self._using

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

    @overload
    def where(self, node: "QueryNode") -> "Query[T]": ...

    @overload
    def where(self, node: "Predicate[T]") -> "Query[T]": ...

    def where(self, node: "QueryNode | Predicate[T]") -> "Query[T]":
        """Add a filter condition to the query.

        The recommended style is a lambda predicate of shape
        ``Callable[[QueryProxy[T]], QueryNode]``. The lambda receives a
        fresh :class:`QueryProxy` whose attributes return
        :class:`FieldProxy` instances, so ``lambda t: t.archived == False``
        builds a comparison without static-typing friction. A prebuilt
        :class:`QueryNode` is also accepted, built either with
        :func:`ferro.query.col` (the type-safe escape hatch that preserves
        operator shape) or with operator syntax on class attributes. The
        bare operator form (``User.where(User.age >= 18)``) is deprecated and
        on the v0.13.0 removal track. It does not
        type-check statically:
        the class attribute types as the field type, so the comparison
        resolves to ``bool``, not ``QueryNode``.

        Args:
            node: A predicate callable or a ``QueryNode``.

        Returns:
            The current Query instance for chaining.

        Raises:
            TypeError: If ``node`` is neither a ``QueryNode`` nor a callable,
                or if the callable does not return a ``QueryNode``.

        Examples:
            >>> q1 = User.where(lambda t: t.archived == False)  # noqa: E712
            >>> q2 = User.where(lambda t: t.id == 1)
            >>> isinstance(q1, Query) and isinstance(q2, Query)
            True
        """
        self.where_clause.append(_resolve_where_node(node))
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
            >>> users = await User.where(lambda t: t.active == True).all()  # noqa: E712
            >>> isinstance(users, list)
            True
        """
        query_def = {
            "model_name": self.model_cls.__name__,
            "where": [node.to_ir_dict() for node in self.where_clause],
            "order_by": self.order_by_clause,
            "limit": self._limit,
            "offset": self._offset,
            "m2m": self._m2m_context,
        }
        tx_id, using = self._transaction_or_using()
        results = await fetch_filtered(
            self.model_cls, _query_ir_payload_to_json(query_def), tx_id, using
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
            >>> total = await User.where(lambda t: t.active == True).count()  # noqa: E712
            >>> isinstance(total, int)
            True
        """
        query_def = {
            "model_name": self.model_cls.__name__,
            "where": [node.to_ir_dict() for node in self.where_clause],
            "order_by": [],
            "limit": None,
            "offset": None,
            "m2m": self._m2m_context,
        }
        tx_id, using = self._transaction_or_using()
        return await count_filtered(
            self.model_cls.__name__, _query_ir_payload_to_json(query_def), tx_id, using
        )

    async def update(self, **fields) -> int:
        """Update all records matching the current query

        Args:
            **fields: Field names and values to update.

        Returns:
            The number of records updated.

        Examples:
            >>> updated = await User.where(lambda t: t.id == 1).update(name="Taylor")
            >>> isinstance(updated, int)
            True
        """
        query_def = {
            "model_name": self.model_cls.__name__,
            "where": [node.to_ir_dict() for node in self.where_clause],
            "order_by": [],
            "limit": self._limit,
            "offset": self._offset,
            "m2m": None,
        }
        from pydantic_core import to_json

        tx_id, using = self._transaction_or_using()
        # Use pydantic_core.to_json to handle Decimals, UUIDs, etc. in kwargs
        return await update_filtered(
            self.model_cls.__name__,
            _query_ir_payload_to_json(query_def),
            to_json(fields).decode(),
            tx_id,
            using,
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
            >>> deleted = await User.where(lambda t: t.disabled == True).delete()  # noqa: E712
            >>> isinstance(deleted, int)
            True
        """
        query_def = {
            "model_name": self.model_cls.__name__,
            "where": [node.to_ir_dict() for node in self.where_clause],
            "order_by": [],
            "limit": self._limit,
            "offset": self._offset,
            "m2m": None,
        }
        tx_id, using = self._transaction_or_using()
        return await delete_filtered(
            self.model_cls.__name__, _query_ir_payload_to_json(query_def), tx_id, using
        )

    async def exists(self) -> bool:
        """Return whether at least one record matches the current query

        Returns:
            True if records exist, otherwise False.

        Examples:
            >>> found = await User.where(lambda t: t.email == "a@b.com").exists()
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

        tx_id, using = self._transaction_or_using()
        await add_m2m_links(
            self._m2m_context["join_table"],
            self._m2m_context["source_col"],
            self._m2m_context["target_col"],
            self._m2m_context["source_id"],
            ids,
            tx_id,
            using,
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

        tx_id, using = self._transaction_or_using()
        await remove_m2m_links(
            self._m2m_context["join_table"],
            self._m2m_context["source_col"],
            self._m2m_context["target_col"],
            self._m2m_context["source_id"],
            ids,
            tx_id,
            using,
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

        tx_id, using = self._transaction_or_using()
        await clear_m2m_links(
            self._m2m_context["join_table"],
            self._m2m_context["source_col"],
            self._m2m_context["source_id"],
            tx_id,
            using,
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

    @overload
    def where(self, node: "QueryNode") -> "Relation[T]": ...

    @overload
    def where(self, node: "Predicate[T]") -> "Relation[T]": ...

    def where(self, node: "QueryNode | Predicate[T]") -> "Relation[T]":
        super().where(node)  # type: ignore[arg-type]
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
