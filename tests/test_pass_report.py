"""``ferro.migrate()`` and ``ferro.create_tables()`` return what the pass
executed (ADR-0049).

```python
class Author(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    name: str
    slug: str | None = None  # the edit

report = await ferro.migrate()
[(s.subject, s.sql) for s in report.statements if s.role == "schema"]
# [('author', 'ALTER TABLE "author" ADD COLUMN "slug" varchar')]
```

The report is built from what the DDL executor ran, never from the plan; a
pass that fails partway raises with ``.report`` set to what committed.
"""

from __future__ import annotations

import dataclasses
from typing import Annotated

import pytest

import ferro
from ferro import ExecutedStatement, Model, PassReport
from ferro.base import FerroField
from ferro.pass_report import Report, Subject
from ferro.raw import execute

pytestmark = pytest.mark.backend_matrix


@pytest.fixture(autouse=True)
def _fresh(clean_registry):
    yield
    ferro.reset_engine()


def _declare_author(*, slug: bool) -> None:
    if slug:

        class Author(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            name: str
            slug: str | None = None

    else:

        class Author(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            name: str


def _rewind() -> None:
    from ferro.registry import REGISTRY

    ferro.reset_engine()
    ferro.clear_registry()
    REGISTRY.reset_for_test()


@pytest.mark.asyncio
async def test_migrate_returns_every_statement_it_ran_in_order(db_url, db_backend):
    _declare_author(slug=False)
    await ferro.connect(db_url)
    created = await ferro.create_tables()
    assert isinstance(created, PassReport)
    created_schema = [s for s in created.statements if s.role == "schema"]
    assert [s.subject for s in created_schema] == ["author"]
    assert created_schema[0].sql.startswith('CREATE TABLE IF NOT EXISTS "author"')
    assert created.warnings == ()

    _rewind()
    _declare_author(slug=True)
    await ferro.connect(db_url)
    report = await ferro.migrate()

    schema = [(s.subject, s.sql) for s in report.statements if s.role == "schema"]
    assert schema == [("author", 'ALTER TABLE "author" ADD COLUMN "slug" varchar')]
    # Postgres runs the table's unit under its lock timeout (ADR-0044); the
    # report lists that statement too, as the executor's own.
    timeouts = [s.sql for s in report.statements if s.role == "lock_timeout"]
    assert timeouts == (
        ["SET LOCAL lock_timeout = '5000ms'"] if db_backend == "postgres" else []
    )
    assert report.warnings == ()

    # Nothing left to do: an empty report.
    _rewind()
    _declare_author(slug=True)
    await ferro.connect(db_url)
    assert await ferro.migrate() == PassReport()


def test_the_report_is_frozen_and_its_warnings_read_as_their_sentence():
    statement = ExecutedStatement("author", "SELECT 1", "probe")
    with pytest.raises(dataclasses.FrozenInstanceError):
        statement.sql = "x"  # type: ignore[misc]
    warning = Report("RunLockWait", Subject("modelset"), "migrate() is waiting", True)
    assert str(warning) == "migrate() is waiting"
    report = PassReport((statement,), (warning,))
    with pytest.raises(dataclasses.FrozenInstanceError):
        report.warnings = ()  # type: ignore[misc]


def test_a_wire_report_reads_kinds_with_fields_by_their_name():
    report = PassReport._from_json(
        '{"statements": [{"subject": "t", "sql": "ALTER", "role": "schema"}],'
        ' "warnings": [{"kind": {"LeftoverChecks": {"names": ["ck_t_x"]}},'
        ' "subject": {"scope": "table", "table": "t"}, "text": "leftover",'
        ' "recurs": false, "blocks": false}]}'
    )
    assert report.statements == (ExecutedStatement("t", "ALTER", "schema"),)
    assert report.warnings == (
        Report("LeftoverChecks", Subject("table", table="t"), "leftover", False),
    )


@pytest.mark.asyncio
async def test_a_warning_is_raised_and_listed_typed(db_url, db_backend):
    """A SQLite type change the pass cannot make in place: the same warning is
    raised as a ``UserWarning`` and listed in the report, with its kind."""
    if db_backend != "sqlite":
        pytest.skip("the in-place limit is SQLite's")

    class Gadget(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        size: str

    await ferro.connect(db_url)
    await ferro.create_tables()
    _rewind()

    class Gadget(Model):  # noqa: F811 - the edited model
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        size: int

    await ferro.connect(db_url)
    with pytest.warns(UserWarning) as raised:
        report = await ferro.migrate()

    assert [(w.kind, w.subject) for w in report.warnings] == [
        ("SqliteInPlace", Subject("column", table="gadget", column="size"))
    ]
    assert f"ferro auto-migrate: {report.warnings[0]}" in [
        str(w.message) for w in raised
    ]


@pytest.mark.asyncio
@pytest.mark.postgres_only
async def test_a_failed_pass_carries_what_committed_before_it(db_url):
    """Each table is its own transaction on Postgres: ``aaledger`` commits,
    then ``zzledger``'s cast fails and rolls back. The error's ``.report``
    is the record of which tables changed."""

    class AaLedger(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        note: str | None = None

    class ZzLedger(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        amount: int | None = None

    await ferro.connect(db_url)
    async with ferro.engines.session():
        await execute('CREATE TABLE "aaledger" ("id" serial PRIMARY KEY)')
        await execute('CREATE TABLE "zzledger" ("id" serial PRIMARY KEY, "amount" varchar)')
        await execute("INSERT INTO \"zzledger\" (\"amount\") VALUES ('not-a-number')")

    with pytest.raises(Exception, match="Auto-migrate DDL failed") as raised:
        await ferro.migrate()

    report = raised.value.report
    assert [(s.subject, s.role, s.sql) for s in report.statements] == [
        ("aaledger", "lock_timeout", "SET LOCAL lock_timeout = '5000ms'"),
        ("aaledger", "schema", 'ALTER TABLE "aaledger" ADD COLUMN "note" varchar'),
    ]


@pytest.mark.asyncio
async def test_connect_carries_the_report_on_its_error_too(db_url):
    """``connect()`` returns nothing, but a pass it ran that fails raises with
    the same ``.report`` (here: refused before any DDL, so empty)."""
    from ferro.migrations import MigrationRefused

    await ferro.connect(db_url)
    async with ferro.engines.session():
        await execute('CREATE VIEW "heldview" AS SELECT 1 AS "id"')
    ferro.reset_engine()

    class HeldView(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None

    with pytest.raises(MigrationRefused) as raised:
        await ferro.connect(db_url, auto_migrate=True)
    assert raised.value.report == PassReport()
