# ruff: noqa: F811 - pytest fixtures imported from a sibling module are redefined as arguments
"""The run lock (#519, ADR-0029): one run at a time, and it dies with the process.

On Postgres it is a session advisory lock on a dedicated connection, verified
after acquiring and before every step; on SQLite an OS file lock on
``<database>.ferro-migrate.lock``. A second run says at once that it is
waiting, waits up to ``lock_timeout``, then re-reads the records.
"""

from __future__ import annotations

import asyncio

import pytest

import ferro
from ferro import _core
from ferro.migrations import runner
from tests.test_migrate_new import (  # noqa: F401 - fixtures
    AUTHOR,
    isolated_imports,
    pkg,
    project,
    write_models,
)
from tests.test_migrate_up import (  # noqa: F401 - fixtures
    configure,
    db,
    new,
    settings_and_database,
    sql_step,
)

pytestmark = [
    pytest.mark.usefixtures("isolated_imports", "clean_registry"),
    pytest.mark.backend_matrix,
    pytest.mark.asyncio,
]

WAITING = (
    "Another ferro migration run holds the lock on this database; waiting "
    "(--lock-timeout to bound it)."
)


def _project(project, pkg, db, *steps: str):
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    for name in steps:
        sql_step(project, name, f'CREATE TABLE "{name}" ("id" integer);\n')
    return settings_and_database()


async def _hold(db) -> int:
    """Take the run lock from the test, as another run would."""
    await ferro.connect(db.url, name="holder")
    return await _core._acquire_run_lock("holder", None, 5.0)


async def test_a_second_run_waits_says_so_and_applies_nothing_after_the_first(
    project, pkg, db, capsys, monkeypatch
):
    settings, database = _project(project, pkg, db)
    real = runner._execute_sql_step

    async def slow(*args, **kwargs):
        await asyncio.sleep(1.5)
        return await real(*args, **kwargs)

    monkeypatch.setattr(runner, "_execute_sql_step", slow)
    first = asyncio.create_task(runner.up(settings, database, url=db.url))
    await asyncio.sleep(0.3)
    second = asyncio.create_task(runner.up(settings, database, url=db.url))
    await asyncio.sleep(0.7)

    assert WAITING in capsys.readouterr().err
    assert not second.done()
    done_first, done_second = await asyncio.gather(first, second)

    assert done_first.refusal is None and len(done_first.applied) == 1
    assert done_second.refusal is None and done_second.applied == []


async def test_a_lock_timeout_gives_up_naming_it_and_leaves_the_holder_alone(
    project, pkg, db, capsys
):
    settings, database = _project(project, pkg, db)
    handle = await _hold(db)
    try:
        report = await runner.up(settings, database, url=db.url, lock_timeout="1s")

        assert report.refusal is not None
        assert "lock timeout (1s)" in report.refusal
        assert report.applied == []
        assert WAITING in capsys.readouterr().err
        assert await _core._run_lock_is_held("holder", None)
    finally:
        await _core._release_run_lock(handle)
    assert not await _core._run_lock_is_held("holder", None)


async def test_status_shows_running_while_a_run_holds_the_lock(project, pkg, db):
    settings, database = _project(project, pkg, db, "second")
    handle = await _hold(db)
    try:
        report = await runner.status(settings, database, url=db.url)
    finally:
        await _core._release_run_lock(handle)

    assert [m.state for m in report.migrations] == ["running", "pending"]
    assert "  01_schema.up." in report.render(steps=False)
    assert report.exit_code == 3


async def test_a_lock_connection_lost_between_steps_stops_the_run(
    project, pkg, db, monkeypatch
):
    if db.backend != "postgres":
        pytest.skip("only a Postgres lock lives on a connection that can drop")
    settings, database = _project(project, pkg, db, "second")
    handles: list[int] = []
    acquire = runner._acquire_run_lock
    execute = runner._execute_sql_step

    async def capture(*args, **kwargs):
        handle = await acquire(*args, **kwargs)
        handles.append(handle)
        return handle

    async def after_first(*args, **kwargs):
        outcome = await execute(*args, **kwargs)
        await _core._close_run_lock_connection_for_test(handles[0])
        return outcome

    monkeypatch.setattr(runner, "_acquire_run_lock", capture)
    monkeypatch.setattr(runner, "_execute_sql_step", after_first)

    report = await runner.up(settings, database, url=db.url)

    assert [a.migration for a in report.applied] == ["0001_create_author"]
    assert report.refusal is not None
    assert "the run lock was lost" in report.refusal
    assert "second" not in db.tables()
