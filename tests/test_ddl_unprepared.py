"""Migration DDL never enters a connection's prepared-statement cache.

A model with an index::

    class Widget(Model):
        id: int | None = Field(default=None, primary_key=True)
        name: str = Field(index=True)

is created by the create pass with::

    CREATE TABLE IF NOT EXISTS "widget" ( "id" serial PRIMARY KEY NOT NULL, ... )
    CREATE INDEX IF NOT EXISTS "idx_widget_name" ON "widget" ("name")

Those statements describe a schema the next migration may change, so they
must run unprepared, like every other DDL ferro executes
(`docs/solutions/patterns/ddl-on-live-engine.md`): a prepared statement
outlives the schema it was prepared against, on the connection it ran on.
On Postgres the server lists a connection's prepared statements in
``pg_prepared_statements``; with a pool of one connection the create pass
and the read below share it. The SQLite twin of this pin is the Rust unit
test ``the_create_pass_leaves_no_statement_in_the_connection_cache`` in
``src/schema.rs`` (SQLite has no catalog of a connection's statements).
"""

from __future__ import annotations

import enum

import pytest
from pydantic import Field

import ferro
from ferro import Model, PoolConfig

pytestmark = [pytest.mark.asyncio, pytest.mark.postgres_only]


class Shade(enum.Enum):
    LIGHT = "light"
    DARK = "dark"


@pytest.fixture
def widget():
    class Widget(Model):
        id: int | None = Field(default=None, json_schema_extra={"primary_key": True})
        name: str = Field(json_schema_extra={"index": True})
        shade: Shade = Shade.LIGHT

    return Widget


async def _cached_statements() -> list[str]:
    rows = await ferro.fetch_all(
        "SELECT statement FROM pg_prepared_statements", using="one"
    )
    return [row["statement"] for row in rows]


async def test_the_create_pass_prepares_none_of_its_ddl(db_url, widget):
    await ferro.connect(
        db_url,
        name="one",
        pool=PoolConfig(max_connections=1, min_connections=0),
    )
    await ferro.create_tables(using="one")

    ddl = [
        statement for statement in await _cached_statements() if '"widget"' in statement
    ]
    assert ddl == []
