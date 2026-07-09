"""SchemaIR compilation and fingerprint helpers for Phase 1.

This module compiles the canonical Ferro-enriched JSON schema metadata into
RFC-shaped SchemaIR envelopes and persists deterministic fingerprints for
individual models and full model sets.
"""

from __future__ import annotations

import json
from typing import Any

from .. import state as ferro_state
from .._core import (
    _ddl_check_constraint_name,
    _ddl_composite_index_name,
    _ddl_composite_unique_name,
    _ddl_fk_name,
    _ddl_single_index_name,
    _ddl_single_unique_name,
)
from ..schema_metadata import build_model_schema
from ..state import (
    _JOIN_TABLE_REGISTRY,
    _MODEL_REGISTRY_PY,
    _SCHEMA_IR_BY_MODEL,
)

_IR_VERSION = 1

# Test-only counter bumped at the single SchemaIR compile choke point (#245).
_SCHEMA_IR_COMPILE_COUNT_FOR_TEST = 0


def schema_ir_compile_count_for_test() -> int:
    """Return how many per-model SchemaIR compiles have run (test instrument)."""
    return _SCHEMA_IR_COMPILE_COUNT_FOR_TEST


def reset_schema_ir_compile_count_for_test() -> None:
    """Reset the compile counter (``clean_registry`` fixture)."""
    global _SCHEMA_IR_COMPILE_COUNT_FOR_TEST
    _SCHEMA_IR_COMPILE_COUNT_FOR_TEST = 0


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


def _is_nullable(col_name: str, col_info: dict[str, Any], required_fields: set[str]) -> bool:
    """Determine nullability from explicit Ferro hint or required-field fallback."""
    nullable_hint = col_info.get("ferro_nullable")
    if isinstance(nullable_hint, bool):
        return nullable_hint
    return col_name not in required_fields


def _column_ir(
    col_name: str, col_info: dict[str, Any], required_fields: set[str]
) -> dict[str, Any]:
    """Compile one schema property into a SchemaIR ``columns[]`` entry."""
    db_type_value = col_info.get("db_type")
    db_type_explicit = isinstance(db_type_value, str) and bool(db_type_value)
    is_pk = bool(col_info.get("primary_key", False))
    column_ir = {
        "name": col_name,
        "logical_type": _logical_type(col_info),
        # PK columns are NOT NULL by SQL semantics regardless of an Optional
        # annotation (used only for the autoincrement-default ergonomics);
        # clamping here keeps the IR truthful so the Alembic bridge and the
        # Rust emitter agree without per-consumer special cases (FF-B B5).
        "nullable": False if is_pk else _is_nullable(col_name, col_info, required_fields),
        "primary_key": is_pk,
        # A primary key auto-increments by default unless the field explicitly
        # disables it (e.g. a uuid PK sets autoincrement=False). Mirrors the
        # historical create-path default (build_column_plan used unwrap_or(true)
        # for PK columns); without it, a PK declared without an explicit
        # autoincrement (e.g. json_schema_extra={"primary_key": True}) renders as a
        # plain INTEGER on Postgres with no SERIAL and fails inserts. (#153)
        "autoincrement": bool(col_info.get("autoincrement", is_pk)),
        "unique": bool(col_info.get("unique", False)),
        "index": bool(col_info.get("index", False)),
        "default": col_info.get("default"),
        "format": col_info.get("format"),
    }
    enum_values = _enum_values(col_info)
    if isinstance(enum_values, list):
        column_ir["enum_values"] = list(enum_values)
    if db_type_explicit:
        column_ir["db_type"] = db_type_value
        column_ir["db_type_explicit"] = True
    enum_type_name = col_info.get("enum_type_name")
    if isinstance(enum_type_name, str) and enum_type_name:
        column_ir["enum_type_name"] = enum_type_name
    return column_ir


# Artifact names come from the single-source builders in ferro-ddl-lowering
# (via _core FFI) — including the 63-char truncation guards the old hand-rolled
# single-column helpers lacked. Do not re-implement name formats here
# (AGENTS.md § I-1; FF-B B3).


def _fk_name(table_name: str, col_name: str, to_table: str) -> str:
    """Canonical foreign-key constraint name (shared Rust builder)."""
    return _ddl_fk_name(table_name, col_name, to_table)


def _single_index_name(table_name: str, col_name: str) -> str:
    """Canonical single-column index name (shared Rust builder)."""
    return _ddl_single_index_name(table_name, col_name)


def _single_unique_name(table_name: str, col_name: str) -> str:
    """Canonical single-column unique name (shared Rust builder)."""
    return _ddl_single_unique_name(table_name, col_name)


def _composite_index_name(table_name: str, columns: list[str]) -> str:
    """Canonical composite index name (shared Rust builder)."""
    return _ddl_composite_index_name(table_name, columns)


def _composite_unique_name(table_name: str, columns: list[str]) -> str:
    """Canonical composite unique name (shared Rust builder)."""
    return _ddl_composite_unique_name(table_name, columns)


def _checks_from_columns(table_name: str, columns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compile per-column ``db_check`` markers into SchemaIR ``checks[]`` entries."""
    checks: list[dict[str, Any]] = []
    for col in columns:
        if col.get("db_check") is not True:
            continue
        col_name = col.get("name")
        if not isinstance(col_name, str) or not col_name:
            continue
        enum_values = _enum_values(col)
        if not isinstance(enum_values, list) or not enum_values:
            continue
        rendered: list[str] = []
        for value in enum_values:
            if isinstance(value, bool):
                rendered.append(str(value).lower())
            elif isinstance(value, (int, float)):
                rendered.append(str(value))
            else:
                escaped = str(value).replace("'", "''")
                rendered.append(f"'{escaped}'")
        checks.append(
            {
                "name": _ddl_check_constraint_name(table_name, col_name),
                "column": col_name,
                "values": rendered,
            }
        )
    return checks


def compile_schema_ir_payload(
    model_name: str,
    schema: dict[str, Any],
    *,
    table_name: str | None = None,
) -> dict[str, Any]:
    """Compile one model schema dict into a SchemaIR payload object.

    Args:
        model_name: Registered model class name.
        schema: Canonical Ferro-enriched model schema.

    Returns:
        A SchemaIR payload object ready to be wrapped in an IR envelope.
    """
    resolved_table_name = table_name or model_name.lower()
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        properties = {}
    required_fields = schema.get("required", [])
    required = set(required_fields) if isinstance(required_fields, list) else set()

    ordered_props = sorted(properties.items(), key=lambda item: item[0])
    resolved_columns: list[dict[str, Any]] = []
    for col_name, col_info in ordered_props:
        if not isinstance(col_info, dict):
            continue
        resolved = _resolve_ref(schema, col_info)
        resolved = _resolve_nested_refs(schema, resolved)
        resolved_with_name = {"name": col_name, **resolved}
        resolved_columns.append(resolved_with_name)

    columns = [
        _column_ir(col["name"], col, required)
        for col in resolved_columns
        if isinstance(col.get("name"), str)
    ]

    foreign_keys: list[dict[str, Any]] = []
    indexes: list[dict[str, Any]] = []
    uniques: list[dict[str, Any]] = []

    for col in resolved_columns:
        col_name = col.get("name")
        if not isinstance(col_name, str) or not col_name:
            continue
        fk = col.get("foreign_key")
        if isinstance(fk, dict):
            to_table = fk.get("to_table")
            if isinstance(to_table, str) and to_table:
                foreign_keys.append(
                    {
                        "column": col_name,
                        "to_table": to_table,
                        "to_column": "id",
                        "on_delete": fk.get("on_delete"),
                        "name": _fk_name(resolved_table_name, col_name, to_table),
                    }
                )
        if bool(col.get("index", False)):
            indexes.append(
                {
                    "name": _single_index_name(resolved_table_name, col_name),
                    "columns": [col_name],
                    "unique": False,
                }
            )
        if bool(col.get("unique", False)):
            uniques.append(
                {
                    "name": _single_unique_name(resolved_table_name, col_name),
                    "columns": [col_name],
                }
            )

    for composite in schema.get("ferro_composite_indexes") or []:
        if not isinstance(composite, list) or not composite:
            continue
        cols = [c for c in composite if isinstance(c, str) and c]
        if len(cols) != len(composite):
            continue
        indexes.append(
            {
                "name": _composite_index_name(resolved_table_name, cols),
                "columns": cols,
                "unique": False,
            }
        )

    for composite in schema.get("ferro_composite_uniques") or []:
        if not isinstance(composite, list) or not composite:
            continue
        cols = [c for c in composite if isinstance(c, str) and c]
        if len(cols) != len(composite):
            continue
        uniques.append(
            {"name": _composite_unique_name(resolved_table_name, cols), "columns": cols}
        )

    model_payload = {
        "model_name": model_name,
        "table_name": resolved_table_name,
        "columns": columns,
        "foreign_keys": sorted(
            foreign_keys,
            key=lambda item: (item["column"], item["to_table"], item["to_column"]),
        ),
        "indexes": sorted(indexes, key=lambda item: item["name"]),
        "uniques": sorted(uniques, key=lambda item: item["name"]),
        "checks": sorted(
            _checks_from_columns(resolved_table_name, resolved_columns),
            key=lambda item: item["name"],
        ),
    }
    return {"dialect_agnostic": True, "models": [model_payload]}


def _persist_schema_ir_envelope(model_name: str, envelope: dict[str, Any]) -> None:
    """Store one model's compiled SchemaIR envelope and fingerprint.

    Routes through the single ``ferro.state`` envelope-write entrypoint so every
    per-model store mutation lives at the store-owning layer (#243); the
    fingerprint is derived from the envelope inside that entrypoint.
    """
    ferro_state.persist_model_envelope(model_name, envelope)


def _compile_and_persist_model_envelope(
    model_name: str,
    schema: dict[str, Any],
    *,
    table_name: str | None = None,
    model_cls: type[Any] | None = None,
) -> dict[str, Any]:
    """Compile one model or join table to SchemaIR and persist its envelope.

    Single compile choke point for the test instrument and for every producer
    of per-model envelopes (#245).
    """
    global _SCHEMA_IR_COMPILE_COUNT_FOR_TEST
    _SCHEMA_IR_COMPILE_COUNT_FOR_TEST += 1
    resolved_table_name = table_name
    if resolved_table_name is None and model_cls is not None:
        resolved_table_name = getattr(model_cls, "__ferro_table__", None)
    payload = compile_schema_ir_payload(
        model_name,
        schema,
        table_name=resolved_table_name,
    )
    envelope = wrap_schema_ir(payload)
    _persist_schema_ir_envelope(model_name, envelope)
    return envelope


def register_model_with_ir(
    model_name: str,
    schema: dict[str, Any],
    table_name: str,
) -> dict[str, Any]:
    """Compile SchemaIR once and persist the envelope.

    Single registration bundle for metaclass, relationship resolution, join
    tables, and ``Model._reregister_ferro`` (#236). Rust registration is
    installed only via the bulk push seam (ADR #242 / #245).
    """
    return _compile_and_persist_model_envelope(
        model_name, schema, table_name=table_name
    )


def wrap_schema_ir(payload: dict[str, Any]) -> dict[str, Any]:
    """Wrap a SchemaIR payload with the standard IR envelope fields."""
    return {
        "ir_kind": "schema",
        "ir_version": _IR_VERSION,
        "payload": payload,
    }


def compile_model_schema_ir(
    model_name: str, model_cls: type[Any], schema: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Compile and persist a single model's SchemaIR envelope + fingerprint.

    Args:
        model_name: Registry key / model class name.
        model_cls: Python model class to compile.
        schema: Optional prebuilt canonical schema — avoids a redundant
            build_model_schema pass when the caller just built it (FF-E E3).

    Returns:
        The compiled SchemaIR envelope for ``model_cls``.
    """
    if schema is None:
        schema = build_model_schema(model_cls)
    return _compile_and_persist_model_envelope(
        model_name, schema, model_cls=model_cls
    )


def _model_payload_from_envelope(name: str) -> dict[str, Any]:
    """Return one model's SchemaIR payload object from the envelope cache.

    The returned dict is a live reference into ``_SCHEMA_IR_BY_MODEL`` — the
    assembled modelset shares these payload objects. Treat them as read-only;
    in-place mutation corrupts the per-model cache and survives clean-path
    re-assembles indefinitely (#245).
    """
    envelope = _SCHEMA_IR_BY_MODEL.get(name)
    if envelope is None:
        raise RuntimeError(
            f"Missing SchemaIR envelope for '{name}'. "
            "Compile the model (or run relationship resolution) before assembling "
            "the modelset."
        )
    return envelope["payload"]["models"][0]


def _assemble_modelset_envelope() -> dict[str, Any]:
    """Stitch per-model envelopes into one sorted modelset envelope."""
    models: list[dict[str, Any]] = []
    for model_name, _model_cls in sorted(_MODEL_REGISTRY_PY.items(), key=lambda item: item[0]):
        models.append(_model_payload_from_envelope(model_name))

    for table_name, table_schema in sorted(
        _JOIN_TABLE_REGISTRY.items(), key=lambda item: item[0]
    ):
        if not isinstance(table_schema, dict):
            continue
        models.append(_model_payload_from_envelope(table_name))

    envelope = {
        "ir_kind": "schema",
        "ir_version": _IR_VERSION,
        "payload": {
            "dialect_agnostic": True,
            "models": models,
        },
    }

    ferro_state._SCHEMA_IR_MODELSET = envelope
    ferro_state._SCHEMA_IR_MODELSET_FINGERPRINT = ferro_state.ir_fingerprint(envelope)
    return envelope


def _missing_registry_envelope_names() -> list[str]:
    missing: list[str] = []
    for name in _MODEL_REGISTRY_PY:
        if name not in _SCHEMA_IR_BY_MODEL:
            missing.append(name)
    for table_name, table_schema in _JOIN_TABLE_REGISTRY.items():
        if isinstance(table_schema, dict) and table_name not in _SCHEMA_IR_BY_MODEL:
            missing.append(table_name)
    return missing


def _recompile_missing_registry_envelopes() -> None:
    for model_name in _missing_registry_envelope_names():
        model_cls = _MODEL_REGISTRY_PY.get(model_name)
        if model_cls is not None:
            compile_model_schema_ir(model_name, model_cls)
            continue
        table_schema = _JOIN_TABLE_REGISTRY.get(model_name)
        if isinstance(table_schema, dict):
            register_model_with_ir(model_name, table_schema, model_name)


def _recompile_all_registered_envelopes() -> None:
    """Recompile every registered model and join-table envelope.

    Used when ``compile_registry_schema_ir()`` is invoked on a dirty registry
    without going through ``resolve_relationships()`` — direct callers (tests,
    Alembic-adjacent tooling) historically relied on compile-not-assemble here.
    The connect/reconnect clean path never hits this (#245).
    """
    for model_name, model_cls in sorted(_MODEL_REGISTRY_PY.items(), key=lambda item: item[0]):
        compile_model_schema_ir(model_name, model_cls)
    for table_name, table_schema in sorted(
        _JOIN_TABLE_REGISTRY.items(), key=lambda item: item[0]
    ):
        if isinstance(table_schema, dict):
            register_model_with_ir(table_name, table_schema, table_name)


def compile_registry_schema_ir() -> dict[str, Any]:
    """Assemble and persist a deterministic SchemaIR envelope for all models.

    Stitches already-compiled per-model envelopes from ``_SCHEMA_IR_BY_MODEL``
    for every entry in ``_MODEL_REGISTRY_PY`` and ``_JOIN_TABLE_REGISTRY``.
    On a dirty registry, recompiles envelopes first so direct callers that
    bypass ``resolve_relationships()`` still observe current registry state.
    After ``ensure_resolved_modelset()`` on the clean path, this is
    assemble-only (#245).

    Returns:
        The assembled model-set SchemaIR envelope, sorted by model name.
    """
    if ferro_state.is_modelset_dirty():
        _recompile_all_registered_envelopes()
    else:
        _recompile_missing_registry_envelopes()
    return _assemble_modelset_envelope()


def schema_ir_fingerprint(ir_envelope: dict[str, Any]) -> str:
    """Return a deterministic SHA-256 fingerprint for a SchemaIR envelope."""
    return ferro_state.ir_fingerprint(ir_envelope)
