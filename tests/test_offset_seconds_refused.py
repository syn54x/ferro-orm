"""A datetime or time whose UTC offset is not whole minutes is refused on every write path."""

import signal
from contextlib import contextmanager
from dataclasses import dataclass
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


@dataclass
class Span:
    opens_at: datetime


@pytest_asyncio.fixture
async def message_model(db_url, clean_registry):
    class Message(Model):
        id: Annotated[int, FerroField(primary_key=True)]
        pinned_at: datetime
        alarm: time | None = None
        seen_at: list[datetime] = []
        window: Window | None = None
        span: Span | None = None
        marks: dict[datetime, int] = {}
        stamps: frozenset[datetime] = frozenset()
        tree: dict | None = None
        chain: list | None = None

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


def _save(field, value):
    return lambda Message: Message(
        id=1, pinned_at=UTC_PINNED_AT, **{field: value}
    ).save()


def _update(field, value):
    return lambda Message: Message.where(lambda message: message.id == 1).update(
        **{field: value}
    )


@pytest.mark.parametrize(
    ("write", "names"),
    [
        pytest.param(_save("seen_at", [PINNED_AT]), "Message.seen_at", id="save-list"),
        pytest.param(
            _save("window", Window(opens_at=PINNED_AT)),
            "Message.window",
            id="save-nested-model",
        ),
        pytest.param(
            _save("span", Span(opens_at=PINNED_AT)), "Message.span", id="save-dataclass"
        ),
        pytest.param(
            _save("marks", {PINNED_AT: 1}), "Message.marks", id="save-dict-key"
        ),
        pytest.param(
            _save("stamps", frozenset({PINNED_AT})),
            "Message.stamps",
            id="save-frozenset",
        ),
        pytest.param(
            _update("window", Window(opens_at=PINNED_AT)), FIX, id="update-nested-model"
        ),
        pytest.param(
            _update("span", Span(opens_at=PINNED_AT)), FIX, id="update-dataclass"
        ),
        pytest.param(_update("marks", {PINNED_AT: 1}), FIX, id="update-dict-key"),
        pytest.param(
            _update("stamps", frozenset({PINNED_AT})), FIX, id="update-frozenset"
        ),
    ],
)
@pytest.mark.asyncio
async def test_offset_seconds_refused_in_every_container_shape(
    message_model, statement_records, write, names
):
    Message = message_model
    statement_records.clear()
    with pytest.raises(ValueError) as raised:
        await write(Message)
    assert REFUSAL in str(raised.value)
    assert names in str(raised.value)
    assert [record.sql for record in statement_records] == []


@contextmanager
def _fails_after(seconds: float):
    """Turn a hang into a failure: SIGALRM interrupts even a busy Python loop."""

    def _expire(signum, frame):
        raise TimeoutError(f"still running after {seconds}s")

    previous = signal.signal(signal.SIGALRM, _expire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _cyclic_dict():
    root = {"name": "root", "children": []}
    root["children"].append({"name": "leaf", "parent": root})
    return root


def _cyclic_list():
    chain = ["head"]
    chain.append(chain)
    return chain


@pytest.mark.skipif(not hasattr(signal, "setitimer"), reason="needs SIGALRM")
@pytest.mark.parametrize(
    ("write", "names"),
    [
        pytest.param(_save("tree", _cyclic_dict()), "Message.tree", id="save-dict"),
        pytest.param(_save("chain", _cyclic_list()), "Message.chain", id="save-list"),
        pytest.param(_update("tree", _cyclic_dict()), "", id="update-dict"),
        pytest.param(_update("chain", _cyclic_list()), "", id="update-list"),
    ],
)
@pytest.mark.asyncio
async def test_circular_value_is_refused_without_hanging(
    message_model, statement_records, write, names
):
    Message = message_model
    statement_records.clear()
    with _fails_after(5), pytest.raises(ValueError) as raised:
        await write(Message)
    assert "refers to itself" in str(raised.value)
    assert names in str(raised.value)
    assert [record.sql for record in statement_records] == []


@pytest.mark.asyncio
async def test_shared_container_reached_twice_saves(message_model):
    Message = message_model
    shared = {"at": "2026-03-01T15:00:00Z"}
    tree = {"first": shared, "second": shared}
    await Message(
        id=1, pinned_at=UTC_PINNED_AT, tree=tree, chain=[shared, shared]
    ).save()
    async with ferro.engines.session():
        fetched = await Message.get(1)
    assert fetched.tree == tree
    assert fetched.chain == [shared, shared]


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
