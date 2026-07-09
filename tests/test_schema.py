from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Dict, List

import pytest
from pydantic import Field

import ferro
from ferro import FerroField, Model

pytestmark = pytest.mark.backend_matrix


class SchemaProduct(Model):
    id: int = Field(json_schema_extra={"primary_key": True})
    name: str
    price: float
    is_active: bool = True


@pytest.mark.asyncio
async def test_create_tables_success(db_url):
    """Test that create_tables generates and executes SQL correctly."""
    await ferro.connect(db_url)

    # This should generate CREATE TABLE schemaproduct (...)
    await ferro.create_tables()

    # Verification: We'll try to insert a record later,
    # but for now, we just want to ensure it doesn't crash
    # and the engine handles the registry.
    assert True


@pytest.mark.asyncio
async def test_create_tables_no_connection():
    """Test that create_tables raises an error if no connection exists."""
    ferro.reset_engine()
    with pytest.raises(ferro.InterfaceError) as excinfo:
        await ferro.create_tables()
    assert "Engine not initialized" in str(excinfo.value)


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_auto_migrate_runtime_ddl_infers_required_field_not_null(db_url):
    """Runtime DDL should use the same nullability metadata as Alembic."""

    class NullabilityRow(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        required_name: str
        optional_note: str | None = None

    await ferro.connect(db_url, auto_migrate=True)

    import sqlite3

    db_path = db_url.removeprefix("sqlite:").split("?", 1)[0]
    conn = sqlite3.connect(db_path)
    rows = conn.execute("PRAGMA table_info(nullabilityrow)").fetchall()
    conn.close()

    not_null_by_column = {row[1]: row[3] for row in rows}
    assert not_null_by_column["required_name"] == 1
    assert not_null_by_column["optional_note"] == 0


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_auto_migrate_runtime_ddl_preserves_ref_field_nullability(db_url):
    """Runtime DDL should not lose Ferro metadata when resolving JSON-schema refs."""

    class RowStatus(StrEnum):
        DRAFT = "draft"
        ACTIVE = "active"

    class RefNullabilityRow(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        status: RowStatus = RowStatus.DRAFT

    await ferro.connect(db_url, auto_migrate=True)

    import sqlite3

    db_path = db_url.removeprefix("sqlite:").split("?", 1)[0]
    conn = sqlite3.connect(db_path)
    rows = conn.execute("PRAGMA table_info(refnullabilityrow)").fetchall()
    conn.close()

    not_null_by_column = {row[1]: row[3] for row in rows}
    assert not_null_by_column["status"] == 1


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_auto_migrate_runtime_ddl_uses_logical_decimal_and_json_types(db_url):
    """Runtime DDL should preserve Decimal/JSON logical type intent."""

    class LogicalTypeRow(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        price: Decimal
        metadata: Dict[str, str]
        tags: List[str]

    columns = LogicalTypeRow.__ferro_columns__
    assert columns["price"].format == "decimal"
    assert columns["metadata"].logical_type == "json"
    assert columns["tags"].logical_type == "json"

    await ferro.connect(db_url, auto_migrate=True)

    import sqlite3

    db_path = db_url.removeprefix("sqlite:").split("?", 1)[0]
    conn = sqlite3.connect(db_path)
    rows = conn.execute("PRAGMA table_info(logicaltyperow)").fetchall()
    conn.close()

    types_by_column = {row[1]: row[2].upper() for row in rows}
    assert types_by_column["price"] == "NUMERIC"
    assert types_by_column["metadata"] == "JSON"
    assert types_by_column["tags"] == "JSON"
