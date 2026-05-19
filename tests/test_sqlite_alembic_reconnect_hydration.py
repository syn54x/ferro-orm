"""SQLite + Alembic schema: typed column hydration after reset_engine() / reconnect.

Most Ferro tests use auto_migrate=True (Rust DDL) and same-session reads (identity map).
Bugs like #56 (NULL -> int 0) and #58 (INTEGER Decimal -> None) only appear when:

- Schema comes from Alembic ``create_all`` (``auto_migrate=False``)
- Rows are re-fetched on a fresh ``ferro.connect()``
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import ferro
import pytest
from ferro import Field, Model, clear_registry, reset_engine
from ferro.migrations.alembic import get_metadata
from sqlalchemy import pool
from sqlalchemy.engine.url import URL
from sqlalchemy.ext.asyncio import create_async_engine

pytest.importorskip("aiosqlite")
pytest.importorskip("greenlet")

pytestmark = pytest.mark.sqlite_only


@pytest.fixture(autouse=True)
def cleanup() -> None:
    from ferro.state import _JOIN_TABLE_REGISTRY, _MODEL_REGISTRY_PY, _PENDING_RELATIONS

    _MODEL_REGISTRY_PY.clear()
    _PENDING_RELATIONS.clear()
    _JOIN_TABLE_REGISTRY.clear()
    reset_engine()
    clear_registry()
    yield


def _sqlite_url(path: Path) -> str:
    return URL.create(
        drivername="sqlite+aiosqlite",
        database=str(path.resolve()),
    ).render_as_string(hide_password=False)


@asynccontextmanager
async def _alembic_sqlite_db() -> AsyncIterator[tuple[str, Path]]:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "app.db"
        uri = f"sqlite:{db_path}?mode=rwc"
        engine = create_async_engine(_sqlite_url(db_path), poolclass=pool.NullPool)
        async with engine.begin() as conn:
            await conn.run_sync(lambda sync_conn: get_metadata().create_all(sync_conn))
        await engine.dispose()
        yield uri, db_path


async def _reload_row[M: Model](model: type[M], uri: str, row_id: str) -> M:
    reset_engine()
    await ferro.connect(uri, auto_migrate=False)
    row = await model.where(model.id == row_id).first()  # type: ignore[attr-defined]
    assert row is not None
    return row


def _sqlite_typeof(db_path: Path, table: str, column: str, row_id: str) -> tuple[Any, str]:
    with sqlite3.connect(db_path) as conn:
        value, affinity = conn.execute(
            f"SELECT {column}, typeof({column}) FROM {table} WHERE id = ?",
            (row_id,),
        ).fetchone()
    return value, affinity


@pytest.mark.asyncio
async def test_null_optional_datetime_is_none_not_zero() -> None:
    """Issue #56: NULL DATETIME must not hydrate as int(0)."""

    class Client(Model):
        id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True, db_type="text")
        name: str
        archived_at: datetime | None = None

    async with _alembic_sqlite_db() as (uri, db_path):
        await ferro.connect(uri, auto_migrate=False)
        created = await Client.create(name="Acme")
        row = await _reload_row(Client, uri, created.id)
        raw, affinity = _sqlite_typeof(db_path, "client", "archived_at", created.id)

    assert raw is None
    assert row.archived_at is None


@pytest.mark.asyncio
async def test_non_null_datetime_round_trips() -> None:
    class Event(Model):
        id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True, db_type="text")
        happened_at: datetime = Field(default_factory=lambda: datetime(2026, 4, 24, 18, 30, tzinfo=UTC))

    async with _alembic_sqlite_db() as (uri, db_path):
        await ferro.connect(uri, auto_migrate=False)
        expected = datetime(2026, 4, 24, 18, 30, tzinfo=UTC)
        created = await Event.create(happened_at=expected)
        row = await _reload_row(Event, uri, created.id)
        _sqlite_typeof(db_path, "event", "happened_at", created.id)

    assert row.happened_at == expected


@pytest.mark.asyncio
async def test_non_null_date_round_trips() -> None:
    class Day(Model):
        id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True, db_type="text")
        on_date: date

    async with _alembic_sqlite_db() as (uri, db_path):
        await ferro.connect(uri, auto_migrate=False)
        expected = date(2026, 4, 24)
        created = await Day.create(on_date=expected)
        row = await _reload_row(Day, uri, created.id)
        _sqlite_typeof(db_path, "day", "on_date", created.id)

    assert row.on_date == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("hours", "expected"),
    [
        (Decimal(3), Decimal(3)),
        (Decimal("1.5"), Decimal("1.5")),
        (Decimal("0"), Decimal("0")),
    ],
    ids=["integer_affinity", "real_affinity", "decimal_zero"],
)
async def test_decimal_round_trips(hours: Decimal, expected: Decimal) -> None:
    """Issue #58 class: NUMERIC columns must survive SQLite affinity + reconnect."""

    class Widget(Model):
        id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True, db_type="text")
        hours: Decimal

    async with _alembic_sqlite_db() as (uri, db_path):
        await ferro.connect(uri, auto_migrate=False)
        created = await Widget.create(hours=hours)
        row = await _reload_row(Widget, uri, created.id)
        raw, affinity = _sqlite_typeof(db_path, "widget", "hours", created.id)

    assert row.hours == expected
    if hours == Decimal(3):
        assert raw == 3
        assert affinity == "integer"


@pytest.mark.asyncio
async def test_decimal_string_literal_round_trips() -> None:
    """String INSERT into NUMERIC may coerce affinity; value must still hydrate."""

    class Widget(Model):
        id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True, db_type="text")
        hours: Decimal

    row_id = str(uuid4())
    async with _alembic_sqlite_db() as (uri, db_path):
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO widget (id, hours) VALUES (?, ?)",
                (row_id, "12.34"),
            )
        await ferro.connect(uri, auto_migrate=False)
        row = await _reload_row(Widget, uri, row_id)
        raw, affinity = _sqlite_typeof(db_path, "widget", "hours", row_id)

    assert row.hours == Decimal("12.34")
    assert affinity in ("text", "real")
    assert raw in ("12.34", 12.34)


@pytest.mark.asyncio
async def test_optional_decimal_null_is_none() -> None:
    class Line(Model):
        id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True, db_type="text")
        amount: Decimal | None = None

    async with _alembic_sqlite_db() as (uri, db_path):
        await ferro.connect(uri, auto_migrate=False)
        created = await Line.create()
        row = await _reload_row(Line, uri, created.id)
        raw, _ = _sqlite_typeof(db_path, "line", "amount", created.id)

    assert raw is None
    assert row.amount is None


@pytest.mark.asyncio
async def test_bool_round_trips() -> None:
    class Flag(Model):
        id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True, db_type="text")
        is_active: bool

    async with _alembic_sqlite_db() as (uri, db_path):
        await ferro.connect(uri, auto_migrate=False)
        created = await Flag.create(is_active=True)
        row = await _reload_row(Flag, uri, created.id)
        raw, affinity = _sqlite_typeof(db_path, "flag", "is_active", created.id)

    assert row.is_active is True
    assert raw in (1, True)
    assert affinity == "integer"


@pytest.mark.asyncio
async def test_uuid_round_trips() -> None:
    class Token(Model):
        id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True, db_type="text")
        subject_id: UUID

    token = uuid4()
    async with _alembic_sqlite_db() as (uri, db_path):
        await ferro.connect(uri, auto_migrate=False)
        created = await Token.create(subject_id=token)
        row = await _reload_row(Token, uri, created.id)
        _sqlite_typeof(db_path, "token", "subject_id", created.id)

    assert row.subject_id == token


@pytest.mark.asyncio
async def test_json_dict_round_trips() -> None:
    class Doc(Model):
        id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True, db_type="text")
        payload: dict[str, str]

    data = {"k": "v", "n": "2"}
    async with _alembic_sqlite_db() as (uri, db_path):
        await ferro.connect(uri, auto_migrate=False)
        created = await Doc.create(payload=data)
        row = await _reload_row(Doc, uri, created.id)
        raw, affinity = _sqlite_typeof(db_path, "doc", "payload", created.id)

    assert row.payload == data
    assert affinity == "text"
    assert json.loads(raw) == data


@pytest.mark.asyncio
async def test_non_null_int_zero_round_trips() -> None:
    """Legitimate integer 0 must not be confused with NULL (#56 regression guard)."""

    class Counter(Model):
        id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True, db_type="text")
        score: int

    async with _alembic_sqlite_db() as (uri, db_path):
        await ferro.connect(uri, auto_migrate=False)
        created = await Counter.create(score=0)
        row = await _reload_row(Counter, uri, created.id)
        raw, affinity = _sqlite_typeof(db_path, "counter", "score", created.id)

    assert row.score == 0
    assert raw == 0
    assert affinity == "integer"
