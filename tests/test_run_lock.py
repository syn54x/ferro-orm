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
from ferro.migrations.report import RunRefused
from tests.test_migrate_new import (  # noqa: F401 - fixtures
    AUTHOR,
    isolated_imports,
    pkg,
    project,
    run,
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


async def test_lock_timeout_zero_from_the_cli_refuses_at_once_without_waiting(
    project, pkg, db, capsys
):
    _project(project, pkg, db)
    handle = await _hold(db)
    capsys.readouterr()
    try:
        code = await asyncio.to_thread(
            run, "migrate", "up", "--url", db.url, "--lock-timeout", "0"
        )
    finally:
        await _core._release_run_lock(handle)

    captured = capsys.readouterr()
    assert code == 1
    assert "longer than the lock timeout (0s)" in captured.err
    assert WAITING not in captured.err
    assert captured.out == ""
    assert "author" not in db.tables()


async def test_a_session_that_never_took_the_lock_fails_the_first_check_as_a_pooler(
    project, pkg, db
):
    if db.backend != "postgres":
        pytest.skip("only a Postgres lock is checked on its session")
    _project(project, pkg, db)
    await ferro.connect(db.url, name="pooled")
    handle = await _core._unacquired_run_lock_for_test("pooled")
    try:
        with pytest.raises(RunRefused) as refused:
            await _core._verify_run_lock(handle)
    finally:
        await _core._release_run_lock(handle)

    assert "migrations need a direct or session-mode connection" in str(refused.value)
    assert "Nothing was applied." in str(refused.value)


async def test_a_lock_timeout_beyond_the_bound_is_refused_from_the_cli(
    project, pkg, db, capsys
):
    _project(project, pkg, db)
    capsys.readouterr()

    code = await asyncio.to_thread(
        run, "migrate", "up", "--url", db.url, "--lock-timeout", "100000000000000000000"
    )

    captured = capsys.readouterr()
    assert code == 1
    assert "100000000000000000000" in captured.err
    assert "at most" in captured.err
    assert "Traceback" not in captured.err
    assert "author" not in db.tables()


def test_parse_lock_timeout_accepts_the_bound_and_refuses_beyond_it():
    assert runner.parse_lock_timeout(runner.MAX_LOCK_TIMEOUT_S) == (
        runner.MAX_LOCK_TIMEOUT_S
    )
    with pytest.raises(runner.SettingsError, match="at most"):
        runner.parse_lock_timeout(runner.MAX_LOCK_TIMEOUT_S + 1)


async def test_the_ffi_refuses_an_unrepresentable_timeout_without_panicking():
    with pytest.raises(ValueError, match="1e+20"):
        await _core._acquire_run_lock(None, None, 1e20)
