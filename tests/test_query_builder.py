import os
import uuid
import pytest
from ferro import Model, connect
from pydantic import Field
from ferro.query import Query, QueryNode

@pytest.fixture
def db_url():
    db_file = f"test_{uuid.uuid4()}.db"
    url = f"sqlite:{db_file}?mode=rwc"
    yield url
    if os.path.exists(db_file):
        os.remove(db_file)

def test_field_proxy_operator_overloading():
    """
    Test that accessing a field on the Model class returns a FieldProxy
    and that operators on it return a QueryNode.
    """
    class QueryUser(Model):
        id: int = Field(json_schema_extra={"primary_key": True})
        age: int
        username: str

    # 1. Accessing via class should return something that supports operators
    expr = QueryUser.age >= 18
    
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

    query = QueryUser.where(QueryUser.age >= 21)
    
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

    query = QueryUser.where(QueryUser.age >= 18).limit(10).offset(5)
    
    assert query._limit == 10
    assert query._offset == 5
    assert len(query.where_clause) == 1

def test_in_operator_lshift():
    """
    Test that the << operator correctly creates an IN condition.
    """
    class QueryUser(Model):
        id: int = Field(json_schema_extra={"primary_key": True})
        username: str

    expr = QueryUser.username << ["taylor", "jeff"]
    
    assert isinstance(expr, QueryNode)
    assert expr.column == "username"
    assert expr.operator == "IN"
    assert expr.value == ["taylor", "jeff"]

    # Test with tuple
    expr_tuple = QueryUser.username << ("alice", "bob")
    assert expr_tuple.value == ["alice", "bob"]

    with pytest.raises(TypeError, match="expects a list, tuple, or set"):
        _ = QueryUser.username << "not a list"

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

    # Seed data
    await FilterUser(id=1, username="taylor", age=30).save()
    await FilterUser(id=2, username="jeff", age=25).save()
    await FilterUser(id=3, username="alice", age=35).save()

    # 1. Test basic filter
    results = await FilterUser.where(FilterUser.age >= 30).all()
    assert len(results) == 2
    assert {r.username for r in results} == {"taylor", "alice"}

    # 2. Test IN filter
    results_in = await FilterUser.where(FilterUser.username << ["jeff", "alice"]).all()
    assert len(results_in) == 2
    assert {r.username for r in results_in} == {"jeff", "alice"}

    # 3. Test combined filters (Chaining)
    results_chained = await FilterUser.where(FilterUser.age < 35).where(FilterUser.age > 20).all()
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
    await FirstUser(id=1, username="taylor").save()

    # 1. Match found
    user = await FirstUser.where(FirstUser.username == "taylor").first()
    assert user is not None
    assert user.username == "taylor"

    # 2. No match found
    no_user = await FirstUser.where(FirstUser.username == "nonexistent").first()
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
    await SafeUser(id=1, username="taylor").save()

    # Attempt standard SQL injection
    injection_string = "' OR '1'='1"
    
    # If not parameterized, this might return the user.
    # If parameterized, it should look for the literal string and return None.
    result = await SafeUser.where(SafeUser.username == injection_string).first()
    
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
    await LogicUser(id=1, username="taylor", age=30).save()
    await LogicUser(id=2, username="jeff", age=25).save()
    await LogicUser(id=3, username="alice", age=35).save()

    # 1. Test OR (|)
    # SQL: SELECT * FROM logicuser WHERE age < 30 OR username == 'alice'
    results_or = await LogicUser.where((LogicUser.age < 30) | (LogicUser.username == "alice")).all()
    assert len(results_or) == 2
    assert {r.username for r in results_or} == {"jeff", "alice"}

    # 2. Test nested AND (&) within WHERE
    # SQL: SELECT * FROM logicuser WHERE (age > 20) AND (username != 'taylor')
    results_and = await LogicUser.where((LogicUser.age > 20) & (LogicUser.username != "taylor")).all()
    assert len(results_and) == 2
    assert {r.username for r in results_and} == {"jeff", "alice"}

    # 3. Test Complex Nesting: (A OR B) AND C
    # SQL: SELECT * FROM logicuser WHERE (username == 'taylor' OR username == 'jeff') AND age > 28
    # Only taylor (30) matches both. jeff (25) is under 28.
    results_complex = await LogicUser.where(
        ((LogicUser.username == "taylor") | (LogicUser.username == "jeff")) 
        & (LogicUser.age > 28)
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
    await LogicUser(id=1, username="taylor", age=30).save()
    await LogicUser(id=2, username="jeff", age=25).save()
    await LogicUser(id=3, username="alice", age=35).save()

    # (A OR B) AND (C)
    query = LogicUser.where((LogicUser.username == "jeff") | (LogicUser.username == "alice"))
    query = query.where(LogicUser.age > 30)
    
    results = await query.all()
    assert len(results) == 1
    assert results[0].username == "alice"
