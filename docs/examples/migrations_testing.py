# ruff: noqa: E402 - the models come first, as the docs page shows them
"""Runnable companion for testing migrations (docs/pages/guide/schema/testing.md).

Generates two migrations (the second backfills a new required column), then
runs the page's two harness tests against fresh SQLite files.
"""

# --8<-- [start:models]
from ferro import Field, Model


class Author(Model):
    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    slug: str  # new


# --8<-- [end:models]


# --8<-- [start:tests]
import ferro
from ferro.migrations.testing import harness


async def test_the_slug_backfill(database_url):
    await ferro.connect(database_url)
    h = harness()
    await h.apply_through("0001")
    async with h.models_at("0001") as models:  # the table as 0001 left it
        await models.Author(name="Ada Lovelace").save()
    await h.apply("0002")  # refused unless the database stands at 0001
    async with h.models_at("0002") as models:
        ada = await models.Author.where(
            lambda author: author.name == "Ada Lovelace"
        ).first()
        assert ada.slug == "ada-lovelace"
    await h.revert_to("0001")


async def test_every_migration_round_trips(database_url):
    await ferro.connect(database_url)
    result = await harness().round_trip()
    assert result.irreversible is None


# --8<-- [end:tests]


import asyncio
import os

from _migrations_project import project

TODO = '        author.slug = todo("the slug for an existing author")\n'
WRITTEN = '        author.slug = author.name.lower().replace(" ", "-")\n'


def main() -> None:
    with project(__file__) as app:
        app.models_before()
        app.ferro("migrate", "new", "create_author")
        app.models_after()
        app.ferro("migrate", "new", "author_slug")
        backfill = app.migrations / "0002_author_slug" / "02_backfill_author.py"
        backfill.write_text(backfill.read_text().replace(TODO, WRITTEN))

        # The harness reads the project's config from the working directory,
        # as it does under pytest run from the project root.
        cwd = os.getcwd()
        os.chdir(app.root)
        try:
            for number, test in enumerate(
                (test_the_slug_backfill, test_every_migration_round_trips)
            ):
                asyncio.run(test(f"sqlite:{app.root / f'test{number}.db'}?mode=rwc"))
                ferro.reset_engine()
        finally:
            os.chdir(cwd)


if __name__ == "__main__":
    main()
