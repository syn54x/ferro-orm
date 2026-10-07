# ruff: noqa: F811 - pytest fixtures imported from a sibling module are redefined as arguments
"""``ferro migrate up`` (#519): the run, its records and its refusals.

Every test builds a real project under ``tmp_path`` (a ``ferro.toml`` and a
models package), writes migrations with ``ferro migrate new``, runs them
against the parametrized database (SQLite and Postgres) and reads the
tracking table back with a plain driver, the way an operator would.
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import sqlite3
import warnings
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import ferro
from ferro.migrations import runner
from ferro.settings import FerroSettings
from tests.test_migrate_new import (  # noqa: F401 - fixtures
    AUTHOR,
    LIBRARY,
    isolated_imports,
    pkg,
    project,
    run,
    write_models,
)

pytestmark = [
    pytest.mark.usefixtures("isolated_imports", "clean_registry"),
    pytest.mark.backend_matrix,
]


COLUMNS = (
    "migration, step, migration_name, file, kind, checksum, snapshot_checksum, "
    "origin, ferro_version, finished_at, failed_at, error"
)


# -- the database, read the way an operator would -----------------------------


class Db:
    """Plain-driver access to the database behind ``db_url``."""

    def __init__(self, url: str, backend: str, base: str | None, schema: str | None):
        self.url = url
        self.backend = backend
        self.base = base
        self.schema = schema

    def _connect(self) -> Any:
        if self.backend == "sqlite":
            path = self.url.removeprefix("sqlite:").split("?")[0]
            return sqlite3.connect(path, isolation_level=None)
        import psycopg

        conn = psycopg.connect(self.base, autocommit=True)
        conn.execute(f'SET search_path TO "{self.schema}"')
        return conn

    def rows(self, sql: str) -> list[tuple]:
        conn = self._connect()
        try:
            return list(conn.execute(sql).fetchall())
        finally:
            conn.close()

    def execute(self, sql: str) -> None:
        conn = self._connect()
        try:
            conn.execute(sql)
        finally:
            conn.close()

    def tables(self) -> set[str]:
        if self.backend == "sqlite":
            rows = self.rows("SELECT name FROM sqlite_master WHERE type = 'table'")
        else:
            rows = self.rows(
                "SELECT table_name FROM information_schema.tables "
                f"WHERE table_schema = '{self.schema}'"
            )
        return {row[0] for row in rows}

    def records(self) -> list[tuple]:
        return self.rows(
            f"SELECT {COLUMNS} FROM _ferro_migrations ORDER BY migration, step"
        )


@pytest.fixture
def db(db_url, db_backend, postgres_base_url, db_schema_name) -> Db:
    return Db(db_url, db_backend, postgres_base_url, db_schema_name)


# -- the project ------------------------------------------------------------------


def configure(project: Path, pkg: str, backend: str, extra: str = "") -> None:
    (project / "ferro.toml").write_text(
        f'models = ["{pkg}.models"]\ndialects = ["{backend}"]\n{extra}'
    )


def new(*argv: str) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        assert run("migrate", "new", *argv) == 0


def sql_step(project: Path, name: str, body: str) -> Path:
    """Write the next migration as one hand-written SQL step holding ``body``."""
    new(name, "--sql-step", name)
    migration = sorted((project / "migrations").glob(f"*_{name}"))[-1]
    up = migration / f"01_{name}.up.sql"
    up.write_text(body)
    (migration / f"01_{name}.down.sql").write_text("SELECT 1;\n")
    return up


def sha384(path: Path) -> str:
    return hashlib.sha384(path.read_bytes()).hexdigest()


def migrations(project: Path) -> Path:
    return project / "migrations"


def short_time(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    return f"{value[:10]} {value[11:16]} UTC"


def settings_and_database() -> tuple[FerroSettings, Any]:
    settings = FerroSettings()
    return settings, settings.database()


# -- the happy path -----------------------------------------------------------------


def test_up_applies_every_step_and_writes_one_finished_record_each(
    project, pkg, db, capsys
):
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    write_models(project, pkg, LIBRARY)
    new("create_post")
    capsys.readouterr()

    assert run("migrate", "up", "--url", db.url) == 0

    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("0001_create_author  01_schema  applied (")
    assert out[1].startswith("0002_create_post    01_schema  applied (")
    assert {"_ferro_migrations", "_ferro_migrations_format", "author", "post"} <= (
        db.tables()
    )
    governed = "main" if db.backend == "sqlite" else db.schema
    assert db.rows("SELECT format, governed_schema FROM _ferro_migrations_format") == [
        (1, governed)
    ]
    from importlib.metadata import version as installed

    version = installed("ferro-orm")
    records = db.records()
    assert len(records) == 2
    for record, name in zip(records, ["0001_create_author", "0002_create_post"]):
        number, step, migration_name, file, kind, checksum, snapshot, origin, ran_by = (
            record[:9]
        )
        up_file = migrations(project) / name / f"01_schema.up.{db.backend}.sql"
        assert (step, migration_name, file, kind, origin, ran_by) == (
            1,
            name,
            up_file.name,
            "ddl",
            "run",
            version,
        )
        assert number == int(name[:4])
        assert checksum == sha384(up_file)
        assert snapshot == sha384(migrations(project) / name / "ir.json")
        assert record[9] is not None and record[10] is None and record[11] is None

    assert run("migrate", "up", "--url", db.url) == 0
    assert capsys.readouterr().out == "nothing to apply: the database is up to date\n"
    assert len(db.records()) == 2


def test_tracking_tables_live_in_tracking_schema_and_a_missing_one_is_refused(
    project, pkg, db, capsys
):
    if db.backend != "postgres":
        pytest.skip("tracking_schema is Postgres-only")
    tracking = f"{db.schema}_track"
    configure(project, pkg, "postgres", f'tracking_schema = "{tracking}"\n')
    write_models(project, pkg, AUTHOR)
    new("create_author")
    capsys.readouterr()

    assert run("migrate", "up", "--url", db.url) == 1
    err = capsys.readouterr().err
    assert f'CREATE SCHEMA "{tracking}";' in err
    assert "author" not in db.tables()

    db.execute(f'CREATE SCHEMA "{tracking}"')
    try:
        assert run("migrate", "up", "--url", db.url) == 0
        rows = db.rows(
            f'SELECT migration, governed_schema FROM "{tracking}"._ferro_migrations, '
            f'"{tracking}"._ferro_migrations_format'
        )
        assert rows == [(1, db.schema)]
        assert "_ferro_migrations" not in db.tables()
    finally:
        db.execute(f'DROP SCHEMA "{tracking}" CASCADE')


def test_a_failed_step_is_recorded_and_the_next_up_resumes_at_it(
    project, pkg, db, capsys
):
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    header = "-- ferro: no-transaction\n" if db.backend == "postgres" else ""
    fix = sql_step(
        project,
        "fix",
        f'{header}CREATE TABLE "ok_one" ("id" integer);\n'
        'INSERT INTO "missing" VALUES (1);\n',
    )
    sql_step(project, "three", 'CREATE TABLE "three" ("id" integer);\n')
    capsys.readouterr()

    assert run("migrate", "up", "--url", db.url) == 1
    captured = capsys.readouterr()
    assert captured.out.startswith("0001_create_author  01_schema  applied (")
    assert "ferro migrate: 0002_fix/01_fix.up.sql failed:" in captured.err
    assert "missing" in captured.err
    records = db.records()
    assert [(r[0], r[9] is not None) for r in records] == [(1, True), (2, False)]
    failed = records[1]
    assert failed[10] is not None and "missing" in failed[11]
    assert "three" not in db.tables()

    assert run("migrate", "status", "--url", db.url) == 4
    out = capsys.readouterr().out
    assert "  01_fix.up.sql  failed" in out
    assert failed[11] in out

    fix.write_text(
        f'{header}CREATE TABLE IF NOT EXISTS "ok_one" ("id" integer);\n'
        'CREATE TABLE "missing" ("id" integer);\n'
        'INSERT INTO "missing" VALUES (1);\n'
    )
    assert run("migrate", "up", "--url", db.url) == 0
    out = capsys.readouterr().out
    assert (
        f"re-recorded 0002_fix/01_fix.up.sql (sha384:{failed[5]} → sha384:{sha384(fix)})"
        in out
    )
    assert "0002_fix" in out and "0003_three" in out
    records = db.records()
    assert all(r[9] is not None for r in records) and len(records) == 3
    assert records[1][5] == sha384(fix)
    assert records[1][10] is None and records[1][11] is None
    assert run("migrate", "status", "--url", db.url) == 0


def test_create_index_concurrently_runs_in_a_no_transaction_step(
    project, pkg, db, capsys
):
    if db.backend != "postgres":
        pytest.skip("no-transaction is Postgres-only")
    configure(project, pkg, "postgres")
    write_models(project, pkg, AUTHOR)
    new("create_author")
    sql_step(
        project,
        "name_index",
        "-- ferro: no-transaction\n"
        'CREATE INDEX CONCURRENTLY "idx_author_name_c" ON "author" ("name");\n',
    )

    assert run("migrate", "up", "--url", db.url) == 0

    assert db.rows(
        "SELECT indexname FROM pg_indexes WHERE indexname = 'idx_author_name_c'"
    ) == [("idx_author_name_c",)]
    assert db.records()[1][4] == "ddl-no-transaction"


REBUILD = """\
-- ferro: foreign-keys-off
CREATE TABLE "author_new" ("id" INTEGER PRIMARY KEY, "name" TEXT NOT NULL, "status" TEXT NOT NULL);
INSERT INTO "author_new" SELECT "id", "name", "status" FROM "author";
DROP TABLE "author";
ALTER TABLE "author_new" RENAME TO "author";
"""


@pytest.mark.asyncio
async def test_a_foreign_keys_off_step_rebuilds_a_referenced_table_and_restores_the_pragma(
    project, pkg, db
):
    if db.backend != "sqlite":
        pytest.skip("foreign-keys-off is SQLite-only")
    configure(project, pkg, "sqlite")
    write_models(project, pkg, LIBRARY)
    new("library")
    settings, database = settings_and_database()
    assert (await runner.up(settings, database, url=db.url)).refusal is None
    db.execute("INSERT INTO author (id, name, status) VALUES (1, 'a', 'draft')")
    db.execute(
        "INSERT INTO post (id, title, kind, author_id) VALUES (1, 't', 'note', 1)"
    )
    sql_step(project, "rebuild_author", REBUILD)

    report = await runner.up(settings, database, url=db.url)

    assert report.refusal is None
    assert [a.migration for a in report.applied] == ["0002_rebuild_author"]
    await ferro.connect(db.url, name="fresh")
    row = await ferro.raw.fetch_one("PRAGMA foreign_keys", using="fresh")
    assert row is not None and list(row.values()) == [1]
    assert db.rows("SELECT id FROM post") == [(1,)]


@pytest.mark.asyncio
async def test_a_foreign_keys_off_step_that_breaks_a_reference_fails_with_the_rows(
    project, pkg, db
):
    if db.backend != "sqlite":
        pytest.skip("foreign-keys-off is SQLite-only")
    configure(project, pkg, "sqlite")
    write_models(project, pkg, LIBRARY)
    new("library")
    settings, database = settings_and_database()
    assert (await runner.up(settings, database, url=db.url)).refusal is None
    db.execute("INSERT INTO author (id, name, status) VALUES (1, 'a', 'draft')")
    db.execute(
        "INSERT INTO post (id, title, kind, author_id) VALUES (1, 't', 'note', 1)"
    )
    sql_step(project, "orphan", '-- ferro: foreign-keys-off\nDELETE FROM "author";\n')

    report = await runner.up(settings, database, url=db.url)

    assert report.refusal is not None
    assert "foreign_key_check" in report.refusal
    assert 'table "post" rowid 1 references "author"' in report.refusal
    assert db.rows("SELECT id FROM author") == [(1,)]
    assert db.records()[1][9] is None


# -- refusals before anything runs ---------------------------------------------------


@pytest.mark.asyncio
async def test_up_over_tables_auto_migrate_built_names_baseline_and_creates_nothing(
    project, pkg, db
):
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    settings, database = settings_and_database()
    database.import_models()
    await ferro.connect(db.url, auto_migrate=True)
    ferro.reset_engine()

    report = await runner.up(settings, database, url=db.url)

    assert report.refusal == (
        'This database has no ferro migration records, but table "author" from 0001 '
        'already exists. If it was built by auto-migrate or Alembic, run "ferro migrate '
        'baseline 0001". up will not run 0001 over it.'
    )
    assert report.applied == []
    assert "_ferro_migrations" not in db.tables()


def _apply(project, pkg, db, *names: str) -> None:
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    for name in names:
        sql_step(project, name, f'CREATE TABLE "{name}" ("id" integer);\n')
    assert run("migrate", "up", "--url", db.url) == 0


def _refused_by_up_and_status(db, capsys, expected: str) -> None:
    capsys.readouterr()
    assert run("migrate", "up", "--url", db.url) == 1
    assert capsys.readouterr().err.rstrip("\n") == expected
    assert run("migrate", "status", "--url", db.url) == 4
    assert expected in capsys.readouterr().out


def test_an_applied_file_edited_on_disk_is_refused_naming_rerecord(
    project, pkg, db, capsys
):
    _apply(project, pkg, db)
    up_file = migrations(project) / f"0001_create_author/01_schema.up.{db.backend}.sql"
    applied = sha384(up_file)
    up_file.write_bytes(up_file.read_bytes() + b"\n")
    finished_at = db.rows("SELECT finished_at FROM _ferro_migrations")[0][0]

    _refused_by_up_and_status(
        db,
        capsys,
        f"ferro migrate: 0001_create_author/{up_file.name} was edited after it was "
        f"applied to this database.\n"
        f"  applied   sha384:{applied}  ({short_time(finished_at)})\n"
        f"  on disk   sha384:{sha384(up_file)}\n"
        f"An applied step is never run again. Restore the file, or accept a deliberate "
        f"edit with\n`ferro migrate rerecord 0001:01`. Nothing was applied.",
    )


def test_an_edited_snapshot_is_refused(project, pkg, db, capsys):
    _apply(project, pkg, db)
    snapshot = migrations(project) / "0001_create_author/ir.json"
    applied = sha384(snapshot)
    snapshot.write_bytes(snapshot.read_bytes() + b"\n")

    _refused_by_up_and_status(
        db,
        capsys,
        f"ferro migrate: 0001_create_author/ir.json is not the snapshot this database "
        f"applied 0001_create_author under.\n"
        f"  applied   sha384:{applied}\n"
        f"  on disk   sha384:{sha384(snapshot)}\n"
        f"A snapshot is never edited: later migrations and historical models are built "
        f"from it.\nRestore the file. Nothing was applied.",
    )


def test_a_broken_chain_is_refused_with_both_checksums(project, pkg, db, capsys):
    _apply(project, pkg, db, "second")
    first = migrations(project) / "0001_create_author/ir.json"
    expected = sha384(first)
    first.write_bytes(first.read_bytes() + b"\n")

    _refused_by_up_and_status(
        db,
        capsys,
        f"ferro migrate: the migration chain is broken at 0002_second.\n"
        f"  0002_second/ir.json expects parent   sha384:{expected}\n"
        f"  0001_create_author/ir.json is        sha384:{sha384(first)}\n"
        f"0001_create_author's snapshot changed after 0002_second was generated. "
        f"Restore it, or\nregenerate 0002_second. Nothing was applied.",
    )


def test_a_pending_migration_below_an_applied_one_is_out_of_order(
    project, pkg, db, capsys
):
    _apply(project, pkg, db, "second", "third")
    db.execute("DELETE FROM _ferro_migrations WHERE migration = 2")

    _refused_by_up_and_status(
        db,
        capsys,
        "ferro migrate: 0002_second is pending, but 0003_third is already applied to "
        "this database.\nMigrations apply in order, with no override. Regenerate "
        "0002_second at the head\n(it becomes 0004). Nothing was applied.",
    )


def test_a_database_ahead_of_the_directory_is_refused_unless_allowed(
    project, pkg, db, capsys
):
    _apply(project, pkg, db, "second", "third")
    shutil.rmtree(migrations(project) / "0003_third")
    expected = (
        "ferro migrate: this database has applied 0003_third, which is not in "
        "migrations/.\nThe directory is behind the database: check out the branch "
        "that holds it. Nothing was applied."
    )
    settings, database = settings_and_database()

    _refused_by_up_and_status(db, capsys, expected)
    report = asyncio.run(runner.status(settings, database, url=db.url))
    assert report.exit_code == 4
    assert [m.state for m in report.migrations] == ["installed", "installed"]
    assert report.ahead == ["0003_third"]

    allowed = asyncio.run(runner.up(settings, database, url=db.url, allow_ahead=True))
    assert allowed.refusal is None and allowed.applied == []
    assert allowed.ahead == ["0003_third"]


def test_a_newer_tracking_format_is_refused_naming_both_ferros(
    project, pkg, db, capsys
):
    _apply(project, pkg, db)
    db.execute("UPDATE _ferro_migrations_format SET format = 99")
    db.execute("UPDATE _ferro_migrations SET ferro_version = '0.99.0'")
    from importlib.metadata import version

    _refused_by_up_and_status(
        db,
        capsys,
        f"ferro migrate: this database's tracking table is format 99; this ferro "
        f"({version('ferro-orm')}) understands format 1.\nIt was last migrated by ferro "
        f"0.99.0. Upgrade ferro to run migrations against it.",
    )


def test_a_reverting_record_refuses_up(project, pkg, db, capsys):
    _apply(project, pkg, db)
    truth = "TRUE" if db.backend == "postgres" else "1"
    db.execute(f"UPDATE _ferro_migrations SET reverting = {truth}")
    capsys.readouterr()

    assert run("migrate", "up", "--url", db.url) == 1
    assert "ferro migrate down" in capsys.readouterr().err
