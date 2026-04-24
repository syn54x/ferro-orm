import sqlite3
from typing import Annotated

import pytest

from ferro import (
    BackRef,
    Field,
    FerroField,
    ForeignKey,
    Model,
    clear_registry,
    connect,
    reset_engine,
)

pytestmark = pytest.mark.sqlite_only


@pytest.fixture(autouse=True)
def cleanup():
    reset_engine()
    clear_registry()
    yield


@pytest.mark.asyncio
async def test_runtime_create_tables_respects_explicit_nullable_override():
    """Rust DDL should honor the same explicit nullable override that Alembic sees."""
    db_path = "test_nullable_override.db"
    import os

    if os.path.exists(db_path):
        os.remove(db_path)

    url = f"sqlite:{db_path}?mode=rwc"

    class NullableOverrideRow(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        field_a: int | None = Field(default=None, nullable=False)

    await connect(url, auto_migrate=True)

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info('nullableoverriderow')")
    columns = {row[1]: row for row in cursor.fetchall()}
    conn.close()

    assert columns["field_a"][3] == 1, "field_a should be NOT NULL in runtime DDL"

    if os.path.exists(db_path):
        os.remove(db_path)


@pytest.mark.asyncio
async def test_foreign_key_constraint_exists():
    """Verify that Rust generates the actual FOREIGN KEY constraint in SQL."""
    db_path = "test_constraints.db"
    import os

    if os.path.exists(db_path):
        os.remove(db_path)

    url = f"sqlite:{db_path}?mode=rwc"

    class Category(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        products: BackRef[list["Product"]] = None

    class Product(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        category: Annotated[
            Category, ForeignKey(related_name="products", on_delete="CASCADE")
        ]

    # 1. Connect and Migrate
    await connect(url, auto_migrate=True)

    # 2. Inspect the SQLite schema directly
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

    if os.path.exists(db_path):
        os.remove(db_path)
