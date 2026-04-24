"""Shared Ferro schema normalization used by runtime registration and Alembic."""

from __future__ import annotations

import types
from enum import Enum
from typing import (
    Annotated,
    Any,
    ForwardRef,
    Union,
    get_args,
    get_origin,
    get_type_hints,
)

from ._annotation_utils import annotation_allows_none
from .base import ForeignKey, foreign_key_allows_none
from .composite_uniques import apply_composite_uniques_to_schema


def _property_is_integer(prop: dict[str, Any]) -> bool:
    return prop.get("type") == "integer" or any(
        item.get("type") == "integer" for item in prop.get("anyOf", [])
    )


def _strip_optional_union(hint: Any) -> Any:
    """Unwrap ``T | None`` to ``T`` (same as ``ModelMetaclass``)."""
    while True:
        origin = get_origin(hint)
        if origin is Union or origin is types.UnionType:
            args = get_args(hint)
            non_none = [a for a in args if a is not type(None)]
            if len(non_none) == 1:
                hint = non_none[0]
                continue
        return hint


def _enum_subclass_from_annotation(hint: Any) -> type[Enum] | None:
    hint = _strip_optional_union(hint)
    if get_origin(hint) is Annotated:
        args = get_args(hint)
        if args:
            return _enum_subclass_from_annotation(args[0])
        return None
    if isinstance(hint, type) and issubclass(hint, Enum):
        return hint
    return None


def _target_table_name(target: Any) -> str:
    if isinstance(target, ForwardRef):
        return target.__forward_arg__.lower()
    if isinstance(target, str):
        return target.lower()
    if hasattr(target, "__name__"):
        return target.__name__.lower()
    return str(target).lower()


def build_model_schema(
    model_cls: type[Any], schema: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Return the canonical Ferro-enriched schema for one model class."""
    if schema is None:
        schema = model_cls.model_json_schema()
    else:
        schema = dict(schema)

    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return schema

    model_fields = getattr(model_cls, "model_fields", {})

    for field_name, metadata in getattr(model_cls, "ferro_fields", {}).items():
        prop = properties.get(field_name)
        if not isinstance(prop, dict):
            continue

        prop["primary_key"] = metadata.primary_key

        autoincrement = metadata.autoincrement
        if autoincrement is None:
            autoincrement = metadata.primary_key and _property_is_integer(prop)
        metadata.autoincrement = autoincrement

        prop["autoincrement"] = autoincrement
        prop["unique"] = metadata.unique
        prop["index"] = metadata.index

        field_info = model_fields.get(field_name)
        if field_info is not None:
            if isinstance(getattr(metadata, "nullable", "infer"), bool):
                prop["ferro_nullable"] = metadata.nullable
            else:
                prop["ferro_nullable"] = annotation_allows_none(field_info.annotation)

    for field_name, metadata in getattr(model_cls, "ferro_relations", {}).items():
        if not isinstance(metadata, ForeignKey):
            continue

        id_field = f"{field_name}_id"
        prop = properties.get(id_field)
        if not isinstance(prop, dict):
            continue

        prop["foreign_key"] = {
            "to_table": _target_table_name(metadata.to),
            "on_delete": metadata.on_delete,
            "unique": metadata.unique,
        }
        if metadata.unique:
            prop["unique"] = True

        fk_nullable = foreign_key_allows_none(metadata)
        if fk_nullable is not None:
            prop["ferro_nullable"] = fk_nullable

    try:
        resolved_annotations = get_type_hints(model_cls, include_extras=True)
    except Exception:
        resolved_annotations = {}
    for field_name, finfo in model_fields.items():
        if field_name not in properties or not isinstance(properties[field_name], dict):
            continue
        ann_hint = resolved_annotations.get(field_name, finfo.annotation)
        enum_cls = _enum_subclass_from_annotation(ann_hint)
        if enum_cls is not None:
            properties[field_name]["enum_type_name"] = enum_cls.__name__.lower()

    apply_composite_uniques_to_schema(model_cls, schema)
    return schema


__all__ = ["build_model_schema"]
