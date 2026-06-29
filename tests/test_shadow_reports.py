import json
from pathlib import Path

import pytest
from pydantic import Field

from ferro import Model, connect
from ferro._core import (
    _render_create_table_sql_for_test,
    _render_migration_sql_for_test,
    _shadow_compare_migration_plan_for_test,
    _shadow_compare_query_plan_for_test,
)
from ferro.ir.compiler import compile_schema_ir_payload, wrap_schema_ir
from ferro.query.builder import _query_ir_payload_to_json


def _compile_schema_ir_json(schema: dict, name: str) -> str:
    """Compile an ad-hoc schema dict into a SchemaIR envelope JSON string."""
    payload = compile_schema_ir_payload(name, schema)
    return json.dumps(wrap_schema_ir(payload))

pytestmark = pytest.mark.backend_matrix

SHADOW_FIXTURES = Path(__file__).parent / "fixtures" / "shadow_reports"


def _report_for_backend(dialect: str) -> dict:
    schema = {
        "properties": {
            "id": {"type": "integer", "primary_key": True, "autoincrement": True},
            "name": {"type": "string", "ferro_nullable": True},
            "age": {"type": "integer"},
        }
    }
    query_json = _query_ir_payload_to_json(
        {
            "model_name": "ShadowUser",
            "where": [
                {
                    "node_kind": "leaf",
                    "column": "age",
                    "operator": ">=",
                    "value": {"kind": "int", "value": 18},
                },
                {
                    "node_kind": "leaf",
                    "column": "name",
                    "operator": "LIKE",
                    "value": {"kind": "string", "value": "a%"},
                },
            ],
            "order_by": [{"column": "age", "direction": "desc"}],
            "limit": 5,
            "offset": 1,
            "m2m": None,
        }
    )
    query_compare = json.loads(
        _shadow_compare_query_plan_for_test(query_json, dialect, "select")
    )
    create_table_sql, create_table_extras = _render_create_table_sql_for_test(
        "ShadowUser", json.dumps(compile_schema_ir_payload("ShadowUser", schema)), dialect
    )
    _schema_ir_json = _compile_schema_ir_json(schema, "shadowuser")
    _live_json = json.dumps(
        [
            {
                "name": "id",
                "declared_type": "integer",
                "is_primary_key": True,
                "is_nullable": False,
            }
        ]
    )
    migration_stmts, migration_warns = _render_migration_sql_for_test(
        "ShadowUser",
        _schema_ir_json,
        _live_json,
        dialect,
        True,
        False,
    )
    migration_compare = json.loads(
        _shadow_compare_migration_plan_for_test(
            "ShadowUser",
            _schema_ir_json,
            json.dumps(schema),
            _live_json,
            dialect,
            True,
            False,
        )
    )
    return {
        "query_compare": query_compare,
        "create_table": [create_table_sql, list(create_table_extras)],
        "migration": [list(migration_stmts), list(migration_warns)],
        "migration_compare": migration_compare,
    }


def test_shadow_report_fixture_stable(db_backend: str) -> None:
    report = _report_for_backend(db_backend)
    fixture_path = SHADOW_FIXTURES / f"{db_backend}.json"
    expected = json.loads(fixture_path.read_text(encoding="utf-8"))
    assert report == expected


@pytest.mark.asyncio
async def test_shadow_runtime_strict_has_no_mismatch(monkeypatch: pytest.MonkeyPatch, db_url: str):
    monkeypatch.setenv("FERRO_SHADOW_RUNTIME", "1")
    monkeypatch.setenv("FERRO_SHADOW_RUNTIME_STRICT", "1")

    class ShadowRuntimeUser(Model):
        id: int = Field(json_schema_extra={"primary_key": True})
        name: str
        age: int

    await connect(db_url, auto_migrate=True)
    await ShadowRuntimeUser(id=1, name="alice", age=22).save()
    await ShadowRuntimeUser(id=2, name="bob", age=17).save()

    rows = await ShadowRuntimeUser.where(lambda t: t.age >= 18).all()
    assert [row.name for row in rows] == ["alice"]

    count = await ShadowRuntimeUser.where(lambda t: t.age >= 18).count()
    assert count == 1

    updated = await ShadowRuntimeUser.where(lambda t: t.name == "alice").update(age=23)
    assert updated == 1

    deleted = await ShadowRuntimeUser.where(lambda t: t.name == "bob").delete()
    assert deleted == 1
