# ruff: noqa: F811 - pytest fixtures imported from a sibling module are redefined as arguments
"""``ferro migrate baseline`` and ``ferro.migrations.baseline()`` (#525, ADR-0031).

```text
$ ferro migrate baseline
drift against 0002_add_teams:
  team.name column is missing
nothing was recorded
$ ferro migrate baseline 0001
recorded 0001_create_author as baseline (1 step)
```

Every test builds a real project under ``tmp_path``, generates its
migrations with ``ferro migrate new``, builds the database the way a project
did before it had migrations (``connect(auto_migrate=True)``, or Alembic on
Postgres), then adopts it, on SQLite and Postgres.
"""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path

import pytest

import ferro
from ferro import _core
from ferro.migrations import MigrationRefused, baseline, remove_baseline
from tests.test_migrate_drift import TEAMS
from tests.test_migrate_new import (  # noqa: F401 - fixtures
    AUTHOR,
    isolated_imports,
    pkg,
    project,
    run,
    write_models,
)
from tests.test_migrate_up import (  # noqa: F401
    configure,
    db,
    migrations,
    new,
)

baseline_module = importlib.import_module("ferro.migrations.baseline")

pytestmark = [
    pytest.mark.usefixtures("isolated_imports", "clean_registry"),
    pytest.mark.backend_matrix,
]

ORGS = (
    TEAMS
    + """

class Org(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    title: str
"""
)

BOTH = "0001_create_author … 0002_add_teams"
NOT_COMPARED = (
    "baseline checked what `ferro migrate drift` checks: column defaults and "
    "objects ferro does not own were not compared\n"
)


def generated(project, pkg, db) -> None:
    """``0001_create_author`` and ``0002_add_teams``, from the same models the
    database is then built from."""
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    write_models(project, pkg, TEAMS)
    new("add_teams")


def auto_migrated(project, pkg, db) -> None:
    """The directory, and a database ``connect(auto_migrate=True)`` built from
    the models of ``0002``: it has their tables and no migration records."""
    generated(project, pkg, db)

    async def build() -> None:
        await ferro.connect(db.url, auto_migrate=True, name="built")
        await _core._disconnect("built")

    asyncio.run(build())
    assert {"author", "team"} <= db.tables()
    assert "_ferro_migrations" not in db.tables()


def cli(capsys, *argv: str) -> tuple[int, str, str]:
    code = run("migrate", *argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def origins(db) -> list[tuple]:
    return db.rows(
        "SELECT migration, step, origin, duration_ms, started_at = finished_at "
        "FROM _ferro_migrations ORDER BY migration, step"
    )


def no_records(db) -> bool:
    return "_ferro_migrations" not in db.tables() or not db.records()


# -- the happy path -----------------------------------------------------------------


def test_an_auto_migrated_database_is_baselined_and_up_applies_only_the_next(
    project, pkg, db, capsys
):
    auto_migrated(project, pkg, db)
    capsys.readouterr()

    code, out, err = cli(capsys, "baseline", "--url", db.url)
    assert (code, err) == (0, "")
    assert out == f"recorded {BOTH} as baseline (2 steps)\n" + NOT_COMPARED
    true = 1 if db.backend == "sqlite" else True
    assert origins(db) == [(1, 1, "baseline", 0, true), (2, 1, "baseline", 0, true)]

    code, out, _ = cli(capsys, "status", "--url", db.url)
    assert code == 0
    assert "0001_create_author  installed (baseline)" in out
    assert "0002_add_teams      installed (baseline)" in out

    write_models(project, pkg, ORGS)
    new("add_orgs")
    capsys.readouterr()
    code, out, _ = cli(capsys, "up", "--url", db.url)
    assert code == 0
    assert out.splitlines()[0].startswith("0003_add_orgs  01_schema  applied (")
    assert len(out.splitlines()) == 1
    assert "org" in db.tables()


def test_data_steps_are_recorded_without_running_and_listed(project, pkg, db, capsys):
    auto_migrated(project, pkg, db)
    data_step = migrations(project) / "0001_create_author/02_backfill_author.py"
    data_step.write_text(
        "raise SystemExit('a baseline never runs a data step')\n\n\n"
        "@chunked(lambda models: models.Author.select(), batch_size=100)\n"
        "async def up(ctx, batch): ...\n\n\n"
        '@nothing_to_reverse("derived")\n'
        "def down(ctx): ...\n"
    )
    capsys.readouterr()

    code, out, _ = cli(capsys, "baseline", "--url", db.url)
    assert code == 0
    assert (
        out
        == (
            f"recorded {BOTH} as baseline (3 steps; 1 data step listed, not run)\n"
            "  0001_create_author/02_backfill_author.py  recorded, not run\n"
        )
        + NOT_COMPARED
    )
    assert [row[:3] for row in origins(db)] == [
        (1, 1, "baseline"),
        (1, 2, "baseline"),
        (2, 1, "baseline"),
    ]
    # The step's declared shape, read from its file without running it.
    assert db.rows(
        "SELECT kind FROM _ferro_migrations WHERE migration = 1 AND step = 2"
    ) == [("chunked",)]


def test_an_undeclared_data_step_refuses_the_baseline(project, pkg, db, capsys):
    auto_migrated(project, pkg, db)
    data_step = migrations(project) / "0001_create_author/02_backfill_author.py"
    data_step.write_text("async def up(ctx): ...\n\n\ndef down(ctx): ...\n")
    capsys.readouterr()

    code, _, err = cli(capsys, "baseline", "--url", db.url)

    assert code == 1
    assert "0001_create_author/02_backfill_author.py: up() has no declaration" in err
    assert "_ferro_migrations" not in db.tables() or origins(db) == []


def test_the_in_process_call_returns_its_report(project, pkg, db):
    auto_migrated(project, pkg, db)

    report = asyncio.run(baseline(url=db.url))

    assert report.recorded == ["0001_create_author", "0002_add_teams"]
    assert report.data_steps_listed == []
    assert report.drift is None
    report.raise_for_problems()


def test_an_alembic_built_database_is_baselined_and_alembic_version_untouched(
    project, pkg, db, capsys
):
    if db.backend != "postgres":
        pytest.skip("the Alembic harness runs against Postgres")
    from tests._alembic_harness import autogen_upgrade_code, run_generated_code

    generated(project, pkg, db)
    run_generated_code(autogen_upgrade_code(db.base, db.schema), db.base, db.schema)
    db.execute('CREATE TABLE "alembic_version" ("version_num" varchar(32) NOT NULL)')
    db.execute("INSERT INTO alembic_version VALUES ('ae1027a6acf')")
    assert {"author", "team", "alembic_version"} <= db.tables()
    capsys.readouterr()

    code, out, err = cli(capsys, "baseline", "--url", db.url)

    assert (code, err) == (0, "")
    assert out.startswith(f"recorded {BOTH} as baseline (2 steps)\n")
    assert db.rows("SELECT version_num FROM alembic_version") == [("ae1027a6acf",)]


def test_a_baseline_at_an_earlier_migration_ignores_the_later_tables(
    project, pkg, db, capsys
):
    auto_migrated(project, pkg, db)
    capsys.readouterr()

    code, out, _ = cli(capsys, "baseline", "0001", "--url", db.url)
    assert code == 0
    assert out == "recorded 0001_create_author as baseline (1 step)\n" + NOT_COMPARED
    assert [row[:3] for row in origins(db)] == [(1, 1, "baseline")]

    code, out, _ = cli(capsys, "status", "--url", db.url)
    assert code == 3
    assert "0001_create_author  installed (baseline)" in out
    assert "0002_add_teams      pending" in out


# -- refusals ------------------------------------------------------------------------


def test_drift_is_listed_exits_4_and_records_nothing(project, pkg, db, capsys):
    auto_migrated(project, pkg, db)
    db.execute('ALTER TABLE "team" DROP COLUMN "name"')
    capsys.readouterr()

    code, out, _ = cli(capsys, "baseline", "--url", db.url)

    assert code == 4
    assert out == (
        "drift against 0002_add_teams:\n"
        "  team.name column is missing\n"
        "nothing was recorded\n"
    )
    assert no_records(db)
    report = asyncio.run(baseline(url=db.url))
    assert report.recorded == []
    assert report.drift is not None
    assert report.drift.lines == ["team.name column is missing"]
    with pytest.raises(MigrationRefused, match="team.name column is missing"):
        report.raise_for_problems()
    assert no_records(db)


def test_a_database_with_records_is_refused_naming_status(project, pkg, db, capsys):
    auto_migrated(project, pkg, db)
    assert cli(capsys, "baseline", "0001", "--url", db.url)[0] == 0
    before = db.records()

    code, out, err = cli(capsys, "baseline", "--url", db.url)

    assert (code, out) == (1, "")
    assert "already has migration records (0001_create_author)" in err
    assert "Run `ferro migrate status`" in err
    assert db.records() == before


def test_a_target_the_directory_lacks_is_refused_naming_the_directory(
    project, pkg, db, capsys
):
    auto_migrated(project, pkg, db)

    code, _, err = cli(capsys, "baseline", "0009", "--url", db.url)

    assert code == 1
    assert err.strip() == (
        "ferro migrate baseline: 0009 names no migration in migrations/: give a "
        "migration number from 0001 to 0002, or a migration's full name "
        "(0002_add_teams). Nothing was recorded."
    )
    assert no_records(db)
    with pytest.raises(MigrationRefused, match="0009 names no migration"):
        asyncio.run(baseline(url=db.url, target="0009"))


# -- undoing a baseline, and down ------------------------------------------------------


def test_remove_is_refused_under_a_run_and_undoes_the_baseline_after_down(
    project, pkg, db, capsys
):
    auto_migrated(project, pkg, db)
    assert cli(capsys, "baseline", "--url", db.url)[0] == 0
    write_models(project, pkg, ORGS)
    new("add_orgs")
    assert cli(capsys, "up", "--url", db.url)[0] == 0

    code, out, err = cli(capsys, "baseline", "--remove", "--url", db.url)
    assert (code, out) == (1, "")
    assert err.strip() == (
        "ferro migrate baseline --remove: 0003_add_orgs was applied by a run above "
        "the baseline at 0002_add_teams. Revert it first with `ferro migrate down "
        "--to 0002`, then remove the baseline. Nothing was removed."
    )
    assert len(db.records()) == 3

    assert cli(capsys, "down", "--to", "0002", "--yes", "--url", db.url)[0] == 0
    code, out, _ = cli(capsys, "baseline", "--remove", "--url", db.url)
    assert code == 0
    assert out == f"removed the baseline of {BOTH}\n"
    assert db.records() == []

    code, out, _ = cli(capsys, "status", "--url", db.url)
    assert code == 3
    for name in ("0001_create_author", "0002_add_teams", "0003_add_orgs"):
        assert f"{name}  " in out
    assert "installed" not in out

    assert asyncio.run(remove_baseline(url=db.url)) == []
    assert cli(capsys, "baseline", "--remove", "--url", db.url)[1] == (
        "no baseline to remove\n"
    )


def test_down_below_a_baseline_is_refused_naming_it(project, pkg, db, capsys):
    auto_migrated(project, pkg, db)
    assert cli(capsys, "baseline", "--url", db.url)[0] == 0
    before = db.records()

    code, _, err = cli(capsys, "down", "--to", "0001", "--yes", "--url", db.url)

    assert code == 1
    assert "0002 was recorded by `baseline`" in err
    assert "Nothing was reverted." in err
    assert db.records() == before
    assert {"author", "team"} <= db.tables()


# -- the run lock -----------------------------------------------------------------------


def test_baseline_holds_the_run_lock_while_it_writes(project, pkg, db, monkeypatch):
    auto_migrated(project, pkg, db)
    write = _core._write_baseline_records
    seen: list[bool] = []

    async def observed(using, records_json, tracking_schema=None, lock=None):
        # Asked from a second task, on its own connection, while baseline
        # is about to write.
        async def probe() -> bool:
            await ferro.connect(db.url, name="probe")
            try:
                return await _core._run_lock_is_held("probe")
            finally:
                await _core._disconnect("probe")

        seen.append(await asyncio.create_task(probe()))
        return await write(using, records_json, tracking_schema, lock)

    monkeypatch.setattr(_core, "_write_baseline_records", observed)

    report = asyncio.run(baseline(url=db.url))

    assert report.recorded == ["0001_create_author", "0002_add_teams"]
    assert seen == [True]
    assert asyncio.run(_probe_released(db.url)) is False


async def _probe_released(url: str) -> bool:
    await ferro.connect(url, name="after")
    try:
        return await _core._run_lock_is_held("after")
    finally:
        await _core._disconnect("after")


def test_the_module_and_its_function_are_both_reachable():
    # ``ferro.migrations.baseline`` is the function (as ``drift`` is); the
    # module stays importable by its dotted name.
    assert callable(ferro.migrations.baseline)
    assert baseline_module.baseline is baseline
    assert baseline_module.remove_baseline is remove_baseline
    assert Path(baseline_module.__file__).name == "baseline.py"
