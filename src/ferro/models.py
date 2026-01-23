import json
from typing import Any, Self

from pydantic import BaseModel, ConfigDict

# Import the backend function from our compiled Rust core
from ._core import (
    fetch_all,
    fetch_one,
    register_instance,
    register_model_schema,
    save_record,
)


class ModelMetaclass(type(BaseModel)):
    """
    Metaclass for Ferro models that automatically registers the model schema with the Rust core.
    """

    def __new__(mcs, name, bases, namespace, **kwargs):
        # 1. Create the class using Pydantic's internal logic
        cls = super().__new__(mcs, name, bases, namespace, **kwargs)

        # 2. Skip the 'Model' base class itself
        if name != "Model":
            try:
                # 3. Generate the schema and send it to Rust
                schema_json = json.dumps(cls.model_json_schema())
                register_model_schema(name, schema_json)
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
        await save_record(self.__class__.__name__, self.model_dump_json())

        # Register with Identity Map
        pk_val = None
        for field_name, field in self.model_fields.items():
            if getattr(field, "json_schema_extra", {}).get("primary_key"):
                pk_val = getattr(self, field_name)
                break

        if pk_val is not None:
            register_instance(self.__class__.__name__, str(pk_val), self)

    @classmethod
    async def all(cls) -> list[Self]:
        """
        Fetch all records for this model.

        Uses Direct Injection to bypass standard Pydantic validation for
        maximum performance during mass hydration.

        Returns:
            list[Self]: A list of model instances.
        """
        # Now passing 'cls' directly for Direct Injection in Rust
        return await fetch_all(cls)

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
        # Now passing 'cls' directly for Direct Injection in Rust
        return await fetch_one(cls, str(value))
