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


def update_bind_payload(fields: Mapping[str, Any]) -> dict[str, Any]:
    """Column->value map for ``Query.update(**fields)``.

    Non-bytes values are canonicalized exactly as ``to_json`` does today; bytes
    values are overlaid raw.
    """
    bytes_keys = {k for k, v in fields.items() if isinstance(v, (bytes, bytearray))}
    non_bytes = {k: v for k, v in fields.items() if k not in bytes_keys}
    payload: dict[str, Any] = json.loads(to_json(non_bytes)) if non_bytes else {}
    for k in bytes_keys:
        payload[k] = bytes(fields[k])
    return payload
