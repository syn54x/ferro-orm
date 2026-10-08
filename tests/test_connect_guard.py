# ruff: noqa: F811 - pytest fixtures imported from a sibling module are redefined as arguments
"""The two doors are exclusive per database (#521, ADR-0038).

A database that ``ferro migrate up`` manages refuses every auto-migrate
flag before any DDL::

    await ferro.connect(url, auto_migrate=True)
    # MigrationRefused: connect(auto_migrate=…) is refused: public is governed
    # by ferro migrations (public._ferro_migrations). Use ferro migrate up, or
    # drop the tracking tables to leave migrations.

The guard reads the project's configured ``tracking_schema``s and the
catalog's format tables; a plain ``connect(url)`` reads neither.
"""

from __future__ import annotations

import asyncio
import warnings
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

import pytest

import ferro
from ferro import Model, _core
from ferro.base import FerroField
from ferro.migrations import MigrationRefused
from tests._pass_harness import auto_migrate, schema_steps
from tests.test_migrate_new import (  # noqa: F401 - fixtures
    AUTHOR,
    pkg,
    project,
    write_models,
)
from tests.test_migrate_up import configure, db, new  # noqa: F401

pytestmark = [
    pytest.mark.usefixtures("isolated_imports", "clean_registry"),
    pytest.mark.backend_matrix,
    pytest.mark.asyncio,
]

FLAGS = [
    {"auto_migrate": True},
    {"migrate_updates": True},
    {"migrate_destructive": True},
]


def guard_text(governed: str, home: str) -> str:
    return (
        f"connect(auto_migrate=…) is refused: {governed} is governed by ferro "
        f"migrations ({home}._ferro_migrations). Use ferro migrate up, or drop the "
        f"tracking tables to leave migrations."
    )


@asynccontextmanager
async def holding(db) -> AsyncIterator[None]:
    """The run lock, held by a run of the test's own on a connection of its
    own, as another process's run would hold it."""
    await ferro.connect(db.url, name="holder")
    tracked = await _core._open_tracked("holder", None, "migrations")
    async with tracked.locked(0):
        yield


async def lock_held(using: str | None = None) -> bool:
    """Whether a run holds the run lock, asked without taking it."""
    return await (await _core._open_tracked(using, None, "migrations")).lock_held()


def governed(db) -> str:
    return "main" if db.backend == "sqlite" else db.schema


async def track(project, pkg, db, extra: str = "") -> None:
    """Adopt migrations on ``db``: one migration applied through the API."""
    configure(project, pkg, db.backend, extra)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    await ferro.connect(db.url)
    await ferro.migrations.up()
    ferro.reset_engine()


def declare_fresh_model() -> None:
    class GuardFresh(Model):
        id: Annotated[int | None, FerroField(primary_key=True)] = None
        label: str


@pytest.mark.parametrize("flags", FLAGS, ids=lambda f: next(iter(f)))
async def test_a_tracked_database_refuses_auto_migrate_through_the_config(
    project, pkg, db, flags
):
    await track(project, pkg, db)
    declare_fresh_model()

    with pytest.raises(MigrationRefused) as raised:
        await ferro.connect(db.url, **flags)

    assert str(raised.value) == guard_text(governed(db), governed(db))
    assert "guardfresh" not in db.tables()
    assert ferro._core.connection_backend() is None


@pytest.mark.parametrize("door", ["create_tables", "migrate"])
async def test_the_manual_passes_refuse_a_tracked_database_too(
    project, pkg, db, door
):
    """``ferro.create_tables()`` and ``ferro.migrate()`` are the passes'
    public doors: they share the lock and the guard."""
    await track(project, pkg, db)
    await ferro.connect(db.url)
    declare_fresh_model()

    with pytest.raises(MigrationRefused) as raised:
        await getattr(ferro, door)()

    assert str(raised.value) == guard_text(governed(db), governed(db))
    # Refused before any DDL: the error's report lists nothing.
    assert raised.value.report == ferro.PassReport()
    assert "guardfresh" not in db.tables()


async def test_create_tables_still_creates_on_an_untracked_database(project, pkg, db):
    configure(project, pkg, db.backend)
    await ferro.connect(db.url)
    declare_fresh_model()

    report = await ferro.create_tables()

    assert "guardfresh" in db.tables()
    steps = schema_steps(report)
    assert len(steps) == 1 and steps[0][0] == "guardfresh", steps
    assert steps[0][1].startswith('CREATE TABLE IF NOT EXISTS "guardfresh"')
    assert await lock_held() is False


async def test_without_a_config_file_the_catalog_still_refuses(
    project, pkg, db, tmp_path, monkeypatch
):
    await track(project, pkg, db)
    (project / "ferro.toml").unlink()
    elsewhere = tmp_path / "image"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert ferro.FerroSettings().databases == {}
    declare_fresh_model()

    with pytest.raises(MigrationRefused) as raised:
        await ferro.connect(db.url, migrate_updates=True)

    assert str(raised.value) == guard_text(governed(db), governed(db))
    assert "guardfresh" not in db.tables()


async def test_a_moved_tracking_schema_is_found_and_named(project, pkg, db):
    if db.backend != "postgres":
        pytest.skip("tracking_schema is Postgres-only")
    tracking = f"{db.schema}_track"
    db.execute(f'CREATE SCHEMA "{tracking}"')
    try:
        await track(project, pkg, db, f'tracking_schema = "{tracking}"\n')
        declare_fresh_model()

        with pytest.raises(MigrationRefused) as raised:
            await ferro.connect(db.url, auto_migrate=True)

        assert str(raised.value) == guard_text(db.schema, tracking)
        assert "guardfresh" not in db.tables()
    finally:
        db.execute(f'DROP SCHEMA "{tracking}" CASCADE')


async def test_a_configured_tracking_schema_without_its_format_table_refuses(
    project, pkg, db
):
    """The config source: a tracking table where the project says it keeps
    one refuses, even with no format table the catalog could read."""
    if db.backend != "postgres":
        pytest.skip("tracking_schema is Postgres-only")
    tracking = f"{db.schema}_track"
    db.execute(f'CREATE SCHEMA "{tracking}"')
    try:
        await track(project, pkg, db, f'tracking_schema = "{tracking}"\n')
        db.execute(f'DROP TABLE "{tracking}"._ferro_migrations_format')
        declare_fresh_model()

        with pytest.raises(MigrationRefused) as raised:
            await ferro.connect(db.url, auto_migrate=True)

        assert str(raised.value) == guard_text(db.schema, tracking)
    finally:
        db.execute(f'DROP SCHEMA "{tracking}" CASCADE')


async def test_a_neighbour_schema_under_migrations_does_not_refuse(project, pkg, db):
    """Another schema of the same Postgres database, tracked by its own
    migrations, leaves auto-migrate in this one alone."""
    if db.backend != "postgres":
        pytest.skip("schemas are Postgres-only")
    neighbour = f"{db.schema}_nb"
    db.execute(f'CREATE SCHEMA "{neighbour}"')
    try:
        db.execute(
            f'CREATE TABLE "{neighbour}"._ferro_migrations_format '
            "(format INTEGER NOT NULL, governed_schema TEXT NOT NULL)"
        )
        db.execute(
            f"INSERT INTO \"{neighbour}\"._ferro_migrations_format VALUES (1, '{neighbour}')"
        )
        declare_fresh_model()

        await ferro.connect(db.url, auto_migrate=True)

        assert "guardfresh" in db.tables()
    finally:
        db.execute(f'DROP SCHEMA "{neighbour}" CASCADE')


async def test_a_plain_connect_reads_no_config_and_runs_no_guard(
    project, pkg, db, monkeypatch
):
    """A plain ``connect(url)`` on a tracked database reads no config and
    never enters the guard path. That path takes the run lock first, so with
    the lock held elsewhere a plain connect that ran it would wait; it
    returns at once instead."""
    await track(project, pkg, db)

    def no_config(*_args, **_kwargs):
        raise AssertionError("a plain connect() must not read the config")

    async with holding(db):
        monkeypatch.setattr(ferro.settings.FerroSettings, "__init__", no_config)
        await asyncio.wait_for(ferro.connect(db.url), 5)

    assert _core._catalog_query_count_for_test() == 0
    assert _core.connection_backend() == db.backend


async def test_an_untracked_database_still_auto_migrates_beside_a_config(
    project, pkg, db
):
    configure(project, pkg, db.backend)
    declare_fresh_model()

    await ferro.connect(db.url, migrate_updates=True)

    assert "guardfresh" in db.tables()


# -- the run lock ------------------------------------------------------------------


async def test_two_concurrent_auto_migrates_serialize_on_the_run_lock(project, db):
    """Two processes booting with ``migrate_updates=True``: the second's
    pass waits, then sees the first's DDL and executes nothing."""
    if db.backend != "postgres":
        pytest.skip("two boots against one server is the Postgres shape")
    declare_fresh_model()

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        reports = await asyncio.gather(
            auto_migrate(db.url, name="boot_a", updates=True),
            auto_migrate(db.url, name="boot_b", updates=True),
        )

    # Between them the two passes created the table once.
    assert [step for report in reports for step in schema_steps(report)] == [
        (
            "guardfresh",
            'CREATE TABLE IF NOT EXISTS "guardfresh" '
            '( "id" serial PRIMARY KEY NOT NULL, "label" varchar NOT NULL )',
        )
    ]
    assert "guardfresh" in db.tables()


async def test_auto_migrate_waits_for_a_held_run_lock_then_runs(project, db):
    declare_fresh_model()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        async with holding(db):
            boot = asyncio.create_task(ferro.connect(db.url, migrate_updates=True))
            await asyncio.sleep(0.5)
            assert not boot.done()
            assert "guardfresh" not in db.tables()
        await asyncio.wait_for(boot, 10)

    assert "guardfresh" in db.tables()
    assert [str(w.message) for w in caught if "is waiting" in str(w.message)] == [
        "ferro auto-migrate: connect(auto_migrate=…) is waiting: another ferro "
        "migration run or auto-migrate pass holds the run lock on this database. "
        "It goes on once that one finishes."
    ]


async def test_the_waiting_warning_names_the_call_that_waits(project, db):
    declare_fresh_model()
    await ferro.connect(db.url)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        async with holding(db):
            pending = asyncio.create_task(ferro.create_tables())
            await asyncio.sleep(0.5)
            assert not pending.done()
        report = await asyncio.wait_for(pending, 10)

    assert "guardfresh" in db.tables()
    # The wait is in the report too, typed.
    assert [(w.kind, w.subject.scope) for w in report.warnings] == [
        ("RunLockWait", "modelset")
    ]
    assert [str(w.message) for w in caught if "is waiting" in str(w.message)] == [
        "ferro auto-migrate: create_tables() is waiting: another ferro migration "
        "run or auto-migrate pass holds the run lock on this database. It goes on "
        "once that one finishes."
    ]


def lock_wait_refusal(call: str, schema: str, timeout: str) -> str:
    return (
        f"{call} gave up waiting for the run lock on {schema}: another ferro "
        f"migration run or auto-migrate pass held it longer than ddl_lock_timeout "
        f"({timeout}). Nothing was applied. Wait for that run to finish and try "
        f"again, or raise ddl_lock_timeout; `ferro migrate status` shows a "
        f"migration run while it holds the lock."
    )


async def test_a_tracked_database_refuses_at_once_while_a_run_holds_the_lock(
    project, pkg, db
):
    """The guard reads the catalog only, so it runs before the pass waits
    for the lock: a database ferro migrations govern is refused at once, not
    after the run holding the lock (a ``ferro migrate up`` mid-deploy)
    finishes."""
    await track(project, pkg, db)
    declare_fresh_model()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        async with holding(db):
            with pytest.raises(MigrationRefused) as raised:
                await asyncio.wait_for(ferro.connect(db.url, migrate_updates=True), 3)

    assert str(raised.value) == guard_text(governed(db), governed(db))
    assert [str(w.message) for w in caught if "is waiting" in str(w.message)] == []
    assert "guardfresh" not in db.tables()


@pytest.mark.parametrize("door", ["connect", "create_tables"])
async def test_the_pass_waits_for_the_run_lock_up_to_ddl_lock_timeout(
    project, pkg, db, door
):
    """The pass's wait for the run lock is bounded by the configured
    ``ddl_lock_timeout`` (ADR-0038 as amended), and its refusal names the
    call, the schema, the setting and where to see the run."""
    configure(project, pkg, db.backend, 'ddl_lock_timeout = "1s"\n')
    declare_fresh_model()
    if door == "create_tables":
        await ferro.connect(db.url)
    call = {"connect": "connect(auto_migrate=…)", "create_tables": "create_tables()"}

    loop = asyncio.get_running_loop()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        async with holding(db):
            began = loop.time()
            with pytest.raises(MigrationRefused) as raised:
                pending = (
                    ferro.connect(db.url, migrate_updates=True)
                    if door == "connect"
                    else ferro.create_tables()
                )
                await asyncio.wait_for(pending, 10)
            waited = loop.time() - began

    assert str(raised.value) == lock_wait_refusal(call[door], governed(db), "1s")
    assert 1.0 <= waited < 5.0
    # It said at once that it waits, and the refusal's report carries that.
    assert len([w for w in caught if "is waiting" in str(w.message)]) == 1
    assert [w.kind for w in raised.value.report.warnings] == ["RunLockWait"]
    assert "guardfresh" not in db.tables()
