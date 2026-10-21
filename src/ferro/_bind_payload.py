"""Marshal row values for the typed codec bind path (#162).

Produces a per-column value map where ``bytes``/``bytearray`` are preserved
verbatim (so non-UTF-8 binary survives) and every other value is canonicalized
exactly as pydantic's JSON mode produces it. This replaces the ``model_dump_json``
/ ``to_json`` string envelope in ``Model.save`` / ``bulk_create`` / ``Query.update``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Iterator
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping

from pydantic import SecretBytes, SecretStr
from pydantic_core import to_json

from .base import ForeignKey
from .columns import retired_clause

_SAVE_COLUMN_CONTAINERS = (set, frozenset, list, tuple)
_SAVE_COLUMN_USAGE = {
    "only": 'Use only={"messages"}.',
    "exclude": 'Use exclude={"turns"}.',
}


def unwrap_secret(value: Any) -> Any:
    """Return the value a `SecretStr` / `SecretBytes` hides, or `value` as is.

    The one place every write door (``save``, ``bulk_create``, ``update`` and
    query literals) turns a secret into what the database stores: a secret
    type only protects display, so the column holds the real value. An
    unwrapped ``SecretBytes`` is ``bytes`` and takes the bytes path.
    """
    if isinstance(value, (SecretStr, SecretBytes)):
        return value.get_secret_value()
    return value


def secret_values_in(values: Iterable[object]) -> tuple[object, ...]:
    """Return what each `SecretStr` / `SecretBytes` among `values` hides.

    The one collector of an operation's secret values: a query's compile, a
    write and a raw statement each call it once. The statement log and a
    rendered statement mask a bound parameter equal to any of these.

    Args:
        values: The values about to be bound. A list, tuple or set among them
            is searched too, so a secret inside an `IN` list is found.

    Returns:
        The unwrapped value of every secret in `values`, in order; empty when
        there is none.
    """
    found: list[object] = []
    pending = list(values)
    pending.reverse()
    while pending:
        value = pending.pop()
        if isinstance(value, (SecretStr, SecretBytes)):
            found.append(unwrap_secret(value))
        elif isinstance(value, (list, tuple, set, frozenset)):
            pending.extend(reversed(list(value)))
    return tuple(found)


def deferred_secret_values(values: Iterable[object]) -> Iterator[object]:
    """Yield the secret values in `values`, collected on first read.

    A write or a raw statement hands this to the engine, which reads it only
    when the statement log is on: the engine's one read of the log's switch
    decides whether anything is collected.

    Args:
        values: The values about to be bound, read lazily.

    Yields:
        The unwrapped value of every secret in `values`, in order.
    """
    yield from secret_values_in(values)


def field_values(instances: Iterable[Any]) -> Iterator[object]:
    """Yield every field value of each model instance, in field order.

    Args:
        instances: The model instances about to be saved.

    Yields:
        Each instance's field values, as the instance holds them.
    """
    for instance in instances:
        for name in type(instance).model_fields:
            yield getattr(instance, name, None)


def _raw_bound_fields(instance: Any) -> dict[str, Any]:
    """Fields that bind raw rather than through the JSON dump, with that value.

    Value-driven, so ``bytes``, ``bytes | None`` and ``Any``-typed bytes are
    caught (as ``bytes``), and so is every secret (as its unwrapped value): a
    JSON dump would write its mask, and a field serializer whatever it returns.
    """
    raw: dict[str, Any] = {}
    for name in type(instance).model_fields:
        value = getattr(instance, name, None)
        unwrapped = unwrap_secret(value)
        if isinstance(unwrapped, (bytes, bytearray)):
            raw[name] = bytes(unwrapped)
        elif unwrapped is not value:
            raw[name] = unwrapped
    return raw


def canonicalize_wire_scalar(value: Any) -> Any:
    """Canonical JSON form for query and ``update()`` literals (I-13).

    A secret is unwrapped first (``unwrap_secret``), so a literal binds what
    ``save()`` wrote. ``datetime`` / ``date`` / ``time`` / UUID / Decimal go
    through pydantic ``to_json`` so they match ``save_bind_payload``.
    Containers recurse. Bytes, ``Enum``, int, str, bool, and ``None`` are
    unchanged. Any other object with ``isoformat`` raises — no silent
    ``datetime.isoformat()`` spelling (``…+00:00`` vs ``…Z`` on SQLite TEXT).
    """
    value = unwrap_secret(value)
    if isinstance(value, (bytes, bytearray)):
        return value
    if isinstance(value, Enum):
        return value
    if isinstance(value, (datetime, date, time, uuid.UUID, Decimal)):
        return json.loads(to_json(value))
    if isinstance(value, (list, tuple, set)):
        return [canonicalize_wire_scalar(item) for item in value]
    if isinstance(value, dict):
        return {key: canonicalize_wire_scalar(item) for key, item in value.items()}
    if hasattr(value, "isoformat"):
        raise TypeError(
            f"{type(value).__name__} is not a query/update literal; "
            "datetime, date, and time are canonicalized. "
            "Other isoformat objects are rejected."
        )
    return value


def save_bind_payload(instance: Any) -> dict[str, Any]:
    """Column->value map for ``save``/``bulk_create``, in field order.

    Columns go through pydantic ``model_dump(mode="json")`` (honoring field
    serializers/aliases), except bytes and secret columns, which are bound
    raw: bytes verbatim, a secret as ``unwrap_secret`` gives it.

    The map is in field declaration order, so a statement lists its columns
    the same way on every call: the dump follows the instance's ``__dict__``,
    which a fetch fills in the ``SELECT``'s column order, and the raw columns
    are bound apart from it.
    """
    raw = _raw_bound_fields(instance)
    payload: dict[str, Any] = instance.model_dump(mode="json", exclude=set(raw))
    ordered: dict[str, Any] = {}
    for name in type(instance).model_fields:
        if name in raw:
            ordered[name] = raw[name]
        elif name in payload:
            ordered[name] = payload[name]
    for name, value in payload.items():
        ordered.setdefault(name, value)
    return ordered


def normalize_save_columns(names: object, *, param: str) -> set[str]:
    """Collapse ``only=`` / ``exclude=`` to a set of column names.

    Bare ``str`` / ``bytes`` is rejected so ``only="messages"`` is not
    treated as an iterable of characters. The usage hint names the
    offending keyword.
    """
    usage = _SAVE_COLUMN_USAGE[param]
    if isinstance(names, (str, bytes)):
        raise TypeError(
            f"{param}= must be a set, frozenset, list, or tuple of column names, "
            f"not a string. {usage}"
        )
    if not isinstance(names, _SAVE_COLUMN_CONTAINERS):
        raise TypeError(
            f"{param}= must be a set, frozenset, list, or tuple of column names. "
            f"{usage}"
        )
    out: set[str] = set()
    for item in names:
        if not isinstance(item, str):
            raise TypeError(f"{param}= items must be str column names. {usage}")
        out.add(item)
    return out


def normalize_save_only(only: object) -> set[str]:
    """Collapse ``only=`` to a set of column names (set semantics)."""
    return normalize_save_columns(only, param="only")


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


def _validate_save_column_names(
    model_cls: type, names: set[str], *, param: str
) -> dict[str, Any]:
    """Reject relation / unknown names. Returns ``__ferro_columns__``."""
    columns = getattr(model_cls, "__ferro_columns__", {}) or {}
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
                f"Unknown column {name!r} in save({param}=...)."
                f"{retired_clause(model_cls, name)} "
                f"Legal persisted columns: {legal}."
            )
    return columns


def _keep_write_set(
    payload: dict[str, Any],
    write: set[str],
    *,
    pk: str | None,
    param: str,
    legal: str,
) -> dict[str, Any]:
    keep = set(write)
    if pk is not None:
        keep.add(pk)
    missing = keep - payload.keys()
    if missing:
        raise ValueError(
            f"save({param}=...) bind payload is missing "
            f"{sorted(missing)!r}. Legal persisted columns: {legal}."
        )
    # The payload is in field order; a set's order changes with the hash seed.
    return {key: value for key, value in payload.items() if key in keep}


def apply_save_only(
    instance: Any, payload: dict[str, Any], only: object
) -> dict[str, Any]:
    """Keep the PK plus ``only=`` columns; drop every other bind key.

    ``save_bind_payload`` stays a full dump. This is the allowlist filter.
    """
    names = normalize_save_columns(only, param="only")
    model_cls = type(instance)
    columns = _validate_save_column_names(model_cls, names, param="only")
    pk = getattr(model_cls, "__ferro_pk__", None)
    legal = ", ".join(sorted(columns))

    write = {name for name in names if name != pk}
    if not write:
        raise ValueError(
            "save(only=...) produced an empty write-set "
            "(only=set() or only the primary key). "
            "Name at least one non-primary-key persisted column."
        )
    return _keep_write_set(payload, write, pk=pk, param="only", legal=legal)


def apply_save_exclude(
    instance: Any, payload: dict[str, Any], exclude: object
) -> dict[str, Any]:
    """Drop ``exclude=`` columns from the bind payload (PK is never SET).

    ``exclude=set()`` is a full write: the payload is returned unchanged.
    """
    names = normalize_save_columns(exclude, param="exclude")
    if not names:
        return payload

    model_cls = type(instance)
    columns = _validate_save_column_names(model_cls, names, param="exclude")
    pk = getattr(model_cls, "__ferro_pk__", None)
    legal = ", ".join(sorted(columns))

    write = {name for name in columns if name not in names and name != pk}
    if not write:
        raise ValueError(
            "save(exclude=...) produced an empty write-set "
            "(every non-primary-key column was excluded). "
            "Leave at least one persisted column to write."
        )
    return _keep_write_set(payload, write, pk=pk, param="exclude", legal=legal)


def update_bind_payload(fields: Mapping[str, Any]) -> dict[str, Any]:
    """Column->value map for ``Query.update(**fields)``.

    Every value goes through ``canonicalize_wire_scalar`` (which unwraps a
    secret as ``save()`` binds it), then dict-level ``to_json`` (enums still
    stringify on this door). Bytes, an unwrapped ``SecretBytes`` included, are
    overlaid raw.
    """
    scalars = {key: canonicalize_wire_scalar(value) for key, value in fields.items()}
    non_bytes = {
        key: value
        for key, value in scalars.items()
        if not isinstance(value, (bytes, bytearray))
    }
    canonical = json.loads(to_json(non_bytes)) if non_bytes else {}
    payload: dict[str, Any] = {}
    for key, value in scalars.items():
        payload[key] = (
            bytes(value) if isinstance(value, (bytes, bytearray)) else canonical[key]
        )
    return payload
