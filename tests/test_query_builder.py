import json
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum

import pytest

import ferro
from ferro import Model, connect
from ferro.query import Query, QueryNode
from ferro.query.nodes import FieldProxy, _serialize_query_value
from ferro.query.wire import compile_query
from pydantic import Field

pytestmark = pytest.mark.backend_matrix


class QueryStatus(str, Enum):
    ACTIVE = "active"


def test_serialize_query_value_normalizes_non_json_native_values():
    uid = uuid.uuid4()
    happened_at = datetime(2026, 4, 24, 18, 30, tzinfo=UTC)
    payload = {
        "id": uid,
        "price": Decimal("12.50"),
        "happened_at": happened_at,
        "day": date(2026, 4, 24),
        "status": QueryStatus.ACTIVE,
        "nested": {
            "ids": [uid],
            "amounts": (Decimal("1.25"),),
            "unique_ids": {uid},
        },
    }

    serialized = _serialize_query_value(payload)

    assert serialized["id"] == str(uid)
    assert serialized["price"] == "12.50"
    assert serialized["happened_at"] == happened_at.isoformat()
    assert serialized["day"] == "2026-04-24"
    assert serialized["status"] == QueryStatus.ACTIVE
    assert serialized["nested"]["ids"] == [str(uid)]
    assert serialized["nested"]["amounts"] == ["1.25"]
    assert serialized["nested"]["unique_ids"] == [str(uid)]
    json.dumps(serialized)


def test_to_wire_json_serializes_m2m_context_without_mutating_query_state():
    class WireM2mPost(Model):
        id: int | None = Field(default=None, json_schema_extra={"primary_key": True})

    source_id = uuid.uuid4()
    query = Query(WireM2mPost)._m2m(
        "post_tags",
        "post_id",
        "tag_id",
        source_id,
    )

    query_json = compile_query(query, "fetch").wire_json
    payload = json.loads(query_json)

    assert query._m2m_context.source_id == source_id
    assert isinstance(query._m2m_context.source_id, uuid.UUID)
    assert payload["ir_kind"] == "query"
    assert payload["ir_version"] == 12
    assert payload["payload"]["m2m"]["source_id"] == str(source_id)


def test_query_carries_root_instances_materialization_by_default():
    """Every query without a projection materializes complete root instances
    (ADR-0007): the v3 plan is explicit data on the wire, never inferred."""

    class WirePlainRow(Model):
        id: int | None = Field(default=None, json_schema_extra={"primary_key": True})

    payload = compile_query(Query(WirePlainRow), "fetch").payload.to_ir_dict()
    assert payload["materialization"] == {"kind": "root_instances"}


def test_query_node_to_dict_serializes_uuid_values_inside_in_filters():
    uid1 = uuid.uuid4()
    uid2 = uuid.uuid4()
    node = QueryNode(column="run_id", operator="IN", value=[uid1, uid2])

    assert node.to_ir_dict()["value"]["value"] == [str(uid1), str(uid2)]


def test_query_node_to_ir_dict_uses_query_ir_shape():
    node = QueryNode(column="age", operator=">=", value=18)
    payload = node.to_ir_dict()

    assert payload["node_kind"] == "leaf"
    assert payload["column"] == "age"
    assert payload["operator"] == ">="
    assert payload["value"] == {"kind": "int", "value": 18}
    assert payload["path"] == []


def test_query_node_to_ir_dict_emits_field_proxy_path():
    """A leaf built from a ``FieldProxy`` carries its ``path`` into IR (#269);
    this slice only ever emits ``[]`` (root model) — #270 populates it via
    relation traversal."""
    node = FieldProxy("age", path=("account",)) >= 18
    payload = node.to_ir_dict()

    assert payload["path"] == ["account"]


def test_query_node_to_ir_dict_compound_node_has_no_path_key():
    left = FieldProxy("age") >= 18
    right = FieldProxy("name") == "a"
    compound = left & right

    payload = compound.to_ir_dict()

    assert payload["node_kind"] == "compound"
    assert "path" not in payload


def test_field_proxy_operator_overloading():
    """
    Test that operators on a FieldProxy (the lambda predicate mechanism)
    return a QueryNode.
    """
    expr = FieldProxy("age") >= 18

    assert isinstance(expr, QueryNode)
    assert expr.column == "age"
    assert expr.operator == ">="
    assert expr.value == 18


def test_model_where_clause():
    """
    Test that Model.where() returns a Query object with the correct condition.
    """

    class QueryUser(Model):
        id: int = Field(json_schema_extra={"primary_key": True})
        age: int

    query = QueryUser.where(lambda u: u.age >= 21)

    assert isinstance(query, Query)
    assert len(query.where_clause) == 1
    assert query.where_clause[0].column == "age"
    assert query.where_clause[0].operator == ">="
    assert query.where_clause[0].value == 21


def test_query_chaining_placeholders():
    """
    Test that Query object supports chaining (even if not yet executed).
    """

    class QueryUser(Model):
        id: int = Field(json_schema_extra={"primary_key": True})
        age: int

    query = QueryUser.where(lambda u: u.age >= 18).limit(10).offset(5)

    assert query._limit == 10
    assert query._offset == 5
    assert len(query.where_clause) == 1


def test_in_operator_lshift():
    """
    Test that the << operator correctly creates an IN condition.
    """
    expr = FieldProxy("username") << ["taylor", "jeff"]

    assert isinstance(expr, QueryNode)
    assert expr.column == "username"
    assert expr.operator == "IN"
    assert expr.value == ["taylor", "jeff"]

    # Test with tuple
    expr_tuple = FieldProxy("username") << ("alice", "bob")
    assert expr_tuple.value == ["alice", "bob"]

    with pytest.raises(TypeError, match="expects a list, tuple, or set"):
        _ = FieldProxy("username") << "not a list"


@pytest.mark.asyncio
async def test_query_execution(db_url):
    """
    Test that executing a filtered query actually returns data from the DB.
    """

    class FilterUser(Model):
        id: int = Field(json_schema_extra={"primary_key": True})
        username: str
        age: int

    # Initialize connection and auto-migrate
    await connect(db_url, auto_migrate=True)

    async with ferro.engines.session():
        # Seed data
        await FilterUser(id=1, username="taylor", age=30).save()
        await FilterUser(id=2, username="jeff", age=25).save()
        await FilterUser(id=3, username="alice", age=35).save()

        # 1. Test basic filter
        results = await FilterUser.where(lambda t: t.age >= 30).all()
        assert len(results) == 2
        assert {r.username for r in results} == {"taylor", "alice"}

        # 2. Test IN filter
        results_in = await FilterUser.where(lambda t: t.username << ["jeff", "alice"]).all()
        assert len(results_in) == 2
        assert {r.username for r in results_in} == {"jeff", "alice"}

        # 3. Test combined filters (Chaining)
        results_chained = await FilterUser.where(lambda t: t.age < 35).where(
            lambda t: t.age > 20
        ).all()
        assert len(results_chained) == 2
        assert {r.username for r in results_chained} == {"taylor", "jeff"}


@pytest.mark.asyncio
async def test_query_first(db_url):
    """
    Test that .first() returns a single record or None.
    """

    class FirstUser(Model):
        id: int = Field(json_schema_extra={"primary_key": True})
        username: str

    await connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await FirstUser(id=1, username="taylor").save()

        # 1. Match found
        user = await FirstUser.where(lambda t: t.username == "taylor").first()
        assert user is not None
        assert user.username == "taylor"

        # 2. No match found
        no_user = await FirstUser.where(lambda t: t.username == "nonexistent").first()
        assert no_user is None


@pytest.mark.asyncio
async def test_sql_injection_protection(db_url):
    """
    Test that malicious strings are treated as literals and don't bypass filters.
    """

    class SafeUser(Model):
        id: int = Field(json_schema_extra={"primary_key": True})
        username: str

    await connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await SafeUser(id=1, username="taylor").save()

        # Attempt standard SQL injection
        injection_string = "' OR '1'='1"

        # If not parameterized, this might return the user.
        # If parameterized, it should look for the literal string and return None.
        result = await SafeUser.where(lambda t: t.username == injection_string).first()

        assert result is None


@pytest.mark.asyncio
async def test_query_bitwise_logic(db_url):
    """
    Test that bitwise | (OR) and & (AND) create correct logical conditions.
    """

    class LogicUser(Model):
        id: int = Field(json_schema_extra={"primary_key": True})
        username: str
        age: int

    await connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await LogicUser(id=1, username="taylor", age=30).save()
        await LogicUser(id=2, username="jeff", age=25).save()
        await LogicUser(id=3, username="alice", age=35).save()

        # 1. Test OR (|)
        # SQL: SELECT * FROM logicuser WHERE age < 30 OR username == 'alice'
        results_or = await LogicUser.where(
            lambda t: (t.age < 30) | (t.username == "alice")
        ).all()
        assert len(results_or) == 2
        assert {r.username for r in results_or} == {"jeff", "alice"}

        # 2. Test nested AND (&) within WHERE
        # SQL: SELECT * FROM logicuser WHERE (age > 20) AND (username != 'taylor')
        results_and = await LogicUser.where(
            lambda t: (t.age > 20) & (t.username != "taylor")
        ).all()
        assert len(results_and) == 2
        assert {r.username for r in results_and} == {"jeff", "alice"}

        # 3. Test Complex Nesting: (A OR B) AND C
        # SQL: SELECT * FROM logicuser WHERE (username == 'taylor' OR username == 'jeff') AND age > 28
        # Only taylor (30) matches both. jeff (25) is under 28.
        results_complex = await LogicUser.where(
            lambda t: ((t.username == "taylor") | (t.username == "jeff")) & (t.age > 28)
        ).all()
        assert len(results_complex) == 1
        assert results_complex[0].username == "taylor"


@pytest.mark.asyncio
async def test_query_bitwise_multiple_where(db_url):
    """
    Test that multiple .where() calls are AND-ed together with complex logic.
    """

    class LogicUser(Model):
        id: int = Field(json_schema_extra={"primary_key": True})
        username: str
        age: int

    await connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        await LogicUser(id=1, username="taylor", age=30).save()
        await LogicUser(id=2, username="jeff", age=25).save()
        await LogicUser(id=3, username="alice", age=35).save()

        # (A OR B) AND (C)
        query = LogicUser.where(lambda t: (t.username == "jeff") | (t.username == "alice"))
        query = query.where(lambda t: t.age > 30)

        results = await query.all()
        assert len(results) == 1
        assert results[0].username == "alice"
