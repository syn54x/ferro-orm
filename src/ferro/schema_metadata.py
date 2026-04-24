"""Shared Ferro schema normalization used by runtime registration and Alembic."""

from __future__ import annotations

from typing import Any, ForwardRef

from ._annotation_utils import annotation_allows_none
from .base import ForeignKey, foreign_key_allows_none
from .composite_uniques import apply_composite_uniques_to_schema


def _property_is_integer(prop: dict[str, Any]) -> bool:
    return prop.get("type") == "integer" or any(
        item.get("type") == "integer" for item in prop.get("anyOf", [])
    )


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

    apply_composite_uniques_to_schema(model_cls, schema)
    return schema


__all__ = ["build_model_schema"]
