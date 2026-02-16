from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from ferro.models import Model

from ..state import _MODEL_REGISTRY_PY


class RelationshipDescriptor(BaseModel):
    """Descriptor that returns either a Query object or a single object (for 1:1)."""

    target_model_name: str
    field_name: str
    is_one_to_one: bool = False
    is_m2m: bool = False
    join_table: str | None = None
    source_col: str | None = None
    target_col: str | None = None
    _target_model: Model | None = None

    def __get__(self, instance, owner):
        if instance is None:
            return self

        if self._target_model is None:
            self._target_model = _MODEL_REGISTRY_PY.get(self.target_model_name)
            if self._target_model is None:
                raise RuntimeError(
                    f"Model '{self.target_model_name}' not found in registry"
                )

        # Find the primary key value of the current instance
        pk_field = "id"
        if hasattr(instance.__class__, "ferro_fields"):
            for f_name, f_meta in instance.__class__.ferro_fields.items():
                if f_meta.primary_key:
                    pk_field = f_name
                    break
        pk_val = getattr(instance, pk_field)

        if self.is_m2m:
            from ..query.builder import Query

            return Query(self._target_model)._m2m(
                self.join_table, self.source_col, self.target_col, pk_val
            )

        # Find the primary key value of the current instance
        pk_field = "id"
        if hasattr(instance.__class__, "ferro_fields"):
            for f_name, f_meta in instance.__class__.ferro_fields.items():
                if f_meta.primary_key:
                    pk_field = f_name
                    break

        pk_val = getattr(instance, pk_field)

        query = self._target_model.where(
            getattr(self._target_model, f"{self.field_name}_id") == pk_val
        )

        if self.is_one_to_one:
            return query.first()

        return query


class ForwardDescriptor(BaseModel):
    """Descriptor that handles lazy loading of a related object."""

    target_model_name: str
    field_name: str
    _target_model: Model | None = None

    def __get__(self, instance, owner):
        if instance is None:
            return self

        if self._target_model is None:
            self._target_model = _MODEL_REGISTRY_PY.get(self.target_model_name)
            if self._target_model is None:
                raise RuntimeError(
                    f"Model '{self.target_model_name}' not found in registry"
                )

        async def _fetch():
            id_val = getattr(instance, f"{self.field_name}_id")
            if id_val is None:
                return None
            return await self._target_model.get(id_val)

        return _fetch()
