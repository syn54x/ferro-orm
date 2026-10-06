# ruff: noqa: F811 - pytest fixtures imported from a sibling module are redefined as arguments
"""``ferro migrate drift`` and ``ferro.migrations.drift()`` (#523, ADR-0031,
ADR-0043, ADR-0044).

```text
$ ferro migrate drift
drift against 0002_add_teams:
  team.name column is missing
```

Every test builds a real project under ``tmp_path``, applies its migrations
with ``ferro migrate up`` against the parametrized database (SQLite and
Postgres), then changes the database by hand with a plain driver, the way
drift happens in production, and reads the report back.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

import ferro
from ferro import _core
from ferro.migrations import DriftReport, MigrationRefused
from ferro.migrations.drift import _RENDERERS, render_op
from tests.test_migrate_new import (  # noqa: F401 - fixtures
    AUTHOR,
    isolated_imports,
    pkg,
    project,
    run,
    write_models,
)
from tests.test_migrate_up import configure, db, new, sql_step  # noqa: F401

pytestmark = [
    pytest.mark.usefixtures("isolated_imports", "clean_registry"),
    pytest.mark.backend_matrix,
]

TEAMS = (
    AUTHOR
    + """

from ferro import Check


class Team(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    name: str
    size: Annotated[int, FerroField(index=True)] = 0
    __ferro_checks__ = (Check("size_ok", lambda team: team.size >= 0),)
"""
)

HEAD = "0002_add_teams"
MIGRATION_OP = Path(__file__).parents[1] / "crates/ferro-migrate/src/lib.rs"


def applied(project, pkg, db, capsys) -> None:
    """``0001_create_author`` then ``0002_add_teams``, both applied."""
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    write_models(project, pkg, TEAMS)
    new("add_teams")
    assert run("migrate", "up", "--url", db.url) == 0
    capsys.readouterr()


def drift_cli(db, capsys) -> tuple[int, str, str]:
    code = run("migrate", "drift", "--url", db.url)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def drift_api(db) -> DriftReport:
    return asyncio.run(ferro.migrations.drift(url=db.url))


# -- the happy path -----------------------------------------------------------------


def test_after_up_there_is_no_drift_against_the_head(project, pkg, db, capsys):
    applied(project, pkg, db, capsys)

    assert drift_cli(db, capsys) == (0, f"no drift against {HEAD}\n", "")
    report = drift_api(db)
    assert report.clean
    assert report.against == HEAD
    assert report.lines == []
    assert report.operations == []
    report.raise_for_problems()


def test_a_column_dropped_by_hand_is_one_line_and_exit_4(project, pkg, db, capsys):
    applied(project, pkg, db, capsys)
    db.execute('ALTER TABLE "team" DROP COLUMN "name"')

    code, out, _ = drift_cli(db, capsys)
    assert code == 4
    assert out == f"drift against {HEAD}:\n  team.name column is missing\n"
    report = drift_api(db)
    assert not report.clean
    assert report.against == HEAD
    assert report.lines == ["team.name column is missing"]
    assert [op["kind"] for op in report.operations] == ["AddColumn"]


def test_a_changed_type_and_nullability_name_both_sides(project, pkg, db, capsys):
    if db.backend != "postgres":
        pytest.skip("SQLite cannot alter a column in place")
    applied(project, pkg, db, capsys)
    db.execute('ALTER TABLE "team" ALTER COLUMN "size" TYPE bigint')
    db.execute('ALTER TABLE "team" ALTER COLUMN "name" DROP NOT NULL')

    assert sorted(drift_api(db).lines) == [
        "team.name is nullable, snapshot says NOT NULL",
        "team.size has type bigint, snapshot says int",
    ]


def test_a_not_valid_check_and_an_invalid_index_have_their_own_lines(
    project, pkg, db, capsys
):
    if db.backend != "postgres":
        pytest.skip("SQLite has no unvalidated constraints or invalid indexes")
    applied(project, pkg, db, capsys)
    db.execute('ALTER TABLE "team" DROP CONSTRAINT "ck_team_size_ok"')
    db.execute(
        'ALTER TABLE "team" ADD CONSTRAINT "ck_team_size_ok" '
        'CHECK ("size" >= 0) NOT VALID'
    )
    db.execute(
        "UPDATE pg_index SET indisvalid = false "
        f"WHERE indexrelid = '\"{db.schema}\".\"idx_team_size\"'::regclass"
    )

    code, out, _ = drift_cli(db, capsys)
    assert code == 4
    lines = sorted(drift_api(db).lines)
    assert lines == [
        "ck_team_size_ok check is not validated",
        "idx_team_size index is invalid",
    ]
    assert "body differs" not in out


# -- what drift never reports ------------------------------------------------------


def test_alembic_version_an_extension_and_the_tracking_tables_are_not_drift(
    project, pkg, db, capsys
):
    applied(project, pkg, db, capsys)
    db.execute('CREATE TABLE "alembic_version" ("version_num" varchar(32) NOT NULL)')
    db.execute("INSERT INTO alembic_version VALUES ('abc123')")
    extension = False
    if db.backend == "postgres":
        found = db.rows(
            "SELECT n.nspname FROM pg_extension e "
            "JOIN pg_namespace n ON n.oid = e.extnamespace "
            "WHERE e.extname = 'pg_stat_statements'"
        )
        if found:
            pytest.skip(
                f"pg_stat_statements is already installed in schema {found[0][0]}; "
                "it can be installed once per database, so this test cannot place "
                "it in its own schema"
            )
        available = db.rows(
            "SELECT 1 FROM pg_available_extensions WHERE name = 'pg_stat_statements'"
        )
        if not available:
            pytest.skip("this Postgres server does not ship pg_stat_statements")
        db.execute(f'CREATE EXTENSION pg_stat_statements SCHEMA "{db.schema}"')
        extension = True
    try:
        tables = db.tables()
        assert {"alembic_version", "_ferro_migrations"} <= tables
        if extension:
            assert "pg_stat_statements" in {
                row[0]
                for row in db.rows(
                    "SELECT table_name FROM information_schema.views "
                    f"WHERE table_schema = '{db.schema}'"
                )
            }

        assert drift_cli(db, capsys) == (0, f"no drift against {HEAD}\n", "")
        assert drift_api(db).clean
        assert db.rows("SELECT version_num FROM alembic_version") == [("abc123",)]
    finally:
        if extension:
            db.execute("DROP EXTENSION pg_stat_statements")


def test_a_foreign_index_is_not_reported_and_a_ferro_named_extra_one_is(
    project, pkg, db, capsys
):
    applied(project, pkg, db, capsys)
    db.execute('CREATE INDEX "team_name_lookup" ON "team" ("name")')
    assert drift_api(db).clean

    db.execute('CREATE INDEX "idx_team_name" ON "team" ("name")')
    code, out, _ = drift_cli(db, capsys)
    assert code == 4
    assert out == f"drift against {HEAD}:\n  idx_team_name index is extra\n"


# -- the refusals ---------------------------------------------------------------------


def test_a_failed_step_refuses_naming_status(project, pkg, db, capsys):
    applied(project, pkg, db, capsys)
    sql_step(project, "broken", 'INSERT INTO "missing" VALUES (1);\n')
    capsys.readouterr()
    assert run("migrate", "up", "--url", db.url) == 1
    capsys.readouterr()

    code, out, err = drift_cli(db, capsys)
    assert code == 4
    assert out == ""
    assert "0003_broken" in err and "failed" in err
    assert "`ferro migrate status`" in err
    report = drift_api(db)
    assert not report.clean
    assert report.lines == [] and report.against is None
    assert report.refusal is not None and "`ferro migrate status`" in report.refusal
    with pytest.raises(MigrationRefused, match="ferro migrate status"):
        report.raise_for_problems()


def test_a_held_lock_refuses_naming_status(project, pkg, db, capsys):
    applied(project, pkg, db, capsys)
    # A run is in its step: the record is open and the lock is held.
    db.execute(
        "UPDATE _ferro_migrations SET finished_at = NULL WHERE migration = 2"
    )

    async def held() -> DriftReport:
        await ferro.connect(db.url, name="holder")
        handle = await _core._acquire_run_lock("holder", None, 0)
        try:
            assert await _core._run_lock_is_held("holder") is True
            report = await ferro.migrations.drift(url=db.url)
        finally:
            await _core._release_run_lock(handle)
            await _core._disconnect("holder")
        return report

    report = asyncio.run(held())
    assert report.lines == [] and not report.clean
    assert report.refusal is not None
    assert "`ferro migrate status`" in report.refusal
    assert "running" in report.refusal


def test_an_interrupted_step_refuses_naming_status(project, pkg, db, capsys):
    applied(project, pkg, db, capsys)
    db.execute(
        "UPDATE _ferro_migrations SET finished_at = NULL WHERE migration = 2"
    )

    code, out, err = drift_cli(db, capsys)
    assert code == 4
    assert out == ""
    assert "interrupted" in err and "`ferro migrate status`" in err


def test_a_database_with_no_records_drifts_against_nothing(project, pkg, db, capsys):
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    capsys.readouterr()

    code, out, err = drift_cli(db, capsys)
    assert code == 4
    assert out == ""
    assert "no migration records" in err and "`ferro migrate baseline`" in err
    report = drift_api(db)
    assert report.against is None and report.lines == [] and not report.clean
    assert "_ferro_migrations" not in db.tables()


def test_raise_for_problems_carries_every_line(project, pkg, db, capsys):
    applied(project, pkg, db, capsys)
    db.execute('ALTER TABLE "team" DROP COLUMN "name"')
    db.execute('CREATE INDEX "idx_team_name" ON "team" ("size")')
    report = drift_api(db)
    assert len(report.lines) == 2

    with pytest.raises(MigrationRefused) as raised:
        report.raise_for_problems()
    message = str(raised.value)
    assert f"drift against {HEAD}" in message
    for line in report.lines:
        assert line in message


# -- integration ------------------------------------------------------------------------


def test_drift_takes_no_lock_and_creates_nothing(
    project, pkg, db, capsys, monkeypatch
):
    applied(project, pkg, db, capsys)
    held_while_reading: list[bool] = []
    live_schema_ir = _core._live_schema_ir

    async def probing(using=None, tables_json=None):
        held_while_reading.append(await _core._run_lock_is_held(using))
        return await live_schema_ir(using, tables_json)

    monkeypatch.setattr(_core, "_live_schema_ir", probing)
    assert drift_api(db).clean
    assert held_while_reading == [False]


def test_drift_on_the_default_connection(project, pkg, db, capsys):
    applied(project, pkg, db, capsys)

    async def on_default() -> DriftReport:
        await ferro.connect(db.url)
        return await ferro.migrations.drift()

    report = asyncio.run(on_default())
    assert report.clean and report.against == HEAD


# -- the renderer -------------------------------------------------------------------------


def migration_op_variants() -> list[str]:
    source = MIGRATION_OP.read_text()
    body = source.split("pub enum MigrationOp {", 1)[1].split("\nimpl MigrationOp", 1)[0]
    return re.findall(r"^    ([A-Z][A-Za-z]+) \{", body, flags=re.MULTILINE)


def test_every_planner_op_kind_has_its_own_line():
    variants = migration_op_variants()
    assert len(variants) >= 25
    assert sorted(_RENDERERS) == sorted(variants)
    for kind in variants:
        op = {
            "kind": kind,
            "table": "team",
            "column": "name",
            "name": "ck_team_size_ok",
            "old_name": "fk_team_owner_id_user",
            "type_name": "status",
            "label": "archived",
            "labels": ["draft"],
            "columns": ["name"],
            "unique": False,
        }
        line = render_op(op)
        assert line and not line.startswith(f"{kind} on "), kind


def test_an_unknown_op_kind_still_renders_a_line():
    assert render_op({"kind": "Frobnicate", "table": "team"}) == "Frobnicate on team"
    assert render_op({"kind": "Frobnicate"}) == "Frobnicate"
