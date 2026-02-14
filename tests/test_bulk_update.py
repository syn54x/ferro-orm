import pytest
import uuid
import os
from typing import Annotated
from ferro import Model, connect, FerroField


@pytest.fixture
def db_url():
    db_file = f"test_bulk_{uuid.uuid4()}.db"
    url = f"sqlite:{db_file}?mode=rwc"
    yield url
    if os.path.exists(db_file):
        os.remove(db_file)


@pytest.mark.asyncio
async def test_bulk_update_operation(db_url):
    """Test that .update(**kwargs) correctly modifies multiple records."""

    class BulkProduct(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        in_stock: bool
        category: str

    await connect(db_url, auto_migrate=True)

    await BulkProduct(name="P1", in_stock=True, category="Electronics").save()
    await BulkProduct(name="P2", in_stock=True, category="Electronics").save()
    await BulkProduct(name="P3", in_stock=True, category="Furniture").save()

    # Bulk update: set in_stock=False for all Electronics
    updated_count = await BulkProduct.where(
        BulkProduct.category == "Electronics"
    ).update(in_stock=False)
    assert updated_count == 2

    # Verify results
    electronics = await BulkProduct.where(BulkProduct.category == "Electronics").all()
    assert all(p.in_stock is False for p in electronics)

    furniture = await BulkProduct.where(BulkProduct.category == "Furniture").all()
    assert furniture[0].in_stock is True


@pytest.mark.asyncio
async def test_bulk_update_evicts_identity_map(db_url):
    """Test that bulk update evicts objects from the Identity Map to prevent stale data."""

    class BulkProduct(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        price: float

    await connect(db_url, auto_migrate=True)

    p1 = BulkProduct(name="Gadget", price=10.0)
    await p1.save()

    # Ensure it's in Identity Map
    cached_p1 = await BulkProduct.get(p1.id)
    assert cached_p1 is p1

    # Update price via bulk query
    await BulkProduct.where(BulkProduct.id == p1.id).update(price=20.0)

    # Fetching again should NOT return the old 'p1' object (it should be a fresh object or re-hydrated)
    fresh_p1 = await BulkProduct.get(p1.id)
    assert fresh_p1 is not p1
    assert fresh_p1.price == 20.0
