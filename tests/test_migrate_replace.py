# ruff: noqa: F811 - pytest fixtures imported from a sibling module are redefined as arguments
"""``ferro migrate new --replace`` (#629): two branches, one number.

Two branches each generated ``0002``; after the rebase the directory holds
``0002_add_x`` and ``0002_add_y``. ``ferro migrate new add_x --replace
0002_add_x`` regenerates ``add_x`` on top of the newest snapshot below it,
carrying every data step and hand-written SQL step across by name with its
body intact. It never reads a database (ADR-0023).

Every test builds a real project under ``tmp_path`` and drives the CLI
in-process, the way ``tests/test_migrate_new.py`` does; the run tests apply
the old files to the parametrized database first.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import warnings
from pathlib import Path

import pytest

from tests.test_migrate_new import (  # noqa: F401 - fixtures
    AUTHOR,
    pkg,
    project,
    run,
    write_config,
    write_models,
)

pytestmark = pytest.mark.usefixtures("isolated_imports", "clean_registry")

X = AUTHOR + "    bio: str | None = None\n"
Y = AUTHOR + "    nick: str | None = None\n"
XY = AUTHOR + "    bio: str | None = None\n    nick: str | None = None\n"

FILLED = '''"""Backfill author.slug from the name."""


async def up(ctx):
    await ctx.execute("UPDATE author SET slug = lower(name)")


async def down(ctx):
    pass
'''


def migrations(project: Path) -> Path:
    return project / "migrations"


def dirs(project: Path) -> list[str]:
    return sorted(
        p.name
        for p in migrations(project).iterdir()
        if p.is_dir() and not p.name.startswith((".", "_"))
    )


def sha384(path: Path) -> str:
    return hashlib.sha384(path.read_bytes()).hexdigest()


def parent_of(project: Path, name: str) -> str | None:
    return json.loads((migrations(project) / name / "ir.json").read_text())[
        "parent_checksum"
    ]


def tree(project: Path) -> dict[str, bytes]:
    root = migrations(project)
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def new(*argv: str) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        assert run("migrate", "new", *argv) == 0


def aside(project: Path, name: str) -> Path:
    """Move ``name`` out of the directory's sight (``_`` entries are ignored),
    as if another branch held it."""
    hidden = migrations(project) / f"_{name}"
    (migrations(project) / name).rename(hidden)
    return hidden


def back(project: Path, hidden: Path, name: str) -> None:
    hidden.rename(migrations(project) / name)


def two_branches(project: Path, pkg: str, x: str = X, *x_flags: str) -> None:
    """``0001_create_author``, then ``0002_add_x`` (``x``) and ``0002_add_y``
    (``Y``) both generated on top of it, as two branches do; the models end
    up declaring both."""
    write_config(project, pkg)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    write_models(project, pkg, x)
    new("add_x", *x_flags)
    hidden = aside(project, "0002_add_x")
    write_models(project, pkg, Y)
    new("add_y")
    back(project, hidden, "0002_add_x")
    write_models(project, pkg, x.replace(AUTHOR, Y))


# -- the happy paths ----------------------------------------------------------------


def test_same_parent_regenerates_on_the_other_branch_and_takes_the_next_number(
    project, pkg, capsys
):
    two_branches(project, pkg)
    assert run("migrate", "check") == 3  # the duplicate number
    capsys.readouterr()

    assert run("migrate", "new", "add_x", "--replace", "0002_add_x") == 0

    assert dirs(project) == ["0001_create_author", "0002_add_y", "0003_add_x"]
    assert parent_of(project, "0003_add_x") == sha384(
        migrations(project) / "0002_add_y/ir.json"
    )
    up = (migrations(project) / "0003_add_x/01_schema.up.postgres.sql").read_text()
    assert '"bio"' in up and '"nick"' not in up, up
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "migrations/0003_add_x/  (replaces 0002_add_x, now deleted)"
    assert (
        "  01_schema.up.postgres.sql  moved (was 0002_add_x/01_schema.up.postgres.sql)"
        in out
    )
    assert "  ir.json  new snapshot, chained from 0002_add_y" in out

    assert run("migrate", "check") == 0


def test_a_new_parent_is_the_newest_snapshot_below_never_the_stale_one(
    project, pkg, capsys
):
    two_branches(project, pkg)
    hidden = aside(project, "0002_add_x")
    write_models(project, pkg, Y + "    rank: int | None = None\n")
    new("more")
    back(project, hidden, "0002_add_x")
    write_models(project, pkg, XY + "    rank: int | None = None\n")
    stale = parent_of(project, "0002_add_x")
    capsys.readouterr()

    assert run("migrate", "new", "add_x", "--replace", "0002_add_x") == 0

    assert dirs(project) == [
        "0001_create_author",
        "0002_add_y",
        "0003_more",
        "0004_add_x",
    ]
    parent = parent_of(project, "0004_add_x")
    assert parent == sha384(migrations(project) / "0003_more/ir.json") != stale
    assert run("migrate", "check") == 0


def test_a_free_number_is_regenerated_in_place_carrying_a_filled_in_backfill(
    project, pkg, capsys
):
    write_config(project, pkg)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    write_models(project, pkg, AUTHOR + "    slug: str\n")
    new("add_x")
    [backfill] = (migrations(project) / "0002_add_x").glob("*_backfill_author.py")
    backfill.write_text(FILLED)
    # Main merged 0002_add_y meanwhile; the developer moved theirs to 0003 by hand.
    hidden = aside(project, "0002_add_x")
    write_models(project, pkg, Y)
    new("add_y")
    back(project, hidden, "0003_add_x")
    write_models(project, pkg, Y + "    slug: str\n")
    capsys.readouterr()

    assert run("migrate", "new", "add_x", "--replace", "0003_add_x") == 0

    assert dirs(project) == ["0001_create_author", "0002_add_y", "0003_add_x"]
    assert parent_of(project, "0003_add_x") == sha384(
        migrations(project) / "0002_add_y/ir.json"
    )
    [carried] = (migrations(project) / "0003_add_x").glob("*_backfill_author.py")
    assert carried.read_bytes() == FILLED.encode()
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "migrations/0003_add_x/  (regenerated in place)"
    assert (
        f"  {carried.name}  carried, review it (was 0003_add_x/{backfill.name}; it "
        "wins over the regenerated step of the same name)"
    ) in out.splitlines(), out
    assert "  01_expand.up.postgres.sql  kept" in out.splitlines(), out
    assert run("migrate", "check") == 0


def test_a_carried_sql_step_keeps_its_body_and_moves_to_its_new_place(
    project, pkg, capsys
):
    two_branches(project, pkg, X, "--sql-step", "fix_rows")
    old = migrations(project) / "0002_add_x/02_fix_rows.up.sql"
    body = "-- ferro: expand\nUPDATE author SET bio = name;\n"
    old.write_text(body)
    (migrations(project) / "0002_add_x/02_fix_rows.down.sql").write_text("SELECT 1;\n")
    # The developer made bio required while rebasing: a backfill and a
    # contract now come before the SQL step.
    write_models(project, pkg, Y + "    bio: str\n")
    capsys.readouterr()

    assert (
        run("migrate", "new", "add_x", "--replace", "0002_add_x", "--sql-step", "extra")
        == 0
    )

    new_dir = migrations(project) / "0003_add_x"
    [up] = new_dir.glob("*_fix_rows.up.sql")
    [down] = new_dir.glob("*_fix_rows.down.sql")
    assert up.read_text() == body and down.read_text() == "SELECT 1;\n"
    assert int(up.name[:2]) > 2, sorted(p.name for p in new_dir.iterdir())
    [extra] = new_dir.glob("*_extra.up.sql")
    assert int(extra.name[:2]) == int(up.name[:2]) + 1  # beside the carried one
    out = capsys.readouterr().out.splitlines()
    assert f"  {up.name}  carried, review it (was 0002_add_x/02_fix_rows.up.sql)" in out


# -- the target and the refusals ---------------------------------------------------


def test_a_bare_number_one_directory_carries_works(project, pkg):
    write_config(project, pkg)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    write_models(project, pkg, X)
    new("add_x")
    write_models(project, pkg, XY)

    assert run("migrate", "new", "add_x", "--replace", "0002") == 0

    assert dirs(project) == ["0001_create_author", "0002_add_x"]
    assert (
        '"nick"'
        in (migrations(project) / "0002_add_x/01_schema.up.postgres.sql").read_text()
    )


def test_a_shared_bare_number_and_an_unknown_name_are_refused(project, pkg, capsys):
    two_branches(project, pkg)
    before = tree(project)
    capsys.readouterr()

    assert run("migrate", "new", "add_x", "--replace", "0002") != 0
    err = capsys.readouterr().err
    assert "--replace 0002_add_x" in err and "--replace 0002_add_y" in err, err

    assert run("migrate", "new", "add_x", "--replace", "0009_nothing") != 0
    err = capsys.readouterr().err
    assert "migrations" in err and "0009_nothing" in err, err
    assert tree(project) == before


def test_a_migration_a_later_one_chains_from_is_refused(project, pkg, capsys):
    write_config(project, pkg)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    write_models(project, pkg, X)
    new("add_x")
    write_models(project, pkg, XY)
    new("later")
    before = tree(project)
    capsys.readouterr()

    assert run("migrate", "new", "add_x", "--replace", "0002_add_x") != 0

    err = capsys.readouterr().err
    assert "0003_later" in err and "replace the later one first, or delete it" in err
    assert tree(project) == before


def test_an_empty_regeneration_with_nothing_carried_is_refused(project, pkg, capsys):
    two_branches(project, pkg)
    write_models(project, pkg, Y)  # main already holds everything add_x did
    before = tree(project)
    capsys.readouterr()

    assert run("migrate", "new", "add_x", "--replace", "0002_add_x") != 0

    err = capsys.readouterr().err
    assert "delete the directory" in err and "0002_add_x" in err, err
    assert tree(project) == before


def test_replace_with_url_is_refused_and_opens_no_connection(project, pkg, capsys):
    two_branches(project, pkg)
    before = tree(project)
    capsys.readouterr()

    code = run(
        "migrate",
        "new",
        "add_x",
        "--replace",
        "0002_add_x",
        "--url",
        "postgresql://nobody@127.0.0.1:1/nowhere",
    )

    assert code == 1
    assert "never a database; drop --url" in capsys.readouterr().err
    assert tree(project) == before


# -- a database that applied the old files ------------------------------------------


@pytest.mark.backend_matrix
def test_a_database_that_applied_the_replaced_files_reverts_them_then_goes_up(
    project, pkg, db_url, db_backend, tmp_path, capsys
):
    (project / "ferro.toml").write_text(
        f'models = ["{pkg}.models"]\ndialects = ["{db_backend}"]\n'
    )
    write_models(project, pkg, AUTHOR)
    new("create_author")
    write_models(project, pkg, X)
    new("add_x")
    assert run("migrate", "up", "--url", db_url) == 0
    applied = tmp_path / "applied"  # the commit that applied 0002_add_x
    shutil.copytree(migrations(project), applied)

    # A rebase: main's 0002_add_y lands, and add_x is replaced on top of it.
    hidden = aside(project, "0002_add_x")
    write_models(project, pkg, Y)
    new("add_y")
    back(project, hidden, "0002_add_x")
    write_models(project, pkg, XY)
    new("add_x", "--replace", "0002_add_x")
    replaced = tmp_path / "replaced"
    shutil.copytree(migrations(project), replaced)
    capsys.readouterr()

    assert run("migrate", "up", "--url", db_url) != 0
    err = capsys.readouterr().err
    assert (
        "or 0002 was replaced: revert it with the files that were applied (check out "
        "the commit that applied it, `ferro migrate down --to 0001`), then `up`"
    ) in err, err

    # The recipe: check out the commit that applied it, down, check out, up.
    shutil.rmtree(migrations(project))
    shutil.copytree(applied, migrations(project))
    assert run("migrate", "down", "--to", "0001", "--yes", "--url", db_url) == 0
    shutil.rmtree(migrations(project))
    shutil.copytree(replaced, migrations(project))
    assert run("migrate", "up", "--url", db_url) == 0
    capsys.readouterr()
    assert run("migrate", "status", "--json", "--url", db_url) == 0
    document = json.loads(capsys.readouterr().out)
    assert [(m["name"], m["state"]) for m in document["migrations"]] == [
        ("0001_create_author", "applied"),
        ("0002_add_y", "applied"),
        ("0003_add_x", "applied"),
    ]


@pytest.mark.backend_matrix
def test_a_migration_replaced_in_place_meets_the_snapshot_mismatch_naming_it(
    project, pkg, db_url, db_backend, capsys
):
    (project / "ferro.toml").write_text(
        f'models = ["{pkg}.models"]\ndialects = ["{db_backend}"]\n'
    )
    write_models(project, pkg, AUTHOR)
    new("create_author")
    write_models(project, pkg, X)
    new("add_x")
    assert run("migrate", "up", "--url", db_url) == 0
    write_models(project, pkg, XY)
    new("add_x", "--replace", "0002_add_x")
    capsys.readouterr()

    assert run("migrate", "up", "--url", db_url) != 0

    err = capsys.readouterr().err
    assert "0002_add_x/ir.json is not the snapshot this database applied" in err, err
    assert (
        "Restore the file, or 0002 was replaced: revert it with the files that were "
        "applied (check out the commit that applied it, `ferro migrate down --to "
        "0001`), then `up`. Nothing was applied."
    ) in err, err


# -- the guide ----------------------------------------------------------------------

GUIDE = Path(__file__).resolve().parents[1] / "docs/pages/guide/schema/migrations.md"


def _guide_blocks(command: str) -> list[str]:
    """The lines of the guide's "Two branches, one number" text block that
    ``$ <command>`` opens, without the prompt."""
    text = GUIDE.read_text()
    section = text.split("## Two branches, one number\n", 1)[1].split("\n## ", 1)[0]
    block = section.split(f"$ {command}\n", 1)[1].split("```", 1)[0]
    return block.splitlines()


def test_the_guide_quotes_what_check_and_replace_print(project, pkg, capsys):
    # The guide's 0006 / 0007 / 0008 are this project's 0001 / 0002 / 0003.
    def shifted(lines: list[str]) -> list[str]:
        return [
            line.replace("0008", "0003").replace("0007", "0002").replace("0006", "0001")
            for line in lines
        ]

    write_config(project, pkg, '["postgres"]')
    write_models(project, pkg, AUTHOR)
    new("create_author")
    write_models(project, pkg, X)
    new("add_x", "--sql-step", "fix_rows")
    hidden = aside(project, "0002_add_x")
    write_models(project, pkg, Y)
    new("add_y")
    back(project, hidden, "0002_add_x")
    write_models(project, pkg, XY)
    capsys.readouterr()

    assert run("migrate", "check") == 3
    assert capsys.readouterr().err.splitlines() == shifted(
        _guide_blocks("ferro migrate check")
    )

    assert run("migrate", "new", "add_x", "--replace", "0002_add_x") == 0
    assert capsys.readouterr().out.splitlines() == shifted(
        _guide_blocks("ferro migrate new add_x --replace 0007_add_x")
    )
