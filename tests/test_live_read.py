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
from ferro.migrations import MigrationRefused, runner
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
    run_report,
    settings_and_database,
)

pytestmark = [
    pytest.mark.usefixtures("isolated_imports", "clean_registry"),
    pytest.mark.backend_matrix,
]

AUTHOR_VIEW = (
    'CREATE VIEW "author" AS SELECT 1 AS "id", \'x\' AS "name", \'draft\' AS "status"'
)


PLAIN_AUTHOR = """
class Author(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    name: str
"""


def _relations(db) -> dict[str, str]:
    """Every table and view in the schema, with its catalog kind."""
    if db.backend == "sqlite":
        rows = db.rows(
            "SELECT name, type FROM sqlite_master WHERE type IN ('table', 'view') "
            "AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\'"
        )
    else:
        rows = db.rows(
            "SELECT table_name, table_type FROM information_schema.tables "
            f"WHERE table_schema = '{db.schema}'"
        )
    return {name: kind.lower() for name, kind in rows}


@pytest.mark.parametrize(
    "body", [AUTHOR, PLAIN_AUTHOR], ids=["with_unique", "without_index"]
)
def test_a_view_named_like_a_model_is_not_its_table_on_the_pass(
    project, pkg, db, body
):
    """The create pass sees that a view, not a table, holds ``author``, and
    refuses before any DDL, naming the view. It does not let
    ``CREATE TABLE IF NOT EXISTS`` skip over the view, with or without an
    index on the model, and it creates nothing."""
    configure(project, pkg, db.backend)
    write_models(project, pkg, body)
    _, database = settings_and_database()
    database.import_models()
    db.execute(AUTHOR_VIEW)

    with pytest.raises(MigrationRefused) as raised:
        asyncio.run(ferro.connect(db.url, auto_migrate=True))
    ferro.reset_engine()

    assert str(raised.value) == (
        "Table creation is refused: a declared table's name is held by something "
        "that is not a table, so CREATE TABLE would skip it and leave the model "
        "without one.\n"
        '  "author" is a view: rename or drop the view, or declare a different '
        f"__ferro_table__ on {pkg}.models.Author.\n"
        "Nothing was created."
    )
    assert _relations(db) == {"author": "view"}


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

    report = await run_report(runner.up(settings, database.name, url=db.url))

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
