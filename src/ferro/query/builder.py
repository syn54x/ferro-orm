"""Build fluent query objects that serialize QueryIR payloads for the Rust core."""

import copy
from typing import TYPE_CHECKING, Any, Callable, Generic, Self, Type, TypeVar, overload

from .._bind_payload import update_bind_payload
from .._core import (
    add_m2m_links,
    clear_m2m_links,
    count_filtered,
    delete_filtered,
    fetch_filtered,
    remove_m2m_links,
    update_filtered,
)
from .nodes import (
    FieldProxy,
    Predicate,
    QueryNode,
    QueryProxy,
    _serialize_query_value,
    validate_query_column,
)

if TYPE_CHECKING:
    from .._core import RouteHandle

T = TypeVar("T")
E = TypeVar("E")


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


def _model_identity(model_cls: type) -> str:
    """Qualified registry identity of a Ferro model class (FF-E)."""
    return model_cls.__ferro_identity__  # ty: ignore[unresolved-attribute]


def _resolve_where_node(predicate: "Predicate[Any]", model_cls: type) -> QueryNode:
    """Evaluate a lambda predicate against a validating ``QueryProxy``."""
    if not callable(predicate):
        raise TypeError(
            "where() expected a predicate callable "
            f"(e.g. `lambda user: user.age >= 18`), got {type(predicate).__name__}"
        )
    result = predicate(QueryProxy(model_cls))
    if not isinstance(result, QueryNode):
        raise TypeError(
            "where() predicate callable must return QueryNode, "
            f"got {type(result).__name__}"
        )
    return result


class Query(Generic[T]):
    """Build and execute fluent ORM queries.

    Attributes:
        model_cls: Model class used to hydrate results.
        where_clause: Accumulated filter nodes for the query.
        order_by_clause: Sort definitions sent to the Rust core.
    """

    def __init__(
        self, model_cls: Type[T], using: str | None = None, session: Any | None = None
    ):
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
        self._session = session
        self.where_clause: list["QueryNode"] = []
        self.order_by_clause: list[dict[str, str]] = []
        self._limit: int | None = None
        self._offset: int | None = None
        self._m2m_context: dict[str, Any] | None = None

    async def _transaction_or_using(self) -> "RouteHandle":
        from .. import _ensure_rust_registration_synced_for_operation
        from ..state import resolve_operation_scope

        await _ensure_rust_registration_synced_for_operation()
        return resolve_operation_scope(using=self._using, session=self._session)

    def _clone(self) -> Self:
        """Return a copy of this query with no shared mutable state.

        ``copy.copy`` preserves the concrete class (``Relation`` stays
        ``Relation``); the mutable containers are then replaced so chained
        queries never alias the originals (FF-F F-1).
        """
        new = copy.copy(self)
        new.where_clause = list(self.where_clause)
        new.order_by_clause = list(self.order_by_clause)
        new._m2m_context = (
            dict(self._m2m_context) if self._m2m_context is not None else None
        )
        return new

    def _m2m(
        self, join_table: str, source_col: str, target_col: str, source_id: Any
    ) -> Self:
        """Store many-to-many linkage context for relationship operations.

        A new ``Query`` with the m2m context set; ``self`` is unchanged.
        """
        new = self._clone()
        new._m2m_context = {
            "join_table": join_table,
            "source_col": source_col,
            "target_col": target_col,
            "source_id": source_id,
        }
        return new

    def where(self, predicate: "Predicate[T]") -> Self:
        """Add a filter condition to the query.

        ``predicate`` is a lambda of shape ``Callable[[QueryProxy[T]], QueryNode]``.
        The lambda receives a fresh :class:`QueryProxy` whose attributes
        return :class:`FieldProxy` instances, so
        ``lambda user: user.archived == False`` builds a comparison. Column
        names are validated at build time against the model's declared
        fields (plus shadow ``{fk}_id`` columns): a misspelled column raises
        ``AttributeError`` naming the closest valid match, before any query
        is sent to the database.

        Args:
            predicate: A callable that takes a :class:`QueryProxy` and
                returns a :class:`QueryNode`.

        Returns:
            A new ``Query`` with the clause added; ``self`` is unchanged.

        Raises:
            TypeError: If ``predicate`` is not callable, or if it does not
                return a ``QueryNode``.

        Examples:
            >>> q1 = User.where(lambda user: user.archived == False)  # noqa: E712
            >>> q2 = User.where(lambda user: user.id == 1)
            >>> isinstance(q1, Query) and isinstance(q2, Query)
            True
        """
        new = self._clone()
        new.where_clause.append(_resolve_where_node(predicate, self.model_cls))
        return new

    def order_by(
        self,
        field: "str | Callable[[QueryProxy[T]], FieldProxy[Any]]",
        direction: str = "asc",
    ) -> Self:
        """Add an ordering clause and return a new query.

        Accepts a lambda naming the column (``order_by(lambda u: u.created_at,
        "desc")`` — the documented style, matching ``where`` predicates) or a
        column-name string (``order_by("created_at", "desc")``). Both forms are
        validated against the model's queryable columns at build time.

        Args:
            field: Column selector — lambda receiving a :class:`QueryProxy`,
                or a column-name string.
            direction: ``"asc"`` (default) or ``"desc"``.

        Returns:
            A new ``Query`` with the ordering added; ``self`` is unchanged.

        Raises:
            AttributeError: If the column is not a queryable column.
            TypeError: If a lambda selector returns something other than a
                single column reference.
            ValueError: If ``direction`` is not ``"asc"`` or ``"desc"``.

        Examples:
            >>> newest = await Post.select().order_by(lambda p: p.created_at, "desc").all()
        """
        if direction.lower() not in ("asc", "desc"):
            raise ValueError("direction must be 'asc' or 'desc'")

        if isinstance(field, str):
            col_name = validate_query_column(self.model_cls, field)
        elif callable(field):
            selected = field(QueryProxy(self.model_cls))
            if not isinstance(selected, FieldProxy):
                raise TypeError(
                    "order_by() selector must return a FieldProxy "
                    f"(e.g. `lambda u: u.created_at`), got {type(selected).__name__}"
                )
            col_name = selected.column
        else:
            raise TypeError(
                "order_by() expected a column-name string or a lambda selector, "
                f"got {type(field).__name__}"
            )

        new = self._clone()
        new.order_by_clause.append({"column": col_name, "direction": direction.lower()})
        return new

    def limit(self, value: int) -> Self:
        """Limit the number of records returned

        Args:
            value: The maximum number of records to return.

        Returns:
            A new ``Query`` with the clause added; ``self`` is unchanged.

        Examples:
            >>> query = User.select().limit(10)
            >>> query._limit
            10
        """
        new = self._clone()
        new._limit = value
        return new

    def offset(self, value: int) -> Self:
        """Skip a specific number of records

        Args:
            value: The number of records to skip.

        Returns:
            A new ``Query`` with the clause added; ``self`` is unchanged.

        Examples:
            >>> query = User.select().offset(20)
            >>> query._offset
            20
        """
        new = self._clone()
        new._offset = value
        return new

    def _mutating_query_def(self, operation: str) -> dict[str, Any]:
        """Build the QueryIR payload for a mutating operation (update/delete).

        Mutating payloads never carry ``limit``/``offset`` keys: portable SQL
        has no ``UPDATE/DELETE ... LIMIT``, so pagination on a mutation is
        rejected loudly instead of being silently ignored.

        Raises:
            ValueError: If ``limit()`` or ``offset()`` was set on this query.
        """
        if self._limit is not None or self._offset is not None:
            raise ValueError(
                f"{operation}() does not support limit/offset: portable SQL has "
                f"no {operation.upper()} ... LIMIT. Remove the .limit()/.offset() "
                f"call, or fetch primary keys first and {operation} by "
                "primary-key set."
            )
        return {
            "model_name": _model_identity(self.model_cls),
            "where": [node.to_ir_dict() for node in self.where_clause],
            "order_by": [],
            "m2m": None,
        }

    async def all(self) -> list[T]:
        """Return all model instances that match the current query

        Returns:
            A list of model instances.

        Examples:
            >>> users = await User.where(lambda user: user.active == True).all()  # noqa: E712
            >>> isinstance(users, list)
            True
        """
        query_def = {
            "model_name": _model_identity(self.model_cls),
            "where": [node.to_ir_dict() for node in self.where_clause],
            "order_by": self.order_by_clause,
            "limit": self._limit,
            "offset": self._offset,
            "m2m": self._m2m_context,
        }
        route = await self._transaction_or_using()
        return await fetch_filtered(
            self.model_cls,
            _query_ir_payload_to_json(query_def),
            route,
        )

    async def count(self) -> int:
        """Return the number of records that match the current query

        Returns:
            The count of matching records.

        Examples:
            >>> total = await User.where(lambda user: user.active == True).count()  # noqa: E712
            >>> isinstance(total, int)
            True
        """
        query_def = {
            "model_name": _model_identity(self.model_cls),
            "where": [node.to_ir_dict() for node in self.where_clause],
            "order_by": [],
            "limit": None,
            "offset": None,
            "m2m": self._m2m_context,
        }
        route = await self._transaction_or_using()
        return await count_filtered(
            _model_identity(self.model_cls),
            _query_ir_payload_to_json(query_def),
            route,
        )

    async def update(self, **fields) -> int:
        """Update all records matching the current query

        Args:
            **fields: Field names and values to update.

        Returns:
            The number of records updated.

        Raises:
            ValueError: If ``limit()`` or ``offset()`` was set on this query.

        Examples:
            >>> updated = await User.where(lambda user: user.id == 1).update(name="Taylor")
            >>> isinstance(updated, int)
            True
        """
        query_def = self._mutating_query_def("update")
        route = await self._transaction_or_using()
        return await update_filtered(
            _model_identity(self.model_cls),
            _query_ir_payload_to_json(query_def),
            update_bind_payload(fields),
            route,
        )

    async def first(self) -> T | None:
        """Return the first matching record, or None

        Returns:
            A model instance or None.

        Examples:
            >>> user = await User.select().order_by("id").first()
            >>> user is None or isinstance(user, User)
            True
        """
        results = await self.limit(1).all()
        return results[0] if results else None

    async def delete(self) -> int:
        """Delete all records matching the current query

        Returns:
            The number of records deleted.

        Raises:
            ValueError: If ``limit()`` or ``offset()`` was set on this query.

        Examples:
            >>> deleted = await User.where(lambda user: user.disabled == True).delete()  # noqa: E712
            >>> isinstance(deleted, int)
            True
        """
        query_def = self._mutating_query_def("delete")
        route = await self._transaction_or_using()
        return await delete_filtered(
            _model_identity(self.model_cls),
            _query_ir_payload_to_json(query_def),
            route,
        )

    async def exists(self) -> bool:
        """Return whether at least one record matches the current query

        Returns:
            True if records exist, otherwise False.

        Examples:
            >>> found = await User.where(lambda user: user.email == "a@b.com").exists()
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

        route = await self._transaction_or_using()
        await add_m2m_links(
            self._m2m_context["join_table"],
            self._m2m_context["source_col"],
            self._m2m_context["target_col"],
            self._m2m_context["source_id"],
            ids,
            route,
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

        route = await self._transaction_or_using()
        await remove_m2m_links(
            self._m2m_context["join_table"],
            self._m2m_context["source_col"],
            self._m2m_context["target_col"],
            self._m2m_context["source_id"],
            ids,
            route,
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

        route = await self._transaction_or_using()
        await clear_m2m_links(
            self._m2m_context["join_table"],
            self._m2m_context["source_col"],
            self._m2m_context["source_id"],
            route,
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
