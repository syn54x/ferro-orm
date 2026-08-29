import json
from typing import Annotated

import pytest

from ferro import Model, connect, FerroField, engines
from ferro._core import update_filtered
from ferro.query.wire import compile_query, model_identity
from ferro.state import resolve_operation_scope

pytestmark = pytest.mark.backend_matrix


@pytest.mark.asyncio
async def test_bulk_update_operation(db_url):
    """Test that .update(**kwargs) correctly modifies multiple records."""

    class BulkProduct(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        in_stock: bool
        category: str

    await connect(db_url, auto_migrate=True)
    async with engines.session():

        await BulkProduct(name="P1", in_stock=True, category="Electronics").save()
        await BulkProduct(name="P2", in_stock=True, category="Electronics").save()
        await BulkProduct(name="P3", in_stock=True, category="Furniture").save()

        # Bulk update: set in_stock=False for all Electronics
        updated_count = await BulkProduct.where(
            lambda p: p.category == "Electronics"
        ).update(in_stock=False)
        assert updated_count == 2

        # Verify results
        electronics = await BulkProduct.where(
            lambda p: p.category == "Electronics"
        ).all()
        assert all(p.in_stock is False for p in electronics)

        furniture = await BulkProduct.where(lambda p: p.category == "Furniture").all()
        assert furniture[0].in_stock is True


@pytest.mark.asyncio
async def test_bulk_update_evicts_identity_map(db_url):
    """Test that bulk update evicts objects from the Identity Map to prevent stale data."""

    class BulkProduct(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str
        price: float

    await connect(db_url, auto_migrate=True)
    async with engines.session():

        p1 = BulkProduct(name="Gadget", price=10.0)
        await p1.save()

        # Ensure it's in Identity Map
        cached_p1 = await BulkProduct.get(p1.id)
        assert cached_p1 is p1

        # Update price via bulk query
        await BulkProduct.where(lambda p: p.id == p1.id).update(price=20.0)

        # Fetching again should NOT return the old 'p1' object (it should be a fresh object or re-hydrated)
        fresh_p1 = await BulkProduct.get(p1.id)
        assert fresh_p1 is not p1
        assert fresh_p1.price == 20.0


@pytest.mark.asyncio
async def test_bulk_update_rejects_integer_outside_native_bind_range(db_url):
    class Counter(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        value: int

    await connect(db_url, auto_migrate=True)
    async with engines.session():
        counter = Counter(value=1)
        await counter.save()

        with pytest.raises(ValueError, match="int|number out of range"):
            await Counter.where(lambda row: row.id == counter.id).update(value=2**100)

        fresh = await Counter.get(counter.id)
        assert fresh.value == 1


@pytest.mark.asyncio
async def test_bulk_update_rejects_query_ir_model_identity_mismatch(db_url):
    class Source(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        value: str

    class Other(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        value: str

    await connect(db_url, auto_migrate=True)
    async with engines.session():
        source = Source(value="original")
        await source.save()

        envelope = json.loads(
            compile_query(
                Source.where(lambda row: row.id == source.id),
                "update",
                assignments={"value": "mutated"},
            ).wire_json
        )
        source_identity = model_identity(Source)
        other_identity = model_identity(Other)
        envelope["payload"]["model_name"] = other_identity
        route = resolve_operation_scope(using=None, session=None)

        with pytest.raises(ValueError) as exc_info:
            await update_filtered(
                source_identity,
                json.dumps(envelope, separators=(",", ":")),
                route,
            )

        message = str(exc_info.value)
        assert source_identity in message
        assert other_identity in message
        assert (await Source.get(source.id)).value == "original"
