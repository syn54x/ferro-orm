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
from pathlib import Path

import pytest

import ferro
from ferro import _core
from ferro.migrations.report import StatusReport
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
    migrations,
    new,
    sha384,
    sql_step,
)

pytestmark = [
    pytest.mark.usefixtures("isolated_imports", "clean_registry"),
    pytest.mark.backend_matrix,
]


def header(db) -> str:
    schema = "main" if db.backend == "sqlite" else db.schema
    return f"default ({db.backend}) · {schema}._ferro_migrations"


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
        f"{header(db)}\n\n0001_create_author  pending\n0002_second         pending\n"
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
    capsys.readouterr()

    assert run("migrate", "status", "--url", db.url) == 0
    assert capsys.readouterr().out == (
        f"{header(db)}\n\n0001_create_author  applied\n0002_second         applied\n"
    )

    assert run("migrate", "status", "--steps", "--url", db.url) == 0
    up_file = f"01_schema.up.{db.backend}.sql"
    width = len(up_file)
    assert capsys.readouterr().out == (
        f"{header(db)}\n\n"
        f"0001_create_author  applied\n"
        f"  {up_file:<{width}}  applied\n"
        f"0002_second         applied\n"
        f"  {'01_second.up.sql':<{width}}  applied\n"
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
        f"{header(db)}\n\n0001_create_author  partial, 0 of 1 steps\n"
        f"  {up_file}  interrupted\n"
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
    assert (
        f"0001_create_author  applied (different checksum)\n"
        f"  {up_file.name}  applied (different checksum)\n"
        f"    applied   sha384:{applied}\n"
        f"    on disk   sha384:{sha384(up_file)}\n"
    ) in out


def test_a_baselined_step_says_so(project, pkg, db, capsys):
    configure(project, pkg, db.backend)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    assert run("migrate", "up", "--url", db.url) == 0
    db.execute("UPDATE _ferro_migrations SET origin = 'baseline'")
    capsys.readouterr()

    assert run("migrate", "status", "--url", db.url) == 0
    assert "0001_create_author  applied (baseline)\n" in capsys.readouterr().out


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
            dialect="sqlite",
            table="main._ferro_migrations",
        )
        head = report.head_applied
        assert head is not None
        assert head.name == ("0002_b" if word in applied else "0001_a"), word
        assert (report.unfinished is not None) == (word in unfinished), word
        assert report.pending == (word not in applied), word
