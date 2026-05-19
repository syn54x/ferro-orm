"""SQLite INTEGER-stored NUMERIC Decimal must hydrate as Decimal after reconnect (issue #58)."""

from __future__ import annotations

import sqlite3
import tempfile
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import ferro
import pytest
from ferro import Field, Model, clear_registry, reset_engine
from ferro.migrations.alembic import get_metadata
from sqlalchemy import pool
from sqlalchemy.engine.url import URL
from sqlalchemy.ext.asyncio import create_async_engine

pytest.importorskip("aiosqlite")
pytest.importorskip("greenlet")


@pytest.fixture(autouse=True)
def cleanup():
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


@pytest.mark.asyncio
async def test_sqlite_integer_decimal_hydrates_after_reconnect() -> None:
    """Whole-number Decimal stored with INTEGER affinity must reload as Decimal."""

    class Widget(Model):
        id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True, db_type="text")
        hours: Decimal

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "repro.db"
        uri = f"sqlite:{db_path}?mode=rwc"

        engine = create_async_engine(_sqlite_url(db_path), poolclass=pool.NullPool)
        async with engine.begin() as conn:
            await conn.run_sync(lambda sync_conn: get_metadata().create_all(sync_conn))
        await engine.dispose()

        await ferro.connect(uri, auto_migrate=False)
        created = await Widget.create(hours=Decimal(3))
        assert created.hours == Decimal(3)

        reset_engine()
        await ferro.connect(uri, auto_migrate=False)
        fresh = await Widget.where(Widget.id == created.id).first()
        assert fresh is not None

        with sqlite3.connect(db_path) as conn:
            raw_value, raw_type = conn.execute(
                "SELECT hours, typeof(hours) FROM widget WHERE id = ?",
                (created.id,),
            ).fetchone()

        assert raw_value == 3
        assert raw_type == "integer"
        assert fresh.hours == Decimal(3)
