# ruff: noqa: F811 - pytest fixtures imported from a sibling module are redefined as arguments
"""``ferro.migrations.testing`` (#535): a project's tests prove its chain.

```python
h = harness()
await h.apply_through("0002")
async with h.models_at("0002") as models:
    await models.Author(name="Ada").save()
await h.apply("0003")                       # 0003 adds slug and backfills it
async with h.models_at("0003") as models:
    assert (await models.Author.where(lambda author: author.name == "Ada").first()).slug == "ada"
await h.round_trip()
```

Every test builds a real three-migration project under ``tmp_path`` with
``ferro migrate new`` and drives it on the parametrized database (SQLite and
Postgres):

```text
0001_create_author   hrn_tag, hrn_author, their many-to-many join table
0002_index_name      an index on hrn_author.name (its own step on Postgres)
0003_add_slug        hrn_author.slug, then a data step backfilling it
```
"""

from __future__ import annotations

import importlib
import json
import re
from pathlib import Path

import pytest

import ferro
from ferro import _core
from ferro.migrations.errors import MigrationRefused
from ferro.migrations.testing import Harness, RoundTripResult, harness
from ferro.registry import SwappedOutModelError
from ferro.settings import FerroSettings
from tests.test_migrate_new import (  # noqa: F401 - fixtures
    isolated_imports,
    pkg,
    project,
    write_models,
)
from tests.test_migrate_up import configure, db, migrations, new  # noqa: F401

pytestmark = [
    pytest.mark.usefixtures("isolated_imports", "clean_registry"),
    pytest.mark.backend_matrix,
]

CREATE = """
from ferro import ManyToMany


class Tag(Model):
    __ferro_table__ = "hrn_tag"
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    label: str
    authors: Relation[list["Author"]] = BackRef()


class Author(Model):
    __ferro_table__ = "hrn_author"
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    name: str
    tags: Relation[list[Tag]] = ManyToMany(related_name="authors")
"""

INDEXED = CREATE.replace(
    "    name: str\n", "    name: Annotated[str, FerroField(index=True)]\n"
)

WITH_SLUG = INDEXED + "    slug: str | None = None\n"

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


def build_chain(project: Path, pkg: str, backend: str) -> None:
    """The three migrations, generated the way a developer would."""
    configure(project, pkg, backend)
    write_models(project, pkg, CREATE)
    new("create_author")
    write_models(project, pkg, INDEXED)
    new("index_name")
    write_models(project, pkg, WITH_SLUG)
    new("add_slug", "--data-step", "Author")
    (step,) = (migrations(project) / "0003_add_slug").glob("*.py")
    step.write_text(BACKFILL)
    importlib.import_module(f"{pkg}.models")  # today's classes, registered


def migration_dir(project: Path, prefix: str) -> Path:
    (found,) = migrations(project).glob(f"{prefix}_*")
    return found


def todays(pkg: str, name: str) -> type:
    return getattr(importlib.import_module(f"{pkg}.models"), name)


@pytest.fixture
def chain(project, pkg, db):
    build_chain(project, pkg, db.backend)
    return project


@pytest.fixture
async def connected(chain, db):
    await ferro.connect(db.url)
    yield db
    ferro.reset_engine()


@pytest.fixture
def migrations_settings(chain) -> FerroSettings:
    return FerroSettings()


async def tracking_exists() -> bool:
    return json.loads(await _core._read_records(None, None))["exists"]


def applied(db) -> list[tuple[int, int]]:
    return [
        (row[0], row[1])
        for row in db.rows(
            "SELECT migration, step FROM _ferro_migrations ORDER BY migration, step"
        )
    ]


def columns(db, table: str) -> set[str]:
    if db.backend == "sqlite":
        return {row[1] for row in db.rows(f'PRAGMA table_info("{table}")')}
    return {
        row[0]
        for row in db.rows(
            "SELECT column_name FROM information_schema.columns "
            f"WHERE table_schema = '{db.schema}' AND table_name = '{table}'"
        )
    }


# -- the happy path -----------------------------------------------------------------


async def test_the_docstring_example_end_to_end(connected, migrations_settings):
    h = harness(settings=migrations_settings, database="default")
    await h.apply_through("0002")
    async with h.models_at("0002") as models:
        await models.Author(name="Ada").save()
    await h.apply("0003")
    async with h.models_at("0003") as models:
        assert (await models.Author.where(lambda a: a.name == "Ada").first()).slug == (
            "ada"
        )
    await h.revert_to("0002")
    result = await h.round_trip()

    assert isinstance(result, RoundTripResult)


async def test_apply_through_applies_every_pending_migration_up_to_it_and_no_further(
    connected,
):
    h = harness()

    report = await h.apply_through("0002")

    assert {step.migration for step in report.applied} == {
        "0001_create_author",
        "0002_index_name",
    }
    assert {m for m, _ in applied(connected)} == {1, 2}
    assert "slug" not in columns(connected, "hrn_author")
    assert (await h.apply_through("0002")).applied == []


async def test_round_trip_over_a_chain_with_a_data_step(connected):
    result = await harness().round_trip()

    assert result == RoundTripResult(
        applied=["0001_create_author", "0002_index_name", "0003_add_slug"],
        reverted_to=None,
        irreversible=None,
    )
    assert {m for m, _ in applied(connected)} == {1, 2, 3}


async def test_models_at_is_that_one_snapshot_and_reaches_join_tables(connected):
    h = harness()
    await h.apply_through("0002")

    async with h.models_at("0002") as models:
        assert "slug" not in models.Author.model_fields
        assert models.rev == "0002_index_name"
        join = [t for t in models.tables() if t not in ("hrn_author", "hrn_tag")]
        assert len(join) == 1
        link = models.table(join[0])
        author, tag = models.Author(name="Ada"), models.Tag(label="poetry")
        await author.save()
        await tag.save()
        await link(author_id=author.id, tag_id=tag.id).save()
        assert [(row.author_id, row.tag_id) for row in await link.all()] == [
            (author.id, tag.id)
        ]

    async with h.models_at("0003") as models:
        assert "slug" in models.Author.model_fields


# -- refusals -----------------------------------------------------------------------


async def test_apply_refuses_unless_the_database_stands_at_the_parent(connected):
    h = harness()
    await h.apply_through("0001")

    with pytest.raises(MigrationRefused) as refused:
        await h.apply("0003")

    message = str(refused.value)
    assert "0001_create_author" in message and "0002_index_name" in message
    assert {m for m, _ in applied(connected)} == {1}

    await h.apply("0002")
    report = await h.apply("0003")

    assert {step.migration for step in report.applied} == {"0003_add_slug"}


async def test_apply_on_an_empty_database_refuses_a_migration_with_a_parent(connected):
    with pytest.raises(MigrationRefused, match="0001_create_author"):
        await harness().apply("0002")

    assert not await tracking_exists()


async def test_an_unknown_migration_is_refused_naming_the_chain(connected):
    with pytest.raises(MigrationRefused, match="0001_create_author"):
        await harness().apply_through("0009")


async def test_revert_to_and_apply_through_refuse_the_wrong_direction(connected):
    h = harness()
    await h.apply_through("0003")

    with pytest.raises(MigrationRefused, match="0003_add_slug"):
        await h.apply_through("0001")

    await h.revert_to("0001")
    assert {m for m, _ in applied(connected)} == {1}
    with pytest.raises(MigrationRefused, match="0001_create_author"):
        await h.revert_to("0002")


async def test_revert_all_on_an_empty_database_is_a_no_op_creating_nothing(connected):
    h = harness()

    report = await h.revert_all()

    assert report.reverted == [] and report.refusal is None
    assert not await tracking_exists()


async def test_revert_all_reverts_everything(connected):
    h = harness()
    await h.apply_through("0003")

    report = await h.revert_all()

    assert [step.migration for step in report.reverted][0] == "0003_add_slug"
    assert applied(connected) == []
    assert "hrn_author" not in connected.tables()


async def test_the_harness_creates_no_tracking_tables_until_it_mutates(connected):
    h = harness()
    async with h.models_at("0001"):
        pass
    with pytest.raises(MigrationRefused):
        await h.apply("0003")

    assert not await tracking_exists()

    await h.apply("0001")

    assert await tracking_exists()


# -- round trip ---------------------------------------------------------------------


async def test_an_irreversible_step_ends_the_downward_walk_and_the_chain_reapplies(
    chain, connected, db
):
    down = next(migration_dir(chain, "0002").glob(f"*.down.{db.backend}.sql"))
    down.write_text(
        "-- ferro: irreversible the index is shared\n" + down.read_text(), "utf-8"
    )

    result = await harness().round_trip()

    stem = re.sub(r"\.down\..*$", "", down.name)
    assert result == RoundTripResult(
        applied=["0001_create_author", "0002_index_name", "0003_add_slug"],
        reverted_to="0002_index_name",
        irreversible=("0002_index_name", stem, "the index is shared"),
    )
    assert {m for m, _ in applied(db)} == {1, 2, 3}


async def test_drift_by_hand_is_refused_with_the_lines(connected):
    h = harness()
    await h.apply_through("0003")
    connected.execute('ALTER TABLE "hrn_author" DROP COLUMN "slug"')

    with pytest.raises(MigrationRefused, match=r"hrn_author\.slug column is missing"):
        await h.round_trip()


async def test_a_down_that_leaves_a_column_is_drift_at_its_stop(chain, connected, db):
    (down,) = migration_dir(chain, "0003").glob(f"*.down.{db.backend}.sql")
    down.write_text("SELECT 1;\n", "utf-8")

    with pytest.raises(
        MigrationRefused, match=r"(?s)0003_add_slug.*hrn_author\.slug column is extra"
    ):
        await harness().round_trip()


# -- today's classes ----------------------------------------------------------------


async def test_todays_classes_are_unreachable_inside_models_at_and_restored_after(
    connected, pkg
):
    h = harness()
    await h.apply_through("0003")
    author = todays(pkg, "Author")

    async with h.models_at("0002"):
        with pytest.raises(SwappedOutModelError, match=r"models\.Author"):
            await author.all()

    assert await author.all() == []

    with pytest.raises(RuntimeError, match="boom"):
        async with h.models_at("0002"):
            raise RuntimeError("boom")

    assert await author.all() == []


def test_harness_is_bound_once(chain):
    h = harness(settings=FerroSettings(), database="default", using="elsewhere")

    assert isinstance(h, Harness)
