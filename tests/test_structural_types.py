import pytest
import uuid
import os
from decimal import Decimal
from enum import Enum
from typing import Annotated, Dict, List
from ferro import Model, connect, FerroField

class UserRole(str, Enum):
    ADMIN = "admin"
    USER = "user"

@pytest.fixture
def db_url():
    db_file = f"test_struct_{uuid.uuid4()}.db"
    url = f"sqlite:{db_file}?mode=rwc"
    yield url
    if os.path.exists(db_file):
        os.remove(db_file)

@pytest.mark.asyncio
async def test_structural_types_roundtrip(db_url):
    """Test that UUID, JSON, Enum, BLOB, and Decimal objects are correctly saved and hydrated."""
    class ComplexModel(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        user_id: uuid.UUID
        metadata: Dict[str, str]
        tags: List[str]
        role: UserRole
        data: bytes
        balance: Decimal

    await connect(db_url, auto_migrate=True)
    
    uid = uuid.uuid4()
    raw_data = b"hello world"
    balance = Decimal("123.456")
    
    item = await ComplexModel.create(
        user_id=uid,
        metadata={"key": "value"},
        tags=["a", "b"],
        role=UserRole.ADMIN,
        data=raw_data,
        balance=balance
    )
    item_id = item.id
    
    # Force eviction from Identity Map to test database hydration
    from ferro import evict_instance
    evict_instance("ComplexModel", str(item_id))
    
    fetched = await ComplexModel.get(item_id)
    assert fetched is not None
    
    # Assertions
    assert isinstance(fetched.user_id, uuid.UUID)
    assert fetched.user_id == uid
    
    assert isinstance(fetched.metadata, dict)
    assert fetched.metadata == {"key": "value"}
    
    assert isinstance(fetched.tags, list)
    assert fetched.tags == ["a", "b"]
    
    assert isinstance(fetched.role, UserRole)
    assert fetched.role == UserRole.ADMIN
    
    assert isinstance(fetched.data, bytes)
    assert fetched.data == raw_data
    
    assert isinstance(fetched.balance, Decimal)
    assert fetched.balance == balance

@pytest.mark.asyncio
async def test_structural_filtering(db_url):
    """Test filtering by UUID and Decimal."""
    class ComplexModel(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        user_id: uuid.UUID
        balance: Decimal

    await connect(db_url, auto_migrate=True)
    
    uid1 = uuid.uuid4()
    uid2 = uuid.uuid4()
    
    await ComplexModel.create(user_id=uid1, balance=Decimal("10.0"))
    await ComplexModel.create(user_id=uid2, balance=Decimal("20.0"))
    
    # Filter by UUID
    res = await ComplexModel.where(ComplexModel.user_id == uid1).first()
    assert res is not None
    assert res.user_id == uid1
    
    # Filter by Decimal
    res = await ComplexModel.where(ComplexModel.balance > Decimal("15.0")).first()
    assert res is not None
    assert res.balance == Decimal("20.0")
