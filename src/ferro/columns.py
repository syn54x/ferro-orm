"""Column specs: each column fact derived exactly once (CONTEXT.md "Column spec").

One frozen ``ColumnSpec`` per column, built from the field declaration at
class-body time (provisional) and replaced during relationship resolution
(resolved). The SchemaIR compiler renders specs; runtime consumers read
``cls.__ferro_columns__`` at use time.
"""

from __future__ import annotations

import types
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, ForwardRef, Union, get_args, get_origin, get_type_hints
from uuid import UUID

from ._annotation_utils import (
    annotation_allows_none,
    annotation_is_decimal,
    enum_subclass_from_annotation,
)
from ._shadow_fk_types import _scalar_part_of_annotation
from .base import ForeignKey, foreign_key_allows_none

__all__ = ["ColumnSpec", "ForeignKeyRef", "build_column_specs", "fk_shadow_spec", "pk_spec"]


@dataclass(frozen=True, slots=True)
class ForeignKeyRef:
    to_table: str
    on_delete: str | None


@dataclass(frozen=True, slots=True)
class ColumnSpec:
    name: str
    logical_type: str
    nullable: bool
    primary_key: bool
    autoincrement: bool
    unique: bool
    index: bool
    default: Any
    format: str | None
    python_type: Any
    enum_values: tuple[Any, ...] | None = None
    enum_type_name: str | None = None
    enum_class: type[Enum] | None = None
    db_type: str | None = None
    db_type_explicit: bool = False
    db_check: bool = False
    foreign_key: ForeignKeyRef | None = None


def _derive_autoincrement(
    explicit: bool | None, primary_key: bool, integer_typed: bool
) -> bool:
    """Single derivation rule for autoincrement (ADR-0002).

    Explicit declaration wins; otherwise a primary key auto-increments iff it
    is integer-typed. Supersedes the raw-path ``pk``-only fallback from #153.
    """
    if explicit is not None:
        return explicit
    return primary_key and integer_typed


def _derive_nullable(
    *,
    explicit: bool | None,
    primary_key: bool,
    fk_nullable: bool | None,
    allows_none: bool,
) -> bool:
    """Single derivation site for nullability (PK clamp included, FF-B B5)."""
    if primary_key:
        return False
    if fk_nullable is not None:
        return fk_nullable
    if explicit is not None:
        return explicit
    return allows_none


def _property_is_integer(prop: dict[str, Any]) -> bool:
    return prop.get("type") == "integer" or any(
        item.get("type") == "integer" for item in prop.get("anyOf", [])
    )


def _target_table_name(target: Any) -> str:
    from .state import resolve_model_reference

    if isinstance(target, ForwardRef):
        target = target.__forward_arg__
    if isinstance(target, str):
        try:
            model = resolve_model_reference(target, default=None)
        except RuntimeError:
            # Ambiguous short ref during first-pass schema build: defer the
            # loud, candidate-listing error to resolve_relationships (the
            # authoritative resolution point), same as a not-yet-defined
            # forward ref. Falling back here keeps the single-resolution-site
            # invariant instead of raising twice from two different phases.
            model = None
        if model is not None:
            return model.__ferro_table__
        # Provisional first-pass fallback for a not-yet-defined forward ref:
        # resolve_relationships' second pass (loud since FF-E E4) re-registers
        # with the target's real table before any DDL consumer runs.
        return target.lower()
    table = getattr(target, "__ferro_table__", None)
    if isinstance(table, str):
        return table
    if hasattr(target, "__name__"):
        return target.__name__.lower()
    return str(target).lower()


def _resolve_ref(schema: dict[str, Any], col_info: dict[str, Any]) -> dict[str, Any]:
    """Inline a local ``#/$defs/...`` reference into a property schema."""
    ref_path = col_info.get("$ref")
    if not isinstance(ref_path, str):
        return col_info
    if not ref_path.startswith("#/$defs/"):
        return col_info
    def_name = ref_path.split("/")[-1]
    resolved = schema.get("$defs", {}).get(def_name)
    if not isinstance(resolved, dict):
        return col_info
    return {
        **resolved,
        **{k: v for k, v in col_info.items() if k != "$ref"},
    }


def _resolve_nested_refs(schema: dict[str, Any], col_info: dict[str, Any]) -> dict[str, Any]:
    """Resolve local refs in one-level nested ``anyOf`` entries."""
    any_of = col_info.get("anyOf")
    if not isinstance(any_of, list):
        return col_info
    resolved_any_of: list[Any] = []
    changed = False
    for candidate in any_of:
        if not isinstance(candidate, dict):
            resolved_any_of.append(candidate)
            continue
        resolved_candidate = _resolve_ref(schema, candidate)
        if resolved_candidate is not candidate:
            changed = True
        resolved_any_of.append(resolved_candidate)
    if not changed:
        return col_info
    return {**col_info, "anyOf": resolved_any_of}


def _logical_type(col_info: dict[str, Any]) -> str:
    """Map schema type metadata to SchemaIR ``logical_type``."""
    field_type, field_format = _effective_type_and_format(col_info)
    if field_type == "integer":
        return "integer"
    if field_type == "number":
        return "decimal" if field_format == "decimal" else "number"
    if field_type == "boolean":
        return "boolean"
    if field_type == "string":
        if field_format == "date-time":
            return "datetime"
        if field_format == "date":
            return "date"
        if field_format == "time":
            return "time"
        if field_format == "uuid":
            return "uuid"
        if field_format == "binary":
            return "binary"
        return "string"
    if field_type in {"object", "array"}:
        return "json"
    return "unknown"



def _effective_type_and_format(col_info: dict[str, Any]) -> tuple[Any, Any]:
    """Resolve concrete type/format from direct fields or ``anyOf`` unions."""
    field_type = col_info.get("type")
    field_format = col_info.get("format")
    if field_type is not None:
        return field_type, field_format
    any_of = col_info.get("anyOf")
    if isinstance(any_of, list):
        for candidate in any_of:
            if not isinstance(candidate, dict):
                continue
            candidate_type = candidate.get("type")
            if candidate_type is None or candidate_type == "null":
                continue
            return candidate_type, candidate.get("format") or field_format
    return field_type, field_format


def _enum_values(col_info: dict[str, Any]) -> list[Any] | None:
    direct = col_info.get("enum")
    if isinstance(direct, list):
        return direct
    any_of = col_info.get("anyOf")
    if isinstance(any_of, list):
        for candidate in any_of:
            if not isinstance(candidate, dict):
                continue
            enum_values = candidate.get("enum")
            if isinstance(enum_values, list):
                return enum_values
    return None


def build_column_specs(model_cls: type[Any]) -> dict[str, ColumnSpec]:
    """Build one ColumnSpec per schema property, in model-field order.

    Reads Pydantic's ``model_json_schema()`` exactly once — the only place in
    Ferro that consumes it (grilling Q2). Returns a plain dict; callers store
    it on ``cls.__ferro_columns__`` and must replace, never mutate.
    """
    schema = model_cls.model_json_schema()
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return {}
    required_raw = schema.get("required", [])
    required = set(required_raw) if isinstance(required_raw, list) else set()
    model_fields = getattr(model_cls, "model_fields", {})
    ferro_fields = getattr(model_cls, "ferro_fields", {}) or {}
    ferro_relations = getattr(model_cls, "ferro_relations", {}) or {}
    try:
        resolved_annotations = get_type_hints(model_cls, include_extras=True)
    except Exception:
        resolved_annotations = {}

    fk_by_shadow = {
        f"{fname}_id": meta
        for fname, meta in ferro_relations.items()
        if isinstance(meta, ForeignKey)
    }

    specs: dict[str, ColumnSpec] = {}
    for field_name, raw_prop in properties.items():
        if not isinstance(raw_prop, dict):
            continue
        prop = _resolve_nested_refs(schema, _resolve_ref(schema, raw_prop))

        field_info = model_fields.get(field_name)
        ann = resolved_annotations.get(
            field_name, field_info.annotation if field_info is not None else None
        )
        if isinstance(ann, str):
            ann = field_info.annotation if field_info is not None else None
        python_type = _scalar_part_of_annotation(ann) if ann is not None else None

        declared = ferro_fields.get(field_name)
        fk_meta = fk_by_shadow.get(field_name)

        is_pk = (
            declared.primary_key
            if declared is not None
            else bool(prop.get("primary_key", False))
        )
        raw_autoincrement = prop.get("autoincrement")
        explicit_autoincrement = (
            declared.autoincrement
            if declared is not None
            else (raw_autoincrement if isinstance(raw_autoincrement, bool) else None)
        )
        autoincrement = _derive_autoincrement(
            explicit_autoincrement, is_pk, _property_is_integer(prop)
        )

        if fk_meta is not None:
            unique = bool(fk_meta.unique)
            index = bool(fk_meta.index)
            fk_ref = ForeignKeyRef(
                to_table=_target_table_name(fk_meta.to),
                on_delete=fk_meta.on_delete,
            )
            fk_nullable = foreign_key_allows_none(fk_meta)
        else:
            unique = (
                bool(declared.unique)
                if declared is not None
                else bool(prop.get("unique", False))
            )
            index = (
                bool(declared.index)
                if declared is not None
                else bool(prop.get("index", False))
            )
            fk_ref = None
            fk_nullable = None

        if declared is not None and isinstance(declared.nullable, bool):
            explicit_nullable: bool | None = declared.nullable
        else:
            raw_nullable = prop.get("ferro_nullable")
            explicit_nullable = raw_nullable if isinstance(raw_nullable, bool) else None
        allows_none = (
            annotation_allows_none(field_info.annotation)
            if field_info is not None
            else field_name not in required
        )
        nullable = _derive_nullable(
            explicit=explicit_nullable,
            primary_key=is_pk,
            fk_nullable=fk_nullable,
            allows_none=allows_none,
        )

        if declared is not None:
            db_type = declared.db_type
            db_check = bool(declared.db_check)
        else:
            raw_db_type = prop.get("db_type")
            db_type = raw_db_type if isinstance(raw_db_type, str) and raw_db_type else None
            db_check = prop.get("db_check") is True

        fmt = "decimal" if annotation_is_decimal(ann) else prop.get("format")
        effective_prop = {**prop, "format": fmt} if fmt != prop.get("format") else prop

        enum_cls = enum_subclass_from_annotation(ann)
        if enum_cls is not None:
            enum_type_name: str | None = enum_cls.__name__.lower()
        else:
            raw_enum_name = prop.get("enum_type_name")
            enum_type_name = (
                raw_enum_name
                if isinstance(raw_enum_name, str) and raw_enum_name
                else None
            )
        enum_values = _enum_values(prop)

        specs[field_name] = ColumnSpec(
            name=field_name,
            logical_type=_logical_type(effective_prop),
            nullable=nullable,
            primary_key=is_pk,
            autoincrement=autoincrement,
            unique=unique,
            index=index,
            default=prop.get("default"),
            format=fmt,
            python_type=python_type,
            enum_values=tuple(enum_values) if isinstance(enum_values, list) else None,
            enum_type_name=enum_type_name,
            enum_class=enum_cls,
            db_type=db_type,
            db_type_explicit=bool(db_type),
            db_check=db_check,
            foreign_key=fk_ref,
        )
    return specs


_PK_TYPE_FACTS: dict[Any, tuple[str, str | None]] = {
    int: ("integer", None),
    str: ("string", None),
    UUID: ("uuid", "uuid"),
    float: ("number", None),
    bool: ("boolean", None),
    bytes: ("binary", "binary"),
}


def pk_spec(name: str, python_type: Any) -> ColumnSpec:
    """Spec for a column typed like a primary key (join-table columns).

    Mirrors the historical ``schema_fragment_for_pk`` mapping, including the
    fall-back-to-string for None/unknown types.
    """
    scalar = _scalar_part_of_annotation(python_type) if python_type is not None else None
    origin = get_origin(scalar)
    if origin is Union or origin is types.UnionType:
        non_none = [a for a in get_args(scalar) if a is not type(None)]
        if len(non_none) == 1:
            scalar = _scalar_part_of_annotation(non_none[0])
    logical_type, fmt = _PK_TYPE_FACTS.get(scalar, ("string", None))
    return ColumnSpec(
        name=name,
        logical_type=logical_type,
        nullable=False,
        primary_key=False,
        autoincrement=False,
        unique=False,
        index=False,
        default=None,
        format=fmt,
        python_type=scalar,
    )


def fk_shadow_spec(
    name: str,
    *,
    python_type: Any,
    to_table: str,
    on_delete: str | None = "CASCADE",
    unique: bool = False,
    index: bool = False,
    nullable: bool = False,
) -> ColumnSpec:
    """FK column spec at a known target — join tables and resolve-time rebuilds."""
    return replace(
        pk_spec(name, python_type),
        foreign_key=ForeignKeyRef(to_table=to_table, on_delete=on_delete),
        unique=unique,
        index=index,
        nullable=nullable,
    )
