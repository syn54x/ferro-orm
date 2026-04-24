"""Shadow foreign-key column typing helpers (issue #16, design spec 2026-04-22)."""

from __future__ import annotations

import types
from typing import Annotated, Any, Union, get_args, get_origin
from uuid import UUID

from pydantic import BaseModel

# Matches metaclass fallback for unresolved FK targets
_FALLBACK_SHADOW_ANNOTATION = Union[int, str, None]


def is_concrete_ferro_model(obj: Any) -> bool:
    return isinstance(obj, type) and issubclass(obj, BaseModel) and hasattr(
        obj, "ferro_fields"
    )


def _scalar_part_of_annotation(ann: Any) -> Any:
    """Strip ``Annotated[T, ...]`` to ``T`` so shadow columns stay plain unions/scalars."""
    origin = get_origin(ann)
    if origin is Annotated:
        return get_args(ann)[0]
    return ann


def pk_python_type_for_model(target: type[Any]) -> Any | None:
    """Return the PK field's scalar annotation (inner ``T`` of ``Annotated[T, ...]``), or None."""
    ferro_fields = getattr(target, "ferro_fields", None)
    if not ferro_fields:
        return None
    pk_name = None
    for fname, fmeta in ferro_fields.items():
        if getattr(fmeta, "primary_key", False):
            pk_name = fname
            break
    if pk_name is None:
        return None
    mf = getattr(target, "model_fields", {}).get(pk_name)
    if mf is None:
        return None
    return _scalar_part_of_annotation(mf.annotation)


def shadow_annotation_for_pk(pk_ann: Any) -> Any:
    """Shadow *_id is optional at the ORM level (default None before assignment)."""
    if pk_ann is None:
        return _FALLBACK_SHADOW_ANNOTATION
    pk_ann = _scalar_part_of_annotation(pk_ann)
    origin = get_origin(pk_ann)
    args = get_args(pk_ann)
    if origin is Union or origin is types.UnionType:
        if type(None) in args:
            return pk_ann
    return pk_ann | None


def schema_fragment_for_pk(pk_ann: Any) -> dict[str, Any]:
    """JSON-schema fragment for a primary-key scalar annotation."""
    if pk_ann is None:
        return {"type": "string"}

    pk_ann = _scalar_part_of_annotation(pk_ann)
    origin = get_origin(pk_ann)
    args = get_args(pk_ann)
    if origin is Union or origin is types.UnionType:
        non_none = [arg for arg in args if arg is not type(None)]
        if len(non_none) == 1:
            pk_ann = _scalar_part_of_annotation(non_none[0])

    if pk_ann is int:
        return {"type": "integer"}
    if pk_ann is str:
        return {"type": "string"}
    if pk_ann is UUID:
        return {"type": "string", "format": "uuid"}
    if pk_ann is float:
        return {"type": "number"}
    if pk_ann is bool:
        return {"type": "boolean"}
    if pk_ann is bytes:
        return {"type": "string", "format": "binary"}

    return {"type": "string"}


def shadow_annotation_for_foreign_key(metadata: Any) -> Any:
    """Annotation for {name}_id at class creation time."""
    from .base import ForeignKey  # local import to avoid cycles at module import

    if not isinstance(metadata, ForeignKey):
        return _FALLBACK_SHADOW_ANNOTATION
    to = metadata.to
    if not is_concrete_ferro_model(to):
        return _FALLBACK_SHADOW_ANNOTATION
    pk_ann = pk_python_type_for_model(to)
    return shadow_annotation_for_pk(pk_ann)


def is_fallback_shadow_annotation(ann: Any) -> bool:
    """True if annotation is the legacy Union[int, str, None] shadow."""
    if ann is _FALLBACK_SHADOW_ANNOTATION:
        return True
    origin = get_origin(ann)
    if origin is not Union and origin is not types.UnionType:
        return False
    args = set(get_args(ann))
    return args == {int, str, type(None)}


def reconcile_shadow_fk_types(registry: dict[str, type[Any]]) -> list[type[Any]]:
    """
    After ForeignKey.to is concrete, upgrade shadow *_id annotations from fallback.

    Mutates cls.__annotations__ and calls model_rebuild(force=True) per changed class.

    Returns model classes that were rebuilt.
    """
    from .base import ForeignKey

    rebuilt: list[type[Any]] = []
    for model_name, model_cls in registry.items():
        if model_name == "Model":
            continue
        relations = getattr(model_cls, "ferro_relations", None)
        if not relations:
            continue
        id_fields_updated: list[str] = []
        for fname, meta in relations.items():
            if not isinstance(meta, ForeignKey):
                continue
            if not is_concrete_ferro_model(meta.to):
                continue
            pk_ann = pk_python_type_for_model(meta.to)
            if pk_ann is None:
                continue
            desired = shadow_annotation_for_pk(pk_ann)
            id_field = f"{fname}_id"
            if id_field not in getattr(model_cls, "model_fields", {}):
                continue
            current = model_cls.__annotations__.get(id_field)
            if current == desired:
                continue
            if current is not None and not is_fallback_shadow_annotation(current):
                # Explicit non-fallback annotation: do not override (custom user types)
                continue
            ann = model_cls.__dict__.get("__annotations__")
            if ann is None:
                model_cls.__annotations__ = {}
            else:
                model_cls.__annotations__ = dict(ann)
            model_cls.__annotations__[id_field] = desired
            id_fields_updated.append(id_field)
        if id_fields_updated:
            # Pydantic only re-evaluates a field when FieldInfo._complete is False; without
            # this, model_rebuild updates __annotations__ but model_fields stays stale (#16).
            pydantic_fields = model_cls.__pydantic_fields__
            for id_field in id_fields_updated:
                fi = pydantic_fields.get(id_field)
                if fi is not None:
                    fi._complete = False
                    fi._original_annotation = model_cls.__annotations__[id_field]
            model_cls.model_rebuild(force=True)
            rebuilt.append(model_cls)
    return rebuilt
