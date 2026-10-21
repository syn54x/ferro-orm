# ruff: noqa: E402, F811 - the models come first, and each of the page's files imports what it uses
"""Runnable companion for testing migrations (docs/pages/guide/schema/testing.md).

Generates two migrations (the second backfills a new required column), then
runs the page's tests under pytest, in the project, the way a reader runs
them: its ``conftest.py`` fixture, its test module and its pytest settings.
Then it declares the backfill's down irreversible and runs the page's round
trip that stops there.
"""

# --8<-- [start:models]
from typing import Annotated

from ferro import Field, Model


class Author(Model):
    id: Annotated[int | None, Field(primary_key=True)] = None
    name: Annotated[str, Field(index=True)]
    slug: str  # new


# --8<-- [end:models]


# --8<-- [start:fixture]
import pytest

import ferro


@pytest.fixture
def database_url(tmp_path):
    yield f"sqlite:{tmp_path / 'test.db'}?mode=rwc"
    ferro.reset_engine()  # close the test's connection


# --8<-- [end:fixture]


# --8<-- [start:tests]
import ferro
from ferro.migrations.testing import harness


async def test_the_slug_backfill(database_url):
    await ferro.connect(database_url)
    migrations = harness()
    await migrations.apply_through("0001")
    async with migrations.models_at("0001") as models:  # the table as 0001 left it
        await models.Author(name="Ada Lovelace").save()
    await migrations.apply("0002")  # refused unless the database stands at 0001
    async with migrations.models_at("0002") as models:
        ada = await models.Author.where(
            lambda author: author.name == "Ada Lovelace"
        ).first()
        assert ada.slug == "ada-lovelace"
    await migrations.revert_to("0001")


async def test_every_migration_round_trips(database_url):
    await ferro.connect(database_url)
    result = await harness().round_trip()
    assert result.irreversible is None


# --8<-- [end:tests]


# --8<-- [start:irreversible]
import ferro
from ferro.migrations.testing import RoundTripResult, harness


async def test_the_round_trip_stops_at_the_irreversible_step(database_url):
    await ferro.connect(database_url)
    result = await harness().round_trip()
    assert result == RoundTripResult(
        applied=["0001_create_author", "0002_author_slug"],
        reverted_to="0002_author_slug",
        irreversible=(
            "0002_author_slug",
            "02_backfill_author",
            "the old slugs are gone",
        ),
    )


# --8<-- [end:irreversible]


import os
import subprocess
import sys
from pathlib import Path

from _migrations_project import _region, project

TODO = '        author.slug = todo("the slug for an existing author")\n'
WRITTEN = '        author.slug = author.name.lower().replace(" ", "-")\n'
NOTHING_TO_REVERSE = '@nothing_to_reverse("01_expand.down.sql drops the column")'
IRREVERSIBLE = '@irreversible("the old slugs are gone")'
PYTEST_SETTINGS = '[tool.pytest.ini_options]\nasyncio_mode = "auto"\n'


def run_pytest(app, *modules: str, passed: int) -> None:
    """``pytest`` over ``modules`` in the project, as a reader runs it."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *modules],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=app.root,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"{passed} passed" in result.stdout, result.stdout


def main() -> None:
    script = Path(__file__)
    with project(__file__) as app:
        app.models_before()
        app.ferro("migrate", "new", "create_author")
        app.models_after()
        app.ferro("migrate", "new", "author_slug")
        backfill = app.migrations / "0002_author_slug" / "02_backfill_author.py"
        backfill.write_text(backfill.read_text().replace(TODO, WRITTEN))

        # The page's files, in the project: pytest runs from its root, where the
        # harness reads the project's configuration, as every ferro command does.
        (app.root / "pyproject.toml").write_text(PYTEST_SETTINGS)
        (app.root / "conftest.py").write_text(_region(script, "fixture"))
        (app.root / "test_migrations.py").write_text(_region(script, "tests"))
        run_pytest(app, "test_migrations.py", passed=2)

        # A backfill whose down is declared irreversible ends the downward walk.
        source = backfill.read_text()
        assert NOTHING_TO_REVERSE in source
        backfill.write_text(
            source.replace(NOTHING_TO_REVERSE, IRREVERSIBLE).replace(
                "import chunked, nothing_to_reverse, todo",
                "import chunked, irreversible, todo",
            )
        )
        (app.root / "test_round_trip.py").write_text(_region(script, "irreversible"))
        run_pytest(app, "test_round_trip.py", passed=1)


if __name__ == "__main__":
    main()
