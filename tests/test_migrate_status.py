# ruff: noqa: F811 - pytest fixtures imported from a sibling module are redefined as arguments
"""``ferro migrate status`` (#519): where a database stands, read-only.

``status`` takes no lock and creates nothing; it prints one line per fully
applied or fully pending migration, expands the steps of any migration
that needs attention (or every one with ``--steps``), prints the same as a
document with ``--json``, and exits 3 when anything is pending, 4 when
anything needs attention.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

import ferro
from ferro import _core
from ferro.cli.output import applied_at_short
from ferro.migrations.report import StatusReport
from tests.test_migrate_new import (  # noqa: F401 - fixtures
    AUTHOR,
    pkg,
    project,
    run,
    write_models,
)
from tests.test_migrate_baseline import auto_migrated
from tests.test_migrate_up import (  # noqa: F401 - fixtures
    configure,
    db,
    migrations,
    new,
    sha384,
    sql_step,
)

pytestmark = [
    pytest.mark.usefixtures("isolated_imports", "clean_registry"),
    pytest.mark.backend_matrix,
]


def table(db) -> str:
    schema = "main" if db.backend == "sqlite" else db.schema
    return f"{schema}._ferro_migrations"


def applied_at(capsys, db) -> list[str]:
    """Each migration's ``applied_at`` as ``status`` shows it, read from
    ``status --json``."""
    document = status_document(capsys, db)
    return [applied_at_short(m["applied_at"]) for m in document["migrations"]]


def test_before_any_run_everything_is_pending_and_nothing_is_created(
    project, pkg, db, capsys
):
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    sql_step(project, "second", 'CREATE TABLE "second" ("id" integer);\n')
    capsys.readouterr()

    assert run("migrate", "status", "--url", db.url) == 3

    assert capsys.readouterr().out == (
        f"{db.line}\n"
        f"-- 2 pending  {table(db)}\n"
        "  -- 0001_create_author  pending\n"
        "  -- 0002_second         pending\n"
    )
    assert "_ferro_migrations" not in db.tables()
    assert "_ferro_migrations_format" not in db.tables()


def test_after_up_everything_is_installed_and_steps_expand_on_request(
    project, pkg, db, capsys
):
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    sql_step(project, "second", 'CREATE TABLE "second" ("id" integer);\n')
    assert run("migrate", "up", "--url", db.url) == 0
    first, second = applied_at(capsys, db)

    assert run("migrate", "status", "--url", db.url) == 0
    assert capsys.readouterr().out == (
        f"{db.line}\n"
        f"ok 2 applied  {table(db)}\n"
        f"  ok 0001_create_author  applied  {first}\n"
        f"  ok 0002_second         applied  {second}\n"
    )

    # Every step's class after its flags (ADR-0054): the generated table
    # creation is expand, the hand-written step without `-- ferro: expand`
    # contract.
    assert run("migrate", "status", "--steps", "--url", db.url) == 0
    up_file = f"01_schema.up.{db.backend}.sql"
    assert capsys.readouterr().out == (
        f"{db.line}\n"
        f"ok 2 applied  {table(db)}\n"
        f"  ok 0001_create_author  applied  {first}\n"
        f"      {up_file}  applied  expand\n"
        f"  ok 0002_second         applied  {second}\n"
        "      01_second.up.sql  applied  contract\n"
    )

    assert run("migrate", "status", "--json", "--url", db.url) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["database"] == "default"
    assert document["dialect"] == db.backend
    assert document["pending"] is False and document["needs_attention"] is False
    assert [m["name"] for m in document["migrations"]] == [
        "0001_create_author",
        "0002_second",
    ]
    assert document["migrations"][0]["state"] == "applied"
    assert document["migrations"][0]["steps"][0]["state"] == "applied"
    assert [m["steps"][0]["step_class"] for m in document["migrations"]] == [
        "expand",
        "contract",
    ]


def test_a_part_applied_migration_expands_and_an_interrupted_step_needs_attention(
    project, pkg, db, capsys
):
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    assert run("migrate", "up", "--url", db.url) == 0
    # A started record with no error and no live run: the process was killed.
    db.execute(
        "UPDATE _ferro_migrations SET finished_at = NULL, error = NULL, failed_at = NULL"
    )
    capsys.readouterr()

    assert run("migrate", "status", "--url", db.url) == 4
    up_file = f"01_schema.up.{db.backend}.sql"
    assert capsys.readouterr().out == (
        f"{db.line}\n"
        f"!! 1 interrupted · 1 pending  {table(db)}\n"
        "  ~~ 0001_create_author  partial, 0 of 1 steps\n"
        f"      {up_file}  interrupted\n"
    )


def test_an_edited_applied_step_shows_both_checksums(project, pkg, db, capsys):
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    assert run("migrate", "up", "--url", db.url) == 0
    up_file = migrations(project) / f"0001_create_author/01_schema.up.{db.backend}.sql"
    applied = sha384(up_file)
    up_file.write_bytes(up_file.read_bytes() + b"\n")
    capsys.readouterr()

    assert run("migrate", "status", "--url", db.url) == 4
    out = capsys.readouterr().out
    assert "  xx 0001_create_author  applied (different checksum)  " in out
    assert (
        f"      {up_file.name}  applied (different checksum)\n"
        f"        applied  sha384:{applied}\n"
        f"        on disk  sha384:{sha384(up_file)}\n"
    ) in out


def test_steps_print_their_class_beside_their_flags(project, pkg, db, capsys):
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    write_models(
        project, pkg, AUTHOR.replace("    status: Status = Status.DRAFT\n", "")
    )
    new("drop_status")
    capsys.readouterr()

    # Pending steps show the class a run would record.
    assert run("migrate", "status", "--steps", "--url", db.url) == 3
    lines = [line.split() for line in capsys.readouterr().out.splitlines()]
    assert [f"01_schema.up.{db.backend}.sql", "pending", "expand"] in lines
    assert [
        f"01_schema.up.{db.backend}.sql",
        "pending",
        "[destructive]",
        "contract",
    ] in lines
    # Without --steps no class is printed.
    assert run("migrate", "status", "--url", db.url) == 3
    out = capsys.readouterr().out
    assert "expand" not in out and "contract" not in out


def test_a_baselined_step_says_so(project, pkg, db, capsys):
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    assert run("migrate", "up", "--url", db.url) == 0
    db.execute("UPDATE _ferro_migrations SET origin = 'baseline'")
    capsys.readouterr()

    (first,) = applied_at(capsys, db)
    assert run("migrate", "status", "--url", db.url) == 0
    assert (
        f"  ok 0001_create_author  applied (baseline)  {first}\n"
        in capsys.readouterr().out
    )


# -- where the database stands -----------------------------------------------------------


def stands(db) -> StatusReport:
    """``ferro.migrations.status()`` on a connection of its own."""

    async def read() -> StatusReport:
        await ferro.connect(db.url, name="stands")
        try:
            return await ferro.migrations.status(using="stands")
        finally:
            await _core._disconnect("stands")

    return asyncio.run(read())


def test_a_database_at_head_has_its_last_migration_applied_and_nothing_else(
    project, pkg, db, capsys
):
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    sql_step(project, "second", 'CREATE TABLE "second" ("id" integer);\n')
    assert run("migrate", "up", "--url", db.url) == 0

    report = stands(db)

    assert report.head_applied is not None
    assert report.head_applied.name == "0002_second"
    assert report.unfinished is None
    assert report.pending is False


def test_a_database_with_an_unfinished_step_names_it_and_stands_below_it(
    project, pkg, db, capsys
):
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    sql_step(project, "second", 'CREATE TABLE "second" ("id" integer);\n')
    assert run("migrate", "up", "--url", db.url) == 0
    # The process was killed inside 0002's step: started, never finished.
    db.execute(
        "UPDATE _ferro_migrations SET finished_at = NULL, error = NULL, "
        "failed_at = NULL WHERE migration = 2"
    )

    report = stands(db)

    assert report.head_applied is not None
    assert report.head_applied.name == "0001_create_author"
    assert report.unfinished is not None
    assert report.unfinished.name == "0002_second"
    assert report.unfinished.unfinished_step is not None
    assert report.unfinished.unfinished_step.state == "interrupted"
    assert report.pending is True


def test_a_database_with_a_pending_migration_stands_at_the_one_before(
    project, pkg, db, capsys
):
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    assert run("migrate", "up", "--url", db.url) == 0
    sql_step(project, "second", 'CREATE TABLE "second" ("id" integer);\n')

    report = stands(db)

    assert report.head_applied is not None
    assert report.head_applied.name == "0001_create_author"
    assert report.unfinished is None
    assert report.pending is True


STEP_STATE = Path(__file__).parents[1] / "crates/ferro-migrate/src/run_plan.rs"


def step_state_words() -> list[str]:
    """Every ``StepState`` variant as the core serializes it (snake_case)."""
    body = STEP_STATE.read_text().split("pub enum StepState {", 1)[1].split("}", 1)[0]
    variants = re.findall(r"^    ([A-Z][A-Za-z]+),", body, flags=re.MULTILINE)
    return [re.sub(r"(?<!^)([A-Z])", r"_\1", v).lower() for v in variants]


def one_step(word: str) -> dict:
    return {
        "step": 1,
        "file": "01_schema.up.sql",
        "state": word,
        "error": None,
        "applied_checksum": None,
        "on_disk_checksum": None,
        "flags": [],
        "class": "contract",
    }


def test_every_core_step_state_is_read_into_where_the_database_stands():
    """A ``StepState`` the core adds without a reading here fails, rather
    than leaving drift, the harness and ``status`` to disagree about it."""
    words = step_state_words()
    applied = {"applied", "applied_different_checksum", "applied_baseline"}
    unfinished = {"running", "failed", "interrupted", "reverting"}
    assert set(words) == applied | unfinished | {"pending"}
    for word in words:
        report = StatusReport.from_core(
            {
                "migrations": [
                    {"number": 1, "name": "0001_a", "steps": [one_step("applied")]},
                    {"number": 2, "name": "0002_b", "steps": [one_step(word)]},
                ],
                "ahead": [],
                "refusal": None,
                "refusal_needs_attention": False,
            },
            database="default",
            url="sqlite:app.db",
            dialect="sqlite",
            table="main._ferro_migrations",
        )
        head = report.head_applied
        assert head is not None
        assert head.name == ("0002_b" if word in applied else "0001_a"), word
        assert (report.unfinished is not None) == (word in unfinished), word
        assert report.pending == (word not in applied), word


# -- when each step and migration was applied (#650) -----------------------------------


def stored_finished(db) -> dict[tuple[int, int], str | None]:
    """The tracking table's ``finished_at`` per ``(migration, step)`` in the
    form the runner writes it (SQLite keeps that text; psycopg hands Postgres's
    ``timestamptz`` back as a ``datetime`` of the same instant)."""
    return {
        (migration, step): (
            finished.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            if isinstance(finished, datetime)
            else finished
        )
        for migration, step, finished in db.rows(
            "SELECT migration, step, finished_at FROM _ferro_migrations"
        )
    }


def status_document(capsys, db) -> dict:
    capsys.readouterr()
    run("migrate", "status", "--json", "--url", db.url)
    return json.loads(capsys.readouterr().out)


def test_after_up_every_step_and_migration_carries_the_stored_time(
    project, pkg, db, capsys
):
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    sql_step(project, "second", 'CREATE TABLE "second" ("id" integer);\n')
    assert run("migrate", "up", "--url", db.url) == 0

    stored = stored_finished(db)
    assert all(stored.values())
    document = status_document(capsys, db)
    for migration in document["migrations"]:
        for step in migration["steps"]:
            assert step["finished_at"] == stored[(migration["number"], step["step"])]
        assert migration["applied_at"] == migration["steps"][-1]["finished_at"]
        assert re.fullmatch(
            r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z", migration["applied_at"]
        )
    assert list(document["migrations"][0]) == [
        "number",
        "name",
        "state",
        "applied_at",
        "steps",
    ]


def test_after_baseline_applied_at_is_the_baseline_time(project, pkg, db, capsys):
    auto_migrated(project, pkg, db)
    assert run("migrate", "baseline", "--url", db.url) == 0

    stored = stored_finished(db)
    document = status_document(capsys, db)
    assert len(document["migrations"]) == 2
    for migration in document["migrations"]:
        assert migration["state"] == "applied (baseline)"
        assert migration["applied_at"] == stored[(migration["number"], 1)]
        assert migration["applied_at"] is not None


def test_nothing_applied_means_no_times(project, pkg, db, capsys):
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    sql_step(project, "second", 'CREATE TABLE "second" ("id" integer);\n')

    document = status_document(capsys, db)
    for migration in document["migrations"]:
        assert migration["applied_at"] is None
        assert [s["finished_at"] for s in migration["steps"]] == [None]


def test_a_failed_second_step_keeps_the_first_steps_time(project, pkg, db, capsys):
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    (folder,) = (project / "migrations").glob("0001_*")
    (folder / "02_two_b.up.sql").write_text('INSERT INTO "missing" VALUES (1);\n')
    (folder / "02_two_b.down.sql").write_text("SELECT 1;\n")
    assert run("migrate", "up", "--url", db.url) == 1

    stored = stored_finished(db)
    document = status_document(capsys, db)
    (migration,) = document["migrations"]
    first, second = migration["steps"]
    assert second["state"] == "failed"
    assert first["finished_at"] == stored[(1, 1)] is not None
    assert second["finished_at"] is None
    assert migration["applied_at"] == first["finished_at"]
    assert migration["state"].startswith("partial")


def test_the_library_report_exposes_the_same_fields(project, pkg, db):
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    assert run("migrate", "up", "--url", db.url) == 0

    migration = stands(db).migrations[0]
    assert migration.steps[0].finished_at == migration.applied_at is not None
