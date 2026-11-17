"""A datetime or time whose UTC offset is not whole minutes is refused on every write path."""

from datetime import UTC, datetime, time, timedelta, timezone
from typing import Annotated

import pytest
import pytest_asyncio
from pydantic import BaseModel

import ferro
from ferro import FerroField, Model

pytestmark = pytest.mark.backend_matrix

OFFSET_WITH_SECONDS = timezone(timedelta(hours=5, minutes=30, seconds=15))
PINNED_AT = datetime(2026, 3, 1, 15, 0, tzinfo=OFFSET_WITH_SECONDS)
UTC_PINNED_AT = datetime(2026, 3, 1, 15, 0, tzinfo=UTC)
REFUSAL = "not a whole number of minutes"
FIX = "value.astimezone(timezone.utc)"


class Window(BaseModel):
    opens_at: datetime


@pytest_asyncio.fixture
async def message_model(db_url, clean_registry):
    class Message(Model):
        id: Annotated[int, FerroField(primary_key=True)]
        pinned_at: datetime
        alarm: time | None = None
        seen_at: list[datetime] = []
        window: Window | None = None

    await ferro.connect(db_url, auto_migrate=True)
    async with ferro.engines.session():
        yield Message


@pytest.mark.asyncio
async def test_save_refuses_offset_seconds_and_stores_nothing(message_model):
    Message = message_model
    with pytest.raises(ValueError) as raised:
        await Message(id=1, pinned_at=PINNED_AT).save()
    for text in ("Message.pinned_at", REFUSAL, FIX):
        assert text in str(raised.value)
    assert await Message.all() == []


@pytest.mark.asyncio
async def test_bulk_create_refuses_offset_seconds(message_model):
    Message = message_model
    with pytest.raises(ValueError) as raised:
        await Message.bulk_create([Message(id=1, pinned_at=PINNED_AT)])
    for text in ("Message.pinned_at", REFUSAL, FIX):
        assert text in str(raised.value)
    assert await Message.all() == []


@pytest.mark.asyncio
async def test_query_literal_refuses_offset_seconds(message_model):
    Message = message_model
    with pytest.raises(ValueError) as raised:
        await Message.where(lambda message: message.pinned_at == PINNED_AT).all()
    assert FIX in str(raised.value)


@pytest.mark.asyncio
async def test_update_refuses_offset_seconds(message_model):
    Message = message_model
    with pytest.raises(ValueError) as raised:
        await Message.where(lambda message: message.id == 1).update(pinned_at=PINNED_AT)
    assert FIX in str(raised.value)


@pytest.mark.asyncio
async def test_raw_bind_refuses_offset_seconds(message_model):
    with pytest.raises(ValueError) as raised:
        await ferro.raw.fetch_all('SELECT id FROM "message" WHERE id = 1', PINNED_AT)
    assert FIX in str(raised.value)


@pytest.mark.asyncio
async def test_save_refuses_offset_seconds_nested_in_a_field(message_model):
    Message = message_model
    with pytest.raises(ValueError, match="Message.seen_at"):
        await Message(id=1, pinned_at=UTC_PINNED_AT, seen_at=[PINNED_AT]).save()
    with pytest.raises(ValueError, match="Message.window"):
        await Message(
            id=1, pinned_at=UTC_PINNED_AT, window=Window(opens_at=PINNED_AT)
        ).save()


@pytest.mark.asyncio
async def test_save_refuses_offset_seconds_on_time_and_sub_second(message_model):
    Message = message_model
    with pytest.raises(ValueError, match="whole minutes, or no tzinfo"):
        await Message(
            id=1, pinned_at=UTC_PINNED_AT, alarm=time(15, 0, tzinfo=OFFSET_WITH_SECONDS)
        ).save()
    sub_second = timezone(timedelta(hours=5, microseconds=5))
    with pytest.raises(ValueError, match=REFUSAL):
        await Message(id=1, pinned_at=datetime(2026, 3, 1, tzinfo=sub_second)).save()


@pytest.mark.asyncio
async def test_save_keeps_a_whole_minute_offset(message_model):
    Message = message_model
    half_hour = datetime(
        2026, 3, 1, 15, 0, tzinfo=timezone(timedelta(hours=5, minutes=30))
    )
    await Message(id=1, pinned_at=half_hour).save()
    async with ferro.engines.session():
        assert (await Message.get(1)).pinned_at == half_hour
