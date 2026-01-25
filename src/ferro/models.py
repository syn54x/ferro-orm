import json
from contextlib import asynccontextmanager
from contextvars import ContextVar
from enum import Enum
from typing import (
    Annotated,
    Any,
    Self,
    get_args,
    get_origin,
    get_type_hints,
)

from pydantic import BaseModel, ConfigDict

# Import the backend function from our compiled Rust core
from ._core import (
    fetch_all,
    fetch_one,
    register_instance,
    register_model_schema,
    save_record,
    delete_record,
    evict_instance,
    save_bulk_records,
    begin_transaction,
    commit_transaction,
    rollback_transaction,
)
from .query import FieldProxy, Query, QueryNode

# Context variable to store the active transaction ID for the current task
_CURRENT_TRANSACTION: ContextVar[str | None] = ContextVar("current_transaction", default=None)

@asynccontextmanager
async def transaction():
    """
    Asynchronous context manager for database transactions.
    
    Usage:
        async with ferro.transaction():
            await User.create(username="alice")
            ...
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



class FerroField:
    """
    Metadata container for Ferro-specific field configuration.
    """

    def __init__(
        self,
        primary_key: bool = False,
        autoincrement: bool | None = None,
        unique: bool = False,
        index: bool = False,
    ):
        self.primary_key = primary_key
        self.autoincrement = autoincrement
        self.unique = unique
        self.index = index


class ModelMetaclass(type(BaseModel)):
    """
    Metaclass for Ferro models that automatically registers the model schema with the Rust core.
    """

    def __new__(mcs, name, bases, namespace, **kwargs):
        # 1. Create the class using Pydantic's internal logic
        cls = super().__new__(mcs, name, bases, namespace, **kwargs)

        # 2. Skip the 'Model' base class itself
        if name != "Model":
            # Inject FieldProxy for each field to enable operator overloading on the class
            for field_name in cls.model_fields:
                setattr(cls, field_name, FieldProxy(field_name))

            # 3. Parse FerroField metadata from Annotated hints
            ferro_fields = {}
            try:
                # include_extras=True is key to seeing Annotated metadata
                hints = get_type_hints(cls, include_extras=True)
                for field_name, hint in hints.items():
                    if get_origin(hint) is Annotated:
                        for metadata in get_args(hint):
                            if isinstance(metadata, FerroField):
                                ferro_fields[field_name] = metadata
                                break
            except Exception as e:
                # Fallback or log if type hints fail to resolve
                pass

            cls.ferro_fields = ferro_fields

            try:
                # 4. Generate the schema and send it to Rust
                schema = cls.model_json_schema()

                # Inject our custom metadata into the schema so Rust can see it
                if "properties" in schema:
                    for field_name, metadata in ferro_fields.items():
                        if field_name in schema["properties"]:
                            schema["properties"][field_name]["primary_key"] = (
                                metadata.primary_key
                            )
                            # Default autoincrement to True only for integers if not specified
                            prop = schema["properties"][field_name]
                            is_int = prop.get("type") == "integer" or any(
                                item.get("type") == "integer"
                                for item in prop.get("anyOf", [])
                            )
                            auto = metadata.autoincrement
                            if auto is None:
                                auto = metadata.primary_key and is_int
                            
                            # Update the metadata object itself so save() can use it
                            metadata.autoincrement = auto
                            schema["properties"][field_name]["autoincrement"] = auto
                            schema["properties"][field_name]["unique"] = metadata.unique
                            schema["properties"][field_name]["index"] = metadata.index

                register_model_schema(name, json.dumps(schema))
            except Exception as e:
                # CX Choice: Catch and re-raise with a Ferro-specific hint
                raise RuntimeError(f"Ferro failed to register model '{name}': {e}")

        return cls


class Model(BaseModel, metaclass=ModelMetaclass):
    """
    Base class for all Ferro models.

    Inherits from Pydantic's BaseModel and provides asynchronous CRUD operations
    backed by a high-performance Rust engine.
    """

    # This ensures Pydantic behaves like a standard object when needed
    model_config = ConfigDict(
        from_attributes=True,
        use_attribute_docstrings=True,
    )

    async def save(self) -> None:
        """
        Persist the model instance to the database.

        This method performs an upsert (INSERT or UPDATE) based on the primary key.
        After saving, the instance is registered with the internal Identity Map.

        Returns:
            None
        """
        # Thin Bridge: Pass the model name and the serialized data
        tx_id = _CURRENT_TRANSACTION.get()
        new_id = await save_record(self.__class__.__name__, self.model_dump_json(), tx_id)

        # Register with Identity Map
        pk_val = None
        pk_field_name = None

        # Try to find primary key via ferro_fields metadata first
        for field_name, metadata in self.__class__.ferro_fields.items():
            if metadata.primary_key:
                pk_field_name = field_name
                # If autoincrement was used and we don't have an ID yet, apply the new one
                if metadata.autoincrement and getattr(self, field_name) is None:
                    if new_id is not None:
                        setattr(self, field_name, new_id)
                pk_val = getattr(self, field_name)
                break

        # Fallback to Pydantic field extra (legacy support)
        if pk_field_name is None:
            for field_name, field in self.__class__.model_fields.items():
                if getattr(field, "json_schema_extra", {}).get("primary_key"):
                    pk_field_name = field_name
                    # Legacy fallback assumes autoincrement=True for ints
                    if getattr(self, field_name) is None and new_id is not None:
                        setattr(self, field_name, new_id)
                    pk_val = getattr(self, field_name)
                    break

        if pk_val is not None:
            register_instance(self.__class__.__name__, str(pk_val), self)

    async def delete(self) -> None:
        """
        Delete the model instance from the database and evict it from the Identity Map.
        """
        pk_val = None
        for field_name, metadata in self.__class__.ferro_fields.items():
            if metadata.primary_key:
                pk_val = getattr(self, field_name)
                break
        
        if pk_val is None:
            # Fallback for models without FerroField metadata
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
        """Fix up types that Rust couldn't perfectly hydrate (like Enums)."""
        if not hasattr(cls, "_enum_fields"):
            cls._enum_fields = {}
            try:
                # Use cls.__dict__.get('__annotations__') as a fallback
                # because get_type_hints can fail in local scopes
                hints = get_type_hints(cls, globalns=globals(), localns=locals())
                for field_name, hint in hints.items():
                    actual_type = hint
                    origin = get_origin(hint)
                    if origin is Annotated:
                        actual_type = get_args(hint)[0]
                    
                    # Handle Optional (Union[T, None])
                    from typing import Union
                    if origin is Union:
                        args = get_args(hint)
                        for arg in args:
                            try:
                                if isinstance(arg, type) and issubclass(arg, Enum):
                                    actual_type = arg
                                    break
                            except TypeError:
                                pass

                    try:
                        if isinstance(actual_type, type) and issubclass(actual_type, Enum):
                            cls._enum_fields[field_name] = actual_type
                    except TypeError:
                        pass
            except Exception as e:
                # If get_type_hints fails, we might still have __annotations__
                for field_name, hint in getattr(cls, "__annotations__", {}).items():
                    if field_name not in cls._enum_fields:
                        # Simple check for Enum in annotations
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
        """
        Fetch all records for this model.

        Uses Direct Injection to bypass standard Pydantic validation for
        maximum performance during mass hydration.

        Returns:
            list[Self]: A list of model instances.
        """
        tx_id = _CURRENT_TRANSACTION.get()
        results = await fetch_all(cls, tx_id)
        for instance in results:
            cls._fix_types(instance)
        return results

    @classmethod
    async def get(cls, value: Any) -> Self | None:
        """
        Fetch a single record by primary key.

        The operation first checks the internal Identity Map (cache). if not found,
        it performs a database lookup.

        Args:
            value: The primary key value to look up.

        Returns:
            Self | None: The model instance if found, otherwise None.

        Example:
            >>> user = await User.get(1)
        """
        tx_id = _CURRENT_TRANSACTION.get()
        instance = await fetch_one(cls, str(value), tx_id)
        if instance:
            cls._fix_types(instance)
        return instance

    async def refresh(self) -> None:
        """
        Reload the model instance's fields from the database.
        
        This method will fetch the latest data for this record and update
        the current instance in-place.
        """
        # 1. Find the primary key
        pk_val = None
        for field_name, metadata in self.__class__.ferro_fields.items():
            if metadata.primary_key:
                pk_val = getattr(self, field_name)
                break
        
        if pk_val is None:
            # Fallback for models without FerroField metadata
            for field_name, field in self.__class__.model_fields.items():
                if getattr(field, "json_schema_extra", {}).get("primary_key"):
                    pk_val = getattr(self, field_name)
                    break
        
        if pk_val is None:
            raise RuntimeError("Cannot refresh a model without a primary key")

        # 2. Evict from Identity Map to force a fresh fetch from DB
        name = self.__class__.__name__
        evict_instance(name, str(pk_val))

        # 3. Fetch from DB
        tx_id = _CURRENT_TRANSACTION.get()
        fresh_instance = await fetch_one(self.__class__, str(pk_val), tx_id)
        
        if fresh_instance is None:
            raise RuntimeError(f"Instance not found in database: {name}({pk_val})")

        # 4. Update the current instance in-place
        self.__dict__.update(fresh_instance.__dict__)
        
        # 5. Ensure the Identity Map points back to THIS instance (not the temp one created by fetch_one)
        register_instance(name, str(pk_val), self)
        
        # Also ensure types are fixed if necessary
        self.__class__._fix_types(self)

    @classmethod
    def where(cls, node: QueryNode) -> Query:
        """
        Start a fluent query with a condition.

        Args:
            node: A QueryNode captured via operator overloading (e.g., User.age >= 18).

        Returns:
            Query: A query builder object.
        """
        return Query(cls).where(node)

    @classmethod
    async def create(cls, **kwargs) -> Self:
        """
        Create and persist a new model instance.
        """
        instance = cls(**kwargs)
        await instance.save()
        return instance

    @classmethod
    async def bulk_create(cls, instances: list[Self]) -> int:
        """
        Efficiently persist multiple model instances in a single batch operation.
        """
        if not instances:
            return 0
        data = [i.model_dump() for i in instances]
        count = await save_bulk_records(cls.__name__, json.dumps(data))
        
        # Identity Map Registration: We need to find PKs for all these.
        # This is a bit tricky for autoincrement without returning all IDs.
        # For now, we leave them out of IM or require manual refresh.
        # SQLite's RETURNING clause would be ideal here.
        return count

    @classmethod
    async def get_or_create(
        cls, defaults: dict[str, Any] | None = None, **kwargs
    ) -> tuple[Self, bool]:
        """
        Look up an object with the given kwargs, creating one if it doesn't exist.
        """
        # Build query from kwargs
        query = Query(cls)
        for key, val in kwargs.items():
            query = query.where(getattr(cls, key) == val)
        
        instance = await query.first()
        if instance:
            return instance, False
        
        params = {**kwargs, **(defaults or {})}
        return await cls.create(**params), True

    @classmethod
    async def update_or_create(
        cls, defaults: dict[str, Any] | None = None, **kwargs
    ) -> tuple[Self, bool]:
        """
        Update an object with the given kwargs, creating one if it doesn't exist.
        """
        query = Query(cls)
        for key, val in kwargs.items():
            query = query.where(getattr(cls, key) == val)
        
        instance = await query.first()
        if instance:
            for key, val in (defaults or {}).items():
                setattr(instance, key, val)
            await instance.save()
            return instance, False
        
        params = {**kwargs, **(defaults or {})}
        return await cls.create(**params), True
