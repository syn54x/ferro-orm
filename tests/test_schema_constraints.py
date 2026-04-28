import sqlite3
from typing import Annotated

import pytest

from ferro import (
    BackRef,
    Field,
    FerroField,
    ForeignKey,
    Model,
    Relation,
    clear_registry,
    connect,
    reset_engine,
)

pytestmark = pytest.mark.backend_matrix


@pytest.fixture(autouse=True)
def cleanup():
    reset_engine()
    clear_registry()
    yield


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_runtime_create_tables_respects_explicit_nullable_override(db_url):
    """Rust DDL should honor the same explicit nullable override that Alembic sees."""

    class NullableOverrideRow(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        field_a: int | None = Field(default=None, nullable=False)

    await connect(db_url, auto_migrate=True)

    db_path = db_url.replace("sqlite:", "").split("?")[0]
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info('nullableoverriderow')")
    columns = {row[1]: row for row in cursor.fetchall()}
    conn.close()

    assert columns["field_a"][3] == 1, "field_a should be NOT NULL in runtime DDL"


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_foreign_key_constraint_exists(db_url):
    """Verify that Rust generates the actual FOREIGN KEY constraint in SQL."""

    class Category(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        products: Relation[list["Product"]] = BackRef()

    class Product(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        category: Annotated[
            Category, ForeignKey(related_name="products", on_delete="CASCADE")
        ]

    # 1. Connect and Migrate
    await connect(db_url, auto_migrate=True)

    # 2. Inspect the SQLite schema directly
    db_path = db_url.replace("sqlite:", "").split("?")[0]
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # PRAGMA foreign_key_list(table_name) returns rows like:
    # (id, seq, table, from, to, on_update, on_delete, match)
    cursor.execute("PRAGMA foreign_key_list('product')")
    fk_list = cursor.fetchall()
    conn.close()

    assert len(fk_list) > 0, "No foreign key constraint found on 'product' table"

    fk = fk_list[0]
    assert fk[2] == "category", f"Expected reference to 'category', got {fk[2]}"
    assert fk[3] == "category_id", f"Expected column 'category_id', got {fk[3]}"
    assert fk[4] == "id", f"Expected reference to 'id', got {fk[4]}"
    assert fk[6] == "CASCADE", f"Expected ON DELETE CASCADE, got {fk[6]}"


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_foreign_key_constraint_exists_in_postgres(
    db_url, postgres_base_url, db_schema_name
):
    class Category(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        products: Relation[list["Product"]] = BackRef()

    class Product(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        category: Annotated[
            Category, ForeignKey(related_name="products", on_delete="CASCADE")
        ]

    await connect(db_url, auto_migrate=True)

    import psycopg

    with psycopg.connect(postgres_base_url) as conn:
        conn.execute(f'SET search_path TO "{db_schema_name}"')
        row = conn.execute(
            """
            SELECT
                ccu.table_name,
                kcu.column_name,
                ccu.column_name,
                rc.delete_rule
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.constraint_schema = kcu.constraint_schema
            JOIN information_schema.constraint_column_usage ccu
              ON ccu.constraint_name = tc.constraint_name
             AND ccu.constraint_schema = tc.constraint_schema
            JOIN information_schema.referential_constraints rc
              ON rc.constraint_name = tc.constraint_name
             AND rc.constraint_schema = tc.constraint_schema
            WHERE tc.constraint_type = 'FOREIGN KEY'
              AND tc.table_schema = %s
              AND tc.table_name = 'product'
            """,
            (db_schema_name,),
        ).fetchone()

    assert row is not None
    assert row[0] == "category"
    assert row[1] == "category_id"
    assert row[2] == "id"
    assert row[3] == "CASCADE"


@pytest.mark.asyncio
@pytest.mark.sqlite_only
async def test_foreign_key_index_runtime_ddl_parity(db_url):
    """Rust runtime DDL emits CREATE INDEX for ForeignKey(index=True)."""

    class Org(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        projects: Relation[list["Project"]] = BackRef()

    class Project(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        org: Annotated[
            Org,
            ForeignKey(related_name="projects", index=True),
        ]

    await connect(db_url, auto_migrate=True)

    db_path = db_url.replace("sqlite:", "").split("?")[0]
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT name, tbl_name, sql FROM sqlite_master "
        "WHERE type = 'index' AND tbl_name = 'project'"
    )
    indexes = cursor.fetchall()
    conn.close()

    matching = [row for row in indexes if row[0] == "idx_project_org_id"]
    assert matching, (
        f"Expected idx_project_org_id on table 'project', got: {indexes!r}"
    )
    assert "org_id" in (matching[0][2] or ""), (
        f"Index DDL should reference org_id column: {matching[0][2]!r}"
    )
