"""Define the core ORM model base and transaction helpers for Ferro."""

import json
from contextlib import asynccontextmanager
from enum import Enum
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    Literal,
    Self,
)

if TYPE_CHECKING:
    from .query import Predicate
    from .session import Session

from pydantic import BaseModel, ConfigDict, model_validator

from ._bind_payload import save_bind_payload
from ._core import (
    begin_transaction,
    commit_transaction,
    evict_instance as _core_evict_instance,
    fetch_all,
    register_instance,
    rollback_transaction,
    save_bulk_records,
    save_record,
    transaction_connection_name,
    update_record,
)
from .base import ForeignKey, foreign_key_allows_none
from .exceptions import ModelDoesNotExist
from .metaclass import ModelMetaclass
from .query import Predicate, Query
from .state import (
    RouteHandle,
    _CURRENT_TRANSACTION,
    _CURRENT_TRANSACTION_CONNECTION,
    resolve_operation_scope,
    resolve_transaction_scope,
    route_for_transaction,
)


_FERRO_CONNECTION_ATTR = "__ferro_connection_name"
_FERRO_PERSISTED_ATTR = "__ferro_persisted"


def _is_persisted(instance: object) -> bool:
    """True when the instance is known to have a row in the database.

    Set by Rust hydration (`hydrate_model_instance`) and by a successful
    ``save()``; cleared by ``delete()``. Absent means transient (FF-A A4).
    """
    return bool(instance.__dict__.get(_FERRO_PERSISTED_ATTR, False))


def _set_persisted(instance: object, value: bool) -> None:
    object.__setattr__(instance, _FERRO_PERSISTED_ATTR, value)


def _field_eq(field_name: str, value: Any) -> Predicate[Any]:
    """Build a lambda predicate for dynamic field equality without operator style."""
    return lambda t, f=field_name, v=value: getattr(t, f) == v


def _transaction_or_using(
    using: str | None, session: "Session | None"
) -> RouteHandle:
    return resolve_operation_scope(using=using, session=session)


def _instance_transaction_route(
    instance: object, using: str | None, session: "Session | None"
) -> tuple[RouteHandle, str]:
    """Resolve the route for an instance method (`save`/`delete`/`refresh`).

    Returns `(route, effective_connection)`. `effective_connection` is
    `route.connection_name` — always the real connection the instance's
    identity-map entry lives on, whether or not an ambient transaction is
    active (FF-D D3/D4 folded the old `identity_using`/`operation_using`
    split into the single resolved route).
    """
    origin = _instance_origin(instance)
    if using is not None and origin is not None and using != origin:
        raise ValueError("Instance is already bound to a different connection")

    # Origin is the instance's implicit route (FF-D D4): it participates in
    # session-conflict checks instead of silently bypassing the session.
    route = _transaction_or_using(using or origin, session)
    return route, route.connection_name


def _instance_origin(instance: object) -> str | None:
    origin = getattr(instance, _FERRO_CONNECTION_ATTR, None)
    return origin if isinstance(origin, str) else None


def _set_instance_origin(instance: object, using: str | None) -> None:
    if using is not None:
        object.__setattr__(instance, _FERRO_CONNECTION_ATTR, using)


def evict_instance(
    model: "type[Model] | str",
    pk: str,
    *,
    using: str | None = None,
    session: "Session | None" = None,
) -> None:
    """Remove one instance from the active scope's identity map.

    ``model`` is a model class, its qualified identity, or an unambiguous
    bare class name (ambiguity raises with the candidates listed).

    Public wrapper around the FFI `evict_instance` (FF-D D3): resolves the
    route once via `resolve_operation_scope`, then passes it through. Model
    instance methods (`save`/`delete`/`refresh`) call the FFI symbol
    directly with their already-resolved route instead of going through this
    wrapper, so a route is never resolved twice for one operation.
    """
    from .state import resolve_model_reference

    model_cls = resolve_model_reference(model) if isinstance(model, str) else model
    route = resolve_operation_scope(using=using, session=session)
    _core_evict_instance(model_cls.__ferro_identity__, pk, route)


@asynccontextmanager
async def transaction(using: str | None = None, *, session: "Session | None" = None):
    """Run database operations inside a transaction context.

    Yields a :class:`~ferro.raw.Transaction` handle bound to this transaction's
    connection. The handle exposes ``execute`` / ``fetch_all`` / ``fetch_one``
    for raw SQL on the same connection — useful for setting Postgres GUCs,
    advisory locks, and any one-off statement that doesn't fit a Model.

    Examples:
        >>> async with transaction() as tx:
        ...     user = await User.create(name="Taylor")
        ...     await tx.execute(
        ...         "select set_config('request.jwt.claims', $1, true)",
        ...         claims_json,
        ...     )

    Existing callers that don't bind the yielded value continue to work; the
    handle is simply discarded::

        >>> async with transaction():
        ...     user = await User.create(name="Taylor")
        ...     await user.save()
    """
    from .raw import Transaction

    route = resolve_transaction_scope(using=using, session=session)
    tx_id = await begin_transaction(route)
    connection_name = transaction_connection_name(tx_id, session_id=route.session_id)
    child_route = route_for_transaction(connection_name, tx_id, route.session_id)
    token = _CURRENT_TRANSACTION.set(tx_id)
    connection_token = _CURRENT_TRANSACTION_CONNECTION.set(connection_name)
    try:
        yield Transaction(child_route)
        await commit_transaction(tx_id, session_id=route.session_id)
    except Exception:
        await rollback_transaction(tx_id, session_id=route.session_id)
        raise
    finally:
        _CURRENT_TRANSACTION.reset(token)
        _CURRENT_TRANSACTION_CONNECTION.reset(connection_token)


class Model(BaseModel, metaclass=ModelMetaclass):
    """Provide the base class for all Ferro models

    Inheriting from this class registers schema metadata with the Rust core and
    exposes high-performance CRUD and query entrypoints.

    **Composite unique constraints:** declare a ``typing.ClassVar`` named
    ``__ferro_composite_uniques__`` as a tuple of tuples of column names
    (for example ``(("user_id", "org_id"),)``) to enforce uniqueness on those
    columns together. This is separate from per-column uniqueness
    (``Field(unique=True)`` on the field, ``Annotated[..., Field(unique=True)]``,
    or ``Annotated[..., FerroField(unique=True)]``), each of which applies to a
    single column only. Default many-to-many join tables get a
    composite unique on their two foreign-key columns automatically.

    **Composite indexes:** declare a ``typing.ClassVar`` named
    ``__ferro_composite_indexes__`` as a tuple of tuples of column names
    (for example ``(("user_id", "created_at"),)``) for non-unique multi-column
    indexes. Validation rules mirror ``__ferro_composite_uniques__``: each
    inner tuple must contain at least two columns, columns must exist on the
    model, and order is preserved (matters for leftmost-prefix optimization).
    For single-column indexes use ``Field(index=True)``. Default many-to-many
    join tables get a non-unique reverse-direction composite index
    automatically; opt out with ``ManyToMany(reverse_index=False)``.

    Examples:
        >>> class User(Model):
        ...     id: int | None = None
        ...     name: str
    """

    __ferro_composite_uniques__: ClassVar[tuple[tuple[str, ...], ...]] = ()
    __ferro_composite_indexes__: ClassVar[tuple[tuple[str, ...], ...]] = ()
    _enum_fields: ClassVar[dict[str, type[Enum]]] = {}

    @classmethod
    def _reregister_ferro(cls) -> None:
        """Re-register this model's schema with the Rust core (e.g. after clear_registry)."""
        schema = getattr(cls, "__ferro_schema__", None)
        if schema is not None:
            from ._core import register_model_schema

            register_model_schema(
                cls.__ferro_identity__, json.dumps(schema), cls.__ferro_table__
            )

    model_config = ConfigDict(
        from_attributes=True,
        use_attribute_docstrings=True,
        arbitrary_types_allowed=True,
    )

    def __init__(self, **data: Any):
        """Initialize a model instance and normalize relationship inputs

        Args:
            **data: Field values used to construct the model.

        Examples:
            >>> user = User(name="Taylor")
            >>> isinstance(user, User)
            True
        """
        # 1. Handle relationship inputs (e.g. Product(category=my_cat))
        relations = getattr(self.__class__, "ferro_relations", {})
        for field_name, metadata in relations.items():
            if isinstance(metadata, ForeignKey) and field_name in data:
                val = data.pop(field_name)
                # If it's a Model instance, extract the ID
                if isinstance(val, Model):
                    # Read the *target* model's PK (FF-D D5) — the source
                    # model's PK name is irrelevant to the related instance.
                    pk_field = val.__class__._primary_key_field_name() or "id"
                    id_val = getattr(val, pk_field, None)
                    data[f"{field_name}_id"] = id_val
                else:
                    # It's already an ID or something else
                    data[f"{field_name}_id"] = val

        super().__init__(**data)

    @model_validator(mode="after")
    def _validate_required_foreign_keys(self) -> Self:
        """Keep Python model validation aligned with required FK nullability."""
        relations = getattr(self.__class__, "ferro_relations", {})
        for field_name, metadata in relations.items():
            if not isinstance(metadata, ForeignKey):
                continue
            if foreign_key_allows_none(metadata) is False:
                if getattr(self, f"{field_name}_id", None) is None:
                    raise ValueError(f"{field_name} is required")
        return self

    async def save(
        self,
        *,
        using: str | None = None,
        session: "Session | None" = None,
        on_conflict: Literal["update"] | None = None,
    ) -> None:
        """Persist the current model instance.

        A transient instance (constructed with ``Model(...)`` and never saved)
        is INSERTed — a duplicate primary key or unique value raises
        :class:`~ferro.exceptions.UniqueViolationError`. A persistent instance
        (fetched from the database, or previously saved) is UPDATEd by primary
        key. Pass ``on_conflict="update"`` for insert-or-update semantics
        regardless of persistence state (the primitive behind
        :meth:`upsert`).

        Note that ``model_copy()`` copies persistence state: saving a copy of
        a persisted instance updates the same row. The UPDATE targets the
        instance's *current* primary-key value, so mutating the PK of a
        persisted instance before ``save()`` matches no row and raises. A row
        inserted inside a rolled-back transaction leaves the instance marked
        persisted; a later ``save()`` raises ``ModelDoesNotExist``.

        Args:
            using: Connection name override.
            session: Session scope for the operation.
            on_conflict: ``None`` (default) or ``"update"`` to upsert.

        Raises:
            UniqueViolationError: A duplicate primary key or unique value on
                INSERT.
            ModelDoesNotExist: The row behind a persisted instance no longer
                exists (deleted underneath, or the PK was mutated).
            ValueError: ``on_conflict`` is not ``None`` or ``"update"``, or a
                persisted instance has no primary-key value.

        Examples:
            >>> user = User(name="Taylor")
            >>> await user.save()
        """
        if on_conflict not in (None, "update"):
            raise ValueError(
                f'on_conflict must be None or "update", got {on_conflict!r}'
            )
        route, identity_using = _instance_transaction_route(self, using, session)
        new_id = None
        if on_conflict == "update":
            new_id = await save_record(
                self.__class__.__ferro_identity__,
                save_bind_payload(self),
                route,
                mode="upsert",
            )
        elif _is_persisted(self):
            pk_field_name = self.__class__._primary_key_field_name()
            pk_val = getattr(self, pk_field_name) if pk_field_name is not None else None
            if pk_val is None:
                raise ValueError(
                    f"Cannot UPDATE a persisted {self.__class__.__name__} "
                    "without a primary key value"
                )
            rows_affected = await update_record(
                self.__class__.__ferro_identity__,
                save_bind_payload(self),
                route,
            )
            if rows_affected == 0:
                raise ModelDoesNotExist(self.__class__, pk_val)
        else:
            new_id = await save_record(
                self.__class__.__ferro_identity__,
                save_bind_payload(self),
                route,
                mode="insert",
            )

        pk_val = None
        pk_field_name = None

        for field_name, metadata in self.__class__.ferro_fields.items():
            if metadata.primary_key:
                pk_field_name = field_name
                if metadata.autoincrement and getattr(self, field_name) is None:
                    if new_id is not None:
                        setattr(self, field_name, new_id)
                pk_val = getattr(self, field_name)
                break

        if pk_field_name is None:
            for field_name, field in self.__class__.model_fields.items():
                if getattr(field, "json_schema_extra", {}).get("primary_key"):
                    pk_field_name = field_name
                    if getattr(self, field_name) is None and new_id is not None:
                        setattr(self, field_name, new_id)
                    pk_val = getattr(self, field_name)
                    break

        if pk_val is not None:
            register_instance(
                self.__class__.__ferro_identity__,
                str(pk_val),
                self,
                route,
            )
            _set_instance_origin(self, identity_using)
        _set_persisted(self, True)

    async def delete(
        self, *, using: str | None = None, session: "Session | None" = None
    ) -> None:
        """Delete the current model instance from storage

        Returns:
            None

        Examples:
            >>> user = await User.get_or_none(1)
            >>> if user:
            ...     await user.delete()
        """
        pk_field_name = self.__class__._primary_key_field_name()
        pk_val = getattr(self, pk_field_name) if pk_field_name is not None else None
        route, _identity_using = _instance_transaction_route(self, using, session)

        if pk_val is not None:
            name = self.__class__.__ferro_identity__
            query = Query(self.__class__, using=route.connection_name).where(
                _field_eq(pk_field_name, pk_val)
            )
            await query.delete()
            _core_evict_instance(name, str(pk_val), route)
            # The instance is transient again: a later save() re-INSERTs.
            _set_persisted(self, False)

    @classmethod
    def _primary_key_field_name(cls) -> str | None:
        for field_name, metadata in cls.ferro_fields.items():
            if metadata.primary_key:
                return field_name

        for field_name, field in cls.model_fields.items():
            if getattr(field, "json_schema_extra", {}).get("primary_key"):
                return field_name

        return None

    @classmethod
    async def all(
        cls, *, using: str | None = None, session: "Session | None" = None
    ) -> list[Self]:
        """Fetch all records for this model class

        Returns:
            A list of hydrated model instances.

        Examples:
            >>> users = await User.all()
            >>> isinstance(users, list)
            True
        """
        route = _transaction_or_using(using, session)
        return await fetch_all(cls, route)

    @classmethod
    async def get(cls, pk: Any, *, session: "Session | None" = None) -> Self:
        """Fetch one record by primary key value.

        Args:
            pk: Primary key value to fetch a single record.

        Returns:
            The matching model instance.

        Raises:
            ModelDoesNotExist: When no row exists for this primary key. Use
                :meth:`get_or_none` if you need optional lookup without raising.

        Examples:
            >>> user = await User.get(1)
            >>> isinstance(user, User)
            True
        """
        instance = await cls.get_or_none(pk, session=session)
        if instance is None:
            raise ModelDoesNotExist(cls, pk)
        return instance

    @classmethod
    async def get_or_none(
        cls, pk: Any, *, session: "Session | None" = None
    ) -> Self | None:
        """Fetch one record by primary key, or return None if no row exists.

        Args:
            pk: Primary key value to fetch a single record.

        Returns:
            The matching model instance, or None when no record exists.
        """
        pk_field_name = cls._primary_key_field_name()
        if pk_field_name is None:
            raise RuntimeError(f"Model {cls.__name__} does not define a primary key")

        return await cls.where(_field_eq(pk_field_name, pk), session=session).first()

    async def refresh(
        self, *, using: str | None = None, session: "Session | None" = None
    ) -> None:
        """Reload this instance from storage using its primary key

        Returns:
            None

        Raises:
            RuntimeError: If no primary key is available or the record no longer exists.

        Examples:
            >>> user = await User.get(1)
            >>> await user.refresh()
        """
        pk_field_name = self.__class__._primary_key_field_name()
        pk_val = getattr(self, pk_field_name) if pk_field_name is not None else None

        if pk_val is None:
            raise RuntimeError("Cannot refresh a model without a primary key")

        name = self.__class__.__ferro_identity__
        route, identity_using = _instance_transaction_route(self, using, session)

        _core_evict_instance(name, str(pk_val), route)
        query = Query(self.__class__, using=route.connection_name).where(
            _field_eq(pk_field_name, pk_val)
        )
        fresh_instance = await query.first()

        if fresh_instance is None:
            raise RuntimeError(f"Instance not found in database: {name}({pk_val})")

        self.__dict__.update(fresh_instance.__dict__)
        register_instance(name, str(pk_val), self, route)
        _set_instance_origin(self, identity_using)
        _set_persisted(self, True)

    @classmethod
    def where(
        cls, predicate: "Predicate[Self]", *, session: "Session | None" = None
    ) -> Query[Self]:
        """Start a fluent query with an initial condition.

        ``predicate`` is a lambda of shape
        ``Callable[[QueryProxy[Self]], QueryNode]``, e.g.
        ``User.where(lambda user: user.age >= 18)``. The lambda receives a
        :class:`QueryProxy` whose attributes build comparisons as
        :class:`QueryNode` instances, so predicates type-check cleanly.
        Name the parameter after the model in lowercase singular (``user`` for
        ``User``, ``post`` for ``Post``). Column names are validated at build
        time against the model's declared fields (plus shadow ``{fk}_id``
        columns).

        Args:
            predicate: A callable that takes a :class:`QueryProxy` and
                returns a :class:`QueryNode`.

        Returns:
            A query object scoped to this model class.

        Examples:
            >>> q1 = User.where(lambda user: user.archived == False)  # noqa: E712
            >>> q2 = User.where(lambda user: user.id == 1)
            >>> isinstance(q1, Query) and isinstance(q2, Query)
            True
        """
        return Query(cls, session=session).where(predicate)

    @classmethod
    def select(cls, *, session: "Session | None" = None) -> Query[Self]:
        """Start an empty fluent query for this model class

        Returns:
            A query object scoped to this model class.

        Examples:
            >>> query = User.select().limit(5)
            >>> isinstance(query, Query)
            True
        """
        return Query(cls, session=session)

    @classmethod
    def using(cls, name: str) -> "ModelConnection[Self]":
        """Bind ORM operations for this model to a named connection."""
        return ModelConnection(cls, name)

    @classmethod
    async def create(cls, *, session: "Session | None" = None, **fields) -> Self:
        """Create and persist a new model instance

        ``create()`` is a plain INSERT: it never updates an existing row.

        Args:
            **fields: Field values to construct the model.

        Returns:
            The newly created and persisted model instance.

        Raises:
            UniqueViolationError: A row with the same primary key or unique
                value already exists — use :meth:`upsert` for
                insert-or-update semantics.

        Examples:
            >>> user = await User.create(name="Taylor")
            >>> isinstance(user, User)
            True
        """
        instance = cls(**fields)
        await instance.save(session=session)
        return instance

    @classmethod
    async def upsert(cls, *, session: "Session | None" = None, **fields) -> Self:
        """Insert the row, or update the existing row on primary-key conflict.

        Equivalent to ``cls(**fields).save(on_conflict="update")``. With an
        autoincrement primary key left unset there is no conflict target, so
        this degrades to a plain INSERT.

        Args:
            **fields: Field values to construct the model.

        Returns:
            The persisted model instance.

        Examples:
            >>> user = await User.upsert(id=1, name="Taylor")
            >>> isinstance(user, User)
            True
        """
        instance = cls(**fields)
        await instance.save(session=session, on_conflict="update")
        return instance

    @classmethod
    async def bulk_create(
        cls,
        instances: list[Self],
        *,
        using: str | None = None,
        session: "Session | None" = None,
    ) -> int:
        """Persist multiple instances in a single bulk operation

        Args:
            instances: Model instances to persist.

        Returns:
            The number of records inserted.

        Examples:
            >>> rows = await User.bulk_create([User(name="A"), User(name="B")])
            >>> isinstance(rows, int)
            True
        """
        if not instances:
            return 0
        data = [save_bind_payload(i) for i in instances]
        route = _transaction_or_using(using, session)
        return await save_bulk_records(cls.__ferro_identity__, data, route)

    @classmethod
    async def get_or_create(
        cls,
        defaults: dict[str, Any] | None = None,
        *,
        session: "Session | None" = None,
        **fields,
    ) -> tuple[Self, bool]:
        """Fetch a record by filters or create one when missing

        Args:
            defaults: Values applied only when creating a new record.
            **fields: Exact-match filters used for lookup.

        Returns:
            A tuple of ``(instance, created)`` where ``created`` is True for new records.

        Examples:
            >>> user, created = await User.get_or_create(email="a@b.com")
            >>> isinstance(created, bool)
            True
        """
        query = Query(cls, session=session)
        for key, val in fields.items():
            query = query.where(_field_eq(key, val))

        instance = await query.first()
        if instance:
            return instance, False

        params = {**fields, **(defaults or {})}
        return await cls.create(session=session, **params), True

    @classmethod
    async def update_or_create(
        cls,
        defaults: dict[str, Any] | None = None,
        *,
        session: "Session | None" = None,
        **fields,
    ) -> tuple[Self, bool]:
        """Update a matched record or create one when missing

        Args:
            defaults: Values applied on update or create paths.
            **fields: Exact-match filters used for lookup.

        Returns:
            A tuple of ``(instance, created)`` where ``created`` is True for new records.
        """
        query = Query(cls, session=session)
        for key, val in fields.items():
            query = query.where(_field_eq(key, val))

        instance = await query.first()
        if instance:
            for key, val in (defaults or {}).items():
                setattr(instance, key, val)
            await instance.save(session=session)
            return instance, False

        params = {**fields, **(defaults or {})}
        return await cls.create(session=session, **params), True


class ModelConnection[M: Model]:
    """Connection-bound ORM entrypoint returned by ``Model.using(name)``.

    Generic over the concrete model class so that every accessor preserves
    the bound type — e.g. ``Transcript.using("service").get(pk)`` resolves
    to ``Transcript`` rather than ``Model``.
    """

    def __init__(self, model_cls: type[M], connection_name: str) -> None:
        self.model_cls: type[M] = model_cls
        self._connection_name: str = connection_name

    async def create(self, **fields: Any) -> M:
        """Create and persist a new instance on this connection.

        A plain INSERT — a duplicate primary key or unique value raises
        :class:`~ferro.exceptions.UniqueViolationError`; use :meth:`upsert`
        for insert-or-update semantics.
        """
        instance = self.model_cls(**fields)
        await instance.save(using=self._connection_name)
        return instance

    async def upsert(self, **fields: Any) -> M:
        """Insert the row on this connection, or update it on PK conflict."""
        instance = self.model_cls(**fields)
        await instance.save(using=self._connection_name, on_conflict="update")
        return instance

    async def all(self) -> list[M]:
        return await self.model_cls.all(using=self._connection_name)

    def select(self) -> Query[M]:
        return Query(self.model_cls, using=self._connection_name)

    def where(self, predicate: "Predicate[M]") -> Query[M]:
        return self.select().where(predicate)

    async def get(self, pk: Any) -> M:
        instance = await self.get_or_none(pk)
        if instance is None:
            raise ModelDoesNotExist(self.model_cls, pk)
        return instance

    async def get_or_none(self, pk: Any) -> M | None:
        pk_field_name = self.model_cls._primary_key_field_name()
        if pk_field_name is None:
            raise RuntimeError(
                f"Model {self.model_cls.__name__} does not define a primary key"
            )

        return await self.where(_field_eq(pk_field_name, pk)).first()

    async def bulk_create(self, instances: list[M]) -> int:
        return await self.model_cls.bulk_create(instances, using=self._connection_name)

    async def get_or_create(
        self, defaults: dict[str, Any] | None = None, **fields: Any
    ) -> tuple[M, bool]:
        query = Query(self.model_cls, using=self._connection_name)
        for key, val in fields.items():
            query = query.where(_field_eq(key, val))

        instance = await query.first()
        if instance:
            return instance, False

        params = {**fields, **(defaults or {})}
        return await self.create(**params), True

    async def update_or_create(
        self, defaults: dict[str, Any] | None = None, **fields: Any
    ) -> tuple[M, bool]:
        query = Query(self.model_cls, using=self._connection_name)
        for key, val in fields.items():
            query = query.where(_field_eq(key, val))

        instance = await query.first()
        if instance:
            for key, val in (defaults or {}).items():
                setattr(instance, key, val)
            await instance.save(using=self._connection_name)
            return instance, False

        params = {**fields, **(defaults or {})}
        return await self.create(**params), True
