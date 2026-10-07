# ruff: noqa: F811 - pytest fixtures imported from a sibling module are redefined as arguments
"""Data steps (#530): a Python step over historical models, run atomically.

```text
$ ferro migrate new add_slug --data-step Author
  0002_add_slug/
    01_schema.up.sqlite.sql      ALTER TABLE "author" ADD COLUMN "slug" TEXT
    02_backfill_author.py        @atomic up / down holding todo("write this step")
    ir.json
```

Every test builds a real project under ``tmp_path``, writes migrations with
``ferro migrate new``, edits the step the way a developer would, runs it
against the parametrized database (SQLite and Postgres) and reads the rows
and the tracking table back with a plain driver.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import sys
import textwrap
from pathlib import Path

import pytest

from ferro.migrations import runner
from ferro.migrations.steps import (
    Atomic,
    NothingToReverse,
    NotWrittenError,
    StepRefused,
    load_step,
    todo,
)
from ferro.registry import REGISTRY
from ferro.settings import FerroSettings
from tests.test_migrate_new import (  # noqa: F401 - fixtures
    isolated_imports,
    pkg,
    project,
    run,
    write_models,
)
from tests.test_migrate_down import plan_against, snapshot
from tests.test_migrate_up import configure, db, migrations, new  # noqa: F401

pytestmark = [
    pytest.mark.usefixtures("isolated_imports", "clean_registry"),
    pytest.mark.backend_matrix,
]

MODELS = """
from ferro import Field, ManyToMany


class Tag(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    label: str
    authors: Relation[list["Author"]] = BackRef()


class Author(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    name: str
    tags: Relation[list[Tag]] = ManyToMany(related_name="authors")
"""

WITH_SLUG = MODELS + "    slug: str | None = None\n"

RENAMED = MODELS.replace(
    "    name: str\n",
    '    full_name: str = Field(renamed_from="name")\n'
    "    nickname: str | None = None\n",
)

BACKFILL = """\
from ferro.migrations import atomic, nothing_to_reverse


@atomic
async def up(ctx):
    for author in await ctx.models.Author.where(lambda author: author.slug == None).all():
        author.slug = author.name.lower().replace(" ", "-")
        await author.save()


@nothing_to_reverse("slugs are derived; nothing to put back")
def down(ctx): ...
"""


def sha384(path: Path) -> str:
    return hashlib.sha384(path.read_bytes()).hexdigest()


def applied_with_authors(project: Path, pkg: str, db) -> None:
    """``0001_create_author`` applied, holding two authors."""
    configure(project, pkg, db.backend)
    write_models(project, pkg, MODELS)
    new("create_author")
    assert run("migrate", "up", "--url", db.url) == 0
    db.execute("INSERT INTO author (name) VALUES ('Ann Lee'), ('Bo')")


def slug_step(project: Path, pkg: str, body: str | None) -> Path:
    """``0002_add_slug``: the ``slug`` column and a data step holding ``body``
    (the scaffold, unedited, when ``None``)."""
    write_models(project, pkg, WITH_SLUG)
    new("add_slug", "--data-step", "Author")
    step = migrations(project) / "0002_add_slug" / "02_backfill_author.py"
    if body is not None:
        step.write_text(body)
    return step


def record(db, migration: int, step: int) -> tuple | None:
    rows = [r for r in db.records() if r[:2] == (migration, step)]
    return rows[0] if rows else None


# -- the happy path -----------------------------------------------------------------


def test_an_atomic_step_updates_rows_through_its_historical_model(
    project, pkg, db, capsys
):
    applied_with_authors(project, pkg, db)
    step = slug_step(project, pkg, BACKFILL)
    capsys.readouterr()

    assert run("migrate", "up", "--url", db.url) == 0

    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("0002_add_slug  01_schema           applied (")
    assert out[1].startswith("0002_add_slug  02_backfill_author  applied (")
    assert db.rows("SELECT name, slug FROM author ORDER BY id") == [
        ("Ann Lee", "ann-lee"),
        ("Bo", "bo"),
    ]
    _, _, name, file, kind, checksum, _, origin, _, finished, failed, error = record(
        db, 2, 2
    )
    assert (name, file, kind, origin) == (
        "0002_add_slug",
        "02_backfill_author.py",
        "atomic",
        "run",
    )
    assert checksum == sha384(step)
    assert finished is not None and failed is None and error is None

    assert run("migrate", "down", "--to", "0002:01", "--yes", "--url", db.url) == 0

    assert (
        "0002_add_slug  02_backfill_author  nothing to reverse: slugs are derived; "
        "nothing to put back" in capsys.readouterr().out
    )
    assert record(db, 2, 2) is None
    assert record(db, 2, 1) is not None
    assert db.rows("SELECT slug FROM author ORDER BY id") == [("ann-lee",), ("bo",)]


def test_an_atomic_down_runs_and_removes_the_record_in_its_transaction(
    project, pkg, db, capsys
):
    applied_with_authors(project, pkg, db)
    slug_step(
        project,
        pkg,
        BACKFILL.replace(
            '@nothing_to_reverse("slugs are derived; nothing to put back")\n'
            "def down(ctx): ...\n",
            "@atomic\nasync def down(ctx):\n"
            '    await ctx.execute("UPDATE author SET slug = NULL")\n',
        ),
    )
    assert run("migrate", "up", "--url", db.url) == 0
    capsys.readouterr()

    assert run("migrate", "down", "--to", "0002:01", "--yes", "--url", db.url) == 0

    assert "02_backfill_author  reverted (" in capsys.readouterr().out
    assert record(db, 2, 2) is None
    assert db.rows("SELECT slug FROM author ORDER BY id") == [(None,), (None,)]


# -- what the step sees --------------------------------------------------------------


def test_a_step_sees_the_union_and_never_todays_models(
    project, pkg, db, tmp_path, capsys
):
    applied_with_authors(project, pkg, db)
    db.execute("INSERT INTO tag (label) VALUES ('poetry')")
    db.execute("INSERT INTO author_tags (author_id, tag_id) VALUES (1, 1)")
    write_models(project, pkg, RENAMED)
    new("nicknames", "--data-step", "Author")
    seen_file = tmp_path / "seen.json"
    step = sorted((migrations(project) / "0002_nicknames").glob("*_backfill_author.py"))
    step[0].write_text(
        textwrap.dedent(
            f"""\
            import json
            from pathlib import Path

            from ferro.migrations import atomic, nothing_to_reverse
            from {pkg}.models import Author


            @atomic
            async def up(ctx):
                seen = {{"fields": sorted(ctx.models.Author.model_fields)}}
                authors = await ctx.models.Author.where(lambda author: author.id == 1).all()
                for author in authors:
                    seen["nickname_before"] = author.nickname
                    author.nickname = author.full_name.split()[0]
                    await author.save()
                links = await ctx.models.table("author_tags").all()
                seen["links"] = [[link.author_id, link.tag_id] for link in links]
                try:
                    Author.where(lambda author: author.id == 1)
                except Exception as err:
                    seen["today"] = str(err)
                Path({str(seen_file)!r}).write_text(json.dumps(seen))


            @nothing_to_reverse("nicknames are derived")
            def down(ctx): ...
            """
        )
    )
    capsys.readouterr()

    assert run("migrate", "up", "--url", db.url) == 0, capsys.readouterr().err

    seen = json.loads(seen_file.read_text())
    assert seen["fields"] == ["full_name", "id", "nickname"]
    assert seen["nickname_before"] is None
    assert seen["links"] == [[1, 1]]
    assert "use ctx.models.Author" in seen["today"]
    assert db.rows("SELECT nickname FROM author WHERE id = 1") == [("Ann",)]
    today = importlib.import_module(f"{pkg}.models").Author
    assert today.where(lambda author: author.id == 1) is not None


def test_a_column_the_migration_drops_is_dropped_by_a_contract_after_the_step(
    project, pkg, db, capsys
):
    # `name` is dropped and `slug` derived from it: the step must still read
    # `name`, so the drop waits for a contract after it (ADR-0025).
    applied_with_authors(project, pkg, db)
    write_models(project, pkg, WITH_SLUG.replace("    name: str\n", ""))
    new("slugs", "--data-step", "Author")
    migration = migrations(project) / "0002_slugs"
    backend = db.backend
    assert sorted(path.name for path in migration.iterdir()) == [
        f"01_schema.down.{backend}.sql",
        f"01_schema.up.{backend}.sql",
        "02_backfill_author.py",
        f"03_contract.down.{backend}.sql",
        f"03_contract.up.{backend}.sql",
        "ir.json",
    ]
    assert (migration / f"01_schema.up.{backend}.sql").read_text() == (
        'ALTER TABLE "author" ADD COLUMN "slug" varchar;\n'
    )
    assert (migration / f"03_contract.up.{backend}.sql").read_text() == (
        '-- ferro: destructive\n\nALTER TABLE "author" DROP COLUMN "name";\n'
    )
    (migration / "02_backfill_author.py").write_text(BACKFILL)
    capsys.readouterr()

    assert run("migrate", "up", "--url", db.url) == 0, capsys.readouterr().err

    out = capsys.readouterr().out.splitlines()
    assert [line.split()[1] for line in out[:3]] == [
        "01_schema",
        "02_backfill_author",
        "03_contract",
    ]
    assert db.rows("SELECT slug FROM author ORDER BY id") == [("ann-lee",), ("bo",)]
    assert plan_against(db, snapshot(project, 2), snapshot(project, 1)) == []


def test_a_sql_step_follows_the_data_step_whether_or_not_the_models_changed(
    project, pkg, db
):
    configure(project, pkg, db.backend)
    write_models(project, pkg, MODELS)
    new("create_author")
    backend = db.backend

    # The models drop `name`: the data step, the contract, then the SQL step.
    write_models(project, pkg, WITH_SLUG.replace("    name: str\n", ""))
    new("slugs", "--data-step", "Author", "--sql-step", "audit")
    assert sorted(
        path.name for path in (migrations(project) / "0002_slugs").iterdir()
    ) == [
        f"01_schema.down.{backend}.sql",
        f"01_schema.up.{backend}.sql",
        "02_backfill_author.py",
        f"03_contract.down.{backend}.sql",
        f"03_contract.up.{backend}.sql",
        "04_audit.down.sql",
        "04_audit.up.sql",
        "ir.json",
    ]

    # The models change nothing: the data step, then the SQL step.
    new("again", "--data-step", "Author", "--sql-step", "audit")
    assert sorted(
        path.name for path in (migrations(project) / "0003_again").iterdir()
    ) == ["01_backfill_author.py", "02_audit.down.sql", "02_audit.up.sql", "ir.json"]


def test_a_nested_transaction_in_a_step_is_a_savepoint(project, pkg, db):
    applied_with_authors(project, pkg, db)
    slug_step(
        project,
        pkg,
        textwrap.dedent(
            """\
            import ferro
            from ferro.migrations import atomic, nothing_to_reverse


            @atomic
            async def up(ctx):
                await ctx.execute("UPDATE author SET slug = 'outer'")
                try:
                    async with ferro.transaction():
                        await ferro.execute("UPDATE author SET name = 'inner'")
                        raise ValueError("the inner block gives up")
                except ValueError:
                    pass


            @nothing_to_reverse("test step")
            def down(ctx): ...
            """
        ),
    )

    assert run("migrate", "up", "--url", db.url) == 0

    assert db.rows("SELECT name, slug FROM author ORDER BY id") == [
        ("Ann Lee", "outer"),
        ("Bo", "outer"),
    ]


# -- refusals --------------------------------------------------------------------------


def test_an_unwritten_step_refuses_the_run_before_anything_is_applied(
    project, pkg, db, capsys
):
    applied_with_authors(project, pkg, db)
    slug_step(project, pkg, None)
    capsys.readouterr()

    assert run("migrate", "up", "--url", db.url) == 1

    err = capsys.readouterr().err
    assert (
        "0002_add_slug/02_backfill_author.py:7: not written yet: write this step" in err
    )
    assert "Nothing was applied." in err
    assert record(db, 2, 1) is None
    assert run("migrate", "check") == 3
    assert (
        "unwritten_step: 0002_add_slug/02_backfill_author.py:7: not written yet: "
        "write this step" in capsys.readouterr().err
    )


def test_an_undeclared_up_refuses_the_run_naming_the_function(project, pkg, db, capsys):
    applied_with_authors(project, pkg, db)
    slug_step(project, pkg, BACKFILL.replace("@atomic\n", ""))
    capsys.readouterr()

    assert run("migrate", "up", "--url", db.url) == 1

    err = capsys.readouterr().err
    assert (
        "0002_add_slug/02_backfill_author.py: up() has no declaration; decorate it "
        "with @atomic or @chunked(...)" in err
    )
    assert record(db, 2, 1) is None


def test_an_irreversible_down_refuses_the_revert_before_anything_is_reverted(
    project, pkg, db, capsys
):
    applied_with_authors(project, pkg, db)
    slug_step(
        project,
        pkg,
        BACKFILL.replace(
            '@nothing_to_reverse("slugs are derived; nothing to put back")',
            '@irreversible("the old slugs are gone")',
        ).replace("nothing_to_reverse", "irreversible"),
    )
    assert run("migrate", "up", "--url", db.url) == 0
    capsys.readouterr()

    assert run("migrate", "down", "--yes", "--url", db.url) == 1

    assert (
        "ferro migrate: 0002:02 is irreversible: the old slugs are gone"
        in capsys.readouterr().err
    )
    assert record(db, 2, 2) is not None and record(db, 2, 1) is not None


# -- failure and interruption ---------------------------------------------------------


def test_a_step_that_raises_commits_nothing_and_is_recorded_failed(
    project, pkg, db, capsys
):
    applied_with_authors(project, pkg, db)
    slug_step(
        project,
        pkg,
        BACKFILL.replace(
            "        await author.save()\n",
            "        await author.save()\n"
            '    raise RuntimeError("the slug service is down")\n',
        ),
    )
    capsys.readouterr()

    assert run("migrate", "up", "--url", db.url) == 1

    err = capsys.readouterr().err
    assert (
        "ferro migrate: 0002_add_slug/02_backfill_author.py failed: RuntimeError: "
        "the slug service is down" in err
    )
    assert db.rows("SELECT slug FROM author ORDER BY id") == [(None,), (None,)]
    *_, kind, _, _, _, _, finished, failed, error = record(db, 2, 2)
    assert kind == "atomic" and finished is None and failed is not None
    assert error == "RuntimeError: the slug service is down"
    today = importlib.import_module(f"{pkg}.models").Author
    assert today.where(lambda author: author.id == 1) is not None


def test_cancelling_a_running_step_rolls_it_back_and_releases_everything(
    project, pkg, db
):
    applied_with_authors(project, pkg, db)
    (project / pkg / "signal.py").write_text(
        "import asyncio\n\nentered = asyncio.Event()\n"
    )
    step = slug_step(
        project,
        pkg,
        textwrap.dedent(
            f"""\
            import asyncio

            from ferro.migrations import atomic, nothing_to_reverse
            from {pkg} import signal


            @atomic
            async def up(ctx):
                await ctx.execute("UPDATE author SET slug = 'half'")
                signal.entered.set()
                await asyncio.sleep(60)


            @nothing_to_reverse("test step")
            def down(ctx): ...
            """
        ),
    )
    sys.path.insert(0, str(project))
    signal = importlib.import_module(f"{pkg}.signal")
    settings = FerroSettings()
    database = settings.database()
    before = dict(REGISTRY.models())

    async def cancelled_run() -> None:
        task = asyncio.create_task(runner.up(settings, database, url=db.url))
        await asyncio.wait_for(signal.entered.wait(), 30)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancelled_run())

    assert db.rows("SELECT slug FROM author ORDER BY id") == [(None,), (None,)]
    assert REGISTRY.models() == before
    # The lock is free: a run that refuses to wait for it applies the step.
    step.write_text(BACKFILL)
    report = asyncio.run(runner.up(settings, database, url=db.url, lock_timeout=0))
    assert report.refusal is None
    assert [s.step for s in report.applied] == ["02_backfill_author"]
    assert db.rows("SELECT slug FROM author ORDER BY id") == [("ann-lee",), ("bo",)]


# -- scaffolding -----------------------------------------------------------------------


def test_data_only_writes_the_step_and_a_full_copy_of_the_parent_snapshot(
    project, pkg, db, capsys
):
    configure(project, pkg, db.backend)
    write_models(project, pkg, MODELS)
    new("create_author")
    write_models(project, pkg, WITH_SLUG)  # a model change --data-only ignores
    capsys.readouterr()

    new("backfill_slugs", "--data-step", "Author", "--data-only")

    migration = migrations(project) / "0002_backfill_slugs"
    assert sorted(p.name for p in migration.iterdir()) == [
        "01_backfill_author.py",
        "ir.json",
    ]
    parent = migrations(project) / "0001_create_author" / "ir.json"
    # Byte for byte the parent's document, save the link back to the parent
    # (a byte copy would break the snapshot chain).
    own = json.loads((migration / "ir.json").read_text())
    copied = json.loads(parent.read_text())
    assert own.pop("parent_checksum") == sha384(parent)
    copied.pop("parent_checksum")
    assert own == copied

    write_models(project, pkg, MODELS)
    (migration / "01_backfill_author.py").write_text(
        BACKFILL.replace("author.slug == None", "author.id == 0").replace(
            "author.slug = ", "author.name = "
        )
    )
    capsys.readouterr()
    assert run("migrate", "check") == 0
    assert capsys.readouterr().out == "ok: models match 0002_backfill_slugs\n"


def test_a_project_template_overrides_the_skeleton(project, pkg, db):
    configure(project, pkg, db.backend)
    write_models(project, pkg, MODELS)
    new("create_author")
    templates = migrations(project) / "_templates"
    templates.mkdir()
    (templates / "data_step.py").write_text(
        "# our house style for {model}\n"
        "from ferro.migrations import atomic, todo\n\n\n"
        "@atomic\nasync def up(ctx):\n"
        '    todo("backfill {model}")\n\n\n'
        "@atomic\nasync def down(ctx):\n"
        '    todo("undo {model}")\n'
    )

    new("backfill", "--data-step", "Author", "--data-only")

    step = migrations(project) / "0002_backfill" / "01_backfill_author.py"
    assert step.read_text().startswith("# our house style for Author\n")
    assert 'todo("backfill Author")' in step.read_text()
    assert run("migrate", "check") == 3


def test_a_data_step_names_a_model_the_snapshot_has(project, pkg, db, capsys):
    configure(project, pkg, db.backend)
    write_models(project, pkg, MODELS)
    new("create_author")
    capsys.readouterr()

    assert run("migrate", "new", "backfill", "--data-step", "Writer") == 1

    assert "no model 'Writer'; it has: Author, Tag, author_tags" in (
        capsys.readouterr().err
    )


# -- the loader ----------------------------------------------------------------------


def _step_file(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "0003_fix" / "01_backfill_author.py"
    path.parent.mkdir(exist_ok=True)
    path.write_text(body)
    return path


def test_the_loader_reads_the_declarations_and_every_todo(tmp_path):
    path = _step_file(
        tmp_path,
        "from ferro.migrations import atomic, nothing_to_reverse, todo as later\n"
        "import ferro.migrations as fm\n\n\n"
        "@atomic\nasync def up(ctx):\n"
        '    x = later("the first value")\n'
        '    y = fm.todo("the second value")\n\n\n'
        '@nothing_to_reverse("derived")\ndef down(ctx): ...\n',
    )

    loaded = load_step(path, sha384(path))

    assert loaded.up.shape == Atomic()
    assert loaded.down.shape == NothingToReverse("derived")
    assert loaded.todos == [(7, "the first value"), (8, "the second value")]
    with pytest.raises(NotWrittenError, match="not written yet: the first value"):
        todo("the first value")


@pytest.mark.parametrize(
    ("up", "expected"),
    [
        ("async def up(ctx): ...", r"up\(\) has no declaration"),
        (
            "@atomic\n@chunked(lambda models: models.Author.select(), batch_size=5)\n"
            "async def up(ctx): ...",
            r"up\(\) carries @chunked and @atomic; .* keep one",
        ),
        (
            '@irreversible("no")\ndef up(ctx): ...',
            r"up\(\) is declared @irreversible, which only a down can be",
        ),
        (
            "@atomic\ndef up(ctx): ...",
            r"up\(\) is declared @atomic, so it runs: write it as `async def up\(\.\.\.\)`",
        ),
    ],
)
def test_the_loader_refuses_a_misdeclared_up_naming_the_file_and_function(
    tmp_path, up, expected
):
    path = _step_file(
        tmp_path,
        "from ferro.migrations import atomic, chunked, irreversible, "
        "nothing_to_reverse\n\n\n"
        f"{up}\n\n\n"
        '@nothing_to_reverse("derived")\ndef down(ctx): ...\n',
    )

    with pytest.raises(
        StepRefused, match=rf"0003_fix/01_backfill_author\.py: {expected}"
    ):
        load_step(path, None)


def test_the_loader_refuses_a_file_edited_since_it_was_planned(tmp_path):
    path = _step_file(tmp_path, BACKFILL)
    planned = sha384(path)
    path.write_text(BACKFILL + "\n# edited\n")

    with pytest.raises(StepRefused, match=f"planned with sha384:{planned}"):
        load_step(path, planned)
