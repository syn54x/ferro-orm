import pytest
import uuid
import os
from typing import Annotated
from ferro import Model, connect, FerroField


@pytest.fixture
def db_url():
    db_file = f"test_agg_{uuid.uuid4()}.db"
    url = f"sqlite:{db_file}?mode=rwc"
    yield url
    if os.path.exists(db_file):
        os.remove(db_file)


@pytest.mark.asyncio
async def test_count_operation(db_url):
    """Test that .count() correctly returns the number of records."""

    class AggProduct(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        price: float
        category: str

    await connect(db_url, auto_migrate=True)

    await AggProduct(name="Item 1", price=10.0, category="A").save()
    await AggProduct(name="Item 2", price=20.0, category="A").save()
    await AggProduct(name="Item 3", price=30.0, category="B").save()

    # Total count
    assert await AggProduct.where(AggProduct.id >= 0).count() == 3

    # Filtered count
    assert await AggProduct.where(AggProduct.category == "A").count() == 2
    assert await AggProduct.where(AggProduct.price > 25).count() == 1
    assert await AggProduct.where(AggProduct.category == "C").count() == 0


@pytest.mark.asyncio
async def test_order_by_operation(db_url):
    """Test that .order_by() correctly sorts results."""

    class AggProduct(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        price: float
        category: str

    await connect(db_url, auto_migrate=True)

    await AggProduct(name="Z", price=100.0, category="X").save()
    await AggProduct(name="A", price=50.0, category="Y").save()
    await AggProduct(name="M", price=75.0, category="X").save()

    # Sort by price ascending
    results = (
        await AggProduct.where(AggProduct.id >= 0).order_by(AggProduct.price).all()
    )
    assert [r.name for r in results] == ["A", "M", "Z"]

    # Sort by name descending
    results = (
        await AggProduct.where(AggProduct.id >= 0)
        .order_by(AggProduct.name, direction="desc")
        .all()
    )
    assert [r.name for r in results] == ["Z", "M", "A"]

    # Sort by category (asc), then price (desc)
    results = (
        await AggProduct.where(AggProduct.id >= 0)
        .order_by(AggProduct.category)
        .order_by(AggProduct.price, direction="desc")
        .all()
    )
    assert [r.name for r in results] == ["Z", "M", "A"]
