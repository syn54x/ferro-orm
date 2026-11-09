"""Primary-key typing helpers that join tables and key-column checks read."""

from __future__ import annotations

from typing import Annotated, Any, get_args, get_origin


def _scalar_part_of_annotation(ann: Any) -> Any:
    """Strip ``Annotated[T, ...]`` to ``T`` so a column spec stores plain unions/scalars."""
    origin = get_origin(ann)
    if origin is Annotated:
        return get_args(ann)[0]
    return ann


def pk_python_type_for_model(target: type[Any]) -> Any | None:
    """Return the PK column's scalar python type from the target's specs."""
    pk_field = getattr(target, "__ferro_pk__", None)
    if pk_field is None:
        return None
    specs = getattr(target, "__ferro_columns__", None) or {}
    spec = specs.get(pk_field)
    return spec.python_type if spec is not None else None
