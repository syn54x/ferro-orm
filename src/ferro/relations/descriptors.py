"""The attributes that read a relation from a model instance."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from ferro.models import Model

from ..registry import REGISTRY


def _instance_origin_outside_transaction(instance: object) -> str | None:
    from ..models import _instance_origin
    from ..state import _CURRENT_TRANSACTION

    if _CURRENT_TRANSACTION.get() is not None:
        return None
    return _instance_origin(instance)


class RelationshipDescriptor(BaseModel):
    """The attribute behind a reverse (`BackRef`) or many-to-many relation.

    On an instance, a to-many relation reads as a
    [`Relation`][ferro.Relation] query over the related rows, and a
    one-to-one reverse relation as an awaitable of the related instance or
    `None`. See [Relationships](../guide/relationships.md).
    """

    target_model_name: str
    """The related model's name."""
    field_name: str
    """The name of the related model's foreign key field that points back."""
    is_one_to_one: bool = False
    """Whether at most one related row exists."""
    is_m2m: bool = False
    """Whether the relation goes through a join table."""
    join_table: str | None = None
    """The join table, for a many-to-many relation."""
    source_col: str | None = None
    """The join table column that points at this model."""
    target_col: str | None = None
    """The join table column that points at the related model."""
    _target_model: Model | None = None

    def __get__(self, instance, owner):
        if instance is None:
            return self

        if self._target_model is None:
            self._target_model = REGISTRY.resolve_reference(self.target_model_name)

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
            )._m2m(
                self.join_table,
                self.source_col,
                self.target_col,
                pk_val,
                label=f"{type(instance).__name__.lower()}.{self.field_name}",
            )

        fk_field = f"{self.field_name}_id"
        if self.is_one_to_one:
            return self._target_model.where(
                lambda related, f=fk_field, v=pk_val: getattr(related, f) == v
            ).first()

        from ..query.builder import Relation

        return Relation(
            self._target_model,
            using=_instance_origin_outside_transaction(instance),
        ).where(lambda related, f=fk_field, v=pk_val: getattr(related, f) == v)


class ForwardDescriptor(BaseModel):
    """The attribute behind a foreign key relation, loaded when awaited.

    On an instance, the relation reads as an awaitable of the related
    instance, or `None` when the foreign key is empty. A query with
    `include()` fills the relation in instead, as a plain attribute.
    """

    target_model_name: str
    """The related model's name."""
    field_name: str
    """The relation's field name; its foreign key column is `<field_name>_id`."""
    _target_model: Model | None = None

    def __get__(self, instance, owner):
        if instance is None:
            return self

        if self._target_model is None:
            self._target_model = REGISTRY.resolve_reference(self.target_model_name)

        async def _fetch():
            id_val = getattr(instance, f"{self.field_name}_id")
            if id_val is None:
                return None

            origin = _instance_origin_outside_transaction(instance)
            if origin is not None:
                return await self._target_model.using(origin).get_or_none(id_val)
            return await self._target_model.get_or_none(id_val)

        return _fetch()
