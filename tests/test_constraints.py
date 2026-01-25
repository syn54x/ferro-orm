import pytest
import uuid
import os
from typing import Annotated
from ferro import Model, connect, FerroField

@pytest.fixture
def db_url():
    db_file = f"test_const_{uuid.uuid4()}.db"
    url = f"sqlite:{db_file}?mode=rwc"
    yield url
    if os.path.exists(db_file):
        os.remove(db_file)

@pytest.mark.asyncio
async def test_unique_constraint(db_url):
    """Test that unique=True correctly enforces database uniqueness."""
    class UniqueUser(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        email: Annotated[str, FerroField(unique=True)]

    await connect(db_url, auto_migrate=True)
    
    # Save first user
    await UniqueUser(email="test@example.com").save()
    
    # Attempt to save second user with same email should fail
    with pytest.raises(Exception) as excinfo:
        await UniqueUser(email="test@example.com").save()
    
    assert "UNIQUE constraint failed" in str(excinfo.value) or "uniqueness" in str(excinfo.value).lower()

@pytest.mark.asyncio
async def test_index_creation(db_url):
    """Test that index=True creates an index (verified via SQLite schema)."""
    class IndexedProduct(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        sku: Annotated[str, FerroField(index=True)]

    await connect(db_url, auto_migrate=True)
    
    # Verify index exists in SQLite
    import sqlite3
    db_path = db_url.replace("sqlite:", "").split("?")[0]
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    # SQLite stores indexes in sqlite_master
    cursor.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='indexedproduct' AND name LIKE '%sku%';")
    index = cursor.fetchone()
    conn.close()
    
    assert index is not None
    assert "sku" in index[0]
