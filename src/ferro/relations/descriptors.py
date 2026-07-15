from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from ferro.models import Model

from ..state import resolve_model_reference


def _instance_origin_outside_transaction(instance: object) -> str | None:
    from ..models import _instance_origin
    from ..state import _CURRENT_TRANSACTION

    if _CURRENT_TRANSACTION.get() is not None:
        return None
    return _instance_origin(instance)


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
            self._target_model = resolve_model_reference(self.target_model_name)

        # The related rows are keyed by this instance's PK (cached fact —
        # this runs on every reverse-relation attribute access).
        pk_field = instance.__class__.__ferro_pk__
        if pk_field is None:
            raise ValueError(
                f"Cannot access relation {self.field_name!r}: "
                f"{instance.__class__.__name__} declares no primary-key "
                "column, so its related rows cannot be keyed."
            )
        pk_val = getattr(instance, pk_field)

        if self.is_m2m:
            from ..query.builder import Relation

            return Relation(
                self._target_model,
                using=_instance_origin_outside_transaction(instance),
            )._m2m(self.join_table, self.source_col, self.target_col, pk_val)

        fk_field = f"{self.field_name}_id"
        if self.is_one_to_one:
            return self._target_model.where(
                lambda t, f=fk_field, v=pk_val: getattr(t, f) == v
            ).first()

        from ..query.builder import Relation

        return Relation(
            self._target_model,
            using=_instance_origin_outside_transaction(instance),
        ).where(lambda t, f=fk_field, v=pk_val: getattr(t, f) == v)


class ForwardDescriptor(BaseModel):
    """Descriptor that handles lazy loading of a related object."""

    target_model_name: str
    field_name: str
    _target_model: Model | None = None

    def __get__(self, instance, owner):
        if instance is None:
            return self

        if self._target_model is None:
            self._target_model = resolve_model_reference(self.target_model_name)

        async def _fetch():
            id_val = getattr(instance, f"{self.field_name}_id")
            if id_val is None:
                return None

            origin = _instance_origin_outside_transaction(instance)
            if origin is not None:
                return await self._target_model.using(origin).get_or_none(id_val)
            return await self._target_model.get_or_none(id_val)

        return _fetch()
