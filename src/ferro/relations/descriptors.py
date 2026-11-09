"""The attributes that read a relation from a model instance."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from ferro._core import RouteHandle
    from ferro.models import Model

from .._core import unloaded_instance
from ..registry import REGISTRY


def _instance_origin_outside_transaction(instance: object) -> str | None:
    from ..models import _instance_origin
    from ..state import _CURRENT_TRANSACTION

    if _CURRENT_TRANSACTION.get() is not None:
        return None
    return _instance_origin(instance)


def _first_read_route(instance: object) -> "RouteHandle | None":
    """The route whose session shares a forward relation's instance.

    The parent's own connection, inside the session or transaction around
    the read; `None` when neither the parent nor a session names one, and
    the instance is cached on the parent alone. A parent from another
    connection than the session's gets its own connection outside the
    session: a read never raises, and `.load()` reports the conflict.
    """
    from ..state import (
        _CURRENT_SESSION,
        _CURRENT_TRANSACTION,
        RouteHandle,
        resolve_operation_scope,
    )

    origin = _instance_origin_outside_transaction(instance)
    session = _CURRENT_SESSION.get()
    if origin is None and session is None and _CURRENT_TRANSACTION.get() is None:
        return None
    if origin is not None and session is not None and origin != session.connection_name:
        return RouteHandle(connection_name=origin)
    return resolve_operation_scope(using=origin, session=None)


class RelationshipDescriptor(BaseModel):
    """The attribute behind a reverse (`Reverse`) or many-to-many relation.

    On an instance, the relation reads as a [`Relation`][ferro.Relation]
    query over the related rows at every cardinality; a one-to-one reverse
    relation reads its instance, or `None`, with `.first()`. See
    [Relationships](../guide/relationships.md).
    """

    target_model_name: str
    """The related model's name."""
    field_name: str
    """The relation's own field name."""
    fk_column: str | None = None
    """The related model's foreign-key column that points back; `None` for
    a many-to-many relation."""
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

        fk_field = self.fk_column
        from ..query.builder import Relation

        return Relation(
            self._target_model,
            using=_instance_origin_outside_transaction(instance),
        ).where(lambda related, f=fk_field, v=pk_val: getattr(related, f) == v)


class ForwardDescriptor(BaseModel):
    """The attribute behind a forward relation, read as the related instance.

    On an instance, the relation reads as the instance its foreign-key column
    points at: the complete one an `include()` or `.load()` filled, or the
    session's instance for that key, or an unloaded instance holding only the
    key, which `.load()` fills in place. An empty key reads as `None`. See
    [Relationships](../guide/relationships.md).
    """

    target_model_name: str
    """The related model's name."""
    field_name: str
    """The relation's field name."""
    fk_column: str
    """The foreign-key column the relation reads."""
    _target_model: Model | None = None

    def __get__(self, instance, owner):
        if instance is None:
            return self

        if self._target_model is None:
            self._target_model = REGISTRY.resolve_reference(self.target_model_name)

        key = getattr(instance, self.fk_column)
        if key is None:
            return None
        related = unloaded_instance(
            self._target_model,
            key,
            (type(instance).__name__, self.field_name),
            _first_read_route(instance),
        )
        # Cached under the relation name, as include() caches a loaded one, so
        # the next read is a plain attribute and a refresh keeps it while the
        # key still points at it.
        instance.__dict__[self.field_name] = related
        return related
