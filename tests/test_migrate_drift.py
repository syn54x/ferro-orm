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
import importlib
import json
import logging
import re
import shutil
import sys
from pathlib import Path

import pytest

import ferro
from ferro import _core
from ferro.migrations import DriftReport, MigrationRefused, render_op
from tests.test_migrate_new import (  # noqa: F401 - fixtures
    AUTHOR,
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
    sql_step,
)

drift_module = importlib.import_module("ferro.migrations.drift")

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


TEMPORAL = (
    AUTHOR
    + """

import datetime as dt
import uuid
from decimal import Decimal


class Event(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    seen: dt.datetime
    day: dt.date
    at: dt.time
    ref: uuid.UUID
    amount: Decimal
"""
)


def test_same_storage_types_are_not_drift_after_up(project, pkg, db, capsys):
    """A live SQLite ``DATETIME`` reads back as token ``timestamp`` while a
    ``datetime`` field declares ``timestamptz``; both store as ``DATETIME``,
    so a database ``up`` just created has nothing to report on either
    dialect (no phantom ``event.seen has type timestamp, snapshot says
    timestamptz``)."""
    configure(project, pkg, db.backend)
    write_models(project, pkg, TEMPORAL)
    new("create_event")
    assert run("migrate", "up", "--url", db.url) == 0
    capsys.readouterr()

    assert drift_cli(db, capsys) == (0, "no drift against 0001_create_event\n", "")
    assert drift_api(db).lines == []


NAMED = """
from ferro import Check


class Ckn_Named(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    name: str
    __ferro_checks__ = (Check("named", lambda named: named.name != ""),)
"""


class _ReconcileStatements(logging.Handler):
    """Collect the DDL the reconciliation pass logs for one table."""

    def __init__(self, table: str):
        super().__init__(level=logging.DEBUG)
        self.prefix = f"Ferro Engine: auto-migrate executing on '{table}': "
        self.statements: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if message.startswith(self.prefix):
            self.statements.append(message[len(self.prefix) :])


def migrate_updates_statements(url: str, table: str) -> list[str]:
    """``connect(migrate_updates=True)``; the statements the pass executed
    for ``table``."""
    ferro.reset_engine()
    logger = logging.getLogger("ferro")
    handler = _ReconcileStatements(table)
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        asyncio.run(ferro.connect(url, migrate_updates=True))
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
    return handler.statements


def test_a_text_comparison_check_is_not_drift_after_up(project, pkg, db, capsys):
    """``name != ""`` over a ``str`` column: ferro renders ``"name" <> ''``,
    Postgres prints ``CHECK (((name)::text <> ''::text))``. Same predicate,
    so ``drift()`` reports no ``ck_ckn_named_named check body differs``.
    SQLite keeps the body as written and is clean too."""
    configure(project, pkg, db.backend)
    write_models(project, pkg, NAMED)
    new("create_named")
    assert run("migrate", "up", "--url", db.url) == 0
    capsys.readouterr()

    report = drift_api(db)
    assert report.lines == [] and report.clean, report.lines
    assert report.operations == []


def test_a_text_comparison_check_is_not_rebuilt_on_every_connect(project, pkg, db):
    """The reconciliation pass reads the same catalog body through the same
    normalizer: after ``connect(auto_migrate=True)`` creates the table, each
    ``connect(migrate_updates=True)`` executes nothing for it."""
    write_models(project, pkg, NAMED)
    sys.path.insert(0, str(project))
    importlib.import_module(f"{pkg}.models")
    asyncio.run(ferro.connect(db.url, auto_migrate=True))
    if db.backend == "postgres":
        assert db.rows(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'ck_ckn_named_named' "
            f"AND connamespace = '{db.schema}'::regnamespace"
        ) == [("CHECK (((name)::text <> ''::text))",)], (
            "the pin is only meaningful if Postgres prints the text casts"
        )

    assert migrate_updates_statements(db.url, "ckn_named") == []
    assert migrate_updates_statements(db.url, "ckn_named") == []


PRICED = """
from decimal import Decimal

from ferro import Check


class Ckn_Priced(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    price: Decimal
    __ferro_checks__ = (Check("priced", lambda priced: priced.price > 10),)
"""


def test_a_cast_ferro_did_not_write_is_rebuilt(project, pkg, db):
    """Declared ``price > 10`` prints as ``CHECK ((price > (10)::numeric))``:
    that literal coercion is display, so a connect changes nothing. A hand
    edit to ``price::integer > 10`` prints as ``CHECK (((price)::integer >
    10))``: that cast is predicate, so ``migrate_updates`` rebuilds the
    declared body."""
    if db.backend != "postgres":
        pytest.skip("SQLite cannot alter a check in place (ADR-0014)")
    write_models(project, pkg, PRICED)
    sys.path.insert(0, str(project))
    importlib.import_module(f"{pkg}.models")
    asyncio.run(ferro.connect(db.url, auto_migrate=True))

    def constraintdef() -> str:
        rows = db.rows(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'ck_ckn_priced_priced' "
            f"AND connamespace = '{db.schema}'::regnamespace"
        )
        assert len(rows) == 1, rows
        return rows[0][0]

    assert constraintdef() == "CHECK ((price > (10)::numeric))"
    assert migrate_updates_statements(db.url, "ckn_priced") == []

    db.execute('ALTER TABLE "ckn_priced" DROP CONSTRAINT "ck_ckn_priced_priced"')
    db.execute(
        'ALTER TABLE "ckn_priced" ADD CONSTRAINT "ck_ckn_priced_priced" '
        'CHECK ("price"::integer > 10)'
    )
    assert constraintdef() == "CHECK (((price)::integer > 10))"

    assert migrate_updates_statements(db.url, "ckn_priced") == [
        'ALTER TABLE "ckn_priced" DROP CONSTRAINT "ck_ckn_priced_priced"',
        'ALTER TABLE "ckn_priced" ADD CONSTRAINT "ck_ckn_priced_priced" '
        'CHECK ("price" > 10)',
    ]
    assert constraintdef() == "CHECK ((price > (10)::numeric))"


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


SQUADS = TEAMS.replace(
    "class Team(Model):\n", 'class Squad(Model):\n    __ferro_renamed_from__ = "team"\n'
).replace("lambda team: team.size", "lambda squad: squad.size")


def test_a_rename_undone_by_hand_reads_the_hinted_old_table(project, pkg, db, capsys):
    """The snapshot's ``Squad`` was ``team``; a database holding ``team`` and
    no ``squad`` is one rename away from it, the same read the
    reconciliation pass makes, never a missing table."""
    applied(project, pkg, db, capsys)
    write_models(project, pkg, SQUADS)
    new("rename_team")
    assert run("migrate", "up", "--url", db.url) == 0
    capsys.readouterr()
    db.execute('ALTER TABLE "squad" RENAME TO "team"')

    report = drift_api(db)
    assert report.lines == ["team table is named squad in the snapshot"]
    assert [op["kind"] for op in report.operations] == ["RenameTable"]


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
        f'WHERE indexrelid = \'"{db.schema}"."idx_team_size"\'::regclass'
    )

    code, out, _ = drift_cli(db, capsys)
    assert code == 4
    lines = sorted(drift_api(db).lines)
    assert lines == [
        "ck_team_size_ok check is not validated",
        "idx_team_size index is invalid",
    ]
    assert "body differs" not in out


def test_a_primary_key_moved_by_hand_is_one_line(project, pkg, db, capsys):
    if db.backend != "postgres":
        pytest.skip("SQLite cannot move a primary key in place")
    applied(project, pkg, db, capsys)
    db.execute('ALTER TABLE "team" DROP CONSTRAINT "team_pkey"')
    db.execute('ALTER TABLE "team" ADD PRIMARY KEY ("name")')

    code, out, _ = drift_cli(db, capsys)
    assert code == 4
    assert out == (
        f"drift against {HEAD}:\n  team primary key is (name), snapshot says (id)\n"
    )
    assert [op["kind"] for op in drift_api(db).operations] == ["ChangePrimaryKey"]


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
    db.execute("UPDATE _ferro_migrations SET finished_at = NULL WHERE migration = 2")

    async def held() -> DriftReport:
        await ferro.connect(db.url, name="holder")
        tracked = await _core._open_tracked("holder", None, "migrations")
        try:
            async with tracked.locked(0):
                assert await tracked.lock_held() is True
                report = await ferro.migrations.drift(url=db.url)
        finally:
            await _core._disconnect("holder")
        return report

    report = asyncio.run(held())
    assert report.lines == [] and not report.clean
    assert report.refusal is not None
    assert "`ferro migrate status`" in report.refusal
    assert "running" in report.refusal


def test_an_interrupted_step_refuses_naming_status(project, pkg, db, capsys):
    applied(project, pkg, db, capsys)
    db.execute("UPDATE _ferro_migrations SET finished_at = NULL WHERE migration = 2")

    code, out, err = drift_cli(db, capsys)
    assert code == 4
    assert out == ""
    assert "interrupted" in err and "`ferro migrate status`" in err


def assert_refused_naming_status(db, capsys, *expected: str) -> None:
    code, out, err = drift_cli(db, capsys)
    assert (code, out) == (4, "")
    assert "`ferro migrate status`" in err
    for text in expected:
        assert text in err
    report = drift_api(db)
    assert report.lines == [] and report.against is None and not report.clean


def test_a_held_lock_with_every_record_finished_refuses(project, pkg, db, capsys):
    applied(project, pkg, db, capsys)

    async def held() -> DriftReport:
        await ferro.connect(db.url, name="holder")
        tracked = await _core._open_tracked("holder", None, "migrations")
        try:
            async with tracked.locked(0):
                return await ferro.migrations.drift(url=db.url)
        finally:
            await _core._disconnect("holder")

    report = asyncio.run(held())
    assert report.lines == [] and not report.clean
    assert report.refusal is not None
    assert "a migration run holds the run lock" in report.refusal
    assert "`ferro migrate status`" in report.refusal


def test_a_reverting_step_refuses(project, pkg, db, capsys):
    applied(project, pkg, db, capsys)
    db.execute("UPDATE _ferro_migrations SET reverting = TRUE WHERE migration = 2")

    assert_refused_naming_status(db, capsys, "0002_add_teams/", "is reverting")


def test_a_partly_applied_migration_refuses(project, pkg, db, capsys):
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    write_models(project, pkg, TEAMS)
    new("add_teams", "--sql-step", "seed")
    seed = sorted(migrations(project).glob("0002_add_teams/02_seed.*"))
    assert seed, sorted(p.name for p in migrations(project).rglob("*"))
    assert run("migrate", "up", "--url", db.url) == 0
    db.execute("DELETE FROM _ferro_migrations WHERE migration = 2 AND step = 2")
    capsys.readouterr()

    assert_refused_naming_status(db, capsys, "0002_add_teams is partly applied")


def test_a_database_ahead_of_the_checkout_refuses(project, pkg, db, capsys):
    applied(project, pkg, db, capsys)
    shutil.rmtree(migrations(project) / HEAD)

    assert_refused_naming_status(db, capsys, f"has applied {HEAD}")


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


def test_drift_takes_no_lock_and_creates_nothing(project, pkg, db, capsys, monkeypatch):
    applied(project, pkg, db, capsys)
    held_while_reading: list[bool] = []
    live_schema_ir = _core._live_schema_ir

    async def probing(using, declared_json, extra_tables_json=None):
        tracked = await _core._open_tracked(using, None, "migrations")
        held_while_reading.append(await tracked.lock_held())
        return await live_schema_ir(using, declared_json, extra_tables_json)

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
    body = source.split("pub enum MigrationOp {", 1)[1].split("\nimpl MigrationOp", 1)[
        0
    ]
    return re.findall(r"^    ([A-Z][A-Za-z]+) \{", body, flags=re.MULTILINE)


def test_every_planner_op_kind_has_its_own_line():
    variants = migration_op_variants()
    assert len(variants) >= 25
    assert sorted(drift_module._RENDERERS) == sorted(variants)
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
            "from": ["id"],
            "to": ["name"],
            "old": "ck_team_size",
            "new": "ck_team_size_ok",
        }
        line = render_op(op)
        assert line and not line.startswith(f"{kind} on "), kind


def test_a_moved_primary_key_names_both_column_sets():
    op = {"kind": "ChangePrimaryKey", "table": "team", "from": ["name"], "to": ["id"]}
    assert render_op(op) == "team primary key is (name), snapshot says (id)"
    composite = {**op, "from": ["org_id", "name"], "to": []}
    assert (
        render_op(composite) == "team primary key is (org_id, name), snapshot says ()"
    )
    with pytest.raises(ValueError, match=r"ChangePrimaryKey op has no 'to' field"):
        render_op({"kind": "ChangePrimaryKey", "table": "team", "from": ["id"]})


def test_an_unknown_op_kind_still_renders_a_line():
    assert render_op({"kind": "Frobnicate", "table": "team"}) == "Frobnicate on team"
    assert render_op({"kind": "Frobnicate"}) == "Frobnicate"


def test_a_known_op_kind_missing_a_field_fails_naming_both():
    with pytest.raises(ValueError, match=r"AddColumn op has no 'column' field"):
        render_op({"kind": "AddColumn", "table": "team"})


def test_render_op_is_published_beside_drift():
    """``ferro.migrations.drift`` is the function, so the renderer is reached
    through the package; it is the drift module's own ``render_op``."""
    assert ferro.migrations.drift is not drift_module
    assert ferro.migrations.render_op is drift_module.render_op


# -- against one snapshot --------------------------------------------------------------


def snapshot_of(project, name: str) -> dict:
    contents = _core._read_migrations_dir(str(migrations(project)))
    return next(
        m["snapshot"]["ir"]
        for m in json.loads(contents)["migrations"]
        if Path(m["dir"]).name == name
    )


def test_against_one_snapshot_gives_the_lines_drift_prints(project, pkg, db, capsys):
    """``drift`` is ``against`` the last applied migration's snapshot: the
    same lines, the same ops, the same text."""
    applied(project, pkg, db, capsys)
    db.execute('ALTER TABLE "team" DROP COLUMN "name"')
    db.execute('CREATE INDEX "idx_team_name" ON "team" ("size")')

    async def compared() -> DriftReport:
        await ferro.connect(db.url, name="probe")
        try:
            return await drift_module.against(
                snapshot_of(project, HEAD), migration=HEAD, using="probe"
            )
        finally:
            await _core._disconnect("probe")

    report = asyncio.run(compared())
    audited = drift_api(db)
    assert report == audited
    assert sorted(report.lines) == [
        "idx_team_name index is extra",
        "team.name column is missing",
    ]
    code, out, _ = drift_cli(db, capsys)
    assert (code, out) == (4, report.render() + "\n")


def test_against_an_earlier_snapshot_reads_only_its_tables(project, pkg, db, capsys):
    """Against ``0001``'s snapshot the ``team`` table ``0002`` added is not
    drift: only the snapshot's tables are read (ADR-0031)."""
    applied(project, pkg, db, capsys)

    async def compared() -> DriftReport:
        await ferro.connect(db.url, name="probe")
        try:
            return await drift_module.against(
                snapshot_of(project, "0001_create_author"),
                migration="0001_create_author",
                using="probe",
            )
        finally:
            await _core._disconnect("probe")

    report = asyncio.run(compared())
    assert report.clean and report.against == "0001_create_author"


def test_against_a_connection_that_is_not_open_is_refused(project, pkg, db, capsys):
    applied(project, pkg, db, capsys)
    with pytest.raises(MigrationRefused, match="connection `nowhere` is not open"):
        asyncio.run(
            drift_module.against(
                snapshot_of(project, HEAD), migration=HEAD, using="nowhere"
            )
        )


# -- a redefined index and a removed foreign key (ADR-0051) ------------------------


def test_a_ferro_named_index_over_other_columns_is_drift(project, pkg, db, capsys):
    """``idx_team_size`` rebuilt by hand over ``name``: the name is the
    snapshot's, its definition is not."""
    applied(project, pkg, db, capsys)
    db.execute('DROP INDEX "idx_team_size"')
    db.execute('CREATE INDEX "idx_team_size" ON "team" ("name")')

    report = drift_api(db)
    assert report.lines == ["idx_team_size index is on (name), snapshot says (size)"]
    assert report.operations[0]["kind"] == "RedefineIndex"


def test_a_ferro_named_index_made_unique_is_drift(project, pkg, db, capsys):
    applied(project, pkg, db, capsys)
    db.execute('DROP INDEX "idx_team_size"')
    db.execute('CREATE UNIQUE INDEX "idx_team_size" ON "team" ("size")')

    assert drift_api(db).lines == [
        "idx_team_size index is unique, snapshot says not unique"
    ]
