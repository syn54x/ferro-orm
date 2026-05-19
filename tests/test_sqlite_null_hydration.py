"""SQLite NULL columns must hydrate as None, not int 0 (issue #56)."""

from __future__ import annotations

import sqlite3
import tempfile
from datetime import datetime
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
async def test_sqlite_null_optional_datetime_hydrates_as_none_after_reconnect() -> None:
    """Alembic-created DATETIME NULL must not become int(0) on a fresh connection."""

    class Client(Model):
        id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True, db_type="text")
        name: str
        archived_at: datetime | None = None

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "app.db"
        uri = f"sqlite:{db_path}?mode=rwc"

        engine = create_async_engine(_sqlite_url(db_path), poolclass=pool.NullPool)
        async with engine.begin() as conn:
            await conn.run_sync(lambda sync_conn: get_metadata().create_all(sync_conn))
        await engine.dispose()

        await ferro.connect(uri, auto_migrate=False)
        await Client.create(name="Acme")
        ferro.reset_engine()

        await ferro.connect(uri, auto_migrate=False)
        row = (await Client.all())[0]

        with sqlite3.connect(db_path) as conn:
            raw = conn.execute("SELECT archived_at FROM client").fetchone()[0]

    assert raw is None
    assert row.archived_at is None
