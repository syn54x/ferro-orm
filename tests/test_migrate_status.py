# ruff: noqa: F811 - pytest fixtures imported from a sibling module are redefined as arguments
"""``ferro migrate status`` (#519): where a database stands, read-only.

``status`` takes no lock and creates nothing; it prints one line per fully
installed or fully pending migration, expands the steps of any migration
that needs attention (or every one with ``--steps``), prints the same as a
document with ``--json``, and exits 3 when anything is pending, 4 when
anything needs attention.
"""

from __future__ import annotations

import json

import pytest

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
    fresh_cli,
    migrations,
    new,
    sha384,
    sql_step,
)

pytestmark = [
    pytest.mark.usefixtures("isolated_imports", "clean_registry", "fresh_cli"),
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
        f"{header(db)}\n\n0001_create_author  installed\n0002_second         installed\n"
    )

    assert run("migrate", "status", "--steps", "--url", db.url) == 0
    up_file = f"01_schema.up.{db.backend}.sql"
    width = len(up_file)
    assert capsys.readouterr().out == (
        f"{header(db)}\n\n"
        f"0001_create_author  installed\n"
        f"  {up_file:<{width}}  installed\n"
        f"0002_second         installed\n"
        f"  {'01_second.up.sql':<{width}}  installed\n"
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
    assert document["migrations"][0]["state"] == "installed"
    assert document["migrations"][0]["steps"][0]["state"] == "installed"


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


def test_an_edited_installed_step_shows_both_checksums(project, pkg, db, capsys):
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
        f"0001_create_author  installed (different checksum)\n"
        f"  {up_file.name}  installed (different checksum)\n"
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
    assert "0001_create_author  installed (baseline)\n" in capsys.readouterr().out
