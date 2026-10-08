# ruff: noqa: F811 - pytest fixtures imported from a sibling module are redefined as arguments
"""``ferro.migrations`` in-process (#521, ADR-0038): the two-line adoption.

```python
await ferro.connect(url)
await ferro.migrations.up()                # a desktop app with no deploy step
await ferro.migrations.require_applied()   # a server that migrates in its deploy step
```

Every test builds a real project under ``tmp_path`` (cwd moved there), writes
migrations with ``ferro migrate new`` and drives the public calls against the
parametrized database on the default connection (or ``using=``).
"""

from __future__ import annotations

import asyncio
import shutil
import sys

import pytest

import ferro
from ferro import _core
from ferro.migrations import (
    DatabaseAheadError,
    MigrationRefused,
    PendingMigrationsError,
    runner,
)
from ferro.migrations.report import StatusReport
from ferro.migrations.target import Target
from tests.test_migrate_new import (  # noqa: F401 - fixtures
    AUTHOR,
    LIBRARY,
    pkg,
    project,
    write_models,
)
from tests.test_migrate_up import configure, db, migrations, new  # noqa: F401

pytestmark = [
    pytest.mark.usefixtures("isolated_imports", "clean_registry"),
    pytest.mark.backend_matrix,
    pytest.mark.asyncio,
]


def two_migrations(project, pkg, backend: str) -> None:
    configure(project, pkg, backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    write_models(project, pkg, LIBRARY)
    new("add_teams")


async def connect(url: str, **kwargs) -> None:
    await ferro.connect(url, **kwargs)


# -- the happy path ---------------------------------------------------------------


async def test_up_applies_on_the_default_connection_and_require_applied_passes(
    project, pkg, db
):
    two_migrations(project, pkg, db.backend)
    await connect(db.url)

    with pytest.raises(PendingMigrationsError) as raised:
        await ferro.migrations.require_applied()
    assert raised.value.pending == ["0001_create_author", "0002_add_teams"]
    assert raised.value.refusals == []
    assert "0001_create_author" in str(raised.value)
    assert "0002_add_teams" in str(raised.value)
    assert "author" not in db.tables()

    report = await ferro.migrations.up()

    assert isinstance(report, runner.RunReport)
    assert [(s.migration, s.step) for s in report.applied] == [
        ("0001_create_author", "01_schema"),
        ("0002_add_teams", "01_schema"),
    ]
    assert {"author", "post", "_ferro_migrations"} <= db.tables()
    assert await ferro.migrations.require_applied() is None
    status = await ferro.migrations.status()
    assert isinstance(status, StatusReport)
    assert status.pending is False


async def test_require_applied_lists_only_what_is_pending(project, pkg, db):
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    await connect(db.url)
    await ferro.migrations.up()
    write_models(project, pkg, LIBRARY)
    new("add_teams")
    await connect(db.url)

    with pytest.raises(PendingMigrationsError) as raised:
        await ferro.migrations.require_applied()

    assert raised.value.pending == ["0002_add_teams"]
    assert str(raised.value) == (
        "ferro.migrations: this database is behind its migrations.\n"
        "  pending  0002_add_teams\n"
        "Run `ferro migrate up` (or `await ferro.migrations.up()`) before serving."
    )


async def test_require_applied_carries_the_edited_file_refusal_and_takes_no_lock(
    project, pkg, db, monkeypatch
):
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    await connect(db.url)
    await ferro.migrations.up()
    up_file = migrations(project) / f"0001_create_author/01_schema.up.{db.backend}.sql"
    up_file.write_bytes(up_file.read_bytes() + b"\n")
    with pytest.raises(MigrationRefused) as refused_up:
        await ferro.migrations.up()
    expected = str(refused_up.value)
    assert "was edited after it was applied" in expected

    held_while_reading: list[bool] = []
    open_tracked = _core._open_tracked

    async def probing_open_tracked(*args, **kwargs):
        tracked = await open_tracked(*args, **kwargs)
        held_while_reading.append(await tracked.lock_held())
        return tracked

    monkeypatch.setattr(_core, "_open_tracked", probing_open_tracked)
    with pytest.raises(PendingMigrationsError) as raised:
        await ferro.migrations.require_applied()

    assert held_while_reading == [False]
    assert raised.value.pending == []
    assert raised.value.refusals == [expected]
    assert expected in str(raised.value)


async def test_require_applied_answers_while_a_run_holds_the_lock(project, pkg, db):
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    await connect(db.url)
    await ferro.migrations.up()
    await connect(db.url, name="holder")
    tracked = await _core._open_tracked("holder", None, str(migrations(project)))
    async with tracked.locked(0):
        assert await tracked.lock_held() is True
        assert await asyncio.wait_for(ferro.migrations.require_applied(), 10) is None


# -- ahead --------------------------------------------------------------------------


async def test_a_database_ahead_of_the_checkout_is_refused_unless_allowed(
    project, pkg, db
):
    two_migrations(project, pkg, db.backend)
    await connect(db.url)
    await ferro.migrations.up()
    shutil.rmtree(migrations(project) / "0002_add_teams")
    expected = (
        "ferro migrate: this database has applied 0002_add_teams, which is not in "
        "migrations/.\nThe directory is behind the database: check out the branch "
        "that holds it. Nothing was applied.\n"
        "Pass allow_ahead=True to run beside them (a rolling deploy)."
    )

    with pytest.raises(DatabaseAheadError) as from_require:
        await ferro.migrations.require_applied()
    with pytest.raises(DatabaseAheadError) as from_up:
        await ferro.migrations.up()

    for raised in (from_require, from_up):
        assert raised.value.ahead == ["0002_add_teams"]
        assert str(raised.value) == expected
    assert await ferro.migrations.require_applied(allow_ahead=True) is None
    report = await ferro.migrations.up(allow_ahead=True)
    assert report.applied == [] and report.ahead == ["0002_add_teams"]


async def test_up_raises_a_run_refusal_rather_than_returning_it(project, pkg, db):
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    await connect(db.url)
    await ferro.migrations.up()
    up_file = migrations(project) / f"0001_create_author/01_schema.up.{db.backend}.sql"
    up_file.write_bytes(up_file.read_bytes() + b"\n")

    with pytest.raises(MigrationRefused, match="was edited after it was applied"):
        await ferro.migrations.up()


# -- check --------------------------------------------------------------------------


async def test_check_returns_the_offline_report_and_raises_on_request(project, pkg, db):
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")

    report = await ferro.migrations.check()
    assert report.ok and report.problems == []
    report.raise_for_problems()

    write_models(project, pkg, LIBRARY)
    report = await ferro.migrations.check()
    assert not report.ok
    with pytest.raises(MigrationRefused) as raised:
        report.raise_for_problems()
    assert "ungenerated" in str(raised.value)


# -- which database, which connection ------------------------------------------------


def configure_two(project, pkg, backend: str) -> None:
    (project / "ferro.toml").write_text(
        f'[databases.main]\nmodels = ["{pkg}.models"]\ndialects = ["{backend}"]\n'
        f'[databases.analytics]\nmodels = ["{pkg}.reports"]\n'
        f'dialects = ["{backend}"]\n'
    )


async def test_two_databases_need_a_name(project, pkg, db):
    configure_two(project, pkg, db.backend)

    with pytest.raises(MigrationRefused) as raised:
        await ferro.migrations.status()
    assert "`analytics`" in str(raised.value) and "`main`" in str(raised.value)

    target = Target.resolve(database="analytics")
    assert target.database.name == "analytics"
    assert target.settings.databases["analytics"] is target.database


async def test_using_names_the_connection_the_run_happens_on(project, pkg, db):
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    await connect(db.url, name="reporting")

    report = await ferro.migrations.up(using="reporting")

    assert [s.migration for s in report.applied] == ["0001_create_author"]
    assert {"author", "_ferro_migrations"} <= db.tables()
    assert await ferro.migrations.require_applied(using="reporting") is None


async def test_no_default_connection_is_refused_naming_using(project, pkg, db):
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")

    with pytest.raises(MigrationRefused, match="using="):
        await ferro.migrations.up()


# -- the target: one resolution for every verb --------------------------------------


async def test_the_target_refuses_both_using_and_url(project, pkg, db):
    configure(project, pkg, db.backend)

    with pytest.raises(MigrationRefused) as raised:
        Target.resolve(using="reporting", url=db.url)
    assert str(raised.value) == "pass using= or url=, not both"
    for verb in (ferro.migrations.up, ferro.migrations.status, ferro.migrations.drift):
        with pytest.raises(MigrationRefused, match=r"^pass using= or url=, not both$"):
            await verb(using="reporting", url=db.url)


async def test_the_target_refuses_an_unknown_database(project, pkg, db):
    configure_two(project, pkg, db.backend)

    with pytest.raises(MigrationRefused) as raised:
        Target.resolve(database="billing")
    assert str(raised.value) == (
        f"{project / 'ferro.toml'} configures no database `billing`; "
        f"the configured databases are `main`, `analytics`"
    )
    with pytest.raises(MigrationRefused) as from_up:
        await ferro.migrations.up(database="billing")
    assert str(from_up.value) == str(raised.value)


async def test_the_target_refuses_no_configuration(project):
    with pytest.raises(MigrationRefused) as raised:
        Target.resolve()
    assert str(raised.value).startswith("no ferro config found; searched:\n")
    with pytest.raises(MigrationRefused) as from_status:
        await ferro.migrations.status()
    assert str(from_status.value) == str(raised.value)


async def test_the_target_opens_a_private_connection_on_url_and_closes_it(
    project, pkg, db
):
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    target = Target.resolve(url=db.url)

    async with target.open() as name:
        assert name.startswith("_ferro_migrate_")
        assert _core.connection_backend(name) == db.backend
    assert _core.connection_backend(name) is None
    assert _core._default_connection_name() is None

    report = await ferro.migrations.up(url=db.url)
    assert [s.migration for s in report.applied] == ["0001_create_author"]


async def test_up_refused_in_process_carries_its_run_report(project, pkg, db):
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    await connect(db.url)
    await ferro.migrations.up()
    up_file = migrations(project) / f"0001_create_author/01_schema.up.{db.backend}.sql"
    up_file.write_bytes(up_file.read_bytes() + b"\n")

    with pytest.raises(MigrationRefused) as raised:
        await ferro.migrations.up()

    report = raised.value.report
    assert isinstance(report, runner.RunReport)
    assert report.applied == []
    assert report.refusal == str(raised.value)
    assert report.refused is not None and report.refused.kind == "edited_applied"


async def test_importing_the_api_does_not_import_alembic():
    import subprocess

    code = (
        "import sys, ferro.migrations as m; "
        "assert callable(m.up) and callable(m.require_applied); "
        "print('alembic' in sys.modules)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "False"
