# ruff: noqa: F811 - pytest fixtures imported from a sibling module are redefined as arguments
"""One live read (ADR-0047 amendment): every door reads the same live tables.

A database holding a view named ``author`` beside a model ``Author``::

    CREATE VIEW "author" AS SELECT 1 AS "id", 'x' AS "name", 'draft' AS "status";

has no ``author`` table. The reconciliation pass, ``drift`` and the adoption
refusal of ``ferro migrate up`` all say so, because all three ask one reader
which tables are live, and a live table is a base table: never a view, never
SQLite's own ``sqlite_*`` bookkeeping.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import ferro
from ferro import _core
from ferro.ir.compiler import compile_registry_schema_ir
from ferro.migrations import runner
from tests.test_migrate_drift import HEAD, applied, drift_api
from tests.test_migrate_new import (  # noqa: F401 - fixtures
    AUTHOR,
    pkg,
    project,
    write_models,
)
from tests.test_migrate_up import (  # noqa: F401
    configure,
    db,
    new,
    settings_and_database,
)

pytestmark = [
    pytest.mark.usefixtures("isolated_imports", "clean_registry"),
    pytest.mark.backend_matrix,
]

AUTHOR_VIEW = (
    'CREATE VIEW "author" AS SELECT 1 AS "id", \'x\' AS "name", \'draft\' AS "status"'
)


def test_a_view_named_like_a_model_is_not_its_table_on_the_pass(project, pkg, db):
    """The create pass sees no ``author`` table, so it builds one; the
    database refuses to index a view, and the connect fails naming it,
    instead of the view standing in for the table in silence."""
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    _, database = settings_and_database()
    database.import_models()
    db.execute(AUTHOR_VIEW)

    with pytest.raises(
        ferro.exceptions.OperationalError,
        match=r'CREATE UNIQUE INDEX IF NOT EXISTS "uq_author_name" ON "author"',
    ):
        asyncio.run(ferro.connect(db.url, auto_migrate=True))
    ferro.reset_engine()


def test_a_view_named_like_a_snapshot_table_is_drift(project, pkg, db, capsys):
    """``drift`` reads the view as no table at all: the snapshot's ``team``
    is missing, not a table without its index and check."""
    applied(project, pkg, db, capsys)
    db.execute('DROP TABLE "team"')
    db.execute('CREATE VIEW "team" AS SELECT 1 AS "id", \'x\' AS "name", 0 AS "size"')

    report = drift_api(db)
    assert report.against == HEAD
    assert report.lines == ["team table is missing"]


@pytest.mark.asyncio
async def test_a_view_named_like_a_model_does_not_trip_the_adoption_refusal(
    project, pkg, db
):
    """A database with no records whose only ``author`` is a view holds no
    table from 0001, so ``up`` does not refuse with "run ferro migrate
    baseline" — it runs 0001, and the database refuses to index the view."""
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    settings, database = settings_and_database()
    db.execute(AUTHOR_VIEW)

    report = await runner.up(settings, database, url=db.url)

    assert report.refusal is not None
    assert report.refusal.startswith(
        f"ferro migrate: 0001_create_author/01_schema.up.{db.backend}.sql failed: "
    ), report.refusal
    assert report.applied == []


def test_sqlite_bookkeeping_tables_are_never_read(project, pkg, db):
    """``sqlite_sequence`` is SQLite's, never schema: asked for by name, the
    live read still leaves it out."""
    if db.backend != "sqlite":
        pytest.skip("sqlite_* tables are SQLite's own")
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    _, database = settings_and_database()
    database.import_models()

    async def read() -> list[str]:
        await ferro.connect(db.url, auto_migrate=True)
        try:
            live, _ = await _core._live_schema_ir(
                None,
                json.dumps(compile_registry_schema_ir()),
                json.dumps(["sqlite_sequence"]),
            )
        finally:
            ferro.reset_engine()
        return [model["table_name"] for model in json.loads(live)["payload"]["models"]]

    db.execute('CREATE TABLE "tally" ("id" integer PRIMARY KEY AUTOINCREMENT)')
    db.execute('INSERT INTO "tally" DEFAULT VALUES')
    assert "sqlite_sequence" in db.tables()
    assert asyncio.run(read()) == ["author"]
