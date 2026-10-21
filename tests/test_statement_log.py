# ruff: noqa: F811 - pytest fixtures imported from a sibling module are redefined as arguments
"""The statement log on migration DDL (#764, ADR-0057).

```python
logging.getLogger("ferro.sql").setLevel(logging.DEBUG)
report = await ferro.migrate()
# ferro.sql DEBUG CREATE TABLE "post" (...) params=() 1.2ms
# ferro.sql DEBUG ALTER TABLE "author" ADD COLUMN "bio" varchar params=() 0.8ms
```

Every statement the reconciliation pass or a run sends is one record on
``ferro.sql`` as it finishes, carrying ``sql``, ``params``, ``dialect``,
``duration_ms``, ``operation``, ``subject``, ``connection``, ``rows`` and
``error``. The logger is the only switch: with ``ferro.sql`` off, nothing is
collected and the emitter is never called.
"""

from __future__ import annotations

import logging
from typing import Annotated

import pytest

import ferro
import ferro._sql_log
from ferro import Model
from ferro.base import FerroField
from ferro.migrations.testing import harness
from tests.test_migrate_new import (  # noqa: F401 - fixtures
    pkg,
    project,
    write_models,
)
from tests.test_migrate_up import (  # noqa: F401 - fixtures
    configure,
    db,
    migrations,
    new,
    sql_step,
)

pytestmark = pytest.mark.backend_matrix

RECORD_ATTRIBUTES = (
    "sql",
    "params",
    "dialect",
    "duration_ms",
    "operation",
    "subject",
    "connection",
    "rows",
    "error",
)


# -- the reconciliation pass ----------------------------------------------------------


@pytest.fixture
def _fresh(clean_registry):
    yield
    ferro.reset_engine()


def _declare_author(*, bio: bool) -> None:
    if bio:

        class Author(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            name: str
            bio: str | None = None

    else:

        class Author(Model):
            id: Annotated[int | None, FerroField(primary_key=True)] = None
            name: str


def _rewind() -> None:
    from ferro.registry import REGISTRY

    ferro.reset_engine()
    ferro.clear_registry()
    REGISTRY.reset_for_test()


def _pass_records(records: list[logging.LogRecord]) -> list[logging.LogRecord]:
    """The records of statements the pass reports: those with a subject."""
    return [record for record in records if record.subject is not None]


def _assert_matches_report(records, report, backend: str) -> None:
    logged = _pass_records(records)
    assert [(r.subject, r.sql) for r in logged] == [
        (s.subject, s.sql) for s in report.statements
    ]
    for record in logged:
        assert record.name == "ferro.sql"
        assert record.levelno == logging.DEBUG
        assert record.operation == "migrate"
        assert record.params == ()
        assert record.dialect == backend
        assert record.connection == "default"
        assert isinstance(record.duration_ms, float) and record.duration_ms >= 0
        assert record.error is None
        assert record.getMessage() == (
            f"{record.sql} params=() {record.duration_ms:.1f}ms"
        )


@pytest.mark.usefixtures("_fresh")
@pytest.mark.asyncio
async def test_create_tables_logs_one_record_per_reported_statement(
    db_url, db_backend, statement_records
):
    _declare_author(bio=False)
    await ferro.connect(db_url)

    report = await ferro.create_tables()

    assert [s.subject for s in report.statements if s.role == "schema"] == ["author"]
    _assert_matches_report(statement_records, report, db_backend)
    for record in statement_records:
        assert {a: getattr(record, a) for a in RECORD_ATTRIBUTES}.keys() == set(
            RECORD_ATTRIBUTES
        )


@pytest.mark.usefixtures("_fresh")
@pytest.mark.asyncio
async def test_migrate_logs_the_alter_it_ran(db_url, db_backend, statement_records):
    _declare_author(bio=False)
    await ferro.connect(db_url)
    await ferro.create_tables()
    _rewind()
    _declare_author(bio=True)
    await ferro.connect(db_url)
    statement_records.clear()

    report = await ferro.migrate()

    _assert_matches_report(statement_records, report, db_backend)
    assert ("author", 'ALTER TABLE "author" ADD COLUMN "bio" varchar') in [
        (r.subject, r.sql) for r in statement_records
    ]


@pytest.mark.usefixtures("_fresh")
@pytest.mark.asyncio
async def test_with_the_log_off_the_emitter_is_never_called(db_url, monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(ferro._sql_log, "emit", calls.append)
    logger = logging.getLogger("ferro.sql")
    level = logger.level
    logger.setLevel(logging.WARNING)
    try:
        _declare_author(bio=False)
        await ferro.connect(db_url)
        await ferro.create_tables()
        await ferro.migrate()
    finally:
        logger.setLevel(level)

    assert calls == []


@pytest.mark.usefixtures("_fresh")
@pytest.mark.asyncio
async def test_with_the_log_on_the_emitter_is_the_one_ferro_calls(
    db_url, monkeypatch, statement_records
):
    calls: list[dict] = []
    emit = ferro._sql_log.emit

    def spy(record: dict) -> None:
        calls.append(record)
        emit(record)

    monkeypatch.setattr(ferro._sql_log, "emit", spy)
    _declare_author(bio=False)
    await ferro.connect(db_url)
    await ferro.create_tables()

    assert calls and len(calls) == len(statement_records)


@pytest.mark.usefixtures("_fresh")
@pytest.mark.asyncio
async def test_caplog_captures_statement_records_without_setup(db_url, caplog):
    caplog.set_level(logging.DEBUG)
    _declare_author(bio=False)
    await ferro.connect(db_url)

    report = await ferro.create_tables()

    sql = [r for r in caplog.records if r.name == "ferro.sql"]
    assert [r.sql for r in sql if r.subject is not None] == [
        s.sql for s in report.statements
    ]
    assert any(r.name == "ferro" for r in caplog.records)


def test_the_ferro_logger_has_only_a_null_handler_and_propagates():
    logger = logging.getLogger("ferro")
    assert [type(h) for h in logger.handlers] == [logging.NullHandler]
    assert logger.propagate is True


def test_redacted_reads_as_three_stars():
    assert repr(ferro._sql_log.REDACTED) == "***"
    assert str(ferro._sql_log.REDACTED) == "***"


def test_emit_substitutes_redacted_for_masked_parameters(statement_records):
    ferro._sql_log.emit(
        {
            "sql": 'SELECT 1 WHERE "token" = ?',
            "params": (None, 7),
            "mask": (True, False),
            "dialect": "sqlite",
            "duration_ms": 0.25,
            "operation": "query",
            "subject": None,
            "connection": "default",
            "rows": None,
            "error": None,
        }
    )

    (record,) = statement_records
    assert record.params == (ferro._sql_log.REDACTED, 7)
    assert record.getMessage() == 'SELECT 1 WHERE "token" = ? params=(***, 7) 0.2ms'


# -- a run ----------------------------------------------------------------------------

AUTHOR = """
class Author(Model):
    __ferro_table__ = "slog_author"
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    name: str
"""

LOGS_A_LINE = """\
from ferro.migrations import atomic, nothing_to_reverse


@atomic
async def up(ctx):
    ctx.log.info("backfilled 3 rows")


@nothing_to_reverse("nothing was written")
def down(ctx): ...
"""


@pytest.fixture
def chain(project, pkg, db, clean_registry):
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    return project


async def _connected(db) -> None:
    await ferro.connect(db.url)


@pytest.mark.usefixtures("isolated_imports")
@pytest.mark.asyncio
async def test_a_run_logs_each_step_statement_under_its_step_file(
    chain, db, statement_records
):
    sql_step(chain, "seed", "INSERT INTO \"slog_author\" (name) VALUES ('Ada');\n")
    await _connected(db)
    try:
        h = harness()
        await h.apply("0001")
        await h.apply("0002")
    finally:
        ferro.reset_engine()

    (ddl_file,) = (migrations(chain) / "0001_create_author").glob(
        f"*.up.{db.backend}.sql"
    )
    ddl = [
        r
        for r in statement_records
        if r.subject == f"0001_create_author/{ddl_file.name}"
        and "lock_timeout" not in r.sql
    ]
    assert ddl and all(r.sql in ddl_file.read_text() for r in ddl)
    seed = [
        r.sql
        for r in statement_records
        if r.subject == "0002_seed/01_seed.up.sql" and "lock_timeout" not in r.sql
    ]
    assert seed == ["INSERT INTO \"slog_author\" (name) VALUES ('Ada')"]
    for record in ddl:
        assert record.operation == "migrate"
        assert record.connection == "default"
        assert record.dialect == db.backend
        assert record.params == ()
        assert record.error is None


@pytest.mark.usefixtures("isolated_imports")
@pytest.mark.asyncio
async def test_a_statement_the_database_rejects_is_logged_with_its_error(
    chain, db, statement_records
):
    sql_step(chain, "broken", 'INSERT INTO "nowhere" VALUES (1);\n')
    await _connected(db)
    try:
        h = harness()
        await h.apply("0001")
        with pytest.raises(Exception) as raised:
            await h.apply("0002")
    finally:
        ferro.reset_engine()

    (failed,) = [
        r
        for r in statement_records
        if r.subject == "0002_broken/01_broken.up.sql" and r.error is not None
    ]
    assert failed.sql == 'INSERT INTO "nowhere" VALUES (1)'
    assert "nowhere" in failed.error
    assert failed.getMessage().endswith(f" failed: {failed.error}")
    assert "nowhere" in str(raised.value)


@pytest.mark.usefixtures("isolated_imports")
def test_ferro_migrate_up_prints_a_data_steps_log_line(chain, pkg, db, capsys):
    write_models(chain, pkg, AUTHOR + "    bio: str | None = None\n")
    new("add_bio", "--data-step", "Author")
    (step,) = (migrations(chain) / "0002_add_bio").glob("*.py")
    step.write_text(LOGS_A_LINE)
    capsys.readouterr()

    from tests.test_migrate_new import run

    assert run("migrate", "up", "--url", db.url) == 0

    assert "backfilled 3 rows" in capsys.readouterr().err
    logger = logging.getLogger("ferro")
    assert [type(h) for h in logger.handlers] == [logging.NullHandler]
