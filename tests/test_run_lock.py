# ruff: noqa: F811 - pytest fixtures imported from a sibling module are redefined as arguments
"""The run lock (#519, ADR-0029): one run at a time, and it dies with the process.

On Postgres it is a session advisory lock on a dedicated connection, verified
after acquiring and by every write of the locked run (ADR-0048); on SQLite an
OS file lock on ``<database>.ferro-migrate.lock``. A second run says at once
that it is waiting, waits up to ``lock_timeout``, then re-reads the records.
A holder here is the run object itself: ``tracked.locked(...)``.
"""

from __future__ import annotations

import asyncio

import pytest

import ferro
from ferro.migrations import runner
from ferro.migrations.report import RunRefused
from tests.test_migrate_new import (  # noqa: F401 - fixtures
    AUTHOR,
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


async def _tracked(db, database, name: str = "holder"):
    """``database`` opened on a connection of the test's own, as another
    run would open it."""
    await ferro.connect(db.url, name=name)
    return await runner.open_tracked(name, database)


async def test_a_second_run_waits_says_so_and_applies_nothing_after_the_first(
    project, pkg, db, capsys
):
    settings, database = _project(project, pkg, db)
    tracked = await _tracked(db, database)
    async with tracked.locked(5.0) as first:
        second = asyncio.create_task(runner.up(settings, database, url=db.url))
        await asyncio.sleep(0.7)
        assert WAITING in capsys.readouterr().err
        assert not second.done()
        plan = await first.plan({"direction": "up"})
        outcomes = [await first.execute(step) for step in plan.steps]
    done_second = await second

    assert [outcome["ok"] for outcome in outcomes] == [True]
    assert done_second.refusal is None and done_second.applied == []


async def test_a_lock_timeout_gives_up_naming_it_and_leaves_the_holder_alone(
    project, pkg, db, capsys
):
    settings, database = _project(project, pkg, db)
    tracked = await _tracked(db, database)
    async with tracked.locked(5.0):
        report = await runner.up(settings, database, url=db.url, lock_timeout="1s")

        assert report.refusal is not None
        assert "lock timeout (1s)" in report.refusal
        assert report.applied == []
        assert WAITING in capsys.readouterr().err
        assert await tracked.lock_held()
    assert not await tracked.lock_held()


async def test_status_shows_running_while_a_run_holds_the_lock(project, pkg, db):
    settings, database = _project(project, pkg, db, "second")
    tracked = await _tracked(db, database)
    async with tracked.locked(5.0):
        report = await runner.status(settings, database, url=db.url)

    assert [m.state for m in report.migrations] == ["running", "pending"]
    assert "  01_schema.up." in report.render(steps=False)
    assert report.exit_code == 3


async def test_a_lock_connection_lost_between_steps_stops_the_run(project, pkg, db):
    if db.backend != "postgres":
        pytest.skip("only a Postgres lock lives on a connection that can drop")
    _, database = _project(project, pkg, db, "second")
    tracked = await _tracked(db, database)
    async with tracked.locked(5.0) as run:
        first, second = (await run.plan({"direction": "up"})).steps
        assert (await run.execute(first))["ok"]
        await run._close_lock_connection_for_test()
        with pytest.raises(RunRefused, match="the run lock was lost"):
            await run.execute(second)

    assert "author" in db.tables()
    assert "second" not in db.tables()
    assert [(r[0], r[1]) for r in db.rows(RECORDS)] == [(1, 1)]


RECORDS = "SELECT migration, step FROM _ferro_migrations ORDER BY migration, step"


async def test_a_lock_lost_while_a_transactional_step_runs_commits_nothing(
    project, pkg, db
):
    """The finished record is written inside the step's transaction once the
    lock is verified there (ADR-0029 as amended): a lock lost while the
    step's statements run rolls the step back, and the run refuses as a
    lost lock."""
    if db.backend != "postgres":
        pytest.skip("only a Postgres lock lives on a connection that can drop")
    _, database = _project(project, pkg, db)
    sql_step(
        project, "slow", 'SELECT pg_sleep(1);\nCREATE TABLE "slow" ("id" integer);\n'
    )
    tracked = await _tracked(db, database)
    async with tracked.locked(5.0) as run:
        first, slow = (await run.plan({"direction": "up"})).steps
        assert (await run.execute(first))["ok"]
        running = asyncio.ensure_future(run.execute(slow))
        await asyncio.sleep(0.4)
        await run._close_lock_connection_for_test()
        with pytest.raises(RunRefused, match="the run lock was lost"):
            await running

    assert "slow" not in db.tables()
    assert [(r[0], r[1]) for r in db.rows(RECORDS)] == [(1, 1), (2, 1)]
    assert db.rows("SELECT finished_at FROM _ferro_migrations WHERE migration = 2") == [
        (None,)
    ]


async def test_a_released_run_writes_nothing(project, pkg, db):
    _, database = _project(project, pkg, db)
    tracked = await _tracked(db, database)
    async with tracked.locked(5.0) as run:
        (step,) = (await run.plan({"direction": "up"})).steps

    with pytest.raises(RunRefused, match="lock was released"):
        await run.execute(step)
    assert "author" not in db.tables()


async def test_a_preview_plan_cannot_execute(project, pkg, db):
    _, database = _project(project, pkg, db)
    tracked = await _tracked(db, database)
    (preview,) = (await tracked.plan({"direction": "up"})).steps
    async with tracked.locked(5.0) as run:
        with pytest.raises(ValueError, match="preview plan"):
            await run.execute(preview)

    assert "author" not in db.tables()


async def test_lock_timeout_zero_from_the_cli_refuses_at_once_without_waiting(
    project, pkg, db, capsys
):
    _, database = _project(project, pkg, db)
    tracked = await _tracked(db, database)
    capsys.readouterr()
    async with tracked.locked(5.0):
        code = await asyncio.to_thread(
            run, "migrate", "up", "--url", db.url, "--lock-timeout", "0"
        )

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
    _, database = _project(project, pkg, db)
    tracked = await _tracked(db, database, "pooled")
    async with tracked._locked_unacquired_for_test() as run:
        (step,) = (await run.plan({"direction": "up"})).steps
        with pytest.raises(RunRefused) as refused:
            await run.execute(step)

    assert "migrations need a direct or session-mode connection" in str(refused.value)
    assert "Nothing was applied." in str(refused.value)
    assert "author" not in db.tables()


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


async def test_the_ffi_refuses_an_unrepresentable_timeout_without_panicking(
    project, pkg, db
):
    _, database = _project(project, pkg, db)
    tracked = await _tracked(db, database)
    with pytest.raises(ValueError, match="1e+20"):
        tracked.locked(1e20)
