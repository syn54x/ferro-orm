# ruff: noqa: F811 - pytest fixtures imported from a sibling module are redefined as arguments
"""``ferro migrate squash`` (#669): a run of migrations becomes one.

A feature branch wrote ``0002_add_tmp``, ``0003_add_bio`` (with a filled-in
data step and a hand-written SQL step) and ``0004_drop_tmp``.
``ferro migrate squash epic --from 0002`` replaces the three with
``0002_epic``: planned once by the one planner from ``0001``'s snapshot to
the run's composed target (ADR-0056), the run's data steps and hand-written
SQL steps carried byte for byte, and the three directories deleted. It reads
snapshots and the directory only: the models are never imported, no
database is opened.

Every test builds a real project under ``tmp_path`` and drives the CLI
in-process, the way ``tests/test_migrate_new.py`` and
``tests/test_migrate_replace.py`` do.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import warnings
from pathlib import Path

import pytest

from tests.test_migrate_new import (  # noqa: F401 - fixtures
    AUTHOR,
    DIRECTORY_LINE,
    pkg,
    project,
    run,
    statements,
    words,
    write_config,
    write_models,
)

pytestmark = pytest.mark.usefixtures(
    "isolated_imports", "clean_registry", "no_bytecode"
)


@pytest.fixture
def no_bytecode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Write no ``.pyc``: a rewritten ``models.py`` of the same size inside
    the same second (``bio`` for ``tmp``) would otherwise import the stale
    one."""
    monkeypatch.setattr(sys, "dont_write_bytecode", True)


TMP = AUTHOR + "    tmp: str | None = None\n"
BIO = AUTHOR + "    bio: str | None = None\n"
TMP_BIO = TMP + "    bio: str | None = None\n"

FILLED = '''"""Copy every author's name into bio."""

from ferro.migrations import atomic


@atomic
async def up(ctx):
    await ctx.execute("UPDATE author SET bio = name")


@atomic
async def down(ctx):
    await ctx.execute("UPDATE author SET bio = NULL")
'''

FIX_UP = "-- ferro: expand\nUPDATE author SET bio = '' WHERE bio IS NULL;\n"
FIX_DOWN = "SELECT 1;\n"


def migrations(project: Path) -> Path:
    return project / "migrations"


def dirs(project: Path) -> list[str]:
    return sorted(
        p.name
        for p in migrations(project).iterdir()
        if p.is_dir() and not p.name.startswith((".", "_"))
    )


def hidden_entries(project: Path) -> list[str]:
    return sorted(
        p.name for p in migrations(project).iterdir() if p.name.startswith(".")
    )


def sha384(path: Path) -> str:
    return hashlib.sha384(path.read_bytes()).hexdigest()


def snapshot(project: Path, name: str) -> dict:
    return json.loads((migrations(project) / name / "ir.json").read_text())


def tree(project: Path) -> dict[str, bytes]:
    root = migrations(project)
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def files(directory: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(directory.iterdir())}


def ddl(directory: Path) -> dict[str, bytes]:
    """The generated DDL files of a migration: one rendering per dialect."""
    return {
        name: body
        for name, body in files(directory).items()
        if name.endswith((".postgres.sql", ".sqlite.sql"))
    }


def new(*argv: str) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        assert run("migrate", "new", *argv) == 0


def squash(*argv: str) -> int:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return run("migrate", "squash", *argv)


def feature_branch(project: Path, pkg: str) -> None:
    """``0001_create_author``; ``0002_add_tmp`` adds ``tmp``;
    ``0003_add_bio`` adds ``bio`` with a ``--sql-step fix_rows`` and a
    ``--data-step Author``, both filled in; ``0004_drop_tmp`` drops ``tmp``.
    The models end at ``BIO``."""
    write_config(project, pkg)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    write_models(project, pkg, TMP)
    new("add_tmp")
    write_models(project, pkg, TMP_BIO)
    new("add_bio", "--sql-step", "fix_rows", "--data-step", "Author")
    added = migrations(project) / "0003_add_bio"
    [data] = added.glob("*_backfill_author.py")
    data.write_text(FILLED)
    [up] = added.glob("*_fix_rows.up.sql")
    up.write_text(FIX_UP)
    [down] = added.glob("*_fix_rows.down.sql")
    down.write_text(FIX_DOWN)
    write_models(project, pkg, BIO)
    new("drop_tmp")


def from_scratch(project: Path, pkg: str, tmp_path: Path, keep: list[str]) -> Path:
    """The migration ``new epic`` writes from the current models on a copy of
    the project holding only ``keep``."""
    copy = tmp_path / "scratch"
    shutil.copytree(project, copy)
    for name in dirs(copy):
        if name not in keep:
            shutil.rmtree(migrations(copy) / name)
    here = Path.cwd()
    try:
        import os

        os.chdir(copy)
        new("epic")
    finally:
        os.chdir(here)
    [written] = [d for d in dirs(copy) if d not in keep]
    return migrations(copy) / written


# -- the happy paths ----------------------------------------------------------------


def test_a_run_becomes_the_migration_new_would_have_written_carrying_its_steps(
    project, pkg, tmp_path, capsys
):
    feature_branch(project, pkg)
    expected = from_scratch(project, pkg, tmp_path, ["0001_create_author"])
    old = migrations(project) / "0003_add_bio"
    [data] = old.glob("*_backfill_author.py")
    [sql_up] = old.glob("*_fix_rows.up.sql")
    stored = {
        name: sha384(migrations(project) / name / "ir.json")
        for name in ("0002_add_tmp", "0003_add_bio", "0004_drop_tmp")
    }
    steps = {
        "0002_add_tmp": [1],
        "0003_add_bio": sorted(
            {int(p.name[:2]) for p in old.iterdir() if p.name != "ir.json"}
        ),
        "0004_drop_tmp": [1],
    }
    capsys.readouterr()

    assert squash("epic", "--from", "0002") == 0

    assert dirs(project) == ["0001_create_author", "0002_epic"]
    assert hidden_entries(project) == [".gitattributes"]
    squashed = migrations(project) / "0002_epic"
    assert ddl(squashed) == ddl(expected)
    names = sorted(files(squashed))
    [fix_up] = [n for n in names if n.endswith("_fix_rows.up.sql")]
    [fix_down] = [n for n in names if n.endswith("_fix_rows.down.sql")]
    [carried] = [n for n in names if n.endswith("_backfill_author.py")]
    assert (squashed / fix_up).read_text() == FIX_UP
    assert (squashed / fix_down).read_text() == FIX_DOWN
    assert (squashed / carried).read_text() == FILLED
    assert int(fix_up[:2]) < int(carried[:2])  # chain order, as new places them

    ir = snapshot(project, "0002_epic")
    assert ir["ir_version"] == 3
    assert ir["parent_checksum"] == sha384(
        migrations(project) / "0001_create_author/ir.json"
    )
    assert ir["replaces"] == [
        {
            "migration": name,
            "snapshot_checksum": stored[name],
            "steps": steps[name],
            "replaces": [],
        }
        for name in ("0002_add_tmp", "0003_add_bio", "0004_drop_tmp")
    ]
    assert ir["payload"] == json.loads((expected / "ir.json").read_text())["payload"]

    out = capsys.readouterr().out
    lines = words(out)
    assert out.splitlines()[:2] == [
        DIRECTORY_LINE,
        "ok Wrote migrations/0002_epic/  "
        "(replaces 0002_add_tmp … 0004_drop_tmp, now deleted)",
    ]
    assert "01_schema.up.postgres.sql schema step, up (postgres) regenerated" in lines
    assert (
        f"{fix_up} SQL step, up carried, review it (was 0003_add_bio/{sql_up.name})"
    ) in lines, out
    assert (
        f"{carried} data step carried, review it (was 0003_add_bio/{data.name})"
    ) in lines, out
    assert (
        "ir.json snapshot 0004_drop_tmp's, chained from 0001_create_author, "
        "replaces 0002_add_tmp…0004_drop_tmp"
    ) in lines, out

    assert run("migrate", "check") == 0


def test_a_rename_whose_hint_a_later_migration_deleted_squashes_to_a_rename(
    project, pkg
):
    write_config(project, pkg)
    write_models(project, pkg, BIO)
    new("create_author")
    renamed = AUTHOR + (
        "    blurb: Annotated[str | None, FerroField(renamed_from='bio')] = None\n"
    )
    write_models(project, pkg, renamed)
    new("rename_bio")
    blurb = AUTHOR + "    blurb: str | None = None\n"
    write_models(project, pkg, blurb + "    rank: int | None = None\n")
    new("add_rank")  # the hint is gone from this snapshot

    assert squash("epic", "--from", "0002") == 0

    for dialect in ("postgres", "sqlite"):
        up = (migrations(project) / f"0002_epic/01_schema.up.{dialect}.sql").read_text()
        assert 'RENAME COLUMN "bio" TO "blurb"' in up, up
        assert "DROP COLUMN" not in up, up
    assert run("migrate", "check") == 0


def test_a_run_from_the_first_migration_has_no_parent(project, pkg):
    feature_branch(project, pkg)

    assert squash("all", "--from", "0001") == 0

    assert dirs(project) == ["0001_all"]
    ir = snapshot(project, "0001_all")
    assert ir["parent_checksum"] is None
    assert [entry["migration"] for entry in ir["replaces"]] == [
        "0001_create_author",
        "0002_add_tmp",
        "0003_add_bio",
        "0004_drop_tmp",
    ]
    assert run("migrate", "check") == 0


def test_a_run_of_one_is_new_replace_of_it_but_the_witness(project, pkg, tmp_path):
    feature_branch(project, pkg)
    copy = tmp_path / "copy"
    shutil.copytree(project, copy)

    assert squash("drop_tmp", "--from", "0004_drop_tmp") == 0

    import os

    here = Path.cwd()
    try:
        os.chdir(copy)
        new("drop_tmp", "--replace", "0004_drop_tmp")
    finally:
        os.chdir(here)
    squashed = files(migrations(project) / "0004_drop_tmp")
    replaced = files(migrations(copy) / "0004_drop_tmp")
    ir = json.loads(squashed.pop("ir.json"))
    assert squashed == {k: v for k, v in replaced.items() if k != "ir.json"}
    witness = ir.pop("replaces")
    assert [entry["migration"] for entry in witness] == ["0004_drop_tmp"]
    assert ir == json.loads(replaced["ir.json"])


def test_squash_composes_with_the_options_new_takes(project, pkg, capsys):
    write_config(project, pkg)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    write_models(project, pkg, TMP)
    new("add_tmp")
    write_models(project, pkg, TMP + "    slug: str\n")
    new("add_slug")
    capsys.readouterr()

    # --no-backfill puts a guard in the generated backfill's place, as on
    # `new`; the run's own backfill step is carried beside it.
    assert squash("epic", "--from", "0002", "--no-backfill", "author.slug") == 0

    lines = words(capsys.readouterr().out)
    assert "02_guard_author.py guard regenerated" in lines, lines
    assert (
        "04_backfill_author.py data step carried, review it (was "
        "0003_add_slug/02_backfill_author.py)"
    ) in lines, lines
    assert "05_contract.up.postgres.sql schema step, up (postgres) regenerated" in lines


def test_squash_with_data_step_sql_step_and_data_only(project, pkg, capsys):
    write_config(project, pkg)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    write_models(project, pkg, TMP)
    new("add_tmp")
    write_models(project, pkg, TMP_BIO)
    new("add_bio")
    capsys.readouterr()

    assert (
        squash("epic", "--from", "0002", "--data-step", "Author", "--sql-step", "x")
        == 0
    )
    names = sorted(files(migrations(project) / "0002_epic"))
    assert names == [
        "01_schema.down.postgres.sql",
        "01_schema.down.sqlite.sql",
        "01_schema.up.postgres.sql",
        "01_schema.up.sqlite.sql",
        "02_x.down.sql",
        "02_x.up.sql",
        "03_backfill_author.py",
        "ir.json",
    ]
    lines = words(capsys.readouterr().out)
    assert "02_x.up.sql SQL step, up new" in lines, lines
    assert "03_backfill_author.py data step new" in lines, lines

    # --data-only squashes into the data step alone, carrying the one the
    # run holds over the rescaffolded one of its name.
    (migrations(project) / "0002_epic/03_backfill_author.py").write_text(FILLED)
    assert (
        squash("again", "--from", "0002", "--data-step", "Author", "--data-only") == 0
    )
    assert sorted(files(migrations(project) / "0002_again")) == [
        "01_x.down.sql",
        "01_x.up.sql",
        "02_backfill_author.py",
        "ir.json",
    ]
    assert (
        migrations(project) / "0002_again/02_backfill_author.py"
    ).read_text() == FILLED


def test_squashing_a_run_that_holds_a_squash_nests_its_witness(project, pkg):
    feature_branch(project, pkg)
    assert squash("epic", "--from", "0002") == 0
    inner = snapshot(project, "0002_epic")
    inner_sum = sha384(migrations(project) / "0002_epic/ir.json")
    first_sum = sha384(migrations(project) / "0001_create_author/ir.json")

    assert squash("all", "--from", "0001") == 0

    ir = snapshot(project, "0001_all")
    assert ir["parent_checksum"] is None
    [first, epic] = ir["replaces"]
    assert first == {
        "migration": "0001_create_author",
        "snapshot_checksum": first_sum,
        "steps": [1],
        "replaces": [],
    }
    assert epic["migration"] == "0002_epic"
    assert epic["snapshot_checksum"] == inner_sum
    assert epic["replaces"] == inner["replaces"]
    assert run("migrate", "check") == 0


def test_an_unwritten_step_is_carried_and_listed_as_still_needing_writing(
    project, pkg, capsys
):
    write_config(project, pkg)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    write_models(project, pkg, BIO)
    new("add_bio", "--data-step", "Author")  # left holding todo(...)
    write_models(project, pkg, BIO + "    rank: int | None = None\n")
    new("add_rank")
    capsys.readouterr()

    assert squash("epic", "--from", "0002") == 0

    [step] = (migrations(project) / "0002_epic").glob("*_backfill_author.py")
    out = capsys.readouterr().out
    assert (
        f"- {step.name} still needs writing where it says todo(...) (carried from "
        "0002_add_bio/02_backfill_author.py)"
    ) in words(out), out
    assert run("migrate", "check") == 3  # the unwritten step, as before


def test_squash_reads_no_models(project, pkg):
    feature_branch(project, pkg)
    # Models that do not even import: a squash never reads them.
    (project / pkg / "models.py").write_text("raise RuntimeError('imported')\n")

    assert squash("epic", "--from", "0002") == 0


# -- the refusals -------------------------------------------------------------------


def refused(capsys, *argv: str) -> str:
    capsys.readouterr()
    assert squash(*argv) == 1
    return capsys.readouterr().err


def test_a_run_that_does_not_reach_the_head_is_refused_naming_both_fixes(
    project, pkg, capsys
):
    write_config(project, pkg)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    write_models(project, pkg, BIO)
    new("x")
    hidden = migrations(project) / "_0002_x"
    (migrations(project) / "0002_x").rename(hidden)
    write_models(project, pkg, TMP)
    new("y")
    write_models(project, pkg, TMP + "    rank: int | None = None\n")
    new("z")
    hidden.rename(migrations(project) / "0002_x")
    before = tree(project)

    err = refused(capsys, "epic", "--from", "0002_x")

    assert err == (
        "the run from 0002_x does not reach the head: 0003_z chains from 0002_y; "
        "squash from a migration the head descends from (--from 0002_y), or "
        "regenerate 0002_x on top of the head with ferro migrate new <name> "
        "--replace 0002_x. Nothing was written.\n"
    ), err
    assert tree(project) == before


def test_an_empty_squash_is_refused_naming_the_directories_to_delete(
    project, pkg, capsys
):
    write_config(project, pkg)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    write_models(project, pkg, BIO)
    new("add_bio")
    write_models(project, pkg, BIO + "    tmp: str | None = None\n")
    new("add_tmp")
    write_models(project, pkg, BIO)
    new("drop_tmp")
    before = tree(project)
    root = migrations(project).resolve()

    err = refused(capsys, "epic", "--from", "0003")

    assert err == (
        "squash --from 0003: on top of 0002_add_bio, 0003_add_tmp through "
        "0004_drop_tmp change nothing that renders DDL and carry no data step or "
        "hand-written SQL step, so the squashed migration would be empty; delete "
        f"the directories {root / '0003_add_tmp'} and {root / '0004_drop_tmp'} "
        "instead. Nothing was written.\n"
    ), err
    assert tree(project) == before


def test_squash_with_url_is_refused_as_new_refuses_it(project, pkg, capsys):
    feature_branch(project, pkg)
    before = tree(project)

    err = refused(
        capsys, "epic", "--from", "0002", "--url", "postgresql://nobody@127.0.0.1:1/x"
    )

    assert err == (
        "squash reads the models and the migrations directory, never a database; "
        "drop --url\n"
    ), err
    assert tree(project) == before


def test_a_shared_number_and_an_absent_migration_are_refused(project, pkg, capsys):
    write_config(project, pkg)
    write_models(project, pkg, AUTHOR)
    new("create_author")
    write_models(project, pkg, BIO)
    new("x")
    hidden = migrations(project) / "_0002_x"
    (migrations(project) / "0002_x").rename(hidden)
    write_models(project, pkg, TMP)
    new("y")
    hidden.rename(migrations(project) / "0002_x")
    before = tree(project)

    err = refused(capsys, "epic", "--from", "0002")
    assert "0002_x" in err and "0002_y" in err, err

    err = refused(capsys, "epic", "--from", "0009")
    assert "0009" in err and str(migrations(project)) in err, err
    assert tree(project) == before


def test_a_name_readded_after_its_rename_is_refused_with_the_from_to_pass(
    project, pkg, capsys
):
    write_config(project, pkg)
    write_models(project, pkg, BIO)
    new("create_author")
    renamed = AUTHOR + (
        "    blurb: Annotated[str | None, FerroField(renamed_from='bio')] = None\n"
    )
    write_models(project, pkg, renamed)
    new("rename_bio")
    blurb = AUTHOR + "    blurb: str | None = None\n"
    write_models(project, pkg, blurb + "    bio: str | None = None\n")
    new("readd_bio")
    before = tree(project)

    err = refused(capsys, "epic", "--from", "0002")

    assert err.startswith(
        'squash --from 0002: the run renames column author."bio" -> "blurb", then '
        'declares "bio" again from migration #2 of the range'
    ), err
    assert err.endswith("pass --from 0003_readd_bio to end the range before it\n"), err
    assert tree(project) == before


def test_a_name_the_parent_held_is_refused_with_the_from_to_pass(project, pkg, capsys):
    write_config(project, pkg)
    two = AUTHOR + "    bio: str | None = None\n    blurb: str | None = None\n"
    write_models(project, pkg, two)
    new("create_author")
    write_models(project, pkg, BIO)
    new("drop_blurb")  # frees "blurb"
    renamed = AUTHOR + (
        "    blurb: Annotated[str | None, FerroField(renamed_from='bio')] = None\n"
    )
    write_models(project, pkg, renamed)
    new("rename_bio")
    before = tree(project)

    err = refused(capsys, "epic", "--from", "0002")

    assert "start the squash range after migration #1" in err, err
    assert err.endswith(
        "pass --from 0003_rename_bio to start the range after 0002_drop_blurb\n"
    ), err
    assert tree(project) == before


def test_disagreeing_renames_are_refused_with_the_from_to_pass(project, pkg, capsys):
    write_config(project, pkg)
    write_models(project, pkg, BIO)
    new("create_author")
    to_blurb = AUTHOR + (
        "    blurb: Annotated[str | None, FerroField(renamed_from='bio')] = None\n"
    )
    write_models(project, pkg, to_blurb)
    new("rename_bio")
    # A later migration claims bio was renamed to "about": a hint the
    # generator leaves inert (bio is gone), which the composition refuses.
    to_about = AUTHOR + (
        "    blurb: str | None = None\n"
        "    about: Annotated[str | None, FerroField(renamed_from='bio')] = None\n"
    )
    write_models(project, pkg, to_about)
    new("add_about")
    before = tree(project)

    err = refused(capsys, "epic", "--from", "0002")

    assert err.startswith("squash --from 0002: cannot compose the rename hints"), err
    assert err.endswith("pass --from 0003_add_about to end the range before it\n"), err
    assert tree(project) == before


def test_a_snapshot_whose_hint_the_planner_refuses_is_refused(project, pkg, capsys):
    feature_branch(project, pkg)
    # A hand-edited snapshot inside the run (no generator writes this): a
    # column renamed from a name its own model still declares.
    path = migrations(project) / "0003_add_bio/ir.json"
    document = json.loads(path.read_text())
    [author] = document["payload"]["models"]
    [bio] = [c for c in author["columns"] if c["name"] == "bio"]
    bio["renamed_from"] = "name"
    path.write_text(json.dumps(document))
    # Re-link the child so the directory reads (the snapshot's bytes changed).
    child = migrations(project) / "0004_drop_tmp/ir.json"
    linked = json.loads(child.read_text())
    linked["parent_checksum"] = sha384(path)
    child.write_text(json.dumps(linked))
    before = tree(project)

    err = refused(capsys, "epic", "--from", "0002")

    assert "the snapshot of migration #2 of the range" in err, err
    assert err.endswith(
        "pass --from 0004_drop_tmp to start the range after 0003_add_bio\n"
    ), err
    assert tree(project) == before


def test_an_aside_already_present_is_refused_before_anything_moves(
    project, pkg, capsys
):
    feature_branch(project, pkg)
    root = migrations(project)
    (root / ".0002_add_tmp..0004_drop_tmp.0002_epic.squashed").mkdir()
    before = tree(project)

    err = refused(capsys, "epic", "--from", "0002")

    assert ".0002_add_tmp..0004_drop_tmp.0002_epic.squashed" in err, err
    assert tree(project) == before
    assert dirs(project) == [
        "0001_create_author",
        "0002_add_tmp",
        "0003_add_bio",
        "0004_drop_tmp",
    ]


def test_a_write_that_raises_moves_every_original_back(project, pkg, monkeypatch):
    feature_branch(project, pkg)
    before = tree(project)
    from ferro.migrations import generate

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(generate, "write_migration", boom)

    with pytest.raises(OSError, match="disk full"):
        squash("epic", "--from", "0002")

    assert tree(project) == before
    assert hidden_entries(project) == [".gitattributes"]


# -- an interrupted squash ------------------------------------------------------------
#
# A squash moves the originals one by one into one hidden aside, writes the
# replacement and deletes the aside. A kill between two of those leaves the
# aside; every reader refuses it by name with its recovery, and a second
# squash meets it before moving anything.

ASIDE = ".0002_add_tmp..0004_drop_tmp.0002_epic.squashed"


def test_a_squash_killed_before_writing_is_named_by_every_reader(project, pkg, capsys):
    feature_branch(project, pkg)
    root = migrations(project)
    # Killed after moving two originals, before the third.
    (root / ASIDE).mkdir()
    for name in ("0002_add_tmp", "0003_add_bio"):
        (root / name).rename(root / ASIDE / name)
    before = tree(project)
    recovery = (
        f"{ASIDE} holds 0002_add_tmp through 0004_drop_tmp, set aside by an "
        "interrupted `ferro migrate squash epic --from 0002_add_tmp`; 0002_epic was "
        f"never written. Move every directory in {ASIDE} back into the migrations "
        f"directory (beside 0004_drop_tmp, never moved) and delete {ASIDE}, then run "
        "`ferro migrate squash epic --from 0002_add_tmp` again"
    )
    capsys.readouterr()

    assert run("migrate", "check") == 3
    assert capsys.readouterr().err == f"squashed_aside: {recovery}\n"
    for argv in (
        ("migrate", "squash", "epic", "--from", "0001"),
        ("migrate", "new", "more"),
        ("migrate", "new", "x", "--replace", "0001"),
        ("migrate", "up", "--url", f"sqlite:{project / 'app.db'}?mode=rwc"),
    ):
        assert run(*argv) == 1
        assert recovery in capsys.readouterr().err
    assert tree(project) == before

    # The recovery the refusal names.
    for name in ("0002_add_tmp", "0003_add_bio"):
        (root / ASIDE / name).rename(root / name)
    (root / ASIDE).rmdir()
    assert run("migrate", "check") == 0
    assert squash("epic", "--from", "0002_add_tmp") == 0
    assert dirs(project) == ["0001_create_author", "0002_epic"]


def test_a_squash_killed_before_its_cleanup_names_the_aside_to_delete(
    project, pkg, capsys
):
    feature_branch(project, pkg)
    root = migrations(project)
    originals = {name: files(root / name) for name in dirs(project)[1:]}
    assert squash("epic", "--from", "0002") == 0
    # Killed after 0002_epic appeared, before the aside was deleted.
    for name, contents in originals.items():
        (root / ASIDE / name).mkdir(parents=True)
        for file, body in contents.items():
            (root / ASIDE / name / file).write_bytes(body)
    before = tree(project)
    capsys.readouterr()

    assert run("migrate", "check") == 3
    assert capsys.readouterr().err == (
        f"squashed_aside: {ASIDE} holds 0002_add_tmp through 0004_drop_tmp, set "
        "aside by an interrupted `ferro migrate squash epic --from 0002_add_tmp`; "
        f"its replacement 0002_epic is complete. Delete {ASIDE}\n"
    )
    assert squash("again", "--from", "0002") == 1
    assert f"Delete {ASIDE}" in capsys.readouterr().err
    assert tree(project) == before

    shutil.rmtree(root / ASIDE)
    assert run("migrate", "check") == 0
