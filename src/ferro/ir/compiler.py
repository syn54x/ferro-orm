from __future__ import annotations

import hashlib
import json
from typing import Any

from ..schema_metadata import build_model_schema
from ..state import (
    _MODEL_REGISTRY_PY,
    _SCHEMA_IR_BY_MODEL,
    _SCHEMA_IR_FINGERPRINT_BY_MODEL,
    _SCHEMA_IR_MODELSET,
    _SCHEMA_IR_MODELSET_FINGERPRINT,
)

_IR_VERSION = 1


def _canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def _fingerprint(value: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _resolve_ref(schema: dict[str, Any], col_info: dict[str, Any]) -> dict[str, Any]:
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


def _logical_type(col_info: dict[str, Any]) -> str:
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
        return "string"
    return "unknown"


def _default_db_type(col_info: dict[str, Any]) -> str:
    field_type, field_format = _effective_type_and_format(col_info)
    if field_type == "integer":
        return "bigint"
    if field_type == "number":
        return "text"
    if field_type == "string":
        if field_format == "date-time":
            return "timestamptz"
        if field_format == "date":
            return "date"
        if field_format == "time":
            return "time"
        if field_format == "uuid":
            return "uuid"
        return "text"
    return "text"


def _effective_type_and_format(col_info: dict[str, Any]) -> tuple[Any, Any]:
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
            return candidate_type, candidate.get("format")
    return field_type, field_format


def _is_nullable(col_name: str, col_info: dict[str, Any], required_fields: set[str]) -> bool:
    nullable_hint = col_info.get("ferro_nullable")
    if isinstance(nullable_hint, bool):
        return nullable_hint
    return col_name not in required_fields


def _column_ir(
    col_name: str, col_info: dict[str, Any], required_fields: set[str]
) -> dict[str, Any]:
    return {
        "name": col_name,
        "logical_type": _logical_type(col_info),
        "db_type": col_info.get("db_type") or _default_db_type(col_info),
        "nullable": _is_nullable(col_name, col_info, required_fields),
        "primary_key": bool(col_info.get("primary_key", False)),
        "autoincrement": bool(col_info.get("autoincrement", False)),
        "unique": bool(col_info.get("unique", False)),
        "index": bool(col_info.get("index", False)),
        "default": col_info.get("default"),
        "format": col_info.get("format"),
    }


def _fk_name(table_name: str, col_name: str, to_table: str) -> str:
    return f"fk_{table_name}_{col_name}_{to_table}"


def _single_index_name(table_name: str, col_name: str) -> str:
    return f"idx_{table_name}_{col_name}"


def _single_unique_name(table_name: str, col_name: str) -> str:
    return f"uq_{table_name}_{col_name}"


def _composite_index_name(table_name: str, columns: list[str]) -> str:
    return f"idx_{table_name}_{'_'.join(columns)}"


def _composite_unique_name(table_name: str, columns: list[str]) -> str:
    return f"uq_{table_name}_{'_'.join(columns)}"


def _checks_from_columns(table_name: str, columns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for col in columns:
        if col.get("db_check") is not True:
            continue
        col_name = col.get("name")
        if not isinstance(col_name, str) or not col_name:
            continue
        checks.append(
            {
                "name": f"ck_{table_name}_{col_name}",
                "expression": f"{col_name} IS NOT NULL",
            }
        )
    return checks


def compile_schema_ir_payload(model_name: str, schema: dict[str, Any]) -> dict[str, Any]:
    table_name = model_name.lower()
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
                        "name": _fk_name(table_name, col_name, to_table),
                    }
                )
        if bool(col.get("index", False)):
            indexes.append(
                {
                    "name": _single_index_name(table_name, col_name),
                    "columns": [col_name],
                    "unique": False,
                }
            )
        if bool(col.get("unique", False)):
            uniques.append(
                {
                    "name": _single_unique_name(table_name, col_name),
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
                "name": _composite_index_name(table_name, cols),
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
        uniques.append({"name": _composite_unique_name(table_name, cols), "columns": cols})

    model_payload = {
        "model_name": model_name,
        "table_name": table_name,
        "columns": columns,
        "foreign_keys": sorted(
            foreign_keys,
            key=lambda item: (item["column"], item["to_table"], item["to_column"]),
        ),
        "indexes": sorted(indexes, key=lambda item: item["name"]),
        "uniques": sorted(uniques, key=lambda item: item["name"]),
        "checks": sorted(
            _checks_from_columns(table_name, resolved_columns),
            key=lambda item: item["name"],
        ),
    }
    return {"dialect_agnostic": True, "models": [model_payload]}


def wrap_schema_ir(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "ir_kind": "schema",
        "ir_version": _IR_VERSION,
        "payload": payload,
    }


def compile_model_schema_ir(model_name: str, model_cls: type[Any]) -> dict[str, Any]:
    schema = build_model_schema(model_cls)
    payload = compile_schema_ir_payload(model_name, schema)
    envelope = wrap_schema_ir(payload)
    _SCHEMA_IR_BY_MODEL[model_name] = envelope
    _SCHEMA_IR_FINGERPRINT_BY_MODEL[model_name] = _fingerprint(envelope)
    return envelope


def compile_registry_schema_ir() -> dict[str, Any]:
    models: list[dict[str, Any]] = []
    for model_name, model_cls in sorted(_MODEL_REGISTRY_PY.items(), key=lambda item: item[0]):
        if model_name == "Model":
            continue
        model_envelope = compile_model_schema_ir(model_name, model_cls)
        model_payload = model_envelope["payload"]["models"][0]
        models.append(model_payload)

    envelope = {
        "ir_kind": "schema",
        "ir_version": _IR_VERSION,
        "payload": {
            "dialect_agnostic": True,
            "models": models,
        },
    }

    global _SCHEMA_IR_MODELSET, _SCHEMA_IR_MODELSET_FINGERPRINT
    _SCHEMA_IR_MODELSET = envelope
    _SCHEMA_IR_MODELSET_FINGERPRINT = _fingerprint(envelope)
    return envelope


def schema_ir_fingerprint(ir_envelope: dict[str, Any]) -> str:
    return _fingerprint(ir_envelope)

