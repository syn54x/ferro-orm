import pytest
import uuid
import os
from datetime import datetime, date, timezone
from typing import Annotated
from ferro import Model, connect, FerroField


@pytest.fixture
def db_url():
    db_file = f"test_temp_{uuid.uuid4()}.db"
    url = f"sqlite:{db_file}?mode=rwc"
    yield url
    if os.path.exists(db_file):
        os.remove(db_file)


@pytest.mark.asyncio
async def test_temporal_types_roundtrip(db_url):
    """Test that datetime and date objects are correctly saved and hydrated."""

    class TemporalModel(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        occurred_at: datetime
        day: date

    await connect(db_url, auto_migrate=True)

    # Create fixed timestamps (stripping microseconds for SQLite TEXT comparison stability)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    today = date.today()

    item = await TemporalModel.create(occurred_at=now, day=today)
    item_id = item.id

    # Force eviction from Identity Map to test database hydration
    from ferro import evict_instance

    evict_instance("TemporalModel", str(item_id))

    fetched = await TemporalModel.get(item_id)
    assert fetched is not None

    # Assertions: Pydantic should have converted the stored strings back to objects
    assert isinstance(fetched.occurred_at, datetime)
    assert isinstance(fetched.day, date)

    # Values should match (within precision)
    # Note: SQLite stores as ISO strings, Pydantic parses them back.
    assert fetched.occurred_at.isoformat() == now.isoformat()
    assert fetched.day == today


@pytest.mark.asyncio
async def test_temporal_filtering(db_url):
    """Test that we can filter records using datetime and date objects."""

    class TemporalModel(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        occurred_at: datetime

    await connect(db_url, auto_migrate=True)

    past = datetime(2020, 1, 1, tzinfo=timezone.utc)
    future = datetime(2030, 1, 1, tzinfo=timezone.utc)

    await TemporalModel.create(occurred_at=past)
    await TemporalModel.create(occurred_at=future)

    now = datetime(2025, 1, 1, tzinfo=timezone.utc)

    # Filter for future events
    future_events = await TemporalModel.where(TemporalModel.occurred_at > now).all()
    assert len(future_events) == 1
    assert future_events[0].occurred_at.year == 2030

    # Filter for past events
    past_events = await TemporalModel.where(TemporalModel.occurred_at < now).all()
    assert len(past_events) == 1
    assert past_events[0].occurred_at.year == 2020
