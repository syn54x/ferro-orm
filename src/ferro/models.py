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
    # This ensures Pydantic behaves like a standard object when needed
    model_config = ConfigDict(from_attributes=True)

    async def save(self) -> None:
        """Persist the model instance to the database."""
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
    async def all(cls) -> list:
        """Fetch all records for this model."""
        items = await fetch_all(cls.__name__)
        results = []
        for item in items:
            if isinstance(item, dict):
                obj = cls(**item)
                # Find PK value from the object
                pk_val = None
                for field_name, field in cls.model_fields.items():
                    if getattr(field, "json_schema_extra", {}).get("primary_key"):
                        pk_val = getattr(obj, field_name)
                        break

                if pk_val is not None:
                    register_instance(cls.__name__, str(pk_val), obj)
                results.append(obj)
            else:
                # It's already a Python object from the Identity Map
                results.append(item)
        return results

    @classmethod
    async def get(cls, value: Any) -> Self | None:
        """
        Fetch a single record by primary key.

        Example:
          User.get(1)
        """
        # 1. Call backend (Rust handles checking Identity Map)
        result = await fetch_one(cls.__name__, str(value))
        if result is None:
            return None

        if isinstance(result, dict):
            obj = cls(**result)
            register_instance(cls.__name__, str(value), obj)
            return obj

        return result
