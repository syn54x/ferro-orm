"""Marshal row values for the typed codec bind path (#162).

Produces a per-column value map where ``bytes``/``bytearray`` are preserved
verbatim (so non-UTF-8 binary survives) and every other value is canonicalized
exactly as pydantic's JSON mode produces it. This replaces the ``model_dump_json``
/ ``to_json`` string envelope in ``Model.save`` / ``bulk_create`` / ``Query.update``.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from pydantic_core import to_json

from .base import ForeignKey

_ONLY_CONTAINERS = (set, frozenset, list, tuple)
_ONLY_USAGE = 'Use only={"messages"}.'


def _bytes_field_names(instance: Any) -> set[str]:
    """Fields whose *current value* is bytes-like (value-driven: catches
    ``bytes``, ``bytes | None``, and ``Any``-typed bytes)."""
    return {
        name
        for name in type(instance).model_fields
        if isinstance(getattr(instance, name, None), (bytes, bytearray))
    }


def save_bind_payload(instance: Any) -> dict[str, Any]:
    """Column->value map for ``save``/``bulk_create``.

    Non-bytes columns go through pydantic ``model_dump(mode="json")`` (byte-identical
    to today, honoring field serializers/aliases); bytes columns are overlaid raw.
    """
    bytes_fields = _bytes_field_names(instance)
    payload: dict[str, Any] = instance.model_dump(mode="json", exclude=bytes_fields)
    for name in bytes_fields:
        payload[name] = bytes(getattr(instance, name))
    return payload


def normalize_save_only(only: object) -> set[str]:
    """Collapse ``only=`` to a set of column names (set semantics).

    Bare ``str`` / ``bytes`` is rejected so ``only="messages"`` is not
    treated as an iterable of characters.
    """
    if isinstance(only, (str, bytes)):
        raise TypeError(
            "only= must be a set, frozenset, list, or tuple of column names, "
            f"not a string. {_ONLY_USAGE}"
        )
    if not isinstance(only, _ONLY_CONTAINERS):
        raise TypeError(
            "only= must be a set, frozenset, list, or tuple of column names. "
            f"{_ONLY_USAGE}"
        )
    names: set[str] = set()
    for item in only:
        if not isinstance(item, str):
            raise TypeError(f"only= items must be str column names. {_ONLY_USAGE}")
        names.add(item)
    return names


def _relation_shadows(model_cls: type) -> dict[str, str | None]:
    """Relation field name → shadow column, if this table stores one."""
    shadows: dict[str, str | None] = {}
    for source in (
        getattr(model_cls, "ferro_relations", None),
        getattr(model_cls, "__ferro_relation_specs__", None),
        getattr(model_cls, "__ferro_reverse_specs__", None),
    ):
        if not source:
            continue
        for name, meta in source.items():
            if name in shadows and shadows[name] is not None:
                continue
            shadow = getattr(meta, "shadow_column", None)
            if not isinstance(shadow, str) and isinstance(meta, ForeignKey):
                shadow = f"{name}_id"
            if isinstance(shadow, str):
                shadows[name] = shadow
            else:
                shadows.setdefault(name, None)
    return shadows


def apply_save_only(
    instance: Any, payload: dict[str, Any], only: object
) -> dict[str, Any]:
    """Keep the PK plus ``only=`` columns; drop every other bind key.

    ``save_bind_payload`` stays a full dump. This is the allowlist filter.
    """
    names = normalize_save_only(only)
    model_cls = type(instance)
    columns = getattr(model_cls, "__ferro_columns__", {}) or {}
    pk = getattr(model_cls, "__ferro_pk__", None)
    legal = ", ".join(sorted(columns))
    relations = _relation_shadows(model_cls)

    for name in sorted(names):
        if name in relations:
            shadow = relations[name]
            extra = f" Use the shadow column {shadow!r}." if shadow else ""
            raise ValueError(
                f"{name!r} is a relation, not a persisted column.{extra} "
                f"Legal persisted columns: {legal}."
            )
        if name not in columns:
            raise ValueError(
                f"Unknown column {name!r} in save(only=...). "
                f"Legal persisted columns: {legal}."
            )

    write = {name for name in names if name != pk}
    if not write:
        raise ValueError(
            "save(only=...) produced an empty write-set "
            "(only=set() or only the primary key). "
            "Name at least one non-primary-key persisted column."
        )

    keep = set(write)
    if pk is not None:
        keep.add(pk)
    missing = keep - payload.keys()
    if missing:
        raise ValueError(
            "save(only=...) bind payload is missing "
            f"{sorted(missing)!r}. Legal persisted columns: {legal}."
        )
    return {key: payload[key] for key in keep}


def update_bind_payload(fields: Mapping[str, Any]) -> dict[str, Any]:
    """Column->value map for ``Query.update(**fields)``.

    Non-bytes values are canonicalized exactly as ``to_json`` does today; bytes
    values are overlaid raw.
    """
    non_bytes = {
        key: value
        for key, value in fields.items()
        if not isinstance(value, (bytes, bytearray))
    }
    canonical = json.loads(to_json(non_bytes)) if non_bytes else {}
    payload: dict[str, Any] = {}
    for key, value in fields.items():
        payload[key] = (
            bytes(value) if isinstance(value, (bytes, bytearray)) else canonical[key]
        )
    return payload
