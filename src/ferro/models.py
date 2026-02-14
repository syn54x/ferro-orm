"""Define the core ORM model base and transaction helpers for Ferro."""

import json
from contextlib import asynccontextmanager
from enum import Enum
from typing import (
    Any,
    Self,
    get_args,
    get_origin,
    get_type_hints,
)

from pydantic import BaseModel, ConfigDict

from ._core import (
    begin_transaction,
    commit_transaction,
    delete_record,
    evict_instance,
    fetch_all,
    fetch_one,
    register_instance,
    rollback_transaction,
    save_bulk_records,
    save_record,
)
from .base import ForeignKey
from .metaclass import ModelMetaclass
from .query import Query, QueryNode
from .state import _CURRENT_TRANSACTION


@asynccontextmanager
async def transaction():
    """Run database operations inside a transaction context

    Yields control to the caller within an open transaction.

    Examples:
        >>> async with transaction():
        ...     user = await User.create(name="Taylor")
        ...     await user.save()
    """
    tx_id = await begin_transaction()
    token = _CURRENT_TRANSACTION.set(tx_id)
    try:
        yield
        await commit_transaction(tx_id)
    except Exception:
        await rollback_transaction(tx_id)
        raise
    finally:
        _CURRENT_TRANSACTION.reset(token)


class Model(BaseModel, metaclass=ModelMetaclass):
    """Provide the base class for all Ferro models

    Inheriting from this class registers schema metadata with the Rust core and
    exposes high-performance CRUD and query entrypoints.

    Examples:
        >>> class User(Model):
        ...     id: int | None = None
        ...     name: str
    """

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

    async def save(self) -> None:
        """Persist the current model instance

        Returns:
            None

        Examples:
            >>> user = User(name="Taylor")
            >>> await user.save()
        """
        tx_id = _CURRENT_TRANSACTION.get()
        new_id = await save_record(
            self.__class__.__name__, self.model_dump_json(), tx_id
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
            register_instance(self.__class__.__name__, str(pk_val), self)

    async def delete(self) -> None:
        """Delete the current model instance from storage

        Returns:
            None

        Examples:
            >>> user = await User.get(1)
            >>> if user:
            ...     await user.delete()
        """
        pk_val = None
        for field_name, metadata in self.__class__.ferro_fields.items():
            if metadata.primary_key:
                pk_val = getattr(self, field_name)
                break

        if pk_val is None:
            for field_name, field in self.__class__.model_fields.items():
                if getattr(field, "json_schema_extra", {}).get("primary_key"):
                    pk_val = getattr(self, field_name)
                    break

        if pk_val is not None:
            name = self.__class__.__name__
            tx_id = _CURRENT_TRANSACTION.get()
            await delete_record(name, str(pk_val), tx_id)
            evict_instance(name, str(pk_val))

    @classmethod
    def _fix_types(cls, instance: Self) -> None:
        """Normalize hydrated values to declared Python types

        Args:
            instance: Model instance to normalize in-place.

        Returns:
            None
        """
        if not hasattr(cls, "_enum_fields"):
            cls._enum_fields = {}
            try:
                hints = get_type_hints(cls, globalns=globals(), localns=locals())
                for field_name, hint in hints.items():
                    actual_type = hint
                    origin = get_origin(hint)
                    from typing import Union as TypingUnion

                    if origin is TypingUnion:
                        args = get_args(hint)
                        for arg in args:
                            try:
                                if isinstance(arg, type) and issubclass(arg, Enum):
                                    actual_type = arg
                                    break
                            except TypeError:
                                pass

                    try:
                        if isinstance(actual_type, type) and issubclass(
                            actual_type, Enum
                        ):
                            cls._enum_fields[field_name] = actual_type
                    except TypeError:
                        pass
            except Exception:
                for field_name, hint in getattr(cls, "__annotations__", {}).items():
                    if field_name not in cls._enum_fields:
                        if isinstance(hint, type) and issubclass(hint, Enum):
                            cls._enum_fields[field_name] = hint

        for field_name, enum_cls in cls._enum_fields.items():
            val = getattr(instance, field_name)
            if val is not None and not isinstance(val, enum_cls):
                try:
                    setattr(instance, field_name, enum_cls(val))
                except Exception:
                    pass

    @classmethod
    async def all(cls) -> list[Self]:
        """Fetch all records for this model class

        Returns:
            A list of hydrated model instances.

        Examples:
            >>> users = await User.all()
            >>> isinstance(users, list)
            True
        """
        tx_id = _CURRENT_TRANSACTION.get()
        results = await fetch_all(cls, tx_id)
        for instance in results:
            cls._fix_types(instance)
        return results

    @classmethod
    async def get(cls, pk: Any) -> Self | None:
        """Fetch one record by primary key value

        Args:
            pk: Primary key value to fetch a single record.

        Returns:
            The matching model instance, or None when no record exists.

        Examples:
            >>> user = await User.get(1)
            >>> user is None or isinstance(user, User)
            True
        """
        tx_id = _CURRENT_TRANSACTION.get()
        instance = await fetch_one(cls, str(pk), tx_id)
        if instance:
            cls._fix_types(instance)
        return instance

    async def refresh(self) -> None:
        """Reload this instance from storage using its primary key

        Returns:
            None

        Raises:
            RuntimeError: If no primary key is available or the record no longer exists.

        Examples:
            >>> user = await User.get(1)
            >>> if user:
            ...     await user.refresh()
        """
        pk_val = None
        for field_name, metadata in self.__class__.ferro_fields.items():
            if metadata.primary_key:
                pk_val = getattr(self, field_name)
                break

        if pk_val is None:
            for field_name, field in self.__class__.model_fields.items():
                if getattr(field, "json_schema_extra", {}).get("primary_key"):
                    pk_val = getattr(self, field_name)
                    break

        if pk_val is None:
            raise RuntimeError("Cannot refresh a model without a primary key")

        name = self.__class__.__name__
        evict_instance(name, str(pk_val))

        tx_id = _CURRENT_TRANSACTION.get()
        fresh_instance = await fetch_one(self.__class__, str(pk_val), tx_id)

        if fresh_instance is None:
            raise RuntimeError(f"Instance not found in database: {name}({pk_val})")

        self.__dict__.update(fresh_instance.__dict__)
        register_instance(name, str(pk_val), self)
        self.__class__._fix_types(self)

    @classmethod
    def where(cls, node: QueryNode) -> Query[Self]:
        """Start a fluent query with an initial condition

        Args:
            node: Query predicate node to apply first.

        Returns:
            A query object scoped to this model class.

        Examples:
            >>> query = User.where(User.id == 1)
            >>> isinstance(query, Query)
            True
        """
        return Query(cls).where(node)

    @classmethod
    def select(cls) -> Query[Self]:
        """Start an empty fluent query for this model class

        Returns:
            A query object scoped to this model class.

        Examples:
            >>> query = User.select().limit(5)
            >>> isinstance(query, Query)
            True
        """
        return Query(cls)

    @classmethod
    async def create(cls, **fields) -> Self:
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
        await instance.save()
        return instance

    @classmethod
    async def bulk_create(cls, instances: list[Self]) -> int:
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
        return await save_bulk_records(cls.__name__, json.dumps(data))

    @classmethod
    async def get_or_create(
        cls, defaults: dict[str, Any] | None = None, **fields
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
        query = Query(cls)
        for key, val in fields.items():
            query = query.where(getattr(cls, key) == val)

        instance = await query.first()
        if instance:
            return instance, False

        params = {**fields, **(defaults or {})}
        return await cls.create(**params), True

    @classmethod
    async def update_or_create(
        cls, defaults: dict[str, Any] | None = None, **fields
    ) -> tuple[Self, bool]:
        """Update a matched record or create one when missing

        Args:
            defaults: Values applied on update or create paths.
            **fields: Exact-match filters used for lookup.

        Returns:
            A tuple of ``(instance, created)`` where ``created`` is True for new records.

        Examples:
            >>> user, created = await User.update_or_create(
            ...     email="a@b.com",
            ...     defaults={"name": "Taylor"},
            ... )
            >>> isinstance(created, bool)
            True
        """
        query = Query(cls)
        for key, val in fields.items():
            query = query.where(getattr(cls, key) == val)

        instance = await query.first()
        if instance:
            for key, val in (defaults or {}).items():
                setattr(instance, key, val)
            await instance.save()
            return instance, False

        params = {**fields, **(defaults or {})}
        return await cls.create(**params), True
