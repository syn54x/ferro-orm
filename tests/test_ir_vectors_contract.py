from __future__ import annotations

import json
from pathlib import Path
from typing import Any


VECTORS_DIR = Path(__file__).parent / "fixtures" / "ir_vectors"
SUPPORTED_DOMAINS = {"schema", "query", "codec"}
SUPPORTED_IR_VERSION = 1
QUERY_OPERATORS = {"==", "!=", "<", "<=", ">", ">=", "IN", "LIKE", "AND", "OR"}


def _load_vectors() -> list[tuple[Path, dict[str, Any]]]:
    loaded: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(VECTORS_DIR.glob("*.json")):
        loaded.append((path, json.loads(path.read_text(encoding="utf-8"))))
    return loaded


def _require_keys(obj: dict[str, Any], required: set[str], label: str) -> None:
    missing = required - set(obj.keys())
    assert not missing, f"{label} missing keys: {sorted(missing)}"


def _validate_query_node(node: dict[str, Any], label: str) -> None:
    _require_keys(node, {"node_kind", "operator"}, label)
    node_kind = node["node_kind"]
    operator = node["operator"]
    assert node_kind in {"leaf", "compound"}, f"{label}.node_kind invalid: {node_kind!r}"
    assert operator in QUERY_OPERATORS, f"{label}.operator invalid: {operator!r}"

    if node_kind == "leaf":
        _require_keys(node, {"column", "value"}, label)
        assert isinstance(node["column"], str) and node["column"], (
            f"{label}.column must be non-empty string"
        )
        value = node["value"]
        assert isinstance(value, dict), f"{label}.value must be object"
        _require_keys(value, {"kind", "value"}, f"{label}.value")
        return

    _require_keys(node, {"left", "right"}, label)
    assert isinstance(node["left"], dict), f"{label}.left must be object"
    assert isinstance(node["right"], dict), f"{label}.right must be object"
    _validate_query_node(node["left"], f"{label}.left")
    _validate_query_node(node["right"], f"{label}.right")


def _validate_schema_payload(payload: dict[str, Any], label: str) -> None:
    _require_keys(payload, {"dialect_agnostic", "models"}, label)
    assert isinstance(payload["dialect_agnostic"], bool), (
        f"{label}.dialect_agnostic must be bool"
    )
    models = payload["models"]
    assert isinstance(models, list) and models, f"{label}.models must be non-empty list"
    for i, model in enumerate(models):
        model_label = f"{label}.models[{i}]"
        assert isinstance(model, dict), f"{model_label} must be object"
        _require_keys(
            model,
            {"model_name", "table_name", "columns", "foreign_keys", "indexes", "uniques", "checks"},
            model_label,
        )
        assert isinstance(model["model_name"], str) and model["model_name"], (
            f"{model_label}.model_name must be non-empty string"
        )
        assert isinstance(model["table_name"], str) and model["table_name"], (
            f"{model_label}.table_name must be non-empty string"
        )
        assert isinstance(model["columns"], list) and model["columns"], (
            f"{model_label}.columns must be non-empty list"
        )


def _validate_query_payload(payload: dict[str, Any], label: str) -> None:
    _require_keys(payload, {"model_name", "where", "order_by", "limit", "offset", "m2m"}, label)
    assert isinstance(payload["model_name"], str) and payload["model_name"], (
        f"{label}.model_name must be non-empty string"
    )
    where_nodes = payload["where"]
    assert isinstance(where_nodes, list) and where_nodes, f"{label}.where must be non-empty list"
    for i, node in enumerate(where_nodes):
        node_label = f"{label}.where[{i}]"
        assert isinstance(node, dict), f"{node_label} must be object"
        _validate_query_node(node, node_label)
    assert isinstance(payload["order_by"], list), f"{label}.order_by must be list"
    if payload["limit"] is not None:
        assert isinstance(payload["limit"], int) and payload["limit"] >= 0, (
            f"{label}.limit must be null or non-negative int"
        )
    if payload["offset"] is not None:
        assert isinstance(payload["offset"], int) and payload["offset"] >= 0, (
            f"{label}.offset must be null or non-negative int"
        )
    if payload["m2m"] is not None:
        assert isinstance(payload["m2m"], dict), f"{label}.m2m must be null or object"


def _validate_codec_payload(payload: dict[str, Any], label: str) -> None:
    _require_keys(payload, {"bind_rules", "fetch_rules", "hydration_abi"}, label)
    assert isinstance(payload["bind_rules"], list) and payload["bind_rules"], (
        f"{label}.bind_rules must be non-empty list"
    )
    assert isinstance(payload["fetch_rules"], list) and payload["fetch_rules"], (
        f"{label}.fetch_rules must be non-empty list"
    )
    hydration_abi = payload["hydration_abi"]
    assert isinstance(hydration_abi, dict), f"{label}.hydration_abi must be object"
    _require_keys(hydration_abi, {"constructor_mode", "required_slots"}, f"{label}.hydration_abi")
    assert hydration_abi["constructor_mode"] == "direct_dict", (
        f"{label}.hydration_abi.constructor_mode must be direct_dict"
    )
    required_slots = hydration_abi["required_slots"]
    assert isinstance(required_slots, list) and required_slots, (
        f"{label}.hydration_abi.required_slots must be non-empty list"
    )


def _validate_domain_payload(domain: str, payload: dict[str, Any], label: str) -> None:
    if domain == "schema":
        _validate_schema_payload(payload, label)
    elif domain == "query":
        _validate_query_payload(payload, label)
    elif domain == "codec":
        _validate_codec_payload(payload, label)
    else:
        raise AssertionError(f"{label}.domain unsupported: {domain}")


def test_ir_vectors_directory_has_seed_vectors() -> None:
    vectors = _load_vectors()
    assert vectors, "Expected at least one IR vector fixture in tests/fixtures/ir_vectors"
    found_domains = {payload["domain"] for _, payload in vectors if "domain" in payload}
    assert SUPPORTED_DOMAINS.issubset(found_domains), (
        f"Expected at least one vector for each domain: {sorted(SUPPORTED_DOMAINS)}"
    )


def test_ir_vectors_match_phase0_contract_envelope() -> None:
    for path, vector in _load_vectors():
        label = path.name
        _require_keys(vector, {"vector_name", "domain", "expect_valid", "ir"}, label)
        assert isinstance(vector["vector_name"], str) and vector["vector_name"], (
            f"{label}.vector_name must be non-empty string"
        )
        assert vector["domain"] in SUPPORTED_DOMAINS, f"{label}.domain unsupported: {vector['domain']!r}"
        assert vector["expect_valid"] is True, f"{label}.expect_valid must be true for Phase 0"

        ir = vector["ir"]
        assert isinstance(ir, dict), f"{label}.ir must be object"
        _require_keys(ir, {"ir_kind", "ir_version", "payload"}, f"{label}.ir")
        assert ir["ir_kind"] == vector["domain"], (
            f"{label}.ir.ir_kind ({ir['ir_kind']!r}) must match domain ({vector['domain']!r})"
        )
        assert ir["ir_version"] == SUPPORTED_IR_VERSION, (
            f"{label}.ir.ir_version must equal {SUPPORTED_IR_VERSION}"
        )
        assert isinstance(ir["payload"], dict), f"{label}.ir.payload must be object"
        _validate_domain_payload(vector["domain"], ir["payload"], f"{label}.ir.payload")
