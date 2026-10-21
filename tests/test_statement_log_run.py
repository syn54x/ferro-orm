# ruff: noqa: F811 - pytest fixtures imported from a sibling module are redefined as arguments
"""The run lock's statements are in the statement log (#764, ADR-0057).

On Postgres a run holds its lock as a session advisory lock on a connection
of its own. Taking it, checking it and letting it go are statements like any
other, so a run's log shows them among its DDL:

```text
ferro.sql DEBUG SELECT pg_try_advisory_lock($1) params=(4242...,) 0.4ms
ferro.sql DEBUG SELECT EXISTS (SELECT 1 FROM pg_locks ...) params=(...) 0.6ms
ferro.sql DEBUG CREATE TABLE "slog_author" (...) params=() 1.2ms
ferro.sql DEBUG SELECT pg_advisory_unlock($1) params=(4242...,) 0.3ms
```

SQLite's run lock is a file lock and sends no statement.
"""

import logging
from typing import Annotated

import pytest

import ferro
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
    new,
)

pytestmark = [pytest.mark.postgres_only, pytest.mark.asyncio]

TRY_LOCK = "SELECT pg_try_advisory_lock($1)"
UNLOCK = "SELECT pg_advisory_unlock($1)"
PROBE_PREFIX = "SELECT EXISTS (SELECT 1 FROM pg_locks"


def _is_probe(sql: str) -> bool:
    """The check that this run's own session holds the lock (``status``'s
    probe of any session's lock reads the same view without the pid)."""
    return sql.startswith(PROBE_PREFIX) and "pg_backend_pid()" in sql


AUTHOR = """
class Author(Model):
    __ferro_table__ = "slog_author"
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    name: str
"""


def _lock_records(records: list[logging.LogRecord]) -> list[logging.LogRecord]:
    return [r for r in records if r.sql in (TRY_LOCK, UNLOCK) or _is_probe(r.sql)]


def _assert_lock_lifecycle(records: list[logging.LogRecord]) -> None:
    """The lock is taken, checked on its own session, then released, each one
    record under ``operation="migrate"``, around every other statement of the
    run."""
    lock = _lock_records(records)
    assert [r.sql for r in lock][:1] == [TRY_LOCK]
    assert _is_probe(lock[1].sql)
    assert lock[-1].sql == UNLOCK
    key = lock[0].params
    assert len(key) == 1 and isinstance(key[0], int)
    assert lock[-1].params == key
    assert len(lock[1].params) == 2 and all(isinstance(p, int) for p in lock[1].params)
    for record in lock:
        assert record.operation == "migrate"
        assert record.subject is None
        assert record.connection == "default"
        assert record.dialect == "postgres"
        assert record.error is None
        assert record.duration_ms >= 0
    first, last = records.index(lock[0]), records.index(lock[-1])
    ddl = [r for r in records if r.sql.startswith("CREATE TABLE")]
    assert ddl and all(first < records.index(r) < last for r in ddl)


@pytest.mark.usefixtures("isolated_imports", "clean_registry")
async def test_a_run_logs_taking_checking_and_releasing_its_lock(
    project, pkg, db, statement_records
):
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    await ferro.connect(db.url)
    try:
        await harness().apply("0001")
    finally:
        ferro.reset_engine()

    _assert_lock_lifecycle(statement_records)


@pytest.fixture
def _fresh(clean_registry):
    yield
    ferro.reset_engine()


@pytest.mark.usefixtures("_fresh")
async def test_the_reconciliation_pass_logs_its_lock(db_url, statement_records):
    class Author(Model):
        __ferro_table__ = "slog_pass_author"
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        name: str

    await ferro.connect(db_url)

    await ferro.migrate()

    _assert_lock_lifecycle(statement_records)
