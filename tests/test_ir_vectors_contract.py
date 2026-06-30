from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Any

import pytest

from ferro import BackRef, Field, ManyToMany, Model, Relation, clear_registry, reset_engine


VECTORS_DIR = Path(__file__).parent / "fixtures" / "ir_vectors"
SUPPORTED_DOMAINS = {"schema", "query", "codec"}
SUPPORTED_IR_VERSION = 1
QUERY_OPERATORS = {"==", "!=", "<", "<=", ">", ">=", "IN", "LIKE", "AND", "OR"}


class _DocFormat(StrEnum):
    PDF = "pdf"
    JSON = "json"


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
    bind_rules = payload["bind_rules"]
    fetch_rules = payload["fetch_rules"]
    for i, rule in enumerate(bind_rules):
        rule_label = f"{label}.bind_rules[{i}]"
        assert isinstance(rule, dict), f"{rule_label} must be object"
        _require_keys(
            rule,
            {"logical_type", "db_type", "non_null_wire_kind", "null_wire_kind"},
            rule_label,
        )
    for i, rule in enumerate(fetch_rules):
        rule_label = f"{label}.fetch_rules[{i}]"
        assert isinstance(rule, dict), f"{rule_label} must be object"
        _require_keys(rule, {"db_type", "wire_kind", "python_kind"}, rule_label)

    bind_type_pairs = {(r["logical_type"], r["db_type"]) for r in bind_rules}
    required_bind_pairs = {
        ("uuid", "uuid"),
        ("decimal", "numeric"),
        ("datetime", "timestamptz"),
        ("date", "date"),
        ("enum", "enum"),
    }
    assert required_bind_pairs.issubset(bind_type_pairs), (
        f"{label}.bind_rules missing required type pairs: "
        f"{sorted(required_bind_pairs - bind_type_pairs)}"
    )

    fetch_db_types = {r["db_type"] for r in fetch_rules}
    required_fetch_db_types = {"uuid", "numeric", "timestamptz", "date", "enum"}
    assert required_fetch_db_types.issubset(fetch_db_types), (
        f"{label}.fetch_rules missing required db types: "
        f"{sorted(required_fetch_db_types - fetch_db_types)}"
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
    assert {
        "__pydantic_fields_set__",
        "__pydantic_extra__",
        "__pydantic_private__",
    }.issubset(set(required_slots)), f"{label}.hydration_abi.required_slots missing required slots"


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


@pytest.fixture()
def clean_model_registry() -> None:
    from ferro import state as ferro_state

    reset_engine()
    clear_registry()
    ferro_state._MODEL_REGISTRY_PY.clear()
    ferro_state._PENDING_RELATIONS.clear()
    ferro_state._JOIN_TABLE_REGISTRY.clear()
    ferro_state._SCHEMA_IR_BY_MODEL.clear()
    ferro_state._SCHEMA_IR_FINGERPRINT_BY_MODEL.clear()
    ferro_state._SCHEMA_IR_MODELSET = None
    ferro_state._SCHEMA_IR_MODELSET_FINGERPRINT = None
    yield
    reset_engine()
    clear_registry()
    ferro_state._MODEL_REGISTRY_PY.clear()
    ferro_state._PENDING_RELATIONS.clear()
    ferro_state._JOIN_TABLE_REGISTRY.clear()
    ferro_state._SCHEMA_IR_BY_MODEL.clear()
    ferro_state._SCHEMA_IR_FINGERPRINT_BY_MODEL.clear()
    ferro_state._SCHEMA_IR_MODELSET = None
    ferro_state._SCHEMA_IR_MODELSET_FINGERPRINT = None


def _load_vector(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_phase1_schema_compiler_matches_snapshot(clean_model_registry: None) -> None:
    from ferro.ir import compile_registry_schema_ir, schema_ir_fingerprint
    from ferro.relations import resolve_relationships

    from tests.test_cross_emitter_parity import _build_fixture_models

    _build_fixture_models()
    resolve_relationships()

    compiled = compile_registry_schema_ir()
    snapshot = _load_vector(VECTORS_DIR / "schema_phase1_fixture_models_v1.json")

    assert compiled == snapshot["ir"]
    assert schema_ir_fingerprint(compiled) == snapshot["fingerprint"]


def test_phase1_schema_compiler_is_deterministic(clean_model_registry: None) -> None:
    from ferro.ir import compile_registry_schema_ir, schema_ir_fingerprint
    from ferro.relations import resolve_relationships

    from tests.test_cross_emitter_parity import _build_fixture_models

    _build_fixture_models()
    resolve_relationships()

    first = compile_registry_schema_ir()
    first_fp = schema_ir_fingerprint(first)
    second = compile_registry_schema_ir()
    second_fp = schema_ir_fingerprint(second)

    assert first == second
    assert first_fp == second_fp


def test_compile_registry_schema_ir_persists_modelset_cache(
    clean_model_registry: None,
) -> None:
    from ferro import state as ferro_state
    from ferro.ir import compile_registry_schema_ir, schema_ir_fingerprint
    from ferro.relations import resolve_relationships

    from tests.test_cross_emitter_parity import _build_fixture_models

    _build_fixture_models()
    resolve_relationships()

    ferro_state._SCHEMA_IR_MODELSET = None
    ferro_state._SCHEMA_IR_MODELSET_FINGERPRINT = None

    compiled = compile_registry_schema_ir()

    assert ferro_state._SCHEMA_IR_MODELSET == compiled
    assert ferro_state._SCHEMA_IR_MODELSET_FINGERPRINT == schema_ir_fingerprint(compiled)


def test_schema_ir_compiler_emits_db_check_expression_for_closed_domain(
    clean_model_registry: None,
) -> None:
    from ferro.ir import compile_registry_schema_ir
    from ferro.relations import resolve_relationships

    class Document(Model):
        id: int | None = Field(default=None, primary_key=True)
        format: _DocFormat = Field(db_type="text", db_check=True)

    resolve_relationships()
    compiled = compile_registry_schema_ir()
    models = compiled["payload"]["models"]
    document = next(model for model in models if model["table_name"] == "document")
    checks = document["checks"]
    assert checks == [
        {
            "name": "ck_document_format",
            "expression": "format IN ('pdf', 'json')",
            "column": "format",
            "values": ["'pdf'", "'json'"],
        }
    ]


def test_compiler_omits_db_type_for_non_explicit_columns(clean_model_registry: None) -> None:
    from ferro import Model, Field, clear_registry
    from ferro.schema_metadata import build_model_schema
    from ferro.ir.compiler import compile_schema_ir_payload

    clear_registry()
    M = type("Acct", (Model,), {
        "__annotations__": {"id": int | None, "balance": int, "code": str},
        "id": Field(default=None, primary_key=True),
        "code": Field(db_type="varchar(32)"),
    })
    cols = {c["name"]: c for c in compile_schema_ir_payload("Acct", build_model_schema(M))["models"][0]["columns"]}
    assert "db_type" not in cols["balance"], cols["balance"]   # non-explicit -> omitted
    assert "db_type" not in cols["id"], cols["id"]             # non-explicit -> omitted
    assert cols["code"]["db_type"] == "varchar(32)"            # explicit -> kept
    assert cols["code"]["db_type_explicit"] is True


def test_compiler_bytes_field_emits_binary_logical_type(clean_model_registry: None) -> None:
    from ferro import Model, Field, clear_registry
    from ferro.schema_metadata import build_model_schema
    from ferro.ir.compiler import compile_schema_ir_payload

    clear_registry()
    M = type("Blob", (Model,), {
        "__annotations__": {"id": int | None, "contents": bytes},
        "id": Field(default=None, primary_key=True),
    })
    cols = {c["name"]: c for c in compile_schema_ir_payload("Blob", build_model_schema(M))["models"][0]["columns"]}
    assert cols["contents"]["logical_type"] == "binary", cols["contents"]


def test_schema_ir_compiler_includes_join_table_models(clean_model_registry: None) -> None:
    from ferro.ir import compile_registry_schema_ir
    from ferro.relations import resolve_relationships

    class Tag(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str
        posts: Relation[list["Post"]] = ManyToMany(related_name="tags")

    class Post(Model):
        id: int | None = Field(default=None, primary_key=True)
        title: str
        tags: Relation[list["Tag"]] = BackRef()

    resolve_relationships()
    compiled = compile_registry_schema_ir()
    table_names = {model["table_name"] for model in compiled["payload"]["models"]}
    assert "tag_posts" in table_names


def test_primary_key_autoincrements_by_default(clean_model_registry: None) -> None:
    """A PK declared without an explicit autoincrement defaults to
    autoincrement=True, matching the historical create path (so Postgres gets a
    SERIAL/identity column rather than a plain INTEGER that fails NOT NULL on
    insert). uuid PKs set autoincrement=False explicitly and are covered by the
    uuid tests. (#153)
    """
    from ferro.ir.compiler import compile_schema_ir_payload
    from ferro.schema_metadata import build_model_schema

    class IntPk(Model):
        id: int = Field(json_schema_extra={"primary_key": True})
        name: str

    cols = {
        c["name"]: c
        for c in compile_schema_ir_payload("IntPk", build_model_schema(IntPk))["models"][0]["columns"]
    }
    assert cols["id"]["primary_key"] is True
    assert cols["id"]["autoincrement"] is True, cols["id"]


def test_clear_registry_clears_join_table_registry(clean_model_registry: None) -> None:
    """clear_registry() must clear the join-table registry, not only models.

    connect/create_tables/migrate compile the full registry via
    compile_registry_schema_ir(); a join table left behind by a prior run would
    be re-created with foreign keys to tables that no longer exist — tolerated by
    SQLite, rejected by Postgres (``relation ... does not exist``). (#153)
    """
    from ferro import state as ferro_state
    from ferro.ir import compile_registry_schema_ir
    from ferro.relations import resolve_relationships

    class Tag(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str
        posts: Relation[list["Post"]] = ManyToMany(related_name="tags")

    class Post(Model):
        id: int | None = Field(default=None, primary_key=True)
        title: str
        tags: Relation[list["Tag"]] = BackRef()

    resolve_relationships()
    assert ferro_state._JOIN_TABLE_REGISTRY, "precondition: a join table is registered"

    # Simulate a lean test cleanup that clears the declared models but (as the
    # failing fixtures did) NOT the join-table registry directly. clear_registry
    # must reset the join registry so a later full-registry compile can't
    # resurrect a stale join table whose FK targets no longer exist.
    clear_registry()
    ferro_state._MODEL_REGISTRY_PY.clear()

    assert ferro_state._JOIN_TABLE_REGISTRY == {}, "clear_registry must clear join tables"
    table_names = {m["table_name"] for m in compile_registry_schema_ir()["payload"]["models"]}
    assert "tag_posts" not in table_names, f"stale join table resurrected: {table_names}"
