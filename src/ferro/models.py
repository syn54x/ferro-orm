"""Define the core ORM model base and transaction helpers for Ferro."""

import json
from contextlib import asynccontextmanager
from enum import Enum
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    Self,
    overload,
)

if TYPE_CHECKING:
    from .query import Predicate
    from .session import Session

from pydantic import BaseModel, ConfigDict, model_validator

from ._core import (
    begin_transaction,
    commit_transaction,
    evict_instance,
    fetch_all,
    register_instance,
    rollback_transaction,
    save_bulk_records,
    save_record,
    transaction_connection_name,
)
from .base import ForeignKey, foreign_key_allows_none
from .exceptions import ModelDoesNotExist
from .metaclass import ModelMetaclass
from .query import Query, QueryNode
from .state import (
    _CURRENT_TRANSACTION,
    _CURRENT_TRANSACTION_CONNECTION,
    resolve_operation_scope,
    resolve_transaction_scope,
)


_FERRO_CONNECTION_ATTR = "__ferro_connection_name"


def _transaction_or_using(
    using: str | None, session: "Session | None"
) -> tuple[str | None, str | None, str | None]:
    return resolve_operation_scope(
        using=using, session=session, allow_legacy_default=True
    )


def _instance_transaction_route(
    instance: object, using: str | None, session: "Session | None"
) -> tuple[str | None, str | None, str | None, str | None]:
    origin = _instance_origin(instance)
    if using is not None and origin is not None and using != origin:
        raise ValueError("Instance is already bound to a different connection")

    tx_id, route_using, session_id = _transaction_or_using(using, session)
    if tx_id is not None:
        tx_connection = _CURRENT_TRANSACTION_CONNECTION.get()
        return tx_id, route_using, origin or tx_connection, session_id

    effective_using = route_using or origin
    return None, effective_using, effective_using, session_id


def _instance_origin(instance: object) -> str | None:
    origin = getattr(instance, _FERRO_CONNECTION_ATTR, None)
    return origin if isinstance(origin, str) else None


def _set_instance_origin(instance: object, using: str | None) -> None:
    if using is not None:
        object.__setattr__(instance, _FERRO_CONNECTION_ATTR, using)


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

    parent_tx_id, effective_using, session_id = resolve_transaction_scope(
        using=using, session=session, allow_legacy_default=True
    )
    tx_id = await begin_transaction(parent_tx_id, effective_using, session_id=session_id)
    connection_name = transaction_connection_name(tx_id, session_id=session_id)
    token = _CURRENT_TRANSACTION.set(tx_id)
    connection_token = _CURRENT_TRANSACTION_CONNECTION.set(connection_name)
    try:
        yield Transaction(tx_id, session_id=session_id)
        await commit_transaction(tx_id, session_id=session_id)
    except Exception:
        await rollback_transaction(tx_id, session_id=session_id)
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

            register_model_schema(cls.__name__, json.dumps(schema))

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
                    pk_field = "id"
                    for f_name, f_meta in self.__class__.ferro_fields.items():
                        if f_meta.primary_key:
                            pk_field = f_name
                            break
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
        self, *, using: str | None = None, session: "Session | None" = None
    ) -> None:
        """Persist the current model instance

        Returns:
            None

        Examples:
            >>> user = User(name="Taylor")
            >>> await user.save()
        """
        tx_id, operation_using, identity_using, session_id = _instance_transaction_route(
            self, using, session
        )
        new_id = await save_record(
            self.__class__.__name__,
            self.model_dump_json(),
            tx_id,
            operation_using,
            session_id=session_id,
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
                self.__class__.__name__,
                str(pk_val),
                self,
                identity_using,
                session_id=session_id,
            )
            _set_instance_origin(self, identity_using)

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
        _tx_id, operation_using, identity_using, session_id = _instance_transaction_route(
            self, using, session
        )

        if pk_val is not None:
            name = self.__class__.__name__
            query = self.__class__.where(
                getattr(self.__class__, pk_field_name) == pk_val
            )
            if operation_using is not None:
                query = Query(self.__class__, using=operation_using).where(
                    getattr(self.__class__, pk_field_name) == pk_val
                )
            await query.delete()
            evict_instance(
                name, str(pk_val), identity_using, session_id=session_id
            )

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
    def _fix_types(cls, instance: Self) -> None:
        """Normalize hydrated values to declared Python types

        Args:
            instance: Model instance to normalize in-place.

        Returns:
            None
        """
        for field_name, enum_cls in cls._enum_fields.items():
            val = getattr(instance, field_name)
            if val is not None and not isinstance(val, enum_cls):
                try:
                    setattr(instance, field_name, enum_cls(val))
                except Exception:
                    pass

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
        tx_id, using, session_id = _transaction_or_using(using, session)
        results = await fetch_all(cls, tx_id, using, session_id=session_id)
        for instance in results:
            cls._fix_types(instance)
        return results

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

        instance = await cls.where(getattr(cls, pk_field_name) == pk, session=session).first()
        if instance:
            cls._fix_types(instance)
        return instance

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

        name = self.__class__.__name__
        _tx_id, operation_using, identity_using, session_id = _instance_transaction_route(
            self, using, session
        )

        evict_instance(name, str(pk_val), identity_using, session_id=session_id)
        query = self.__class__.where(getattr(self.__class__, pk_field_name) == pk_val)
        if operation_using is not None:
            query = Query(self.__class__, using=operation_using).where(
                getattr(self.__class__, pk_field_name) == pk_val
            )
        fresh_instance = await query.first()

        if fresh_instance is None:
            raise RuntimeError(f"Instance not found in database: {name}({pk_val})")

        self.__dict__.update(fresh_instance.__dict__)
        register_instance(
            name, str(pk_val), self, identity_using, session_id=session_id
        )
        _set_instance_origin(self, identity_using)
        self.__class__._fix_types(self)

    @overload
    @classmethod
    def where(cls, node: QueryNode) -> Query[Self]: ...

    @overload
    @classmethod
    def where(cls, node: "Predicate[Self]") -> Query[Self]: ...

    @classmethod
    def where(
        cls, node: "QueryNode | Predicate[Self]", *, session: "Session | None" = None
    ) -> Query[Self]:
        """Start a fluent query with an initial condition.

        The recommended style is a lambda predicate of shape
        ``Callable[[QueryProxy[Self]], QueryNode]``, e.g.
        ``User.where(lambda t: t.age >= 18)``. The lambda receives a
        :class:`QueryProxy` whose attributes build comparisons as
        :class:`QueryNode` instances, so predicates type-check cleanly.
        A prebuilt :class:`QueryNode` is also accepted, built either with
        :func:`ferro.query.col` (the type-safe escape hatch that preserves
        operator shape) or with operator syntax on class attributes. The
        bare operator form (``User.where(User.age >= 18)``) is deprecated and
        on the v0.13.0 removal track. It does not
        type-check statically:
        the class attribute types as the field type, so the comparison
        resolves to ``bool``, not ``QueryNode``. See
        ``docs/concepts/query-typing.md`` for the trade-offs between the
        three styles.

        Args:
            node: A predicate callable or a ``QueryNode``.

        Returns:
            A query object scoped to this model class.

        Examples:
            >>> q1 = User.where(lambda t: t.archived == False)  # noqa: E712
            >>> q2 = User.where(lambda t: t.id == 1)
            >>> isinstance(q1, Query) and isinstance(q2, Query)
            True
        """
        return Query(cls, session=session).where(node)

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

        Args:
            **fields: Field values to construct the model.

        Returns:
            The newly created and persisted model instance.

        Examples:
            >>> user = await User.create(name="Taylor")
            >>> isinstance(user, User)
            True
        """
        instance = cls(**fields)
        await instance.save(session=session)
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
        # Use mode="json" to ensure Decimals, UUIDs, etc. are serialized correctly
        data = [i.model_dump(mode="json") for i in instances]
        tx_id, using, session_id = _transaction_or_using(using, session)
        return await save_bulk_records(
            cls.__name__, json.dumps(data), tx_id, using, session_id=session_id
        )

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
            query = query.where(getattr(cls, key) == val)

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
            query = query.where(getattr(cls, key) == val)

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
        instance = self.model_cls(**fields)
        await instance.save(using=self._connection_name)
        return instance

    async def all(self) -> list[M]:
        return await self.model_cls.all(using=self._connection_name)

    def select(self) -> Query[M]:
        return Query(self.model_cls, using=self._connection_name)

    @overload
    def where(self, node: QueryNode) -> Query[M]: ...

    @overload
    def where(self, node: "Predicate[M]") -> Query[M]: ...

    def where(self, node: "QueryNode | Predicate[M]") -> Query[M]:
        return self.select().where(node)

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

        instance = await self.where(
            getattr(self.model_cls, pk_field_name) == pk
        ).first()
        if instance:
            self.model_cls._fix_types(instance)
        return instance

    async def bulk_create(self, instances: list[M]) -> int:
        return await self.model_cls.bulk_create(instances, using=self._connection_name)

    async def get_or_create(
        self, defaults: dict[str, Any] | None = None, **fields: Any
    ) -> tuple[M, bool]:
        query = Query(self.model_cls, using=self._connection_name)
        for key, val in fields.items():
            query = query.where(getattr(self.model_cls, key) == val)

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
            query = query.where(getattr(self.model_cls, key) == val)

        instance = await query.first()
        if instance:
            for key, val in (defaults or {}).items():
                setattr(instance, key, val)
            await instance.save(using=self._connection_name)
            return instance, False

        params = {**fields, **(defaults or {})}
        return await self.create(**params), True
